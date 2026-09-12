# TODO

- **Evaluate Colibri for a background "deep thinking" inference path** (https://github.com/JustVugg/colibri)
  Sparse-MoE inference engine that streams experts from NVMe/RAM/VRAM to run frontier-scale MoE models (hundreds of billions to trillions of params) on consumer hardware. Throughput is very slow (sub-1 to a few tok/s), but that's acceptable for background/batch jobs that need more reasoning depth than the fast model.
  - Hardware available: RTX 4060 16GB, 64GB RAM, 1TB NVMe + 2TB SSD (dual-drive read for expert streaming)
  - Start with a smaller MoE family (OLMoE or DeepSeek V4 Flash) to validate the pipeline before committing storage to a larger model
  - Wire in as a separate slow/background route in inference-gate's proxy — not the interactive/fast path — e.g. a job queue that routes "needs deep reasoning" tasks to Colibri and everything else to the existing fast model
  - Windows: prebuilt build available with optional native CUDA DLL

  **Implementation plan** (fits inference-gate's actual design: `X-Priority` gates
  contention for *one* upstream server; Colibri is a second, independent backend
  with its own lifecycle, so it gets its own upstream + route, not a new priority tier):

  1. **Validate Colibri standalone** — no inference-gate involved. Run it on the
     Windows box against a small MoE family (OLMoE) with 1TB NVMe primary /
     2TB SSD secondary. Confirm it exposes an OpenAI/Ollama-compatible HTTP
     endpoint (unverified so far — check its docs) and note port + process name.
  2. **Add a second upstream, not a new priority tier** — `app/config.py`:
     add `background_upstream_url` alongside `upstream_url`. `app/routers/proxy.py`:
     add a routing branch (path prefix `/background/*`, or `model` name matching a
     configured `background_models: list[str]`) that dispatches to
     `background_upstream_url` instead. `_forward()` is already upstream-agnostic —
     just parameterize the URL.
  3. **Gating: reuse `should_allow`/`is_effectively_free`, don't rebuild** — route
     Colibri requests through the existing GPU-contention checks in `app/priority.py`,
     always at `LOW` semantics regardless of the caller's own priority header (never
     triggers a model switch, always defers to GPU contention). Skip the
     `ServerManager` drain/kill/restart lifecycle — that exists for Ollama's WDDM/
     fullscreen-crash problem; Colibri is disk/CPU-bound by design and doesn't need
     to be hard-killed for a game the way a VRAM-resident CUDA runner does.
  4. **Long-running job semantics** — Colibri responses can take minutes; holding
     an HTTP connection open like the existing streaming proxy does is the wrong
     shape. Add `POST /background/submit` (enqueue, return job id) and
     `GET /background/jobs/{id}` (poll status/result), sitting in front of the
     Phase 2 routing rather than replacing `_forward()`.
  5. **Prove it end-to-end** — run one real background task (e.g. a
     summarization/reasoning job normally sent to the fast model) through the
     full path: submit → gate defers if GPU busy → Colibri processes on
     NVMe/SSD → job completes → result polled. Confirm a concurrent game
     session isn't disrupted.
