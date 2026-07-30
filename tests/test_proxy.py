import pytest
import respx
import httpx
from fastapi.testclient import TestClient

from app.main import app
from app.gpu import GpuState

FREE_GPU = GpuState(0, 0, 16376, 0.0, [], True)
BUSY_GPU = GpuState(94, 12000, 16376, 73.3, [{"name": "game.exe", "mem_mb": 11200}], False)


@pytest.fixture(autouse=True)
def setup_settings():
    from app.config import get_settings
    app.state.settings = get_settings()


def test_health():
    with TestClient(app) as client:
        # Patch after lifespan runs
        app.state.gpu_query = lambda: FREE_GPU
        r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_stats_endpoint():
    with TestClient(app) as client:
        app.state.gpu_query = lambda: FREE_GPU
        r = client.get("/gpu")
    assert r.status_code == 200
    data = r.json()
    assert data["free"] is True
    assert data["external_consumers"] == []


def test_normal_request_blocked_when_busy():
    with TestClient(app) as client:
        app.state.gpu_query = lambda: BUSY_GPU
        r = client.post("/api/generate", json={"model": "qwen2.5:7b", "prompt": "hi"})
    assert r.status_code == 429
    assert "Retry-After" in r.headers


def test_realtime_request_always_forwarded():
    with respx.mock:
        respx.post("http://ollama:11434/api/generate").mock(
            return_value=httpx.Response(200, json={"response": "ok"})
        )
        with TestClient(app) as client:
            app.state.gpu_query = lambda: BUSY_GPU
            r = client.post(
                "/api/generate",
                json={"model": "qwen2.5:7b", "prompt": "hi"},
                headers={"X-Priority": "realtime"},
            )
    assert r.status_code == 200


def test_normal_request_forwarded_when_free():
    with respx.mock:
        respx.post("http://ollama:11434/api/generate").mock(
            return_value=httpx.Response(200, json={"response": "ok"})
        )
        with TestClient(app) as client:
            app.state.gpu_query = lambda: FREE_GPU
            r = client.post("/api/generate", json={"model": "qwen2.5:7b", "prompt": "hi"})
    assert r.status_code == 200


def test_unknown_priority_header_treated_as_normal():
    with TestClient(app) as client:
        app.state.gpu_query = lambda: BUSY_GPU
        r = client.post(
            "/api/generate",
            json={"model": "qwen2.5:7b", "prompt": "hi"},
            headers={"X-Priority": "garbage"},
        )
    assert r.status_code == 429
