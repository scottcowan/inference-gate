# TODO

- **Evaluate Colibri for a background "deep thinking" inference path** (https://github.com/JustVugg/colibri)
  Sparse-MoE inference engine that streams experts from NVMe/RAM/VRAM to run frontier-scale MoE models (hundreds of billions to trillions of params) on consumer hardware. Throughput is very slow (sub-1 to a few tok/s), but that's acceptable for background/batch jobs that need more reasoning depth than the fast model.
  - Hardware available: RTX 4060 16GB, 64GB RAM, 1TB NVMe + 2TB SSD (dual-drive read for expert streaming)
  - Start with a smaller MoE family (OLMoE or DeepSeek V4 Flash) to validate the pipeline before committing storage to a larger model
  - Wire in as a separate slow/background route in inference-gate's proxy — not the interactive/fast path — e.g. a job queue that routes "needs deep reasoning" tasks to Colibri and everything else to the existing fast model
  - Windows: prebuilt build available with optional native CUDA DLL
