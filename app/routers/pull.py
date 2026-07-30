from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

logger = logging.getLogger(__name__)

router = APIRouter()


async def _run_pull(request: Request, model: str, command: str):
    pull_ready = request.app.state.pull_ready
    pull_ready.clear()  # block inference requests
    cmd = command.replace("{model}", model)
    logger.info("pull: running %r", cmd)
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert proc.stdout is not None
        async for line in proc.stdout:
            yield json.dumps({"status": line.decode().rstrip(), "done": False}).encode() + b"\n"
        await proc.wait()
        if proc.returncode == 0:
            yield json.dumps({"status": "done", "done": True}).encode() + b"\n"
        else:
            yield json.dumps({"status": f"pull failed (exit {proc.returncode})", "done": False, "error": True}).encode() + b"\n"
    finally:
        pull_ready.set()  # release held inference requests


@router.post("/api/pull", response_model=None)
async def pull(request: Request) -> StreamingResponse | JSONResponse:
    settings = request.app.state.settings

    # No pull_command configured — pass through to upstream like any other request.
    if not settings.pull_command:
        from .proxy import _forward
        body = await request.body()
        return await _forward(request, "POST", "/api/pull", body or None)

    body = await request.body()
    try:
        model = json.loads(body).get("model", "") if body else ""
    except ValueError:
        model = ""

    if not model:
        return JSONResponse(status_code=422, content={"error": "model field required"})

    return StreamingResponse(
        _run_pull(request, model, settings.pull_command),
        media_type="application/x-ndjson",
    )
