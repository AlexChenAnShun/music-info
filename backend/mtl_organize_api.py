"""本地自动整理 HTTP 接口（挂在现有 Music Info app 上）。

只新增 /api/organize/* 端点；任务仍然走 mtl_store + mtl_jobs，
因此任务中心、取消、日志全部复用现有机制。
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

from fastapi import HTTPException
from pydantic import BaseModel

import mtl_organize as organize

RUNNING_STATES = ('pending', 'running')
KIND_LABELS = {'move': '待移动', 'wash': '洗版替换', 'duplicate': '重复',
               'junk': '冗余/失败', 'sidecar': '附属文件'}
DIR_KEYS = ('source', 'duplicates', 'junk', 'library')
DIR_CACHE = {}          # 目录统计的 30 秒缓存，避免状态轮询反复走全库
DIR_TTL = 30.0


class OrganizeSettingsIn(BaseModel):
    source: Optional[str] = None
    duplicates: Optional[str] = None
    junk: Optional[str] = None
    library: Optional[str] = None
    track_prefix: Optional[bool] = None
    track_sep: Optional[str] = None
    follow_sidecars: Optional[bool] = None
    unknown_artist: Optional[str] = None
    unknown_album: Optional[str] = None
    name_template: Optional[str] = None
    dir_template: Optional[str] = None
    classify_rules: Optional[str] = None
    wash_mode: Optional[str] = None
    wash_max_mb: Optional[int] = None
    host_root: Optional[str] = None


class OrganizeApplyIn(BaseModel):
    plan_id: str
    confirmed: bool = False


class OrganizePreviewIn(BaseModel):
    name_template: Optional[str] = None
    host_root: Optional[str] = None
    dir_template: Optional[str] = None
    classify_rules: Optional[str] = None
    track_prefix: Optional[bool] = None
    track_sep: Optional[str] = None
    unknown_artist: Optional[str] = None
    unknown_album: Optional[str] = None


class OrganizeMkdirIn(BaseModel):
    path: str = ''
    name: str


def _settings_node(settings) -> dict:
    try:
        node = settings.get('organize')
    except Exception:
        node = None
    return node if isinstance(node, dict) else {}


def current_settings(settings) -> dict:
    node = _settings_node(settings)
    try:
        return organize.normalize_settings(node)
    except HTTPException:
        pass
    # 万一磁盘上的配置坏了（例如手工改过文件），至少把其它字段正常显示出来
    try:
        return organize.normalize_settings({key: value for key, value in node.items()
                                            if key != 'classify_rules'})
    except HTTPException:
        return organize.default_settings()


def store_settings(settings, cfg: dict, persist=None):
    settings['organize'] = {key: cfg[key] for key in organize.DEFAULT_SETTINGS}
    if callable(persist):
        try:
            persist()
        except Exception:
            pass


def resolve_dir(media: Path, rel: str) -> Path:
    """把相对路径解析成音乐库内的目录（空串 = 音乐库根），越界即拒绝。"""
    text = str(rel or '').strip().strip('/')
    node = media if not text else (media / organize.norm_rel_path(text))
    node = node.resolve()
    if not node.is_relative_to(media):
        raise HTTPException(400, '路径必须位于音乐库目录内')
    if not node.is_dir():
        raise HTTPException(404, '目录不存在：%s' % (text or '/'))
    return node


def dir_stats(media: Path, rel: str, ttl: float = DIR_TTL):
    """递归统计目录里的文件数与总体积（带缓存，供状态条显示）。"""
    key = str(media / rel)
    now = time.time()
    hit = DIR_CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1], hit[2]
    files = 0
    total = 0
    base = media / rel
    if base.is_dir():
        stack = [str(base)]
        while stack:
            current = stack.pop()
            try:
                for child in os.scandir(current):
                    if child.is_dir(follow_symlinks=False):
                        stack.append(child.path)
                    elif child.is_file(follow_symlinks=False):
                        files += 1
                        try:
                            total += child.stat().st_size
                        except OSError:
                            pass
            except OSError:
                continue
    DIR_CACHE[key] = (now, files, total)
    return files, total


def paths_state(media: Path, cfg: dict, problems=None) -> dict:
    bad = {row['key']: row['reason'] for row in (problems or [])}
    state = {}
    for key in DIR_KEYS:
        path = media / cfg[key]
        files, total = dir_stats(media, cfg[key])
        state[key] = {'rel': cfg[key], 'abs': str(path), 'host': organize.host_path(cfg, cfg[key]),
                      'exists': path.is_dir(), 'valid': key not in bad, 'files': files, 'bytes': total,
                      'problem': bad.get(key, '')}
    return state


def ensure_dirs(media: Path, cfg: dict, include_source: bool = True) -> list:
    created = []
    keys = DIR_KEYS if include_source else ('duplicates', 'junk', 'library')
    for key in keys:
        path = media / cfg[key]
        DIR_CACHE.pop(str(path), None)
        if not path.is_dir():
            path.mkdir(parents=True, exist_ok=True)
            created.append(cfg[key])
    return created


def running_jobs(store, kind_prefix=None):
    try:
        rows = store.jobs(30)
    except Exception:
        return []
    out = []
    for row in rows:
        kind = str(row.get('kind') or '')
        if kind_prefix and not kind.startswith(kind_prefix):
            continue
        if row.get('status') in RUNNING_STATES:
            out.append(row)
    return out


def scan_job_of(store, plan_id: str):
    for row in running_jobs(store, 'organize'):
        if row.get('kind') == 'organize_scan' and (row.get('options') or {}).get('plan_id') == plan_id:
            return row
    return None


def active_job(store):
    rows = running_jobs(store, 'organize')
    return rows[0] if rows else None


def plan_payload(plan: dict, scan_job: str = '', include_items: bool = False,
                 kind: str = '', offset: int = 0, limit: int = 300) -> dict:
    payload = {
        'plan_id': plan.get('plan_id'),
        'created': plan.get('created'),
        'elapsed': plan.get('elapsed'),
        'status': plan.get('status'),
        'note': plan.get('note', ''),
        'scanned': plan.get('scanned', 0),
        'cfg': plan.get('cfg'),
        'counts': plan.get('counts') or {},
        'applied': plan.get('applied') or {},
        'index': plan.get('index') or {},
        'scan_job': scan_job,
    }
    if include_items:
        wanted = [row.strip() for row in str(kind or '').split(',') if row.strip()]
        items = []
        for item in plan.get('items') or []:
            if wanted and item.get('kind') not in wanted:
                continue
            row = {key: value for key, value in item.items() if key not in ('sha256', 'mtime_ns', 'fp', 'dup_of', 'owner')}
            row['sidecars'] = [{'name': side.get('name'), 'target': side.get('target'),
                                'status': side.get('status'), 'error': side.get('error', '')}
                               for side in item.get('sidecars') or []]
            items.append(row)
        offset = max(0, int(offset or 0))
        limit = max(1, min(2000, int(limit or 300)))
        payload['total'] = len(items)
        payload['items'] = items[offset:offset + limit]
        payload['offset'] = offset
    return payload


def register(app, store, engine, settings, persist_settings=None, log=None):
    """把 /api/organize/* 挂到现有 FastAPI app 上。"""
    def emit(message):
        if callable(log):
            try:
                log(message)
            except Exception:
                pass

    @app.get('/api/organize/status')
    def organize_status():
        media = organize._media_root()
        node = _settings_node(settings)
        cfg = current_settings(settings)
        problems = organize.settings_problems(media, node)
        index = organize.load_index()
        running = running_jobs(store, 'organize')
        return {
            'media_root': str(media),
            'host_root': cfg.get('host_root', ''),
            'media_writable': os.access(str(media), os.W_OK),
            'settings': cfg,
            'defaults': organize.default_settings(),
            'paths': paths_state(media, cfg, problems),
            'problems': problems,
            'kinds': dict(KIND_LABELS),
            'library_index': {'files': len(index.get('files') or {}),
                              'updated': index.get('updated', 0)},
            'effective_name_template': organize.name_template_of(cfg),
            'wash_modes': list(organize.WASH_MODES),
            'running_plans': [{'job_id': row.get('id'), 'kind': row.get('kind'),
                               'status': row.get('status'),
                               'plan_id': (row.get('options') or {}).get('plan_id')}
                              for row in running],
        }

    @app.get('/api/organize/settings')
    def organize_get_settings():
        media = organize._media_root()
        cfg = current_settings(settings)
        problems = organize.settings_problems(media, _settings_node(settings))
        return {'settings': cfg, 'defaults': organize.default_settings(),
                'paths': paths_state(media, cfg, problems), 'problems': problems,
                'kinds': dict(KIND_LABELS)}

    @app.post('/api/organize/settings')
    def organize_save_settings(body: OrganizeSettingsIn):
        media = organize._media_root()
        node = dict(_settings_node(settings))
        for key, value in body.model_dump(exclude_none=True).items():
            node[key] = value
        cfg = organize.validate_settings(media, node)
        created = ensure_dirs(media, cfg, include_source=False)
        store_settings(settings, cfg, persist=persist_settings)
        emit('整理设置已保存：%s' % '、'.join('%s=%s' % (k, cfg[k]) for k in DIR_KEYS))
        return {'ok': True, 'settings': cfg, 'created': created,
                'paths': paths_state(media, cfg)}

    @app.post('/api/organize/scan')
    def organize_scan():
        media = organize._media_root()
        cfg = organize.validate_settings(media, current_settings(settings))
        if not os.access(str(media), os.W_OK):
            raise HTTPException(400, '音乐目录不可写：容器里的 /media 需要可写（rw）挂载（compose 里 ./music:/media/music:rw）')
        running = active_job(store)
        if running:
            raise HTTPException(409, '已有整理任务在运行（%s，%s），请先等它结束或点「安全停止」'
                                % (running.get('id'), running.get('kind')))
        ensure_dirs(media, cfg, include_source=True)
        if not (media / cfg['source']).is_dir():
            raise HTTPException(400, '源文件夹不存在：%s（请先在 NAS 上创建）' % cfg['source'])
        plan_id = organize.new_plan_id()
        organize.prepare_plan(cfg, plan_id)          # 先落「扫描中」占位，前端立即可见
        try:
            job_id = store.create_job('organize_scan', [{'path': cfg['source']}],
                                      {'plan_id': plan_id, 'cfg': cfg})
        except ValueError as exc:
            # 任务被拒（例如类型/条数校验失败）时，别把占位计划一直挂在「扫描中」。
            organize.mark_terminal(plan_id, 'failed', '任务创建失败：%s' % exc)
            raise HTTPException(400, str(exc))
        try:
            engine.notify()
        except Exception:
            pass
        emit('整理扫描任务已创建：plan=%s 源=%s（只读）' % (plan_id, cfg['source']))
        return {'job_id': job_id, 'plan_id': plan_id, 'status': 'pending', 'writes_music': False}

    @app.post('/api/organize/preview')
    def organize_preview(body: OrganizePreviewIn):
        """按当前表单里的模板给出两条示例路径（不保存、不扫描）。"""
        media = organize._media_root()
        node = dict(_settings_node(settings))
        for key, value in body.model_dump(exclude_none=True).items():
            node[key] = value
        try:
            cfg = organize.validate_settings(media, node)
            rules = organize.parse_classify_rules(cfg.get('classify_rules') or '')
        except HTTPException as exc:
            return {'ok': False, 'error': str(exc.detail)}
        rows = []
        for sample in (organize.SAMPLE_TAGS, organize.SAMPLE_TAGS_EMPTY):
            tokens, filled = organize.meta_tokens(cfg, sample)
            stem = organize.tidy_stem(organize.sanitize_segment(
                organize.tidy_stem(organize.render_template(organize.name_template_of(cfg), tokens, filled)),
                '未命名作品'))
            template, rule = cfg['dir_template'], ''
            for candidate in rules:
                if organize.rule_matches(candidate, {'genre': tokens['genre'], 'year': tokens['year'],
                                                     'decade': tokens['decade']}):
                    template, rule = candidate['path'], candidate['name']
                    break
            rendered = organize.render_template(template, tokens, filled)
            parts = [part for part in
                     (organize.tidy_stem(organize.sanitize_segment(piece, '')) for piece in str(rendered).split('/'))
                     if part]
            shown = '/'.join([cfg['library']] + parts + [stem + '.flac'])
            rows.append({'sample': sample['title'], 'rule': rule, 'path': shown,
                         'host': organize.host_path(cfg, shown)})
        return {'ok': True, 'paths': rows, 'name_template': organize.name_template_of(cfg),
                'rules': len(rules)}

    @app.get('/api/organize/browse')
    def organize_browse(path: str = ''):
        """目录选择器：列出音乐库内某一层的子目录（只读，不允许越出音乐库）。"""
        media = organize._media_root()
        node = resolve_dir(media, path)
        dirs = []
        files = 0
        try:
            for child in os.scandir(node):
                try:
                    if child.is_dir(follow_symlinks=False):
                        if not child.name.startswith('.'):
                            dirs.append(child.name)
                    elif child.is_file(follow_symlinks=False):
                        files += 1
                except OSError:
                    continue
        except OSError as exc:
            raise HTTPException(400, '目录无法读取：%s' % organize.clean_error(exc))
        dirs.sort(key=lambda name: name.lower())
        rel = node.relative_to(media).as_posix()
        rel = '' if rel == '.' else rel
        parent = None
        if rel:
            parent = rel.rsplit('/', 1)[0] if '/' in rel else ''
        cfg = current_settings(settings)
        roles_exact = [key for key in DIR_KEYS if cfg[key] == rel]
        roles_below = [key for key in DIR_KEYS if rel and cfg[key].startswith(rel + '/')]
        return {'path': rel, 'abs': str(node), 'parent': parent, 'dirs': dirs[:300],
                'files': files, 'dirs_total': len(dirs),
                'roles_exact': roles_exact, 'roles_below': roles_below}

    @app.post('/api/organize/mkdir')
    def organize_mkdir(body: OrganizeMkdirIn):
        media = organize._media_root()
        node = resolve_dir(media, body.path)
        name = organize.sanitize_segment(body.name, '')
        if not name or name in ('.', '..'):
            raise HTTPException(400, '文件夹名不能为空')
        if '/' in name or '\\' in name:
            raise HTTPException(400, '文件夹名不能包含路径分隔符')
        target = node / name
        if not target.resolve().is_relative_to(media):
            raise HTTPException(400, '路径必须位于音乐库目录内')
        created = not target.exists()
        if target.exists() and not target.is_dir():
            raise HTTPException(400, '同名文件已存在：%s' % name)
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HTTPException(400, '创建失败：%s' % organize.clean_error(exc))
        rel = target.relative_to(media).as_posix()
        DIR_CACHE.pop(str(node), None)
        emit('已在音乐库中创建目录：%s' % rel)
        return {'ok': True, 'created': created, 'path': rel}

    @app.get('/api/organize/plans')
    def organize_plans(limit: int = 20):
        rows = organize.list_plans(limit)
        running = active_job(store)
        return {'plans': rows,
                'active_job': ({'id': running.get('id'), 'kind': running.get('kind'),
                                'status': running.get('status'),
                                'plan_id': (running.get('options') or {}).get('plan_id')}
                               if running else None)}

    @app.get('/api/organize/plan/{plan_id}')
    def organize_plan(plan_id: str, view: str = '', kind: str = '', offset: int = 0, limit: int = 300):
        plan = organize.load_plan(plan_id)
        job = scan_job_of(store, plan_id)
        if plan.get('status') == 'scanning' and not job:
            # 扫描任务已经不在了（取消/重启/中断）→ 收敛成终态，避免计划永远卡住
            organize.mark_terminal(plan_id, 'cancelled', '扫描任务已结束但没有出结果（可能被中断），请重新扫描')
            plan = organize.load_plan(plan_id)
        return plan_payload(plan, scan_job=(job.get('id') if job else ''),
                            include_items=(view == 'full'), kind=kind, offset=offset, limit=limit)

    @app.post('/api/organize/plan/{plan_id}/discard')
    def organize_discard(plan_id: str):
        plan = organize.load_plan(plan_id)
        if plan.get('status') == 'applying' and active_job(store):
            raise HTTPException(409, '整理执行任务还在运行，无法丢弃计划')
        try:
            organize._plan_path(plan_id).unlink(missing_ok=True)
        except OSError as exc:
            raise HTTPException(400, '删除计划失败：%s' % organize.clean_error(exc))
        emit('整理计划已丢弃：%s' % plan_id)
        return {'ok': True, 'discarded': plan_id}

    @app.post('/api/organize/apply')
    def organize_apply(body: OrganizeApplyIn):
        if not body.confirmed:
            raise HTTPException(400, '整理执行必须经过明确确认')
        plan = organize.load_plan(body.plan_id)
        if plan.get('status') not in ('ready', 'applying'):
            raise HTTPException(400, '这个计划不能执行（状态：%s）' % (plan.get('status') or '未知'))
        pending = organize.pending_items(plan)
        if not pending:
            raise HTTPException(400, '计划里已经没有待执行的条目：可能已经整理过了')
        running = active_job(store)
        if running:
            raise HTTPException(409, '已有整理任务在运行（%s），请先等它结束' % running.get('id'))
        problems = organize.preflight(plan)
        if problems:
            rows = '、'.join('%s（%s）' % (row['name'], row['reason']) for row in problems[:5])
            raise HTTPException(409, '以下文件已变化，请重新扫描后再执行：%s' % rows)
        batch = [{'path': row['src'], 'ord': row['ord']} for row in pending][:5000]
        try:
            job_id = store.create_job('organize_apply', batch, {'plan_id': plan['plan_id']})
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        emit('整理执行任务已创建：plan=%s 本次 %d 项' % (plan['plan_id'], len(batch)))
        try:
            engine.notify()
        except Exception:
            pass
        return {'job_id': job_id, 'plan_id': plan['plan_id'], 'status': 'pending',
                'total': len(batch), 'remaining': max(0, len(pending) - len(batch)),
                'writes_music': True, 'counts': plan.get('counts') or {}}

    return app
