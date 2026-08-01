"""Per-process GPU dedicated VRAM via Windows Performance Counters (PDH).

NVML/nvidia-smi cannot report per-process VRAM under WDDM. Task Manager uses
PDH ``GPU Process Memory(*)\\Dedicated Usage`` instead — this module reads the
same counters through ``pdh.dll`` (typically ~2ms per sample).
"""

from __future__ import annotations

import logging
import re
import sys
import time
from collections import defaultdict
from ctypes import POINTER, Structure, Union, WinDLL, byref, c_double, c_longlong, c_void_p, create_unicode_buffer
from ctypes.wintypes import DWORD, LONG, LPCSTR, LPCWSTR

logger = logging.getLogger(__name__)

_PDH_FMT_DOUBLE = 0x00000200
_PID_RE = re.compile(r"pid_(\d+)", re.IGNORECASE)

# Cache — watch_gpu polls ~1Hz; avoid re-enumerating counters every call.
_CACHE_TTL_SECS = 0.75
_cache_at = 0.0
_cache: dict[int, float] = {}


class _PDH_FMT_COUNTERVALUE(Structure):
    class _U(Union):
        _fields_ = [
            ("longValue", LONG),
            ("doubleValue", c_double),
            ("largeValue", c_longlong),
            ("AnsiStringValue", LPCSTR),
            ("WideStringValue", LPCWSTR),
        ]

    _anonymous_ = ("u",)
    _fields_ = [("CStatus", DWORD), ("u", _U)]


def available() -> bool:
    return sys.platform == "win32"


def dedicated_mb_by_pid(*, force: bool = False) -> dict[int, float]:
    """Return {pid: dedicated_vram_mb} summed across adapter LUIDs.

    Returns an empty dict on non-Windows or if PDH fails.
    """
    global _cache_at, _cache
    if not available():
        return {}
    now = time.monotonic()
    if not force and _cache and now - _cache_at < _CACHE_TTL_SECS:
        return _cache
    try:
        _cache = _sample_dedicated_mb()
        _cache_at = now
        return _cache
    except Exception:
        logger.warning("PDH GPU Process Memory sample failed", exc_info=True)
        return _cache or {}


def _sample_dedicated_mb() -> dict[int, float]:
    pdh = WinDLL("pdh")
    path = r"\GPU Process Memory(*)\Dedicated Usage"

    buflen = DWORD(0)
    # Size probe — expects ERROR / MORE_DATA style status with size filled in.
    pdh.PdhExpandWildCardPathW(None, path, None, byref(buflen), 0)
    if buflen.value <= 1:
        return {}
    buf = create_unicode_buffer(buflen.value)
    status = pdh.PdhExpandWildCardPathW(None, path, buf, byref(buflen), 0)
    if status != 0:
        raise OSError(f"PdhExpandWildCardPathW failed: 0x{status & 0xFFFFFFFF:08X}")

    paths = [p for p in buf[:].split("\x00") if p]
    if not paths:
        return {}

    h_query = c_void_p()
    status = pdh.PdhOpenQueryW(None, None, byref(h_query))
    if status != 0:
        raise OSError(f"PdhOpenQueryW failed: 0x{status & 0xFFFFFFFF:08X}")

    counters: list[tuple[str, c_void_p]] = []
    try:
        for p in paths:
            h_counter = c_void_p()
            if pdh.PdhAddEnglishCounterW(h_query, p, None, byref(h_counter)) == 0:
                counters.append((p, h_counter))

        # Absolute counters need two collects; no sleep required.
        pdh.PdhCollectQueryData(h_query)
        pdh.PdhCollectQueryData(h_query)

        by_pid: dict[int, float] = defaultdict(float)
        typ = DWORD()
        val = _PDH_FMT_COUNTERVALUE()
        get = pdh.PdhGetFormattedCounterValue
        for p, h_counter in counters:
            if get(h_counter, _PDH_FMT_DOUBLE, byref(typ), byref(val)) != 0:
                continue
            m = _PID_RE.search(p)
            if not m:
                continue
            mb = float(val.doubleValue) / (1024 * 1024)
            if mb > 0:
                by_pid[int(m.group(1))] += mb
        return dict(by_pid)
    finally:
        pdh.PdhCloseQuery(h_query)
