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
    # Env var accepts JSON array or space-separated string:
    #   IGNORED_GPU_PROCESSES='["ollama","game.exe"]'  or  IGNORED_GPU_PROCESSES="ollama game.exe"
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
