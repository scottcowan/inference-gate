from __future__ import annotations

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ..priority import get_priority, should_allow

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
    url = f"{settings.ollama_url}{path}"

    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length", "x-priority")
    }

    is_streaming = False
    if body:
        import json
        try:
            parsed = json.loads(body)
            is_streaming = bool(parsed.get("stream", False))
        except (ValueError, KeyError):
            pass

    if is_streaming:
        async def _stream():
            async with httpx.AsyncClient() as client:
                async with client.stream(
                    method, url, content=body, headers=headers, timeout=None
                ) as upstream:
                    async for chunk in upstream.aiter_bytes():
                        yield chunk

        return StreamingResponse(_stream(), media_type="application/x-ndjson")

    async with httpx.AsyncClient() as client:
        upstream = await client.request(
            method, url, content=body, headers=headers, timeout=120
        )
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=dict(upstream.headers),
    )


@router.api_route("/{path:path}", methods=["GET", "POST", "DELETE", "HEAD"])
async def proxy(request: Request, path: str) -> Response:
    settings = request.app.state.settings
    priority = get_priority(request)
    gpu = request.app.state.gpu_query()

    if not should_allow(priority, gpu, settings.high_priority_util_threshold):
        return _make_429()

    body = await request.body()
    return await _forward(request, request.method, f"/{path}", body or None)
