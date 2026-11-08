"""KiriKiri XP3 归档（只处理未加密的索引，加密包会给出提示）"""
import zlib

from ..core import Entry, NotThisFormat
from ..registry import register_archive

MAGIC = b"XP3\r\n \n\x1a\x8b\x67\x01"
MAX_INDEX_BYTES = 256 * 1024 * 1024


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


def _find_in_exe(view, magic: bytes) -> int:
    """有些 KiriKiri 游戏把 xp3 直接塞进 exe 里，往前找魔数"""
    step = 1 << 20
    pos = 0
    overlap = len(magic) - 1
    while pos < view.size:
        chunk = view.bytes(pos, min(step, view.size - pos))
        idx = chunk.find(magic)
        if idx >= 0:
            return pos + idx
        if len(chunk) <= overlap:
            break
        pos += len(chunk) - overlap
    return -1


@register_archive("XP3", "KiriKiri XP3")
def detect(view):
    base = 0
    if view.size < 0x13:
        return None
    if view.bytes(0, 2) == b"MZ":
        base = _find_in_exe(view, MAGIC)
        if base < 0:
            return None
    elif view.bytes(0, 11) != MAGIC:
        return None
    dir_off = base + view.u64(base + 0x0B)
    if not base + 0x13 <= dir_off <= view.size - 9:
        raise NotThisFormat("XP3 索引偏移不合法")
    if view.u32(dir_off) == 0x80:
        if dir_off + 17 > view.size:
            raise NotThisFormat("XP3 v2 索引指针不完整")
        dir_off = base + view.u64(dir_off + 9)
        if not base + 0x13 <= dir_off <= view.size - 9:
            raise NotThisFormat("XP3 v2 索引偏移不合法")

    kind = view.u8(dir_off)
    if kind == 0:
        size = view.u64(dir_off + 1)
        if size > MAX_INDEX_BYTES or size > view.size - dir_off - 9:
            raise NotThisFormat("XP3 索引过大或不完整")
        index = view.bytes(dir_off + 9, size)
    elif kind == 1:
        if dir_off + 17 > view.size:
            raise NotThisFormat("XP3 压缩索引头不完整")
        packed = view.u64(dir_off + 1)
        expected = view.u64(dir_off + 9)
        if (expected > MAX_INDEX_BYTES or packed > MAX_INDEX_BYTES
                or packed > view.size - dir_off - 17):
            raise NotThisFormat("XP3 压缩索引过大或不完整")
        try:
            inflater = zlib.decompressobj()
            index = inflater.decompress(view.bytes(dir_off + 17, packed), expected + 1)
        except zlib.error:
            raise NotThisFormat("XP3 索引解压失败（可能被加密）")
        if len(index) != expected or not inflater.eof:
            raise NotThisFormat("XP3 索引解压长度不正确")
    else:
        raise NotThisFormat("XP3 索引类型未知")

    r = _Reader(index)
    entries: list[Entry] = []
    while not r.eof():
        if r.p + 12 > len(index):
            raise NotThisFormat("XP3 索引块头不完整")
        sig = r.u32()
        size = r.u64()
        if size > len(index) - r.p:
            raise NotThisFormat("XP3 索引块超出索引边界")
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
                    raise NotThisFormat("XP3 文件子块超出索引边界")
                if sect == 0x6F666E69:              # "info"
                    if ssize < 22:
                        raise NotThisFormat("XP3 文件信息不完整")
                    encrypted = r.u32() != 0
                    unpacked = r.u64()
                    stored = r.u64()
                    n = r.u16()
                    if n * 2 > nxt - r.p:
                        raise NotThisFormat("XP3 文件名不完整")
                    if n <= 0x200:
                        name = r.utf16(n)
                elif sect == 0x6D676573:            # "segm"
                    if ssize % 0x1C:
                        raise NotThisFormat("XP3 数据段记录不完整")
                    n = ssize // 0x1C
                    for _ in range(n):
                        compressed = r.u32() != 0
                        off = base + r.u64()
                        seg_size = r.u64()
                        packed_size = r.u64()
                        stored_size = packed_size if compressed else seg_size
                        if off > view.size or stored_size > view.size - off:
                            raise NotThisFormat("XP3 数据段超出归档边界")
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
