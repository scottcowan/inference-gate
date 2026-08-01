from app.gpu import GpuState
from app.priority import Priority, is_effectively_free, should_allow

FREE = GpuState(0, 0, 16376, 0.0, [], True)
LIGHT = GpuState(30, 4000, 16376, 24.4, [], True)  # docker only, still free
BUSY = GpuState(94, 12000, 16376, 73.3, [{"name": "cyberpunk2077", "mem_mb": 11200}], False)
VERY_BUSY = GpuState(
    95, 14000, 16376, 85.4, [{"name": "cyberpunk2077", "mem_mb": 13000}], False
)

HIGH_THRESHOLD = 80
VRAM_THRESHOLD = 500
UTIL_FALLBACK = 40


def test_realtime_always_allowed():
    for state in [FREE, LIGHT, BUSY, VERY_BUSY]:
        assert should_allow(Priority.REALTIME, state, HIGH_THRESHOLD)


def test_high_allowed_when_free():
    assert should_allow(Priority.HIGH, FREE, HIGH_THRESHOLD)


def test_high_allowed_when_light_external_load():
    # External process present but under VRAM threshold
    light_external = GpuState(30, 4000, 16376, 24.4, [{"name": "some", "mem_mb": 400}], False)
    assert should_allow(
        Priority.HIGH, light_external, HIGH_THRESHOLD, VRAM_THRESHOLD, UTIL_FALLBACK
    )


def test_high_blocked_when_heavy_load():
    assert not should_allow(
        Priority.HIGH, VERY_BUSY, HIGH_THRESHOLD, VRAM_THRESHOLD, UTIL_FALLBACK
    )


def test_normal_blocked_when_busy():
    assert not should_allow(Priority.NORMAL, BUSY, HIGH_THRESHOLD, VRAM_THRESHOLD, UTIL_FALLBACK)


def test_normal_allowed_when_external_under_vram_threshold():
    light_external = GpuState(10, 1000, 16376, 6.1, [{"name": "some", "mem_mb": 400}], False)
    assert should_allow(
        Priority.NORMAL, light_external, HIGH_THRESHOLD, VRAM_THRESHOLD, UTIL_FALLBACK
    )


def test_normal_blocked_when_external_over_vram_threshold():
    heavy = GpuState(10, 1000, 16376, 6.1, [{"name": "some", "mem_mb": 2000}], False)
    assert not should_allow(Priority.NORMAL, heavy, HIGH_THRESHOLD, VRAM_THRESHOLD, UTIL_FALLBACK)


def test_normal_allowed_when_free():
    assert should_allow(Priority.NORMAL, FREE, HIGH_THRESHOLD)


def test_low_allowed_when_free():
    assert should_allow(Priority.LOW, FREE, HIGH_THRESHOLD)


def test_low_blocked_when_busy():
    assert not should_allow(Priority.LOW, BUSY, HIGH_THRESHOLD, VRAM_THRESHOLD, UTIL_FALLBACK)


def test_windows_desktop_noise_only_is_free():
    """WDDM idle desktop: browsers/DWM with mem_mb=0 must not trip the gate."""
    idle = GpuState(
        5,
        3000,
        16376,
        18.0,
        [{"name": "dwm", "mem_mb": 0}, {"name": "chrome", "mem_mb": 0}],
        False,
    )
    assert is_effectively_free(idle, 2000, UTIL_FALLBACK)
    assert should_allow(Priority.NORMAL, idle, HIGH_THRESHOLD, 2000, UTIL_FALLBACK)


def test_windows_game_present_busy_even_at_low_util():
    """WDDM: watch_dogs on the GPU with mem_mb=0 is busy immediately (no util wait)."""
    game = GpuState(
        2,
        9700,
        16376,
        59.0,
        [
            {"name": "dwm", "mem_mb": 0},
            {"name": "watch_dogs", "mem_mb": 0},
            {"name": "chrome", "mem_mb": 0},
        ],
        False,
    )
    assert not is_effectively_free(game, 2000, UTIL_FALLBACK)
    assert not should_allow(Priority.NORMAL, game, HIGH_THRESHOLD, 2000, UTIL_FALLBACK)


def test_windows_game_with_nvml_vram_uses_threshold():
    """Without mem_source=pdh, treat reported VRAM as trustworthy (Linux/NVML)."""
    game = GpuState(
        2,
        9700,
        16376,
        59.0,
        [{"name": "watch_dogs", "mem_mb": 100}],
        False,
    )
    assert is_effectively_free(game, 2000, UTIL_FALLBACK)
    game_heavy = GpuState(2, 9700, 16376, 59.0, [{"name": "watch_dogs", "mem_mb": 5000}], False)
    assert not is_effectively_free(game_heavy, 2000, UTIL_FALLBACK)


def test_windows_game_with_pdh_vram_busy_even_when_under_threshold():
    """PDH under-reports games (e.g. 22–100 MB) — presence must still drain the gate."""
    game = GpuState(
        2,
        9700,
        16376,
        59.0,
        [{"name": "watch_dogs", "mem_mb": 22, "mem_source": "pdh"}],
        False,
    )
    assert not is_effectively_free(game, 2000, UTIL_FALLBACK)
    assert not should_allow(Priority.NORMAL, game, HIGH_THRESHOLD, 2000, UTIL_FALLBACK)


def test_taskmgr_is_desktop_noise_not_a_game():
    idle = GpuState(
        5,
        2000,
        16376,
        12.0,
        [{"name": "taskmgr", "mem_mb": 8, "mem_source": "pdh"}, {"name": "dwm", "mem_mb": 500, "mem_source": "pdh"}],
        False,
    )
    assert is_effectively_free(idle, 2000, UTIL_FALLBACK)
