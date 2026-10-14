"""插件库（数据源注册表）与启停生效的测试。

- 注册表单测：默认启用、停用/启用持久化、统计累加、未知 id 报错
- 接口与行为：/api/plugins、/api/plugins/toggle、停用后单源接口被拒绝、融合搜索跳过停用源

运行：python -m pytest tests/test_plugins.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
sys.path.insert(0, BACKEND)

pytest.importorskip("fastapi")

BOOT = tempfile.mkdtemp(prefix="mtl-plugins-")
os.environ["MEDIA_DIR"] = str(Path(BOOT) / "music")
os.environ["DATA_DIR"] = str(Path(BOOT) / "data")
Path(os.environ["MEDIA_DIR"]).mkdir(parents=True, exist_ok=True)

import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from mtl_plugins import PLUGIN_IDS, PluginRegistry  # noqa: E402

client = TestClient(app.app)


# ---------- 注册表单测 ----------
def test_default_all_enabled_and_meta_complete():
    reg = PluginRegistry(tempfile.mkdtemp(prefix="reg-"))
    items = reg.list()
    assert len(items) == len(PLUGIN_IDS)
    for it in items:
        assert it["enabled"] is True
        assert it["display_name"] and it["kind"] in ("tag_source", "fingerprint", "lyric")
        assert it["stats"] == {"calls": 0, "ok": 0, "fail": 0, "avg_ms": 0, "last_ms": 0, "last_at": 0}


def test_toggle_persists_across_instances():
    root = tempfile.mkdtemp(prefix="reg-")
    reg = PluginRegistry(root)
    reg.set_enabled("deezer", False)
    assert reg.is_enabled("deezer") is False
    assert reg.is_enabled("migu") is True
    again = PluginRegistry(root)          # 重新加载同一目录
    assert again.is_enabled("deezer") is False
    payload = json.loads((Path(root) / "plugins.json").read_text(encoding="utf-8"))
    assert "deezer" in payload["disabled"]


def test_toggle_unknown_id_raises():
    reg = PluginRegistry(tempfile.mkdtemp(prefix="reg-"))
    with pytest.raises(KeyError):
        reg.set_enabled("nope", False)


def test_record_accumulates_stats():
    root = tempfile.mkdtemp(prefix="reg-")
    reg = PluginRegistry(root)
    reg.record("migu", True, 100)
    reg.record("migu", True, 300)
    reg.record("migu", False, 200)
    st = next(x for x in reg.list() if x["id"] == "migu")["stats"]
    assert (st["calls"], st["ok"], st["fail"]) == (3, 2, 1)
    assert st["last_ms"] == 200 and st["avg_ms"] == 200
    assert st["last_at"] > 0


# ---------- 接口与行为 ----------
def test_plugins_endpoint_shape():
    r = client.get("/api/plugins")
    assert r.status_code == 200
    items = r.json()["plugins"]
    assert {x["id"] for x in items} == set(PLUGIN_IDS)
    assert all("enabled" in x and "stats" in x for x in items)


def test_toggle_endpoint_and_unknown_404():
    r = client.post("/api/plugins/toggle", json={"id": "kuwo", "enabled": False})
    assert r.status_code == 200
    assert next(x for x in r.json()["plugins"] if x["id"] == "kuwo")["enabled"] is False
    assert client.post("/api/plugins/toggle", json={"id": "zzz", "enabled": True}).status_code == 404
    client.post("/api/plugins/toggle", json={"id": "kuwo", "enabled": True})   # 还原


def test_disabled_source_rejected_and_search_skipped(monkeypatch):
    calls = []

    def fake_migu(q, limit=10):
        calls.append(q)
        return [{"source": "migu", "title": q}]

    def fake_itunes(q, limit=10):
        calls.append("itunes")
        return [{"source": "itunes", "title": q}]

    monkeypatch.setattr(app, "migu_search_raw", fake_migu)
    monkeypatch.setattr(app, "_SMART_JOBS", [("migu", fake_migu)])

    def migu_stats():
        return next(x for x in app._PLUGINS.list() if x["id"] == "migu")["stats"]

    before = migu_stats()["calls"]
    app._PLUGINS.set_enabled("migu", False)
    # 1) 单源接口：停用即拒绝
    r = client.get("/api/migu_search", params={"q": "test"})
    assert r.status_code == 502 and "plugin disabled" in r.text
    # 2) 融合搜索：不再调用停用源
    calls.clear()
    app.smart_search_raw("test", limit=5)
    assert "migu" not in calls
    # 3) 被拒绝 / 被跳过的调用不产生统计（它不是一次真实调用）
    assert migu_stats()["calls"] == before
    app._PLUGINS.set_enabled("migu", True)
    r2 = client.get("/api/migu_search", params={"q": "test"})
    assert r2.status_code == 200 and r2.json()["results"][0]["source"] == "migu"
    st2 = migu_stats()
    assert st2["ok"] >= 1 and st2["calls"] == before + 1   # 启用后正常计数

