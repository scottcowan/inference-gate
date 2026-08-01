"""Live smoke tests against a running inference-gate instance.

Skip automatically when the gate is unreachable.

  pytest tests/test_smoke.py
  INFERENCE_GATE_URL=http://192.168.0.253:11435 pytest tests/test_smoke.py
"""

from __future__ import annotations

import os

import httpx
import pytest

GATE_URL = os.environ.get("INFERENCE_GATE_URL", "http://127.0.0.1:11435").rstrip("/")
# Prefer a short model name; override if your install differs.
SMOKE_MODEL = os.environ.get("INFERENCE_GATE_SMOKE_MODEL", "gemma4:12b-it-q4_K_M")


def _gate_up() -> bool:
    try:
        r = httpx.get(f"{GATE_URL}/health", timeout=2.0)
        return r.status_code == 200
    except (httpx.HTTPError, OSError):
        return False


pytestmark = pytest.mark.skipif(not _gate_up(), reason=f"inference-gate not reachable at {GATE_URL}")


@pytest.fixture(scope="module")
def client() -> httpx.Client:
    with httpx.Client(base_url=GATE_URL, timeout=httpx.Timeout(300.0, connect=5.0)) as c:
        yield c


def test_health(client: httpx.Client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_gpu(client: httpx.Client):
    r = client.get("/gpu")
    assert r.status_code == 200
    body = r.json()
    for key in (
        "gpu_utilization_pct",
        "memory_used_mb",
        "memory_total_mb",
        "memory_used_pct",
        "external_consumers",
        "free",
        "server",
    ):
        assert key in body
    assert isinstance(body["external_consumers"], list)
    assert body["server"]["state"] in {"running", "draining", "down", "starting"}
    # None mem_mb must not appear (Windows NVML quirk)
    for p in body["external_consumers"]:
        assert p.get("mem_mb") is not None


def test_proxy_tags(client: httpx.Client):
    r = client.get("/api/tags", headers={"X-Priority": "normal"})
    assert r.status_code == 200
    models = r.json().get("models", [])
    assert isinstance(models, list)
    assert len(models) >= 1


def test_head_gate_status(client: httpx.Client):
    r = client.head("/api/tags")
    assert r.status_code in {200, 429}
    assert "X-Server-State" in r.headers
    assert "X-External-VRAM-MB" in r.headers


def test_generate_normal(client: httpx.Client):
    r = client.post(
        "/api/generate",
        headers={"X-Priority": "normal", "Content-Type": "application/json"},
        json={
            "model": SMOKE_MODEL,
            "prompt": "Reply with exactly: ok",
            "stream": False,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "response" in body
    assert len(body["response"].strip()) > 0


def test_generate_low_same_model(client: httpx.Client):
    """After a model is loaded, low priority should forward (no switch)."""
    r = client.post(
        "/api/generate",
        headers={"X-Priority": "low", "Content-Type": "application/json"},
        json={
            "model": SMOKE_MODEL,
            "prompt": "Reply with exactly: ok",
            "stream": False,
        },
    )
    assert r.status_code == 200, r.text
    assert len(r.json().get("response", "").strip()) > 0
