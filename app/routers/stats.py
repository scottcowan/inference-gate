from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/gpu")
async def gpu_stats(request: Request) -> JSONResponse:
    """Raw GPU state — used by external monitors and the pipeline scheduler."""
    gpu = request.app.state.gpu_query()
    return JSONResponse(gpu.to_dict())
