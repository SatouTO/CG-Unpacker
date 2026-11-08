r"""Artemis 引擎 PFS 归档（pf6 / pf7 / pf8，外加老式的 pf2）

格式规范照抄 GARbro 的 ArcFormats/Artemis/ArcPFS.cs（morkt/GARbro）：

    0x00  3  "pf6" / "pf7" / "pf8"
    0x03  4  索引大小（从 0x07 数起，包含文件数那一项）
    0x07  4  文件数
    0x0B      条目数组，每条：
                i32 名字长度
                名字（cp932 / utf8 混用，统一交给 smart_decode 猜）
                u32 分隔字段（GARbro 直接跳过，这里当"打包长度"用，见下）
                u32 偏移（从文件开头数的绝对偏移）
                u32 存储大小
    之后是文件数据区。

pf8 的正文按 GARbro 是 XOR 加密的：key = SHA1(索引区 0x07..0x07+index_size)，
每个文件都从 key 的第 0 个字节重新开始异或（ByteStringEncryptedStream）。
Key 在归档里没有，只能从索引算，所以这里只把算好的 key 挂到 meta["pfs_key"]、
并标 meta["encrypted"]=True——和 npa.py 处理加密包的办法一样：能列目录，正文不解。

pf2 是另一套表头（GARbro 的 OpenPf2）：0x0B 处是文件数，条目从索引的 0x08 起，
名字和偏移之间隔着 0x10 字节。

GARbro 之外加的容错（正常样本走不到，走不通就退回 None）：
  * 索引整段是 zlib 流：0x03 仍然是从 0x07 起算的字节数，解开后是"文件数 + 条目"；
  * 文件数是明文、只有条目数组是 zlib 流；
  * 条目第 3 个字段非 0、且数据开头看着像 zlib 流时，按压缩条目记
    meta["segments"] = [(True, 偏移, 解压后大小, 打包大小)]，读取交给 core.Archive；
  * 单个条目偏移/大小越界就跳过该条；索引结构烂到读不下去才整包放弃。

还不支持 / 不保证的：
  * pf8 正文的 XOR：core.Archive.read() 没有这个钩子，要真解出来得在 core 里加一个
    XOR 分支，或者让 detect 返回 Archive 子类（README 说这两种都行，本文件按需求
    保持返回 list[Entry]）。
  * pf4 / pf5 / pf9：GARbro 主分支的 TryOpen 不路由，只在注释里提了一句，这里不做。
  * 多卷（root.pfs.000 / 001 / ...）：每个卷当独立包看，没有跨卷合并。
  * 文件名编码不保证逐字节一致：GARbro 用可配置的 PFSEncodingCP（默认 cp932、UTF-8
    兜底），这里用 smart_decode 的启发式，日文原名和中文补丁包都能出人话。
"""
import hashlib
import os
import struct
import zlib

from ..core import Entry, smart_decode
from ..registry import register_archive

MAX_COUNT = 0x100000
MAX_NAME = 0x1000
# GARbro: 版本 4/5/8/9 的正文做 XOR，其余（6/7）是明文
_ENCRYPTED_VERSIONS = frozenset((4, 5, 8, 9))
_PF_VERSIONS = frozenset((6, 7, 8))


def _i32(buf: bytes, off: int) -> int:
    return struct.unpack_from("<i", buf, off)[0]


def _u32(buf: bytes, off: int) -> int:
    return struct.unpack_from("<I", buf, off)[0]


def _zlib_head(buf: bytes, off: int = 0) -> bool:
    """看两个字节像不像 zlib 头，别把没压的数据当成 zlib"""
    if off < 0 or off + 2 > len(buf):
        return False
    cmf, flg = buf[off], buf[off + 1]
    return (cmf & 0x0F) == 8 and (cmf >> 4) <= 7 and ((cmf << 8) | flg) % 31 == 0


def _inflate(raw: bytes) -> bytes | None:
    """解 zlib；带输出上限，别被炸弹拖死（索引正常就几十 KB）"""
    if len(raw) < 4:
        return None
    try:
        out = zlib.decompressobj().decompress(raw, 1 << 26)
    except zlib.error:
        return None
    return out or None


def _make_entry(view, raw_name: bytes, flag: int, off: int, size: int) -> Entry | None:
    """一条索引 -> Entry，坏了就 None（调用方跳过，不抛异常）"""
    if not raw_name or size == 0 or off >= view.size:
        return None
    name = smart_decode(raw_name)
    if not name:
        return None
    # 扩展：第 3 个字段当打包长度，数据本身是 zlib 流
    if 0 < flag < size and off + flag <= view.size and _zlib_head(view.bytes(off, 2)):
        return Entry(name, off, flag, unpacked=size, kind="zlib",
                     meta={"segments": [(True, off, size, flag)]})
    if size > view.size - off:
        return None
    return Entry(name, off, size)


def _parse_entries(view, index: bytes, pos: int, count: int,
                   gap: int = 4, use_flag: bool = True):
    """从头走 count 条，返回 (entries, 是否完整走完)；结构坏了就停，不崩"""
    entries: list[Entry] = []
    end = len(index)
    for _ in range(count):
        if pos + 4 > end:
            return entries, False
        name_len = _i32(index, pos)
        pos += 4
        if name_len <= 0 or name_len > MAX_NAME or pos + name_len + gap + 8 > end:
            return entries, False
        raw_name = index[pos:pos + name_len]
        pos += name_len
        flag = _u32(index, pos) if use_flag else 0
        pos += gap
        off = _u32(index, pos)
        size = _u32(index, pos + 4)
        pos += 8
        entry = _make_entry(view, raw_name, flag, off, size)
        if entry is not None:
            entries.append(entry)
    return entries, True


def _read_index(view, raw: bytes) -> list[Entry] | None:
    """索引可能明文、也可能 zlib 压过；先按明文来，不行再解压"""
    if len(raw) >= 4:
        count = _i32(raw, 0)
        if 0 < count <= MAX_COUNT:
            entries, complete = _parse_entries(view, raw, 4, count)
            if complete or entries:
                return entries

    # 扩展一：整段索引就是一个 zlib 流
    if _zlib_head(raw):
        body = _inflate(raw)
        if body is not None and len(body) >= 4:
            count = _i32(body, 0)
            if 0 < count <= MAX_COUNT:
                entries, complete = _parse_entries(view, body, 4, count)
                if complete or entries:
                    return entries

    # 扩展二：文件数明文，只有条目数组被压过
    if len(raw) >= 4 and _zlib_head(raw, 4):
        count = _i32(raw, 0)
        if 0 < count <= MAX_COUNT:
            body = _inflate(raw[4:])
            if body is not None:
                entries, complete = _parse_entries(view, body, 0, count)
                if complete or entries:
                    return entries
    return None


def _pf_entries(view, version: int) -> list[Entry] | None:
    """pf6 / pf7 / pf8 共用这一套表头"""
    if view.size < 0x0B:
        return None
    index_size = view.u32(3)
    if index_size < 4 or index_size > view.size - 7:
        return None
    raw = view.bytes(7, index_size)
    entries = _read_index(view, raw)
    if not entries:
        return None
    if version in _ENCRYPTED_VERSIONS:
        # GARbro: key = SHA1(索引原始字节)，不管索引有没有被压过
        key = hashlib.sha1(raw).digest()
        for entry in entries:
            entry.meta["encrypted"] = True
            entry.meta["pfs_key"] = key
            entry.meta["xor_key"] = key          # core.Archive.read 会按 key 循环异或
    return entries


def _pf2_entries(view) -> list[Entry] | None:
    """GARbro 的 OpenPf2：0x0B 是文件数，条目从索引 0x08 起，空隙 0x10 字节"""
    if view.size < 0x10:
        return None
    index_size = view.u32(3)
    if index_size < 8 or index_size > view.size - 7:
        return None
    raw = view.bytes(7, index_size)
    count = _i32(raw, 4)
    if count <= 0 or count > MAX_COUNT:
        return None
    entries, _complete = _parse_entries(view, raw, 8, count, gap=0x10, use_flag=False)
    return entries or None


@register_archive("PFS", "Artemis PFS (pf6/pf7/pf8)")
def detect(view):
    if view.size < 0x0B:
        return None
    head = view.bytes(0, 3)
    if len(head) < 3 or head[:2] != b"pf" or not 0x30 <= head[2] <= 0x39:
        return None
    version = head[2] - 0x30
    if version == 2:
        return _pf2_entries(view)
    if version in _PF_VERSIONS:
        return _pf_entries(view, version)
    return None


# ---------------------------------------------------------------- 自测

def _xor(data: bytes, key: bytes) -> bytes:
    """GARbro ByteStringEncryptedStream 的逆运算：key 从头循环，每个文件重新数"""
    if not key:
        return data
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


def _build_pfs(version: int, items, index_zlib: bool = False, encrypt_body: bool = False):
    """按 ArcPFS.cs 的布局反着拼一个最小 PFS（自测用）

    items: [(名字, 原始数据, 存储方式)]，存储方式 "raw" 原样 / "zlib" 压一下
    返回 (文件内容, 说明)，说明里带 key、索引块、每条数据的绝对偏移和索引里
    偏移字段在文件中的位置（测试要拿它去改坏数据）
    """
    stored = []
    for name, data, mode in items:
        if mode == "zlib":
            packed = zlib.compress(data, 9)
            stored.append((name, packed, len(data), len(packed)))
        else:
            stored.append((name, data, len(data), 0))

    def make_index(offsets):
        buf = bytearray(struct.pack("<i", len(stored)))
        fields = []
        for (name, _blob, size, flag), off in zip(stored, offsets):
            raw_name = name.encode("utf-8")
            base = len(buf)
            fields.append(7 + base + 8 + len(raw_name))
            buf += struct.pack("<i", len(raw_name)) + raw_name
            buf += struct.pack("<III", flag, off, size)
        return bytes(buf), fields

    offsets = [0] * len(stored)
    blob = b""
    fields: list[int] = []
    for _ in range(8):
        body, fields = make_index(offsets)
        blob = zlib.compress(body, 9) if index_zlib else body
        start = 7 + len(blob)
        moved, pos = [], start
        for _name, data, _size, _flag in stored:
            moved.append(pos)
            pos += len(data)
        if moved == offsets:
            break                       # 偏移和索引长度互相自洽了
        offsets = moved

    if stored:
        assert 7 + len(blob) == offsets[0], "索引长度没收敛，自测构造失败"

    key = hashlib.sha1(blob).digest() if encrypt_body and stored else b""
    out = bytearray(f"pf{version}".encode("ascii"))
    out += struct.pack("<I", len(blob))
    out += blob
    for _name, data, _size, _flag in stored:
        out += _xor(data, key) if encrypt_body else data
    return bytes(out), {"key": key, "index": blob, "offsets": offsets, "fields": fields}


def _selftest() -> bool:
    from ..core import Archive, FileView          # 自测才用得到

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    out_dir = os.path.join(root, "_pfs_test")
    os.makedirs(out_dir, exist_ok=True)
    failed = 0

    def check(cond, label):
        nonlocal failed
        if not cond:
            failed += 1
        print(f"  [{'ok' if cond else 'FAIL'}] {label}")

    def save(name, payload):
        path = os.path.join(out_dir, name)
        with open(path, "wb") as f:
            f.write(payload)
        return path

    def detect_path(path):
        view = FileView(path)
        try:
            return detect(view)
        finally:
            view.close()

    def dump(entries):
        print(f"  {'名字':<26}{'偏移':>8}{'大小':>8}{'解压后':>9}  来源")
        for e in entries:
            print(f"  {e.name:<26}{e.offset:>8}{e.size:>8}{e.display_size:>9}  "
                  f"{e.kind or '原始'}")

    # ---- 1. pf8：索引 zlib 压缩 + 正文 SHA1 异或 ---------------------
    print("\n[1] pf8（索引整段 zlib 压缩，正文按 GARbro 的 SHA1(索引) 异或）")
    items8 = [
        ("script/main.scr", b"@label start\r\n" * 40, "raw"),
        ("bg/room01.png", b"\x89PNG\r\n\x1a\n" + bytes(range(128)), "raw"),
        ("se/click.ogg", b"OggS\x00\x02" + b"\x00" * 400, "raw"),
    ]
    payload, info = _build_pfs(8, items8, index_zlib=True, encrypt_body=True)
    path = save("pfs_pf8_zlibindex.pfs", payload)
    view = FileView(path)
    try:
        entries = detect(view) or []
        index_size = view.u32(3)
        zlib_index = _zlib_head(view.bytes(7, 2))
        stored_bytes = [view.bytes(e.offset, e.size) for e in entries]
    finally:
        view.close()
    dump(entries)
    check(len(entries) == len(items8), f"条目数 {len(entries)} == {len(items8)}")
    check(zlib_index and index_size == len(info["index"]),
          f"0x03 处是 zlib 索引（{index_size} 字节，78 开头），明文解析失败后靠 inflate 兜底")
    if len(entries) == len(items8):
        for entry, (name, original, _mode), off in zip(entries, items8, info["offsets"]):
            ok = (entry.name == name and entry.offset == off
                  and entry.size == len(original) and entry.unpacked == 0)
            check(ok, f"{name}：名字/偏移 {entry.offset}（期望 {off}）/大小 {entry.size}")
        check(all(e.meta.get("encrypted") and e.meta.get("pfs_key") == info["key"]
                  for e in entries),
              "每条带 meta['encrypted'] 和 meta['pfs_key'] = SHA1(索引)")
        for entry, (name, original, _mode), blob in zip(entries, items8, stored_bytes):
            check(_xor(blob, info["key"]) == original,
                  f"{name}：用 meta['pfs_key'] 异或回来和原文一致")

    # ---- 2. pf6：明文索引 + 压缩条目 ----------------------------------
    print("\n[2] pf6（明文索引，脚本条目在索引里记了打包长度）")
    script = b"@chapter 1\r\n" * 80
    items6 = [("script/ch01.scr", script, "zlib"), ("data/title.txt", b"PFS-SAMPLE", "raw")]
    payload, info = _build_pfs(6, items6)
    path = save("pfs_pf6_plainindex.pfs", payload)
    view = FileView(path)
    try:
        entries = detect(view) or []
        read_back = Archive(view, "PFS", entries).read(entries[0]) if entries else b""
    finally:
        view.close()
    dump(entries)
    check(len(entries) == len(items6), f"条目数 {len(entries)} == {len(items6)}")
    if len(entries) == len(items6):
        e0, e1 = entries
        packed = zlib.compress(script, 9)
        want = [(True, info["offsets"][0], len(script), len(packed))]
        check(e0.name == "script/ch01.scr", f"名字 {e0.name!r}")
        check(e0.meta.get("segments") == want,
              f"segments == {want}，实际 {e0.meta.get('segments')}")
        check(e0.size == len(packed) and e0.unpacked == len(script) and e0.kind == "zlib",
              f"存储 {e0.size} / 解压后 {e0.unpacked}（期望 {len(packed)} / {len(script)}）")
        check(read_back == script, "Archive.read 按 segments 解压出来和原文一致")
        check("segments" not in e1.meta and "encrypted" not in e1.meta
              and e1.size == len(b"PFS-SAMPLE") and e1.unpacked == 0,
              f"没压的条目原样：size={e1.size}，unpacked=0（未知），也没加密标记")

    # ---- 3. 边界与坏数据 ---------------------------------------------
    print("\n[3] 边界检查 / 坏数据")
    payload, info = _build_pfs(6, [("a.txt", b"aaaa", "raw"), ("b.txt", b"bbbb", "raw")])
    bad = bytearray(payload)
    struct.pack_into("<I", bad, info["fields"][0], 0xFFFFFF00)
    entries = detect_path(save("pfs_badoffset.pfs", bytes(bad)))
    dump(entries or [])
    check(entries is not None and len(entries) == 1 and entries[0].name == "b.txt",
          "偏移越界的条目被跳过，剩下的照常返回")
    check(detect_path(save("pfs_truncated.pfs", payload[:0x10])) is None,
          "索引被截断 -> None")
    check(detect_path(save("pfs_zeroindex.pfs", b"pf8" + b"\x00" * 64)) is None,
          "索引长度 0 -> None")
    check(detect_path(save("pfs_junk.pfs", b"pf8" + bytes(range(64)))) is None,
          "索引长度离谱 / 条目乱码 -> None")
    check(detect_path(save("pfs_not.pfs", b"NOTPF8" + b"\x00" * 64)) is None,
          "魔数不对 -> None")
    check(detect_path(save("pfs_empty.pfs", b"")) is None, "空文件 -> None")

    print(f"\n{'全部通过' if not failed else f'有 {failed} 项失败'}，文件在 {out_dir}")
    return not failed


if __name__ == "__main__":
    raise SystemExit(0 if _selftest() else 1)
