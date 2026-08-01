from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ..priority import (
    Priority,
    external_vram_mb,
    get_priority,
    is_effectively_free,
    should_allow,
)
from ..server import ServerManager

router = APIRouter()

# Retry-After seconds returned with 429 responses
RETRY_AFTER = "10"


def _make_429() -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"error": "GPU busy — retry after GPU is free"},
        headers={"Retry-After": RETRY_AFTER},
    )


async def _forward(request: Request, method: str, path: str, body: bytes | None) -> Response:
    settings = request.app.state.settings
    client: httpx.AsyncClient = request.app.state.http_client
    url = f"{settings.upstream_url}{path}"

    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length", "x-priority")
    }

    is_streaming = False
    if body:
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                is_streaming = bool(parsed.get("stream", False))
        except ValueError:
            pass

    if is_streaming:
        _ct: list[str] = []
        _queue: asyncio.Queue[bytes | BaseException | None] = asyncio.Queue()

        async def _produce() -> None:
            _headers_sent = False
            try:
                async with client.stream(
                    method, url, content=body, headers=headers, timeout=None
                ) as upstream:
                    _ct.append(upstream.headers.get("content-type", "application/octet-stream"))
                    _queue.put_nowait(None)  # headers-ready signal
                    _headers_sent = True
                    async for chunk in upstream.aiter_bytes():
                        await _queue.put(chunk)
                await _queue.put(b"")  # end sentinel
            except BaseException as exc:
                if not _headers_sent:
                    _ct.append("application/octet-stream")
                    _queue.put_nowait(None)
                await _queue.put(exc)

        task = asyncio.create_task(_produce())
        await _queue.get()  # wait until upstream headers are known
        content_type = _ct[0] if _ct else "application/octet-stream"

        async def _consume() -> AsyncIterator[bytes]:
            while True:
                item = await _queue.get()
                if item == b"":
                    break
                if item is None:
                    raise RuntimeError("unexpected None in streaming queue")
                if isinstance(item, BaseException):
                    raise item
                yield item
            await task

        async def _stream_with_cleanup() -> AsyncIterator[bytes]:
            try:
                async for chunk in _consume():
                    yield chunk
            finally:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

        return StreamingResponse(_stream_with_cleanup(), media_type=content_type)

    try:
        upstream = await client.request(method, url, content=body, headers=headers)
    except httpx.TimeoutException:
        return JSONResponse(
            status_code=504,
            content={"error": "upstream timed out waiting for LLM server"},
        )
    except httpx.ConnectError:
        # Upstream may have been stopped to free the GPU — caller maps this to 429
        # when the gate is draining/down or the GPU is busy.
        raise
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=dict(upstream.headers),
    )


def _check_model_change(request: Request, body: bytes, priority: Priority) -> JSONResponse | None:
    """On a model switch, hold subsequent requests until the load window expires.

    Low-priority requests are rejected with 429 rather than triggering a model load —
    they run on whatever model is already loaded or not at all. The first request
    (last_model is None) is allowed for every priority so cold start works.
    """
    try:
        parsed = json.loads(body)
        model = parsed.get("model") if isinstance(parsed, dict) else None
    except (ValueError, AttributeError):
        return None
    if not model or model == request.app.state.last_model:
        return None
    prev = request.app.state.last_model
    if prev is None:
        request.app.state.last_model = model
        return None  # first request — no load window needed
    if priority == Priority.LOW:
        return JSONResponse(
            status_code=429,
            content={"error": "low-priority requests cannot switch models"},
            headers={"Retry-After": RETRY_AFTER},
        )
    request.app.state.last_model = model
    secs = request.app.state.settings.model_load_immunity_secs
    model_ready: asyncio.Event = request.app.state.model_ready
    old_task = request.app.state._release_task
    if old_task and not old_task.done():
        old_task.cancel()
    model_ready.clear()
    request.app.state._release_task = asyncio.ensure_future(_release_after(model_ready, secs))
    return None


async def _release_after(event: asyncio.Event, secs: float) -> None:
    await asyncio.sleep(secs)
    event.set()


def _gate_status_response(manager: ServerManager, gpu, settings) -> Response:
    """HEAD preflight: returns gate status without forwarding or counting as in-flight."""
    server = manager.to_dict()
    total_external_mb = external_vram_mb(gpu)
    accepting = manager.accepting()
    gpu_free = is_effectively_free(
        gpu,
        settings.external_vram_threshold_mb,
        settings.external_util_fallback_threshold,
    )
    status = 200 if accepting and gpu_free else 429
    return Response(
        status_code=status,
        headers={
            "X-Server-State": server["state"],
            "X-In-Flight": str(server["in_flight"]),
            "X-GPU-Utilization": str(gpu.utilization_pct),
            "X-External-VRAM-MB": str(total_external_mb),
        },
    )


@router.api_route("/{path:path}", methods=["GET", "POST", "DELETE", "HEAD"])
async def proxy(request: Request, path: str) -> Response:
    settings = request.app.state.settings
    priority = get_priority(request)
    manager: ServerManager = request.app.state.server_manager

    # HEAD — gate status check, no forwarding, no in-flight tracking
    if request.method == "HEAD":
        gpu = await request.app.state.gpu_query()
        return _gate_status_response(manager, gpu, settings)

    # Server draining or down — gate stays up and 429s (Ollama may be killed).
    if not await manager.acquire():
        return _make_429()

    try:
        pull_ready: asyncio.Event = request.app.state.pull_ready
        model_ready: asyncio.Event = request.app.state.model_ready
        if not pull_ready.is_set() or not model_ready.is_set():
            try:
                await asyncio.wait_for(
                    asyncio.gather(pull_ready.wait(), model_ready.wait()),
                    timeout=settings.pull_hold_timeout_secs,
                )
            except TimeoutError:
                return JSONResponse(
                    status_code=503,
                    content={"error": "timed out waiting for model pull to complete"},
                )

        body = await request.body()
        if body:
            if (resp := _check_model_change(request, body, priority)) is not None:
                return resp

        gpu = await request.app.state.gpu_query()
        if not should_allow(
            priority,
            gpu,
            settings.high_priority_util_threshold,
            settings.external_vram_threshold_mb,
            settings.external_util_fallback_threshold,
        ):
            return _make_429()

        try:
            return await _forward(request, request.method, f"/{path}", body or None)
        except httpx.ConnectError:
            # Race: Ollama killed for a game while this request was mid-flight.
            # Prefer 429 so clients back off instead of treating the gate as dead.
            if not manager.accepting() or not is_effectively_free(
                gpu,
                settings.external_vram_threshold_mb,
                settings.external_util_fallback_threshold,
            ):
                return _make_429()
            return JSONResponse(
                status_code=502,
                content={"error": "upstream unreachable"},
            )
    finally:
        await manager.release()
