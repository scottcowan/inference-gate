import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from .config import get_settings
from .gpu import GpuState, query_gpu
from .routers.proxy import router as proxy_router
from .routers.pull import router as pull_router
from .routers.stats import router as stats_router

logger = logging.getLogger(__name__)

_GPU_TTL = 1.0  # seconds between gpustat calls

_gpu_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gpu-query")


def _make_gpu_query(settings) -> "callable":
    """Return an async gpu_query with a 1s TTL cache backed by a single-thread executor."""
    _cache: dict = {"state": None, "at": 0.0}
    _lock = asyncio.Lock()

    async def gpu_query_cached() -> GpuState:
        now = time.monotonic()
        if _cache["state"] is not None and now - _cache["at"] < _GPU_TTL:
            return _cache["state"]
        async with _lock:
            now = time.monotonic()
            if _cache["state"] is not None and now - _cache["at"] < _GPU_TTL:
                return _cache["state"]
            loop = asyncio.get_running_loop()
            state = await loop.run_in_executor(
                _gpu_executor,
                lambda: query_gpu(settings.ignored_gpu_processes),
            )
            _cache["state"] = state
            _cache["at"] = time.monotonic()
            return state

    return gpu_query_cached


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    app.state.gpu_query = _make_gpu_query(settings)
    app.state.http_client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=5.0))
    app.state.last_model = None
    app.state.pull_ready = asyncio.Event()
    app.state.pull_ready.set()  # starts ready — not pulling
    app.state.model_ready = asyncio.Event()
    app.state.model_ready.set()  # starts ready — no pending model load
    app.state._release_task = None
    logger.info("inference-gate starting — upstream: %s", settings.upstream_url)
    yield
    await app.state.http_client.aclose()


app = FastAPI(title="inference-gate", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}


app.include_router(stats_router)  # GET /gpu
app.include_router(pull_router)  # POST /api/pull — before catch-all
app.include_router(proxy_router)  # catch-all — must be last
