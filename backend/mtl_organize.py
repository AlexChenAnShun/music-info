"""本地曲库自动整理（Music Info · V5）

把「未整理/待整理」里的音乐按 歌手/专辑/歌名 归档到「已整理」，重复文件进「已存在」，
无用文件（非音频、孤儿歌词/图片、隐藏文件、读取失败）进「冗余数据」。

安全约定（与云端 CMS 的自动整理思路一致，但全部在本机文件系统完成）：
* 只移动 / 改名，绝不删除、绝不覆盖：目标重名时自动加 “ (2)” 后缀。
* 先「扫描」生成计划（干跑，只读），再把计划中的每个文件作为一个后台任务条目执行。
* 执行前逐个核对大小与 mtime 未变化；服务重启后不会自动重放。
* 与「已整理」库比对仍使用严格 SHA-256 内容相同判定。

本模块不依赖 FastAPI 之外的第三方库；标签读取用 mutagen。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
import unicodedata
import uuid
from pathlib import Path

from fastapi import HTTPException
from mutagen import File as MFile

try:                       # 二级分类策略用 YAML 书写（与 CMS 一致）
    import yaml
except Exception:          # pragma: no cover - 容器内已固定安装 PyYAML
    yaml = None

# ---------------------------------------------------------------- 常量

AUDIO_EXTS = {
    '.flac', '.mp3', '.m4a', '.mp4', '.ogg', '.oga', '.opus', '.wav', '.aif', '.aiff',
    '.wma', '.ape', '.wv', '.dsf', '.dff', '.mka', '.ac3', '.dts',
}
IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif', '.tif', '.tiff'}
LRC_EXTS = {'.lrc'}
JUNK_FILE_NAMES = {'desktop.ini', 'thumbs.db', 'folder.jpg.tmp'}
JUNK_DIR_NAMES = {
    '@eadir', '.appledouble', '#recycle', '$recycle.bin', 'system volume information',
    '.trash', '.trashes', '.spotlight-v100', '.fseventsd',
}
LABELS = {'source': '待整理目录', 'duplicates': '已存在目录', 'junk': '冗余数据目录',
          'library': '已整理归档目录'}
UNKNOWN_ARTIST = '未知歌手'
UNKNOWN_ALBUM = '未知专辑'
SEGMENT_LIMIT = 120

DEFAULT_SETTINGS = {
    'source': 'music/未整理/待整理',
    'duplicates': 'music/未整理/已存在',
    'junk': 'music/未整理/冗余数据',
    'library': 'music/已整理',
    # 容器内 /media 在 NAS 上的真实目录（只用于页面显示/让人照着去 NAS 里找）
    'host_root': '/vol1/1000/Docker/music-tag-lite',
    'track_prefix': False,
    'track_sep': '. ',
    'follow_sidecars': True,
    'unknown_artist': '未知歌手',
    'unknown_album': '未知专辑',
    # 重命名规则（模板；空 = 按下面的快捷开关推导）
    'name_template': '',
    'dir_template': '{artist}/{album}',
    # 二级分类策略（YAML 文本；空 = 关闭，仍用 目录模板）
    'classify_rules': '',
    # 洗版策略
    'wash_mode': 'coexist',      # coexist | prefer_better | always_replace | skip
    'wash_max_mb': 0,            # 0 = 不限制；超过该体积的新文件不触发洗版
}
WASH_MODES = ('coexist', 'prefer_better', 'always_replace', 'skip')
SAMPLE_TAGS = {'title': 'Shape of My Heart', 'artist': 'Sting', 'album': "Ten Summoner's Tales",
               'albumartist': 'Sting', 'track': 3, 'date': '1993-03-09', 'genre': 'Pop'}
SAMPLE_TAGS_EMPTY = {'title': '未命名作品', 'artist': '', 'album': '', 'albumartist': '',
                     'track': None, 'date': '', 'genre': ''}
TRACK_SHORTCUT_DEFAULT = '<{track:02d}. >{title}'   # 可选块：没有音轨号时不会留下孤立的分隔符

# Windows / SMB 不合法字符 → 全角
_ILLEGAL = {'\\': '＼', '/': '／', ':': '：', '*': '＊', '?': '？', '"': '＂',
            '<': '＜', '>': '＞', '|': '｜'}
_TRACK_SEPS = ('. ', ' - ', ' ')

_LOCK = threading.RLock()
_ACTIVE = {'plan_id': None, 'kind': None}


# ---------------------------------------------------------------- 设置

def default_settings():
    return dict(DEFAULT_SETTINGS)


def norm_rel_path(value):
    """把用户填写的路径整理成相对路径；拒绝空值、绝对路径与 ..。"""
    text = str(value or '').strip().replace('\\', '/').strip('/')
    while text.startswith('./'):
        text = text[2:]
    parts = [part for part in text.split('/') if part not in ('', '.')]
    if not parts:
        raise HTTPException(400, '目录不能为空')
    if any(part == '..' for part in parts):
        raise HTTPException(400, '目录不能包含 ..')
    if os.path.isabs(text) or re.match(r'^[A-Za-z]:', text):
        raise HTTPException(400, '请填写相对音乐库根目录的路径')
    return '/'.join(parts)


def normalize_settings(node):
    """把 _SETTINGS['organize'] 里的节点整理成合法配置（缺省值补全）。"""
    cfg = default_settings()
    node = node if isinstance(node, dict) else {}
    for key in ('source', 'duplicates', 'junk', 'library'):
        if node.get(key) is not None:
            cfg[key] = norm_rel_path(node[key])
    if node.get('track_prefix') is not None:
        cfg['track_prefix'] = bool(node['track_prefix'])
    if node.get('track_sep') is not None:
        sep = str(node['track_sep'])
        if sep not in _TRACK_SEPS:
            raise HTTPException(400, '音轨号分隔符只支持 “. ”、“ - ” 或空格')
        cfg['track_sep'] = sep
    if node.get('follow_sidecars') is not None:
        cfg['follow_sidecars'] = bool(node['follow_sidecars'])
    for key in ('unknown_artist', 'unknown_album'):
        if node.get(key) is not None:
            text = str(node[key]).strip()
            if not text:
                cfg[key] = DEFAULT_SETTINGS[key]
            else:
                cfg[key] = sanitize_segment(text, DEFAULT_SETTINGS[key])
    if node.get('name_template') is not None:
        cfg['name_template'] = str(node['name_template']).strip()
    if node.get('dir_template') is not None:
        text = str(node['dir_template']).strip().strip('/')
        if not text:
            raise HTTPException(400, '目录模板不能为空，例如：{artist}/{album}')
        cfg['dir_template'] = text
    if node.get('classify_rules') is not None:
        text = str(node['classify_rules'])
        parse_classify_rules(text)      # 写错的 YAML / 缺字段在这里就报错，不等到扫描
        cfg['classify_rules'] = text
    if node.get('wash_mode') is not None:
        mode = str(node['wash_mode']).strip()
        if mode not in WASH_MODES:
            raise HTTPException(400, '洗版策略只能是：%s' % ' / '.join(WASH_MODES))
        cfg['wash_mode'] = mode
    if node.get('host_root') is not None:
        text = str(node['host_root']).strip().rstrip('/')
        if text and not text.startswith('/'):
            raise HTTPException(400, '服务器实际路径要以 / 开头，例如 /vol1/1000/Docker/music-tag-lite（留空 = 只显示容器内路径）')
        cfg['host_root'] = text
    if node.get('wash_max_mb') is not None:
        try:
            size = int(node['wash_max_mb'])
        except (TypeError, ValueError):
            raise HTTPException(400, '洗版体积上限要写数字（MB，0 = 不限制）')
        if size < 0 or size > 1024 * 1024:
            raise HTTPException(400, '洗版体积上限超出范围（0–1048576 MB）')
        cfg['wash_max_mb'] = size
    return cfg


def host_path(cfg: dict, rel: str) -> str:
    """把容器内的相对路径翻译成 NAS 上的真实路径（纯显示用，不参与任何读写校验）。"""
    root = str(cfg.get('host_root') or '').rstrip('/')
    text = str(rel or '').strip().strip('/')
    if not root:
        return ''
    return root + ('/' + text if text else '')


def _media_root():
    return Path(os.environ.get('MEDIA_DIR', '/media')).resolve()


def _data_root():
    return Path(os.environ.get('DATA_DIR', '/data')).resolve()


def resolve_under(media: Path, rel: str) -> Path:
    rel = norm_rel_path(rel)
    path = (media / rel).resolve()
    if not path.is_relative_to(media):
        raise HTTPException(400, '路径必须位于音乐库目录内')
    return path


def settings_problems(media: Path, cfg: dict) -> list:
    """不抛异常地检查配置，返回 [{key, reason}]，供状态接口展示。"""
    problems = []
    try:
        cfg = normalize_settings(cfg)
    except HTTPException as exc:
        return [{'key': 'settings', 'reason': str(exc.detail)}]
    paths = {}
    for key in ('source', 'duplicates', 'junk', 'library'):
        try:
            paths[key] = resolve_under(media, cfg[key])
        except HTTPException as exc:
            problems.append({'key': key, 'reason': str(exc.detail)})
    keys = list(paths)
    for index, left in enumerate(keys):
        for right in keys[index + 1:]:
            first, second = paths[left], paths[right]
            if first == second or first.is_relative_to(second) or second.is_relative_to(first):
                if {left, right} == {'source', 'library'}:
                    reason = ('待整理目录（%s）必须在「已整理」归档目录（%s）之外，两者不能相同或互相包含'
                              % (cfg['source'], cfg['library']))
                else:
                    reason = ('%s（%s）与 %s（%s）不能相同或互相包含'
                              % (LABELS.get(left, left), cfg[left], LABELS.get(right, right), cfg[right]))
                problems.append({'key': right, 'reason': reason})
    return problems


def validate_settings(media: Path, cfg: dict) -> dict:
    """规范化 + 校验四个目录：媒体根之下、互不重叠。返回带绝对路径的 cfg。"""
    cfg = normalize_settings(cfg)
    problems = settings_problems(media, cfg)
    if problems:
        raise HTTPException(400, problems[0]['reason'])
    out = dict(cfg)
    out['abs'] = {key: str(resolve_under(media, cfg[key]))
                  for key in ('source', 'duplicates', 'junk', 'library')}
    return out


# ---------------------------------------------------------------- 小工具

_TOKEN_RE = re.compile(r'\{([a-z_]+)(?::0?(\d+)d)?\}')
_BLOCK_RE = re.compile(r'<([^<>]*)>')


def render_template(template: str, tokens: dict, filled: dict = None) -> str:
    """渲染命名模板。

    * `{变量}` 直接替换；`{track:02d}` 支持补零。
    * `<...>` 可选块：块内出现的变量只要有一个为空，整块丢弃（与 CMS 的写法一致）。
    * `tokens` 用于判断「是否为空」（缺标签就是空），`filled` 用于真正替换（可带占位名）。
    """
    filled = filled or tokens
    text = str(template or '')

    def _block(match):
        inner = match.group(1)
        names = [row[0] for row in _TOKEN_RE.findall(inner)]
        if names and all(str(tokens.get(name, '') or '') for name in names):
            return inner
        return ''

    for _ in range(5):                     # 支持一层嵌套的可选块
        newer = _BLOCK_RE.sub(_block, text)
        if newer == text:
            break
        text = newer

    def _token(match):
        name, pad = match.group(1), match.group(2)
        if name not in tokens and name not in filled:
            return match.group(0)          # 未知变量原样保留，便于用户发现拼写错误
        value = str((filled if name in filled else tokens).get(name, '') or '')
        if pad and value.isdigit():
            value = value.zfill(int(pad))
        return value

    return _TOKEN_RE.sub(_token, text)


_ORPHAN_EDGE = re.compile(r'^[\s._\-\u00b7]+|[\s._\-\u00b7]+$')


def tidy_stem(text: str) -> str:
    """清掉空变量留下的孤立分隔符：「. 歌名」→「歌名」、「A - 」→「A」，并压缩多余空格。"""
    text = re.sub(r'\s+', ' ', str(text or '')).strip()
    return _ORPHAN_EDGE.sub('', text).strip()


def name_template_of(cfg: dict) -> str:
    """生效的文件命名模板：自定义优先，否则按「音轨号前缀」快捷开关推导。"""
    text = str(cfg.get('name_template') or '').strip()
    if text:
        return text
    if cfg.get('track_prefix'):
        return '<{track:02d}%s>{title}' % (cfg.get('track_sep') or '. ')
    return '{title}'


def meta_tokens(cfg: dict, meta: dict):
    """把标签整理成模板变量：(raw, filled)。raw 用于 <...> 判空，filled 用于替换。"""
    title = str(meta.get('title') or '')
    artist = str(meta.get('artist') or '')
    album = str(meta.get('album') or '')
    albumartist = str(meta.get('albumartist') or artist)
    track = meta.get('track')
    year = str(meta.get('date') or '')[:4]
    if not year.isdigit():
        year = ''
    decade = (year[:3] + '0s') if year else ''
    source = albumartist or artist
    initial = ''
    if source:
        head = source[0]
        initial = head.upper() if head.isascii() and head.isalpha() else head
    raw = {'title': title, 'artist': artist, 'album': album, 'albumartist': albumartist,
           'track': '' if not track else str(track), 'year': year,
           'genre': str(meta.get('genre') or ''), 'decade': decade, 'artist_initial': initial}
    filled = dict(raw)
    filled['artist'] = artist or cfg.get('unknown_artist') or UNKNOWN_ARTIST
    filled['album'] = album or cfg.get('unknown_album') or UNKNOWN_ALBUM
    filled['albumartist'] = filled['albumartist'] or filled['artist']
    return raw, filled


def parse_classify_rules(text: str) -> list:
    """解析二级分类策略（YAML 列表）；空文本表示关闭。"""
    text = str(text or '').strip()
    if not text:
        return []
    if yaml is None:
        raise HTTPException(400, '服务端缺少 YAML 解析库，无法读取二级分类策略')
    try:
        data = yaml.safe_load(text)
    except Exception as exc:
        raise HTTPException(400, '二级分类策略不是合法 YAML：%s' % clean_error(exc))
    if data in (None, ''):
        return []
    if not isinstance(data, list):
        raise HTTPException(400, '二级分类策略要写成列表：每一条以 “- name:” 开头')
    rules = []
    for index, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise HTTPException(400, '第 %d 条二级分类规则要写成键值对' % index)
        path = str(item.get('path') or '').strip()
        if not path:
            raise HTTPException(400, '第 %d 条二级分类规则缺少 path' % index)
        match = item.get('match') or {}
        if not isinstance(match, dict):
            raise HTTPException(400, '第 %d 条二级分类规则的 match 要写成键值对' % index)

        def _list(value):
            if value in (None, ''):
                return []
            return [str(x).casefold() for x in value] if isinstance(value, list) else [str(value).casefold()]

        try:
            rule = {'name': str(item.get('name') or ('规则 %d' % index)), 'path': path,
                    'genre': _list(match.get('genre')),
                    'initial': _list(match.get('artist_initial')),
                    'decade': [str(x) for x in (match.get('decade') or [])] if isinstance(match.get('decade'), list)
                              else ([str(match.get('decade'))] if match.get('decade') else []),
                    'year_min': int(match.get('year_min') or 0), 'year_max': int(match.get('year_max') or 0)}
        except (TypeError, ValueError):
            raise HTTPException(400, '第 %d 条二级分类规则的 year_min/year_max 要写数字' % index)
        rules.append(rule)
    return rules


def rule_matches(rule: dict, meta: dict) -> bool:
    """判断一条二级分类规则是否命中（同一维度多个条件为「或」，不同维度为「且」）。"""
    genres = rule.get('genre') or []
    if genres:
        pool = str(meta.get('genre') or '').casefold()
        if not any(word and word in pool for word in genres):
            return False
    decades = rule.get('decade') or []
    if decades and str(meta.get('decade') or '') not in decades:
        return False
    initials = rule.get('initial') or []
    if initials:
        pool = str(meta.get('artist_initial') or '').casefold()
        if not pool or pool not in initials:
            return False
    year = str(meta.get('year') or '')
    if rule.get('year_min') and (not year.isdigit() or int(year) < rule['year_min']):
        return False
    if rule.get('year_max') and (not year.isdigit() or int(year) > rule['year_max']):
        return False
    return True


def quality_of(path: Path):
    """读取音频质量参数（洗版比较用）：码率 → 采样率 → 位深 → 体积。"""
    try:
        audio = MFile(str(path))
    except Exception:
        return None
    if audio is None:
        return None
    info = getattr(audio, 'info', None)
    try:
        size = path.stat().st_size
    except OSError:
        return None
    length = float(getattr(info, 'length', 0) or 0)
    rate = int(getattr(info, 'sample_rate', 0) or 0)
    bits = int(getattr(info, 'bits_per_sample', 0) or 0)
    kbps = int(getattr(info, 'bitrate', 0) or 0)
    if kbps > 10000:                       # 有些格式（如 WAV）返回的是 bit/s
        kbps = int(round(kbps / 1000))
    if kbps <= 0 and length > 0:
        kbps = int(size * 8 / length / 1000)
    if kbps <= 0:
        kbps = int(size / 1024)
    return {'kbps': kbps, 'rate': rate, 'bits': bits, 'size': size,
            'score': (kbps, rate, bits, size)}


def clean_error(error) -> str:
    text = str(error)
    text = re.sub(r'(?i)(token|secret|api[_-]?key|password)=([^\s&]+)', r'\1=[已隐藏]', text)
    text = re.sub(r'(https?://)[^\s/@]+:[^\s/@]+@', r'\1[已隐藏]@', text)
    return text[:500]


def _log(log, message, level='info'):
    if not log:
        return
    try:
        log(message, level)
    except Exception:
        pass


def _mklog(store, job_id):
    if store is None or not job_id:
        return None

    def log(message, level='info'):
        try:
            store.log(job_id, str(message), level)
        except Exception:
            pass
    return log


def _cancelled(store, job_id):
    if store is None or not job_id:
        return False
    try:
        return bool(store.cancelled(job_id))
    except Exception:
        return False


def file_fingerprint(path: Path, size: int, window: int = 64 * 1024) -> str:
    """快速内容指纹（首尾各 64 KiB 的 SHA-256），用于执行前复核文件是否变化。"""
    digest = hashlib.sha256()
    with open(path, 'rb') as fh:
        head = fh.read(window)
        digest.update(head)
        if size > len(head) + window:
            fh.seek(-window, os.SEEK_END)
            digest.update(fh.read(window))
    return digest.hexdigest()[:32]


def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def sanitize_segment(value, fallback='') -> str:
    """把一个片段清洗为可跨 Windows / SMB 使用的目录或文件名片段。"""
    text = unicodedata.normalize('NFC', str(value or ''))
    text = ''.join('' if unicodedata.category(ch) in ('Cc', 'Cf', 'Cs') else ch for ch in text)
    text = ''.join(_ILLEGAL.get(ch, ch) for ch in text)
    text = re.sub(r'\s+', ' ', text).strip()
    text = text.rstrip('. ')          # 只清理结尾的点和空格；前导点表示隐藏文件，保留
    if len(text) > SEGMENT_LIMIT:
        text = text[:SEGMENT_LIMIT].rstrip('. ')
    return text or fallback


def _tag_first(value) -> str:
    if value is None:
        return ''
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, (list, tuple)):
                item = item[0] if item else ''
            text = str(item).strip()
            if text:
                return text
        return ''
    return str(value).strip()


def _lookup(tags, *keys):
    """大小写不敏感地取值（兼容 ID3 帧、Vorbis、MP4 键名）。"""
    for key in keys:
        try:
            if key in tags:
                return tags[key]
        except Exception:
            pass
        lowered = key.lower()
        for existing in list(tags.keys() if hasattr(tags, 'keys') else []):
            if str(existing).lower() == lowered:
                try:
                    return tags[existing]
                except Exception:
                    return None
    return None


def read_tags(path: Path):
    """读取 title / artist / album / track。

    返回 (tags_dict, readable)：
    * readable=False 表示 mutagen 无法解析这个文件（视为读取失败）。
    """
    raw = None
    try:
        raw = MFile(str(path))
    except Exception:
        raw = None
    if raw is None:
        return {}, False
    tags = getattr(raw, 'tags', None)
    out = {'title': '', 'artist': '', 'album': '', 'track': '', 'date': '', 'genre': '', 'albumartist': ''}
    if not tags:
        return out, True
    try:
        out['title'] = _tag_first(_lookup(tags, 'TIT2', 'title', 'TITLE', '©nam'))
        out['artist'] = _tag_first(_lookup(tags, 'TPE1', 'artist', 'ARTIST', '©ART', 'aART'))
        out['album'] = _tag_first(_lookup(tags, 'TALB', 'album', 'ALBUM', '©alb'))
        out['track'] = _tag_first(_lookup(tags, 'TRCK', 'tracknumber', 'TRACKNUMBER', 'trkn'))
        out['date'] = _tag_first(_lookup(tags, 'TDRC', 'date', 'DATE', 'year', 'YEAR', '©day'))
        out['genre'] = _tag_first(_lookup(tags, 'TCON', 'genre', 'GENRE', '©gen'))
        out['albumartist'] = _tag_first(_lookup(tags, 'TPE2', 'albumartist', 'ALBUMARTIST', 'aART'))
    except Exception:
        pass
    return out, True


def parse_name(name: str):
    """从常见下载命名「歌名 - 歌手 - … - 专辑」里解析；最多给出 歌名/歌手/专辑。"""
    stem = Path(name).stem
    parts = [part.strip() for part in re.split(r'\s+-\s+', stem) if part.strip()]
    if not parts:
        return {'title': '', 'artist': '', 'album': ''}
    if len(parts) == 1:
        return {'title': parts[0], 'artist': '', 'album': ''}
    if len(parts) == 2:
        return {'title': parts[0], 'artist': parts[1], 'album': ''}
    return {'title': parts[0], 'artist': parts[1], 'album': parts[-1]}


def track_number(value) -> int:
    match = re.match(r'\s*(\d{1,3})', str(value or ''))
    return int(match.group(1)) if match else 0


def audio_kind(path: Path):
    ext = path.suffix.lower()
    if ext in AUDIO_EXTS:
        return 'audio'
    if ext in LRC_EXTS:
        return 'lyric'
    if ext in IMAGE_EXTS:
        return 'image'
    return ''

# ---------------------------------------------------------------- 计划存取

def _plans_dir() -> Path:
    return _data_root() / 'organize' / 'plans'


def _plan_path(plan_id: str) -> Path:
    return _plans_dir() / ('%s.json' % plan_id)


def new_plan_id() -> str:
    return time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]


def _save_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(payload, fh, ensure_ascii=False)
    os.replace(tmp, path)


def save_plan(plan: dict):
    with _LOCK:
        _save_json(_plan_path(plan['plan_id']), plan)


def load_plan(plan_id: str) -> dict:
    if not re.fullmatch(r'[0-9]{8}-[0-9]{6}-[0-9a-f]{6}', str(plan_id or '')):
        raise HTTPException(400, '计划编号无效')
    path = _plan_path(plan_id)
    if not path.is_file():
        raise HTTPException(404, '找不到这个整理计划（可能已被清理）')
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            return json.load(fh)
    except Exception as exc:
        raise HTTPException(400, '计划文件损坏：%s' % clean_error(exc))


def list_plans(limit: int = 20):
    rows = []
    directory = _plans_dir()
    if not directory.is_dir():
        return rows
    files = sorted(directory.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files[:max(1, min(50, int(limit or 20)))]:
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                plan = json.load(fh)
        except Exception:
            continue
        rows.append(plan_summary(plan))
    return rows


def plan_progress(plan_id: str, cfg: dict, scanned: int, total: int, started: float, status: str = 'scanning') -> dict:
    """占位 / 进度计划：让前端在扫描期间也能看到进度。"""
    payload = {
        'plan_id': plan_id, 'created': started, 'elapsed': round(time.time() - started, 2),
        'status': status, 'media': str(_media_root()),
        'cfg': {key: cfg.get(key) for key in DEFAULT_SETTINGS},
        'scanned': int(scanned), 'note': '',
        'counts': {'total': int(total), 'move': 0, 'duplicate': 0, 'junk': 0, 'error': 0, 'sidecar': 0},
        'applied': {'move': 0, 'wash': 0, 'duplicate': 0, 'junk': 0, 'failed': 0},
        'index': {'files': 0}, 'items': [],
    }
    _save_json(_plan_path(plan_id), payload)
    return payload


def prepare_plan(cfg: dict, plan_id: str) -> dict:
    """扫描任务创建时就落一个「扫描中」的计划，避免前端轮询 404。"""
    return plan_progress(plan_id, cfg, 0, 0, time.time())


def mark_terminal(plan_id: str, status: str, note: str = '') -> dict:
    """把还在 scanning 的计划改成终态（取消 / 中断 / 失败）。"""
    try:
        plan = load_plan(plan_id)
    except HTTPException:
        return {}
    if plan.get('status') != 'scanning':
        return plan
    plan['status'] = status
    plan['note'] = note
    plan['finished'] = time.time()
    save_plan(plan)
    return plan


def plan_summary(plan: dict) -> dict:
    return {
        'plan_id': plan.get('plan_id'),
        'created': plan.get('created'),
        'status': plan.get('status'),
        'source': (plan.get('cfg') or {}).get('source'),
        'library': (plan.get('cfg') or {}).get('library'),
        'counts': plan.get('counts') or {},
        'applied': plan.get('applied') or {},
    }

# ---------------------------------------------------------------- 已整理库索引

def _index_path() -> Path:
    return _data_root() / 'organize' / 'index.json'


def load_index() -> dict:
    try:
        with open(_index_path(), 'r', encoding='utf-8') as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get('files'), dict):
            return data
    except Exception:
        pass
    return {'version': 1, 'files': {}}


def save_index(index: dict):
    index['version'] = 1
    index['updated'] = time.time()
    _save_json(_index_path(), index)


def index_add(index: dict, rel: str, size: int, mtime_ns: int, digest: str, meta: dict = None):
    row = {'size': size, 'mtime_ns': mtime_ns, 'sha256': digest}
    if meta:
        row['tags'] = {'title': str(meta.get('title') or '')[:200], 'artist': str(meta.get('artist') or '')[:200],
                       'album': str(meta.get('album') or '')[:200]}
    index.setdefault('files', {})[rel] = row


def refresh_index(media: Path, library_abs: Path, log=None, cancel=None):
    """增量刷新「已整理」库索引：只在 大小/修改时间 变化时重新计算 SHA-256。"""
    index = load_index()
    files = index.get('files') or {}
    if not library_abs.is_dir():
        save_index(index)
        return index
    seen = set()
    pending = []
    for root, dirs, names in os.walk(library_abs, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d.lower() not in JUNK_DIR_NAMES and not d.startswith('.'))
        for name in sorted(names):
            path = Path(root) / name
            if audio_kind(path) != 'audio':
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            rel = path.relative_to(media).as_posix()
            seen.add(rel)
            known = files.get(rel)
            if known and known.get('size') == stat.st_size and known.get('mtime_ns') == stat.st_mtime_ns:
                continue
            pending.append((path, rel, stat))
    if pending:
        _log(log, '正在建立已整理库索引：%d 个文件需要计算（首次会稍慢）…' % len(pending))
    for done, (path, rel, stat) in enumerate(pending, start=1):
        if cancel and cancel():
            _log(log, '索引更新被停止；本次计划取消。', 'warning')
            raise HTTPException(499, '已请求停止')
        try:
            digest = sha256_file(path)
        except OSError as exc:
            _log(log, '索引跳过 %s：%s' % (path.name, clean_error(exc)), 'warning')
            continue
        tags, _readable = read_tags(path)
        index_add(index, rel, stat.st_size, stat.st_mtime_ns, digest, meta=tags)
        if done % 25 == 0 or done == len(pending):
            _log(log, '索引 %d/%d：%s' % (done, len(pending), path.name))
    dropped = [rel for rel in files if rel not in seen]
    for rel in dropped:
        files.pop(rel, None)
    if dropped:
        _log(log, '索引清理：移除 %d 个已不存在的条目。' % len(dropped))
    save_index(index)
    return index


def same_song_index(index: dict) -> dict:
    """按「歌名 + 歌手」聚合已整理库里的条目，供洗版策略使用。"""
    out = {}
    for rel, meta in (index.get('files') or {}).items():
        tags = meta.get('tags') or {}
        title = str(tags.get('title') or '').strip().casefold()
        artist = str(tags.get('artist') or '').strip().casefold()
        if not title or not artist:
            continue
        out.setdefault((title, artist), []).append({'rel': rel, 'size': int(meta.get('size') or 0)})
    return out


def index_lookup(index: dict):
    """返回 (按大小分组, sha256→rel)。"""
    by_size, by_hash = {}, {}
    for rel, meta in (index.get('files') or {}).items():
        try:
            by_size.setdefault(int(meta.get('size')), []).append(rel)
        except Exception:
            continue
        digest = meta.get('sha256')
        if digest:
            by_hash.setdefault(digest, rel)
    return by_size, by_hash

# ---------------------------------------------------------------- 扫描

def _walk_source(src_abs: Path):
    for root, dirs, names in os.walk(src_abs, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d.lower() not in JUNK_DIR_NAMES and not d.startswith('.'))
        for name in sorted(names, key=lambda x: x.lower()):
            yield Path(root) / name


def _reason_for_tags(tags, parsed, name) -> str:
    if tags.get('title') or tags.get('artist') or tags.get('album'):
        if tags.get('title'):
            return '按标签整理'
        return '标签不完整，标题取自文件名'
    if parsed.get('title') and parsed.get('title') != Path(name).stem:
        return '标签缺失，按文件名解析'
    return '标签缺失，标题取自文件名'


def build_plan(cfg: dict, plan_id: str = None, log=None, cancel=None) -> dict:
    """扫描源目录并生成整理计划（只读；不移动任何文件）。"""
    media = _media_root()
    cfg = validate_settings(media, cfg)
    src_abs = media / cfg['source']
    if not src_abs.is_dir():
        raise HTTPException(400, '源文件夹不存在：%s（请先在 NAS 上创建）' % cfg['source'])
    plan_id = plan_id or new_plan_id()
    started = time.time()

    index = refresh_index(media, media / cfg['library'], log=log, cancel=cancel)
    by_size, by_hash = index_lookup(index)
    library_sizes = set(by_size)
    same_song = same_song_index(index)
    rules = parse_classify_rules(cfg.get('classify_rules') or '')
    if rules:
        _log(log, '二级分类策略生效：%d 条规则（从上往下匹配，命中即用）' % len(rules))
    if (cfg.get('wash_mode') or 'coexist') != 'coexist':
        _log(log, '洗版策略：%s（同歌手 + 同歌名视为同一首歌）' % cfg['wash_mode'])

    _log(log, '开始扫描 %s …' % cfg['source'])
    entries = []
    for path in _walk_source(src_abs):
        if cancel and cancel():
            raise HTTPException(499, '已请求停止')
        try:
            stat = path.stat()
        except OSError as exc:
            entries.append({'ord': len(entries), 'kind': 'error', 'src': path.relative_to(media).as_posix(),
                            'name': path.name, 'reason': '文件读取失败：%s' % clean_error(exc),
                            'size': 0, 'mtime_ns': 0, 'status': 'pending', 'sidecars': [],
                            'target': None, 'error': '', 'at': time.time()})
            continue
        try:
            fingerprint = file_fingerprint(path, stat.st_size)
        except OSError as exc:
            entries.append({'ord': len(entries), 'kind': 'error', 'src': path.relative_to(media).as_posix(),
                            'name': path.name, 'reason': '文件无法读取：%s' % clean_error(exc),
                            'size': 0, 'mtime_ns': 0, 'fp': '', 'status': 'pending', 'sidecars': [],
                            'target': None, 'error': '', 'at': time.time()})
            continue
        entries.append({'ord': len(entries), 'kind': '', 'src': path.relative_to(media).as_posix(),
                        'name': path.name, 'reason': '', 'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns,
                        'fp': fingerprint, 'status': 'pending', 'sidecars': [], 'target': None,
                        'error': '', 'at': time.time()})
    _log(log, '共发现 %d 个文件，开始分类…' % len(entries))

    # 同一批里同名同内容 → 需要比对体积
    size_counter = {}
    for entry in entries:
        if audio_kind(Path(entry['src'])) == 'audio':
            size_counter[entry['size']] = size_counter.get(entry['size'], 0) + 1

    seen_hashes = {}
    actions = []
    for done, entry in enumerate(entries, start=1):
        if cancel and cancel():
            raise HTTPException(499, '已请求停止')
        if done % 25 == 0 or done == len(entries):
            _log(log, '分类 %d/%d：%s' % (done, len(entries), entry['name']))
            if done != len(entries):
                try:
                    plan_progress(plan_id, cfg, done, len(entries), started)
                except OSError as exc:
                    _log(log, '写入进度失败：%s' % clean_error(exc), 'warning')
        path = media / entry['src']
        name = entry['name']
        if name.startswith('.'):
            entry['kind'], entry['reason'] = 'junk', '隐藏文件'
            entry['target'] = cfg['junk'] + '/' + sanitize_segment(name, 'hidden')
            continue
        if name.lower() in JUNK_FILE_NAMES:
            entry['kind'], entry['reason'] = 'junk', '系统文件'
            entry['target'] = cfg['junk'] + '/' + sanitize_segment(name, 'system')
            continue
        kind = audio_kind(path)
        if kind == 'audio':
            tags, readable = read_tags(path)
            parsed = parse_name(name)
            if not readable:
                entry['kind'], entry['reason'] = 'junk', '无法读取音频信息（可能损坏或格式不支持）'
                entry['target'] = cfg['junk'] + '/' + sanitize_segment(name, 'unknown')
                continue
            title = sanitize_segment(tags.get('title') or parsed.get('title'), '')
            artist = sanitize_segment(tags.get('artist') or parsed.get('artist'), '')
            album = sanitize_segment(tags.get('album') or parsed.get('album'), '')
            if not title:
                entry['kind'], entry['reason'] = 'junk', '标题为空，无法归档'
                entry['target'] = cfg['junk'] + '/' + sanitize_segment(name, 'untitled')
                continue
            number = track_number(tags.get('track'))
            tokens, filled = meta_tokens(cfg, {'title': title, 'artist': artist, 'album': album,
                                               'albumartist': tags.get('albumartist'), 'track': number,
                                               'date': tags.get('date'), 'genre': tags.get('genre')})
            entry['title'], entry['artist'], entry['album'], entry['track'] = title, artist, album, number
            entry['year'], entry['genre'] = tokens['year'], tokens['genre']
            stem = tidy_stem(sanitize_segment(
                tidy_stem(render_template(name_template_of(cfg), tokens, filled)), title))
            dir_template, rule_name = cfg['dir_template'], ''
            for rule in rules:
                if rule_matches(rule, {'genre': entry['genre'], 'year': tokens['year'],
                                    'decade': tokens['decade'], 'artist_initial': tokens['artist_initial']}):
                    dir_template, rule_name = rule['path'], rule['name']
                    break
            dir_parts = [part for part in
                         (tidy_stem(sanitize_segment(piece, '')) for piece in
                          str(render_template(dir_template, tokens, filled)).split('/')) if part]
            entry['target'] = '/'.join([cfg['library']] + dir_parts + [stem + path.suffix.lower()])
            entry['reason'] = _reason_for_tags(tags, parsed, name)
            if rule_name:
                entry['reason'] += '；二级分类：' + rule_name
                entry['rule'] = rule_name
            # 内容判定：只在 体积与库内/本批 相同时才计算 SHA-256
            may_dup = entry['size'] in library_sizes or size_counter.get(entry['size'], 0) > 1
            if may_dup:
                try:
                    entry['sha256'] = sha256_file(path)
                except OSError as exc:
                    entry['sha256'] = ''
                    _log(log, '哈希失败 %s：%s' % (name, clean_error(exc)), 'warning')
                digest = entry.get('sha256')
                if digest:
                    first = seen_hashes.get(digest)
                    if first:
                        entry['kind'] = 'duplicate'
                        entry['reason'] = '与本批《%s》内容相同（SHA-256）' % Path(first).name
                        entry['dup_of'] = first
                        entry['target'] = cfg['duplicates'] + '/' + sanitize_segment(name, 'dup' + path.suffix.lower())
                        continue
                    lib_rel = by_hash.get(digest)
                    if lib_rel:
                        entry['kind'] = 'duplicate'
                        entry['reason'] = '与已整理库《%s》内容相同（SHA-256）' % Path(lib_rel).name
                        entry['dup_of'] = lib_rel
                        entry['target'] = cfg['duplicates'] + '/' + sanitize_segment(name, 'dup' + path.suffix.lower())
                        continue
                    seen_hashes[digest] = entry['src']
            entry['kind'] = 'move'
            model = cfg.get('wash_mode') or 'coexist'
            if model != 'coexist' and artist:
                rivals = same_song.get((title.casefold(), artist.casefold())) or []
                limit = int(cfg.get('wash_max_mb') or 0) * 1024 * 1024
                if rivals and limit and entry['size'] > limit:
                    entry['reason'] += '；新文件超过洗版体积上限 %d MB，保留两份' % int(cfg['wash_max_mb'])
                elif rivals:
                    mine = quality_of(path)
                    best, best_score = None, None
                    for row in rivals:
                        score = quality_of(media / row['rel'])
                        if score is None:
                            continue
                        if best is None or score['score'] > best_score['score']:
                            best, best_score = row, score
                    mine_kbps = mine['kbps'] if mine else 0
                    better = bool(mine and best_score and mine['score'] > best_score['score'])
                    if best and model == 'skip':
                        entry['kind'] = 'duplicate'
                        entry['dup_of'] = best['rel']
                        entry['target'] = cfg['duplicates'] + '/' + sanitize_segment(name, 'wash' + path.suffix.lower())
                        entry['reason'] = '洗版跳过：库内已有《%s》，新文件不覆盖' % Path(best['rel']).stem
                    elif best and model == 'prefer_better' and not better:
                        entry['kind'] = 'duplicate'
                        entry['dup_of'] = best['rel']
                        entry['target'] = cfg['duplicates'] + '/' + sanitize_segment(name, 'wash' + path.suffix.lower())
                        entry['reason'] = '洗版：库内版本音质更好（%d kbps ≥ %d kbps），保留原版' % (best_score['kbps'], mine_kbps)
                    elif best:
                        entry['kind'] = 'wash'
                        entry['wash_of'] = best['rel']
                        entry['wash_to'] = cfg['duplicates'] + '/' + sanitize_segment(
                            Path(best['rel']).name, 'wash' + path.suffix.lower())
                        entry['reason'] = ('洗版替换（强制）：库内《%s》移入「已存在」' % Path(best['rel']).stem
                                           if model == 'always_replace' else
                                           '洗版替换：库内《%s》（%d kbps）移入「已存在」，新文件 %d kbps'
                                           % (Path(best['rel']).stem, best_score['kbps'], mine_kbps))
            continue
        if kind in ('lyric', 'image'):
            entry['kind'] = 'sidecar'
            entry['reason'] = '附属文件待匹配同名歌曲'
            continue
        entry['kind'], entry['reason'] = 'junk', '非音乐文件'
        entry['target'] = cfg['junk'] + '/' + sanitize_segment(name, 'other')

    # 附属文件：跟随同名歌曲
    audio_by_stem = {}
    for entry in entries:
        if entry.get('kind') in ('move', 'duplicate'):
            key = (os.path.dirname(entry['src']).lower(), Path(entry['name']).stem.lower())
            audio_by_stem.setdefault(key, entry)
    for entry in entries:
        if entry.get('kind') != 'sidecar':
            continue
        key = (os.path.dirname(entry['src']).lower(), Path(entry['name']).stem.lower())
        owner = audio_by_stem.get(key)
        if not owner:
            entry['kind'], entry['reason'] = 'junk', '找不到对应歌曲的附属文件'
            entry['target'] = cfg['junk'] + '/' + sanitize_segment(entry['name'], 'orphan')
            continue
        if not cfg.get('follow_sidecars', True):
            entry['kind'], entry['reason'] = 'junk', '未启用附属文件跟随'
            entry['target'] = cfg['junk'] + '/' + sanitize_segment(entry['name'], 'sidecar')
            continue
        new_name = Path(owner['target']).stem + Path(entry['name']).suffix.lower()
        entry['kind'], entry['reason'] = 'sidecar', '跟随歌曲（%s）' % owner['name']
        entry['target'] = (Path(owner['target']).parent / new_name).as_posix()
        entry['owner'] = owner['ord']
        owner['sidecars'].append({'src': entry['src'], 'name': entry['name'], 'size': entry.get('size', 0),
                                  'fp': entry.get('fp', ''), 'target': entry['target'],
                                  'status': 'pending', 'error': ''})

    # 目标重名：先按计划内顺序占位，实际执行时还会再次避让
    taken = set()
    for entry in entries:
        target = entry.get('target')
        if not target:
            continue
        if entry.get('kind') == 'sidecar' and entry.get('owner') is not None:
            continue  # 跟随歌曲，避免与歌曲各自去重逻辑冲突
        if entry.get('kind') == 'wash':
            taken.add(target.lower())   # 洗版：目标就是被替换的旧文件位置，执行时先把它挪走
            continue
        target = _unique_in_plan(media, target, taken)
        entry['target'] = target
        taken.add(target.lower())
    for entry in entries:
        if entry.get('kind') == 'sidecar' and entry.get('owner') is not None:
            owner = entries[entry['owner']]
            new_name = Path(owner['target']).stem + Path(entry['name']).suffix.lower()
            entry['target'] = (Path(owner['target']).parent / new_name).as_posix()

    counts = {'total': len(entries), 'move': 0, 'wash': 0, 'duplicate': 0, 'junk': 0, 'error': 0, 'sidecar': 0}
    for entry in entries:
        kind = entry.get('kind')
        if kind == 'sidecar':
            continue                      # 附属文件通过所属歌曲统计
        if kind not in ('move', 'wash', 'duplicate', 'junk'):
            kind = 'error'
        counts[kind] += 1
        counts['sidecar'] += len(entry.get('sidecars') or [])

    plan = {
        'plan_id': plan_id,
        'created': time.time(),
        'elapsed': round(time.time() - started, 2),
        'status': 'ready',
        'scanned': len(entries),
        'note': '',
        'media': str(media),
        'cfg': {key: cfg[key] for key in DEFAULT_SETTINGS},
        'counts': counts,
        'applied': {'move': 0, 'wash': 0, 'duplicate': 0, 'junk': 0, 'failed': 0},
        'index': {'files': len((index.get('files') or {}))},
        'items': entries,
    }
    save_plan(plan)
    move_rules = {'按标签整理': 0, '按文件名': 0}
    for entry in entries:
        if entry.get('kind') == 'move':
            move_rules['按文件名' if '文件名' in entry.get('reason', '') else '按标签整理'] += 1
    _log(log, '扫描完成：待移动 %d（标签 %d / 文件名 %d）· 洗版 %d · 重复 %d · 冗余 %d · 附属 %d'
         % (counts['move'], move_rules['按标签整理'], move_rules['按文件名'], counts['wash'],
            counts['duplicate'], counts['junk'] + counts['error'], counts['sidecar']))
    return plan


def _unique_in_plan(media: Path, target: str, taken: set) -> str:
    """计划阶段的重名避让：既避开磁盘上已有文件，也避开本计划已占用的目标。"""
    candidate = target
    stem = Path(target).stem
    suffix = Path(target).suffix
    parent = Path(target).parent
    counter = 1
    while True:
        key = candidate.lower()
        exists = key in taken or (media / candidate).exists()
        if not exists:
            return candidate
        counter += 1
        candidate = (parent / ('%s (%d)%s' % (stem, counter, suffix))).as_posix()

# ---------------------------------------------------------------- 执行

def pending_items(plan: dict):
    return [item for item in (plan.get('items') or [])
            if item.get('status') == 'pending' and item.get('kind') in ('move', 'wash', 'duplicate', 'junk')]


def preflight(plan: dict):
    """执行前的整体复核：只检查待处理项，返回问题列表。"""
    media = _media_root()
    problems = []
    for item in pending_items(plan):
        path = media / item['src']
        try:
            stat = path.stat()
        except OSError:
            problems.append({'ord': item['ord'], 'name': item['name'], 'reason': '文件已不存在'})
            continue
        try:
            fingerprint = file_fingerprint(path, stat.st_size)
        except OSError:
            problems.append({'ord': item['ord'], 'name': item['name'], 'reason': '文件无法读取'})
            continue
        if stat.st_size != item['size'] or (item.get('fp') and fingerprint != item['fp']):
            problems.append({'ord': item['ord'], 'name': item['name'], 'reason': '文件已变化'})
    return problems


def _next_free(media: Path, target: str) -> str:
    candidate = target
    stem, suffix, parent = Path(target).stem, Path(target).suffix, Path(target).parent
    counter = 1
    while (media / candidate).exists():
        counter += 1
        candidate = (parent / ('%s (%d)%s' % (stem, counter, suffix))).as_posix()
    return candidate


def _move_file(media: Path, src_rel: str, target_rel: str):
    """移动单个文件（跨设备自动降级为复制 + 校验 + 删源）；返回实际目标。"""
    src = (media / src_rel).resolve()
    if not src.is_file():
        raise OSError('源文件不存在')
    target_rel = _next_free(media, target_rel)
    target = (media / target_rel).resolve()
    if not str(target).startswith(str(media)):
        raise HTTPException(400, '目标路径越界')
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(src, target)
    except OSError as exc:
        if getattr(exc, 'errno', None) != 18:  # EXDEV
            raise
        shutil.copy2(src, target)
        if target.stat().st_size != src.stat().st_size:
            target.unlink(missing_ok=True)
            raise OSError('复制校验失败')
        src.unlink()
    return target_rel


def apply_item(plan_id: str, ordinal: int, log=None) -> dict:
    """执行计划中的一个条目（含它的附属文件）。由任务中心的每个曲目项调用。"""
    media = _media_root()
    with _LOCK:
        plan = load_plan(plan_id)
        items = plan.get('items') or []
        item = next((row for row in items if row.get('ord') == ordinal), None)
        if item is None:
            raise HTTPException(400, '计划里没有这个条目')
        if item.get('status') != 'pending':
            return {'skipped': True, 'status': item.get('status'), 'name': item.get('name')}
        if item.get('kind') not in ('move', 'wash', 'duplicate', 'junk'):
            item['status'] = 'skipped'
            item['at'] = time.time()
            save_plan(plan)
            return {'skipped': True, 'status': 'skipped', 'name': item.get('name')}
        if plan.get('status') == 'ready':
            plan['status'] = 'applying'
        src = media / item['src']
        try:
            stat = src.stat()
        except OSError:
            return _fail(plan, item, '文件已不存在（可能在扫描后被移走）')
        if stat.st_size != item['size']:
            return _fail(plan, item, '文件已变化，请重新扫描')
        if item.get('fp'):
            try:
                if file_fingerprint(src, stat.st_size) != item['fp']:
                    return _fail(plan, item, '文件已变化，请重新扫描')
            except OSError:
                return _fail(plan, item, '文件无法读取，请重新扫描')
        if item['kind'] == 'wash':
            old_rel = item.get('wash_of') or ''
            old_path = (media / old_rel) if old_rel else None
            if old_path is not None and old_path.is_file():
                try:
                    item['wash_old_actual'] = _move_file(media, old_rel, item.get('wash_to') or '')
                except Exception as exc:
                    return _fail(plan, item, '洗版：旧版本移动失败：%s' % clean_error(exc))
            else:
                item['wash_old_actual'] = ''
        try:
            actual = _move_file(media, item['src'], item['target'])
        except Exception as exc:
            return _fail(plan, item, '移动失败：%s' % clean_error(exc))
        item['status'] = 'done'
        item['actual'] = actual
        item['at'] = time.time()
        plan['applied'][item['kind']] = plan['applied'].get(item['kind'], 0) + 1
        side_done = 0
        side_failed = 0
        for side in item.get('sidecars') or []:
            try:
                side_path = media / side['src']
                side_stat = side_path.stat()
                if side_stat.st_size != side.get('size', 0) or (side.get('fp') and
                                                                file_fingerprint(side_path, side_stat.st_size) != side['fp']):
                    raise OSError('附属文件已变化')
                side['actual'] = _move_file(media, side['src'], side['target'])
                side['status'] = 'done'
                side_done += 1
            except Exception as exc:
                side['status'] = 'failed'
                side['error'] = clean_error(exc)
                side_failed += 1
        if item['kind'] in ('move', 'wash'):
            digest = item.get('sha256') or ''
            if not digest:
                try:
                    digest = sha256_file(media / actual)
                except OSError:
                    digest = ''
            index = load_index()
            library_prefix = plan['cfg']['library'] + '/'
            changed = False
            old_rel = item.get('wash_of') if item['kind'] == 'wash' else ''
            if old_rel and old_rel in (index.get('files') or {}):
                index['files'].pop(old_rel, None)          # 被替换的旧版本不再算库内条目
                changed = True
            if digest and actual.startswith(library_prefix):
                try:
                    stat2 = (media / actual).stat()
                    index_add(index, actual, stat2.st_size, stat2.st_mtime_ns, digest,
                              meta={'title': item.get('title'), 'artist': item.get('artist'),
                                    'album': item.get('album')})
                    changed = True
                except OSError:
                    pass
            if changed:
                save_index(index)
        remaining = pending_items(plan)
        if not remaining:
            plan['status'] = 'done'
            plan['finished'] = time.time()
        save_plan(plan)
    _log(log, '%s → %s%s' % (item['name'], actual, ('（附属 %d）' % side_done) if side_done else ''))
    return {'ord': ordinal, 'kind': item['kind'], 'name': item['name'], 'target': actual,
            'sidecars': side_done, 'sidecar_failed': side_failed, 'status': 'done',
            'wash_old': item.get('wash_old_actual', '') if item['kind'] == 'wash' else ''}


def _fail(plan: dict, item: dict, reason: str):
    item['status'] = 'failed'
    item['error'] = reason
    item['at'] = time.time()
    plan['applied']['failed'] = plan['applied'].get('failed', 0) + 1
    save_plan(plan)
    raise HTTPException(400, '整理失败：%s（%s）' % (item['name'], reason))

# ---------------------------------------------------------------- 任务入口

def run_scan(item, options, store=None, job_id=None):
    """任务中心：organize_scan —— 一个条目就是整次扫描。"""
    cfg = options.get('cfg') or default_settings()
    plan_id = options.get('plan_id') or new_plan_id()
    log = _mklog(store, job_id)
    try:
        plan = build_plan(cfg, plan_id, log=log, cancel=(lambda: _cancelled(store, job_id)))
    except HTTPException as exc:
        if exc.status_code == 499:
            mark_terminal(plan_id, 'cancelled', '已请求停止')
            _log(log, '扫描已停止；未生成计划，文件未发生变化。', 'warning')
            return {'plan_id': plan_id, 'cancelled': True}
        mark_terminal(plan_id, 'failed', clean_error(exc.detail))
        raise
    except Exception as exc:
        mark_terminal(plan_id, 'failed', clean_error(exc))
        raise
    return {'plan_id': plan['plan_id'], 'counts': plan['counts'], 'status': plan['status']}


def run_apply_item(item, options, store=None, job_id=None):
    """任务中心：organize_apply —— 一个条目 = 一个文件（含附属）。"""
    plan_id = options.get('plan_id')
    if not plan_id:
        raise HTTPException(400, '整理任务缺少计划编号')
    ordinal = item.get('ord')
    if ordinal is None:
        raise HTTPException(400, '整理任务缺少条目序号')
    log = _mklog(store, job_id)
    return apply_item(plan_id, int(ordinal), log=log)
