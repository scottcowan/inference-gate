import json
from functools import lru_cache
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    upstream_url: str = "http://localhost:11434"
    proxy_port: int = 11435

    # LLM server process names that are expected GPU consumers — never trigger backoff.
    # These are inference server binary/script names, NOT game names.
    # In .env this must be a JSON array (pydantic-settings JSON-decodes list fields first):
    #   IGNORED_GPU_PROCESSES=["ollama","ollama_llama_server","llama-server"]
    ignored_gpu_processes: list[str] = [
        # Ollama native
        "ollama",
        "ollama_llama_server",
        # llama.cpp server (official binary name) and llamafile (Mozilla self-contained binary)
        "llama-server",
        "llamafile",
        # Python-based LLM servers: vLLM (`python -m vllm.entrypoints.openai.api_server`)
        # and llama-cpp-python (`python -m llama_cpp.server`)
        "python",
        "python3",
        # LM Studio
        "lmstudio",
        "lms",
        # KoboldCpp
        "koboldcpp",
        # Jan inference backend (current: cortex-cpp, legacy: nitro)
        "cortex-cpp",
        "nitro",
    ]

    # gpu_utilization_pct threshold above which even high-priority requests back off
    high_priority_util_threshold: int = 80

    # total VRAM used by external consumers (MB) below which the GPU is still treated as free.
    # Handles background Windows processes (dwm.exe, browser GPU processes, Discord, etc.)
    # that always hold a small amount of VRAM. Default 500 MB.
    external_vram_threshold_mb: int = 500

    # When per-process VRAM is unavailable (common on Windows — every mem_mb is 0), treat the
    # GPU as busy if a non-desktop process is on the GPU, or if utilization exceeds this percent.
    # Default 40 (util is a secondary signal; process presence is primary on WDDM).
    external_util_fallback_threshold: int = 40

    # Consecutive free polls required before restarting the LLM server after a drain.
    # Prevents util-dip thrashing while a game is still loading.
    server_restart_stable_secs: int = 5

    # seconds to treat GPU as free after a model change is detected
    model_load_immunity_secs: int = 60

    # shell command to download a model for non-Ollama backends.
    # {model} is replaced with the model name from the request body.
    # e.g. "huggingface-cli download {model}"
    # When unset, /api/pull is forwarded to the upstream like any other request.
    pull_command: str | None = None

    # how long (seconds) to hold inference requests waiting for a pull to finish
    # before giving up with a 503
    pull_hold_timeout_secs: int = 300

    # Process name stopped after unload when SERVER_KILL_PROCESSES=true (e.g. "ollama").
    # Matches "stop the Ollama container" — unload alone often leaves CUDA/driver state
    # that exclusive-fullscreen games dislike even when nvidia-smi looks free.
    server_process: str | None = None

    # If true, stop SERVER_PROCESS (and related runners) after unloading models.
    # Prefer soft terminate (SERVER_FORCE_KILL=false). Hard-killing CUDA runners mid-frame
    # on Windows WDDM can TDR and take Steam/games/Elgato with them.
    server_kill_processes: bool = False

    # If true with SERVER_KILL_PROCESSES, force-kill processes that ignore terminate.
    # Default false — safer on gaming PCs after a successful model unload.
    server_force_kill: bool = False

    # Shell command to restart the LLM server after a game exits (only if stop was enabled).
    # Long-running commands like `ollama serve` are spawned in the background; the gate
    # waits for UPSTREAM_URL /api/tags to become healthy, then optionally preloads models
    # while still returning 429 (STARTING), then flips to RUNNING.
    # e.g. "ollama serve" or "llama-server --model /models/qwen2.5-7b.gguf"
    server_start_command: str | None = None

    # After restarting the LLM server, preload remembered models (from /api/ps at drain)
    # before clearing 429 / entering RUNNING.
    server_preload_on_start: bool = True

    # keep_alive passed when preloading (Ollama duration string or seconds).
    server_preload_keep_alive: str = "24h"

    # Fallback model to preload if none were loaded at drain time.
    server_preload_model: str | None = None

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)

    @field_validator("ignored_gpu_processes", mode="before")
    @classmethod
    def _coerce_list(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("["):
                return json.loads(v)
            return v.split()
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
