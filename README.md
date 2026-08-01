# inference-gate

A GPU-gating reverse proxy for local LLM servers. Sits in front of Ollama, vLLM, LM Studio, llama.cpp, or any compatible server and blocks inference requests when something else owns the GPU — a game, a video editor, a render job. Realtime requests always pass through. Everything else waits.

## How it works

```
  client (X-Priority: realtime)     client (X-Priority: low)
          │                                   │
          └──────────────┬────────────────────┘
                         ▼
              ┌─────────────────────┐
              │   inference-gate    │ ◀──── gpustat (NVML)
              │   :11435            │       reads GPU util +
              │                     │       process list
              └──────┬──────────────┘
                     │ allow / 429
                     ▼
              ┌─────────────────────┐
              │   LLM Server        │
              │   :11434            │
              └─────────────────────┘
                     │
                  [ GPU ] ◀── also: games, Blender, SD
```

GPU state comes from `gpustat`. A process on the GPU that isn't in `IGNORED_GPU_PROCESSES` is an external consumer. When external consumers are present, `normal` and `low` priority requests get a `429`. `high` gets through unless utilization is also above the threshold. `realtime` always passes.

When external consumer VRAM exceeds `EXTERNAL_VRAM_THRESHOLD_MB` (a game is running), inference-gate drains in-flight requests, kills the LLM server, and frees the VRAM. When the game exits and VRAM drops back below the threshold, it restarts the server automatically. All requests 429 during drain and while the server is down.

On Windows, NVML often reports mem_mb: 0 under WDDM. inference-gate fills dedicated VRAM from Windows Performance Counters (PDH `GPU Process Memory(*)\Dedicated Usage`, same counter family as Task Manager, ~2ms/sample) so processes appear in `/gpu` with `mem_source: "pdh"`.

**Finding:** PDH dedicated-usage MB is unreliable for games. Watch Dogs showed ~22 MB on PDH while needing GBs; treating that as under `EXTERNAL_VRAM_THRESHOLD_MB` left the LLM loaded and fullscreen crashed. DWM can also show inflated PDH values (GBs) — filtered via the desktop-process allowlist. So on Windows, any non-desktop GPU process with PDH (or zero) VRAM counts as **busy by presence**; the VRAM threshold applies only to trustworthy NVML-style numbers (typical on Linux).

**Also:** API unload alone is not equivalent to stopping a Docker Ollama container.
Prefer soft-stop of Ollama when a non-desktop GPU process appears (`SERVER_KILL_PROCESSES=true`,
`SERVER_FORCE_KILL=false`). Hard-kill of CUDA runners can wedge WDDM so exclusive
fullscreen keeps crashing until reboot. Detection is GPU-based only: a hardcoded
desktop allowlist in `app/windows_gpu.py` (browsers, Steam/Epic/EA/Battle.net/GOG
helpers, Razer, Signal, PowerToys, …) filters idle noise; everything else on the
GPU is treated as a real workload. No per-game name lists. The gate re-opens after
`SERVER_RESTART_STABLE_SECS` of continuous free time, restarts the LLM, optionally
preloads remembered models while still returning 429 (`STARTING`), then goes `running`.

`IGNORED_GPU_PROCESSES` is only for the LLM server binaries themselves (Ollama, vLLM,
llama-server, …). Launchers belong on the desktop allowlist (or should not autostart).

## Priority routing

Set `X-Priority` on the request. Missing or unrecognised values are treated as `normal`.

| `X-Priority` | Server up, GPU free | Server up, GPU busy | Draining / down |
|---|---|---|---|
| `realtime` | forward | forward | 429 |
| `high` | forward | forward if util ≤ threshold | 429 |
| `normal` | forward | 429 | 429 |
| `low` | forward | 429 | 429 |

`low` requests cannot trigger a model switch — they run on whatever is already loaded or get a 429.

Refused requests get `429` with `Retry-After: 10`:

```json
{ "error": "GPU busy — retry after GPU is free" }
```

## Quick start

Run natively on the same machine as the LLM server (required for soft-stop / restart):

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\Activate.ps1
pip install -e .
cp .env.example .env
# edit .env — see recommended Windows block below
uvicorn app.main:app --host 0.0.0.0 --port 11435
```

```bash
curl localhost:11435/health
curl localhost:11435/gpu

# Ollama
curl localhost:11435/api/generate \
  -H 'X-Priority: low' \
  -d '{"model":"qwen2.5:7b","prompt":"hello"}'

# OpenAI-compatible (vLLM, LM Studio, llama.cpp, etc.)
curl localhost:11435/v1/chat/completions \
  -H 'X-Priority: low' \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen2.5:7b","messages":[{"role":"user","content":"hello"}]}'
```

Point clients at `:11435` instead of your LLM server's port. Stop publishing the LLM server's own port if you want the gate to be unbypassable.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `UPSTREAM_URL` | `http://localhost:11434` | LLM server base URL |
| `PROXY_PORT` | `11435` | Informational only — the listen port is set by `uvicorn --port` (or `scripts/start-gate.ps1`). |
| `IGNORED_GPU_PROCESSES` | see below | LLM server process names that own the GPU legitimately and never trigger backoff (not launchers) |
| `HIGH_PRIORITY_UTIL_THRESHOLD` | `80` | Utilization % above which `high`-priority requests also back off |
| `MODEL_LOAD_IMMUNITY_SECS` | `60` | Seconds to hold inference requests after a model switch is detected |
| `EXTERNAL_VRAM_THRESHOLD_MB` | `500` | Total external consumer VRAM (MB) above which a game is considered active when figures are trustworthy (NVML). On Windows PDH, non-desktop process presence is primary. |
| `EXTERNAL_UTIL_FALLBACK_THRESHOLD` | `40` | Secondary util busy signal when per-process VRAM is missing. On Windows, PDH + non-desktop presence are preferred. |
| `SERVER_RESTART_STABLE_SECS` | `5` | Seconds the GPU must stay free before restarting the LLM server after a drain (stops util-dip thrashing while a game loads). |
| `SERVER_PROCESS` | _(unset)_ | Process name to stop when a game is detected (e.g. `ollama`, `llama-server`). Must run on the same OS as the LLM server. |
| `SERVER_KILL_PROCESSES` | `false` | Soft-stop `SERVER_PROCESS` (and related runners) after unload when a non-desktop GPU consumer appears. |
| `SERVER_FORCE_KILL` | `false` | If true with kill enabled, force-kill processes that ignore terminate. Keep `false` on gaming PCs (WDDM). |
| `SERVER_START_COMMAND` | _(unset)_ | Shell command to restart the LLM after the game exits (e.g. `ollama serve`). Same host constraint as `SERVER_PROCESS`. |
| `SERVER_PRELOAD_ON_START` | `true` | After restart, preload remembered models while state is `starting` (still 429), then open. |
| `SERVER_PRELOAD_KEEP_ALIVE` | `24h` | `keep_alive` passed to Ollama when preloading. |
| `SERVER_PRELOAD_MODEL` | _(unset)_ | Fallback model to preload if none were loaded at drain time. |
| `PULL_COMMAND` | _(unset)_ | Shell command used to pull a model for non-Ollama backends; `{model}` is substituted. When unset, `/api/pull` is forwarded to the upstream unchanged. |
| `PULL_HOLD_TIMEOUT_SECS` | `300` | Seconds to hold inference requests waiting for a pull to finish before returning 503 |

Default `IGNORED_GPU_PROCESSES`: `ollama ollama_llama_server llama-server llamafile lmstudio lms koboldcpp cortex-cpp nitro python python3`

Add your server's process name if it isn't there. For Docker-based Ollama, add `docker dockerd com.docker.backend`. Games (`cs2.exe`, `eldenring.exe`, `DaVinciResolve.exe`) should never be in this list — they're the reason the gate exists. Idle desktop/launcher processes are handled by `DEFAULT_DESKTOP_GPU_PROCESSES` in `app/windows_gpu.py`, not this variable.

In `.env`, use a JSON array (required — pydantic-settings JSON-decodes list fields before validators run, so space-separated values fail at startup):

```
IGNORED_GPU_PROCESSES=["ollama","llama-server","python3"]
```

## API

### `GET /health`

```json
{ "status": "ok", "version": "0.1.0" }
```

### `GET /gpu`

Raw GPU state for external monitors or schedulers.

```json
{
  "gpu_utilization_pct": 94,
  "memory_used_mb": 12000,
  "memory_total_mb": 16376,
  "memory_used_pct": 73.3,
  "external_consumers": [
    { "name": "cyberpunk2077.exe", "mem_mb": 11200 }
  ],
  "free": false,
  "server": {
    "state": "draining",
    "in_flight": 2
  }
}
```

`server.state` is one of `running`, `draining`, `down`, or `starting`.

### `POST /api/pull`

Behaviour depends on whether `PULL_COMMAND` is set.

- **`PULL_COMMAND` unset (default):** the request is forwarded to the upstream unchanged (standard Ollama pull passthrough).
- **`PULL_COMMAND` set:** inference-gate runs the command locally, substituting `{model}` with the model name from the request body (`{"model": "<name>"}`). It streams NDJSON progress lines:

  ```json
  {"status": "pulling manifest", "done": false}
  {"status": "downloading ...", "done": false}
  {"status": "done", "done": true}
  ```

  While the pull is running, all inference requests are held via the `pull_ready` gate. Requests that wait longer than `PULL_HOLD_TIMEOUT_SECS` receive a 503.

  Example `PULL_COMMAND` for Hugging Face:

  ```
  PULL_COMMAND=huggingface-cli download {model}
  ```

### `ANY /{path}`

Forwards any request to the upstream server over `GET`, `POST`, `DELETE`, `HEAD`.

- `Host`, `Content-Length`, and `X-Priority` are stripped; all other headers pass through.
- Requests with `"stream": true` in the JSON body are streamed with no timeout, passing through the upstream's `Content-Type` (`application/x-ndjson` for Ollama native routes, `text/event-stream` for OpenAI-compatible routes).
- Non-streaming requests use a 600s timeout (cold model loads on large GGUFs can exceed 2 minutes).
- All requests (streaming and non-streaming) are held until any in-progress pull completes (up to `PULL_HOLD_TIMEOUT_SECS`; returns 503 on timeout).
- When a model switch is detected, all requests are held for `MODEL_LOAD_IMMUNITY_SECS` before being forwarded to give the new model time to load.

## Gaming PC / Windows native setup

Run inference-gate natively alongside Ollama (or another local LLM). Soft-stop and restart use host processes via `psutil` — they only work when the gate shares the OS with the LLM server.

```bash
pip install -e .   # from the repo, with venv active
cp .env.example .env
# edit .env
uvicorn app.main:app --host 0.0.0.0 --port 11435
```

Recommended `.env` for Ollama on Windows (see `.env.example` for the full commented set):

```
UPSTREAM_URL=http://localhost:11434
EXTERNAL_VRAM_THRESHOLD_MB=2000
EXTERNAL_UTIL_FALLBACK_THRESHOLD=40
SERVER_RESTART_STABLE_SECS=5
IGNORED_GPU_PROCESSES=["ollama","ollama_llama_server","llama-server"]
SERVER_PROCESS=ollama
SERVER_KILL_PROCESSES=true
SERVER_FORCE_KILL=false
SERVER_START_COMMAND=ollama serve
SERVER_PRELOAD_ON_START=true
SERVER_PRELOAD_KEEP_ALIVE=24h
PROXY_PORT=11435
```

Point clients at `:11435`, not Ollama’s `:11434`.

Run `setup-windows.ps1` once as Administrator to disable GPU hardware acceleration in browsers and Discord, reducing idle VRAM usage.

Desktop launchers that still appear on the GPU (Epic, EA, Battle.net, Signal, Razer, …) are allowlisted so they do not drain Ollama — but trimming their “start with Windows” entries under `HKCU\...\Run` still helps boot noise and avoids races before the allowlist is loaded. If a new tray app falsely keeps the gate `down`, add its process name to `DEFAULT_DESKTOP_GPU_PROCESSES` in `app/windows_gpu.py` and restart the gate.

### Survive reboot (Windows)

Ollama usually installs a Startup shortcut (`Ollama.lnk`). Register the gate as a logon scheduled task:

```powershell
cd d:\Code\inference-gate
.\scripts\register-autostart.ps1
# optional: start without rebooting
Start-ScheduledTask -TaskName InferenceGate
```

That runs `scripts/start-gate.ps1` at logon (30s delay, waits up to ~90s for Ollama, logs under `logs\`). Remove with `.\scripts\unregister-autostart.ps1`.

GPU counters and the Ollama tray need an interactive session. For an unattended gaming PC, enable auto-login (Sysinternals Autologon — account password, not PIN):

```powershell
.\scripts\enable-autologon.ps1
```

Anyone with physical access can then use the PC without signing in. Disable later via Autologon → Disable.

If you still prefer NSSM: point it at `.venv\Scripts\uvicorn.exe` with arguments `app.main:app --host 0.0.0.0 --port 11435` and working directory set to the repo root.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest
```

Unit tests stub `app.state.gpu_query` with fixed `GpuState` values and mock the upstream with `respx` — no GPU or LLM server needed.

Live smoke (requires gate + Ollama running):

```bash
uvicorn app.main:app --host 0.0.0.0 --port 11435
pytest tests/test_smoke.py -v
# optional overrides:
# INFERENCE_GATE_URL=http://192.168.0.253:11435 INFERENCE_GATE_SMOKE_MODEL=gemma4:12b-it-q4_K_M pytest tests/test_smoke.py
```

```bash
uvicorn app.main:app --reload --port 11435
```
