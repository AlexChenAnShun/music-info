"""Versioned, staged metadata writes. The audio payload is never a SQL blob."""
from __future__ import annotations
import base64
import gzip
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import struct
import tempfile
import threading
import uuid
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from fastapi import HTTPException
from mutagen import File as MFile
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3, APIC
from mutagen.mp4 import MP4Cover, MP4FreeForm

MAX_COVER = 8 * 1024 * 1024
MAX_LRC = 2 * 1024 * 1024
FIELDS = ('title','artist','album','albumartist','date','track','tracktotal','disc','disctotal','genre','language','albumtype','comment','description','version','lyrics','lrc')
BASIC = ('title','artist','album','albumartist','date','track','genre','disc')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def clean_error(error):
    text = str(error)
    text = re.sub(r'(?i)(token|secret|api[_-]?key|password)=([^\s&]+)', r'\1=[已隐藏]', text)
    text = re.sub(r'(https?://)[^\s/@]+:[^\s/@]+@', r'\1[已隐藏]@', text)
    return text[:700]


def root_path(root, value):
    path = (Path(root) / str(value)).resolve()
    if not path.is_relative_to(Path(root).resolve()):
        raise HTTPException(400, '路径不能离开音乐目录')
    return path


def cover_bytes(path):
    media = MFile(path)
    if media is None:
        return b'', ''
    if isinstance(media.tags, ID3):
        covers = media.tags.getall('APIC')
        cover = next((x for x in covers if x.type == 3), covers[0] if covers else None)
        return (cover.data, cover.mime) if cover else (b'', '')
    if isinstance(media, FLAC):
        covers = media.pictures
        cover = next((x for x in covers if x.type == 3), covers[0] if covers else None)
        return (cover.data, cover.mime) if cover else (b'', '')
    return b'', ''


def image_payload(url=None, b64=None, mime='image/jpeg'):
    if b64:
        if len(b64) > MAX_COVER * 4 // 3 + 16:
            raise HTTPException(413, '封面不能超过 8 MiB')
        try:
            data = base64.b64decode(b64, validate=True)
        except Exception:
            raise HTTPException(400, '封面编码无效')
    elif url:
        target = url
        data = None
        for _ in range(5):
            parsed = urlparse(target)
            if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password:
                raise HTTPException(400, '封面地址必须是无凭据的 HTTP/HTTPS 地址')
            try:
                port = parsed.port or (443 if parsed.scheme == 'https' else 80)
                addresses = {x[4][0] for x in socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)}
                trusted = any(parsed.hostname == domain or parsed.hostname.endswith('.' + domain) for domain in ('mzstatic.com','music.126.net','music.163.com','kugou.com','gtimg.cn','fanart.tv','archive.org','coverartarchive.org'))
                for address in addresses:
                    ip = ipaddress.ip_address(address)
                    fake_ip = ip in ipaddress.ip_network('198.18.0.0/15')
                    if not ip.is_global and not (trusted and fake_ip):
                        raise HTTPException(400, '封面地址不允许访问内网、回环或系统服务')
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(502, '无法解析封面地址')
            try:
                with requests.get(target, stream=True, allow_redirects=False, timeout=(5, 18), headers={'User-Agent':'MusicTagLite/2.0'}) as response:
                    if response.status_code in (301,302,303,307,308):
                        target = urljoin(target, response.headers.get('Location',''))
                        continue
                    response.raise_for_status()
                    if int(response.headers.get('Content-Length','0') or 0) > MAX_COVER:
                        raise HTTPException(413, '封面不能超过 8 MiB')
                    chunks, length = [], 0
                    for chunk in response.iter_content(65536):
                        length += len(chunk)
                        if length > MAX_COVER:
                            raise HTTPException(413, '封面不能超过 8 MiB')
                        chunks.append(chunk)
                    data = b''.join(chunks)
                    break
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(502, '封面下载失败；音乐文件尚未修改')
        if data is None:
            raise HTTPException(502, '封面重定向次数过多')
    else:
        raise HTTPException(400, '没有封面数据')
    if not data or len(data) > MAX_COVER:
        raise HTTPException(400, '封面为空或过大')
    if data.startswith(b'\xff\xd8\xff'):
        mime = 'image/jpeg'
    elif data.startswith(b'\x89PNG\r\n\x1a\n'):
        mime = 'image/png'
    elif data.startswith((b'GIF87a', b'GIF89a')):
        mime = 'image/gif'
    elif data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        mime = 'image/webp'
    else:
        raise HTTPException(400, '封面必须是 JPEG、PNG、GIF 或 WebP 图片')
    return data, mime


def _encode(value):
    if isinstance(value, MP4Cover):
        return {'@':'MP4Cover','data':base64.b64encode(value).decode(),'format':int(value.imageformat)}
    if isinstance(value, MP4FreeForm):
        return {'@':'MP4FreeForm','data':base64.b64encode(value).decode(),'format':int(value.dataformat),'version':int(value.version)}
    if isinstance(value, bytes):
        return {'@':'bytes','data':base64.b64encode(value).decode()}
    if isinstance(value, tuple):
        return {'@':'tuple','items':[_encode(x) for x in value]}
    if isinstance(value, list):
        return [_encode(x) for x in value]
    if isinstance(value, dict):
        return {str(k):_encode(v) for k,v in value.items()}
    if value is None or isinstance(value, (str,int,float,bool)):
        return value
    raise HTTPException(400, '该格式的扩展标签暂不支持安全备份；未写入')


def _decode(value):
    if isinstance(value, list):
        return [_decode(x) for x in value]
    if isinstance(value, dict):
        kind = value.get('@')
        if kind == 'MP4Cover': return MP4Cover(base64.b64decode(value['data']), imageformat=value['format'])
        if kind == 'MP4FreeForm': return MP4FreeForm(base64.b64decode(value['data']), dataformat=value['format'], version=value['version'])
        if kind == 'bytes': return base64.b64decode(value['data'])
        if kind == 'tuple': return tuple(_decode(x) for x in value['items'])
        return {k:_decode(v) for k,v in value.items()}
    return value


def audio_identity(path):
    """Hash actual audio-bearing payloads; tag-only changes must not alter them."""
    path = Path(path)
    h = hashlib.sha256()
    size = path.stat().st_size
    ext = path.suffix.lower()
    with path.open('rb') as f:
        start, end = 0, size
        if ext == '.flac':
            if f.read(4) != b'fLaC': raise HTTPException(400, 'FLAC 文件头无效')
            while True:
                head = f.read(4)
                if len(head) != 4: raise HTTPException(400, 'FLAC 元数据无效')
                length = int.from_bytes(head[1:4], 'big')
                f.seek(length, 1)
                if head[0] & 128: break
            start = f.tell()
        elif ext == '.mp3':
            head = f.read(10)
            if head[:3] == b'ID3':
                start = 10 + sum((head[6+i] & 127) << (21-7*i) for i in range(4))
            if size >= 128:
                f.seek(size-128)
                if f.read(3) == b'TAG': end -= 128
        elif ext in ('.m4a','.mp4'):
            pos, found = 0, False
            while pos + 8 <= size:
                f.seek(pos)
                head = f.read(8)
                length = int.from_bytes(head[:4],'big')
                header_size = 8
                if length == 1:
                    length = int.from_bytes(f.read(8),'big'); header_size = 16
                elif length == 0: length = size-pos
                if length < header_size or pos+length > size:
                    raise HTTPException(400, 'MP4 音频容器无效')
                if head[4:8] == b'mdat':
                    found = True; remaining = length-header_size
                    while remaining:
                        chunk = f.read(min(1048576,remaining))
                        if not chunk: raise HTTPException(400, 'MP4 音频数据不完整')
                        h.update(chunk); remaining -= len(chunk)
                pos += length
            if not found: raise HTTPException(400, '未找到 MP4 音频数据')
            return h.hexdigest()
        elif ext in ('.ogg','.opus'):
            packet = bytearray()
            while True:
                head = f.read(27)
                if not head: break
                if len(head) != 27 or head[:4] != b'OggS': raise HTTPException(400, 'Ogg 音频容器无效')
                segments = f.read(head[26])
                body = f.read(sum(segments)); offset = 0
                for length in segments:
                    packet.extend(body[offset:offset+length]); offset += length
                    if length < 255:
                        if not packet.startswith((b'\x03vorbis', b'OpusTags')): h.update(packet)
                        packet.clear()
            return h.hexdigest()
        elif ext == '.wav':
            head = f.read(12)
            if head[:4] != b'RIFF' or head[8:] != b'WAVE': raise HTTPException(400,'WAV 文件头无效')
            while f.tell()+8 <= size:
                block = f.read(8); length = int.from_bytes(block[4:8],'little')
                if block[:4] == b'data':
                    remaining = length
                    while remaining:
                        chunk = f.read(min(1048576,remaining))
                        if not chunk: raise HTTPException(400,'WAV 数据不完整')
                        h.update(chunk); remaining -= len(chunk)
                else: f.seek(length,1)
                if length & 1: f.seek(1,1)
            return h.hexdigest()
        f.seek(start)
        remaining = end-start
        while remaining > 0:
            chunk = f.read(min(1048576, remaining))
            if not chunk: break
            h.update(chunk); remaining -= len(chunk)
    return h.hexdigest()


class Writer:
    def __init__(self, media, store, read_tags, write_tags):
        self.media = Path(media).resolve()
        self.store = store
        if store.root.is_relative_to(self.media):
            raise RuntimeError('数据库/历史目录不能放进音乐目录')
        self.snapshots = store.root / 'versions'
        self.snapshots.mkdir(exist_ok=True, mode=0o700)
        self.read_tags = read_tags
        self.write_tags = write_tags
        self.lock = threading.RLock()

    def path(self, value):
        p = root_path(self.media, value)
        if not p.is_file(): raise HTTPException(404, '音乐文件不存在')
        if p.suffix.lower() not in ('.mp3','.flac','.m4a','.mp4','.ogg','.opus','.wav','.aac'): raise HTTPException(400,'不是支持的音频文件')
        if p.name.startswith(('.mtl-stage-','.mtl-rollback-','.mtl-lrc-')): raise HTTPException(400,'不允许访问内部事务文件')
        return p

    def relative(self, path):
        return Path(path).resolve().relative_to(self.media).as_posix()

    def sidecar(self, path):
        logical = Path(path).with_suffix('.lrc')
        return root_path(self.media, logical.relative_to(self.media).as_posix())

    def lrc_bytes(self, path):
        p = self.sidecar(path)
        if not p.is_file(): return None
        if p.stat().st_size > MAX_LRC: raise HTTPException(413, '外置歌词过大，无法安全处理')
        return p.read_bytes()

    def version(self, path, sidecar_override=Ellipsis):
        st = Path(path).stat()
        tags = self.read_tags(path)
        cover, _ = cover_bytes(path)
        lrc = self.lrc_bytes(path) if sidecar_override is Ellipsis else sidecar_override
        payload = {'size':st.st_size,'mtime':st.st_mtime_ns,'tags':tags,'cover':digest(cover),'external_lrc':None if lrc is None else digest(lrc)}
        return digest(json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode())

    def capabilities(self, path):
        ext = Path(path).suffix.lower()
        fields = list(FIELDS if ext in ('.mp3','.flac','.ogg','.opus') else BASIC if ext in ('.m4a','.mp4') else ())
        return {'fields':fields,'cover':ext in ('.mp3','.flac'),'safe_history':True,'atomic_writes':True}

    def info(self, path):
        version = self.version(path)
        tags = self.read_tags(path)
        file_id = self.store.file(self.relative(path), version, tags)
        return {'file_version':version,'file_id':file_id,'capabilities':self.capabilities(path)}

    def snapshot(self, path, version_id):
        media = MFile(path)
        if media is None: raise HTTPException(400,'不支持该音频格式')
        st = path.stat()
        result = {'path':self.relative(path),'audio':audio_identity(path),'mtime_ns':st.st_mtime_ns,'size':st.st_size,'tags':self.read_tags(path),'external_lrc':None}
        sidecar = self.lrc_bytes(path)
        if sidecar is not None: result['external_lrc'] = base64.b64encode(sidecar).decode()
        if isinstance(media.tags, ID3):
            result['kind'] = 'id3'
            tag_path = self.snapshots / (version_id + '.id3')
            result['id3_version'] = media.tags.version[1] if media.tags.version[1] in (3,4) else 4
            media.tags.save(str(tag_path), v2_version=result['id3_version'])
            result['id3'] = tag_path.name
        elif isinstance(media, FLAC):
            result['kind'] = 'flac'
            result['values'] = _encode(dict(media.tags or {}))
            result['pictures'] = [base64.b64encode(pic.write()).decode() for pic in media.pictures]
        else:
            result['kind'] = 'generic'
            result['values'] = _encode(dict(media.tags)) if media.tags is not None else None
        target = self.snapshots / (version_id + '.json.gz')
        with gzip.open(target,'wt',encoding='utf-8') as f: json.dump(result,f,ensure_ascii=False)
        return result, target.name

    def load_snapshot(self, name):
        path = (self.snapshots / name).resolve()
        if path.parent != self.snapshots: raise HTTPException(400,'历史路径无效')
        with gzip.open(path,'rt',encoding='utf-8') as f: return json.load(f)

    def restore_tags(self, stage, snapshot):
        media = MFile(stage)
        kind = snapshot['kind']
        if kind == 'id3':
            source = (self.snapshots / snapshot['id3']).resolve()
            if source.parent != self.snapshots: raise HTTPException(400,'历史路径无效')
            restored = ID3(str(source))
            if media.tags is None: media.add_tags()
            media.tags.clear()
            media.tags.update(restored)
            media.tags.unknown_frames = restored.unknown_frames
            media.save(v2_version=snapshot.get('id3_version',4))
        elif kind == 'flac':
            media.clear()
            for key,value in _decode(snapshot['values']).items(): media[key] = value
            media.clear_pictures()
            for value in snapshot['pictures']: media.add_picture(Picture(base64.b64decode(value)))
            media.save()
        elif snapshot.get('values') is not None:
            if media.tags is None: media.add_tags()
            media.tags.clear()
            media.tags.update(_decode(snapshot['values']))
            media.save()
        elif media.tags is not None:
            media.delete()

    @staticmethod
    def set_cover(stage, data, mime):
        media = MFile(stage)
        if media.tags is None and Path(stage).suffix.lower() == '.mp3': media.add_tags()
        if isinstance(media.tags, ID3):
            kept = [frame for frame in media.tags.getall('APIC') if frame.type != 3]
            media.tags.delall('APIC')
            for frame in kept: media.tags.add(frame)
            media.tags.add(APIC(encoding=3,mime=mime,type=3,desc='Front cover',data=data))
            media.save()
        elif isinstance(media, FLAC):
            kept = [pic for pic in media.pictures if pic.type != 3]
            media.clear_pictures()
            for pic in kept: media.add_picture(pic)
            pic = Picture(); pic.type=3; pic.mime=mime; pic.data=data
            if mime == 'image/png' and len(data)>=24: pic.width,pic.height=struct.unpack('>II',data[16:24])
            media.add_picture(pic); media.save()
        else: raise HTTPException(400,'该格式暂不支持安全封面写入')

    def target(self, source, filename):
        name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', (filename or '').strip()).strip(' .')
        if not name: return source
        ext = source.suffix.lower()
        if Path(name).suffix.lower() != ext: name = Path(name).stem + source.suffix
        if len(name.encode('utf-8')) > 240: raise HTTPException(400,'文件名过长')
        target = root_path(self.media, (source.parent / name).relative_to(self.media).as_posix())
        if target != source and target.exists(): raise HTTPException(409,'文件名已存在；标签未修改')
        return target

    @staticmethod
    def preserve_owner(stage, source):
        st = source.stat()
        if hasattr(os,'chown'):
            cur = stage.stat()
            if (cur.st_uid,cur.st_gid) != (st.st_uid,st.st_gid): os.chown(stage,st.st_uid,st.st_gid)
        os.chmod(stage,st.st_mode & 0o7777)

    @staticmethod
    def fsync(path):
        with Path(path).open('rb') as f: os.fsync(f.fileno())

    def temporary(self, source, prefix):
        fd,name = tempfile.mkstemp(prefix=prefix, suffix=source.suffix, dir=str(source.parent))
        os.close(fd)
        return Path(name)

    def hold(self, source):
        target = self.temporary(source,'.mtl-rollback-')
        target.unlink()
        try: os.link(source,target)
        except OSError: shutil.copy2(source,target)
        return target

    def apply(self, path, changes=None, filename='', expected=None, cover=None, external_lrc=Ellipsis, restore=None, kind='tags'):
        with self.lock:
            source = self.path(path)
            before = self.version(source)
            if expected and expected != before: raise HTTPException(409,'文件在预览后已变化，请重新读取或生成预览')
            original_tags = self.read_tags(source)
            changes = {k:str(v or '') for k,v in (changes or {}).items() if k in FIELDS and str(v or '') != str(original_tags.get(k) or '')}
            unsupported = set(changes)-set(self.capabilities(source)['fields'])
            if unsupported: raise HTTPException(400,'该格式暂不支持这些字段：'+', '.join(sorted(unsupported)))
            target = self.target(source,filename)
            source_lrc = self.sidecar(source)
            target_lrc = self.sidecar(target)
            old_lrc = self.lrc_bytes(source)
            new_lrc = old_lrc if external_lrc is Ellipsis else external_lrc
            if new_lrc is not None and len(new_lrc)>MAX_LRC: raise HTTPException(413,'外置歌词不能超过 2 MiB')
            if target_lrc != source_lrc and target_lrc.exists(): raise HTTPException(409,'目标同名歌词文件已存在；音乐未修改')
            if not changes and not cover and target==source and external_lrc is Ellipsis and restore is None:
                return {'ok':True,'path':self.relative(source),'file_version':before,'noop':True}
            if shutil.disk_usage(source.parent).free < source.stat().st_size*2+64*1024*1024:
                raise HTTPException(507,'剩余空间不足以安全暂存本次修改')
            version_id = str(uuid.uuid4())
            snapshot,snapshot_name = self.snapshot(source,version_id)
            file_id = self.store.file(self.relative(source),before,original_tags)
            self.store.prepare_version(version_id,file_id,kind,self.relative(source),self.relative(target),before,original_tags,snapshot_name)
            stage = None; held = None; held_lrc = None; lrc_stage = None
            media_committed = False; lrc_committed = False; predicted_main = None; keep_recovery = False
            try:
                held = self.hold(source)
                if old_lrc is not None: held_lrc = self.hold(source_lrc)
                snapshot['recovery_paths'] = [self.relative(p) for p in (held, held_lrc) if p is not None]
                with gzip.open(self.snapshots / snapshot_name, 'wt', encoding='utf-8') as f: json.dump(snapshot, f, ensure_ascii=False)
                self.fsync(self.snapshots / snapshot_name)
                stage = self.temporary(source,'.mtl-stage-')
                shutil.copy2(source,stage)
                if restore is not None:
                    if audio_identity(source) != restore['audio']: raise HTTPException(409,'音频内容与历史记录不一致，已停止回退')
                    self.restore_tags(stage,restore)
                elif changes:
                    self.write_tags(stage,changes,original_tags)
                if cover: self.set_cover(stage,cover[0],cover[1])
                self.preserve_owner(stage,source)
                self.fsync(stage)
                if audio_identity(stage) != snapshot['audio']: raise HTTPException(500,'音频校验失败；原文件尚未修改')
                if self.version(source) != before: raise HTTPException(409,'写入前文件再次变化；本次未提交')
                if new_lrc is not None and (new_lrc != old_lrc or target_lrc != source_lrc):
                    lrc_stage = self.temporary(target_lrc,'.mtl-lrc-')
                    lrc_stage.write_bytes(new_lrc)
                    if source_lrc.is_file(): self.preserve_owner(lrc_stage,source_lrc)
                    else: self.preserve_owner(lrc_stage,source)
                    self.fsync(lrc_stage)
                predicted_main = self.version(stage,sidecar_override=None)
                after = self.version(stage,sidecar_override=new_lrc)
                after_tags = self.read_tags(stage)
                self.store.stage_version(version_id,after,after_tags)
                if target != source:
                    os.link(stage,target)  # exclusive destination creation; never clobber another file
                    try: source.unlink()
                    except Exception:
                        target.unlink(); raise
                else:
                    os.replace(stage,source); stage=None
                media_committed=True
                if target_lrc != source_lrc:
                    if lrc_stage is not None:
                        os.link(lrc_stage,target_lrc); lrc_committed=True
                    if old_lrc is not None: source_lrc.unlink()
                elif external_lrc is not Ellipsis:
                    if new_lrc is None:
                        if source_lrc.exists(): source_lrc.unlink()
                    elif lrc_stage is not None:
                        os.replace(lrc_stage,source_lrc); lrc_stage=None
                    lrc_committed=True
                actual = self.version(target)
                if actual != after: raise RuntimeError('提交后的版本校验不一致')
                self.store.relocate(file_id,self.relative(target),actual,after_tags)
                self.store.finish_version(version_id)
                return {'ok':True,'path':self.relative(target),'file_version':actual,'history_id':version_id,'fields_changed':list(changes),'renamed':target!=source}
            except Exception as error:
                rollback_ok=True
                if media_committed:
                    try:
                        if not target.exists() or self.version(target,sidecar_override=None) != predicted_main:
                            raise RuntimeError('提交后的文件被外部修改，不能盲目覆盖')
                        if target != source:
                            if source.exists(): raise RuntimeError('原名称已被其他文件占用')
                            os.link(held,source); target.unlink()
                        else:
                            os.replace(held,source); held=None
                        if target_lrc != source_lrc:
                            if lrc_committed and target_lrc.is_file():
                                if target_lrc.read_bytes()!=new_lrc: raise RuntimeError('歌词被外部修改')
                                target_lrc.unlink()
                            if old_lrc is not None and not source_lrc.exists(): os.link(held_lrc,source_lrc)
                        elif lrc_committed:
                            if held_lrc is not None: os.replace(held_lrc,source_lrc); held_lrc=None
                            elif source_lrc.exists(): source_lrc.unlink()
                        self.store.relocate(file_id,self.relative(source),self.version(source),original_tags)
                    except Exception:
                        rollback_ok=False
                keep_recovery = not rollback_ok
                recovery_note = '' if rollback_ok else '；保留事务临时原件：' + ', '.join(snapshot.get('recovery_paths', []))
                self.store.finish_version(version_id,'failed' if rollback_ok else 'partial',clean_error(error) + recovery_note)
                if not rollback_ok:
                    if target.exists():
                        try: self.store.relocate(file_id,self.relative(target),self.version(target),self.read_tags(target))
                        except Exception: pass
                    raise HTTPException(500, {'message':'发生部分失败，请到修改记录核查；未自动覆盖外部更改','history_id':version_id,'partial':True})
                if isinstance(error,HTTPException): raise
                raise HTTPException(500,'本次未完成，原状态已保留或恢复：'+clean_error(error))
            finally:
                for temporary in (stage,held,held_lrc,lrc_stage):
                    if keep_recovery and temporary in (held, held_lrc): continue
                    if temporary is not None:
                        try: temporary.unlink(missing_ok=True)
                        except OSError: pass

    def restore_preview(self, version_id):
        row = self.store.version(version_id)
        if row['status'] not in ('completed','partial'): raise HTTPException(400,'该记录没有可回退的已提交修改')
        source = self.path(row['current_path'])
        snapshot = self.load_snapshot(row['snapshot'])
        if audio_identity(source) != snapshot['audio']: raise HTTPException(409,'音频内容已被替换，不能套用旧标签')
        current = self.read_tags(source)
        target_tags = snapshot['tags']
        changes = [{'key':k,'before':str(current.get(k) or ''),'after':str(target_tags.get(k) or '')} for k in FIELDS if str(current.get(k) or '')!=str(target_tags.get(k) or '')]
        return {'history_id':version_id,'path':self.relative(source),'target_path':snapshot['path'],'file_version':self.version(source),'changes':changes,'restore_cover':True,'restore_external_lrc':True}

    def restore(self, version_id, expected):
        if not expected: raise HTTPException(400,'回退必须提供当前版本')
        row = self.store.version(version_id)
        if row['status'] not in ('completed','partial'): raise HTTPException(400,'记录不可回退')
        snapshot = self.load_snapshot(row['snapshot'])
        source = self.path(row['current_path'])
        if source.parent != self.path(row['current_path']).parent: raise HTTPException(400,'路径无效')
        old_name = Path(snapshot['path']).name
        old_lrc = None if snapshot['external_lrc'] is None else base64.b64decode(snapshot['external_lrc'])
        return self.apply(self.relative(source),filename=old_name,expected=expected,external_lrc=old_lrc,restore=snapshot,kind='restore')

    def recover(self):
        with self.lock:
            for row in self.store.prepared_versions():
                try:
                    after = root_path(self.media,row['after_path'])
                    before = root_path(self.media,row['before_path'])
                    if after.is_file() and row['after_version'] and self.version(after)==row['after_version']:
                        self.store.relocate(row['file_id'],row['after_path'],row['after_version'],row['after_tags'])
                        self.store.finish_version(row['id'],'completed','服务重启后已验证完成的提交。')
                    elif before.is_file() and self.version(before)==row['before_version']:
                        self.store.finish_version(row['id'],'failed','服务重启；没有发现已提交修改。')
                    else:
                        self.store.finish_version(row['id'],'partial','服务重启时操作未完全落账，请核查后手动恢复。')
                except Exception as e:
                    self.store.finish_version(row['id'],'partial',clean_error(e))
