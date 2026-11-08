"""NScripter 的 YPF 归档

结构很简单：
    0x00  "YPF0" (4)
    0x04  u32 条目数
    0x08  每个条目 0x48 字节：
            0x00 文件名（0x40 字节，cp932，补 0）
            0x40 u32 偏移
            0x44 u32 大小

NScripter 的 .sar / .nsa 是另一套（带压缩和加密），这里只处理 YPF。
"""
import struct

from ..core import Entry, smart_decode
from ..registry import register_archive

MAGICS = (b"YPF0", b"YPF\x00")


def _parse(view):
    if view.bytes(0, 4) not in MAGICS:
        return None
    if view.size < 8:
        return None
    count = view.u32(4)
    if count <= 0 or count > 0x100000:
        return None
    index_end = 8 + count * 0x48
    if index_end > view.size:
        return None

    entries: list[Entry] = []
    pos = 8
    for _ in range(count):
        raw_name = view.bytes(pos, 0x40).split(b"\x00", 1)[0]
        off = view.u32(pos + 0x40)
        size = view.u32(pos + 0x44)
        pos += 0x48
        if not raw_name or size == 0:
            continue
        if off + size > view.size:
            continue
        try:
            name = raw_name.decode('cp932')
        except UnicodeDecodeError:
            name = smart_decode(raw_name)
        entries.append(Entry(name, off, size))
    return entries or None


@register_archive("YPF", "NScripter YPF")
def detect(view):
    return _parse(view)


if __name__ == "__main__":
    import io
    import os
    import tempfile

    from ..core import FileView

    names = ["cg\\title.png", "bgm\\theme.ogg", "顔\\笑顔.bmp"]
    payloads = [b"PNGDATA-1", b"OGGDATA-2", b"BMPDATA-3"]

    buf = io.BytesIO()
    buf.write(b"YPF0")
    buf.write(struct.pack("<I", len(names)))
    offset = 8 + len(names) * 0x48
    offsets = []
    for p in payloads:
        offsets.append(offset)
        offset += len(p)
    for n, off, p in zip(names, offsets, payloads):
        raw = n.encode("cp932").ljust(0x40, b"\x00")[:0x40]
        buf.write(raw)
        buf.write(struct.pack("<II", off, len(p)))
    for p in payloads:
        buf.write(p)

    tmp = os.path.join(os.path.dirname(__file__), "..", "..", "_ypf_test.bin")
    tmp = os.path.abspath(tmp)
    with open(tmp, "wb") as f:
        f.write(buf.getvalue())

    fv = FileView(tmp)
    try:
        result = detect(fv)
        print("entries:", [(e.name, e.offset, e.size) for e in result])
        assert result and len(result) == 3, result
        assert result[0].name == "cg\\title.png", result[0].name
        assert fv.bytes(result[2].offset, result[2].size) == b"BMPDATA-3"
        print("YPF self-test OK")
    finally:
        fv.close()
        os.remove(tmp)
