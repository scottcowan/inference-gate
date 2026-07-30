# ollama-gpu-proxy

A priority-gated reverse proxy for Ollama. It sits in front of an Ollama instance and decides, per request, whether the GPU is free enough to serve it. When something *else* is using the GPU — a game, a Jupyter kernel, Stable Diffusion — low-priority background inference gets a `429` with `Retry-After` instead of fighting for VRAM, while realtime requests always pass through. GPU consumers are detected with `gpustat`; Docker-owned processes (i.e. Ollama itself) are ignored, so the proxy never backs off because of its own upstream.

## Architecture

```
  interactive client            batch / pipeline client
  X-Priority: realtime          X-Priority: low
          │                              │
          └──────────────┬───────────────┘
                         ▼
              ┌──────────────────────┐
              │  ollama-gpu-proxy    │      ┌──────────────┐
              │  :11435              │─────▶│   gpustat    │
              │                      │◀─────│  (NVML)      │
              │  GET  /health        │      └──────────────┘
              │  GET  /gpu           │              │
              │  *    /{path}  ──────┼──┐     reads GPU util +
              └──────────────────────┘  │     process list, filters
                         │              │     out docker/dockerd
              429 + Retry-After         │
              (GPU busy, priority       │  forward
               too low)                 ▼
                                ┌──────────────┐
                                │    Ollama    │
                                │    :11434    │
                                └──────────────┘
                                       │
                                    [ GPU ]  ◀── also: games, Jupyter, SD
```

## Priority routing

Priority comes from the `X-Priority` request header. Missing or unrecognised values are treated as `normal`.

| `X-Priority` | Forwarded when |
|---|---|
| `realtime` | Always — never gated |
| `high` | GPU is free, **or** `gpu_utilization_pct <= HIGH_PRIORITY_UTIL_THRESHOLD` |
| `normal` | GPU is free (zero external consumers) |
| `low` | GPU is free (zero external consumers) |

"Free" means no external GPU consumers — every process on the GPU matched `IGNORED_GPU_PROCESSES`. GPU utilization alone does not make the GPU busy; a single external process does.

When a request is refused the proxy returns `429` with `Retry-After: 10`:

```json
{ "error": "GPU busy — retry after GPU is free" }
```

## Quick start

```bash
cp .env.example .env
docker compose up -d --build

curl localhost:11435/health
curl localhost:11435/gpu

curl localhost:11435/api/generate \
  -H 'X-Priority: low' \
  -d '{"model":"qwen2.5:7b","prompt":"hello"}'
```

The compose file assumes an `ollama` container is reachable on the same network. For standalone testing, uncomment the `ollama` service block in `docker-compose.yml`.

## Configuration

Environment variables (or a `.env` file), read by `app/config.py`:

| Variable | Default | Purpose |
|---|---|---|
| `OLLAMA_URL` | `http://ollama:11434` | Upstream Ollama base URL |
| `PROXY_PORT` | `11435` | Documented listen port (the actual bind is the `uvicorn --port` flag in the Dockerfile `CMD`) |
| `IGNORED_GPU_PROCESSES` | `docker dockerd com.docker.backend ollama ollama_llama_server` | Process names that never count as external consumers. Accepts a JSON array or a space-separated string |
| `HIGH_PRIORITY_UTIL_THRESHOLD` | `80` | Utilization percentage above which `high`-priority requests also back off |

## API

### `GET /health`

```json
{ "status": "ok", "version": "0.1.0" }
```

### `GET /gpu`

Raw GPU state, for external monitors and schedulers that want to make their own admission decisions.

```json
{
  "gpu_utilization_pct": 94,
  "memory_used_mb": 12000,
  "memory_total_mb": 16376,
  "memory_used_pct": 73.3,
  "external_consumers": [
    { "name": "cyberpunk2077.exe", "mem_mb": 11200 }
  ],
  "free": false
}
```

### `ANY /{path}`

Catch-all proxy for every Ollama route (`/api/generate`, `/api/chat`, `/api/tags`, …) over `GET`, `POST`, `DELETE`, `HEAD`. Behaviour:

- Gated by `X-Priority` per the table above.
- `Host`, `Content-Length`, and `X-Priority` are stripped; all other headers pass through.
- Requests with `"stream": true` in the JSON body are proxied as a streaming `application/x-ndjson` response with no timeout. Non-streaming requests use a 120s timeout.

## Deployment

Runs as a sidecar next to the Ollama container. It needs GPU device access of its own — not to run inference, but so `gpustat` can read NVML from inside the container.

Add to an existing Ollama compose stack:

```yaml
services:
  ollama:
    image: ollama/ollama:latest
    # no host port needed — only the proxy needs to reach it
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]

  ollama-gpu-proxy:
    build: ./ollama-gpu-proxy
    ports:
      - "11435:11435"
    pid: host           # required so gpustat can see host process names
    environment:
      - OLLAMA_URL=http://ollama:11434
      - HIGH_PRIORITY_UTIL_THRESHOLD=80
      - NVIDIA_VISIBLE_DEVICES=all
      - NVIDIA_DRIVER_CAPABILITIES=utility,compute
    depends_on:
      - ollama
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
    restart: unless-stopped
```

Then point clients at `:11435` instead of `:11434`. Stop publishing Ollama's own port to the host if you want the gate to be unbypassable.

If `gpustat` cannot reach a GPU — no NVIDIA runtime, dev laptop — the query logs a warning and reports the GPU as **free**, so local development is never blocked. This fail-open behaviour means a broken NVML setup in production silently disables gating; check `/gpu` after deploying to confirm real numbers are coming back.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'

pytest                      # full suite
pytest tests/test_priority.py -v
```

`tests/test_priority.py` covers the admission matrix as pure functions. `tests/test_proxy.py` drives the app through `TestClient`, stubbing `app.state.gpu_query` with fixed `GpuState` values and mocking the upstream with `respx` — no GPU or Ollama instance required. `tests/test_gpu.py` covers the gpustat dict-format parsing, ignored-process filtering, and the `IGNORED_GPU_PROCESSES` env validator.

To run the app directly:

```bash
uvicorn app.main:app --reload --port 11435
```
