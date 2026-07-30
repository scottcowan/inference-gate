from enum import StrEnum

from fastapi import Request

from .gpu import GpuState


class Priority(StrEnum):
    REALTIME = "realtime"
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


def get_priority(request: Request) -> Priority:
    value = request.headers.get("X-Priority", "normal").lower()
    try:
        return Priority(value)
    except ValueError:
        return Priority.NORMAL


def should_allow(
    priority: Priority,
    gpu: GpuState,
    high_util_threshold: int,
    external_vram_threshold_mb: int = 0,
) -> bool:
    """Return True if the request should be forwarded given current GPU state."""
    if priority == Priority.REALTIME:
        return True
    total_external_mb = sum(p.get("mem_mb", 0) for p in gpu.external_consumers)
    effectively_free = gpu.free or total_external_mb <= external_vram_threshold_mb
    if priority == Priority.HIGH:
        return effectively_free or gpu.utilization_pct <= high_util_threshold
    # normal / low — only allow when effectively free
    return effectively_free
