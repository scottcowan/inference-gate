"""Tests for app/routers/proxy.py."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.gpu import GpuState
from app.main import app

FREE_GPU = GpuState(0, 0, 16376, 0.0, [], True)
BUSY_GPU = GpuState(94, 12000, 16376, 73.3, [{"name": "game.exe", "mem_mb": 11200}], False)


def _async_gpu(state: GpuState):
    async def _q():
        return state

    return _q


@pytest.fixture(autouse=True)
def setup_settings():
    from app.config import get_settings

    get_settings.cache_clear()
    app.state.settings = get_settings()


# ---------------------------------------------------------------------------
# Helpers shared by async tests
# ---------------------------------------------------------------------------


async def _setup_async_state(gpu: GpuState = FREE_GPU):
    """Replicate what lifespan does so async tests don't need to enter it."""
    from app.config import get_settings
    from app.server import ServerManager

    get_settings.cache_clear()
    settings = get_settings()
    app.state.settings = settings
    app.state.http_client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=5.0))
    app.state.gpu_query = _async_gpu(gpu)
    app.state.last_model = None
    app.state.pull_ready = asyncio.Event()
    app.state.pull_ready.set()
    app.state.model_ready = asyncio.Event()
    app.state.model_ready.set()
    app.state._release_task = None
    app.state.server_manager = ServerManager(settings)


async def _teardown_async_state():
    await app.state.http_client.aclose()


# ---------------------------------------------------------------------------
# Sync tests (original suite)
# ---------------------------------------------------------------------------


def test_health():
    with TestClient(app) as client:
        app.state.gpu_query = _async_gpu(FREE_GPU)
        r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_stats_endpoint():
    with TestClient(app) as client:
        app.state.gpu_query = _async_gpu(FREE_GPU)
        r = client.get("/gpu")
    assert r.status_code == 200
    data = r.json()
    assert data["free"] is True
    assert data["external_consumers"] == []


def test_normal_request_blocked_when_busy():
    with TestClient(app) as client:
        app.state.gpu_query = _async_gpu(BUSY_GPU)
        r = client.post("/api/generate", json={"model": "qwen2.5:7b", "prompt": "hi"})
    assert r.status_code == 429
    assert "Retry-After" in r.headers


def test_realtime_request_always_forwarded():
    with respx.mock:
        respx.post("http://localhost:11434/api/generate").mock(
            return_value=httpx.Response(200, json={"response": "ok"})
        )
        with TestClient(app) as client:
            app.state.gpu_query = _async_gpu(BUSY_GPU)
            r = client.post(
                "/api/generate",
                json={"model": "qwen2.5:7b", "prompt": "hi"},
                headers={"X-Priority": "realtime"},
            )
    assert r.status_code == 200


def test_normal_request_forwarded_when_free():
    with respx.mock:
        respx.post("http://localhost:11434/api/generate").mock(
            return_value=httpx.Response(200, json={"response": "ok"})
        )
        with TestClient(app) as client:
            app.state.gpu_query = _async_gpu(FREE_GPU)
            r = client.post("/api/generate", json={"model": "qwen2.5:7b", "prompt": "hi"})
    assert r.status_code == 200


def test_unknown_priority_header_treated_as_normal():
    with TestClient(app) as client:
        app.state.gpu_query = _async_gpu(BUSY_GPU)
        r = client.post(
            "/api/generate",
            json={"model": "qwen2.5:7b", "prompt": "hi"},
            headers={"X-Priority": "garbage"},
        )
    assert r.status_code == 429


# ---------------------------------------------------------------------------
# pull_ready hold behaviour — async so Event lives in the same event loop
# ---------------------------------------------------------------------------


async def test_pull_ready_hold_blocks_then_releases():
    """Requests block while pull_ready is cleared, succeed once it is set."""
    await _setup_async_state()
    try:
        app.state.pull_ready.clear()

        async def release():
            await asyncio.sleep(0.05)
            app.state.pull_ready.set()

        with respx.mock:
            respx.post("http://localhost:11434/api/generate").mock(
                return_value=httpx.Response(200, json={"response": "ok"})
            )
            release_task = asyncio.create_task(release())
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                r = await client.post(
                    "/api/generate",
                    json={"model": "qwen2.5:7b", "prompt": "hi"},
                )
            await release_task
    finally:
        await _teardown_async_state()

    assert r.status_code == 200


async def test_pull_hold_timeout_returns_503():
    """When pull_ready stays cleared past the timeout, proxy returns 503."""
    await _setup_async_state()
    try:
        app.state.settings.pull_hold_timeout_secs = 0.001
        app.state.pull_ready.clear()

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            r = await client.post(
                "/api/generate",
                json={"model": "qwen2.5:7b", "prompt": "hi"},
            )
    finally:
        await _teardown_async_state()

    assert r.status_code == 503
    assert "timed out" in r.json()["error"]


# ---------------------------------------------------------------------------
# _check_model_change / model_ready gate — async for same event-loop access
# ---------------------------------------------------------------------------


async def test_model_change_clears_model_ready():
    """Switching model must clear model_ready so subsequent requests are held."""
    await _setup_async_state()
    try:
        app.state.settings.model_load_immunity_secs = 30  # long enough not to fire during test
        app.state.last_model = "model-a"

        with respx.mock:
            respx.post("http://localhost:11434/api/generate").mock(
                return_value=httpx.Response(200, json={"response": "ok"})
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                r = await client.post(
                    "/api/generate",
                    json={"model": "model-b", "prompt": "hi"},
                )

        assert r.status_code == 200
        # model_ready must have been cleared by _check_model_change
        assert app.state.model_ready.is_set() is False
    finally:
        # Cancel the pending release task and await it so the event loop drains cleanly.
        task = getattr(app.state, "_release_task", None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await _teardown_async_state()


async def test_model_release_after_fires_and_sets_model_ready():
    """After model_load_immunity_secs elapses _release_after must set model_ready."""
    await _setup_async_state()
    try:
        app.state.settings.model_load_immunity_secs = 0.05
        app.state.last_model = "model-a"

        with respx.mock:
            respx.post("http://localhost:11434/api/generate").mock(
                return_value=httpx.Response(200, json={"response": "ok"})
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                await client.post(
                    "/api/generate",
                    json={"model": "model-b", "prompt": "hi"},
                )

        # model_ready is cleared; wait for _release_after to fire
        await asyncio.wait_for(app.state.model_ready.wait(), timeout=2.0)
        assert app.state.model_ready.is_set() is True
    finally:
        await _teardown_async_state()


async def test_first_request_does_not_clear_model_ready():
    """When last_model is None (first request), model_ready must remain set."""
    await _setup_async_state()
    try:
        app.state.last_model = None  # confirmed fresh state

        with respx.mock:
            respx.post("http://localhost:11434/api/generate").mock(
                return_value=httpx.Response(200, json={"response": "ok"})
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                await client.post(
                    "/api/generate",
                    json={"model": "any-model", "prompt": "hi"},
                )

        assert app.state.model_ready.is_set() is True
    finally:
        await _teardown_async_state()


async def test_model_change_guard_when_model_ready_already_unset():
    """If model_ready is already cleared, a second model-switch must cancel the old
    _release_after task and schedule exactly one new one — no duplicate tasks."""
    await _setup_async_state()
    try:
        # Use a very short window so tasks finish quickly and don't stall teardown.
        app.state.settings.model_load_immunity_secs = 0.05
        app.state.last_model = "model-a"

        ensure_future_calls: list = []
        original_ensure_future = asyncio.ensure_future

        def _tracking_ensure_future(coro, **kwargs):
            t = original_ensure_future(coro, **kwargs)
            ensure_future_calls.append(t)
            return t

        with patch("asyncio.ensure_future", side_effect=_tracking_ensure_future):
            with respx.mock:
                respx.post("http://localhost:11434/api/generate").mock(
                    return_value=httpx.Response(200, json={"response": "ok"})
                )
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://test",
                ) as client:
                    # First switch: clears model_ready, schedules _release_after
                    await client.post(
                        "/api/generate",
                        json={"model": "model-b", "prompt": "hi"},
                    )
                    first_count = len(ensure_future_calls)

                    # model_ready is now unset; second switch arrives
                    await client.post(
                        "/api/generate",
                        json={"model": "model-c", "prompt": "hi"},
                    )
                    second_count = len(ensure_future_calls)

        # Each model switch schedules exactly one _release_after task
        assert first_count == 1
        assert second_count == 2  # one new task per switch (old one is cancelled first)
    finally:
        task = getattr(app.state, "_release_task", None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await _teardown_async_state()


# ---------------------------------------------------------------------------
# Streaming proxy path
# ---------------------------------------------------------------------------


def test_streaming_request_proxied_correctly():
    """POST with stream=true must be forwarded via the async streaming path."""
    ndjson_chunk = b'{"response":"hello","done":false}\n'
    ndjson_end = b'{"response":"","done":true}\n'
    raw_body = ndjson_chunk + ndjson_end

    with respx.mock:
        respx.post("http://localhost:11434/api/generate").mock(
            return_value=httpx.Response(
                200,
                content=raw_body,
                headers={"content-type": "application/x-ndjson"},
            )
        )
        with TestClient(app) as client:
            app.state.gpu_query = _async_gpu(FREE_GPU)
            r = client.post(
                "/api/generate",
                json={"model": "qwen2.5:7b", "stream": True, "prompt": "hi"},
            )

    assert r.status_code == 200
    assert "ndjson" in r.headers.get("content-type", "")
    assert b"hello" in r.content


async def test_low_priority_first_request_allowed():
    """Cold start: low priority must be allowed when no model is loaded yet."""
    await _setup_async_state()
    try:
        app.state.last_model = None
        with respx.mock:
            respx.post("http://localhost:11434/api/generate").mock(
                return_value=httpx.Response(200, json={"response": "ok"})
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                r = await client.post(
                    "/api/generate",
                    headers={"X-Priority": "low"},
                    json={"model": "gemma4:12b", "prompt": "hi"},
                )
        assert r.status_code == 200
        assert app.state.last_model == "gemma4:12b"
    finally:
        await _teardown_async_state()


async def test_low_priority_model_switch_rejected():
    """Low priority cannot switch away from an already-loaded model."""
    await _setup_async_state()
    try:
        app.state.last_model = "model-a"
        with respx.mock:
            respx.post("http://localhost:11434/api/generate").mock(
                return_value=httpx.Response(200, json={"response": "ok"})
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                r = await client.post(
                    "/api/generate",
                    headers={"X-Priority": "low"},
                    json={"model": "model-b", "prompt": "hi"},
                )
        assert r.status_code == 429
        assert "cannot switch" in r.json()["error"]
    finally:
        await _teardown_async_state()


async def test_upstream_timeout_returns_504():
    """ReadTimeout from upstream must become 504, not an unhandled 500."""
    await _setup_async_state()
    try:

        async def _timeout(*_args, **_kwargs):
            raise httpx.ReadTimeout("slow")

        with patch.object(app.state.http_client, "request", side_effect=_timeout):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                r = await client.post(
                    "/api/generate",
                    json={"model": "gemma4:12b", "prompt": "hi", "stream": False},
                )
        assert r.status_code == 504
    finally:
        await _teardown_async_state()


async def test_down_state_returns_429_including_realtime():
    """Killing Ollama must not break the gate — clients still get 429."""
    from app.server import ServerState

    await _setup_async_state()
    try:
        app.state.server_manager._state = ServerState.DOWN
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            r = await client.post(
                "/api/generate",
                headers={"X-Priority": "realtime"},
                json={"model": "gemma4:12b", "prompt": "hi"},
            )
            head = await client.head("/api/generate")
        assert r.status_code == 429
        assert r.json()["error"]
        assert "Retry-After" in r.headers
        assert head.status_code == 429
    finally:
        await _teardown_async_state()


async def test_connect_error_while_busy_returns_429():
    """If Ollama is killed mid-request while GPU is busy, return 429 not 500."""
    await _setup_async_state(BUSY_GPU)
    try:

        async def _connect_fail(*_args, **_kwargs):
            raise httpx.ConnectError("ollama gone")

        with patch.object(app.state.http_client, "request", side_effect=_connect_fail):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                r = await client.post(
                    "/api/generate",
                    headers={"X-Priority": "realtime"},
                    json={"model": "gemma4:12b", "prompt": "hi", "stream": False},
                )
        assert r.status_code == 429
    finally:
        await _teardown_async_state()
