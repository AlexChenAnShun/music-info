"""Small persistent queue: network work is concurrent; audio writes serialize in Writer."""
import threading
from fastapi import HTTPException
from mtl_safe import clean_error

_CURRENT = threading.local()


def current_job():
    """引擎工作线程内当前正在执行的任务 id（供集成整理这类需要写日志/响应取消的处理器使用）。"""
    return getattr(_CURRENT, 'job_id', None)


class Engine:
    def __init__(self, store, handler, workers=2):
        self.store = store
        self.handler = handler
        self.workers = max(1, min(3, workers))
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.threads = []

    def start(self):
        if self.threads:
            return
        self.store.recover_jobs()
        self.stop_event.clear()
        for i in range(self.workers):
            thread = threading.Thread(target=self.loop, name='mtl-job-%s' % i, daemon=True)
            self.threads.append(thread)
            thread.start()

    def stop(self):
        self.stop_event.set()
        self.wake.set()
        for thread in self.threads:
            thread.join(timeout=2)
        self.threads.clear()

    def notify(self):
        self.wake.set()

    def loop(self):
        while not self.stop_event.is_set():
            try:
                job = self.store.claim_job()
            except Exception:
                self.stop_event.wait(1)
                continue
            if job is None:
                self.wake.wait(0.7)
                self.wake.clear()
                continue
            try:
                for item in self.store.job_items(job['id']):
                    if self.stop_event.is_set():
                        break
                    if self.store.cancelled(job['id']):
                        break
                    self.store.item_started(item['id'])
                    _CURRENT.job_id = job['id']
                    try:
                        result = self.handler(job['kind'], item['input'], job['options'])
                        self.store.item_finished(item['id'], result=result)
                        noun, verb = {'preview': ('首', '生成预览'), 'apply': ('首', '完成写入'),
                                      'organize_scan': ('项', '完成扫描'), 'organize_apply': ('个文件', '完成整理')}.get(job['kind'], ('首', '完成'))
                        self.store.log(job['id'], '第 %s/%s %s已%s。' % (item['ordinal']+1, job['total'], noun, verb))
                    except Exception as e:
                        detail = e.detail if isinstance(e, HTTPException) else str(e)
                        _CURRENT.job_id = job['id']
                        if isinstance(detail, dict):
                            message = detail.get('message', '操作失败')
                            error_result = {'partial': bool(detail.get('partial')), 'history_id': detail.get('history_id')}
                        else:
                            message, error_result = str(detail), None
                        self.store.item_finished(item['id'], result=error_result, error=clean_error(message))
                        self.store.log(job['id'], '第 %s 首失败：%s' % (item['ordinal']+1, clean_error(message)), 'warning')
                    finally:
                        _CURRENT.job_id = None
                if not self.stop_event.is_set():
                    self.store.finish_job(job['id'])
            except Exception as e:
                try:
                    self.store.log(job['id'], '任务中断，请检查已完成项：' + clean_error(e), 'error')
                except Exception:
                    pass
                # A running task is sealed as interrupted on the next startup.
                self.stop_event.wait(0.5)
