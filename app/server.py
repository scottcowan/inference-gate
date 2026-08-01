from __future__ import annotations

import asyncio
import logging
from enum import StrEnum

from .priority import is_effectively_free
from .windows_gpu import DEFAULT_DESKTOP_GPU_PROCESSES, non_desktop_consumers

logger = logging.getLogger(__name__)

# When process stop is enabled, also stop these related runners (Windows Ollama stack).
_RELATED_RUNNERS: dict[str, tuple[str, ...]] = {
    "ollama": ("ollama", "llama-server", "ollama_llama_server", "ollama app"),
}


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
        self._stopped_processes = False
        self._models_to_preload: list[str] = []

    @property
    def state(self) -> ServerState:
        return self._state

    def accepting(self) -> bool:
        return self._state == ServerState.RUNNING

    def remember_models(self, models: list[str]) -> None:
        """Record models to preload after the next restart (e.g. from app.state.last_model)."""
        names = [m for m in models if m]
        if names:
            self._models_to_preload = list(dict.fromkeys(names))

    async def acquire(self) -> bool:
        """Increment in-flight counter. Returns False if not accepting requests."""
        async with self._lock:
            if self._state != ServerState.RUNNING:
                return False
            self._in_flight += 1
            self._drained.clear()
            return True

    async def release(self) -> None:
        """Decrement in-flight counter. Triggers stop when draining and counter hits zero."""
        async with self._lock:
            self._in_flight = max(0, self._in_flight - 1)
            if self._in_flight == 0:
                self._drained.set()
                if self._state == ServerState.DRAINING:
                    asyncio.ensure_future(self._stop_server())

    async def begin_drain(self, reason: str | None = None) -> None:
        async with self._lock:
            if self._state != ServerState.RUNNING:
                return
            if reason:
                logger.info("server-manager: GPU pressure detected (%s) — draining", reason)
            else:
                logger.info("server-manager: GPU pressure detected — draining")
            self._state = ServerState.DRAINING
            if self._in_flight == 0:
                asyncio.ensure_future(self._stop_server())

    async def _snapshot_loaded_models(self) -> list[str]:
        """Read currently loaded upstream models so we can preload them after restart."""
        import httpx

        base = self._settings.upstream_url.rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0)) as client:
                r = await client.get(f"{base}/api/ps")
                r.raise_for_status()
                models = [m.get("name") for m in (r.json().get("models") or []) if m.get("name")]
                if models:
                    logger.info("server-manager: remembering models for preload: %s", models)
                return models
        except Exception:
            logger.warning("server-manager: could not snapshot loaded models", exc_info=True)
            return list(self._models_to_preload)

    async def _preload_models(self) -> None:
        """Load remembered models while still STARTING (clients still get 429)."""
        if not self._settings.server_preload_on_start:
            return

        models = list(self._models_to_preload)
        fallback = self._settings.server_preload_model
        if not models and fallback:
            models = [fallback]
        if not models:
            logger.info("server-manager: nothing to preload")
            return

        import httpx

        base = self._settings.upstream_url.rstrip("/")
        keep_alive = self._settings.server_preload_keep_alive
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=5.0)) as client:
                for model in models:
                    logger.info(
                        "server-manager: preloading model %r (keep_alive=%s) before opening gate",
                        model,
                        keep_alive,
                    )
                    await client.post(
                        f"{base}/api/generate",
                        json={
                            "model": model,
                            "prompt": "hi",
                            "stream": False,
                            "keep_alive": keep_alive,
                        },
                    )
                # Confirm at least one model is resident
                r = await client.get(f"{base}/api/ps")
                loaded = [m.get("name") for m in (r.json().get("models") or []) if m.get("name")]
                logger.info("server-manager: preload complete — loaded=%s", loaded or "(none)")
        except Exception:
            logger.warning("server-manager: model preload failed", exc_info=True)

    async def _unload_models(self) -> None:
        """Ask the upstream to drop loaded models so VRAM frees without killing processes."""
        import httpx

        base = self._settings.upstream_url.rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=5.0)) as client:
                r = await client.get(f"{base}/api/ps")
                r.raise_for_status()
                models = [m.get("name") for m in (r.json().get("models") or []) if m.get("name")]
                if not models:
                    logger.info("server-manager: no models loaded to unload")
                    return
                for model in models:
                    logger.info("server-manager: unloading model %r", model)
                    await client.post(
                        f"{base}/api/generate",
                        json={"model": model, "keep_alive": 0},
                    )
                logger.info("server-manager: model unload requested for %d model(s)", len(models))
        except Exception:
            logger.warning("server-manager: graceful model unload failed", exc_info=True)

    async def _wait_models_gone(self, timeout_secs: float = 30.0) -> bool:
        """Poll /api/ps until empty so the runner can exit before we stop ollama."""
        import httpx

        base = self._settings.upstream_url.rstrip("/")
        deadline = asyncio.get_running_loop().time() + timeout_secs
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0)) as client:
                while asyncio.get_running_loop().time() < deadline:
                    try:
                        r = await client.get(f"{base}/api/ps")
                        r.raise_for_status()
                        models = r.json().get("models") or []
                        if not models:
                            logger.info("server-manager: upstream reports no loaded models")
                            # Brief settle so llama-server can exit and release CUDA.
                            await asyncio.sleep(1.5)
                            return True
                    except Exception:
                        # Upstream already down — treat as cleared.
                        return True
                    await asyncio.sleep(0.5)
        except Exception:
            logger.warning("server-manager: wait-for-unload failed", exc_info=True)
        logger.warning("server-manager: models still present after %.0fs wait", timeout_secs)
        return False

    async def _upstream_healthy(self) -> bool:
        import httpx

        base = self._settings.upstream_url.rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(2.0, connect=1.0)) as client:
                r = await client.get(f"{base}/api/tags")
                return r.status_code < 500
        except Exception:
            return False

    def _target_process_names(self) -> set[str]:
        primary = (self._settings.server_process or "").lower().removesuffix(".exe")
        names = {primary} if primary else set()
        names.update(_RELATED_RUNNERS.get(primary, ()))
        return {n for n in names if n}

    async def _terminate_processes(self) -> None:
        targets = self._target_process_names()
        if not targets:
            return
        force = bool(self._settings.server_force_kill)
        logger.info(
            "server-manager: stopping processes %s (force_kill=%s)",
            sorted(targets),
            force,
        )
        try:
            import psutil

            matched: list = []
            for proc in psutil.process_iter(["name", "pid"]):
                name = (proc.info["name"] or "").lower().removesuffix(".exe")
                if name in targets:
                    matched.append(proc)

            if not matched:
                logger.info("server-manager: no matching LLM processes to stop")
                return

            for proc in matched:
                try:
                    logger.info(
                        "server-manager: terminating pid %d (%s)",
                        proc.pid,
                        proc.info["name"],
                    )
                    proc.terminate()
                except Exception:
                    logger.warning(
                        "server-manager: terminate failed for pid %d",
                        proc.pid,
                        exc_info=True,
                    )

            # Short wait — game launch path needs the GPU free ASAP.
            wait_secs = 3 if force else 15
            _, alive = await asyncio.to_thread(psutil.wait_procs, matched, timeout=wait_secs)
            if alive and force:
                for proc in alive:
                    try:
                        logger.warning(
                            "server-manager: force-killing pid %d (%s) after timeout",
                            proc.pid,
                            proc.info.get("name") if hasattr(proc, "info") else "?",
                        )
                        proc.kill()
                    except Exception:
                        logger.warning(
                            "server-manager: kill failed for pid %d",
                            proc.pid,
                            exc_info=True,
                        )
            elif alive:
                logger.warning(
                    "server-manager: %d process(es) still alive after terminate "
                    "(SERVER_FORCE_KILL=false) — leaving them",
                    len(alive),
                )
            self._stopped_processes = True
        except Exception:
            logger.warning("server-manager: process stop failed", exc_info=True)

    async def _stop_server(self) -> None:
        self._stopped_processes = False

        # Remember what was loaded so we can preload before clearing 429 on return.
        snapped = await self._snapshot_loaded_models()
        if snapped:
            self._models_to_preload = snapped

        # Kill-first when enabled: match "stop the Ollama container before the game
        # grabs DXGI". Unload-then-wait races exclusive fullscreen.
        if self._settings.server_kill_processes and self._settings.server_process:
            logger.info(
                "server-manager: shutting down LLM server (%s)",
                self._settings.server_process,
            )
            await self._terminate_processes()
        else:
            logger.info("server-manager: shutting down inference (unload models only)")
            await self._unload_models()
            await self._wait_models_gone()
            if self._settings.server_process:
                logger.info(
                    "server-manager: leaving %r running (SERVER_KILL_PROCESSES=false) — models unloaded only",
                    self._settings.server_process,
                )

        async with self._lock:
            self._state = ServerState.DOWN
        logger.info("server-manager: gate closed (inference blocked until GPU free)")

    async def start(self) -> None:
        async with self._lock:
            if self._state != ServerState.DOWN:
                return
            self._state = ServerState.STARTING

        should_start = (
            self._settings.server_kill_processes
            and self._settings.server_process
            and self._settings.server_start_command
            and self._stopped_processes
        ) or (
            # Also start if upstream is down (we stopped it, or it died).
            self._settings.server_start_command
            and self._settings.server_kill_processes
            and not await self._upstream_healthy()
        )

        if should_start and self._settings.server_start_command:
            start_cmd = self._settings.server_start_command
            logger.info("server-manager: starting server: %r", start_cmd)
            try:
                await asyncio.create_subprocess_shell(
                    start_cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                healthy = False
                for _ in range(45):
                    await asyncio.sleep(1)
                    if await self._upstream_healthy():
                        healthy = True
                        break
                if healthy:
                    logger.info("server-manager: upstream healthy after start")
                    await self._preload_models()
                else:
                    logger.warning("server-manager: upstream not healthy after start wait")
            except Exception:
                logger.warning("server-manager: start failed", exc_info=True)
        else:
            # Upstream may still be running (unload-only mode) — still preload if needed.
            if await self._upstream_healthy():
                await self._preload_models()
            logger.info(
                "server-manager: re-opening gate (upstream left running; preload attempted if configured)"
            )

        async with self._lock:
            self._state = ServerState.RUNNING
            self._stopped_processes = False
        logger.info("server-manager: gate open (accepting requests)")

    def to_dict(self) -> dict:
        return {"state": self._state, "in_flight": self._in_flight}


async def watch_gpu(app) -> None:
    """Background task — polls GPU state and drives ServerManager transitions."""
    settings = app.state.settings
    manager: ServerManager = app.state.server_manager
    free_streak = 0.0
    poll_secs = 1.0

    while True:
        await asyncio.sleep(poll_secs)
        try:
            gpu = await app.state.gpu_query()
            game_active = not is_effectively_free(
                gpu,
                settings.external_vram_threshold_mb,
                settings.external_util_fallback_threshold,
            )

            if game_active:
                free_streak = 0.0
                if manager.state == ServerState.RUNNING:
                    # Prefer app-tracked last model if /api/ps snapshot is empty later
                    last = getattr(app.state, "last_model", None)
                    if last:
                        manager.remember_models([last])
                    heavy = non_desktop_consumers(
                        gpu.external_consumers, DEFAULT_DESKTOP_GPU_PROCESSES
                    )
                    reason = ", ".join(
                        f"{p.get('name')}={p.get('mem_mb') or 0}MB" for p in heavy[:6]
                    ) or "unknown"
                    await manager.begin_drain(reason=reason)
            else:
                free_streak += poll_secs
                if (
                    manager.state == ServerState.DOWN
                    and free_streak >= settings.server_restart_stable_secs
                ):
                    await manager.start()
                    free_streak = 0.0
        except Exception:
            logger.warning("watch_gpu: error", exc_info=True)
