"""本地自动整理模块的离线测试：在临时目录里造样本，绝不接触真实曲库。"""
import json
import math
import os
import struct
import time
import wave
from pathlib import Path

import pytest
from fastapi import HTTPException
from mutagen.id3 import TALB, TCON, TDRC, TIT2, TPE1, TRCK
from mutagen.wave import WAVE

import mtl_organize as org


# ---------------------------------------------------------------- 工具

def make_wav(path: Path, freq=440, seconds=0.12, rate=8000, tags=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        frames = b''.join(struct.pack('<h', int(3000 * math.sin(2 * math.pi * freq * i / rate)))
                          for i in range(int(rate * seconds)))
        handle.writeframes(frames)
    if tags:
        audio = WAVE(str(path))
        try:
            audio.add_tags()
        except Exception:
            pass
        if tags.get('title'):
            audio.tags.add(TIT2(encoding=3, text=[tags['title']]))
        if tags.get('artist'):
            audio.tags.add(TPE1(encoding=3, text=[tags['artist']]))
        if tags.get('album'):
            audio.tags.add(TALB(encoding=3, text=[tags['album']]))
        if tags.get('track'):
            audio.tags.add(TRCK(encoding=3, text=[str(tags['track'])]))
        if tags.get('genre'):
            audio.tags.add(TCON(encoding=3, text=[tags['genre']]))
        if tags.get('date'):
            audio.tags.add(TDRC(encoding=3, text=[tags['date']]))
        audio.save()
    return path


@pytest.fixture()
def tree(tmp_path, monkeypatch):
    media = tmp_path / 'media'
    source = media / 'music' / '未整理' / '待整理'
    library = media / 'music' / '已整理'
    data = tmp_path / 'data'
    for path in (source, library, data):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv('MEDIA_DIR', str(media))
    monkeypatch.setenv('DATA_DIR', str(data))
    return {'media': media, 'source': source, 'library': library, 'data': data}


def cfg_of(**overrides):
    node = org.default_settings()
    node.update(overrides)
    return org.validate_settings(org._media_root(), node)


def item_named(plan, name):
    return next(row for row in plan['items'] if row['name'] == name)


def apply_plan(plan, only=None):
    results = []
    for item in list(plan['items']):
        if item['status'] != 'pending' or item['kind'] not in ('move', 'wash', 'duplicate', 'junk'):
            continue
        if only and item['name'] not in only:
            continue
        results.append(org.apply_item(plan['plan_id'], item['ord']))
    return results


# ---------------------------------------------------------------- 分类

def test_move_by_tags_and_apply(tree):
    make_wav(tree['source'] / 'raw.wav', tags={'title': 'Hello', 'artist': 'Artist X',
                                               'album': 'Album Y', 'track': 3})
    plan = org.build_plan(cfg_of())
    item = item_named(plan, 'raw.wav')
    assert item['kind'] == 'move'
    assert item['target'] == 'music/已整理/Artist X/Album Y/Hello.wav'   # 不带音轨号前缀
    assert item['reason'] == '按标签整理'
    apply_plan(plan)
    assert (tree['library'] / 'Artist X' / 'Album Y' / 'Hello.wav').is_file()
    assert not (tree['source'] / 'raw.wav').exists()
    saved = json.loads((tree['data'] / 'organize' / 'plans' / (plan['plan_id'] + '.json')).read_text('utf-8'))
    assert saved['status'] == 'done'
    assert saved['applied']['move'] == 1
    index = json.loads((tree['data'] / 'organize' / 'index.json').read_text('utf-8'))
    assert 'music/已整理/Artist X/Album Y/Hello.wav' in index['files']


def test_track_prefix_shortcut_drops_cleanly_without_track(tree):
    """勾选「带音轨号」时用的是可选块：没有音轨号的文件名不会留下孤立的「. 」。"""
    make_wav(tree['source'] / 'a.wav', freq=400, tags={'title': 'With Track', 'artist': 'A', 'album': 'B', 'track': 7})
    make_wav(tree['source'] / 'b.wav', freq=500, tags={'title': 'No Track', 'artist': 'A', 'album': 'B'})
    plan = org.build_plan(cfg_of(track_prefix=True))
    assert item_named(plan, 'a.wav')['target'] == 'music/已整理/A/B/07. With Track.wav'
    assert item_named(plan, 'b.wav')['target'] == 'music/已整理/A/B/No Track.wav'


def test_track_prefix_option(tree):
    make_wav(tree['source'] / 'raw.wav', tags={'title': 'Hello', 'artist': 'A', 'album': 'B', 'track': 3})
    plan = org.build_plan(cfg_of(track_prefix=True, track_sep='. '))
    assert item_named(plan, 'raw.wav')['target'] == 'music/已整理/A/B/03. Hello.wav'


def test_filename_fallback_when_no_tags(tree):
    make_wav(tree['source'] / 'Song Name - Artist Name - master - Album Name.wav')
    plan = org.build_plan(cfg_of())
    item = item_named(plan, 'Song Name - Artist Name - master - Album Name.wav')
    assert item['kind'] == 'move'
    assert item['target'] == 'music/已整理/Artist Name/Album Name/Song Name.wav'
    assert '文件名' in item['reason']


def test_duplicate_against_library(tree):
    make_wav(tree['library'] / 'A' / 'B' / 'song.wav', freq=500)
    make_wav(tree['source'] / 'copy.wav', freq=500)   # 内容完全相同（同频率同长度，均无标签）
    org.save_index({'version': 1, 'files': {}})
    plan = org.build_plan(cfg_of())
    item = item_named(plan, 'copy.wav')
    assert item['kind'] == 'duplicate'
    assert '已整理库' in item['reason']
    assert item['target'] == 'music/未整理/已存在/copy.wav'
    apply_plan(plan)
    assert (tree['media'] / 'music' / '未整理' / '已存在' / 'copy.wav').is_file()
    assert (tree['library'] / 'A' / 'B' / 'song.wav').is_file()


def test_duplicate_within_batch(tree):
    content = {'freq': 700}
    make_wav(tree['source'] / 'one.wav', **content)
    make_wav(tree['source'] / 'two.wav', **content)
    plan = org.build_plan(cfg_of())
    first = item_named(plan, 'one.wav')
    second = item_named(plan, 'two.wav')
    kinds = sorted([first['kind'], second['kind']])
    assert kinds == ['duplicate', 'move']
    assert '本批' in second['reason'] or '本批' in first['reason']


def test_same_size_different_content_is_not_duplicate(tree):
    make_wav(tree['source'] / 'a.wav', freq=400)
    make_wav(tree['source'] / 'b.wav', freq=900)   # 同长度同采样率 → 同体积、不同内容
    assert (tree['source'] / 'a.wav').stat().st_size == (tree['source'] / 'b.wav').stat().st_size
    plan = org.build_plan(cfg_of())
    assert {item_named(plan, 'a.wav')['kind'], item_named(plan, 'b.wav')['kind']} == {'move'}


def test_sidecar_follows_song(tree):
    make_wav(tree['source'] / 'track.wav', tags={'title': 'Hello', 'artist': 'A', 'album': 'B'})
    (tree['source'] / 'track.lrc').write_text('[00:01]hi\n', encoding='utf-8')
    (tree['source'] / 'track.jpg').write_bytes(b'\xff\xd8\xff\xdbtest')
    plan = org.build_plan(cfg_of())
    lrc = item_named(plan, 'track.lrc')
    jpg = item_named(plan, 'track.jpg')
    assert lrc['kind'] == 'sidecar' and lrc['target'] == 'music/已整理/A/B/Hello.lrc'
    assert jpg['kind'] == 'sidecar' and jpg['target'] == 'music/已整理/A/B/Hello.jpg'
    assert plan['counts']['sidecar'] == 2
    apply_plan(plan)
    assert (tree['library'] / 'A' / 'B' / 'Hello.lrc').is_file()
    assert (tree['library'] / 'A' / 'B' / 'Hello.jpg').is_file()
    assert not list(tree['source'].iterdir())


def test_orphans_and_junk_go_to_redundancy(tree):
    make_wav(tree['source'] / 'song.wav', tags={'title': 'T', 'artist': 'A', 'album': 'B'})
    (tree['source'] / 'notes.txt').write_text('hello', encoding='utf-8')
    (tree['source'] / 'lonely.lrc').write_text('[00:01]x', encoding='utf-8')
    (tree['source'] / 'cover.jpg').write_bytes(b'\xff\xd8')
    (tree['source'] / '.DS_Store').write_bytes(b'junk')
    plan = org.build_plan(cfg_of())
    for name, reason in [('notes.txt', '非音乐'), ('lonely.lrc', '找不到'), ('cover.jpg', '找不到'), ('.DS_Store', '隐藏')]:
        item = item_named(plan, name)
        assert item['kind'] == 'junk', name
        assert reason in item['reason'], name
        assert item['target'].startswith('music/未整理/冗余数据/')
    apply_plan(plan)
    assert (tree['media'] / 'music' / '未整理' / '冗余数据' / 'notes.txt').is_file()
    assert (tree['media'] / 'music' / '未整理' / '冗余数据' / '.DS_Store').is_file()


def test_unreadable_audio_is_junk(tree):
    (tree['source'] / 'broken.flac').write_bytes(b'this is not audio at all')
    (tree['source'] / 'mystery.bin').write_bytes(b'\x00\x01')
    plan = org.build_plan(cfg_of())
    assert item_named(plan, 'broken.flac')['kind'] == 'junk'
    assert '无法读取' in item_named(plan, 'broken.flac')['reason']
    assert item_named(plan, 'mystery.bin')['reason'] == '非音乐文件'


def test_hidden_and_junk_dirs_are_skipped(tree):
    make_wav(tree['source'] / '@eaDir' / 'skip.wav', tags={'title': 'Skip'})
    make_wav(tree['source'] / '.AppleDouble' / 'skip2.wav', tags={'title': 'Skip2'})
    make_wav(tree['source'] / 'keep.wav', tags={'title': 'Keep', 'artist': 'A', 'album': 'B'})
    plan = org.build_plan(cfg_of())
    assert [row['name'] for row in plan['items']] == ['keep.wav']


def test_no_overwrite_uses_suffix(tree):
    make_wav(tree['library'] / 'A' / 'B' / 'Hello.wav', freq=300)
    make_wav(tree['source'] / 'raw.wav', freq=600, tags={'title': 'Hello', 'artist': 'A', 'album': 'B'})
    plan = org.build_plan(cfg_of())
    assert item_named(plan, 'raw.wav')['target'] == 'music/已整理/A/B/Hello (2).wav'
    apply_plan(plan)
    assert (tree['library'] / 'A' / 'B' / 'Hello.wav').stat().st_size != (tree['library'] / 'A' / 'B' / 'Hello (2).wav').stat().st_size
    assert (tree['library'] / 'A' / 'B' / 'Hello (2).wav').is_file()


def test_preflight_and_changed_file(tmp_path, tree):
    make_wav(tree['source'] / 'a.wav', freq=400)
    plan = org.build_plan(cfg_of())
    assert org.preflight(plan) == []
    make_wav(tree['source'] / 'a.wav', freq=500)  # 覆盖成不同内容（大小不变）
    problems = org.preflight(plan)
    assert problems and problems[0]['reason'] == '文件已变化'
    with pytest.raises(HTTPException):
        org.apply_item(plan['plan_id'], item_named(plan, 'a.wav')['ord'])
    saved = json.loads((tree['data'] / 'organize' / 'plans' / (plan['plan_id'] + '.json')).read_text('utf-8'))
    assert item_named(saved, 'a.wav')['status'] == 'failed'
    assert item_named(saved, 'a.wav')['error'].startswith('文件已变化')


def test_same_size_same_mtime_edit_is_caught(tree):
    """体积和时间戳都没变、内容变了 → 必须靠指纹拦下（这是最危险的一类改动）。"""
    make_wav(tree['source'] / 'a.wav', freq=400)
    plan = org.build_plan(cfg_of())
    before = (tree['source'] / 'a.wav').stat()
    make_wav(tree['source'] / 'a.wav', freq=500)          # 同体积、不同内容
    os.utime(tree['source'] / 'a.wav', ns=(before.st_atime_ns, before.st_mtime_ns))
    after = (tree['source'] / 'a.wav').stat()
    assert after.st_size == before.st_size and after.st_mtime_ns == before.st_mtime_ns
    problems = org.preflight(plan)
    assert problems and problems[0]['reason'] == '文件已变化'
    with pytest.raises(HTTPException):
        org.apply_item(plan['plan_id'], item_named(plan, 'a.wav')['ord'])


def test_touch_does_not_block_apply(tree):
    """只是被读取/触碰（mtime 变化但内容不变）→ 不应拦，也不应打扰用户。"""
    make_wav(tree['source'] / 'a.wav', tags={'title': 'Hello', 'artist': 'A', 'album': 'B'})
    plan = org.build_plan(cfg_of())
    later = time.time() + 120
    os.utime(tree['source'] / 'a.wav', (later, later))
    assert org.preflight(plan) == []
    apply_plan(plan)
    assert (tree['library'] / 'A' / 'B' / 'Hello.wav').is_file()


def test_index_reuse_after_move(tree):
    make_wav(tree['source'] / 'raw.wav', freq=650, tags={'title': 'Hello', 'artist': 'A', 'album': 'B'})
    payload = (tree['source'] / 'raw.wav').read_bytes()
    plan = org.build_plan(cfg_of())
    apply_plan(plan)
    (tree['source'] / 'again.wav').write_bytes(payload)   # 与已归档文件内容完全相同
    plan2 = org.build_plan(cfg_of())
    item = item_named(plan2, 'again.wav')
    assert item['kind'] == 'duplicate' and item['target'] == 'music/未整理/已存在/again.wav'


def test_cancel_mid_scan(tree):
    for index in range(6):
        make_wav(tree['source'] / ('f%d.wav' % index), freq=400 + index)
    calls = {'n': 0}

    def cancel():
        calls['n'] += 1
        return calls['n'] > 2

    with pytest.raises(HTTPException) as exc:
        org.build_plan(cfg_of(), log=None, cancel=cancel)
    assert exc.value.status_code == 499
    assert len(list(tree['source'].glob('*.wav'))) == 6


def test_run_scan_wrapper_returns_cancelled(tree, monkeypatch):
    make_wav(tree['source'] / 'a.wav', tags={'title': 'A'})
    monkeypatch.setattr(org, '_cancelled', lambda store, job_id: True)
    out = org.run_scan({'path': 'music/未整理/待整理'}, {'cfg': org.default_settings(), 'plan_id': org.new_plan_id()})
    assert out.get('cancelled') is True


def test_custom_placeholder_names(tree):
    make_wav(tree['source'] / 'm.wav', tags={'title': 'Mystery'})
    plan = org.build_plan(cfg_of(unknown_artist='佚名歌手', unknown_album='未收录'))
    assert item_named(plan, 'm.wav')['target'] == 'music/已整理/佚名歌手/未收录/Mystery.wav'


def test_custom_track_separator(tree):
    make_wav(tree['source'] / 't.wav', tags={'title': 'Hello', 'artist': 'A', 'album': 'B', 'track': 7})
    plan_move = org.build_plan(cfg_of(track_prefix=True, track_sep=' - '))
    assert item_named(plan_move, 't.wav')['target'] == 'music/已整理/A/B/07 - Hello.wav'
    plan_space = org.build_plan(cfg_of(track_prefix=True, track_sep=' '))
    assert item_named(plan_space, 't.wav')['target'] == 'music/已整理/A/B/07 Hello.wav'


def test_name_template_variants(tree):
    make_wav(tree['source'] / 'a.wav', tags={'title': 'Hello', 'artist': 'A', 'album': 'B', 'track': 3})
    plan = org.build_plan(cfg_of(name_template='{track:02d}. {title}'))
    assert item_named(plan, 'a.wav')['target'] == 'music/已整理/A/B/03. Hello.wav'
    plan2 = org.build_plan(cfg_of(name_template='<{artist} - >{title}'))
    assert item_named(plan2, 'a.wav')['target'] == 'music/已整理/A/B/A - Hello.wav'
    make_wav(tree['source'] / 'b.wav', tags={'title': 'Solo'})   # 没有歌手 → 可选块丢弃
    plan3 = org.build_plan(cfg_of(name_template='<{artist} - >{title}'))
    assert item_named(plan3, 'b.wav')['target'] == 'music/已整理/未知歌手/未知专辑/Solo.wav'
    plan4 = org.build_plan(cfg_of(dir_template='{artist_initial}/{artist}/{album}'))
    assert item_named(plan4, 'a.wav')['target'] == 'music/已整理/A/A/B/Hello.wav'
    plan5 = org.build_plan(cfg_of(dir_template='{year}/<{genre}/>{artist}'))
    assert item_named(plan5, 'a.wav')['target'] == 'music/已整理/A/Hello.wav'   # 空段自动丢弃


def test_index_records_tags_for_same_song(tree):
    make_wav(tree['library'] / 'X' / 'Y' / 'Tune.wav', tags={'title': 'Tune', 'artist': 'X', 'album': 'Y'})
    org.build_plan(cfg_of())
    index = json.loads((tree['data'] / 'organize' / 'index.json').read_text('utf-8'))
    row = index['files']['music/已整理/X/Y/Tune.wav']
    assert row['tags']['title'] == 'Tune' and row['tags']['artist'] == 'X'
    assert org.same_song_index(index)[('tune', 'x')]


def test_classify_rules(tree):
    make_wav(tree['source'] / 'j.wav', freq=400, tags={'title': 'Blue', 'artist': 'A', 'album': 'B', 'genre': 'Jazz', 'date': '1993-03-09'})
    make_wav(tree['source'] / 'p.wav', freq=500, tags={'title': 'Pop One', 'artist': 'C', 'album': 'D', 'genre': 'Pop'})
    rules = """
- name: 爵士
  match:
    genre: [jazz]
  path: "{genre}/{artist}/{album}"
- name: 九十年代
  match:
    year_min: 1990
    year_max: 1999
  path: "{decade}/{artist}/{album}"
"""
    make_wav(tree['source'] / 'r.wav', freq=600, tags={'title': 'Old Rock', 'artist': 'E', 'album': 'F',
                                                      'genre': 'Rock', 'date': '1995-01-01'})
    plan = org.build_plan(cfg_of(classify_rules=rules))
    jazz = item_named(plan, 'j.wav')
    assert jazz['kind'] == 'move' and jazz['rule'] == '爵士'
    assert jazz['target'] == 'music/已整理/Jazz/A/B/Blue.wav'
    # 没有年份 → 年代规则不命中，回落到默认目录模板，且不带 rule
    pop = item_named(plan, 'p.wav')
    assert pop.get('rule', '') == ''
    assert pop['target'] == 'music/已整理/C/D/Pop One.wav'
    # 有年份且 genre 不是 Jazz → 命中「九十年代」，{decade} 展开成 1990s
    rock = item_named(plan, 'r.wav')
    assert rock['kind'] == 'move' and rock['rule'] == '九十年代'
    assert rock['target'] == 'music/已整理/1990s/E/F/Old Rock.wav'


def test_wash_prefer_better(tree):
    make_wav(tree['library'] / 'A' / 'B' / 'Song.wav', rate=8000, tags={'title': 'Song', 'artist': 'A', 'album': 'B'})
    make_wav(tree['source'] / 'Song - A - B.wav', rate=44100, seconds=0.4,
             tags={'title': 'Song', 'artist': 'A', 'album': 'B'})
    plan = org.build_plan(cfg_of(wash_mode='prefer_better'))
    item = item_named(plan, 'Song - A - B.wav')
    assert item['kind'] == 'wash'
    assert item['wash_of'] == 'music/已整理/A/B/Song.wav'
    assert item['target'] == 'music/已整理/A/B/Song.wav'
    assert item['wash_to'].startswith('music/未整理/已存在/')
    apply_plan(plan)
    assert (tree['library'] / 'A' / 'B' / 'Song.wav').stat().st_size > 20000     # 换成了高音质那份
    kept = tree['media'] / 'music' / '未整理' / '已存在' / 'Song.wav'
    assert kept.is_file() and kept.stat().st_size < 20000
    index = json.loads((tree['data'] / 'organize' / 'index.json').read_text('utf-8'))
    assert 'music/已整理/A/B/Song.wav' in index['files']


def test_wash_skip_and_lower_quality(tree):
    make_wav(tree['library'] / 'A' / 'B' / 'Song.wav', rate=44100, seconds=0.4,
             tags={'title': 'Song', 'artist': 'A', 'album': 'B'})
    make_wav(tree['source'] / 'low.wav', rate=8000, tags={'title': 'Song', 'artist': 'A', 'album': 'B'})
    plan = org.build_plan(cfg_of(wash_mode='prefer_better'))
    item = item_named(plan, 'low.wav')
    assert item['kind'] == 'duplicate' and '保留原版' in item['reason']
    apply_plan(plan)
    assert (tree['media'] / 'music' / '未整理' / '已存在' / 'low.wav').is_file()
    assert (tree['library'] / 'A' / 'B' / 'Song.wav').stat().st_size > 20000

    make_wav(tree['source'] / 'again.wav', rate=22050, tags={'title': 'Song', 'artist': 'A', 'album': 'B'})
    plan2 = org.build_plan(cfg_of(wash_mode='skip'))
    item2 = item_named(plan2, 'again.wav')
    assert item2['kind'] == 'duplicate' and '洗版跳过' in item2['reason']


def test_wash_size_limit_keeps_both(tree):
    """超过体积上限时不做洗版：新文件正常入库（重名加后缀），旧版留在原位。"""
    make_wav(tree['library'] / 'A' / 'B' / 'Song.wav', rate=8000, tags={'title': 'Song', 'artist': 'A', 'album': 'B'})
    make_wav(tree['source'] / 'Song.wav', rate=44100, seconds=13, tags={'title': 'Song', 'artist': 'A', 'album': 'B'})
    assert (tree['source'] / 'Song.wav').stat().st_size > 1024 * 1024      # 确实超过 1 MB 上限
    plan = org.build_plan(cfg_of(wash_mode='always_replace', wash_max_mb=1))
    item = item_named(plan, 'Song.wav')
    assert item['kind'] == 'move' and '体积上限' in item['reason']
    assert item['target'].startswith('music/已整理/A/B/') and item['target'] != 'music/已整理/A/B/Song.wav'
    apply_plan(plan)
    assert (tree['library'] / 'A' / 'B' / 'Song.wav').stat().st_size < 20000   # 旧版还在
    assert (tree['media'] / item['target']).stat().st_size > 1024 * 1024       # 新版也进库了
    assert not list((tree['media'] / 'music' / '未整理' / '已存在').glob('*.wav'))        # 没有任何文件被移出


def test_wash_without_limit_replaces_same_song(tree):
    """同一个文件在上限为 0（不限制）时就会走洗版替换。"""
    make_wav(tree['library'] / 'A' / 'B' / 'Song.wav', rate=8000, tags={'title': 'Song', 'artist': 'A', 'album': 'B'})
    make_wav(tree['source'] / 'Song.wav', rate=44100, seconds=13, tags={'title': 'Song', 'artist': 'A', 'album': 'B'})
    plan = org.build_plan(cfg_of(wash_mode='always_replace', wash_max_mb=0))
    assert item_named(plan, 'Song.wav')['kind'] == 'wash'
    results = apply_plan(plan)
    assert results and results[0].get('wash_old', '').startswith('music/未整理/已存在/')
    assert (tree['library'] / 'A' / 'B' / 'Song.wav').stat().st_size > 1024 * 1024
    old = tree['media'] / 'music' / '未整理' / '已存在' / 'Song.wav'
    assert old.is_file() and old.stat().st_size < 20000


def test_wash_rule_validation(tree):
    with pytest.raises(HTTPException):
        org.validate_settings(org._media_root(), {'wash_mode': 'unknown'})
    with pytest.raises(HTTPException):
        org.validate_settings(org._media_root(), {'wash_max_mb': 'big'})
    with pytest.raises(HTTPException):
        org.validate_settings(org._media_root(), {'dir_template': ''})
    with pytest.raises(HTTPException):
        org.parse_classify_rules('- name: x\n  match: {}\n')
    with pytest.raises(HTTPException):
        org.parse_classify_rules('- name: x\n  path: ""\n')
    assert org.parse_classify_rules('') == []
    rules = org.parse_classify_rules('- name: 华语\n  match:\n    genre: [mandopop]\n  path: "华语/{album}"\n')
    assert rules[0]['name'] == '华语' and rules[0]['path'] == '华语/{album}'
    assert org.rule_matches(rules[0], {'genre': 'Mandopop', 'year': '', 'decade': ''})
    assert not org.rule_matches(rules[0], {'genre': 'Jazz', 'year': '', 'decade': ''})


def test_template_preview_helper(tree):
    cfg = org.validate_settings(org._media_root(), cfg_of(name_template='{track:02d}. {title}',
                                                         dir_template='{genre}/<{year}/>{artist}'))
    tokens, filled = org.meta_tokens(cfg, org.SAMPLE_TAGS)
    assert org.render_template(org.name_template_of(cfg), tokens, filled) == '03. Shape of My Heart'
    assert org.render_template(cfg['dir_template'], tokens, filled) == 'Pop/1993/Sting'
    empty_raw, empty_filled = org.meta_tokens(cfg, org.SAMPLE_TAGS_EMPTY)
    rendered = org.render_template('{genre}/<{year}/>{artist}', empty_raw, empty_filled)
    assert [p for p in rendered.split('/') if p] == ['未知歌手']   # 空段在拼路径时被丢弃
    assert org.render_template('<{artist} - >{title}', empty_raw, empty_filled) == '未命名作品'
    assert org.render_template('<{artist} - >{title}', tokens, filled) == 'Sting - Shape of My Heart'


def test_classify_by_artist_initial(tree):
    rules = ("- name: 字母 N-Z\n  match:\n    artist_initial: [n, o, p, q, r, s, t, u, v, w, x, y, z]\n"
             "  path: \"{artist_initial}/{artist}/{album}\"\n"
             "- name: 其它\n  match: {}\n  path: \"其它/{artist}/{album}\"\n")
    make_wav(tree['source'] / 'a.wav', freq=400, tags={'title': 'Tune', 'artist': 'Sting', 'album': 'X'})
    make_wav(tree['source'] / 'b.wav', freq=500, tags={'title': 'Tune2', 'artist': '周杰伦', 'album': 'Y'})
    plan = org.build_plan(cfg_of(classify_rules=rules))
    assert item_named(plan, 'a.wav')['target'] == 'music/已整理/S/Sting/X/Tune.wav'
    assert item_named(plan, 'a.wav')['rule'] == '字母 N-Z'
    assert item_named(plan, 'b.wav')['target'] == 'music/已整理/其它/周杰伦/Y/Tune2.wav'


def test_name_template_and_dir_template(tree):
    make_wav(tree['source'] / 'a.wav', freq=400,
             tags={'title': 'Hello', 'artist': 'A', 'album': 'B', 'track': 3})
    plan = org.build_plan(cfg_of(name_template='<{track:02d}. >{title}', dir_template='{artist}/{album}'))
    assert item_named(plan, 'a.wav')['target'] == 'music/已整理/A/B/03. Hello.wav'
    plan2 = org.build_plan(cfg_of(name_template='<{track:02d}. >{title}', dir_template='{artist} - {album}/{year}'))
    item = item_named(plan2, 'a.wav')
    assert item['target'].startswith('music/已整理/A - B/')     # 目录模板可自定义
    plan3 = org.build_plan(cfg_of(name_template='<{artist} - >{title}'))
    assert item_named(plan3, 'a.wav')['target'] == 'music/已整理/A/B/A - Hello.wav'


def test_empty_token_does_not_leave_separator(tree):
    """没有音轨号 / 年份时不能留下孤立的「. 」「 - 」——CMS 变量说明里最容易踩的坑。"""
    make_wav(tree['source'] / 'a.wav', freq=400, tags={'title': 'No Track', 'artist': 'A', 'album': 'B'})
    make_wav(tree['source'] / 'c.wav', freq=600, tags={'title': 'Only Title'})
    p1 = org.build_plan(cfg_of(name_template='{track:02d}. {title}'))
    assert item_named(p1, 'a.wav')['target'] == 'music/已整理/A/B/No Track.wav'
    p2 = org.build_plan(cfg_of(name_template='{title} - {year}'))
    assert item_named(p2, 'a.wav')['target'] == 'music/已整理/A/B/No Track.wav'
    p3 = org.build_plan(cfg_of(name_template='{year} - {title}'))
    assert item_named(p3, 'a.wav')['target'] == 'music/已整理/A/B/No Track.wav'
    p4 = org.build_plan(cfg_of(name_template='{track:02d}. {title}', dir_template='{artist} - {album}'))
    assert item_named(p4, 'c.wav')['target'] == 'music/已整理/未知歌手 - 未知专辑/Only Title.wav'


def test_settings_validation(tree):
    media = org._media_root()
    with pytest.raises(HTTPException):
        org.validate_settings(media, {'source': 'music/未整理/待整理', 'library': 'music/未整理/待整理'})
    with pytest.raises(HTTPException):
        org.validate_settings(media, {'library': 'music/未整理'})
    with pytest.raises(HTTPException):
        org.validate_settings(media, {'junk': '..'})
    with pytest.raises(HTTPException):
        org.validate_settings(media, {'source': 'D:/music'})
    with pytest.raises(HTTPException):
        org.validate_settings(media, {'source': 'C:\\Music'})
    with pytest.raises(HTTPException):
        org.validate_settings(media, {'track_sep': '__'})
    ok = org.validate_settings(media, {'source': '/未整理/待整理/', 'track_prefix': True})
    assert ok['source'] == '未整理/待整理' and ok['track_prefix'] is True      # 显式指定则按指定的来
    assert ok['abs']['source'].endswith('未整理/待整理')
    with pytest.raises(HTTPException) as conflict:
        org.validate_settings(media, {'duplicates': 'music/已整理/重复'})       # 在归档目录里面 → 不允许
    assert '已整理归档目录' in str(conflict.value.detail) and '已存在目录' in str(conflict.value.detail)
    host = org.validate_settings(media, {})
    assert host['host_root'] == '/vol1/1000/1'
    assert org.host_path(host, host['source']) == '/vol1/1000/1/music/未整理/待整理'
    assert org.host_path(org.validate_settings(media, {'host_root': ''}), 'music/已整理') == ''


def test_sanitize_segment_rules():
    assert org.sanitize_segment('A/B:C*D?E"F<G>H|I') == 'A／B：C＊D？E＂F＜G＞H｜I'
    assert org.sanitize_segment('   spaced   name  ') == 'spaced name'
    assert org.sanitize_segment('trailing. ') == 'trailing'
    assert org.sanitize_segment('.DS_Store') == '.DS_Store'
    assert org.sanitize_segment('x' * 300).__len__() == org.SEGMENT_LIMIT
    assert org.sanitize_segment('', 'fallback') == 'fallback'


def test_multi_artist_and_unknown_fallbacks(tree):
    make_wav(tree['source'] / 'm.wav', tags={'title': 'Sunflower', 'artist': 'Post Malone、Swae Lee'})
    make_wav(tree['source'] / 'u.wav', tags={'title': 'Mystery'})
    plan = org.build_plan(cfg_of())
    assert item_named(plan, 'm.wav')['target'] == 'music/已整理/Post Malone、Swae Lee/未知专辑/Sunflower.wav'
    assert item_named(plan, 'u.wav')['target'] == 'music/已整理/未知歌手/未知专辑/Mystery.wav'


def test_plan_listing_and_counts(tree):
    make_wav(tree['source'] / 'a.wav', tags={'title': 'A', 'artist': 'X', 'album': 'Y'})
    make_wav(tree['source'] / 'b.mp3', freq=333)
    plan = org.build_plan(cfg_of())
    assert plan['counts']['total'] == 2
    rows = org.list_plans()
    assert rows and rows[0]['plan_id'] == plan['plan_id']
    assert rows[0]['counts']['total'] == 2
    assert org.load_plan(plan['plan_id'])['items'][0]['ord'] == 0
    with pytest.raises(HTTPException):
        org.load_plan('../../etc/passwd')
