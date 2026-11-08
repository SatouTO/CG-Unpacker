"""AliceSoft AFA 归档 (AFAH / AlicArch)，支持 v1 / v2 / v3"""
import zlib

from ..core import Entry, NotThisFormat, smart_decode
from ..registry import register_archive

MAGIC = b"AFAH"


class MsbBitStream:
    """GARbro MsbBitStream 的 Python 版（高位在前）"""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0
        self.buf = 0
        self.bits = 0

    def get_bits(self, n: int) -> int:
        while self.bits < n:
            if self.pos >= len(self.data):
                return -1
            self.buf = (self.buf << 8) | self.data[self.pos]
            self.bits += 8
            self.pos += 1
        self.bits -= n
        v = (self.buf >> self.bits) & ((1 << n) - 1)
        self.buf &= (1 << self.bits) - 1
        return v

    def get_next_bit(self) -> int:
        return self.get_bits(1)


class RandomGenerator:
    """AFA v3 索引混淆用的随机数发生器，照搬 GARbro"""

    def __init__(self, seed: int):
        self.state = [0] * 521
        self.current = -1
        self._init(seed)

    def _init(self, seed: int):
        val = 0
        for i in range(17):
            for _ in range(32):
                seed = (1566083941 * seed + 1) & 0xFFFFFFFF
                val = (seed & 0x80000000) | (val >> 1)
            self.state[i] = val
        s = self.state
        s[16] = (s[15] ^ (s[0] >> 9) ^ ((s[16] << 23) & 0xFFFFFFFF)) & 0xFFFFFFFF
        for i in range(17, 521):
            s[i] = (s[i - 1] ^ (s[i - 16] >> 9) ^ ((s[i - 17] << 23) & 0xFFFFFFFF)) & 0xFFFFFFFF
        for _ in range(4):
            self._shuffle()

    def _shuffle(self):
        s = self.state
        for i in range(0, 32, 4):
            s[i] ^= s[i + 489]
            s[i + 1] ^= s[i + 490]
            s[i + 2] ^= s[i + 491]
            s[i + 3] ^= s[i + 492]
        for i in range(32, 519, 3):
            s[i] ^= s[i - 32]
            s[i + 1] ^= s[i - 31]
            s[i + 2] ^= s[i - 30]
        s = [x & 0xFFFFFFFF for x in s]
        self.state = s

    def next(self) -> int:
        self.current += 1
        if self.current >= 521:
            self._shuffle()
            self.current = 0
        return self.state[self.current]


def _read_int32(bits: MsbBitStream) -> int:
    b0 = bits.get_bits(8)
    b1 = bits.get_bits(8)
    b2 = bits.get_bits(8)
    b3 = bits.get_bits(8)
    if -1 in (b0, b1, b2, b3):
        raise NotThisFormat("AFA v3 索引读取越界")
    return b3 << 24 | b2 << 16 | b1 << 8 | b0


def _read_bytes(bits: MsbBitStream) -> bytes | None:
    size = _read_int32(bits)
    if size < 0 or size > 0x100000:
        return None
    out = bytearray(size)
    rnd = RandomGenerator(size)
    dst = 0
    while dst < size:
        count = rnd.next() & 3
        skipped = bits.get_bits(count + 1)
        if skipped == -1:
            return None
        rnd.next()
        v = bits.get_bits(8)
        if v == -1:
            return None
        out[dst] = v
        dst += 1
    return bytes(out)


def _read_encrypted_chars(bits: MsbBitStream) -> list[int] | None:
    size = _read_int32(bits)
    if size <= 0 or size > 0x1000:
        return None
    out = [0] * size
    rnd = RandomGenerator(size)
    dst = 0
    while dst < size:
        count = rnd.next() & 3
        skipped = bits.get_bits(count + 1)
        if skipped == -1:
            return None
        rnd.next()
        lo = bits.get_bits(8)
        hi = bits.get_bits(8)
        if lo == -1 or hi == -1:
            return None
        out[dst] = lo | (hi << 8)
        dst += 1
    return out


def _v3_entries(view) -> list[Entry] | None:
    index_size = view.u32(4)
    data_offset = index_size + 8
    if data_offset <= 12 or data_offset > view.size:
        return None
    bits = MsbBitStream(view.bytes(12, data_offset - 12))
    bits.get_next_bit()
    table = _read_bytes(bits)
    if table is None:
        return None
    packed_size = _read_int32(bits)
    _read_int32(bits)                      # unpacked size
    packed = bytearray(packed_size)
    for i in range(packed_size):
        v = bits.get_bits(8)
        if v == -1:
            return None
        packed[i] = v
    try:
        index = zlib.decompress(bytes(packed))
    except zlib.error:
        return None

    ibits = MsbBitStream(index)
    ibits.get_next_bit()
    count = _read_int32(ibits)
    if count <= 0 or count > 0x100000:
        return None

    entries: list[Entry] = []
    for _ in range(count):
        if ibits.get_bits(2) == -1:
            break
        name_buf = _read_encrypted_chars(ibits)
        if name_buf is None:
            break
        name = smart_decode(bytes((table[b] ^ 0xA4) for b in name_buf))
        _read_int32(ibits)
        _read_int32(ibits)
        off = _read_int32(ibits) + data_offset
        size = _read_int32(ibits)
        if off <= 0 or size < 0 or off + size > view.size:
            continue
        entries.append(Entry(name, off, size, meta={"aff": True}))
    return entries or None


def _v12_entries(view) -> list[Entry] | None:
    if view.ascii(0x1C, 4) != "INFO":
        return None
    version = view.i32(0x10)
    base_offset = view.u32(0x18)
    packed_size = view.u32(0x20)
    count = view.i32(0x28)
    if count <= 0 or count > 0x100000 or packed_size > view.size:
        return None
    try:
        index = zlib.decompress(view.bytes(0x2C, 0x2C + packed_size))
    except zlib.error:
        return None

    entries: list[Entry] = []
    pos = 0
    for _ in range(count):
        if pos + 8 > len(index):
            break
        name_length, index_step = (int.from_bytes(index[pos:pos + 4], "little", signed=True),
                                   int.from_bytes(index[pos + 4:pos + 8], "little", signed=True))
        pos += 8
        if name_length <= 0 or index_step < name_length or pos + index_step > len(index):
            break
        raw_name = index[pos:pos + name_length]
        pos += index_step
        pos += 8                       # 两个未知字段
        if version < 2:
            pos += 4
        if pos + 8 > len(index):
            break
        off = int.from_bytes(index[pos:pos + 4], "little") + base_offset
        size = int.from_bytes(index[pos + 4:pos + 8], "little")
        pos += 8
        if size <= 0 or off + size > view.size:
            continue
        entries.append(Entry(smart_decode(raw_name), off, size, meta={"aff": True}))
    return entries or None


@register_archive("AFA", "AliceSoft AFA (v1/v2/v3)")
def detect(view):
    if view.size < 0x30 or view.bytes(0, 4) != MAGIC:
        return None
    if view.bytes(8, 8) == b"AlicArch":
        return _v12_entries(view)
    if view.i32(8) == 3:
        return _v3_entries(view)
    return None
