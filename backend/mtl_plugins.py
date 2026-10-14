"""Music Info 插件（数据源）注册表。

职责：
- 元数据：内置数据源清单（与 app.py 中各 `*_search_raw` 一一对应）+ 用户自定义 HTTP 数据源
- 状态：启用 / 停用；内置源删除（可恢复）；调用统计
- 配置：内置源可覆盖显示名 / 描述 / 排序 / 参数；自定义源支持完整增删改
- 执行：自定义源按「URL 模板 + 字段映射」执行检索

实现原则：
- 读操作零副作用；写盘用「临时文件 + 原子替换」，并用锁保护
- 统计写盘做节流（默认 5s），避免高频搜索时频繁 IO；退出时兜底落盘
- 未知插件 id 一律视为「启用」，避免内部拼写差异把数据源静默关掉
- 删除内置源 = 从列表移除（可恢复）；删除自定义源 = 真删
"""

import atexit
import json
import os
import re
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

_SAVE_INTERVAL = 5.0
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024       # 自定义源响应体上限
_MAX_HEADERS = 20
_MAX_HEADER_VALUE = 500
ALLOWED_METHODS = ("GET", "POST")
FIELD_KEYS = ("id", "title", "artist", "album", "date", "track", "genre", "cover")
_SCHEME_RE = re.compile(r"^https?://", re.I)

# 参数 schema：内置源可编辑并真实生效的检索参数
_COMMON_LIMIT = {"key": "limit", "label": "融合搜索条数", "type": "int",
                 "min": 1, "max": 50, "default": 8,
                 "hint": "该数据源在「智能匹配 · 多源融合」中最多返回多少条"}
_MB_USER_AGENT = {"key": "user_agent", "label": "User-Agent", "type": "str",
                  "default": "MusicInfo/1.0 (selfhosted)", "max_len": 160,
                  "hint": "MusicBrainz 要求的应用标识"}

# 内置插件清单：与 app.py 的搜索实现对应
PLUGINS = [
    {
        "id": "migu",
        "display_name": "咪咕音乐",
        "kind": "tag_source",
        "description": "咪咕音乐标签源，支持标题、艺术家、专辑与封面；华语曲库命中率较高。",
        "author": "Music Info",
        "version": "1.0.0",
        "homepage": "https://music.migu.cn",
        "params_schema": [_COMMON_LIMIT],
    },
    {
        "id": "qq",
        "display_name": "QQ 音乐",
        "kind": "tag_source",
        "description": "QQ 音乐标签源，支持标题、艺术家、专辑、发行时间与封面。",
        "author": "Music Info",
        "version": "1.0.0",
        "homepage": "https://y.qq.com",
        "params_schema": [_COMMON_LIMIT],
    },
    {
        "id": "netease",
        "display_name": "网易云音乐",
        "kind": "tag_source",
        "description": "网易云音乐标签源，支持标题、艺术家、专辑、年份、音轨号与封面。",
        "author": "Music Info",
        "version": "1.0.0",
        "homepage": "https://music.163.com",
        "params_schema": [_COMMON_LIMIT],
    },
    {
        "id": "kugou",
        "display_name": "酷狗音乐",
        "kind": "tag_source",
        "description": "酷狗音乐标签源，支持标题、艺术家、专辑、发布时间与封面。",
        "author": "Music Info",
        "version": "1.0.0",
        "homepage": "https://www.kugou.com",
        "params_schema": [_COMMON_LIMIT],
    },
    {
        "id": "kuwo",
        "display_name": "酷我音乐",
        "kind": "tag_source",
        "description": "酷我音乐标签源，支持标题、艺术家、专辑与封面。",
        "author": "Music Info",
        "version": "1.0.0",
        "homepage": "https://www.kuwo.cn",
        "params_schema": [_COMMON_LIMIT],
    },
    {
        "id": "itunes",
        "display_name": "iTunes / Apple Music",
        "kind": "tag_source",
        "description": "iTunes 标签源，字段最全：含音轨号、总量、光盘号、流派与专辑类型。",
        "author": "Music Info",
        "version": "1.0.0",
        "homepage": "https://music.apple.com",
        "params_schema": [_COMMON_LIMIT],
    },
    {
        "id": "mb",
        "display_name": "MusicBrainz",
        "kind": "tag_source",
        "description": "MusicBrainz 标签源：权威发行数据，附带曲目表（音轨号 / 总数 / 光盘号 / 专辑类型）。",
        "author": "Music Info",
        "version": "1.0.0",
        "homepage": "https://musicbrainz.org",
        "params_schema": [_COMMON_LIMIT, _MB_USER_AGENT],
    },
    {
        "id": "deezer",
        "display_name": "Deezer",
        "kind": "tag_source",
        "description": "Deezer 标签源，支持标题、艺术家、专辑与封面。",
        "author": "Music Info",
        "version": "1.0.0",
        "homepage": "https://www.deezer.com",
        "params_schema": [_COMMON_LIMIT],
    },
    {
        "id": "acoustid",
        "display_name": "声纹识别（AcoustID）",
        "kind": "fingerprint",
        "description": "本地 chromaprint 指纹匹配，适合标签完全错误或缺失的曲子。",
        "author": "Music Info",
        "version": "1.0.0",
        "homepage": "https://acoustid.org",
        "params_schema": [],
    },
    {
        "id": "lrclib",
        "display_name": "LRCLIB 歌词",
        "kind": "lyric",
        "description": "无需密钥的歌词兜底源，支持同步歌词（LRC）。",
        "author": "Music Info",
        "version": "1.0.0",
        "homepage": "https://lrclib.net",
        "params_schema": [],
    },
]

PLUGIN_IDS = [p["id"] for p in PLUGINS]
_META = {p["id"]: p for p in PLUGINS}


def _empty_stats():
    return {"calls": 0, "ok": 0, "fail": 0, "avg_ms": 0, "last_ms": 0, "last_at": 0}


# ---------------------------------------------------------------- 值校验
def _clean_str(v, max_len, label, required=False):
    s = str(v if v is not None else "").strip()
    if required and not s:
        raise ValueError(label + "不能为空")
    if len(s) > max_len:
        raise ValueError(label + "最长 %d 个字符" % max_len)
    return s


def _clean_int(v, lo, hi, label):
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ValueError(label + "必须是整数")
    if not (lo <= n <= hi):
        raise ValueError(label + "需在 %d–%d 之间" % (lo, hi))
    return n


def _check_params(schema, incoming):
    """按 schema 校验内置源参数，只保留声明过的键。"""
    if not isinstance(incoming, dict):
        raise ValueError("参数必须是对象")
    out = {}
    for sp in schema:
        key = sp["key"]
        if key not in incoming:
            continue
        v = incoming[key]
        if sp["type"] == "int":
            out[key] = _clean_int(v, sp["min"], sp["max"], sp["label"])
        elif sp["type"] == "float":
            try:
                f = float(v)
            except (TypeError, ValueError):
                raise ValueError(sp["label"] + "必须是数字")
            if not (sp["min"] <= f <= sp["max"]):
                raise ValueError("%s需在 %s–%s 之间" % (sp["label"], sp["min"], sp["max"]))
            out[key] = f
        else:
            out[key] = _clean_str(v, sp.get("max_len", 200), sp["label"])
    return out


def validate_custom_config(cfg):
    """校验自定义源配置，返回规范化后的字段。"""
    name = _clean_str(cfg.get("display_name"), 60, "显示名", required=True)
    url = _clean_str(cfg.get("search_url"), 500, "搜索 URL", required=True)
    if not _SCHEME_RE.match(url):
        raise ValueError("搜索 URL 必须以 http:// 或 https:// 开头")
    if "{q}" not in url:
        raise ValueError("搜索 URL 需要包含 {q} 占位符（可用 {limit} 传条数）")
    method = str(cfg.get("method") or "GET").upper()
    if method not in ALLOWED_METHODS:
        raise ValueError("请求方法只支持 GET / POST")
    headers = cfg.get("headers") or {}
    if not isinstance(headers, dict):
        raise ValueError("请求头必须是 JSON 对象")
    if len(headers) > _MAX_HEADERS:
        raise ValueError("请求头最多 %d 条" % _MAX_HEADERS)
    headers = {str(k)[:80]: str(v)[:_MAX_HEADER_VALUE] for k, v in headers.items()}
    try:
        timeout = float(cfg.get("timeout") if cfg.get("timeout") not in (None, "") else 15)
    except (TypeError, ValueError):
        raise ValueError("超时必须是数字")
    if not (3 <= timeout <= 60):
        raise ValueError("超时需在 3–60 秒之间")
    limit = _clean_int(cfg.get("limit") if cfg.get("limit") not in (None, "") else 8, 1, 50, "条数")
    result_path = _clean_str(cfg.get("result_path"), 200, "结果路径")
    fields = cfg.get("fields") or {}
    if not isinstance(fields, dict):
        raise ValueError("字段映射必须是 JSON 对象")
    bad = [k for k in fields if k not in FIELD_KEYS]
    if bad:
        raise ValueError("未知映射字段: " + ", ".join(sorted(bad)))
    clean_fields = {}
    for k, v in fields.items():
        s = _clean_str(v, 200, "字段映射 %s" % k)
        if s:
            clean_fields[k] = s
    if not clean_fields.get("title"):
        raise ValueError("字段映射必须包含 title（标题对应的响应字段）")
    return {"display_name": name, "description": _clean_str(cfg.get("description"), 300, "描述"),
            "search_url": url, "method": method, "headers": headers, "timeout": timeout,
            "limit": limit, "result_path": result_path, "fields": clean_fields}


# ---------------------------------------------------------------- JSON 取值
def _split_path(path):
    parts = []
    for m in re.finditer(r"\[\s*(-?\d+)\s*\]|([^.\[\]]+)", str(path or "")):
        if m.group(1) is not None:
            parts.append(int(m.group(1)))
        else:
            tok = m.group(2).strip()
            if tok:
                parts.append(tok)
    return parts


def json_path_get(obj, path):
    """按 `a.b[0].c` 语法取 JSON 值；空路径返回原对象；取不到返回 None。"""
    if path is None:
        return None
    if str(path).strip() == "":
        return obj
    cur = obj
    for part in _split_path(path):
        if isinstance(part, int):
            if isinstance(cur, (list, tuple)) and -len(cur) <= part < len(cur):
                cur = cur[part]
            else:
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, (list, tuple)) and str(part).isdigit():
            i = int(part)
            cur = cur[i] if 0 <= i < len(cur) else None
        else:
            return None
        if cur is None:
            return None
    return cur


# ---------------------------------------------------------------- 自定义源执行
def _song(src_id, d):
    """与 app.py `_norm` 同构的歌曲条目。"""
    out = {"source": src_id, "id": "", "title": "", "artist": "", "album": "", "date": "",
           "track": "", "tracktotal": "", "disc": "", "disctotal": "", "language": "",
           "albumtype": "", "comment": "", "description": "", "version": "",
           "cover": "", "genre": ""}
    for k in out:
        v = d.get(k)
        if v is not None and v != "":
            out[k] = str(v)
    return out


def expand_url(template, q, limit):
    return (str(template or "")
            .replace("{q}", urllib.parse.quote(str(q), safe=""))
            .replace("{limit}", str(int(limit))))


def run_custom_search(defn, q, limit=None):
    """执行一次自定义源检索，返回与内置源同构的结果列表。"""
    try:
        import requests
    except Exception:
        raise RuntimeError("requests 不可用，无法执行自定义源")
    limit = int(limit or defn.get("limit") or 8)
    url = expand_url(defn.get("search_url"), q, limit)
    if not _SCHEME_RE.match(url):
        raise ValueError("自定义源 URL 必须以 http:// 或 https:// 开头")
    method = str(defn.get("method") or "GET").upper()
    if method not in ALLOWED_METHODS:
        raise ValueError("请求方法只支持 GET / POST")
    headers = {"User-Agent": "MusicInfo/1.0 (plugins)"}
    headers.update({str(k): str(v) for k, v in (defn.get("headers") or {}).items()})
    timeout = float(defn.get("timeout") or 15)
    resp = requests.request(method, url, headers=headers, timeout=(5, timeout))
    if resp.status_code >= 400:
        raise RuntimeError("接口返回 HTTP %s" % resp.status_code)
    body = resp.content or b""
    if len(body) > _MAX_RESPONSE_BYTES:
        raise RuntimeError("响应超过 10MB 上限")
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        raise RuntimeError("响应不是合法 JSON")
    items = json_path_get(data, defn.get("result_path"))
    if items is None:
        items = []
    if isinstance(items, dict):
        items = list(items.values())
    if not isinstance(items, list):
        raise RuntimeError("结果路径未指向数组，请检查「结果路径」")
    fields = defn.get("fields") or {}
    out = []
    for it in items:
        if len(out) >= limit:
            break
        def g(key):
            p = str(fields.get(key) or "").strip()
            return json_path_get(it, p) if p else None
        title = str(g("title") or "").strip()
        if not title:
            continue
        row = {k: (g(k) if g(k) is not None else "") for k in FIELD_KEYS}
        row["title"] = title
        out.append(_song(defn.get("id") or "custom", row))
    return out


def test_search(defn, q="test", limit=5):
    """试跑一次自定义源，返回样例与耗时（不写统计）。"""
    t0 = time.time()
    try:
        songs = run_custom_search(defn, q, limit)
    except Exception as e:
        return {"ok": False, "count": 0, "sample": [], "error": "%s: %s" % (type(e).__name__, e),
                "elapsed_ms": int((time.time() - t0) * 1000)}
    return {"ok": True, "count": len(songs), "sample": songs[:3], "error": "",
            "elapsed_ms": int((time.time() - t0) * 1000)}


# ---------------------------------------------------------------- 注册表
class PluginRegistry:
    """线程安全的插件状态 / 配置 / 统计存储（JSON 文件）。"""

    def __init__(self, data_dir):
        self._path = Path(data_dir) / "plugins.json"
        self._lock = threading.RLock()
        self._last_save = 0.0
        self._data = self._load()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        atexit.register(self.flush)

    # ---------- 存储 ----------
    def _load(self):
        defaults = {"version": 2, "disabled": [], "removed": [], "stats": {},
                    "overrides": {}, "custom": []}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        data = dict(defaults)
        for k in ("disabled", "removed"):
            v = raw.get(k)
            data[k] = [str(x) for x in v] if isinstance(v, list) else []
        for k in ("stats", "overrides"):
            v = raw.get(k)
            data[k] = dict(v) if isinstance(v, dict) else {}
        v = raw.get("custom")
        data["custom"] = [c for c in v if isinstance(c, dict) and c.get("id")] if isinstance(v, list) else []
        data["version"] = 2
        return data

    def _save_locked(self):
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)
        self._last_save = time.time()

    def flush(self):
        with self._lock:
            self._save_locked()

    # ---------- 查询 ----------
    def is_enabled(self, pid):
        with self._lock:
            return pid not in self._data["disabled"] and pid not in self._data["removed"]

    def is_removed(self, pid):
        with self._lock:
            return pid in self._data["removed"]

    def params(self, pid):
        with self._lock:
            meta = _META.get(pid)
            if meta is not None:
                ov = self._data["overrides"].get(pid) or {}
                return self._params_of(meta, ov)
            c = self._find_custom(pid)
            if c is not None:
                return {"limit": int(c.get("limit") or 8), "timeout": float(c.get("timeout") or 15)}
            return {}

    def param(self, pid, key, default=None):
        v = self.params(pid).get(key, None)
        return default if v is None else v

    def custom_definitions(self):
        with self._lock:
            return [dict(c) for c in self._data["custom"]]

    def custom_definition(self, pid):
        with self._lock:
            c = self._find_custom(pid)
            return dict(c) if c is not None else None

    @staticmethod
    def _params_of(meta, ov):
        out = {}
        for sp in meta.get("params_schema", []):
            out[sp["key"]] = sp.get("default")
        out.update((ov or {}).get("params") or {})
        return out

    def _find_custom(self, pid):
        for c in self._data["custom"]:
            if c.get("id") == pid:
                return c
        return None

    def list(self, include_removed=False):
        with self._lock:
            disabled = set(self._data["disabled"])
            removed = set(self._data["removed"])
            stats = {k: dict(v) for k, v in self._data["stats"].items()}
            overrides = {k: dict(v) for k, v in self._data["overrides"].items()}
            custom = [dict(c) for c in self._data["custom"]]
        out = []
        for idx, meta in enumerate(PLUGINS):
            ov = overrides.get(meta["id"], {})
            pid = meta["id"]
            item = {
                "id": pid, "builtin": True, "kind": meta["kind"],
                "author": meta.get("author", "Music Info"),
                "version": meta.get("version", "1.0.0"),
                "homepage": meta.get("homepage", ""),
                "display_name": ov.get("display_name") or meta["display_name"],
                "description": ov.get("description") or meta["description"],
                "order": ov.get("order", idx),
                "params_schema": [dict(sp) for sp in meta.get("params_schema", [])],
                "params": self._params_of(meta, ov),
                "enabled": pid not in disabled and pid not in removed,
                "removed": pid in removed,
                "stats": stats.get(pid) or _empty_stats(),
            }
            out.append(item)
        for idx, c in enumerate(custom):
            pid = c["id"]
            item = {
                "id": pid, "builtin": False, "kind": c.get("kind", "tag_source"),
                "author": "自定义", "version": c.get("version", "1.0.0"), "homepage": c.get("homepage", ""),
                "display_name": c.get("display_name", ""), "description": c.get("description", ""),
                "order": c.get("order", 100 + idx),
                "params_schema": [dict(_COMMON_LIMIT),
                                  {"key": "timeout", "label": "超时（秒）", "type": "float",
                                   "min": 3, "max": 60, "default": 15}],
                "params": {"limit": int(c.get("limit") or 8), "timeout": float(c.get("timeout") or 15)},
                "enabled": pid not in disabled and pid not in removed,
                "removed": False,
                "custom": {k: c.get(k) for k in ("search_url", "method", "headers", "timeout",
                                                 "limit", "result_path", "fields", "created_at")},
                "stats": stats.get(pid) or _empty_stats(),
            }
            out.append(item)
        out.sort(key=lambda x: (x.get("order", 0), x.get("display_name", "")))
        if not include_removed:
            out = [x for x in out if not x["removed"]]
        return out

    def get(self, pid):
        for item in self.list(include_removed=True):
            if item["id"] == pid:
                return item
        return None

    def snapshot(self, include_removed=False):
        all_items = self.list(include_removed=True)
        active = [x for x in all_items if not x["removed"]]
        items = all_items if include_removed else active
        return {
            "plugins": items,
            "counts": {"total": len(active),
                       "enabled": sum(1 for x in active if x["enabled"]),
                       "removed": sum(1 for x in all_items if x["removed"]),
                       "custom": sum(1 for x in active if not x["builtin"])},
            "custom_fields": list(FIELD_KEYS),
            "methods": list(ALLOWED_METHODS),
        }

    # ---------- 修改 ----------
    def set_enabled(self, pid, enabled):
        if pid not in _META and self.custom_definition(pid) is None:
            raise KeyError(pid)
        if self.is_removed(pid):
            raise ValueError("插件已删除，请先恢复")
        with self._lock:
            disabled = self._data["disabled"]
            if enabled and pid in disabled:
                disabled.remove(pid)
            elif not enabled and pid not in disabled:
                disabled.append(pid)
            self._save_locked()
        return self.is_enabled(pid)

    def save(self, payload):
        """创建（无 id）或更新（有 id）插件；返回更新后的条目。"""
        payload = dict(payload or {})
        pid = str(payload.get("id") or "").strip()
        with self._lock:
            if pid and pid in _META:
                ov = self._data["overrides"].setdefault(pid, {})
                meta = _META[pid]
                if payload.get("display_name") is not None:
                    ov["display_name"] = _clean_str(payload["display_name"], 60, "显示名", required=True)
                if payload.get("description") is not None:
                    ov["description"] = _clean_str(payload["description"], 300, "描述")
                if payload.get("order") is not None:
                    ov["order"] = _clean_int(payload["order"], 0, 999, "排序")
                if payload.get("params") is not None:
                    ov["params"] = _check_params(meta.get("params_schema", []), payload["params"])
                if not ov:
                    self._data["overrides"].pop(pid, None)
            elif pid:
                c = self._find_custom(pid)
                if c is None:
                    raise KeyError(pid)
                merged = dict(c)
                merged.update({k: v for k, v in payload.items() if v is not None and k != "id"})
                clean = validate_custom_config(merged)
                c.update(clean)
                if payload.get("kind"):
                    c["kind"] = str(payload["kind"])
            else:
                base = {"id": "custom-" + uuid.uuid4().hex[:8], "kind": "tag_source",
                        "display_name": "", "description": "",
                        "order": 100 + len(self._data["custom"]), "search_url": "",
                        "method": "GET", "headers": {}, "timeout": 15.0, "limit": 8,
                        "result_path": "", "fields": {}, "created_at": int(time.time())}
                base.update({k: v for k, v in payload.items() if v is not None and k != "id"})
                clean = validate_custom_config(base)
                base.update(clean)
                self._data["custom"].append(base)
                pid = base["id"]
            self._save_locked()
        return self.get(pid)

    def delete(self, pid):
        """删除：内置源从列表移除（可恢复）；自定义源真删。"""
        with self._lock:
            if pid in _META:
                if pid not in self._data["removed"]:
                    self._data["removed"].append(pid)
                if pid in self._data["disabled"]:
                    self._data["disabled"].remove(pid)
                kind = "builtin"
            else:
                c = self._find_custom(pid)
                if c is None:
                    raise KeyError(pid)
                self._data["custom"].remove(c)
                self._data["stats"].pop(pid, None)
                if pid in self._data["disabled"]:
                    self._data["disabled"].remove(pid)
                kind = "custom"
            self._save_locked()
        return {"id": pid, "kind": kind}

    def restore(self, pid):
        with self._lock:
            if pid not in self._data["removed"]:
                raise KeyError(pid)
            self._data["removed"].remove(pid)
            if pid in self._data["disabled"]:
                self._data["disabled"].remove(pid)
            self._save_locked()
        return self.get(pid)

    def record(self, pid, ok, ms):
        """记录一次调用。ms 为耗时毫秒。"""
        if pid not in _META and self.custom_definition(pid) is None:
            return
        ms = max(0, int(ms or 0))
        with self._lock:
            st = self._data["stats"].setdefault(pid, _empty_stats())
            st["calls"] += 1
            st["ok" if ok else "fail"] += 1
            st["last_ms"] = ms
            st["last_at"] = int(time.time())
            st["avg_ms"] = int(((st["avg_ms"] * (st["calls"] - 1)) + ms) / st["calls"])
            if time.time() - self._last_save >= _SAVE_INTERVAL:
                self._save_locked()
