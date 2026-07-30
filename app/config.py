from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    ollama_url: str = "http://ollama:11434"
    proxy_port: int = 11435
    stats_port: int = 9500

    # Process names that are expected GPU consumers — never trigger backoff
    ignored_gpu_processes: list[str] = ["docker", "dockerd", "com.docker.backend"]

    # gpu_utilization_pct threshold above which even high-priority requests back off
    high_priority_util_threshold: int = 80

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)


@lru_cache
def get_settings() -> Settings:
    return Settings()
