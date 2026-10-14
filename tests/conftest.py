"""本地测试公共设施：合成音频样本 + 隔离的音乐库/数据目录。

注意：这里的 FLAC 是「结构合法、元数据完整、体积很小」的合成文件，
只在本沙箱的临时目录里创建，永远不会写入真实音乐库。
"""
import os
import struct
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / 'backend'
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def make_flac(path, mark=b'\x00' * 16, tags=None):
    """写一个最小可用的 FLAC；mark 改变内容（同尺寸不同内容用得上）。"""
    sample_rate, channels, bps, total = 44100, 2, 16, 0
    bits = (sample_rate << 44) | ((channels - 1) << 41) | ((bps - 1) << 36) | total
    streaminfo = (struct.pack('>HH', 4096, 4096) + b'\x00\x00\x00' + b'\x00\x00\x00'
                  + bits.to_bytes(8, 'big') + mark.ljust(16, b'\x00')[:16])
    header = bytes([0x80]) + len(streaminfo).to_bytes(3, 'big')
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'fLaC' + header + streaminfo)
    if tags:
        from mutagen.flac import FLAC
        audio = FLAC(str(path))
        for key, value in tags.items():
            audio[key] = [value]
        audio.save()
    return path


def write_text(path, text='hello'):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    return path


@pytest.fixture()
def env(tmp_path, monkeypatch):
    media = tmp_path / 'media'
    data = tmp_path / 'data'
    media.mkdir()
    data.mkdir()
    monkeypatch.setenv('MEDIA_DIR', str(media))
    monkeypatch.setenv('DATA_DIR', str(data))
    (media / '未整理' / '待整理').mkdir(parents=True)
    (media / '已整理').mkdir(parents=True)
    return {'media': media, 'data': data, 'inbox': media / '未整理' / '待整理'}
