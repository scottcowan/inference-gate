import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class GpuState:
    utilization_pct: int
    memory_used_mb: int
    memory_total_mb: int
    memory_used_pct: float
    external_consumers: list[dict]
    free: bool

    def to_dict(self) -> dict:
        return {
            "gpu_utilization_pct": self.utilization_pct,
            "memory_used_mb": self.memory_used_mb,
            "memory_total_mb": self.memory_total_mb,
            "memory_used_pct": self.memory_used_pct,
            "external_consumers": self.external_consumers,
            "free": self.free,
        }


def query_gpu(ignored_processes: list[str]) -> GpuState:
    try:
        from gpustat import GPUStatCollection

        stats = GPUStatCollection.new_query()
        gpu = stats[0]

        ignored = {p.lower() for p in ignored_processes}
        external = []
        for p in gpu.processes:
            raw = p.get("command") or p.get("full_command") or p.get("username") or ""
            name = raw.split("/")[-1].split("\\")[-1].lower()
            if name not in ignored:
                external.append({"name": name, "mem_mb": p.get("gpu_memory_usage", 0)})

        mem_total = gpu.memory_total or 1
        return GpuState(
            utilization_pct=gpu.utilization or 0,
            memory_used_mb=gpu.memory_used or 0,
            memory_total_mb=mem_total,
            memory_used_pct=round((gpu.memory_used or 0) / mem_total * 100, 1),
            external_consumers=external,
            free=len(external) == 0,
        )
    except Exception:
        # No GPU visible from inside the container (e.g. dev environment without nvidia runtime).
        # Report as free so the proxy doesn't block all requests during local dev.
        logger.warning("gpustat query failed — reporting GPU as free", exc_info=True)
        return GpuState(
            utilization_pct=0,
            memory_used_mb=0,
            memory_total_mb=0,
            memory_used_pct=0.0,
            external_consumers=[],
            free=True,
        )
