import json
from functools import lru_cache
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    ollama_url: str = "http://ollama:11434"
    proxy_port: int = 11435

    # Process names that are expected GPU consumers — never trigger backoff.
    # Env var accepts JSON array or space-separated string:
    #   IGNORED_GPU_PROCESSES='["ollama","game.exe"]'  or  IGNORED_GPU_PROCESSES="ollama game.exe"
    ignored_gpu_processes: list[str] = [
        "docker",
        "dockerd",
        "com.docker.backend",
        "ollama",
        "ollama_llama_server",
    ]

    # gpu_utilization_pct threshold above which even high-priority requests back off
    high_priority_util_threshold: int = 80

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
