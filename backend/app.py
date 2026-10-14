import base64
import hashlib
import json
import os
import random
import re
import shutil
import string
import subprocess
import time
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from mutagen import File as MFile
from mutagen.flac import FLAC, Picture
from mutagen.id3 import (
    APIC,
    COMM,
    ID3,
    ID3NoHeaderError,
    SYLT,
    TALB,
    TCON,
    TDRC,
    TIT2,
    TIT3,
    TLAN,
    TPE1,
    TPE2,
    TPOS,
    TRCK,
    TXXX,
    USLT,
)

MEDIA = Path(os.environ.get("MEDIA_DIR", "/media")).resolve()
EXTS = {
".mp3", ".flac", ".m4a", ".mp4", ".ogg", ".opus", ".wav", ".aac"}

app = FastAPI(title="Music Tag Lite")


def rel_ok(p: str) -> Path:
    path = (MEDIA / p).resolve()
    if not path.is_relative_to(MEDIA):
        raise HTTPException(400, "path escapes media root")
    return path


def first(v) -> str:
    if v is None:
        return ""
    try:
        return str(v[0])
    except (TypeError, IndexError, KeyError):
        return str(v)


def tags_mp3(path: Path) -> dict:
    try:
        t = ID3(path)
    except ID3NoHeaderError:
        return {}
    def g(k: str) -> str:
        v = t.get(k)
        return str(v) if v else ""
    uslts = t.getall("USLT")
    txxx = {}
    for fr in t.getall("TXXX"):
        try:
            txxx[str(fr.desc)] = str(fr.text[0]) if fr.text else ""
        except Exception:
            continue
    trck = g("TRCK").split("/")
    tpos = g("TPOS").split("/")
    comms = t.getall("COMM")
    comm_text = comms[0].text if comms else ""
    if isinstance(comm_text, list):
        comm_text = comm_text[0] if comm_text else ""
    lyrics, lrc = _split_lyric(str(uslts[0].text) if uslts else "")
    for sy in t.getall("SYLT"):
        try:
            pairs = [(str(x[0]), int(x[1])) for x in (sy.text or [])]
        except (TypeError, ValueError, IndexError):
            continue
        if pairs:
            lrc = _sylt_to_lrc(pairs)
            break
    return {
        "title": g("TIT2"), "artist": g("TPE1"), "album": g("TALB"),
        "albumartist": g("TPE2"), "date": g("TDRC"), "track": trck[0].strip(),
        "tracktotal": trck[1].strip() if len(trck) > 1 else "",
        "disc": tpos[0].strip(), "disctotal": tpos[1].strip() if len(tpos) > 1 else "",
        "genre": g("TCON"), "language": g("TLAN"),
        "albumtype": txxx.get("ALBUMTYPE", ""),
        "comment": str(comm_text),
        "description": g("TIT3"), "version": txxx.get("ALBUMVERSION", ""),
        "lyrics": lyrics, "lrc": lrc,
        "has_cover": bool(t.getall("APIC")),
    }


def tags_generic(path: Path) -> dict:
    f = MFile(path, easy=True)
    out = {"title": "", "artist": "", "album": "", "albumartist": "",
           "date": "", "track": "", "tracktotal": "", "disc": "", "disctotal": "",
           "genre": "", "language": "", "albumtype": "", "comment": "",
           "description": "", "version": "", "lyrics": "", "lrc": "",
           "has_cover": False}
    if f is None:
        return out
    if f.tags:
        for k, dst in [("title", "title"), ("artist", "artist"), ("album", "album"),
                       ("albumartist", "albumartist"), ("date", "date"),
                       ("tracknumber", "track"), ("genre", "genre")]:
            try:
                out[dst] = first(f.tags.get(k))
            except Exception:
                pass
        for k, dst in [("language", "language"), ("discnumber", "disc")]:
            try:
                v = first(f.tags.get(k))
                if v:
                    out[dst] = v
            except Exception:
                pass
    if path.suffix.lower() == ".flac":
        try:
            fl = FLAC(path)
            out["has_cover"] = bool(fl.pictures)
            out["lyrics"], out["lrc"] = _split_lyric(
                first(fl.tags.get("LYRICS")) if fl.tags else "")
            for k, dst in [("LANGUAGE", "language"), ("COMMENT", "comment"),
                             ("DESCRIPTION", "description"), ("ALBUMTYPE", "albumtype"),
                             ("ALBUMVERSION", "version"), ("DISCNUMBER", "disc"),
                             ("TRACKTOTAL", "tracktotal"), ("DISCTOTAL", "disctotal")]:
                out[dst] = first(fl.tags.get(k)) if fl.tags else ""
        except Exception:
            pass
    return out


def current_tags(path: Path) -> dict:
    if path.suffix.lower() == ".mp3":
        return tags_mp3(path)
    return tags_generic(path)


# ----- synced lyrics (LRC) helpers -----
_LRC_TIME_RE = re.compile(r"\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\]")
_LRC_HEAD_RE = re.compile(r"^\[(ti|ar|al|by|offset|au|la|re|ve):[^\]]*\]\s*", re.I | re.M)


def _looks_lrc(s):
    return bool(s and _LRC_TIME_RE.search(s))


def _strip_lrc(s):
    s = _LRC_TIME_RE.sub("", s or "")
    s = _LRC_HEAD_RE.sub("", s)
    return "\n".join(x.strip() for x in s.split("\n") if x.strip())


def _parse_lrc(s):
    pairs = []
    for line in (s or "").split("\n"):
        tags = _LRC_TIME_RE.findall(line)
        if not tags:
            continue
        text = _LRC_TIME_RE.sub("", line).strip()
        if not text:
            continue
        for tg in tags:
            m = re.match(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]", tg)
            if not m:
                continue
            mm, ss, frac = int(m.group(1)), int(m.group(2)), m.group(3) or "0"
            ms = (mm * 60 + ss) * 1000 + int(frac.ljust(3, "0")[:3])
            pairs.append((text, ms))
    pairs.sort(key=lambda x: x[1])
    return pairs


def _sylt_to_lrc(pairs):
    lines = []
    for text, ms in pairs:
        cs = ms // 10
        lines.append("[%02d:%02d.%02d]%s" % (cs // 6000, (cs // 100) % 60, cs % 100, text))
    return "\n".join(lines)


def _split_lyric(raw):
    raw = (raw or "").strip()
    if _looks_lrc(raw):
        return _strip_lrc(raw), raw
    return raw, ""


class SaveIn(BaseModel):
    expected_version: Optional[str] = None
    path: str
    title: str = ""
    artist: str = ""
    album: str = ""
    albumartist: str = ""
    date: str = ""
    track: str = ""
    genre: str = ""
    lyrics: str = ""
    lrc: str = ""
    filename: str = ""
    tracktotal: str = ""
    disc: str = ""
    disctotal: str = ""
    language: str = ""
    albumtype: str = ""
    comment: str = ""
    description: str = ""
    version: str = ""


class CoverIn(BaseModel):
    expected_version: Optional[str] = None
    path: str
    url: Optional[str] = None
    b64: Optional[str] = None
    mime: str = "image/jpeg"


@app.get("/api/list")
def list_dir(path: str = ""):
    d = rel_ok(path)
    if not d.is_dir():
        raise HTTPException(404, "not a directory")
    dirs, files = [], []
    try:
        entries = sorted(d.iterdir(), key=lambda x: x.name.lower())
    except PermissionError:
        raise HTTPException(403, "permission denied")
    for c in entries:
        if c.name.startswith("."):
            continue
        if c.is_dir():
            dirs.append(c.name)
        elif c.suffix.lower() in EXTS:
            files.append({"name": c.name, "path": str(c.relative_to(MEDIA)),
                          "size": c.stat().st_size, "mtime": c.stat().st_mtime})
    return {"path": path, "dirs": dirs, "files": files}


@app.get("/api/track")
def get_track(path: str):
    with _WRITER.lock:
        p = _WRITER.path(path)
        before = _WRITER.version(p)
        data = {"path": p.relative_to(MEDIA).as_posix(), "name": p.name, "size": p.stat().st_size}
        data.update(current_tags(p))
        data['tech'] = tech_info(p)
        info = _WRITER.info(p)
        if before != info['file_version']:
            raise HTTPException(409, '读取时文件发生变化，请重试')
        data.update(info)
        return data


@app.post("/api/save")
def save_track(b: SaveIn):
    _must_expected(b.expected_version)
    changes = {key: getattr(b, key) for key in _FIELDS if key in b.model_fields_set}
    return _WRITER.apply(b.path, changes=changes, filename=b.filename, expected=b.expected_version, kind='manual')


def tech_info(p: Path) -> dict:
    out = {"duration": "", "bitrate": "", "sample_rate": "", "bits": "", "channels": ""}
    try:
        a = MFile(p)
        info = getattr(a, "info", None)
        if info is None:
            return out
        out["duration"] = round(getattr(info, "length", 0) or 0, 2)
        br = getattr(info, "bitrate", 0) or 0
        out["bitrate"] = int(br // 1000) if br else ""
        sr = getattr(info, "sample_rate", 0) or 0
        out["sample_rate"] = int(sr) if sr else ""
        out["bits"] = getattr(info, "bits_per_sample", "") or ""
        out["channels"] = getattr(info, "channels", "") or ""
    except Exception:
        pass
    return out


def _lrc_path(p: Path) -> Path:
    return _WRITER.sidecar(p)


@app.get("/api/lrc_file")
def get_lrc_file(path: str):
    p = _WRITER.path(path)
    content = _WRITER.lrc_bytes(p)
    if content is None: raise HTTPException(404, 'no lrc file')
    try: return {'content': content.decode('utf-8-sig')}
    except UnicodeDecodeError: raise HTTPException(400, '歌词不是 UTF-8 编码；未修改文件')


class LrcIn(BaseModel):
    expected_version: Optional[str] = None
    path: str
    content: str = ""


@app.post("/api/lrc_file")
def save_lrc_file(b: LrcIn):
    _must_expected(b.expected_version)
    if not (b.content or '').strip(): raise HTTPException(400, 'empty content')
    return _WRITER.apply(b.path, expected=b.expected_version, external_lrc=b.content.encode('utf-8'), kind='lrc')


@app.get("/api/cover")
def get_cover(path: str):
    p = _WRITER.path(path)
    data, mime = _cover_bytes(p)
    if not data: raise HTTPException(404, 'no cover')
    return Response(data, media_type=mime or 'image/jpeg', headers={'X-Content-Type-Options': 'nosniff'})


@app.post("/api/cover")
def set_cover(b: CoverIn):
    _must_expected(b.expected_version)
    _WRITER.path(b.path)
    cover = _image_payload(url=b.url, b64=b.b64, mime=b.mime)
    return _WRITER.apply(b.path, expected=b.expected_version, cover=cover, kind='cover')


def _mb_queries(q: str):
    title, artist = q.strip(), ""
    if " - " in title:
        parts = [p.strip() for p in title.split(" - ")]
        title, artist = parts[0], (parts[1] if len(parts) > 1 else "")
    elif "-" in title:
        title, artist = (p.strip() for p in title.rsplit("-", 1))
    if artist:
        artist = re.split(r"[,、&/;，]", artist)[0].strip()
    title = title.strip().strip('"')
    queries = []
    if title and artist:
        queries.append(f'recording:"{title}" AND artist:"{artist}"')
    if title:
        queries.append(f'recording:"{title}"')
    queries.append(q)
    return queries


def _mb_fetch(query: str, limit: int):
    r = requests.get(
        "https://musicbrainz.org/ws/2/recording/",
        params={"query": query, "fmt": "json", "limit": limit},
        headers={"User-Agent": _mb_ua()},
        timeout=20,
    )
    r.raise_for_status()
    return r.json().get("recordings", [])


# ----- release 曲目表：补音轨号/总数/光盘号/专辑类型（MusicBrainz 独有） -----
MB_UA = "MusicInfo/1.0 (selfhosted)"
_MB_RELEASE_CACHE = {}


def _mb_release(release_id: str):
    """抓取单个 release 的曲目表（含 media / recordings），进程内缓存。"""
    if not release_id:
        return {}
    if release_id in _MB_RELEASE_CACHE:
        return _MB_RELEASE_CACHE[release_id]
    data = {}
    try:
        r = requests.get(
            f"https://musicbrainz.org/ws/2/release/{release_id}",
            params={"inc": "recordings+media+release-groups", "fmt": "json"},
            headers={"User-Agent": _mb_ua()},
            timeout=20,
        )
        if r.status_code == 200:
            data = r.json()
    except (requests.RequestException, ValueError):
        data = {}
    _MB_RELEASE_CACHE[release_id] = data
    return data


def _mb_track_info(data, rec_id: str, title: str):
    """在 release 曲目表里定位该 recording，返回音轨号/总数/光盘号/专辑类型。"""
    info = {}
    if not data:
        return info
    want = _normalize_text(title)
    for medium in (data.get("media") or []):
        if not isinstance(medium, dict):
            continue
        tracks = medium.get("tracks") or []
        for t in tracks:
            if not isinstance(t, dict):
                continue
            rec = t.get("recording") if isinstance(t.get("recording"), dict) else {}
            hit = bool(rec_id) and rec.get("id") == rec_id
            if not hit and want:
                hit = _normalize_text(t.get("title") or rec.get("title") or "") == want
            if not hit:
                continue
            pos = t.get("position") or t.get("number") or ""
            info["track"] = str(pos)
            info["tracktotal"] = str(len(tracks))
            mpos = medium.get("position")
            if mpos:
                info["disc"] = str(mpos)
                if len(data.get("media") or []) > 1:
                    info["disctotal"] = str(len(data["media"]))
            rg = data.get("release-group") if isinstance(data.get("release-group"), dict) else {}
            types = [rg.get("primary-type") or ""] + list(rg.get("secondary-types") or [])
            types = [x for x in types if x]
            if types:
                info["albumtype"] = " / ".join(types)
            return info
    return info


def _mb_attach_tracklists(out, max_releases: int = 2):
    """给 MB 搜索结果补曲目表信息；最多查询 max_releases 个专辑，间隔 1s 遵守限速。"""
    asked, fetched = set(), 0
    for d in out:
        rid = d.get("mb_release") or ""
        if not rid or d.get("track"):
            continue
        if rid not in asked:
            if fetched >= max_releases:
                continue
            if fetched:
                time.sleep(1.0)
            asked.add(rid)
            fetched += 1
            _mb_release(rid)
        info = _mb_track_info(_MB_RELEASE_CACHE.get(rid) or {},
                              d.get("id") or "", d.get("title") or "")
        for k, v in info.items():
            if v and not d.get(k):
                d[k] = v


def mb_search_raw(q: str, limit: int = 8):
    out = []
    for query in _mb_queries(q):
        try:
            recs = _mb_fetch(query, limit)
        except Exception:
            continue
        if not recs:
            continue
        for rec in recs:
            names = ", ".join(a.get("name", "") for a in (rec.get("artist-credit") or [])
                               if isinstance(a, dict))
            rels = rec.get("releases") or []
            d = _norm("mb", rec.get("id"), rec.get("title"), names,
                      rels[0].get("title", "") if rels else "",
                      (rels[0].get("date", "") if rels else "")[:10])
            d["mb_artist"], d["mb_release"] = _mb_cover_ids(rec)
            d["score"] = rec.get("score", 0)
            out.append(d)
        _mb_attach_tracklists(out)
        _backfill_covers(out)
        return out
    return out


# ----- cover backfill for coverless results (Fanart.tv + iTunes) -----
FANART_KEY = os.environ.get("FANART_API_KEY", "")
_FANART_ALBUMS_CACHE = {}
_ITUNES_COVER_CACHE = {}


def _mb_cover_ids(rec):
    artist_mbid, rel_id = "", ""
    ac = rec.get("artist-credit") or []
    if ac and isinstance(ac[0], dict):
        a = ac[0].get("artist") or {}
        if isinstance(a, dict):
            artist_mbid = a.get("id") or ""
    rels = [r for r in (rec.get("releases") or []) if isinstance(r, dict)]
    if rels:
        pick = next((r for r in rels if r.get("status") == "Official"), rels[0])
        rel_id = pick.get("id") or ""
    return artist_mbid, rel_id


def _fanart_albums(artist_mbid):
    if artist_mbid in _FANART_ALBUMS_CACHE:
        return _FANART_ALBUMS_CACHE[artist_mbid]
    got = {}
    if FANART_KEY and artist_mbid:
        try:
            r = requests.get("https://webservice.fanart.tv/v3/music/%s" % artist_mbid,
                             params={"api_key": FANART_KEY}, timeout=12)
            if r.status_code != 200:
                return got
            albums = r.json().get("albums") or {}
        except (requests.RequestException, ValueError):
            return got
        for rel_id, info in albums.items():
            if not isinstance(info, dict):
                continue
            covers = info.get("albumcover") or []
            if covers and isinstance(covers[0], dict) and covers[0].get("url"):
                got[rel_id] = covers[0]["url"]
        _FANART_ALBUMS_CACHE[artist_mbid] = got
    return got


def _cover_match(a, b):
    def _clean(s):
        s = (s or "").lower()
        s = re.sub(r"\(.*?\)", "", s)
        s = re.sub(r"\[.*?\]", "", s)
        return re.sub(r"[^a-z0-9一-\u9fff]+", "", s)
    a, b = _clean(a), _clean(b)
    if not a or not b:
        return False
    if a == b:
        return True
    if (a in b or b in a) and min(len(a), len(b)) / max(len(a), len(b)) > 0.5:
        return True
    return False


def _itunes_cover(title, artist):
    key = ((title or "").strip().lower(), (artist or "").strip().lower())
    if key in _ITUNES_COVER_CACHE:
        return _ITUNES_COVER_CACHE[key]
    url = ""
    try:
        for s in itunes_search_raw("%s %s" % (title, artist), 4):
            if not _cover_match(title, s.get("title")):
                continue
            if artist and not _cover_match(artist, s.get("artist")):
                continue
            url = s.get("cover") or ""
            if url:
                break
    except Exception:
        url = ""
    if url:
        _ITUNES_COVER_CACHE[key] = url
    return url


def _backfill_one(d):
    try:
        am, rm = d.get("mb_artist") or "", d.get("mb_release") or ""
        if am and rm:
            d["cover"] = _fanart_albums(am).get(rm) or ""
        if not d.get("cover"):
            d["cover"] = _itunes_cover(d.get("title") or "", d.get("artist") or "")
    except Exception:
        pass
    return d


def _backfill_covers(out):
    jobs = [d for d in out if not d.get("cover")]
    if not jobs:
        return
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(_backfill_one, jobs))
@app.get("/api/mb_search")
def mb_search(q: str):
    try:
        out = _timed("mb", mb_search_raw, q)
    except Exception as e:
        raise HTTPException(502, f"musicbrainz failed: {e}")
    return {"results": out}


@app.get("/api/ping")
def ping():
    return {"ok": True, "media": str(MEDIA)}


@app.get("/api/sources")
def list_sources():
    return {
        "sources": ["smart", "netease", "migu", "qq", "kugou", "kuwo", "itunes", "acoustid", "mb", "lrclib"],
        "fpcalc": bool(shutil.which("fpcalc")),
        "fanart_key": bool(FANART_KEY),
    }


# ---------------- metadata providers (uniform result shape) ----------------
# Each search fn returns a list of:
#   {source, id, title, artist, album, date, track, cover}
# Each lyric fn returns an LRC/plain-text string ("" when unavailable).

UA_WIN = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def _norm(source, sid, title="", artist="", album="", date="", track="", cover="", genre="",
          tracktotal="", disc="", disctotal="", language="", albumtype="", comment="",
          description="", version=""):
    return {"source": source, "id": sid, "title": title or "", "artist": artist or "",
            "album": album or "", "date": date or "", "track": track or "",
            "tracktotal": tracktotal or "", "disc": disc or "", "disctotal": disctotal or "",
            "language": language or "", "albumtype": albumtype or "",
            "comment": comment or "", "description": description or "",
            "version": version or "", "cover": cover or "", "genre": genre or ""}


def _clamp_limit(limit: int, lo: int = 1, hi: int = 30) -> int:
    return max(lo, min(hi, int(limit or lo)))


# ----- NetEase -----
NCM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "http://music.163.com",
    "Host": "music.163.com",
}


def ncm_search_raw(q: str, limit: int = 10):
    limit = _clamp_limit(limit, 1, 50)
    variants = [q]
    if "_" in q:
        variants.append(q.replace("_", ":"))
    out, seen = [], set()
    for kw in variants:
        try:
            r = requests.get(
                "https://music.163.com/api/cloudsearch/pc",
                headers=NCM_HEADERS,
                params={"type": "1", "s": kw, "limit": str(limit), "offset": "0"},
                timeout=10,
                verify=False,
            )
            r.raise_for_status()
            songs = r.json().get("result", {}).get("songs", []) or []
        except Exception:
            continue
        for li in songs:
            sid = li.get("id")
            if sid in seen:
                continue
            seen.add(sid)
            artists = ", ".join(a.get("name", "") for a in (li.get("ar") or []))
            al = li.get("al") or {}
            ts = li.get("publishTime") or 0
            year = ""
            if ts:
                try:
                    year = time.strftime("%Y", time.localtime(ts / 1000))
                except (ValueError, OSError):
                    year = ""
            out.append(_norm("netease", sid, li.get("name", ""), artists,
                             al.get("name", ""), year, li.get("no", "") or "",
                             al.get("picUrl", "") or ""))
        if len(out) >= limit:
            break
    return out[:limit]


def ncm_lyric_raw(song_id: str):
    r = requests.get(
        "http://music.163.com/api/song/lyric",
        headers=NCM_HEADERS,
        params={"id": song_id, "lv": "1", "kv": "1", "tv": "-1"},
        timeout=10,
        verify=False,
    )
    r.raise_for_status()
    j = r.json()
    return ((j.get("lrc") or {}).get("lyric", "") or "",
            (j.get("tlyric") or {}).get("lyric", "") or "")


@app.get("/api/ncm_search")
def ncm_search(q: str, limit: int = 10):
    out = _timed("netease", ncm_search_raw, q, limit)
    if not out:
        raise HTTPException(502, "netease unreachable or no results")
    return {"results": out}


@app.get("/api/ncm_lyric")
def ncm_lyric(id: str):
    try:
        lyric, tlyric = ncm_lyric_raw(id)
    except Exception as e:
        raise HTTPException(502, f"netease lyric failed: {e}")
    return {"lyric": lyric, "tlyric": tlyric}


# ----- Migu -----
MIGU_HEADERS = {"User-Agent": UA_WIN, "Referer": "https://m.music.migu.cn/"}


def migu_search_raw(q: str, limit: int = 10):
    r = requests.get("https://m.music.migu.cn/migu/remoting/scr_search_tag",
                     headers=MIGU_HEADERS,
                     params={"rows": _clamp_limit(limit), "type": 2,
                             "keyword": q, "pgc": 1},
                     timeout=12)
    r.raise_for_status()
    if '"musics"' not in r.text:
        raise RuntimeError("migu search api retired (v5 migration)")
    out = []
    for s in (r.json().get("musics") or []):
        out.append(_norm("migu", s.get("copyrightId"), s.get("songName"),
                         s.get("singerName"), s.get("albumName"), "", "",
                         s.get("cover")))
    return out


def migu_lyric_raw(song_id: str) -> str:
    r = requests.get("https://music.migu.cn/v3/api/music/audioPlayer/getLyric",
                     headers=MIGU_HEADERS, params={"copyrightId": song_id},
                     timeout=12)
    r.raise_for_status()
    if '"lyric"' not in r.text and '"data"' not in r.text:
        raise RuntimeError("migu lyric api retired (v5 migration)")
    j = r.json()
    if isinstance(j.get("lyric"), str):
        return j["lyric"]
    return ((j.get("data") or {}).get("lyric") or "")


@app.get("/api/migu_search")
def migu_search(q: str, limit: int = 10):
    try:
        return {"results": _timed("migu", migu_search_raw, q, limit)}
    except Exception as e:
        raise HTTPException(502, f"migu unreachable: {e}")


@app.get("/api/migu_lyric")
def migu_lyric(id: str):
    try:
        return {"lyric": migu_lyric_raw(id)}
    except Exception as e:
        raise HTTPException(502, f"migu lyric failed: {e}")


# ----- QQ Music -----
QQ_HEADERS = {"User-Agent": "QQMusic/73222 CFNetwork/1406.0.2 Darwin/22.4.0",
              "Referer": "https://y.qq.com/portal/profile.html",
              "Content-Type": "application/json"}


def qq_search_raw(q: str, limit: int = 10):
    payload = {
        "comm": {"ct": 19, "cv": 1845},
        "music.search.SearchCgiService.DoSearchForQQMusicDesktop": {
            "module": "music.search.SearchCgiService",
            "method": "DoSearchForQQMusicDesktop",
            "param": {"query": q, "num_per_page": _clamp_limit(limit),
                      "page_num": 1},
        },
    }
    r = requests.post("https://u.y.qq.com/cgi-bin/musicu.fcg", headers=QQ_HEADERS,
                      json=payload, timeout=12)
    r.raise_for_status()
    body = (r.json()["music.search.SearchCgiService.DoSearchForQQMusicDesktop"]
            ["data"]["body"]["song"]["list"])
    out = []
    for s in body:
        singers = ",".join(x.get("name", "") for x in (s.get("singer") or []))
        alb = s.get("album") or {}
        amid = alb.get("mid") or ""
        out.append(_norm("qq", s.get("mid"), s.get("title"), singers,
                         alb.get("title"), s.get("time_public"), "",
                         ("https://y.qq.com/music/photo_new/T002R300x300M000"
                          f"{amid}.jpg") if amid else ""))
    return out


def qq_lyric_raw(songmid: str) -> str:
    r = requests.get("https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg",
                     params={"songmid": songmid, "g_tk": 5381, "format": "json",
                             "inCharset": "utf-8", "outCharset": "utf-8",
                             "notice": 0, "platform": "h5", "needNewCode": 1},
                     headers={"Referer": "https://y.qq.com/", "User-Agent": UA_WIN},
                     timeout=12)
    r.raise_for_status()
    try:
        return base64.b64decode(r.json().get("lyric", "") or "").decode("utf-8", "ignore")
    except Exception:
        return ""


@app.get("/api/qq_search")
def qq_search(q: str, limit: int = 10):
    try:
        return {"results": _timed("qq", qq_search_raw, q, limit)}
    except Exception as e:
        raise HTTPException(502, f"qqmusic unreachable: {e}")


@app.get("/api/qq_lyric")
def qq_lyric(id: str):
    try:
        return {"lyric": qq_lyric_raw(id)}
    except Exception as e:
        raise HTTPException(502, f"qqmusic lyric failed: {e}")


# ----- Kugou -----
KUGOU_KEY = "NVPh5oo715z5DIWAeQlhMDsWXXQV4hwt"


def kugou_search_raw(q: str, limit: int = 10):
    ms = str(int(time.time() * 1000))
    size = _clamp_limit(limit)
    raw = (f"{KUGOU_KEY}bitrate=0clienttime={ms}clientver=2000dfid=-inputtype=0"
           f"iscorrection=1isfuzzy=0keyword={q}mid={ms}page=1pagesize={size}"
           f"platform=WebFilterprivilege_filter=0srcappid=2919tag=emuserid=-1"
           f"uuid={ms}{KUGOU_KEY}")
    sig = hashlib.md5(raw.encode("utf-8")).hexdigest().upper()
    r = requests.get("https://complexsearch.kugou.com/v2/search/song", params={
        "keyword": q, "page": 1, "pagesize": size, "bitrate": 0, "isfuzzy": 0,
        "tag": "em", "inputtype": 0, "platform": "WebFilter", "userid": "-1",
        "clientver": 2000, "iscorrection": 1, "privilege_filter": 0,
        "srcappid": 2919, "clienttime": ms, "mid": ms, "uuid": ms, "dfid": "-",
        "signature": sig}, headers={"User-Agent": UA_WIN}, timeout=12)
    r.raise_for_status()
    out = []
    for s in ((r.json().get("data") or {}).get("lists") or []):
        clean = lambda v: (v or "").replace("<em>", "").replace("</em>", "")
        out.append(_norm("kugou", s.get("FileHash"), clean(s.get("SongName")),
                         ",".join(clean(s.get("SingerName")).split("\u3001")),
                         clean(s.get("AlbumName")), s.get("PublishTime"), "",
                         (s.get("Image") or "").replace("{size}", "150")))
    return out


def kugou_lyric_raw(filehash: str) -> str:
    r = requests.get("http://m.kugou.com/app/i/krc.php",
                     params={"cmd": 100, "timelength": 999999, "hash": filehash},
                     headers={"User-Agent": UA_WIN}, timeout=12)
    r.raise_for_status()
    data = r.content or b""
    if data[:4] == b"krc1":
        try:
            return zlib.decompress(data[4:]).decode("utf-8", "ignore")
        except Exception:
            pass
    try:
        return data.decode("utf-8", "ignore")
    except Exception:
        return ""


@app.get("/api/kugou_search")
def kugou_search(q: str, limit: int = 10):
    try:
        return {"results": _timed("kugou", kugou_search_raw, q, limit)}
    except Exception as e:
        raise HTTPException(502, f"kugou unreachable: {e}")


@app.get("/api/kugou_lyric")
def kugou_lyric(id: str):
    try:
        return {"lyric": kugou_lyric_raw(id)}
    except Exception as e:
        raise HTTPException(502, f"kugou lyric failed: {e}")


# ----- Kuwo -----
def _kuwo_headers():
    token = "".join(random.choices(string.ascii_letters + string.digits, k=32))
    cross = hashlib.md5(hashlib.sha1(token.encode()).hexdigest().encode()).hexdigest()
    return {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/114.0.0.0 Safari/537.36"),
            "Referer": "http://www.kuwo.cn/", "Cross": cross,
            "Cookie": f"Hm_token={token}"}


def kuwo_search_raw(q: str, limit: int = 10):
    r = requests.get("http://www.kuwo.cn/api/www/search/searchMusicBykeyWord",
                     params={"key": q, "pn": 1, "rn": _clamp_limit(limit),
                             "httpsStatus": 1},
                     headers=_kuwo_headers(), timeout=12)
    r.raise_for_status()
    out = []
    for s in ((r.json().get("data") or {}).get("list") or []):
        out.append(_norm("kuwo", s.get("rid"), s.get("name"), s.get("artist"),
                         s.get("album"), "", "", s.get("albumpic")))
    return out


def kuwo_lyric_raw(rid: str) -> str:
    r = requests.get(f"http://kuwo.cn/newh5/singles/songinfoandlrc?musicId={rid}",
                     params={"mid": rid, "type": "music", "httpsStatus": 1,
                             "plat": "web_www"},
                     headers=_kuwo_headers(), timeout=12)
    r.raise_for_status()
    lines = []
    for ln in ((r.json().get("data") or {}).get("lrclist") or []):
        try:
            sec = float(ln.get("time", 0))
        except (TypeError, ValueError):
            continue
        m, s = divmod(int(sec), 60)
        lines.append(f"[{m:02d}:{s:02d}.{int(sec % 1 * 100):02d}]"
                     f"{ln.get('lineLyric', '')}")
    return "\n".join(lines)


@app.get("/api/kuwo_search")
def kuwo_search(q: str, limit: int = 10):
    try:
        return {"results": _timed("kuwo", kuwo_search_raw, q, limit)}
    except Exception as e:
        raise HTTPException(502, f"kuwo unreachable: {e}")


@app.get("/api/kuwo_lyric")
def kuwo_lyric(id: str):
    try:
        return {"lyric": kuwo_lyric_raw(id)}
    except Exception as e:
        raise HTTPException(502, f"kuwo lyric failed: {e}")


# ----- iTunes -----
def itunes_search_raw(q: str, limit: int = 10):
    r = requests.get("https://itunes.apple.com/search",
                     params={"term": q, "media": "music", "entity": "song",
                             "limit": _clamp_limit(limit, 1, 50)},
                     headers={"User-Agent": UA_WIN}, timeout=12)
    r.raise_for_status()
    out = []
    for s in (r.json().get("results") or []):
        art = s.get("artworkUrl100") or ""
        d = _norm("itunes", s.get("trackId"), s.get("trackName"),
                  s.get("artistName"), s.get("collectionName"),
                  (s.get("releaseDate") or "")[:10],
                  str(s.get("trackNumber") or ""),
                  art.replace("100x100", "600x600") if art else "",
                  s.get("primaryGenreName"))
        d.update(tracktotal=str(s.get("trackCount") or ""),
                 disc=str(s.get("discNumber") or ""),
                 disctotal=str(s.get("discCount") or ""),
                 albumtype=s.get("collectionType") or "")
        out.append(d)
    return out


@app.get("/api/itunes_search")
def itunes_search(q: str, limit: int = 10):
    try:
        return {"results": _timed("itunes", itunes_search_raw, q, limit)}
    except Exception as e:
        raise HTTPException(502, f"itunes unreachable: {e}")


# ----- Deezer -----
def deezer_search_raw(q: str, limit: int = 10):
    r = requests.get("https://api.deezer.com/search",
                     params={"q": q, "limit": _clamp_limit(limit, 1, 50)},
                     headers={"User-Agent": UA_WIN}, timeout=12)
    r.raise_for_status()
    out = []
    for s in (r.json().get("data") or []):
        artist = s.get("artist") or {}
        album = s.get("album") or {}
        out.append(_norm("deezer", s.get("id"), s.get("title"),
                         artist.get("name"), album.get("title"), "", "",
                         album.get("cover_big") or album.get("cover_medium")))
    return out


@app.get("/api/deezer_search")
def deezer_search(q: str, limit: int = 10):
    try:
        return {"results": _timed("deezer", deezer_search_raw, q, limit)}
    except Exception as e:
        raise HTTPException(502, f"deezer unreachable: {e}")


# ----- unified lyric dispatcher -----
@app.get("/api/lyric")
def uni_lyric(source: str, id: str):
    try:
        if source == "netease":
            lyric, tlyric = ncm_lyric_raw(id)
            return {"lyric": lyric, "tlyric": tlyric}
        if source == "migu":
            return {"lyric": migu_lyric_raw(id), "tlyric": ""}
        if source == "qq":
            return {"lyric": qq_lyric_raw(id), "tlyric": ""}
        if source == "kugou":
            return {"lyric": kugou_lyric_raw(id), "tlyric": ""}
        if source == "kuwo":
            return {"lyric": kuwo_lyric_raw(id), "tlyric": ""}
    except Exception as e:
        raise HTTPException(502, f"lyric failed: {e}")
    return {"lyric": "", "tlyric": ""}


# ----- smart fusion search -----
def _mscore(a, b) -> int:
    a = (a or "").lower().replace(" ", "")
    b = (b or "").lower().replace(" ", "")
    if not a or not b:
        return 0
    if a == b:
        return 2
    if a in b or b in a:
        return 1
    return 0


_SMART_JOBS = [("migu", migu_search_raw), ("qq", qq_search_raw),
               ("netease", ncm_search_raw), ("kugou", kugou_search_raw),
               ("kuwo", kuwo_search_raw), ("itunes", itunes_search_raw),
               ("mb", mb_search_raw)]


def smart_search_raw(q: str, artist: str = "", album: str = "", limit: int = 15):
    per_source, out = {}, []
    with ThreadPoolExecutor(max_workers=8) as pool:
        active = [(name, fn) for name, fn in _smart_jobs() if _PLUGINS.is_enabled(name)]
        futs = {pool.submit(_timed, name, fn, q,
                            _PLUGINS.param(name, "limit", 8) or 8): name for name, fn in active}
        try:
            done = as_completed(futs, timeout=25)
            for fut in done:
                try:
                    per_source[futs[fut]] = fut.result() or []
                except Exception:
                    per_source[futs[fut]] = []
        except TimeoutError:
            for fut, name in futs.items():
                if name not in per_source:
                    per_source[name] = []
    for _name, songs in per_source.items():
        for s in songs:
            ts = _mscore(q, s.get("title"))
            if ts == 0:
                continue
            ar = _mscore(artist or q, s.get("artist"))
            if artist and ar == 0:
                ar = -2
            al = _mscore(album or q, s.get("album"))
            s["score"] = ts + ar + al
            out.append(s)
    out.sort(key=lambda x: x.get("score", 0), reverse=True)
    return out[:max(1, min(30, int(limit or 15)))]


@app.get("/api/smart_search")
def smart_search(q: str, path: Optional[str] = None, limit: int = 15):
    artist = album = ""
    if path:
        try:
            tags = current_tags(rel_ok(path))
            artist, album = tags.get("artist", ""), tags.get("album", "")
        except Exception:
            pass
    try:
        return {"results": smart_search_raw(q, artist, album, limit)}
    except Exception as e:
        raise HTTPException(502, f"smart search failed: {e}")


# ----- LRCLIB (lyrics fallback, no key) -----
LRCLIB_HEADERS = {"User-Agent": "MusicTagLite/1.0"}


def lrclib_lyric_raw(artist: str, title: str, album: str = ""):
    """Return (lyric_text, is_synced). Empty string when unavailable."""
    if not artist or not title:
        return "", False
    artists = [artist]
    first = re.split(r"[,、&/;，]", artist)[0].strip()
    if first and first.lower() != artist.strip().lower():
        artists.append(first)
    for art in artists:
        params = {"artist_name": art, "track_name": title}
        if album:
            params["album_name"] = album
        try:
            r = requests.get("https://lrclib.net/api/get", params=params,
                             headers=LRCLIB_HEADERS, timeout=12)
        except requests.RequestException:
            continue
        if r.status_code != 200:
            continue
        try:
            d = r.json()
        except ValueError:
            continue
        if d.get("instrumental"):
            return "", False
        synced = d.get("syncedLyrics") or ""
        plain = d.get("plainLyrics") or ""
        if synced or plain:
            return (synced or plain), bool(synced)
    try:
        r = requests.get("https://lrclib.net/api/search",
                         params={"q": f"{title} {artists[0]}"},
                         headers=LRCLIB_HEADERS, timeout=12)
        recs = r.json() if r.status_code == 200 else []
    except (requests.RequestException, ValueError):
        return "", False
    best, best_key = None, None
    for rec in recs or []:
        if not isinstance(rec, dict) or rec.get("instrumental"):
            continue
        if _mscore(title, rec.get("trackName")) < 1:
            continue
        if _mscore(artist, rec.get("artistName")) < 1:
            continue
        synced = rec.get("syncedLyrics") or ""
        plain = rec.get("plainLyrics") or ""
        if not (synced or plain):
            continue
        key = ((1 if synced else 0),
               _mscore(title, rec.get("trackName")) + _mscore(artist, rec.get("artistName")))
        if best_key is None or key > best_key:
            best, best_key = ((synced or plain), bool(synced)), key
    return best if best else ("", False)


@app.get("/api/lrclib_lyric")
def lrclib_lyric(artist: str, title: str, album: str = ""):
    try:
        lyric, synced = _timed("lrclib", lrclib_lyric_raw, artist, title, album)
    except Exception as e:
        raise HTTPException(502, f"lrclib failed: {e}")
    return {"lyric": lyric, "synced": synced}


# ----- AcoustID audio fingerprint -----
ACOUSTID_KEY = os.environ.get("ACOUSTID_CLIENT_KEY", "")


def fp_lookup_raw(path: Path):
    if not shutil.which("fpcalc"):
        raise RuntimeError("fpcalc not installed in container")
    try:
        proc = subprocess.run(["fpcalc", "-json", str(path)], capture_output=True,
                              text=True, timeout=180)
    except subprocess.TimeoutExpired:
        raise RuntimeError("fpcalc timed out")
    if proc.returncode != 0:
        raise RuntimeError(f"fpcalc failed: {(proc.stderr or '')[:300]}")
    try:
        fp = json.loads(proc.stdout or "{}")
    except Exception:
        raise RuntimeError("fpcalc returned invalid json")
    dur, fingerprint = fp.get("duration"), fp.get("fingerprint")
    if not dur or not fingerprint:
        raise RuntimeError("fpcalc produced no fingerprint")
    r = requests.post("https://api.acoustid.org/v2/lookup",
                      data={"format": "json", "client": ACOUSTID_KEY,
                            "meta": "recordings releasegroups",
                            "duration": int(dur), "fingerprint": fingerprint},
                      timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"acoustid http {r.status_code}: {r.text[:200]}")
    out = []
    for res in (r.json().get("results") or [])[:3]:
        for rec in (res.get("recordings") or [])[:2]:
            arts = ",".join(a.get("name", "") for a in (rec.get("artists") or []))
            rgs = rec.get("releasegroups") or []
            out.append(_norm("acoustid", rec.get("id"), rec.get("title"), arts,
                             rgs[0].get("title") if rgs else "",
                             rgs[0].get("date") if rgs else "", "", ""))
    return out


@app.get("/api/fp_search")
def fp_search(path: str):
    p = rel_ok(path)
    if not p.is_file():
        raise HTTPException(404, "not found")
    try:
        return {"results": _timed("acoustid", fp_lookup_raw, p)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"fingerprint lookup failed: {e}")



# ---------- durable workflow: independent implementation, SQLite/WAL ----------
import unicodedata as _unicodedata
from difflib import SequenceMatcher as _SequenceMatcher
from typing import Literal as _Literal
from pydantic import Field as _Field, ConfigDict as _ConfigDict
from fastapi import Request as _Request
from mtl_store import Store as _Store
from mtl_plugins import PluginRegistry as _PluginRegistry
from mtl_plugins import run_custom_search as _custom_search
from mtl_plugins import test_search as _custom_test
from mtl_safe import Writer as _Writer, FIELDS as _FIELDS, image_payload as _image_payload, cover_bytes as _cover_bytes, root_path as _root_path
from mtl_jobs import Engine as _Engine


def _must_expected(value):
    if not value or not re.fullmatch(r'[0-9a-f]{64}', value):
        raise HTTPException(400, '请刷新页面并提供当前文件版本；未经版本核验不允许写入')

_APP_VERSION = '2026.10-workflow.4'
_legacy_current_tags = current_tags


def current_tags(path: Path) -> dict:
    tags = {key:'' for key in _FIELDS}
    tags.update(_legacy_current_tags(path))
    tags['has_cover'] = bool(tags.get('has_cover',False))
    ext = path.suffix.lower()
    if ext in ('.flac','.ogg','.opus'):
        f = MFile(path)
        native = f.tags or {}
        mapping = {'language':'language','albumtype':'albumtype','comment':'comment','description':'description','albumversion':'version','discnumber':'disc','tracktotal':'tracktotal','disctotal':'disctotal'}
        for key,destination in mapping.items():
            tags[destination] = first(native.get(key))
        raw = first(native.get('lyrics'))
        tags['lyrics'],tags['lrc'] = _split_lyric(raw)
        if native.get('unsyncedlyrics'):
            tags['lyrics'] = first(native.get('unsyncedlyrics'))
    if ext in ('.m4a','.mp4'):
        for key,total in [('track','tracktotal'),('disc','disctotal')]:
            pieces = str(tags.get(key) or '').split('/',1)
            tags[key] = pieces[0]
            if len(pieces)>1: tags[total] = pieces[1]
    return tags


def _write_changed_tags(path, changes, original):
    values = {**original, **changes}
    ext = path.suffix.lower()
    if ext == '.mp3':
        try: t = ID3(path)
        except ID3NoHeaderError: t = ID3()
        mapping = {'title':(TIT2,'TIT2'),'artist':(TPE1,'TPE1'),'album':(TALB,'TALB'),'albumartist':(TPE2,'TPE2'),'date':(TDRC,'TDRC'),'genre':(TCON,'TCON'),'description':(TIT3,'TIT3'),'language':(TLAN,'TLAN')}
        for key,(constructor,frame_name) in mapping.items():
            if key not in changes: continue
            value = values.get(key,'')
            if value:
                text = [part.strip() for part in value.split(';') if part.strip()] if key in ('artist','albumartist','genre') else value
                t.setall(frame_name,[constructor(encoding=3,text=text)])
            else: t.delall(frame_name)
        for left,right,constructor,name in [('track','tracktotal',TRCK,'TRCK'),('disc','disctotal',TPOS,'TPOS')]:
            if left in changes or right in changes:
                value = str(values.get(left) or '')
                total = str(values.get(right) or '')
                if value and total: value += '/' + total
                if value: t.setall(name,[constructor(encoding=3,text=value)])
                else: t.delall(name)
        for key,descriptor in [('albumtype','ALBUMTYPE'),('version','ALBUMVERSION')]:
            if key not in changes: continue
            for frame in list(t.getall('TXXX')):
                if frame.desc == descriptor: del t[frame.HashKey]
            if values.get(key): t.add(TXXX(encoding=3,desc=descriptor,text=values[key]))
        if 'comment' in changes:
            frames=t.getall('COMM'); frame=frames[0] if frames else None
            desc,lang=(frame.desc,frame.lang) if frame else ('','eng')
            if frame: del t[frame.HashKey]
            if values.get('comment'): t.add(COMM(encoding=3,lang=lang,desc=desc,text=values['comment']))
        if 'lyrics' in changes:
            frames=t.getall('USLT'); frame=frames[0] if frames else None
            desc,lang=(frame.desc,frame.lang) if frame else ('','eng')
            if frame: del t[frame.HashKey]
            if values.get('lyrics'): t.add(USLT(encoding=3,lang=lang,desc=desc,text=values['lyrics']))
        if 'lrc' in changes:
            frames=t.getall('SYLT'); frame=frames[0] if frames else None
            desc,lang=(frame.desc,frame.lang) if frame else ('','eng')
            pairs=_parse_lrc(values.get('lrc',''))
            if values.get('lrc') and not pairs: raise HTTPException(400,'同步歌词需要有效时间轴；原文件未修改')
            if frame: del t[frame.HashKey]
            if pairs: t.add(SYLT(encoding=3,lang=lang,desc=desc,format=2,type=1,text=pairs))
        t.save(path)
    elif ext in ('.flac','.ogg','.opus'):
        f=MFile(path)
        if f.tags is None: f.add_tags()
        mapping={'title':'title','artist':'artist','album':'album','albumartist':'albumartist','date':'date','track':'tracknumber','genre':'genre','language':'language','albumtype':'albumtype','comment':'comment','description':'description','version':'albumversion','disc':'discnumber','tracktotal':'tracktotal','disctotal':'disctotal'}
        for key,destination in mapping.items():
            if key not in changes: continue
            value=values.get(key,'')
            if value: f[destination]=[part.strip() for part in value.split(';') if part.strip()] if key in ('artist','albumartist','genre') else value
            elif destination in f: del f[destination]
        if 'lyrics' in changes or 'lrc' in changes:
            plain, synced=values.get('lyrics',''),values.get('lrc','')
            if synced and not _looks_lrc(synced): raise HTTPException(400,'同步歌词需要有效时间轴；原文件未修改')
            combined=synced or plain
            if combined: f['lyrics']=combined
            elif 'lyrics' in f: del f['lyrics']
            if synced and plain: f['unsyncedlyrics']=plain
            elif 'unsyncedlyrics' in f: del f['unsyncedlyrics']
        f.save()
    elif ext in ('.m4a','.mp4'):
        f=MFile(path,easy=True)
        if f.tags is None: f.add_tags()
        mapping={'title':'title','artist':'artist','album':'album','albumartist':'albumartist','date':'date','track':'tracknumber','genre':'genre','disc':'discnumber'}
        for key,value in changes.items():
            destination=mapping[key]
            if key in ('track','disc'):
                total=values.get('tracktotal' if key=='track' else 'disctotal')
                if value and total: value += '/'+str(total)
            if value: f.tags[destination]=[value]
            elif destination in f.tags: del f.tags[destination]
        f.save()
    else: raise HTTPException(400,'此格式本轮仅支持读取和安全重命名')


_STORE = _Store(os.environ.get('DATA_DIR','/data'))
_PLUGINS = _PluginRegistry(os.environ.get('DATA_DIR','/data'))


def _timed(pid, fn, *args, **kwargs):
    """调用某个数据源并记录统计；已停用的插件直接拒绝。"""
    if not _PLUGINS.is_enabled(pid):
        raise RuntimeError(f"plugin disabled: {pid}")
    t0 = time.time()
    try:
        out = fn(*args, **kwargs)
    except Exception:
        _PLUGINS.record(pid, False, int((time.time() - t0) * 1000))
        raise
    _PLUGINS.record(pid, True, int((time.time() - t0) * 1000))
    return out


def _mb_ua():
    """MusicBrainz 的 User-Agent（可在插件库中编辑）。"""
    return _PLUGINS.param("mb", "user_agent", MB_UA) or MB_UA


def _smart_jobs():
    """内置源 + 自定义源；每次调用重建，新增 / 删除插件后立即生效。"""
    jobs = list(_SMART_JOBS)
    for defn in _PLUGINS.custom_definitions():
        jobs.append((defn["id"],
                     (lambda d: (lambda q, limit=8: _custom_search(d, q, limit)))(defn)))
    return jobs
_WRITER = _Writer(MEDIA,_STORE,current_tags,_write_changed_tags)


def _normalize_text(value):
    value=_unicodedata.normalize('NFKC',str(value or '')).lower()
    return ''.join(c for c in value if not c.isspace() and _unicodedata.category(c)[0] not in ('P','S'))


def _ratio(a,b):
    a,b=_normalize_text(a),_normalize_text(b)
    return _SequenceMatcher(None,a,b,autojunk=False).ratio() if a and b else 0.0


def _name_parts(name):
    parts=Path(name).stem.replace('_',':').split(' - ')
    return parts[0],parts[1] if len(parts)>1 else ''


def _preview_one(item,options):
    track=get_track(item['path'])
    nt,na=_name_parts(track['name'])
    title,artist=track.get('title') or nt,track.get('artist') or na
    conflict=bool(na and track.get('artist') and _ratio(na,track['artist'])<.45)
    use_name=options.get('query_mode')=='filename' or (options.get('query_mode','auto')=='auto' and conflict)
    if use_name: title,artist=nt,na
    query=title+('-'+artist if artist else '')
    source=options.get('source','smart')
    funcs={'smart':smart_search_raw,'mb':mb_search_raw,'netease':ncm_search_raw,'migu':migu_search_raw,'qq':qq_search_raw,'kugou':kugou_search_raw,'kuwo':kuwo_search_raw,'itunes':itunes_search_raw}
    if source not in funcs: raise HTTPException(400,'匹配来源无效')
    candidates=smart_search_raw(query,artist=artist,album=track.get('album','') if not use_name else '',limit=8) if source=='smart' else funcs[source](query,8)
    if not candidates and source=='netease': candidates=mb_search_raw(query,8)
    candidates=candidates[:8]
    for candidate in candidates:
        title_score=_ratio(title,candidate.get('title'))
        artist_score=_ratio(artist,candidate.get('artist'))
        candidate['match_percent']=round(100*(.75*title_score+.25*artist_score)) if artist else round(100*title_score)
    candidates.sort(key=lambda c:c.get('match_percent',0),reverse=True)
    selected=candidates[0] if candidates else None
    lyric=''
    if selected and options.get('include_lyrics',True):
        if selected.get('source') in ('netease','migu','qq','kugou','kuwo') and selected.get('id'):
            try: lyric=uni_lyric(selected['source'],str(selected['id'])).get('lyric','') or ''
            except Exception: pass
        if not lyric and selected.get('title') and selected.get('artist'):
            try: lyric=lrclib_lyric(selected['artist'],selected['title'],selected.get('album') or '').get('lyric','') or ''
            except Exception: pass
        if _looks_lrc(lyric): selected['_lrc']=lyric; lyric=_strip_lrc(lyric)
        else: selected['_lrc']=''
    return {'path':track['path'],'name':track['name'],'original':track,'candidates':candidates,'candidate':selected,'score':selected.get('match_percent',0) if selected else 0,'lyric':lyric,'needs_review':conflict or not artist,'query':query,'query_mode':'filename' if use_name else 'tags'}


def _run_job_item(kind,item,options):
    if kind=='organize_scan': return _ORGANIZE.run_scan(item,options,store=_STORE,job_id=_jobs.current_job())
    if kind=='organize_apply': return _ORGANIZE.run_apply_item(item,options,store=_STORE,job_id=_jobs.current_job())
    if kind=='preview': return _preview_one(item,options)
    if kind=='apply':
        if not item.get('expected_version'): raise HTTPException(400,'写入任务缺少预览版本')
        cover=None
        if item.get('cover_url'):
            cover=_image_payload(url=item['cover_url'])
        return _WRITER.apply(item['path'],changes=item.get('fields') or {},expected=item['expected_version'],cover=cover,kind='batch')
    raise HTTPException(400,'未知任务类型')


_ENGINE = _Engine(_STORE,_run_job_item,workers=int(os.environ.get('JOB_WORKERS','2')))


class WorkflowTaskIn(BaseModel):
    model_config = _ConfigDict(extra='forbid')
    kind: _Literal['preview','apply']
    paths: list[str] = _Field(default_factory=list,max_length=50)
    items: list[dict] = _Field(default_factory=list,max_length=50)
    options: dict = _Field(default_factory=dict)
    confirmed: bool = False


class RestoreIn(BaseModel):
    expected_version: str = _Field(min_length=64,max_length=64)
    confirmed: bool = False


class CommitIn(SaveIn):
    cover_b64: Optional[str] = None
    cover_mime: str = 'image/jpeg'


@app.on_event('startup')
def workflow_startup():
    _WRITER.recover()
    _ENGINE.start()


@app.on_event('shutdown')
def workflow_shutdown():
    _ENGINE.stop()


@app.middleware('http')
async def workflow_request_guard(request: _Request, call_next):
    if request.method in ('POST','PUT','PATCH','DELETE'):
        from fastapi.responses import JSONResponse
        try: length=int(request.headers.get('content-length','0'))
        except ValueError: return JSONResponse({'detail':'请求长度无效'},status_code=400)
        if length>16*1024*1024: return JSONResponse({'detail':'请求不能超过 16 MiB'},status_code=413)
        origin=request.headers.get('origin')
        if origin:
            from urllib.parse import urlparse
            host=request.headers.get('host','')
            if urlparse(origin).netloc.lower()!=host.lower():
                return JSONResponse({'detail':'不允许跨站写入'},status_code=403)
    return await call_next(request)


class PluginToggleIn(BaseModel):
    id: str
    enabled: bool


@app.get("/api/plugins")
def api_plugins(include_removed: int = 0):
    """插件（数据源）清单：元数据 + 状态 + 统计；include_removed=1 时附带已删除的内置源。"""
    return _PLUGINS.snapshot(include_removed=bool(include_removed))


@app.post("/api/plugins/toggle")
def api_plugins_toggle(b: PluginToggleIn):
    try:
        _PLUGINS.set_enabled(b.id, bool(b.enabled))
    except KeyError:
        raise HTTPException(404, "unknown plugin: " + str(b.id))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "plugins": _PLUGINS.snapshot()["plugins"]}


class PluginSaveIn(BaseModel):
    id: Optional[str] = None
    display_name: Optional[str] = None
    description: Optional[str] = None
    order: Optional[int] = None
    params: Optional[dict] = None
    search_url: Optional[str] = None
    method: Optional[str] = None
    headers: Optional[dict] = None
    timeout: Optional[float] = None
    limit: Optional[int] = None
    result_path: Optional[str] = None
    fields: Optional[dict] = None
    enabled: Optional[bool] = None


class PluginIdIn(BaseModel):
    id: str


class PluginTestIn(BaseModel):
    id: Optional[str] = None
    config: Optional[dict] = None
    q: str = "test"


@app.post("/api/plugins/save")
def api_plugins_save(b: PluginSaveIn):
    """新建（无 id）或编辑（有 id）插件；内置源只允许改显示名 / 描述 / 排序 / 参数。"""
    payload = b.model_dump(exclude_none=True)
    try:
        item = _PLUGINS.save(payload)
    except KeyError as e:
        raise HTTPException(404, "unknown plugin: " + str(e))
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))
    if payload.get("enabled") is not None:
        try:
            _PLUGINS.set_enabled(item["id"], bool(payload["enabled"]))
        except ValueError:
            pass
        item = _PLUGINS.get(item["id"])
    return {"ok": True, "plugin": item, "plugins": _PLUGINS.snapshot()["plugins"]}


@app.post("/api/plugins/delete")
def api_plugins_delete(b: PluginIdIn):
    """删除插件：内置源移入「已删除」（可恢复），自定义源真删。"""
    try:
        info = _PLUGINS.delete(b.id)
    except KeyError:
        raise HTTPException(404, "unknown plugin: " + str(b.id))
    return {"ok": True, "deleted": info, "plugins": _PLUGINS.snapshot()["plugins"]}


@app.post("/api/plugins/restore")
def api_plugins_restore(b: PluginIdIn):
    """恢复被删除的内置源。"""
    try:
        _PLUGINS.restore(b.id)
    except KeyError:
        raise HTTPException(404, "plugin is not removed: " + str(b.id))
    return {"ok": True, "plugins": _PLUGINS.snapshot()["plugins"]}


@app.post("/api/plugins/test")
def api_plugins_test(b: PluginTestIn):
    """试跑一次自定义数据源（保存前也可测），返回样例便于校对字段映射。"""
    if b.config:
        defn = dict(b.config)
        defn.setdefault("id", "custom-preview")
    elif b.id:
        defn = _PLUGINS.custom_definition(b.id)
        if defn is None:
            raise HTTPException(400, "只能试跑自定义数据源")
    else:
        raise HTTPException(400, "需要提供 id 或 config")
    return _custom_test(defn, b.q)


@app.get('/api/system')
def workflow_system():
    return {'app_version':_APP_VERSION,'storage':_STORE.health(),'job_workers':_ENGINE.workers,'metadata_backups_only':True,'max_batch':50,'write_confirmation_required':True,'supported_full_formats':['mp3','flac','ogg','opus'],'basic_edit_formats':['m4a','mp4']}


@app.post('/api/commit')
def workflow_commit(body: CommitIn):
    _must_expected(body.expected_version)
    cover=_image_payload(b64=body.cover_b64,mime=body.cover_mime) if body.cover_b64 else None
    supplied=body.model_fields_set
    changes={key:getattr(body,key) for key in _FIELDS if key in supplied}
    return _WRITER.apply(body.path,changes=changes,filename=body.filename,expected=body.expected_version,cover=cover,kind='manual')


@app.get('/api/tasks')
def workflow_tasks(limit: int=30):
    return {'jobs':_STORE.jobs(limit)}


@app.post('/api/tasks')
def workflow_create_task(body: WorkflowTaskIn):
    if body.kind=='preview':
        paths=list(dict.fromkeys(body.paths))
        items=[{'path':_WRITER.relative(_WRITER.path(path))} for path in paths]
    else:
        if not body.confirmed: raise HTTPException(400,'写入必须经过明确确认')
        items=[]
        for item in body.items:
            path=_WRITER.relative(_WRITER.path(item.get('path','')))
            version=item.get('expected_version')
            if not isinstance(version,str) or not re.fullmatch(r'[0-9a-f]{64}',version): raise HTTPException(400,'写入任务缺少有效的当前版本')
            fields=item.get('fields') or {}
            if not isinstance(fields,dict) or set(fields)-set(_FIELDS): raise HTTPException(400,'标签字段无效')
            if any(not isinstance(v,str) or len(v)>1024*1024 for v in fields.values()): raise HTTPException(400,'标签内容无效或过大')
            cover_url=item.get('cover_url')
            if cover_url is not None and (not isinstance(cover_url,str) or len(cover_url)>4096): raise HTTPException(400,'封面地址无效')
            items.append({'path':path,'expected_version':version,'fields':fields,'cover_url':cover_url})
        if len({item['path'] for item in items})!=len(items): raise HTTPException(400,'同一任务不能重复写同一文件')
    try: job_id=_STORE.create_job(body.kind,items,body.options)
    except ValueError as e: raise HTTPException(400,str(e))
    _ENGINE.notify()
    return {'job_id':job_id,'status':'pending','writes_music':body.kind=='apply'}


@app.get('/api/tasks/{job_id}')
def workflow_task(job_id: str):
    try: return _STORE.job(job_id)
    except KeyError: raise HTTPException(404,'任务不存在')


@app.post('/api/tasks/{job_id}/cancel')
def workflow_cancel_task(job_id: str):
    try: _STORE.cancel(job_id)
    except KeyError: raise HTTPException(404,'任务不存在')
    return {'ok':True,'message':'已请求安全停止'}


@app.get('/api/history')
def workflow_history(path: Optional[str]=None,limit: int=50):
    if path is not None: path=_WRITER.relative(_WRITER.path(path))
    rows=_STORE.versions(limit,path)
    for row in rows:
        row.pop('snapshot',None)
    return {'versions':rows}


@app.get('/api/history/{version_id}/preview')
def workflow_restore_preview(version_id: str):
    try: return _WRITER.restore_preview(version_id)
    except KeyError: raise HTTPException(404,'记录不存在')


@app.post('/api/history/{version_id}/restore')
def workflow_restore(version_id: str,body: RestoreIn):
    if not body.confirmed: raise HTTPException(400,'回退必须再次确认')
    try: return _WRITER.restore(version_id,body.expected_version)
    except KeyError: raise HTTPException(404,'记录不存在')



# ===== V4.1：统一出网代理 + 实时日志（SSE） =====
import collections as _collections
import logging as _logging
import queue as _queue
import threading as _threading
from urllib.parse import urlparse as _urlparse

from fastapi.responses import StreamingResponse as _StreamingResponse

_LOG = _logging.getLogger('musicinfo')
_LOG.setLevel(_logging.INFO)

_DATA_DIR = os.environ.get('DATA_DIR', '/data')
_SETTINGS_FILE = os.path.join(_DATA_DIR, 'settings.json')
_SET_LOCK = _threading.Lock()


def _default_settings():
    return {'proxy': {'enabled': False, 'url': ''}}


def _load_app_settings():
    cfg = _default_settings()
    try:
        with open(_SETTINGS_FILE, 'r', encoding='utf-8') as fh:
            raw = json.load(fh)
    except Exception:
        raw = None
    if isinstance(raw, dict):
        node = raw.get('proxy')
        if isinstance(node, dict):
            cfg['proxy']['enabled'] = bool(node.get('enabled'))
            url = node.get('url')
            cfg['proxy']['url'] = url.strip() if isinstance(url, str) else ''
    return cfg


_SETTINGS = _load_app_settings()


def _save_app_settings():
    with _SET_LOCK:
        os.makedirs(_DATA_DIR, exist_ok=True)
        tmp = _SETTINGS_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(_SETTINGS, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, _SETTINGS_FILE)


def _proxy_url():
    node = _SETTINGS.get('proxy') or {}
    return (node.get('url') or '').strip() if node.get('enabled') else ''


def _valid_proxy_url(url):
    if not url:
        return True
    if len(url) > 300 or any(ch.isspace() for ch in url):
        return False
    try:
        parsed = _urlparse(url)
        port = parsed.port
    except Exception:
        return False
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        return False
    return port is None or 0 < port < 65536


def _apply_proxy_env():
    url = _proxy_url()
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        if url:
            os.environ[key] = url
        else:
            os.environ.pop(key, None)
    if url:
        os.environ['NO_PROXY'] = os.environ['no_proxy'] = 'localhost,127.0.0.1,::1'
    else:
        os.environ.pop('NO_PROXY', None)
        os.environ.pop('no_proxy', None)


_apply_proxy_env()

# ---- 日志环形缓冲 + SSE 广播 ----
_LOG_LIMIT = 3000
_LOG_LINES = _collections.deque(maxlen=_LOG_LIMIT)
_LOG_QUEUES = []
_LOG_SEQ = [0]
_LOG_RANK = {'DEBUG': 10, 'INFO': 20, 'WARNING': 30, 'ERROR': 40, 'CRITICAL': 50}


class _LogCapture(_logging.Handler):
    def __init__(self):
        super().__init__()
        self.setFormatter(_logging.Formatter())

    def emit(self, record):
        try:
            msg = record.getMessage()
            if record.exc_info:
                msg += '\n' + self.formatter.formatException(record.exc_info)
        except Exception:
            msg = str(record.msg)
        _LOG_SEQ[0] += 1
        item = {'id': _LOG_SEQ[0], 'ts': int(record.created * 1000),
                'level': record.levelname, 'src': record.name, 'msg': msg}
        _LOG_LINES.append(item)
        for q in list(_LOG_QUEUES):
            try:
                q.put_nowait(item)
            except Exception:
                pass


_capture = _LogCapture()
_capture.setLevel(_logging.DEBUG)
for _target_name in ('uvicorn.error', 'uvicorn.access', 'musicinfo'):
    _target_logger = _logging.getLogger(_target_name)
    _target_logger.addHandler(_capture)
    if _target_logger.level == _logging.NOTSET or _target_logger.level > _logging.INFO:
        _target_logger.setLevel(_logging.INFO)

_LOG.info('服务启动：出网代理%s · 实时日志缓冲就绪（%d 行）', ('已启用 ' + _proxy_url()) if _proxy_url() else '未启用（直连）', _LOG_LIMIT)


class SettingsIn(BaseModel):
    proxy: Optional[dict] = None


class ProxyTestIn(BaseModel):
    url: Optional[str] = None


@app.get('/api/settings')
def api_settings():
    return {'settings': _SETTINGS}


@app.post('/api/settings')
def api_settings_save(b: SettingsIn):
    node = b.proxy if isinstance(b.proxy, dict) else {}
    url = str(node.get('url') or '').strip()
    if not _valid_proxy_url(url):
        raise HTTPException(400, '代理地址无效：需以 http:// 或 https:// 开头，例如 http://10.0.0.77:7890')
    enabled = bool(node.get('enabled'))
    if enabled and not url:
        raise HTTPException(400, '启用代理前请先填写代理地址')
    _SETTINGS['proxy'] = {'enabled': enabled, 'url': url}
    _save_app_settings()
    _apply_proxy_env()
    _LOG.info('代理设置已保存：%s · %s', '启用' if enabled else '未启用', url or '（未填写地址）')
    return {'ok': True, 'settings': _SETTINGS}


@app.post('/api/settings/test_proxy')
def api_settings_test_proxy(b: ProxyTestIn):
    url = str(b.url or '').strip()
    if url and not _valid_proxy_url(url):
        raise HTTPException(400, '代理地址无效：需以 http:// 或 https:// 开头')
    proxies = {'http': url, 'https': url} if url else None
    targets = [
        'https://itunes.apple.com/search?term=music&limit=1',
        'https://musicbrainz.org/ws/2/artist?query=music&fmt=json&limit=1',
    ]
    results = []
    for target in targets:
        t0 = time.time()
        try:
            resp = requests.get(target, proxies=proxies, timeout=8, headers={'User-Agent': MB_UA})
            ok = resp.status_code < 400
            results.append({'target': target, 'ok': ok, 'status': resp.status_code,
                            'ms': int((time.time() - t0) * 1000), 'error': '' if ok else ('HTTP %d' % resp.status_code)})
        except Exception as exc:
            results.append({'target': target, 'ok': False, 'status': 0,
                            'ms': int((time.time() - t0) * 1000), 'error': str(exc)[:200]})
        if results[-1]['ok']:
            break
    ok = bool(results) and results[0]['ok']
    _LOG.info('代理测试：%s · %s', url or '直连（不填地址）',
              ('通过 %dms' % results[0]['ms']) if ok else ('失败：' + (results[0].get('error') or '')))
    return {'ok': ok, 'results': results, 'url': url}


@app.get('/api/logs')
def api_logs(limit: int = 400, level: str = '', q: str = ''):
    limit = max(1, min(limit, 5000))
    floor = _LOG_RANK.get((level or '').strip().upper(), 0)
    needle = (q or '').strip().lower()
    out = []
    for item in reversed(_LOG_LINES):
        if floor and _LOG_RANK.get(item['level'], 20) < floor:
            continue
        if needle and needle not in (item['msg'] + ' ' + item['src'] + ' ' + item['level']).lower():
            continue
        out.append(item)
        if len(out) >= limit:
            break
    out.reverse()
    return {'logs': out, 'total': len(_LOG_LINES), 'seq': _LOG_SEQ[0], 'limit': limit}


def _log_stream_gen():
    q = _queue.Queue(maxsize=2000)
    _LOG_QUEUES.append(q)

    try:
        yield 'retry: 3000\n\n'
        while True:
            try:
                item = q.get(timeout=15)
            except _queue.Empty:
                yield ': keep-alive\n\n'
                continue
            yield 'data: ' + json.dumps(item, ensure_ascii=False) + '\n\n'
    finally:
        try:
            _LOG_QUEUES.remove(q)
        except ValueError:
            pass


@app.get('/api/logs/stream')
def api_logs_stream():
    return _StreamingResponse(_log_stream_gen(), media_type='text/event-stream',
                              headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no', 'Connection': 'keep-alive'})

# ---------------------------------------------------------------- V4.2 本地自动整理
# 只新增 /api/organize/* 端点；任务仍然走 _STORE + _ENGINE，
# 过程日志进现有实时日志缓冲（来源 musicinfo.organize）。
import mtl_jobs as _jobs                   # noqa: E402
import mtl_organize as _ORGANIZE           # noqa: E402
import mtl_organize_api as _ORGANIZE_API   # noqa: E402


def _organize_log(message, level='info'):
    """把整理过程写进实时日志（环形缓冲 + SSE）。"""
    _logging.getLogger('musicinfo.organize').log(
        {'debug': _logging.DEBUG, 'info': _logging.INFO, 'warning': _logging.WARNING,
         'error': _logging.ERROR, 'critical': _logging.CRITICAL}.get(str(level or 'info').lower(),
                                                                     _logging.INFO),
        str(message))


_ORGANIZE_API.register(app, _STORE, _ENGINE, _SETTINGS,
                       persist_settings=_save_app_settings, log=_organize_log)


app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static"), html=True), name="static")
