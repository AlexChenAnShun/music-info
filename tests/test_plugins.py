"""插件库（数据源注册表 v2）：增删改、自定义源、参数生效与统计的测试。

- 注册表单测：默认启用、停用/启用持久化、统计累加、未知 id 报错
- 增删改：编辑内置源（含参数）、删除内置源（可恢复）、自定义源 CRUD
- 自定义源执行：URL 模板展开、字段映射、JSON 路径取值、错误校验
- 接口与行为：/api/plugins*、停用后单源接口被拒绝、融合搜索跳过停用源

运行：python -m pytest tests/test_plugins.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest
import requests

BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
sys.path.insert(0, BACKEND)

pytest.importorskip("fastapi")

BOOT = tempfile.mkdtemp(prefix="mtl-plugins-")
os.environ["MEDIA_DIR"] = str(Path(BOOT) / "music")
os.environ["DATA_DIR"] = str(Path(BOOT) / "data")
Path(os.environ["MEDIA_DIR"]).mkdir(parents=True, exist_ok=True)

import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from mtl_plugins import (PLUGIN_IDS, PLUGINS as _META_LIST, PluginRegistry,  # noqa: E402
                         json_path_get, run_custom_search, validate_custom_config)

_ORIG = {p["id"]: p for p in _META_LIST}
_ORDER = {p["id"]: i for i, p in enumerate(_META_LIST)}

client = TestClient(app.app)

CSV = {
    "display_name": "我的接口",
    "description": "自定义测试源",
    "search_url": "https://api.example.com/search?q={q}&limit={limit}",
    "result_path": "data.list",
    "fields": {"title": "songname", "artist": "singer[0].name",
               "album": "albumname", "cover": "pic", "track": "track_no"},
}


class FakeResp:
    def __init__(self, payload, status=200):
        self.status_code = status
        self.content = payload if isinstance(payload, bytes) else json.dumps(payload).encode()


def fake_provider(payload):
    calls = []

    def _request(method, url, **kw):
        calls.append({"method": method, "url": url, "kw": kw})
        return FakeResp(payload)
    return _request, calls


PAYLOAD = {"data": {"list": [
    {"songname": "Shape of My Heart", "singer": [{"name": "Sting"}],
     "albumname": "Ten Summoner's Tales", "pic": "https://x/cover.jpg", "track_no": 7},
    {"songname": "", "singer": [{"name": "坏数据"}], "albumname": "跳过", "pic": "", "track_no": 1},
]}}


@pytest.fixture
def restore_plugins():
    """用例结束后把注册表恢复原状（删除自定义源、恢复内置源与参数）。"""
    yield
    reg = app._PLUGINS
    for item in reg.list(include_removed=True):
        if not item["builtin"]:
            try:
                reg.delete(item["id"])
            except KeyError:
                pass
        else:
            if item["removed"]:
                reg.restore(item["id"])
            meta = _ORIG[item["id"]]
            reg.save({"id": item["id"], "display_name": meta["display_name"],
                      "description": meta["description"], "order": _ORDER[item["id"]]})
            if item["params_schema"]:
                defaults = {sp["key"]: sp["default"] for sp in item["params_schema"]}
                defaults.pop("user_agent", None)
                reg.save({"id": item["id"], "params": defaults})
            reg.set_enabled(item["id"], True)


# ---------- 注册表基础 ----------
def test_default_all_enabled_and_meta_complete():
    reg = PluginRegistry(tempfile.mkdtemp(prefix="reg-"))
    items = reg.list()
    assert len(items) == len(PLUGIN_IDS)
    for it in items:
        assert it["enabled"] is True
        assert it["builtin"] is True and it["removed"] is False
        assert it["display_name"] and it["kind"] in ("tag_source", "fingerprint", "lyric")
        assert it["stats"] == {"calls": 0, "ok": 0, "fail": 0, "avg_ms": 0, "last_ms": 0, "last_at": 0}


def test_toggle_persists_across_instances():
    root = tempfile.mkdtemp(prefix="reg-")
    reg = PluginRegistry(root)
    reg.set_enabled("deezer", False)
    assert reg.is_enabled("deezer") is False
    again = PluginRegistry(root)
    assert again.is_enabled("deezer") is False
    payload = json.loads((Path(root) / "plugins.json").read_text(encoding="utf-8"))
    assert "deezer" in payload["disabled"]


def test_toggle_unknown_id_raises():
    reg = PluginRegistry(tempfile.mkdtemp(prefix="reg-"))
    with pytest.raises(KeyError):
        reg.set_enabled("nope", False)


def test_record_accumulates_stats():
    reg = PluginRegistry(tempfile.mkdtemp(prefix="reg-"))
    reg.record("migu", True, 100)
    reg.record("migu", True, 300)
    reg.record("migu", False, 200)
    st = next(x for x in reg.list() if x["id"] == "migu")["stats"]
    assert (st["calls"], st["ok"], st["fail"]) == (3, 2, 1)
    assert st["last_ms"] == 200 and st["avg_ms"] == 200
    assert st["last_at"] > 0


# ---------- JSON 路径 ----------
def test_json_path_get():
    data = {"a": {"b": [{"c": 1}, {"c": 2}]}, "list": [1, 2, 3], "x": "v"}
    assert json_path_get(data, "a.b[1].c") == 2
    assert json_path_get(data, "list[0]") == 1
    assert json_path_get(data, "list[-1]") == 3
    assert json_path_get(data, "x") == "v"
    assert json_path_get(data, "") is data
    assert json_path_get(data, "a.b[9].c") is None
    assert json_path_get(data, "nope.deep") is None


# ---------- 自定义源 ----------
def test_validate_custom_config():
    cfg = validate_custom_config(CSV)
    assert cfg["method"] == "GET" and cfg["limit"] == 8 and cfg["timeout"] == 15
    with pytest.raises(ValueError):
        validate_custom_config({**CSV, "search_url": "file:///etc/passwd?q={q}"})
    with pytest.raises(ValueError):
        validate_custom_config({**CSV, "search_url": "https://api.example.com/search"})
    with pytest.raises(ValueError):
        validate_custom_config({**CSV, "fields": {"artist": "singer"}})      # 缺 title
    with pytest.raises(ValueError):
        validate_custom_config({**CSV, "display_name": "  "})
    with pytest.raises(ValueError):
        validate_custom_config({**CSV, "timeout": 999})
    with pytest.raises(ValueError):
        validate_custom_config({**CSV, "fields": {"title": "t", "bad": "x"}})


def test_run_custom_search_maps_fields(monkeypatch):
    _request, calls = fake_provider(PAYLOAD)
    monkeypatch.setattr(requests, "request", _request)
    defn = {"id": "custom-abc", **CSV}
    out = run_custom_search(defn, "Shape of My Heart", 5)
    assert len(out) == 1                                    # 第二行没有 title，被跳过
    song = out[0]
    assert song["source"] == "custom-abc" and song["title"] == "Shape of My Heart"
    assert song["artist"] == "Sting" and song["album"] == "Ten Summoner's Tales"
    assert song["track"] == "7" and song["cover"] == "https://x/cover.jpg"
    assert "Shape%20of%20My%20Heart" in calls[0]["url"] and "limit=5" in calls[0]["url"]
    assert calls[0]["kw"]["headers"]["User-Agent"]


def test_custom_source_crud(restore_plugins):
    reg = app._PLUGINS
    created = reg.save(dict(CSV))
    pid = created["id"]
    assert pid.startswith("custom-") and created["builtin"] is False and created["enabled"] is True
    assert created["params"]["limit"] == 8
    reg.set_enabled(pid, False)
    assert reg.is_enabled(pid) is False
    reg.save({"id": pid, "limit": 12, "display_name": "改名了",
              "headers": {"X-Key": "v"}, "timeout": 20})
    item = reg.get(pid)
    assert item["display_name"] == "改名了" and item["params"]["limit"] == 12
    assert item["custom"]["headers"] == {"X-Key": "v"} and item["custom"]["timeout"] == 20
    reg.delete(pid)                                          # 自定义源真删
    assert reg.get(pid) is None
    assert pid not in {p["id"] for p in reg.list(include_removed=True)}


def test_builtin_edit_and_delete_restore(restore_plugins):
    reg = app._PLUGINS
    reg.save({"id": "kuwo", "display_name": "酷我（改名）", "description": "自己写的说明", "order": 42})
    item = reg.get("kuwo")
    assert item["display_name"] == "酷我（改名）" and item["order"] == 42
    assert item["builtin"] is True
    reg.save({"id": "mb", "params": {"limit": 3, "user_agent": "MyUA/9"}})
    assert reg.param("mb", "limit") == 3 and reg.param("mb", "user_agent") == "MyUA/9"
    with pytest.raises(ValueError):
        reg.save({"id": "mb", "params": {"limit": 999}})
    # 删除内置源（可恢复）
    reg.delete("kuwo")
    assert "kuwo" not in {p["id"] for p in reg.list()}
    snap = reg.snapshot(include_removed=True)
    assert next(p for p in snap["plugins"] if p["id"] == "kuwo")["removed"] is True
    assert reg.is_enabled("kuwo") is False
    with pytest.raises(ValueError):
        reg.set_enabled("kuwo", True)                        # 已删除的源不能直接启用
    reg.restore("kuwo")
    assert reg.is_enabled("kuwo") is True
    assert reg.get("kuwo")["removed"] is False


# ---------- HTTP 接口 ----------
def test_plugins_endpoint_shape():
    r = client.get("/api/plugins")
    assert r.status_code == 200
    body = r.json()
    assert {x["id"] for x in body["plugins"]} == set(PLUGIN_IDS)
    assert body["counts"]["total"] == len(PLUGIN_IDS)
    assert body["custom_fields"] and body["methods"] == ["GET", "POST"]
    assert all("enabled" in x and "stats" in x and "params" in x for x in body["plugins"])


def test_toggle_endpoint_and_unknown_404(restore_plugins):
    r = client.post("/api/plugins/toggle", json={"id": "kuwo", "enabled": False})
    assert r.status_code == 200
    assert next(x for x in r.json()["plugins"] if x["id"] == "kuwo")["enabled"] is False
    assert client.post("/api/plugins/toggle", json={"id": "zzz", "enabled": True}).status_code == 404
    client.post("/api/plugins/toggle", json={"id": "kuwo", "enabled": True})


def test_save_endpoint_create_edit_and_errors(restore_plugins):
    r = client.post("/api/plugins/save", json=dict(CSV))
    assert r.status_code == 200
    pid = r.json()["plugin"]["id"]
    assert r.json()["plugin"]["builtin"] is False
    # 参数非法 → 400 且带中文提示
    r2 = client.post("/api/plugins/save", json={**CSV, "id": pid, "limit": 99})
    assert r2.status_code == 400 and "条数" in r2.text
    # 内置源编辑
    r3 = client.post("/api/plugins/save", json={"id": "migu", "display_name": "咪咕（新名）"})
    assert r3.status_code == 200 and r3.json()["plugin"]["display_name"] == "咪咕（新名）"
    # 未知 id → 404
    assert client.post("/api/plugins/save", json={"id": "nope", "display_name": "x"}).status_code == 404
    client.post("/api/plugins/delete", json={"id": pid})


def test_delete_restore_endpoints(restore_plugins):
    r = client.post("/api/plugins/delete", json={"id": "deezer"})
    assert r.status_code == 200 and r.json()["deleted"]["kind"] == "builtin"
    ids = [x["id"] for x in r.json()["plugins"]]
    assert "deezer" not in ids
    got = client.get("/api/plugins?include_removed=1").json()
    assert next(x for x in got["plugins"] if x["id"] == "deezer")["removed"] is True
    assert got["counts"]["removed"] >= 1
    assert client.post("/api/plugins/restore", json={"id": "deezer"}).status_code == 200
    assert client.post("/api/plugins/restore", json={"id": "deezer"}).status_code == 404
    assert "deezer" in [x["id"] for x in client.get("/api/plugins").json()["plugins"]]


def test_plugins_test_endpoint(monkeypatch, restore_plugins):
    _request, _calls = fake_provider(PAYLOAD)
    monkeypatch.setattr(requests, "request", _request)
    # 保存前用 config 试跑
    r = client.post("/api/plugins/test", json={"config": CSV, "q": "Shape of My Heart"})
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["count"] == 1
    assert r.json()["sample"][0]["title"] == "Shape of My Heart"
    # 内置源不可试跑
    assert client.post("/api/plugins/test", json={"id": "migu"}).status_code == 400
    # 出错时返回 ok=false 与错误信息，而非 500
    monkeypatch.setattr(requests, "request", lambda *a, **k: FakeResp(b"not json"))
    r2 = client.post("/api/plugins/test", json={"config": CSV})
    assert r2.status_code == 200 and r2.json()["ok"] is False and "JSON" in r2.json()["error"]


# ---------- 行为 ----------
def test_disabled_source_rejected_and_search_skipped(monkeypatch, restore_plugins):
    calls = []

    def fake_migu(q, limit=10):
        calls.append(q)
        return [{"source": "migu", "title": q, "artist": "", "album": ""}]

    monkeypatch.setattr(app, "migu_search_raw", fake_migu)
    monkeypatch.setattr(app, "_SMART_JOBS", [("migu", fake_migu)])

    def migu_stats():
        return next(x for x in app._PLUGINS.list() if x["id"] == "migu")["stats"]

    before = migu_stats()["calls"]
    app._PLUGINS.set_enabled("migu", False)
    r = client.get("/api/migu_search", params={"q": "test"})
    assert r.status_code == 502 and "plugin disabled" in r.text
    calls.clear()
    app.smart_search_raw("test", limit=5)
    assert "migu" not in calls
    assert migu_stats()["calls"] == before
    app._PLUGINS.set_enabled("migu", True)
    r2 = client.get("/api/migu_search", params={"q": "test"})
    assert r2.status_code == 200 and r2.json()["results"][0]["source"] == "migu"
    assert migu_stats()["calls"] == before + 1


def test_smart_search_uses_custom_source_and_params(monkeypatch, restore_plugins):
    _request, calls = fake_provider(PAYLOAD)
    monkeypatch.setattr(requests, "request", _request)
    monkeypatch.setattr(app, "_SMART_JOBS", [])
    created = app._PLUGINS.save(dict(CSV))
    pid = created["id"]
    try:
        out = app.smart_search_raw("Shape of My Heart", limit=5)
        assert out and out[0]["source"] == pid
        assert "Shape%20of%20My%20Heart" in calls[0]["url"]
        st = next(x for x in app._PLUGINS.list() if x["id"] == pid)["stats"]
        assert st["calls"] >= 1 and st["ok"] >= 1
        # 停用后融合搜索不再调用该源
        app._PLUGINS.set_enabled(pid, False)
        calls.clear()
        app.smart_search_raw("Shape of My Heart", limit=5)
        assert calls == []
    finally:
        app._PLUGINS.delete(pid)


def test_builtin_limit_param_applies_to_fusion(monkeypatch, restore_plugins):
    seen = []

    def fake_itunes(q, limit=8):
        seen.append(limit)
        return []

    monkeypatch.setattr(app, "itunes_search_raw", fake_itunes)
    monkeypatch.setattr(app, "_SMART_JOBS", [("itunes", fake_itunes)])
    app._PLUGINS.save({"id": "itunes", "params": {"limit": 3}})
    app.smart_search_raw("x", limit=10)
    assert seen == [3]
    app._PLUGINS.save({"id": "itunes", "params": {"limit": 8}})
    app.smart_search_raw("x", limit=10)
    assert seen[-1] == 8
