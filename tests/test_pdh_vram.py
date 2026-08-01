"""Tests for Windows PDH VRAM enrichment and process-name normalization."""

from unittest.mock import MagicMock, patch

from app.gpu import _enrich_mem_from_pdh, query_gpu


def test_enrich_mem_from_pdh_overlays_by_pid():
    external = [
        {"name": "watch_dogs", "mem_mb": 0, "pid": 100},
        {"name": "chrome", "mem_mb": 0, "pid": 200},
    ]
    with patch("app.pdh_vram.dedicated_mb_by_pid", return_value={100: 4096.4, 200: 80.2}):
        with patch("sys.platform", "win32"):
            _enrich_mem_from_pdh(external)
    assert external[0]["mem_mb"] == 4096
    assert external[0]["mem_source"] == "pdh"
    assert external[1]["mem_mb"] == 80


def test_enrich_mem_noop_without_pids():
    external = [{"name": "watch_dogs", "mem_mb": 0}]
    with patch("app.pdh_vram.dedicated_mb_by_pid", return_value={100: 4096}):
        with patch("sys.platform", "win32"):
            _enrich_mem_from_pdh(external)
    assert external[0]["mem_mb"] == 0


def _make_gpu(processes: list[dict]) -> MagicMock:
    gpu = MagicMock()
    gpu.utilization = 40
    gpu.memory_used = 3000
    gpu.memory_total = 16376
    gpu.processes = [dict(p) for p in processes]
    return gpu


def test_query_gpu_includes_pid_and_pdh_on_windows():
    proc = {"command": "game.exe", "gpu_memory_usage": None, "username": "u", "pid": 42}
    mock_collection = MagicMock()
    mock_collection.__getitem__ = lambda self, i: _make_gpu([proc])
    mock_gpustat = MagicMock()
    mock_gpustat.GPUStatCollection.new_query.return_value = mock_collection
    with patch.dict("sys.modules", {"gpustat": mock_gpustat}):
        with patch("sys.platform", "win32"):
            with patch("app.pdh_vram.dedicated_mb_by_pid", return_value={42: 5120.0}):
                state = query_gpu([])
    assert state.external_consumers[0]["pid"] == 42
    assert state.external_consumers[0]["mem_mb"] == 5120
    assert state.external_consumers[0]["mem_source"] == "pdh"
