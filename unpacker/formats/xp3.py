"""KiriKiri XP3 归档（只处理未加密的索引，加密包会给出提示）"""
import zlib

from ..core import Entry, NotThisFormat
from ..registry import register_archive

MAGIC = b"XP3\r\n \n\x1a\x8b\x67\x01"


class _Reader:
    def __init__(self, data: bytes):
        self.d = data
        self.p = 0

    def u8(self):
        v = self.d[self.p]
        self.p += 1
        return v

    def u16(self):
        v = int.from_bytes(self.d[self.p:self.p + 2], "little")
        self.p += 2
        return v

    def u32(self):
        v = int.from_bytes(self.d[self.p:self.p + 4], "little")
        self.p += 4
        return v

    def u64(self):
        v = int.from_bytes(self.d[self.p:self.p + 8], "little")
        self.p += 8
        return v

    def utf16(self, n: int) -> str:
        raw = self.d[self.p:self.p + n * 2]
        self.p += n * 2
        try:
            return raw.decode("utf-16-le")
        except UnicodeDecodeError:
            return raw.decode("utf-16-le", "replace")

    def eof(self) -> bool:
        return self.p >= len(self.d)


@register_archive("XP3", "KiriKiri XP3")
def detect(view):
    base = 0
    if view.bytes(0, 11) != MAGIC:
        return None
    dir_off = base + view.u64(base + 0x0B)
    if dir_off < 0x13 or dir_off >= view.size:
        raise NotThisFormat("XP3 索引偏移不合法")
    if view.u32(dir_off) == 0x80:
        dir_off = base + view.u64(dir_off + 9)
    if dir_off < 0x13 or dir_off >= view.size:
        raise NotThisFormat("XP3 v2 索引偏移不合法")

    kind = view.u8(dir_off)
    if kind == 0:
        size = view.u64(dir_off + 1)
        index = view.bytes(dir_off + 9, size)
    elif kind == 1:
        packed = view.u64(dir_off + 1)
        view.u64(dir_off + 9)          # unpacked size
        try:
            index = zlib.decompress(view.bytes(dir_off + 17, packed))
        except zlib.error:
            raise NotThisFormat("XP3 索引解压失败（可能被加密）")
    else:
        raise NotThisFormat("XP3 索引类型未知")

    r = _Reader(index)
    entries: list[Entry] = []
    while not r.eof():
        if r.p + 12 > len(index):
            break
        sig = r.u32()
        size = r.u64()
        if size < 0 or r.p + size > len(index):
            break
        end = r.p + size
        if sig == 0x656C6946:                       # "File"
            name = ""
            stored = unpacked = 0
            encrypted = False
            segments = []
            while r.p + 12 <= end:
                sect = r.u32()
                ssize = r.u64()
                nxt = r.p + ssize
                if nxt > end:
                    nxt = end
                if sect == 0x6F666E69:              # "info"
                    encrypted = r.u32() != 0
                    unpacked = r.u64()
                    stored = r.u64()
                    if r.p + 2 <= nxt:
                        n = r.u16()
                        if n <= 0x200 and r.p + n * 2 <= nxt:
                            name = r.utf16(n)
                elif sect == 0x6D676573:            # "segm"
                    n = ssize // 0x1C
                    for _ in range(n):
                        compressed = r.u32() != 0
                        off = r.u64()
                        seg_size = r.u64()
                        packed_size = r.u64()
                        segments.append((compressed, off, seg_size, packed_size))
                r.p = nxt
            if segments and name:
                first = segments[0]
                total_stored = sum(s[3] if s[0] else s[2] for s in segments)
                entries.append(Entry(
                    name, first[1], total_stored, unpacked,
                    meta={"segments": segments, "encrypted": encrypted},
                ))
        r.p = end
    return entries or None
