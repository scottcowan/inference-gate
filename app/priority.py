from enum import Enum
from fastapi import Request
from .gpu import GpuState


class Priority(str, Enum):
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


def should_allow(priority: Priority, gpu: GpuState, high_util_threshold: int) -> bool:
    """Return True if the request should be forwarded given current GPU state."""
    if priority == Priority.REALTIME:
        return True
    if priority == Priority.HIGH:
        return gpu.free or gpu.utilization_pct <= high_util_threshold
    # normal / low — only allow when no external consumers at all
    return gpu.free
