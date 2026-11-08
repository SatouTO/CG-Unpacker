"""zip 归档（顺带支持伪装成 zip 的资源包，很多同人游戏爱用）"""
import zipfile

from ..core import Entry, ZipArchive
from ..registry import register_archive


@register_archive("ZIP", "ZIP / 伪 ZIP")
def detect(view):
    if view.bytes(0, 4) not in (b"PK\x03\x04", b"PK\x05\x06"):
        return None
    try:
        zf = zipfile.ZipFile(view.path)
    except zipfile.BadZipFile:
        return None
    entries = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        entries.append(Entry(
            info.filename, 0, info.compress_size,
            unpacked=info.file_size,
            meta={"zip_name": info.filename},
        ))
    if not entries:
        zf.close()
        return None
    return ZipArchive(view, "ZIP", entries, zf)
