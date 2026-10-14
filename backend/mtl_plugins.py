"""Music Info 插件（数据源）注册表。

职责：
- 元数据：内置标签源清单（与 app.py 中各 `*_search_raw` 一一对应）
- 状态：启用 / 停用，持久化到 `DATA_DIR/plugins.json`
- 统计：调用次数、成功 / 失败次数、最近一次耗时与时间、平均耗时

实现原则：
- 读操作零副作用；写盘用「临时文件 + 原子替换」，并用锁保护
- 统计写盘做节流（默认 5s），避免高频搜索时频繁 IO；退出时兜底落盘
- 未知插件 id 一律视为「启用」，避免内部拼写差异把数据源静默关掉
"""

import atexit
import json
import os
import threading
import time
from pathlib import Path

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
    },
    {
        "id": "qq",
        "display_name": "QQ 音乐",
        "kind": "tag_source",
        "description": "QQ 音乐标签源，支持标题、艺术家、专辑、发行时间与封面。",
        "author": "Music Info",
        "version": "1.0.0",
        "homepage": "https://y.qq.com",
    },
    {
        "id": "netease",
        "display_name": "网易云音乐",
        "kind": "tag_source",
        "description": "网易云音乐标签源，支持标题、艺术家、专辑、年份、音轨号与封面。",
        "author": "Music Info",
        "version": "1.0.0",
        "homepage": "https://music.163.com",
    },
    {
        "id": "kugou",
        "display_name": "酷狗音乐",
        "kind": "tag_source",
        "description": "酷狗音乐标签源，支持标题、艺术家、专辑、发布时间与封面。",
        "author": "Music Info",
        "version": "1.0.0",
        "homepage": "https://www.kugou.com",
    },
    {
        "id": "kuwo",
        "display_name": "酷我音乐",
        "kind": "tag_source",
        "description": "酷我音乐标签源，支持标题、艺术家、专辑与封面。",
        "author": "Music Info",
        "version": "1.0.0",
        "homepage": "https://www.kuwo.cn",
    },
    {
        "id": "itunes",
        "display_name": "iTunes / Apple Music",
        "kind": "tag_source",
        "description": "iTunes 标签源，字段最全：含音轨号、总量、光盘号、流派与专辑类型。",
        "author": "Music Info",
        "version": "1.0.0",
        "homepage": "https://music.apple.com",
    },
    {
        "id": "mb",
        "display_name": "MusicBrainz",
        "kind": "tag_source",
        "description": "MusicBrainz 标签源：权威发行数据，附带曲目表（音轨号 / 总数 / 光盘号 / 专辑类型）。",
        "author": "Music Info",
        "version": "1.0.0",
        "homepage": "https://musicbrainz.org",
    },
    {
        "id": "deezer",
        "display_name": "Deezer",
        "kind": "tag_source",
        "description": "Deezer 标签源，支持标题、艺术家、专辑与封面。",
        "author": "Music Info",
        "version": "1.0.0",
        "homepage": "https://www.deezer.com",
    },
    {
        "id": "acoustid",
        "display_name": "声纹识别（AcoustID）",
        "kind": "fingerprint",
        "description": "本地 chromaprint 指纹匹配，适合标签完全错误或缺失的曲子。",
        "author": "Music Info",
        "version": "1.0.0",
        "homepage": "https://acoustid.org",
    },
    {
        "id": "lrclib",
        "display_name": "LRCLIB 歌词",
        "kind": "lyric",
        "description": "无需密钥的歌词兜底源，支持同步歌词（LRC）。",
        "author": "Music Info",
        "version": "1.0.0",
        "homepage": "https://lrclib.net",
    },
]

PLUGIN_IDS = [p["id"] for p in PLUGINS]
_META = {p["id"]: p for p in PLUGINS}
_SAVE_INTERVAL = 5.0  # 统计写盘节流（秒）


def _empty_stats():
    return {"calls": 0, "ok": 0, "fail": 0, "avg_ms": 0, "last_ms": 0, "last_at": 0}


class PluginRegistry:
    def __init__(self, data_dir):
        self.root = Path(data_dir or ".")
        self.path = self.root / "plugins.json"
        self._lock = threading.Lock()
        self._last_save = 0.0
        self._data = self._load()
        atexit.register(self.flush)

    # ---------- 持久化 ----------
    def _load(self):
        data = {}
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            data = {}
        disabled = [x for x in (data.get("disabled") or []) if x in _META]
        stats = {}
        for pid, st in (data.get("stats") or {}).items():
            if pid in _META and isinstance(st, dict):
                merged = _empty_stats()
                merged.update({k: st.get(k, merged[k]) for k in merged})
                stats[pid] = merged
        return {"version": 1, "disabled": disabled, "stats": stats}

    def _save_locked(self):
        self._last_save = time.time()
        payload = json.dumps(self._data, ensure_ascii=False, indent=2)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(payload, encoding="utf-8", newline="\n")
            os.replace(tmp, self.path)
        except OSError:
            pass  # 落盘失败不影响搜索；下次调用再试

    def flush(self):
        with self._lock:
            self._save_locked()

    # ---------- 查询 ----------
    def is_enabled(self, pid):
        with self._lock:
            return pid not in self._data["disabled"]

    def list(self):
        with self._lock:
            disabled = set(self._data["disabled"])
            stats = {k: dict(v) for k, v in self._data["stats"].items()}
        out = []
        for meta in PLUGINS:
            item = dict(meta)
            item["enabled"] = meta["id"] not in disabled
            item["stats"] = stats.get(meta["id"], _empty_stats())
            out.append(item)
        return out

    # ---------- 修改 ----------
    def set_enabled(self, pid, enabled):
        if pid not in _META:
            raise KeyError(pid)
        with self._lock:
            disabled = self._data["disabled"]
            if enabled and pid in disabled:
                disabled.remove(pid)
            elif not enabled and pid not in disabled:
                disabled.append(pid)
            self._save_locked()
        return self.is_enabled(pid)

    def record(self, pid, ok, ms):
        """记录一次调用。ms 为耗时毫秒。"""
        if pid not in _META:
            return
        ms = max(0, int(ms or 0))
        with self._lock:
            st = self._data["stats"].setdefault(pid, _empty_stats())
            st["calls"] += 1
            st["ok" if ok else "fail"] += 1
            st["last_ms"] = ms
            st["last_at"] = int(time.time())
            st["avg_ms"] = int(((st["avg_ms"] * (st["calls"] - 1)) + ms) / st["calls"])
            due = time.time() - self._last_save >= _SAVE_INTERVAL
            if due:
                self._save_locked()

