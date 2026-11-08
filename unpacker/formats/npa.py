r"""Nitro+ NPA 归档（魔数 "NPA\x01"）

格式规范照抄 GARbro 的 ArcFormats/NitroPlus/ArcNPA.cs（morkt/GARbro），字段如下：

    0x00  4  "NPA\x01"
    0x04  2  0
    0x06  1  0
    0x07  4  key1（GARbro 写包默认 0x4147414E）
    0x0B  4  key2（默认 0x21214F54）
    0x0F  1  内容压缩标记
    0x10  1  内容加密标记
    0x11  4  条目总数（含目录项）
    0x15  4  目录项数
    0x19  4  文件项数
    0x1D  8  0
    0x25  4  索引大小 dir_size
    0x29      索引，每条 = i32 名字长度 + 名字 + u8 类型（1 目录 / 2 文件）
                              + i32 目录 id + u32 偏移 + u32 存储大小 + u32 解压后大小
              偏移是相对数据区（0x29 + dir_size）的

名字是逐字节混淆的：真名[i] = 存的[i] + DecryptName(i, 条目序号, key1*key2)。
名字按 cp932 存，这里统一交给 smart_decode 猜编码（日文原版、中文补丁都可能）。

压缩：规范里只有 zlib（GARbro 的 ZLibStream），没有 NBZ 之类的东西，已实现。
GARbro 对图片条目（Type == "image"）不压，这里也按后缀判断，再用 zlib 头校验兜一层，
免得把没压的文件当压的。

还不支持的情况（都写在这，免得以后踩坑）：
  * 0x10 加密标记 = 1 的包：正文前 0x1000 + 名字长度 字节用「逐游戏 key 表」加密
    （GARbro 的 EncryptionScheme：TitleId / NameKey / Order），这张表存在 GARbro 用户
    设置里，不在归档里，游戏名对不上就没法解。目录本身是明文（名字混淆只跟 key1/key2
    有关），所以这种包还能列出条目，只是每条带 meta["encrypted"]=True，读出来是密文。
  * LAMENTO 用 key1+key2 当归档 key，以及它的 EncryptedStream 不加位置偏移的规则，
    没有实现（只影响加密包）。
  * 规范里没有分卷 / 外挂索引，因此也不支持这种变体。
"""
import os
import struct
import zlib

from ..core import Entry, smart_decode
from ..registry import register_archive

MAGIC = b"NPA\x01"
HEADER_SIZE = 0x29                      # 41
DEFAULT_KEY1 = 0x4147414E               # 'NAGA'
DEFAULT_KEY2 = 0x21214F54               # 'TO!!'

# GARbro: IsPacked = compressed && Type != "image"，所以图片一律不压
_IMAGE_EXTS = frozenset((
    ".png", ".bmp", ".jpg", ".jpeg", ".gif", ".tga", ".tif", ".tiff",
    ".webp", ".dds", ".ico", ".psd", ".avif", ".jxl", ".qoi",
))


def _archive_key(key1: int, key2: int) -> int:
    """GetArchiveKey：非 LAMENTO 是 key1*key2（int32 回绕）"""
    return (key1 * key2) & 0xFFFFFFFF


def decrypt_name_byte(index: int, file_no: int, arc_key: int) -> int:
    """NpaOpener.DecryptName：真名第 index 个字节要加回去的偏移"""
    key = 0xFC * index
    for shift in (24, 16, 8, 0):
        key -= (arc_key >> shift) & 0xFF
        key -= (file_no >> shift) & 0xFF
    return key & 0xFF


def _is_image(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in _IMAGE_EXTS


def _looks_zlib(view, off: int, size: int) -> bool:
    """看前两个字节像不像 zlib 头，别把没压的文件当压的"""
    if size < 2 or off + 2 > view.size:
        return False
    cmf = view.u8(off)
    flg = view.u8(off + 1)
    return (cmf & 0x0F) == 8 and (cmf >> 4) <= 7 and ((cmf << 8) | flg) % 31 == 0


def _parse(view) -> list[Entry] | None:
    if view.size < HEADER_SIZE or view.bytes(0, 4) != MAGIC:
        return None

    key1 = view.i32(0x07)
    key2 = view.i32(0x0B)
    compressed = view.u8(0x0F) != 0
    encrypted = view.u8(0x10) != 0
    total_count = view.i32(0x11)
    folder_count = view.i32(0x15)
    file_count = view.i32(0x19)
    dir_size = view.u32(0x25)

    if total_count <= 0 or folder_count < 0 or file_count < 0:
        return None
    if total_count < folder_count + file_count:
        return None
    if dir_size == 0 or HEADER_SIZE + dir_size > view.size:
        return None
    # 每条至少 4 + 1 + 16 字节，数量对不上就是索引坏了，别死循环
    if total_count > dir_size // 21 + 1:
        return None

    data_start = HEADER_SIZE + dir_size
    arc_key = _archive_key(key1, key2)
    entries: list[Entry] = []
    pos = HEADER_SIZE

    for i in range(total_count):
        if pos + 4 > data_start:
            break
        name_size = view.i32(pos)
        if name_size < 0 or name_size >= dir_size:
            break
        step = 4 + name_size + 17
        if pos + step > data_start:
            break

        name_off = pos + 4
        info_off = name_off + name_size + 1      # 名字后面是 u8 类型
        kind = view.u8(name_off + name_size)
        pos += step

        if kind == 1:                            # 目录项，GARbro 也跳过
            continue

        raw = bytearray(view.bytes(name_off, name_size))
        for x in range(name_size):
            raw[x] = (raw[x] + decrypt_name_byte(x, i, arc_key)) & 0xFF
        name = smart_decode(bytes(raw))
        if not name:
            continue

        offset = view.u32(info_off + 4)
        size = view.u32(info_off + 8)
        unpacked_size = view.u32(info_off + 12)
        abs_off = data_start + offset
        if abs_off > view.size or size > view.size - abs_off:
            continue                             # 越界条目跳过，不抛异常

        meta: dict = {}
        if encrypted:
            meta["encrypted"] = True
        if compressed and not encrypted and not _is_image(name) \
                and _looks_zlib(view, abs_off, size):
            meta["segments"] = [(True, abs_off, unpacked_size, size)]
            entries.append(Entry(name, abs_off, size, unpacked_size,
                                 kind="zlib", meta=meta))
        else:
            entries.append(Entry(name, abs_off, size,
                                 unpacked_size if unpacked_size > 0 else size,
                                 meta=meta))
    return entries or None


@register_archive("NPA", "Nitro+ NPA")
def detect(view):
    try:
        return _parse(view)
    except Exception:            # 探测器不许把异常丢给注册表
        return None


# ---------------------------------------------------------------- 自测

def _build_npa(items, dirs=(), key1=DEFAULT_KEY1, key2=DEFAULT_KEY2, compress=False):
    """按 ArcNPA.cs 的 Create() 反过来拼一个最小 NPA（自测用）

    items: [(相对路径, 原始数据, 要不要 zlib 压)]
    返回 (文件内容, 每个条目在文件里的位置表)，位置表方便测试去改坏某个字段
    """
    arc_key = _archive_key(key1, key2)
    folder_ids = {d: i + 1 for i, d in enumerate(dirs)}

    # [类型, 名字字节, 目录 id, 数据, 相对偏移, 存储大小, 原始大小]
    records = []
    for d in dirs:
        records.append([1, d.encode("cp932"), folder_ids[d], b"", 0, 0, 0])
    for name, data, want_pack in items:
        stored = zlib.compress(data, 9) if (compress and want_pack) else data
        parent = name.rsplit("\\", 1)[0] if "\\" in name else ""
        records.append([2, name.encode("cp932"), folder_ids.get(parent, 0),
                        stored, 0, len(stored), len(data)])

    dir_size = sum(4 + len(r[1]) + 17 for r in records)
    data_start = HEADER_SIZE + dir_size
    buf = bytearray(data_start)
    body = bytearray()
    for r in records:
        if r[0] == 2:
            r[4] = len(body)
            body += r[3]

    struct.pack_into("<I", buf, 0x00, 0x0141504E)
    struct.pack_into("<h", buf, 0x04, 0)
    buf[0x06] = 0
    struct.pack_into("<i", buf, 0x07, key1)
    struct.pack_into("<i", buf, 0x0B, key2)
    buf[0x0F] = 1 if compress else 0
    buf[0x10] = 0
    struct.pack_into("<i", buf, 0x11, len(records))
    struct.pack_into("<i", buf, 0x15, len(dirs))
    struct.pack_into("<i", buf, 0x19, sum(1 for r in records if r[0] == 2))
    struct.pack_into("<q", buf, 0x1D, 0)
    struct.pack_into("<I", buf, 0x25, dir_size)

    spans = []
    pos = HEADER_SIZE
    for i, r in enumerate(records):
        name_size = len(r[1])
        struct.pack_into("<i", buf, pos, name_size)
        for x, byte in enumerate(r[1]):
            buf[pos + 4 + x] = (byte - decrypt_name_byte(x, i, arc_key)) & 0xFF
        buf[pos + 4 + name_size] = r[0]
        struct.pack_into("<i", buf, pos + 5 + name_size, r[2])
        struct.pack_into("<I", buf, pos + 9 + name_size, r[4])
        struct.pack_into("<I", buf, pos + 13 + name_size, r[5])
        struct.pack_into("<I", buf, pos + 17 + name_size, r[6])
        spans.append({"kind": r[0], "offset_pos": pos + 9 + name_size,
                      "data_pos": data_start + r[4]})
        pos += 4 + name_size + 17
    return bytes(buf + body), spans


def _selftest() -> bool:
    from ..core import Archive, FileView          # 自测才用得到

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    out_dir = os.path.join(root, "_npa_test")
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
        print(f"  {'名字':<30}{'偏移':>8}{'大小':>8}{'解压后':>9}  来源")
        for e in entries:
            print(f"  {e.name:<30}{e.offset:>8}{e.size:>8}{e.display_size:>9}  {e.kind or '原始'}")

    # ---- 1. 未压缩包 -------------------------------------------------
    print("\n[1] 未压缩 NPA（含目录项、日文名、图片）")
    items = [
        ("script\\start.nss", b"*start\r\n@label\r\n" * 3, False),
        ("images\\bg001.png", b"\x89PNG\r\n\x1a\n" + bytes(range(32)), False),
        ("テキスト\\readme.txt", "セーブデータ".encode("cp932") * 3, False),
    ]
    payload, spans = _build_npa(items, dirs=("script", "images", "テキスト"))
    path = save("npa_plain.npa", payload)
    entries = detect_path(path) or []
    dump(entries)
    file_spans = [s for s in spans if s["kind"] == 2]
    expected_names = [smart_decode(name.encode("cp932")) for name, _, _ in items]
    check(len(entries) == len(items), f"条目数 {len(entries)} == {len(items)}（目录项被跳过）")
    for entry, (name, data, _pack), expected, span in \
            zip(entries, items, expected_names, file_spans):
        ok = (entry.name == expected and entry.offset == span["data_pos"]
              and entry.size == len(data) and entry.unpacked == len(data)
              and "segments" not in entry.meta)
        check(ok, f"{name!r} 名字/偏移 {entry.offset}(期望 {span['data_pos']})"
                  f"/大小 {entry.size}(期望 {len(data)})")
    if len(entries) == 3:
        print(f"  注：日文名按 cp932 存，smart_decode 选了 "
              f"{entries[2].name!r}（core 的启发式，不是解密错）")

    # ---- 2. 压缩包 ---------------------------------------------------
    print("\n[2] 压缩 NPA（非图片条目 zlib，图片原样）")
    script = b"@chapter 1\r\n" * 120
    png = b"\x89PNG\r\n\x1a\n" + bytes(range(64))
    payload, spans = _build_npa(
        [("script\\main.nss", script, True), ("images\\bg.png", png, False)],
        dirs=("script", "images"), compress=True)
    path = save("npa_compressed.npa", payload)
    file_spans = [s for s in spans if s["kind"] == 2]
    view = FileView(path)
    try:
        entries = detect(view) or []
        packed = zlib.compress(script, 9)
        got = Archive(view, "NPA", entries).read(entries[0]) if entries else b""
    finally:
        view.close()
    dump(entries)
    check(len(entries) == 2, f"条目数 {len(entries)} == 2")
    if len(entries) == 2:
        e0, e1 = entries
        check(e0.name == smart_decode(b"script\\main.nss"), f"名字 {e0.name!r}")
        want_seg = [(True, file_spans[0]["data_pos"], len(script), len(packed))]
        check(e0.meta.get("segments") == want_seg,
              f"segments == {want_seg}，实际 {e0.meta.get('segments')}")
        check(e0.size == len(packed) and e0.unpacked == len(script),
              f"存储 {e0.size} / 解压后 {e0.unpacked}（期望 {len(packed)} / {len(script)}）")
        check(got == script, "Archive.read 按 segments 解压出来的和原文一致")
        check("segments" not in e1.meta and e1.size == len(png) and e1.unpacked == len(png),
              f"图片条目没压：size={e1.size} unpacked={e1.unpacked}")

    # ---- 3. 边界与垃圾 -----------------------------------------------
    print("\n[3] 边界检查 / 垃圾数据")
    payload, spans = _build_npa([("a.txt", b"aaaa", False), ("b.txt", b"bbbb", False)])
    file_spans = [s for s in spans if s["kind"] == 2]
    bad = bytearray(payload)
    struct.pack_into("<I", bad, file_spans[0]["offset_pos"], 0xFFFFFF00)
    entries = detect_path(save("npa_badoffset.npa", bytes(bad)))
    dump(entries or [])
    check(entries is not None and len(entries) == 1
          and entries[0].name == smart_decode(b"b.txt"),
          "偏移越界的条目被跳过，其余照常返回")
    check(detect_path(save("npa_short.npa", payload[:HEADER_SIZE + 8])) is None,
          "索引被截断 -> None")
    check(detect_path(save("npa_not.npa", b"NOTNPA\x01" + b"\x00" * 64)) is None,
          "魔数不对 -> None")
    check(detect_path(save("npa_empty.npa", b"")) is None, "空文件 -> None")

    enc = bytearray(payload)
    enc[0x10] = 1
    entries = detect_path(save("npa_encrypted.npa", bytes(enc)))
    dump(entries or [])
    check(entries is not None and len(entries) == 2
          and all(e.meta.get("encrypted") for e in entries),
          "加密标记=1 仍能列出目录，条目带 meta['encrypted']（正文不解密）")

    print(f"\n{'全部通过' if not failed else f'有 {failed} 项失败'}，文件在 {out_dir}")
    return not failed


if __name__ == "__main__":
    import sys
    try:                        # 免得中文在 Windows 控制台按 cp936 输出成乱码
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(0 if _selftest() else 1)
