"""AliceSoft ALD 归档（老引擎的单文件资源包）"""
from ..core import Entry, smart_decode
from ..registry import register_archive

ALD_VERSIONS = (0x014C4E, 0x012020)   # "NL" / "  "


@register_archive("ALD", "AliceSoft ALD (System 3/4)")
def detect(view):
    if view.size < 0x30:
        return None
    tail = view.size - 0x10
    if view.u32(tail) not in ALD_VERSIONS:
        return None
    if view.u32(tail + 4) != 0x10:
        return None
    count = view.u16(tail + 9)
    if count == 0 or count > 0x4000:
        return None

    offsets = []
    pos = 3
    for _ in range(count):
        if pos + 4 > view.size:
            break
        off = (view.u32(pos) & 0xFFFFFF) << 8
        if off == 0:
            break
        if off >= view.size:
            return None
        offsets.append(off)
        pos += 3

    entries: list[Entry] = []
    for off in offsets:
        if off + 0x10 > view.size:
            return None
        header_size = view.u32(off)
        if header_size <= 0x10 or header_size > 0x10000 or off + header_size > view.size:
            return None
        size = view.u32(off + 4)
        raw_name = view.bytes(off + 0x10, header_size - 0x10)
        name = smart_decode(raw_name.rstrip(b"\x00"))
        if not name:
            continue
        if off + header_size + size > view.size:
            continue
        entries.append(Entry(name, off + header_size, size))
    return entries or None
