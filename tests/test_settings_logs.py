"""V4.1：统一出网代理 + 实时日志（SSE）的测试。

- 代理设置：默认值、URL 校验、保存生效（环境变量 + settings.json）、停用清理
- 代理测试接口：成功/失败/非法地址（mock 出网，不真实联网）
- 实时日志：环形缓冲捕获、/api/logs 的 level/q/limit 过滤、SSE 生成器与广播
- 环境隔离：每条用例后恢复 _SETTINGS 与代理环境变量

运行：python -m pytest tests/test_settings_logs.py
"""
import json
import logging
import os
import queue
import sys
import tempfile
from pathlib import Path

import pytest
import requests

BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
sys.path.insert(0, BACKEND)

pytest.importorskip("fastapi")

BOOT = tempfile.mkdtemp(prefix="mtl-logs-")
os.environ.setdefault("MEDIA_DIR", str(Path(BOOT) / "music"))
os.environ.setdefault("DATA_DIR", str(Path(BOOT) / "data"))
Path(os.environ["MEDIA_DIR"]).mkdir(parents=True, exist_ok=True)

import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app.app)

PROXY_KEYS = ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
              'http_proxy', 'https_proxy', 'all_proxy', 'NO_PROXY', 'no_proxy')
GOOD_URL = 'http://10.0.0.77:7890'


@pytest.fixture(autouse=True)
def restore_state():
    """每条用例后恢复设置与环境变量，避免污染其他测试。"""
    snapshot = json.loads(json.dumps(app._SETTINGS))
    env = {key: os.environ.get(key) for key in PROXY_KEYS}
    yield
    app._SETTINGS.clear()
    app._SETTINGS.update(snapshot)
    for key, value in env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def emit(msg, level='info'):
    getattr(logging.getLogger('musicinfo'), level)(msg)


def fetch_logs(**params):
    resp = client.get('/api/logs', params=params)
    assert resp.status_code == 200
    return resp.json()


# ---------- 代理设置 ----------

def test_settings_defaults():
    data = client.get('/api/settings').json()['settings']
    assert isinstance(data['proxy']['enabled'], bool)
    assert isinstance(data['proxy']['url'], str)


def test_settings_reject_bad_url():
    for bad in ('ftp://10.0.0.1:1', 'not a url', 'http://', 'http://a b', 'x' * 301):
        resp = client.post('/api/settings', json={'proxy': {'enabled': False, 'url': bad}})
        assert resp.status_code == 400, bad
    # 启用但地址为空 -> 拒绝
    resp = client.post('/api/settings', json={'proxy': {'enabled': True, 'url': ''}})
    assert resp.status_code == 400


def test_settings_save_applies_env_and_file():
    resp = client.post('/api/settings', json={'proxy': {'enabled': True, 'url': GOOD_URL}})
    assert resp.status_code == 200 and resp.json()['ok'] is True
    assert resp.json()['settings']['proxy'] == {'enabled': True, 'url': GOOD_URL}
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'):
        assert os.environ.get(key) == GOOD_URL, key
    assert '127.0.0.1' in os.environ.get('NO_PROXY', '')
    saved = json.loads(Path(app._SETTINGS_FILE).read_text('utf-8'))
    assert saved['proxy'] == {'enabled': True, 'url': GOOD_URL}


def test_settings_disable_clears_env():
    client.post('/api/settings', json={'proxy': {'enabled': True, 'url': GOOD_URL}})
    resp = client.post('/api/settings', json={'proxy': {'enabled': False, 'url': GOOD_URL}})
    assert resp.status_code == 200
    for key in PROXY_KEYS:
        assert os.environ.get(key) is None, key
    assert app._proxy_url() == ''


def test_settings_reload_from_file():
    client.post('/api/settings', json={'proxy': {'enabled': True, 'url': GOOD_URL}})
    reloaded = app._load_app_settings()
    assert reloaded['proxy'] == {'enabled': True, 'url': GOOD_URL}
    assert app._valid_proxy_url(GOOD_URL) and app._valid_proxy_url('')
    assert not app._valid_proxy_url('https://') and not app._valid_proxy_url('socks5://a:1')


# ---------- 代理测试接口（mock 出网） ----------

def test_proxy_test_success(monkeypatch):
    captured = {}

    class FakeResp:
        status_code = 200

    def fake_get(url, **kwargs):
        captured['url'] = url
        captured['proxies'] = kwargs.get('proxies')
        captured['timeout'] = kwargs.get('timeout')
        return FakeResp()

    monkeypatch.setattr(app.requests, 'get', fake_get)
    data = client.post('/api/settings/test_proxy', json={'url': GOOD_URL}).json()
    assert data['ok'] is True and data['url'] == GOOD_URL
    first = data['results'][0]
    assert first['ok'] and first['status'] == 200 and isinstance(first['ms'], int)
    assert captured['proxies'] == {'http': GOOD_URL, 'https': GOOD_URL}
    assert captured['url'].startswith('https://itunes.apple.com')
    assert captured['timeout'] == 8


def test_proxy_test_failure_and_direct(monkeypatch):
    def boom(*args, **kwargs):
        raise requests.exceptions.ConnectTimeout('proxy timed out')

    monkeypatch.setattr(app.requests, 'get', boom)
    data = client.post('/api/settings/test_proxy', json={'url': GOOD_URL}).json()
    assert data['ok'] is False
    assert all(not item['ok'] for item in data['results'])
    assert 'timed out' in data['results'][0]['error']
    # 不填地址 = 直连测试，同样走失败分支
    direct = client.post('/api/settings/test_proxy', json={'url': ''}).json()
    assert direct['ok'] is False and direct['url'] == ''
    # 非法地址直接 400
    assert client.post('/api/settings/test_proxy', json={'url': 'ftp://x'}).status_code == 400


# ---------- 实时日志 ----------

def test_logs_capture_and_shape():
    token = 'cap-%d' % os.getpid()
    emit('日志捕获标记 ' + token)
    data = fetch_logs(limit=200)
    hit = [item for item in data['logs'] if token in item['msg']]
    assert hit, '日志未进入环形缓冲'
    item = hit[-1]
    assert set(item) == {'id', 'ts', 'level', 'src', 'msg'}
    assert item['level'] == 'INFO' and item['src'] == 'musicinfo'
    assert isinstance(item['ts'], int) and data['total'] >= 1 and data['seq'] >= item['id']


def test_logs_level_and_keyword_filter():
    token = 'flt-%d' % os.getpid()
    emit('info 行 ' + token, 'info')
    emit('warn 行 ' + token, 'warning')
    only_warn = fetch_logs(level='WARNING', q=token)
    assert only_warn['logs'] and all(i['level'] == 'WARNING' for i in only_warn['logs'])
    only_info = fetch_logs(q='info 行 ' + token)
    assert only_info['logs'] and all(i['level'] == 'INFO' for i in only_info['logs'])
    none = fetch_logs(q=token + '-no-such-suffix')
    assert none['logs'] == []


def test_logs_limit_clamped():
    assert fetch_logs(limit=99999)['limit'] == 5000
    assert fetch_logs(limit=0)['limit'] == 1
    assert client.get('/api/logs', params={'limit': 'bad'}).status_code == 422


def test_logs_stream_broadcast():
    token = 'bus-%d' % os.getpid()
    q = queue.Queue()
    app._LOG_QUEUES.append(q)
    try:
        emit('广播标记 ' + token)
        item = q.get(timeout=3)
        assert token in item['msg'] and item['src'] == 'musicinfo'
    finally:
        app._LOG_QUEUES.remove(q)


def test_logs_stream_generator():
    resp = app.api_logs_stream()
    assert resp.media_type == 'text/event-stream'
    assert resp.headers['cache-control'] == 'no-cache'
    before = len(app._LOG_QUEUES)
    gen = app._log_stream_gen()
    try:
        it = iter(gen)
        assert next(it).startswith('retry:')
        assert len(app._LOG_QUEUES) == before + 1
        token = 'sse-%d' % os.getpid()
        emit('SSE 标记 ' + token)
        payload = next(it)
        assert payload.startswith('data: ') and token in payload
        assert payload.endswith('\n\n')
    finally:
        gen.close()
    assert len(app._LOG_QUEUES) == before, 'SSE 断开后订阅未清理'
