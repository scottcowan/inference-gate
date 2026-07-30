import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import get_settings
from .gpu import query_gpu
from .routers.proxy import router as proxy_router
from .routers.stats import router as stats_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    # Bind gpu_query with settings so routers don't need to import settings themselves
    app.state.gpu_query = lambda: query_gpu(settings.ignored_gpu_processes)
    logger.info("ollama-gpu-proxy starting — upstream: %s", settings.ollama_url)
    yield


app = FastAPI(title="ollama-gpu-proxy", version="0.1.0", lifespan=lifespan)

@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}


app.include_router(stats_router)   # GET /gpu  (stats endpoint)
app.include_router(proxy_router)   # catch-all — must be last
