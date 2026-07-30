"""Tests for gpu.py process parsing (gpustat returns dicts) and config validators."""

from unittest.mock import MagicMock, patch

from app.config import Settings
from app.gpu import query_gpu


def _make_gpu(processes: list[dict]) -> MagicMock:
    gpu = MagicMock()
    gpu.utilization = 40
    gpu.memory_used = 3000
    gpu.memory_total = 16376
    gpu.processes = [dict(p) for p in processes]
    return gpu


def _patched_query(processes, ignored):
    """Run query_gpu with gpustat mocked to return the given processes."""
    mock_collection = MagicMock()
    mock_collection.__getitem__ = lambda self, i: _make_gpu(processes)
    mock_gpustat = MagicMock()
    mock_gpustat.GPUStatCollection.new_query.return_value = mock_collection
    with patch.dict("sys.modules", {"gpustat": mock_gpustat}):
        return query_gpu(ignored)


def test_external_process_detected():
    """game.exe should appear as an external consumer."""
    proc = {"command": "game.exe", "gpu_memory_usage": 8000, "username": "user"}
    state = _patched_query([proc], ["docker", "ollama"])
    assert len(state.external_consumers) == 1
    assert state.external_consumers[0]["name"] == "game.exe"
    assert not state.free


def test_ignored_process_excluded():
    """ollama itself must not appear as an external consumer."""
    proc = {"command": "ollama", "gpu_memory_usage": 4000, "username": "root"}
    state = _patched_query([proc], ["docker", "ollama"])
    assert state.external_consumers == []
    assert state.free


def test_full_path_stripped():
    """/usr/bin/game.exe → game.exe for matching."""
    proc = {"command": "/usr/bin/game.exe", "gpu_memory_usage": 500, "username": "u"}
    state = _patched_query([proc], [])
    assert state.external_consumers[0]["name"] == "game.exe"


def test_missing_command_key_falls_back_to_username():
    """Processes without 'command' key should use username as fallback."""
    proc = {"gpu_memory_usage": 200, "username": "someuser"}
    state = _patched_query([proc], ["docker"])
    assert state.external_consumers[0]["name"] == "someuser"


def test_gpustat_failure_reports_free():
    """Any gpustat exception → fail-open (free=True) so proxy doesn't stall."""
    mock_gpustat = MagicMock()
    mock_gpustat.GPUStatCollection.new_query.side_effect = RuntimeError("no gpu")
    with patch.dict("sys.modules", {"gpustat": mock_gpustat}):
        state = query_gpu([])
    assert state.free


# --- config validator (BL-2) ---


def test_ignored_list_accepts_json_string():
    s = Settings(ignored_gpu_processes='["foo", "bar"]')
    assert s.ignored_gpu_processes == ["foo", "bar"]


def test_ignored_list_accepts_space_separated():
    s = Settings(ignored_gpu_processes="foo bar baz")
    assert s.ignored_gpu_processes == ["foo", "bar", "baz"]


def test_ignored_list_accepts_native_list():
    s = Settings(ignored_gpu_processes=["foo", "bar"])
    assert s.ignored_gpu_processes == ["foo", "bar"]
