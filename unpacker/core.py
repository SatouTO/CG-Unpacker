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

    def __init__(self, path: str, *, should_stop=None):
        self.path = path
        self.should_stop = should_stop
        self._f = open(path, "rb")
        try:
            if os.fstat(self._f.fileno()).st_size == 0:      # 空文件不能 mmap
                self._mm = b""
            else:
                self._mm = mmap.mmap(self._f.fileno(), 0, access=mmap.ACCESS_READ)
        except Exception:
            self._f.close()
            raise
        self.size = len(self._mm)

    def close(self):
        try:
            if not isinstance(self._mm, bytes):
                self._mm.close()
        finally:
            self._f.close()

    def bytes(self, off: int, n: int) -> bytes:
        return self._mm[off:off + n]

    def buffer(self) -> memoryview:
        """用 with 在映射上搜索大文件，离开后立即释放视图。"""
        return memoryview(self._mm)

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
            parts = []
            for compressed, off, size, packed in segments:
                chunk = self.view.bytes(off, packed if compressed else size)
                chunk = self._post(entry, chunk)
                if compressed:
                    import zlib
                    chunk = zlib.decompress(chunk)
                parts.append(chunk)
            return b"".join(parts)
        return self._post(entry, self.view.bytes(entry.offset, entry.size))

    def _post(self, entry: Entry, data: bytes) -> bytes:
        if entry.meta.get("aff"):
            data = unwrap_aff(data)
        key = entry.meta.get("xor_key")
        if key:
            data = xor_bytes(data, key)
        return data

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
        return self._zf.read(entry.meta.get("zip_info") or entry.meta["zip_name"])

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


def xor_bytes(data: bytes, key: bytes) -> bytes:
    """循环异或（pf8 这类用它加密正文），用大整数一次算完，比逐字节快得多"""
    if not data or not key:
        return data
    n = len(data)
    full = (key * (n // len(key) + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(full, "big")).to_bytes(n, "big")


# ---------------------------------------------------------------- 文本解码
_HALF_KANA = range(0xFF61, 0xFFA0)


def _score(text: str) -> int:
    """给一段解码结果打分：假名权重最高，因为把 SJIS 当 GBK 解出来的全是生僻汉字"""
    s = 0
    for ch in text:
        o = ord(ch)
        if o in _HALF_KANA:              # 半角假名：基本可以断定解错了
            s -= 6
        elif 0x3041 <= o <= 0x3096:      # 平假名
            s += 4
        elif 0x30A1 <= o <= 0x30FA:      # 全角片假名
            s += 3
        elif 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF:
            s += 1
        elif 0xFF01 <= o <= 0xFF60:      # 全角符号
            s += 1
        elif o < 0x80:                   # ASCII
            s += 1
        else:
            s -= 2
    return s


def smart_decode(raw: bytes) -> str:
    """日文/中文补丁混在一起，挑一个最像人话的编码

    规则：
      1. 能严格按 UTF-8 解出来且带非 ASCII 字符 → 就是 UTF-8（这个几乎不会误判）
      2. 剩下的按字符得分比，同分时优先 cp932（galgame 里日文名远多于中文名）
    """
    candidates = []
    for enc in ("cp932", "gbk", "euc_jp"):
        try:
            candidates.append((enc, raw.decode(enc)))
        except UnicodeDecodeError:
            continue

    try:
        text = raw.decode("utf-8")
        if any(ord(c) > 127 for c in text):
            return text
    except UnicodeDecodeError:
        pass

    if not candidates:
        return raw.decode("latin-1")

    best = max(candidates, key=lambda kv: _score(kv[1]))
    return best[1]
