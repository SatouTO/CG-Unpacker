"""核心数据结构和文件视图"""
import mmap
import os
import struct
from dataclasses import dataclass, field
from typing import Any


class NotThisFormat(Exception):
    """不是我这个格式，交给下一个探测器"""


@dataclass
class Entry:
    name: str
    offset: int
    size: int                 # 在归档里占用的字节数
    unpacked: int = 0         # 解压后的字节数，0 表示未知
    kind: str = ""            # 格式自己用的标签
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def ext(self) -> str:
        return os.path.splitext(self.name)[1].lower()

    @property
    def display_size(self) -> int:
        return self.unpacked or self.size


class FileView:
    """把一个归档文件映射进内存，探测器只管读"""

    def __init__(self, path: str):
        self.path = path
        self._f = open(path, "rb")
        self._mm = mmap.mmap(self._f.fileno(), 0, access=mmap.ACCESS_READ)
        self.size = len(self._mm)

    def close(self):
        try:
            self._mm.close()
        finally:
            self._f.close()

    def bytes(self, off: int, n: int) -> bytes:
        return self._mm[off:off + n]

    def ascii(self, off: int, n: int) -> str:
        return self.bytes(off, n).decode("latin-1")

    def u8(self, off: int) -> int:
        return self._mm[off]

    def u16(self, off: int) -> int:
        return struct.unpack_from("<H", self._mm, off)[0]

    def u32(self, off: int) -> int:
        return struct.unpack_from("<I", self._mm, off)[0]

    def i32(self, off: int) -> int:
        return struct.unpack_from("<i", self._mm, off)[0]

    def u64(self, off: int) -> int:
        return struct.unpack_from("<Q", self._mm, off)[0]


class Archive:
    def __init__(self, view: FileView, fmt: str, entries: list[Entry]):
        self.view = view
        self.path = view.path
        self.format = fmt
        self.entries = entries

    def read(self, entry: Entry) -> bytes:
        segments = entry.meta.get("segments")
        if segments:
            return self._read_segments(segments)
        data = self.view.bytes(entry.offset, entry.size)
        if entry.meta.get("aff"):
            data = unwrap_aff(data)
        return data

    def _read_segments(self, segments) -> bytes:
        import zlib
        parts = []
        for compressed, off, size, packed in segments:
            chunk = self.view.bytes(off, packed if compressed else size)
            if compressed:
                chunk = zlib.decompress(chunk)
            parts.append(chunk)
        return b"".join(parts)

    def close(self):
        self.view.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


class ZipArchive(Archive):
    """zip / 伪装的 zip 走这里"""

    def __init__(self, view, fmt, entries, zf):
        super().__init__(view, fmt, entries)
        self._zf = zf

    def read(self, entry: Entry) -> bytes:
        return self._zf.read(entry.meta["zip_name"])

    def close(self):
        try:
            self._zf.close()
        finally:
            self.view.close()


# ---------------------------------------------------------------- AFF 包装
AFF_KEY = bytes([0xC8, 0xBB, 0x8F, 0xB7, 0xED, 0x43, 0x99, 0x4A,
                 0xA2, 0x7E, 0x5B, 0xB0, 0x68, 0x18, 0xF8, 0x88])


def unwrap_aff(data: bytes) -> bytes:
    """AliceSoft 的 AFF\\0 加密包装：扔掉 0x10 字节头，再把前 0x40 字节异或开"""
    if len(data) <= 0x10 or data[:4] != b"AFF\x00":
        return data
    body = bytearray(data[0x10:])
    for i in range(min(0x40, len(body))):
        body[i] ^= AFF_KEY[i & 0xF]
    return bytes(body)


# ---------------------------------------------------------------- 文本解码
_HALF_KANA = range(0xFF61, 0xFFA0)


def _score(text: str) -> int:
    s = 0
    for ch in text:
        o = ord(ch)
        if o in _HALF_KANA:
            s -= 4
        elif 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF:
            s += 2
        elif 0x3040 <= o <= 0x30FF:
            s += 1
        elif 0xFF01 <= o <= 0xFF60:
            s += 1
        elif o < 0x80:
            s += 1
        else:
            s -= 1
    return s


def smart_decode(raw: bytes) -> str:
    """日文/中文补丁包混在一起，选一个最像人话的编码"""
    best, best_score = "", -10 ** 9
    for enc in ("utf-8", "gbk", "cp932", "euc_jp", "latin-1"):
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        if enc == "latin-1":
            break  # 兜底放最后
        sc = _score(text)
        if sc > best_score:
            best, best_score = text, sc
    return best
