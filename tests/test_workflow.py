"""Isolated synthetic-container tests; never point this suite at the real media volume."""
import base64
import hashlib
import importlib
import json
import os
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path

BOOT = tempfile.TemporaryDirectory(prefix='mtl-bootstrap-')
os.environ['MEDIA_DIR'] = str(Path(BOOT.name)/'music')
os.environ['DATA_DIR'] = str(Path(BOOT.name)/'data')
Path(os.environ['MEDIA_DIR']).mkdir()
import app
from fastapi import HTTPException
from fastapi.testclient import TestClient
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TXXX, COMM, APIC, USLT
from mtl_store import Store
from mtl_safe import Writer, audio_identity, image_payload
from mtl_jobs import Engine

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aR3sAAAAASUVORK5CYII=')


def mp3(path):
    frame = b'\xff\xfb\x90\x64' + b'\x00'*413
    path.write_bytes(frame*40)
    tags = ID3()
    tags.add(TIT2(encoding=3,text='Old title'))
    tags.add(TPE1(encoding=3,text=['Original artist','Second artist']))
    tags.add(TALB(encoding=3,text='Original album'))
    tags.add(TXXX(encoding=3,desc='ALBUMVERSION',text='Original edition'))
    tags.add(TXXX(encoding=3,desc='KEEP_CUSTOM',text='Do not erase'))
    tags.add(COMM(encoding=3,lang='eng',desc='extra',text='Preserve other comments'))
    tags.add(USLT(encoding=3,lang='jpn',desc='translation',text='Preserve translation'))
    tags.save(path)
    return path


def flac(path):
    stream = b'\x10\x00\x10\x00' + b'\x00'*6 + ((44100<<44)|(1<<41)|(15<<36)|44100).to_bytes(8,'big') + b'\x00'*16
    path.write_bytes(b'fLaC'+b'\x80\x00\x00\x22'+stream+b'\xff\xf8\x69\x00'+bytes(range(256))*8)
    f=FLAC(path)
    f['title']='Old title';f['artist']=['Original artist','Second artist'];f['album']='Original album';f['albumversion']='Original edition';f['CUSTOM_TEST']='Keep this tag';f['lyrics']='[00:01.00]Original line'
    picture=Picture();picture.type=3;picture.mime='image/png';picture.data=PNG;picture.width=1;picture.height=1
    f.add_picture(picture);f.save()
    return path


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='mtl-test-')
        self.root=Path(self.tmp.name)
        self.music=self.root/'music';self.music.mkdir()
        self.data=self.root/'data'
        app.MEDIA=self.music
        app._STORE=Store(self.data)
        app._WRITER=Writer(self.music,app._STORE,app.current_tags,app._write_changed_tags)
        app._ENGINE=Engine(app._STORE,app._run_job_item,workers=2)
        self.store=app._STORE;self.writer=app._WRITER
        self.original_preview=app._preview_one

    def tearDown(self):
        app._ENGINE.stop()
        app._preview_one=self.original_preview
        self.tmp.cleanup()

    def info(self,path):
        return app.get_track(path.name)

    def commit(self,path,**fields):
        return app.workflow_commit(app.CommitIn(path=path.name,expected_version=self.info(path)['file_version'],**fields))

    def assert_http(self,code,call):
        with self.assertRaises(HTTPException) as caught: call()
        self.assertEqual(caught.exception.status_code,code)

    def test_album_version_is_not_overwritten_by_token(self):
        p=mp3(self.music/'one.mp3');t=self.info(p)
        self.assertEqual(t['version'],'Original edition')
        self.assertEqual(len(t['file_version']),64)
        self.assertEqual(set(app._FIELDS)-set(t),set())

    def test_prefix_sibling_escape_rejected(self):
        other=self.root/'music-backup';other.mkdir();mp3(other/'private.mp3')
        self.assert_http(400,lambda:self.writer.path('../music-backup/private.mp3'))
        self.assert_http(400,lambda:app.rel_ok('../music-backup/private.mp3'))

    def test_symlink_escape_rejected(self):
        other=mp3(self.root/'outside.mp3')
        (self.music/'alias.mp3').symlink_to(other)
        self.assert_http(400,lambda:self.writer.path('alias.mp3'))

    def test_external_lrc_symlink_escape_rejected(self):
        p=mp3(self.music/'one.mp3');outside=self.root/'outside.lrc';outside.write_text('private content')
        p.with_suffix('.lrc').symlink_to(outside)
        self.assert_http(400,lambda:self.writer.lrc_bytes(p))

    def test_rename_collision_does_not_change_any_bytes(self):
        p=flac(self.music/'one.flac');flac(self.music/'exists.flac')
        before=p.read_bytes();version=self.info(p)['file_version']
        self.assert_http(409,lambda:app.workflow_commit(app.CommitIn(path=p.name,title='Changed',filename='exists.flac',expected_version=version)))
        self.assertEqual(p.read_bytes(),before)
        self.assertEqual(self.store.versions(),[])

    def test_rename_lrc_collision_is_prevalidated(self):
        p=mp3(self.music/'one.mp3');p.with_suffix('.lrc').write_text('Old LRC')
        (self.music/'new.lrc').write_text('Unrelated LRC')
        before=p.read_bytes();version=self.info(p)['file_version']
        self.assert_http(409,lambda:self.writer.apply(p.name,changes={'title':'Changed'},filename='new.mp3',expected=version))
        self.assertEqual(p.read_bytes(),before)
        self.assertEqual((self.music/'new.lrc').read_text(),'Unrelated LRC')

    def test_stale_version_does_not_write(self):
        p=mp3(self.music/'one.mp3');version=self.info(p)['file_version']
        self.commit(p,title='Another edit')
        after=p.read_bytes()
        self.assert_http(409,lambda:app.save_track(app.SaveIn(path=p.name,title='Stale edit',expected_version=version)))
        self.assertEqual(p.read_bytes(),after)

    def test_legacy_write_without_version_rejected(self):
        p=mp3(self.music/'one.mp3');before=p.read_bytes()
        self.assert_http(400,lambda:app.save_track(app.SaveIn(path=p.name,title='Unsafe')))
        self.assertEqual(p.read_bytes(),before)

    def test_unrelated_mp3_frames_and_multivalues_preserved(self):
        p=mp3(self.music/'one.mp3');audio=audio_identity(p)
        self.commit(p,title='New title')
        tags=ID3(p)
        self.assertEqual(list(tags['TPE1'].text),['Original artist','Second artist'])
        self.assertEqual(tags.getall('COMM')[0].text,['Preserve other comments'])
        self.assertEqual(tags.getall('USLT')[0].text,'Preserve translation')
        self.assertEqual(tags.getall('TXXX:KEEP_CUSTOM')[0].text,['Do not erase'])
        self.assertEqual(audio_identity(p),audio)
        self.assertEqual(app.current_tags(p)['title'],'New title')

    def test_full_flac_fields_roundtrip_and_audio_unchanged(self):
        p=flac(self.music/'one.flac');audio=audio_identity(p)
        fields={'title':'新标题','date':'2020-10-09','genre':'Dance','language':'中文','albumtype':'EP','comment':'注释','description':'描述','version':'Deluxe','track':'3','tracktotal':'12','disc':'1','disctotal':'2','lyrics':'Plain edit','lrc':'[00:01.20]同步歌词'}
        self.commit(p,**fields)
        t=self.info(p)
        for key,value in fields.items():self.assertEqual(t[key],value,key)
        self.assertEqual(FLAC(p)['CUSTOM_TEST'],['Keep this tag'])
        self.assertEqual(FLAC(p)['artist'],['Original artist','Second artist'])
        self.assertEqual(audio_identity(p),audio)

    def test_mp3_history_restores_metadata_and_audio(self):
        p=mp3(self.music/'one.mp3');old=self.info(p);audio=audio_identity(p)
        result=self.commit(p,title='Changed',version='New edition',date='2020-11-27')
        preview=self.writer.restore_preview(result['history_id'])
        self.writer.restore(result['history_id'],preview['file_version'])
        restored=self.info(p)
        self.assertEqual(restored['title'],old['title'])
        self.assertEqual(restored['version'],old['version'])
        self.assertEqual(audio_identity(p),audio)
        self.assertEqual(len(self.store.versions()),2)
        self.assertEqual(self.store.versions()[0]['kind'],'restore')

    def test_flac_history_restores_cover_and_external_lrc(self):
        p=flac(self.music/'one.flac');side=p.with_suffix('.lrc');side.write_bytes(b'\xef\xbb\xbf[00:01.00]Original external lyric')
        original_tags=dict(FLAC(p).tags);original_side=side.read_bytes();audio=audio_identity(p)
        r=self.writer.apply(p.name,changes={'title':'Changed'},expected=self.info(p)['file_version'],external_lrc=b'[00:02.00]New external lyric',cover=(PNG,'image/png'))
        self.writer.restore(r['history_id'],self.info(p)['file_version'])
        self.assertEqual(dict(FLAC(p).tags),original_tags)
        self.assertEqual(side.read_bytes(),original_side)
        self.assertEqual(FLAC(p).pictures[0].data,PNG)
        self.assertEqual(audio_identity(p),audio)

    def test_rename_and_restore_pair_external_lrc(self):
        p=flac(self.music/'one.flac');p.with_suffix('.lrc').write_text('[00:01]line')
        result=self.commit(p,filename='renamed.flac',title='New')
        renamed=self.music/'renamed.flac'
        self.assertFalse(p.exists());self.assertTrue(renamed.exists())
        self.assertFalse((self.music/'one.lrc').exists());self.assertTrue((self.music/'renamed.lrc').exists())
        self.writer.restore(result['history_id'],self.info(renamed)['file_version'])
        self.assertTrue(p.exists());self.assertFalse(renamed.exists())
        self.assertTrue((self.music/'one.lrc').exists());self.assertFalse((self.music/'renamed.lrc').exists())

    def test_undo_detects_replaced_audio_payload(self):
        p=flac(self.music/'one.flac');result=self.commit(p,title='Changed')
        with p.open('ab') as f:f.write(b'new audio content')
        self.assert_http(409,lambda:self.writer.restore_preview(result['history_id']))

    def test_undo_conflict_does_not_override_current_edits(self):
        p=mp3(self.music/'one.mp3');result=self.commit(p,title='Changed')
        preview=self.writer.restore_preview(result['history_id'])
        self.commit(p,title='Later edit')
        self.assert_http(409,lambda:self.writer.restore(result['history_id'],preview['file_version']))
        self.assertEqual(self.info(p)['title'],'Later edit')

    def test_cover_and_tags_commit_together(self):
        p=mp3(self.music/'one.mp3');audio=audio_identity(p)
        r=self.commit(p,title='With cover',cover_b64=base64.b64encode(PNG).decode(),cover_mime='image/png')
        self.assertTrue(self.info(p)['has_cover'])
        self.assertEqual(self.info(p)['title'],'With cover')
        self.assertEqual(len(self.store.versions()),1)
        self.assertEqual(audio_identity(p),audio)

    def test_bad_image_rejected_before_audio_changes(self):
        p=mp3(self.music/'one.mp3');before=p.read_bytes()
        self.assert_http(400,lambda:self.commit(p,title='Do not write',cover_b64=base64.b64encode(b'<svg>not allowed</svg>').decode()))
        self.assertEqual(p.read_bytes(),before)
        self.assertEqual(self.store.versions(),[])

    def test_lrc_export_is_versioned(self):
        p=mp3(self.music/'one.mp3');audio=audio_identity(p)
        result=app.save_lrc_file(app.LrcIn(path=p.name,content='[00:01.00]New lyric',expected_version=self.info(p)['file_version']))
        self.assertEqual(p.with_suffix('.lrc').read_text(),'[00:01.00]New lyric')
        self.writer.restore(result['history_id'],self.info(p)['file_version'])
        self.assertFalse(p.with_suffix('.lrc').exists())
        self.assertEqual(audio_identity(p),audio)

    def test_snapshot_does_not_copy_audio_into_data(self):
        p=mp3(self.music/'one.mp3');self.commit(p,title='Changed')
        snapshot_bytes=sum(f.stat().st_size for f in (self.data/'versions').iterdir())
        self.assertLess(snapshot_bytes,p.stat().st_size)
        self.assertEqual(list(self.music.glob('.mtl-*')),[])

    def test_noop_does_not_create_history(self):
        p=mp3(self.music/'one.mp3')
        result=self.commit(p,title='Old title')
        self.assertTrue(result['noop']);self.assertEqual(self.store.versions(),[])

    def test_wal_and_concurrent_short_writes(self):
        job=self.store.create_job('preview',[{'path':'one.mp3'}],{})
        errors=[]
        def write(index):
            try:
                for n in range(10):self.store.log(job,'thread-%s item-%s'%(index,n))
            except Exception as e:errors.append(str(e))
        threads=[threading.Thread(target=write,args=(i,)) for i in range(8)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual(errors,[])
        self.assertEqual(self.store.health()['journal_mode'],'wal')
        with self.store.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM job_logs WHERE job_id=?',(job,)).fetchone()[0],81)

    def test_jobs_survive_reopening_database(self):
        job=self.store.create_job('preview',[{'path':'one.mp3'}],{'source':'smart'})
        other=Store(self.data)
        self.assertEqual(other.job(job)['options']['source'],'smart')
        self.assertEqual(other.job(job)['total'],1)

    def test_interrupted_writes_are_not_auto_replayed(self):
        job=self.store.create_job('apply',[{'path':'one.mp3','expected_version':'0'*64}],{})
        self.store.recover_jobs()
        self.assertEqual(self.store.job(job)['status'],'interrupted')
        self.assertEqual(self.store.claim_job(),None)

    def test_pending_cancel_never_executes(self):
        job=self.store.create_job('preview',[{'path':'one.mp3'}],{})
        self.store.cancel(job)
        self.assertEqual(self.store.job(job)['status'],'cancelled')
        self.assertEqual(self.store.claim_job(),None)

    def test_real_background_engine_with_mock_metadata_sources(self):
        p=mp3(self.music/'one.mp3');before=p.read_bytes()
        def fake(item,options):
            track=app.get_track(item['path'])
            return {'path':track['path'],'name':track['name'],'original':track,'candidate':{'source':'itunes','title':'Mock candidate','artist':'Test artist'},'candidates':[],'score':90,'lyric':''}
        app._preview_one=fake
        app._ENGINE.start()
        job=app.workflow_create_task(app.WorkflowTaskIn(kind='preview',paths=[p.name],options={'source':'smart'}))['job_id']
        end=time.time()+5
        while time.time()<end and self.store.job(job)['status'] not in ('completed','failed'):time.sleep(.03)
        result=self.store.job(job)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['items'][0]['result']['candidate']['title'],'Mock candidate')
        self.assertEqual(p.read_bytes(),before)

    def test_asgi_confirmation_and_cross_origin_guard(self):
        p=mp3(self.music/'one.mp3')
        client=TestClient(app.app)
        r=client.get('/api/track',params={'path':p.name})
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.json()['version'],'Original edition')
        r=client.post('/api/tasks',json={'kind':'apply','items':[{'path':p.name,'expected_version':self.info(p)['file_version'],'fields':{'title':'No'}}]})
        self.assertEqual(r.status_code,400)
        r=client.post('/api/commit',headers={'Origin':'http://evil.invalid'},json={'path':p.name,'expected_version':self.info(p)['file_version'],'title':'No'})
        self.assertEqual(r.status_code,403)
        self.assertEqual(self.info(p)['title'],'Old title')
        health=client.get('/api/system').json()
        self.assertEqual(health['storage']['journal_mode'],'wal')

    def test_asgi_safe_write_and_restore(self):
        p=flac(self.music/'one.flac');client=TestClient(app.app)
        token=client.get('/api/track',params={'path':p.name}).json()['file_version']
        result=client.post('/api/commit',json={'path':p.name,'expected_version':token,'title':'HTTP edit'})
        self.assertEqual(result.status_code,200,result.text)
        record=result.json()['history_id']
        preview=client.get('/api/history/'+record+'/preview')
        self.assertEqual(preview.status_code,200)
        result=client.post('/api/history/'+record+'/restore',json={'expected_version':preview.json()['file_version'],'confirmed':True})
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(client.get('/api/track',params={'path':p.name}).json()['title'],'Old title')


if __name__=='__main__':
    unittest.main(verbosity=2)
