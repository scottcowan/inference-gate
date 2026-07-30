from app.gpu import GpuState
from app.priority import Priority, should_allow

FREE = GpuState(0, 0, 16376, 0.0, [], True)
LIGHT = GpuState(30, 4000, 16376, 24.4, [], True)  # docker only, still free
BUSY = GpuState(94, 12000, 16376, 73.3, [{"name": "cyberpunk2077.exe", "mem_mb": 11200}], False)
VERY_BUSY = GpuState(
    95, 14000, 16376, 85.4, [{"name": "cyberpunk2077.exe", "mem_mb": 13000}], False
)

HIGH_THRESHOLD = 80


def test_realtime_always_allowed():
    for state in [FREE, LIGHT, BUSY, VERY_BUSY]:
        assert should_allow(Priority.REALTIME, state, HIGH_THRESHOLD)


def test_high_allowed_when_free():
    assert should_allow(Priority.HIGH, FREE, HIGH_THRESHOLD)


def test_high_allowed_when_light_external_load():
    # External process present but under threshold
    light_external = GpuState(30, 4000, 16376, 24.4, [{"name": "some.exe", "mem_mb": 400}], False)
    assert should_allow(Priority.HIGH, light_external, HIGH_THRESHOLD)


def test_high_blocked_when_heavy_load():
    assert not should_allow(Priority.HIGH, VERY_BUSY, HIGH_THRESHOLD)


def test_normal_blocked_when_busy():
    assert not should_allow(Priority.NORMAL, BUSY, HIGH_THRESHOLD)


def test_normal_blocked_when_any_external():
    light_external = GpuState(10, 1000, 16376, 6.1, [{"name": "some.exe", "mem_mb": 400}], False)
    assert not should_allow(Priority.NORMAL, light_external, HIGH_THRESHOLD)


def test_normal_allowed_when_free():
    assert should_allow(Priority.NORMAL, FREE, HIGH_THRESHOLD)


def test_low_allowed_when_free():
    assert should_allow(Priority.LOW, FREE, HIGH_THRESHOLD)


def test_low_blocked_when_busy():
    assert not should_allow(Priority.LOW, BUSY, HIGH_THRESHOLD)
