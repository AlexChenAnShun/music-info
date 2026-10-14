"""Local, bounded SQLite persistence for MusicTag Lite (no audio in SQL)."""
from __future__ import annotations
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


def now() -> float:
    return time.time()


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Store:
    def __init__(self, directory):
        self.root = Path(directory).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = self.root / "music-tag-lite.sqlite3"
        self._write_lock = threading.RLock()
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS files(
                id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE,
                version TEXT, tags TEXT NOT NULL DEFAULT '{}', updated REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
                options TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                cancel_requested INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL,
                error TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS job_items(
                id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
                ordinal INTEGER NOT NULL, path TEXT NOT NULL, status TEXT NOT NULL,
                input TEXT NOT NULL, result TEXT, error TEXT NOT NULL DEFAULT '',
                started REAL, finished REAL
            );
            CREATE TABLE IF NOT EXISTS job_logs(
                id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                at REAL NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS versions(
                id TEXT PRIMARY KEY, file_id TEXT NOT NULL, kind TEXT NOT NULL,
                status TEXT NOT NULL, before_path TEXT NOT NULL, after_path TEXT NOT NULL,
                before_version TEXT NOT NULL, after_version TEXT NOT NULL DEFAULT '',
                before_tags TEXT NOT NULL, after_tags TEXT NOT NULL DEFAULT '{}',
                snapshot TEXT NOT NULL, created REAL NOT NULL, error TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS job_status_time ON jobs(status,created);
            CREATE INDEX IF NOT EXISTS item_job ON job_items(job_id,ordinal);
            CREATE INDEX IF NOT EXISTS version_file_time ON versions(file_id,created DESC);
            CREATE INDEX IF NOT EXISTS version_time ON versions(created DESC);
            CREATE TABLE IF NOT EXISTS preferences(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            PRAGMA user_version=1;
            """)

    @contextmanager
    def connect(self, write=False):
        lock = self._write_lock if write else None
        if lock:
            lock.acquire()
        conn = None
        try:
            conn = sqlite3.connect(str(self.db), timeout=8, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=8000")
            conn.execute("PRAGMA synchronous=FULL")
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if write:
                conn.commit()
        except Exception:
            if conn and write:
                conn.rollback()
            raise
        finally:
            if conn:
                conn.close()
            if lock:
                lock.release()

    def file(self, path, version=None, tags=None):
        with self.connect(write=True) as conn:
            row = conn.execute("SELECT * FROM files WHERE path=?", (path,)).fetchone()
            if row is None:
                file_id = str(uuid.uuid4())
                conn.execute("INSERT INTO files VALUES(?,?,?,?,?)", (file_id, path, version, dumps(tags or {}), now()))
            else:
                file_id = row['id']
                if version is not None:
                    conn.execute("UPDATE files SET version=?,tags=?,updated=? WHERE id=?", (version, dumps(tags or {}), now(), file_id))
        return file_id

    def relocate(self, file_id, path, version, tags):
        with self.connect(write=True) as conn:
            stale = conn.execute("SELECT id FROM files WHERE path=? AND id<>?", (path, file_id)).fetchone()
            if stale:
                # The destination has already been validated absent on disk.
                conn.execute("UPDATE files SET path=? WHERE id=?", ('@missing/' + stale['id'], stale['id']))
            conn.execute("UPDATE files SET path=?,version=?,tags=?,updated=? WHERE id=?", (path, version, dumps(tags), now(), file_id))

    def create_job(self, kind, items, options):
        if kind not in ('preview', 'apply') or not 1 <= len(items) <= 50:
            raise ValueError('任务类型无效，或曲目数不在 1–50 范围内')
        job_id = str(uuid.uuid4())
        created = now()
        with self.connect(write=True) as conn:
            conn.execute("INSERT INTO jobs(id,kind,status,options,created,updated,total) VALUES(?,?,'pending',?,?,?,?)", (job_id, kind, dumps(options), created, created, len(items)))
            for ordinal, item in enumerate(items):
                conn.execute("INSERT INTO job_items(id,job_id,ordinal,path,status,input) VALUES(?,?,?,?,'pending',?)", (str(uuid.uuid4()), job_id, ordinal, item['path'], dumps(item)))
            conn.execute("INSERT INTO job_logs(job_id,at,level,message) VALUES(?,?,'info',?)", (job_id, created, '任务已创建；预览任务不会写入音乐。' if kind == 'preview' else '已接受用户确认的写入任务。'))
        return job_id

    def claim_job(self):
        with self.connect(write=True) as conn:
            row = conn.execute("SELECT * FROM jobs WHERE status='pending' AND cancel_requested=0 ORDER BY created LIMIT 1").fetchone()
            if row is None:
                return None
            conn.execute("UPDATE jobs SET status='running',updated=? WHERE id=?", (now(), row['id']))
            result = dict(row)
            result['options'] = json.loads(result['options'])
            return result

    def job_items(self, job_id):
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM job_items WHERE job_id=? ORDER BY ordinal", (job_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item['input'] = json.loads(item['input'])
            item['result'] = json.loads(item['result']) if item['result'] else None
            result.append(item)
        return result

    def item_started(self, item_id):
        with self.connect(write=True) as conn:
            conn.execute("UPDATE job_items SET status='running',started=? WHERE id=?", (now(), item_id))

    def item_finished(self, item_id, result=None, error=''):
        with self.connect(write=True) as conn:
            conn.execute("UPDATE job_items SET status=?,result=?,error=?,finished=? WHERE id=?", ('failed' if error else 'completed', dumps(result) if result is not None else None, error[:1000], now(), item_id))

    def cancelled(self, job_id):
        with self.connect() as conn:
            row = conn.execute("SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)).fetchone()
        return bool(row and row[0])

    def cancel(self, job_id):
        with self.connect(write=True) as conn:
            row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError('任务不存在')
            if row[0] in ('pending', 'running'):
                conn.execute("UPDATE jobs SET cancel_requested=1,updated=? WHERE id=?", (now(), job_id))
                if row[0] == 'pending':
                    conn.execute("UPDATE jobs SET status='cancelled' WHERE id=?", (job_id,))
                    conn.execute("UPDATE job_items SET status='cancelled' WHERE job_id=? AND status='pending'", (job_id,))
            conn.execute("INSERT INTO job_logs(job_id,at,level,message) VALUES(?,?,'info',?)", (job_id, now(), '已请求停止；正在进行的原子写入会安全结束，不会强杀。'))

    def log(self, job_id, message, level='info'):
        with self.connect(write=True) as conn:
            conn.execute("INSERT INTO job_logs(job_id,at,level,message) VALUES(?,?,?,?)", (job_id, now(), level, message[:1000]))
            conn.execute("UPDATE jobs SET updated=? WHERE id=?", (now(), job_id))

    def finish_job(self, job_id):
        with self.connect(write=True) as conn:
            row = conn.execute("SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)).fetchone()
            counts = {r[0]: r[1] for r in conn.execute("SELECT status,count(*) FROM job_items WHERE job_id=? GROUP BY status", (job_id,))}
            cancelled = bool(row and row[0])
            if cancelled:
                conn.execute("UPDATE job_items SET status='cancelled' WHERE job_id=? AND status='pending'", (job_id,))
            status = 'cancelled' if cancelled else ('partial' if counts.get('failed') and counts.get('completed') else 'failed' if counts.get('failed') else 'completed')
            conn.execute("UPDATE jobs SET status=?,updated=? WHERE id=?", (status, now(), job_id))
            conn.execute("INSERT INTO job_logs(job_id,at,level,message) VALUES(?,?,'info',?)", (job_id, now(), '任务结束：成功 %s，失败 %s。' % (counts.get('completed', 0), counts.get('failed', 0))))

    def recover_jobs(self):
        # Never repeat an interrupted write automatically after a process restart.
        with self.connect(write=True) as conn:
            ids = [row[0] for row in conn.execute("SELECT id FROM jobs WHERE status='running' OR (kind='apply' AND status='pending')")]
            for job_id in ids:
                conn.execute("UPDATE jobs SET status='interrupted',updated=?,error=? WHERE id=?", (now(), '服务重启使任务中断，请检查结果后重新生成预览。', job_id))
                conn.execute("UPDATE job_items SET status='interrupted',error=? WHERE job_id=? AND status IN ('running','pending')", ('任务中断；不会自动重放写入。', job_id))
                conn.execute("INSERT INTO job_logs(job_id,at,level,message) VALUES(?,?,'warning',?)", (job_id, now(), '服务重启：写入任务已封存，未自动重试。'))

    def jobs(self, limit=30):
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM jobs ORDER BY created DESC LIMIT ?", (max(1, min(100, limit)),)).fetchall()
            result = []
            for row in rows:
                job = dict(row)
                job['options'] = json.loads(job['options'])
                job['counts'] = {r[0]: r[1] for r in conn.execute("SELECT status,count(*) FROM job_items WHERE job_id=? GROUP BY status", (job['id'],))}
                result.append(job)
        return result

    def job(self, job_id):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError('任务不存在')
            logs = [dict(r) for r in conn.execute("SELECT at,level,message FROM job_logs WHERE job_id=? ORDER BY id DESC LIMIT 100", (job_id,))]
        job = dict(row)
        job['options'] = json.loads(job['options'])
        job['items'] = self.job_items(job_id)
        job['counts'] = {}
        for item in job['items']:
            job['counts'][item['status']] = job['counts'].get(item['status'], 0) + 1
        job['logs'] = list(reversed(logs))
        return job

    def prepare_version(self, version_id, file_id, kind, before_path, after_path, before_version, before_tags, snapshot):
        with self.connect(write=True) as conn:
            conn.execute("INSERT INTO versions(id,file_id,kind,status,before_path,after_path,before_version,before_tags,snapshot,created) VALUES(?,?,?,'prepared',?,?,?,?,?,?)", (version_id, file_id, kind, before_path, after_path, before_version, dumps(before_tags), snapshot, now()))

    def stage_version(self, version_id, after_version, after_tags):
        with self.connect(write=True) as conn:
            conn.execute("UPDATE versions SET after_version=?,after_tags=? WHERE id=?", (after_version, dumps(after_tags), version_id))

    def finish_version(self, version_id, status='completed', error=''):
        with self.connect(write=True) as conn:
            conn.execute("UPDATE versions SET status=?,error=? WHERE id=?", (status, error[:1000], version_id))

    def version(self, version_id):
        with self.connect() as conn:
            row = conn.execute("SELECT v.*,f.path AS current_path FROM versions v LEFT JOIN files f ON f.id=v.file_id WHERE v.id=?", (version_id,)).fetchone()
        if row is None:
            raise KeyError('历史记录不存在')
        return self._version(dict(row))

    @staticmethod
    def _version(row):
        row['before_tags'] = json.loads(row['before_tags'])
        row['after_tags'] = json.loads(row['after_tags'])
        return row

    def versions(self, limit=50, path=None):
        with self.connect() as conn:
            params = []
            where = ''
            if path is not None:
                where = ' WHERE v.file_id=(SELECT id FROM files WHERE path=?)'
                params.append(path)
            params.append(max(1, min(100, limit)))
            rows = conn.execute("SELECT v.*,f.path AS current_path FROM versions v LEFT JOIN files f ON f.id=v.file_id" + where + " ORDER BY v.created DESC LIMIT ?", params).fetchall()
        return [self._version(dict(row)) for row in rows]

    def prepared_versions(self):
        with self.connect() as conn:
            return [self._version(dict(r)) for r in conn.execute("SELECT * FROM versions WHERE status='prepared'")]

    def health(self):
        with self.connect() as conn:
            mode = conn.execute('PRAGMA journal_mode').fetchone()[0]
            count_jobs = conn.execute('SELECT count(*) FROM jobs').fetchone()[0]
            count_versions = conn.execute("SELECT count(*) FROM versions WHERE status='completed'").fetchone()[0]
            active = conn.execute("SELECT count(*) FROM jobs WHERE status IN ('pending','running')").fetchone()[0]
        return {'database': 'SQLite', 'journal_mode': mode, 'schema': 1, 'jobs': count_jobs, 'history': count_versions, 'active_jobs': active}
