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

```bash
cp .env.example .env
# set UPSTREAM_URL to your LLM server
docker compose up -d --build

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
| `PROXY_PORT` | `11435` | Informational only — changing this value alone has no effect. You must also update the `uvicorn --port` flag in the Dockerfile `CMD` and the `ports` mapping in your compose file. |
| `IGNORED_GPU_PROCESSES` | see below | Process names that own the GPU legitimately and never trigger backoff |
| `HIGH_PRIORITY_UTIL_THRESHOLD` | `80` | Utilization % above which `high`-priority requests also back off |
| `MODEL_LOAD_IMMUNITY_SECS` | `60` | Seconds to hold inference requests after a model switch is detected |
| `EXTERNAL_VRAM_THRESHOLD_MB` | `500` | Total external consumer VRAM (MB) above which a game is considered active. Below this, background processes (dwm.exe, browsers, Discord) are ignored. |
| `SERVER_PROCESS` | _(unset)_ | Process name to kill when a game is detected (e.g. `ollama`, `llama-server`). Only works when the proxy runs natively on the same machine as the LLM server — not from inside Docker on a Windows host. |
| `SERVER_START_COMMAND` | _(unset)_ | Shell command to restart the LLM server after the game exits (e.g. `ollama serve`). Same native-only constraint as `SERVER_PROCESS`. |
| `PULL_COMMAND` | _(unset)_ | Shell command used to pull a model for non-Ollama backends; `{model}` is substituted. When unset, `/api/pull` is forwarded to the upstream unchanged. |
| `PULL_HOLD_TIMEOUT_SECS` | `300` | Seconds to hold inference requests waiting for a pull to finish before returning 503 |

Default `IGNORED_GPU_PROCESSES`: `ollama ollama_llama_server llama-server llamafile lmstudio lms koboldcpp cortex-cpp nitro python python3`

Add your server's process name if it isn't there. For Docker-based Ollama, add `docker dockerd com.docker.backend`. Games (`cs2.exe`, `eldenring.exe`, `DaVinciResolve.exe`) should never be in this list — they're the reason the gate exists.

Accepts a JSON array or space-separated string:

```
IGNORED_GPU_PROCESSES=ollama llama-server python3
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
- Non-streaming requests use a 120s timeout.
- All requests (streaming and non-streaming) are held until any in-progress pull completes (up to `PULL_HOLD_TIMEOUT_SECS`; returns 503 on timeout).
- When a model switch is detected, all requests are held for `MODEL_LOAD_IMMUNITY_SECS` before being forwarded to give the new model time to load.

## Gaming PC / Windows native setup

For a gaming PC, run inference-gate natively alongside your LLM server. The Docker path works for GPU gating but `SERVER_PROCESS` and `SERVER_START_COMMAND` require the proxy to run on the same OS as the LLM server — they use `psutil` to kill and restart host processes, which doesn't work from inside a Docker container on a Windows host.

```bash
pip install inference-gate   # or: pip install -e . from the repo
cp .env.example .env
# edit .env
uvicorn app.main:app --port 11435
```

Recommended `.env` for Ollama on Windows:

```
UPSTREAM_URL=http://localhost:11434
SERVER_PROCESS=ollama
SERVER_START_COMMAND=ollama serve
EXTERNAL_VRAM_THRESHOLD_MB=2000
```

Run `setup-windows.ps1` once as Administrator to disable GPU hardware acceleration in browsers and Discord, reducing idle VRAM usage and letting you set a lower threshold.

To run as a background service, use [NSSM](https://nssm.cc/) or register a scheduled task that starts `uvicorn app.main:app --port 11435` at login.

## Deployment (Docker / Linux server)

inference-gate needs GPU device access to read NVML via `gpustat` — not to run inference.

> **Note:** `SERVER_PROCESS` and `SERVER_START_COMMAND` only work when inference-gate runs natively on the same machine as the LLM server. In Docker these settings have no effect.

```yaml
services:
  inference-gate:
    build: .
    ports:
      - "11435:11435"
    pid: host           # required so gpustat can see host process names
    environment:
      - UPSTREAM_URL=http://llm-server:11434   # replace with your LLM server's service name / address
      - HIGH_PRIORITY_UTIL_THRESHOLD=80
      - NVIDIA_VISIBLE_DEVICES=all
      - NVIDIA_DRIVER_CAPABILITIES=utility,compute
    # depends_on:
    #   - llm-server   # uncomment and set to your LLM service name if it runs in the same compose file
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
    restart: unless-stopped
```

If `gpustat` can't reach a GPU (no NVIDIA runtime, dev laptop), it logs a warning and reports the GPU as free. The proxy never blocks in that state. Check `/gpu` after deploying to confirm real numbers.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest
```

Tests stub `app.state.gpu_query` with fixed `GpuState` values and mock the upstream with `respx` — no GPU or LLM server needed.

```bash
uvicorn app.main:app --reload --port 11435
```
