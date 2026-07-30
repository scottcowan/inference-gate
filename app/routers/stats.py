from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/gpu")
async def gpu_stats(request: Request) -> JSONResponse:
    """Raw GPU state — used by external monitors and the pipeline scheduler."""
    gpu = await request.app.state.gpu_query()
    data = gpu.to_dict()
    data["server"] = request.app.state.server_manager.to_dict()
    return JSONResponse(data)
