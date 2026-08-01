"""Poll until gate drains for any game (not just watch_dogs)."""
from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime

try:
    import psutil
except ImportError:
    psutil = None


def get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())


def ollama_pids() -> list[tuple[int, str]]:
    if not psutil:
        return []
    out = []
    for p in psutil.process_iter(["name", "pid"]):
        name = (p.info["name"] or "").lower()
        if "ollama" in name or name.startswith("llama-server"):
            out.append((p.info["pid"], p.info["name"]))
    return out


def main() -> None:
    for _ in range(90):
        t = datetime.now().strftime("%H:%M:%S")
        try:
            g = get("http://127.0.0.1:11435/gpu")
        except Exception as e:
            print(f"{t} gate_err={e}", flush=True)
            time.sleep(2)
            continue
        from app.windows_gpu import non_desktop_consumers, DEFAULT_DESKTOP_GPU_PROCESSES

        heavy = [
            (p.get("name"), p.get("mem_mb"))
            for p in non_desktop_consumers(
                g.get("external_consumers") or [], DEFAULT_DESKTOP_GPU_PROCESSES
            )
        ]
        try:
            ps = get("http://127.0.0.1:11434/api/ps")
            models = [m.get("name") for m in (ps.get("models") or [])]
            ollama_up = True
        except Exception:
            models = None
            ollama_up = False
        procs = ollama_pids()
        state = g["server"]["state"]
        print(
            f"{t} state={state} vram={g['memory_used_mb']} heavy={heavy} "
            f"models={models} ollama_up={ollama_up} ollama_procs={len(procs)}",
            flush=True,
        )
        if state == "down" and not ollama_up:
            print("STOP_OK", flush=True)
            return
        time.sleep(2)
    print("TIMEOUT", flush=True)


if __name__ == "__main__":
    main()
