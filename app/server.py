from __future__ import annotations

import asyncio
import logging
from enum import StrEnum

logger = logging.getLogger(__name__)


class ServerState(StrEnum):
    RUNNING = "running"
    DRAINING = "draining"
    DOWN = "down"
    STARTING = "starting"


class ServerManager:
    """
    Tracks LLM server lifecycle and in-flight request count.

    State transitions:
      RUNNING -> DRAINING  when GPU pressure exceeds threshold (game detected)
      DRAINING -> DOWN     when in-flight counter reaches zero
      DOWN -> STARTING     when GPU pressure drops (game gone)
      STARTING -> RUNNING  when the start command exits successfully
    """

    def __init__(self, settings):
        self._settings = settings
        self._state = ServerState.RUNNING
        self._in_flight = 0
        self._lock = asyncio.Lock()
        self._drained = asyncio.Event()
        self._drained.set()

    @property
    def state(self) -> ServerState:
        return self._state

    def accepting(self) -> bool:
        return self._state == ServerState.RUNNING

    async def acquire(self) -> bool:
        """Increment in-flight counter. Returns False if not accepting requests."""
        async with self._lock:
            if self._state != ServerState.RUNNING:
                return False
            self._in_flight += 1
            self._drained.clear()
            return True

    async def release(self) -> None:
        """Decrement in-flight counter. Triggers kill when draining and counter hits zero."""
        async with self._lock:
            self._in_flight = max(0, self._in_flight - 1)
            if self._in_flight == 0:
                self._drained.set()
                if self._state == ServerState.DRAINING:
                    asyncio.ensure_future(self._kill())

    async def begin_drain(self) -> None:
        async with self._lock:
            if self._state != ServerState.RUNNING:
                return
            logger.info("server-manager: GPU pressure detected — draining")
            self._state = ServerState.DRAINING
            if self._in_flight == 0:
                asyncio.ensure_future(self._kill())

    async def _kill(self) -> None:
        process_name = self._settings.server_process
        if not process_name:
            async with self._lock:
                self._state = ServerState.DOWN
            logger.info("server-manager: no SERVER_PROCESS configured — marking down")
            return
        logger.info("server-manager: killing %r", process_name)
        try:
            import psutil

            for proc in psutil.process_iter(["name"]):
                name = (proc.info["name"] or "").lower().removesuffix(".exe")
                if name == process_name.lower().removesuffix(".exe"):
                    proc.kill()
                    logger.info("server-manager: killed pid %d (%s)", proc.pid, proc.info["name"])
        except Exception:
            logger.warning("server-manager: kill failed", exc_info=True)
        async with self._lock:
            self._state = ServerState.DOWN
        logger.info("server-manager: server is down")

    async def start(self) -> None:
        async with self._lock:
            if self._state != ServerState.DOWN:
                return
            self._state = ServerState.STARTING
        start_cmd = self._settings.server_start_command
        if not start_cmd:
            logger.info("server-manager: no SERVER_START_COMMAND — marking running")
            async with self._lock:
                self._state = ServerState.RUNNING
            return
        logger.info("server-manager: starting server: %r", start_cmd)
        try:
            proc = await asyncio.create_subprocess_shell(start_cmd)
            await proc.wait()
            if proc.returncode == 0:
                logger.info("server-manager: server started")
            else:
                logger.warning("server-manager: start command exited %d", proc.returncode)
        except Exception:
            logger.warning("server-manager: start failed", exc_info=True)
        async with self._lock:
            self._state = ServerState.RUNNING

    def to_dict(self) -> dict:
        return {"state": self._state, "in_flight": self._in_flight}


async def watch_gpu(app) -> None:
    """Background task — polls GPU state and drives ServerManager transitions."""
    settings = app.state.settings
    manager: ServerManager = app.state.server_manager

    while True:
        await asyncio.sleep(1.0)
        try:
            gpu = await app.state.gpu_query()
            total_external_mb = sum(p.get("mem_mb", 0) for p in gpu.external_consumers)
            game_active = total_external_mb > settings.external_vram_threshold_mb

            if game_active and manager.state == ServerState.RUNNING:
                await manager.begin_drain()
            elif not game_active and manager.state == ServerState.DOWN:
                await manager.start()
        except Exception:
            logger.warning("watch_gpu: error", exc_info=True)
