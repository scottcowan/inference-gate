"""Tests for app/routers/pull.py."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.gpu import GpuState
from app.main import app

FREE_GPU = GpuState(0, 0, 16376, 0.0, [], True)


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
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_process(lines: list[bytes], returncode: int = 0):
    """Return a mock subprocess whose stdout yields *lines* and exits with *returncode*."""

    async def _readline_gen():
        for line in lines:
            yield line

    stdout = MagicMock()
    stdout.__aiter__ = lambda self: _readline_gen()

    proc = MagicMock()
    proc.stdout = stdout
    proc.returncode = returncode

    async def _wait():
        proc.returncode = returncode

    proc.wait = _wait
    return proc


# ---------------------------------------------------------------------------
# Happy-path streaming pull
# ---------------------------------------------------------------------------


def test_pull_streaming_success():
    """200 NDJSON stream; final line has done=True; pull_ready is set after."""
    fake_proc = _make_fake_process(
        [b"pulling manifest\n", b"verifying sha256 digest\n"],
        returncode=0,
    )

    with patch("asyncio.create_subprocess_shell", new=AsyncMock(return_value=fake_proc)):
        with TestClient(app) as client:
            app.state.settings.pull_command = "fake-pull {model}"
            r = client.post("/api/pull", json={"model": "qwen2.5:7b"})

    assert r.status_code == 200
    assert "ndjson" in r.headers.get("content-type", "")

    lines = [json.loads(line) for line in r.content.splitlines() if line.strip()]
    assert len(lines) >= 2
    # All but the last line should have done=False
    for row in lines[:-1]:
        assert row["done"] is False
    # Final line should be the done sentinel
    assert lines[-1]["done"] is True
    # pull_ready must be re-set after the response body is consumed
    assert app.state.pull_ready.is_set() is True


# ---------------------------------------------------------------------------
# Subprocess failure — finally block re-sets pull_ready
# ---------------------------------------------------------------------------


def test_pull_subprocess_failure_resets_pull_ready():
    """On subprocess failure pull_ready must still be set (finally block guarantee)."""
    fake_proc = _make_fake_process([b"something went wrong\n"], returncode=1)

    with patch("asyncio.create_subprocess_shell", new=AsyncMock(return_value=fake_proc)):
        with TestClient(app) as client:
            app.state.settings.pull_command = "fake-pull {model}"
            r = client.post("/api/pull", json={"model": "qwen2.5:7b"})

    lines = [json.loads(line) for line in r.content.splitlines() if line.strip()]
    # Last line must indicate an error
    assert lines[-1].get("error") is True
    # pull_ready must be restored even on failure
    assert app.state.pull_ready.is_set() is True


# ---------------------------------------------------------------------------
# pull_ready cleared at start of _run_pull
# ---------------------------------------------------------------------------


def test_pull_clears_pull_ready_during_run():
    """pull_ready must be False while the subprocess is running."""
    start_event = asyncio.Event()
    mid_event = asyncio.Event()

    async def _readline_gen():
        start_event.set()          # signal that pull started
        await mid_event.wait()     # pause mid-stream
        yield b"line\n"

    stdout = MagicMock()
    stdout.__aiter__ = lambda self: _readline_gen()

    fake_proc = MagicMock()
    fake_proc.stdout = stdout
    fake_proc.returncode = 0

    async def _wait():
        fake_proc.returncode = 0

    fake_proc.wait = _wait

    observed: list[bool] = []

    # Use a streaming client so we can interleave checks.
    with patch("asyncio.create_subprocess_shell", new=AsyncMock(return_value=fake_proc)):
        with TestClient(app) as client:
            app.state.settings.pull_command = "fake-pull {model}"

            # Kick off the pull in a thread (TestClient is synchronous).
            import threading

            result: list = []

            def _do_pull():
                result.append(client.post("/api/pull", json={"model": "qwen2.5:7b"}))

            t = threading.Thread(target=_do_pull, daemon=True)
            t.start()

            # Wait until the generator has cleared pull_ready and is paused mid-stream.
            import time
            deadline = time.monotonic() + 2.0
            while not start_event.is_set():
                time.sleep(0.005)
                if time.monotonic() > deadline:
                    break

            # At this point pull_ready should be cleared.
            observed.append(app.state.pull_ready.is_set())

            # Release the generator so the request can finish.
            mid_event.set()
            t.join(timeout=2)

    assert observed == [False], f"pull_ready was not cleared during pull: observed={observed}"
    assert app.state.pull_ready.is_set() is True


# ---------------------------------------------------------------------------
# Passthrough when pull_command is None
# ---------------------------------------------------------------------------


def test_pull_passthrough_when_no_pull_command():
    """When pull_command is None, /api/pull must be forwarded to upstream."""
    with respx.mock:
        respx.post("http://localhost:11434/api/pull").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )
        with TestClient(app) as client:
            app.state.settings.pull_command = None
            app.state.gpu_query = _async_gpu(FREE_GPU)
            r = client.post("/api/pull", json={"model": "qwen2.5:7b"})

    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# 422 for missing model field
# ---------------------------------------------------------------------------


def test_pull_missing_model_returns_422():
    """POST /api/pull with no model field must return 422."""
    with TestClient(app) as client:
        app.state.settings.pull_command = "fake-pull {model}"
        r = client.post("/api/pull", json={})

    assert r.status_code == 422
    assert "error" in r.json()
