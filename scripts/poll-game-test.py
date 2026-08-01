"""Poll gate until game drains models or timeout."""
from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime


def get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())


def main() -> None:
    for _ in range(90):
        t = datetime.now().strftime("%H:%M:%S")
        g = get("http://127.0.0.1:11435/gpu")
        wd = [
            (p.get("name"), p.get("mem_mb"), p.get("mem_source"))
            for p in (g.get("external_consumers") or [])
            if "watch" in (p.get("name") or "").lower()
        ]
        ps = get("http://127.0.0.1:11434/api/ps")
        models = [m.get("name") for m in (ps.get("models") or [])]
        state = g["server"]["state"]
        print(
            f"{t} state={state} vram={g['memory_used_mb']} "
            f"util={g['gpu_utilization_pct']} wd={wd} models={models or None}",
            flush=True,
        )
        if state == "down" and not models:
            print("DRAIN_OK", flush=True)
            return
        if state in ("draining", "down") and not models:
            print("DRAIN_OK", flush=True)
            return
        time.sleep(2)
    print("TIMEOUT", flush=True)


if __name__ == "__main__":
    main()
