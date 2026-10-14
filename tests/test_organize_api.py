"""自动整理 HTTP 接口测试：用假 Store/Engine 验证端点行为与任务编排。"""
import math
import struct
import time
import wave
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mutagen.id3 import TALB, TCON, TDRC, TIT2, TPE1, TRCK
from mutagen.wave import WAVE

import mtl_organize as org
import mtl_organize_api as api


def make_wav(path: Path, freq=440, seconds=0.12, rate=8000, tags=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b''.join(struct.pack('<h', int(3000 * math.sin(2 * math.pi * freq * i / rate)))
                                    for i in range(int(rate * seconds))))
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


class FakeStore:
    def __init__(self):
        self.rows = []
        self.logs = []

    def create_job(self, kind, items, options):
        job_id = 'job-%d' % (len(self.rows) + 1)
        self.rows.append({'id': job_id, 'kind': kind, 'status': 'pending', 'total': len(items),
                          'counts': {}, 'created': time.time(), 'options': options,
                          'items': [dict(item, status='pending') for item in items]})
        return job_id

    def jobs(self, limit=30):
        return list(reversed(self.rows))[:limit]

    def job(self, job_id):
        return next(row for row in self.rows if row['id'] == job_id)

    def cancelled(self, job_id):
        return False

    def log(self, job_id, message, level='info'):
        self.logs.append((job_id, message, level))


class FakeEngine:
    def __init__(self):
        self.calls = 0

    def notify(self):
        self.calls += 1


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    media = tmp_path / 'media'
    source = media / 'music' / '未整理' / '待整理'
    library = media / 'music' / '已整理'
    data = tmp_path / 'data'
    for path in (source, library, data):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv('MEDIA_DIR', str(media))
    monkeypatch.setenv('DATA_DIR', str(data))
    settings = {}
    store, engine = FakeStore(), FakeEngine()
    app = FastAPI()
    api.register(app, store, engine, settings)
    client = TestClient(app)
    return {'client': client, 'store': store, 'engine': engine, 'settings': settings,
            'media': media, 'source': source, 'library': library, 'data': data}


def finish(env, job_id, status='completed'):
    env['store'].job(job_id)['status'] = status


def run_scan_job(env, job_id):
    job = env['store'].job(job_id)
    out = org.run_scan({'path': job['options']['cfg']['source']}, job['options'])
    env['store'].log(job_id, 'done')
    finish(env, job_id)
    return out


def test_settings_endpoints(app_env):
    client = app_env['client']
    data = client.get('/api/organize/settings').json()
    assert data['settings']['source'] == 'music/未整理/待整理'
    assert data['paths']['source']['exists'] is True
    assert data['paths']['duplicates']['exists'] is False

    saved = client.post('/api/organize/settings', json={'track_prefix': False, 'follow_sidecars': True}).json()
    assert saved['ok'] is True
    assert set(saved['created']) == {'music/未整理/已存在', 'music/未整理/冗余数据'}
    assert (app_env['media'] / 'music' / '未整理' / '已存在').is_dir()
    assert app_env['settings']['organize']['library'] == 'music/已整理'

    bad = client.post('/api/organize/settings', json={'library': 'music/未整理'})
    assert bad.status_code == 400
    bad2 = client.post('/api/organize/settings', json={'junk': '../escape'})
    assert bad2.status_code == 400


def test_scan_apply_full_flow(app_env):
    client = app_env['client']
    client.post('/api/organize/settings', json={})
    make_wav(app_env['source'] / 'one.wav', freq=520, tags={'title': 'Hello', 'artist': 'Artist X', 'album': 'Album Y'})
    (app_env['source'] / 'one.lrc').write_text('[00:01]hi\n', encoding='utf-8')
    (app_env['source'] / 'notes.txt').write_text('junk', encoding='utf-8')

    created = client.post('/api/organize/scan').json()
    assert created['writes_music'] is False
    assert app_env['engine'].calls == 1
    job = app_env['store'].job(created['job_id'])
    assert job['kind'] == 'organize_scan'
    assert job['options']['plan_id'] == created['plan_id']

    out = run_scan_job(app_env, created['job_id'])
    assert out['counts']['move'] == 1 and out['counts']['sidecar'] == 1 and out['counts']['junk'] == 1

    plans = client.get('/api/organize/plans').json()['plans']
    assert plans and plans[0]['plan_id'] == created['plan_id']
    plan = client.get('/api/organize/plan/%s' % created['plan_id']).json()
    assert plan['status'] == 'ready'
    items = client.get('/api/organize/plan/%s?view=full' % created['plan_id']).json()['items']
    assert items and all('sha256' not in item and 'fp' not in item for item in items)

    blocked = client.post('/api/organize/apply', json={'plan_id': created['plan_id']})
    assert blocked.status_code == 400
    assert '确认' in blocked.json()['detail']

    applied = client.post('/api/organize/apply', json={'plan_id': created['plan_id'], 'confirmed': True}).json()
    assert applied['writes_music'] is True and applied['total'] == 2 and applied['remaining'] == 0
    apply_job = app_env['store'].job(applied['job_id'])
    assert apply_job['kind'] == 'organize_apply'
    for row in apply_job['items']:
        org.run_apply_item({'path': row['path'], 'ord': row['ord']}, apply_job['options'])
    finish(app_env, applied['job_id'])

    assert (app_env['library'] / 'Artist X' / 'Album Y' / 'Hello.wav').is_file()
    assert (app_env['library'] / 'Artist X' / 'Album Y' / 'Hello.lrc').is_file()
    assert (app_env['media'] / 'music' / '未整理' / '冗余数据' / 'notes.txt').is_file()
    assert not list(app_env['source'].iterdir())

    plan = client.get('/api/organize/plan/%s' % created['plan_id']).json()
    assert plan['status'] == 'done'
    assert plan['applied']['move'] == 1 and plan['applied']['junk'] == 1

    assert client.post('/api/organize/plan/%s/discard' % created['plan_id']).json()['ok'] is True
    assert client.get('/api/organize/plans').json()['plans'] == []


def test_status_endpoint_shape(app_env):
    client = app_env['client']
    make_wav(app_env['source'] / 'a.wav', tags={'title': 'A', 'artist': 'X', 'album': 'Y'})
    (app_env['source'] / 'b.lrc').write_text('x', encoding='utf-8')
    d = client.get('/api/organize/status').json()
    assert d['media_writable'] is True
    assert d['library_index']['files'] == 0
    row = d['paths']['source']
    assert row['rel'] == 'music/未整理/待整理' and row['exists'] is True and row['valid'] is True
    assert row['files'] == 2 and row['bytes'] > 0
    assert d['paths']['library']['files'] == 0
    assert d['settings']['library'] == 'music/已整理'
    assert d['running_plans'] == []
    bad = client.post('/api/organize/settings', json={'library': 'music/未整理'}).status_code
    assert bad == 400
    assert client.get('/api/organize/status').json()['settings']['library'] == 'music/已整理'


def test_progress_plan_and_stale_sweep(app_env):
    client = app_env['client']
    client.post('/api/organize/settings', json={})
    created = client.post('/api/organize/scan').json()
    plan = client.get('/api/organize/plan/%s' % created['plan_id']).json()
    assert plan['status'] == 'scanning' and plan['scanned'] == 0
    assert plan['scan_job'] == created['job_id']
    assert plan['counts']['total'] == 0
    # 扫描任务结束后没有出结果（例如被停止 / 重启）→ 计划收敛成 cancelled，而不是永远卡住
    finish(app_env, created['job_id'], 'cancelled')
    plan = client.get('/api/organize/plan/%s' % created['plan_id']).json()
    assert plan['status'] == 'cancelled' and '中断' in plan['note']
    blocked = client.post('/api/organize/apply', json={'plan_id': created['plan_id'], 'confirmed': True})
    assert blocked.status_code == 400


def test_plan_items_paging_and_kind_filter(app_env):
    client = app_env['client']
    client.post('/api/organize/settings', json={})
    for index in range(3):
        make_wav(app_env['source'] / ('m%d.wav' % index), freq=400 + index,
                 tags={'title': 'T%d' % index, 'artist': 'A', 'album': 'B'})
    (app_env['source'] / 'x.txt').write_text('junk', encoding='utf-8')
    created = client.post('/api/organize/scan').json()
    run_scan_job(app_env, created['job_id'])
    full = client.get('/api/organize/plan/%s?view=full&kind=move&limit=2' % created['plan_id']).json()
    assert full['total'] == 3 and len(full['items']) == 2
    page2 = client.get('/api/organize/plan/%s?view=full&kind=move&offset=2&limit=2' % created['plan_id']).json()
    assert len(page2['items']) == 1
    junk = client.get('/api/organize/plan/%s?view=full&kind=junk,error' % created['plan_id']).json()
    assert junk['total'] == 1 and junk['items'][0]['target'].startswith('music/未整理/冗余数据/')
    listed = client.get('/api/organize/plans').json()['plans']
    assert listed[0]['source'] == 'music/未整理/待整理'
    summary = client.get('/api/organize/plan/%s' % created['plan_id']).json()
    assert 'items' not in summary and summary['status'] == 'ready'


def test_browse_and_mkdir(app_env):
    client = app_env['client']
    client.post('/api/organize/settings', json={})
    root = client.get('/api/organize/browse').json()
    assert root['path'] == '' and root['parent'] is None
    assert root['dirs'] == ['music']          # 默认结构都在 music/ 下
    (app_env['media'] / 'music' / '未整理' / '待整理' / 'sub').mkdir(parents=True, exist_ok=True)
    (app_env['media'] / 'music' / '未整理' / 'a.txt').write_text('x', encoding='utf-8')
    level1 = client.get('/api/organize/browse', params={'path': 'music/未整理'}).json()
    assert level1['path'] == 'music/未整理' and level1['parent'] == 'music'
    assert '待整理' in level1['dirs'] and level1['files'] == 1
    assert level1['roles_below'] == ['source', 'duplicates', 'junk']   # 未整理 下面有这三个目录
    assert level1['roles_exact'] == []
    level2 = client.get('/api/organize/browse', params={'path': 'music/未整理/待整理'}).json()
    assert level2['parent'] == 'music/未整理' and level2['dirs'] == ['sub']
    assert level2['roles_exact'] == ['source']
    assert client.get('/api/organize/browse', params={'path': '../etc'}).status_code == 400
    assert client.get('/api/organize/browse', params={'path': 'nope'}).status_code == 404
    made = client.post('/api/organize/mkdir', json={'path': '', 'name': '未整理/新目录'}).json()
    assert made['created'] is True and made['path'] == '未整理／新目录'   # / 会转成全角，防止越界
    again = client.post('/api/organize/mkdir', json={'path': '', 'name': '未整理／新目录'}).json()
    assert again['created'] is False
    assert client.post('/api/organize/mkdir', json={'path': '', 'name': '   '}).status_code == 400
    assert (app_env['media'] / '未整理／新目录').is_dir()   # 单层目录名，没有真的建子层


def test_overlap_message_points_at_library(app_env):
    r = app_env['client'].post('/api/organize/settings', json={'source': 'music/已整理/待整理', 'library': 'music/已整理'})
    assert r.status_code == 400
    assert '已整理' in r.json()['detail'] and '之外' in r.json()['detail']


def test_apply_blocked_when_file_changed(app_env):
    client = app_env['client']
    client.post('/api/organize/settings', json={})
    make_wav(app_env['source'] / 'a.wav', freq=400, tags={'title': 'A', 'artist': 'X', 'album': 'Y'})
    created = client.post('/api/organize/scan').json()
    run_scan_job(app_env, created['job_id'])
    make_wav(app_env['source'] / 'a.wav', freq=500, tags={'title': 'A', 'artist': 'X', 'album': 'Y'})
    blocked = client.post('/api/organize/apply', json={'plan_id': created['plan_id'], 'confirmed': True})
    assert blocked.status_code == 409
    assert '重新扫描' in blocked.json()['detail']


def test_container_and_host_paths(app_env):
    """框里显示容器内路径，小字显示 NAS 真实路径（映射可自定义）。"""
    client = app_env['client']
    status = client.get('/api/organize/status').json()
    assert status['host_root'] == '/vol1/1000/1'
    row = status['paths']['source']
    assert row['rel'] == 'music/未整理/待整理'
    assert row['abs'].endswith('/music/未整理/待整理')            # 容器内
    assert row['host'] == '/vol1/1000/1/music/未整理/待整理'      # NAS 上
    assert status['paths']['library']['host'] == '/vol1/1000/1/music/已整理'

    saved = client.post('/api/organize/settings', json={'host_root': '/mnt/user/music/'}).json()['settings']
    assert saved['host_root'] == '/mnt/user/music'                  # 去掉尾斜杠
    assert client.get('/api/organize/status').json()['paths']['library']['host'] == '/mnt/user/music/music/已整理'
    assert client.post('/api/organize/settings', json={'host_root': 'vol1/1000/1'}).status_code == 400
    off = client.post('/api/organize/settings', json={'host_root': ''}).json()['settings']
    assert off['host_root'] == '' and client.get('/api/organize/status').json()['paths']['source']['host'] == ''

    preview = client.post('/api/organize/preview', json={'host_root': '/vol1/1000/1'}).json()
    assert preview['paths'][0]['path'] == "music/已整理/Sting/Ten Summoner's Tales/Shape of My Heart.flac"
    assert preview['paths'][0]['host'] == "/vol1/1000/1/music/已整理/Sting/Ten Summoner's Tales/Shape of My Heart.flac"


def test_preview_endpoint_renders_templates(app_env):
    """设置页「预览示例路径」用的接口：只渲染，不写盘。"""
    client = app_env['client']
    rules = '- name: 流行\n  match:\n    genre: [pop]\n  path: "流行/{artist}"\n'
    d = client.post('/api/organize/preview', json={'name_template': '{track:02d}. {title}',
                                                  'dir_template': '{genre}/{artist}/{album}',
                                                  'classify_rules': rules}).json()
    assert d['ok'] is True and d['rules'] == 1
    assert d['name_template'] == '{track:02d}. {title}'
    assert d['paths'][0]['rule'] == '流行'
    assert d['paths'][0]['path'] == 'music/已整理/流行/Sting/03. Shape of My Heart.flac'
    # 样本二没有任何标签：不命中规则，用占位名兜底
    assert d['paths'][1]['rule'] == '' and d['paths'][1]['path'].endswith('.flac')
    assert not list(app_env['source'].iterdir()) and not list(app_env['library'].iterdir())

    bad = client.post('/api/organize/preview', json={'classify_rules': '- name: x\n  match: {}\n'}).json()
    assert bad['ok'] is False and bad['error']
    worse = client.post('/api/organize/preview', json={'dir_template': ''})
    assert worse.status_code in (400, 422) or worse.json()['ok'] is False


def test_settings_roundtrip_naming_and_wash(app_env):
    client = app_env['client']
    body = {'name_template': '<{artist} - >{title}', 'dir_template': '{genre}/{artist}/{album}',
            'classify_rules': '- name: 华语\n  match:\n    genre: [mandopop]\n  path: "华语/{album}"\n',
            'wash_mode': 'prefer_better', 'wash_max_mb': 12}
    d = client.post('/api/organize/settings', json=body).json()
    assert d['settings']['wash_mode'] == 'prefer_better' and d['settings']['wash_max_mb'] == 12
    assert d['settings']['name_template'] == '<{artist} - >{title}'
    assert app_env['settings']['organize']['classify_rules'].startswith('- name: 华语')
    fetch = client.get('/api/organize/settings').json()['settings']
    assert fetch['dir_template'] == '{genre}/{artist}/{album}' and fetch['wash_max_mb'] == 12

    status = client.get('/api/organize/status').json()
    assert status['effective_name_template'] == '<{artist} - >{title}'
    assert status['wash_modes'] == ['coexist', 'prefer_better', 'always_replace', 'skip']
    assert status['kinds']['wash'] == '洗版替换'

    assert client.post('/api/organize/settings', json={'wash_mode': 'nope'}).status_code == 400
    # 非数字体积会被参数校验拦掉（400 或 422 都算拒绝），并且不会写坏配置
    assert client.post('/api/organize/settings', json={'wash_max_mb': 'big'}).status_code in (400, 422)
    assert client.post('/api/organize/settings', json={'classify_rules': '- name: x\n  match: {}\n'}).status_code == 400
    after = client.get('/api/organize/settings').json()['settings']
    assert after['wash_mode'] == 'prefer_better' and after['wash_max_mb'] == 12   # 400 不会写坏配置

    default = client.post('/api/organize/settings', json={'wash_mode': 'coexist', 'wash_max_mb': 0}).json()
    assert default['settings']['wash_max_mb'] == 0


def test_scan_apply_wash_via_api(app_env):
    client = app_env['client']
    client.post('/api/organize/settings', json={'wash_mode': 'always_replace'})
    make_wav(app_env['library'] / 'A' / 'B' / 'Song.wav', rate=8000,
             tags={'title': 'Song', 'artist': 'A', 'album': 'B'})
    make_wav(app_env['source'] / 'Song.wav', rate=44100, seconds=0.4,
             tags={'title': 'Song', 'artist': 'A', 'album': 'B'})
    created = client.post('/api/organize/scan').json()
    run_scan_job(app_env, created['job_id'])
    plan = client.get('/api/organize/plan/%s' % created['plan_id']).json()
    assert plan['counts']['wash'] == 1 and plan['counts']['move'] == 0
    full = client.get('/api/organize/plan/%s?view=full&kind=wash' % created['plan_id']).json()
    assert full['total'] == 1
    row = full['items'][0]
    assert row['wash_of'] == 'music/已整理/A/B/Song.wav' and row['target'] == 'music/已整理/A/B/Song.wav'
    assert row['wash_to'].startswith('music/未整理/已存在/')

    applied = client.post('/api/organize/apply', json={'plan_id': created['plan_id'], 'confirmed': True}).json()
    assert applied['total'] == 1 and applied['remaining'] == 0
    apply_job = app_env['store'].job(applied['job_id'])
    results = [org.run_apply_item({'path': item['path'], 'ord': item['ord']}, apply_job['options'])
               for item in apply_job['items']]
    finish(app_env, applied['job_id'])
    assert results[0].get('wash_old', '').startswith('music/未整理/已存在/')
    assert (app_env['library'] / 'A' / 'B' / 'Song.wav').stat().st_size > 20000
    kept = app_env['media'] / 'music' / '未整理' / '已存在' / 'Song.wav'
    assert kept.is_file() and kept.stat().st_size < 20000
    final = client.get('/api/organize/plan/%s' % created['plan_id']).json()
    assert final['status'] == 'done' and final['applied']['wash'] == 1


def test_scan_rejected_while_other_job_running(app_env):
    client = app_env['client']
    client.post('/api/organize/settings', json={})
    app_env['store'].rows.append({'id': 'busy', 'kind': 'organize_apply', 'status': 'running',
                                  'total': 1, 'counts': {}, 'created': time.time(), 'options': {}, 'items': []})
    busy = client.post('/api/organize/scan')
    assert busy.status_code == 409
    assert '运行' in busy.json()['detail']

def test_scan_marks_plan_failed_when_job_rejected(app_env):
    """任务被拒时占位计划要封成「失败」，不能永远停在「扫描中」。"""
    def boom(kind, items, options):
        raise ValueError('任务类型无效，或曲目数不在 1–50 范围内')

    app_env['store'].create_job = boom
    res = app_env['client'].post('/api/organize/scan')
    assert res.status_code == 400
    plans = app_env['client'].get('/api/organize/plans').json()['plans']
    assert plans, '应该留下一条计划记录'
    note = plans[0]
    assert note['status'] == 'failed'
    detail = app_env['client'].get('/api/organize/plan/%s' % note['plan_id']).json()
    assert detail['status'] == 'failed'
