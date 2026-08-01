from enum import StrEnum

from fastapi import Request

from .gpu import GpuState
from .windows_gpu import DEFAULT_DESKTOP_GPU_PROCESSES, non_desktop_consumers


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


def external_vram_mb(gpu: GpuState) -> int:
    return sum(p.get("mem_mb") or 0 for p in gpu.external_consumers)


def process_vram_available(consumers: list[dict]) -> bool:
    """True when at least one consumer reported nonzero per-process VRAM."""
    return any((p.get("mem_mb") or 0) > 0 for p in consumers)


def _vram_numbers_trustworthy(consumers: list[dict]) -> bool:
    """NVML-style per-process VRAM is usable; Windows PDH often under-reports games."""
    if not process_vram_available(consumers):
        return False
    # PDH "Dedicated Usage" can show tens of MB for a fullscreen title that needs GBs.
    return not any(p.get("mem_source") == "pdh" for p in consumers)


def is_effectively_free(
    gpu: GpuState,
    external_vram_threshold_mb: int = 0,
    external_util_fallback_threshold: int = 40,
    desktop_gpu_processes: frozenset[str] | set[str] | None = None,
) -> bool:
    """Whether the GPU is free enough for non-realtime inference / to keep the LLM up.

    Prefer per-process VRAM when NVML reports it. On Windows WDDM, NVML usually
    returns mem_mb=0 and PDH numbers are unreliable for games — then any
    non-desktop GPU process (game, render app) counts as busy immediately.
    No per-launcher / per-title name lists — desktop allowlist only.
    """
    if gpu.free:
        return True

    desktop = (
        desktop_gpu_processes
        if desktop_gpu_processes is not None
        else DEFAULT_DESKTOP_GPU_PROCESSES
    )
    heavy = non_desktop_consumers(gpu.external_consumers, desktop)

    # Idle desktop: only DWM/browsers/etc. on the GPU
    if not heavy:
        return True

    # Accurate NVML-style numbers (typical on Linux) — use threshold on heavy consumers
    if _vram_numbers_trustworthy(heavy):
        total = sum(p.get("mem_mb") or 0 for p in heavy)
        return total <= external_vram_threshold_mb

    # Windows WDDM / PDH: presence of a non-desktop GPU process is enough
    # (game launch often sits at low util / low PDH while still needing the card).
    _ = external_util_fallback_threshold
    return False


def should_allow(
    priority: Priority,
    gpu: GpuState,
    high_util_threshold: int,
    external_vram_threshold_mb: int = 0,
    external_util_fallback_threshold: int = 40,
    desktop_gpu_processes: frozenset[str] | set[str] | None = None,
) -> bool:
    """Return True if the request should be forwarded given current GPU state."""
    if priority == Priority.REALTIME:
        return True
    effectively_free = is_effectively_free(
        gpu,
        external_vram_threshold_mb,
        external_util_fallback_threshold,
        desktop_gpu_processes,
    )
    if priority == Priority.HIGH:
        return effectively_free or gpu.utilization_pct <= high_util_threshold
    # normal / low — only allow when effectively free
    return effectively_free
