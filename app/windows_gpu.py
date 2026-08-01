"""Windows GPU busy detection helpers.

Under WDDM, NVML/nvidia-smi cannot report per-process VRAM (Windows KMD owns
allocations). PDH GPU Process Memory Dedicated Usage can list processes
quickly (~2ms) but the MB values misreport games (e.g. Watch Dogs ~22 MB while
needing GBs) and can inflate DWM. Treat any non-desktop GPU process as busy when
VRAM numbers are missing or PDH-sourced; use EXTERNAL_VRAM_THRESHOLD_MB only for
trustworthy NVML-style figures.

Do not maintain per-game or per-launcher name lists — filter desktop noise via
the allowlist below, and treat everything else on the GPU as a real workload.
"""

from __future__ import annotations

# Processes that routinely appear on the GPU on an idle Windows desktop.
# Names are normalized (lowercase, no .exe) to match app.gpu._normalize_process_name.
DEFAULT_DESKTOP_GPU_PROCESSES: frozenset[str] = frozenset(
    {
        "dwm",
        "csrss",
        "system",
        "explorer",
        "searchhost",
        "startmenuexperiencehost",
        "textinputhost",
        "shellexperiencehost",
        "sihost",
        "runtimebroker",
        "applicationframehost",
        "systemsettings",
        "lockapp",
        "logonui",
        "fontdrvhost",
        "msedgewebview2",
        "msedge",
        "chrome",
        "brave",
        "firefox",
        "slack",
        "discord",
        "cursor",
        "code",
        "windowsterminal",
        "powershell",
        "pwsh",
        "cmd",
        "steam",
        "steamwebhelper",
        "epicgameslauncher",
        "epicwebhelper",
        "battle.net",
        "docker desktop",
        "com.docker.backend",
        "xboxpctray",
        "xboxpcapp",
        "xboxpcappft",
        "xboxgamebar",
        "gamebar",
        "xboxgamebarwidgets",
        "edgegameassist",
        "streamdeck",
        "steelseriesggclient",
        "razer synapse 3",
        "razer central",
        "razer cortex",
        "nvidia app",
        "nvidia overlay",
        "snippingtool",
        "taskmgr",
        "processhacker",
        "procexp",
        "procexp64",
        "perfmon",
        "resmon",
        "unigetui.avalonia",
        "cefviewwing",
        "lm studio",
        "lmstudio",
        "uplaywebcore",
        "upc",
        # EA App / Origin helpers (not the game itself)
        "eadesktop",
        "eacefsubprocess",
        "origin",
        "originwebhelperservice",
        # GOG Galaxy client
        "galaxyclient",
        "galaxyclientservice",
        "galaxyclienthelper",
        # Chat / messaging
        "signal",
        "signal-desktop",
        "?",
        # PowerToys
        "powertoys",
        "powertoys.advancedpaste",
        "powertoys.peek.ui",
        "powertoys.fancyzones",
        "powertoys.colorpickerui",
        "powertoys.powerlauncher",
    }
)


def normalize_desktop_name(name: str) -> str:
    return name.lower().removesuffix(".exe").strip()


def is_desktop_gpu_process(name: str, desktop: frozenset[str] | set[str]) -> bool:
    n = normalize_desktop_name(name)
    if n in desktop:
        return True
    # PowerToys / Xbox / Razer / NVIDIA helpers often appear with varying suffixes
    if n.startswith("powertoys"):
        return True
    if n.startswith("xbox"):
        return True
    if n.startswith("razer"):
        return True
    if n.startswith("nvidia"):
        return True
    if n.startswith("epic"):
        return True
    if n.startswith("galaxy"):
        return True
    if n.startswith("ea") and ("desktop" in n or "cef" in n or n == "ea"):
        return True
    return False


def non_desktop_consumers(
    external_consumers: list[dict],
    desktop: frozenset[str] | set[str],
) -> list[dict]:
    return [
        p
        for p in external_consumers
        if not is_desktop_gpu_process(str(p.get("name") or ""), desktop)
    ]
