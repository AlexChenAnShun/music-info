"""MusicBrainz 曲目表补齐（音轨号 / 总数 / 光盘号 / 专辑类型）的离线单测。

不联网：直接对 `app._mb_track_info` / `app._mb_attach_tracklists` 喂合成 JSON。
运行：python -m pytest tests/test_mb_tracklist.py
"""
import os
import sys
import tempfile

import pytest

BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
sys.path.insert(0, BACKEND)

pytest.importorskip("fastapi")
pytest.importorskip("mutagen")

# app 在导入时会解析数据/媒体目录，先指向临时目录
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="mtl-data-"))
os.environ.setdefault("MEDIA_DIR", tempfile.mkdtemp(prefix="mtl-media-"))

import app  # noqa: E402

RELEASE = {
    "id": "rel-1",
    "release-group": {"primary-type": "Album", "secondary-types": ["Live"]},
    "media": [
        {"position": 1, "tracks": [
            {"position": 1, "title": "Intro", "recording": {"id": "rec-x"}},
            {"position": 2, "title": "Shape of My Heart", "recording": {"id": "rec-1"}},
        ]},
        {"position": 2, "tracks": [
            {"position": 1, "title": "Bonus", "recording": {"id": "rec-9"}},
        ]},
    ],
}


def test_track_info_by_recording_id():
    info = app._mb_track_info(RELEASE, "rec-1", "Shape of My Heart")
    assert info["track"] == "2"
    assert info["tracktotal"] == "2"      # 该介质内的曲目数
    assert info["disc"] == "1"
    assert info["disctotal"] == "2"       # 两个介质
    assert info["albumtype"] == "Album / Live"


def test_track_info_by_title_when_id_missing():
    info = app._mb_track_info(RELEASE, "", "Bonus")
    assert info["track"] == "1"
    assert info["disc"] == "2"


def test_track_info_miss_returns_empty():
    assert app._mb_track_info(RELEASE, "nope", "不存在的歌") == {}
    assert app._mb_track_info({}, "x", "y") == {}


def test_attach_fills_empty_only_and_caches(monkeypatch):
    calls = []

    def fake_release(release_id):
        calls.append(release_id)
        app._MB_RELEASE_CACHE[release_id] = RELEASE   # 与真实实现一致：写入进程内缓存
        return RELEASE

    monkeypatch.setattr(app, "_mb_release", fake_release)
    monkeypatch.setattr(app, "_MB_RELEASE_CACHE", {}, raising=False)

    out = [
        {"mb_release": "rel-1", "id": "rec-1", "title": "Shape of My Heart", "track": "", "tracktotal": ""},
        {"mb_release": "rel-1", "id": "rec-9", "title": "Bonus", "track": ""},
        {"mb_release": "rel-2", "id": "rec-z", "title": "Zzz", "track": ""},
        {"mb_release": "rel-3", "id": "rec-y", "title": "Yyy", "track": ""},
        {"mb_release": "rel-1", "id": "rec-1b", "title": "Shape of My Heart", "track": "7", "tracktotal": "9"},
    ]
    app._mb_attach_tracklists(out)

    assert out[0]["track"] == "2" and out[0]["tracktotal"] == "2"
    assert out[1]["disc"] == "2"                       # 同专辑第二首也能命中
    assert out[4]["track"] == "7"                      # 已有值不覆盖（只补空白）
    assert out[3]["track"] == ""                       # 超过 max_releases 不查询
    assert calls == ["rel-1", "rel-2"]                 # 每个专辑只查一次
