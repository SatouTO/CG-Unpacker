"""RealLive (Key) 归档：G00 多帧容器 + 塞满 .g00 的 .dat

GARbro 里 RealLive 相关的归档只有 ArcG00.cs（ArcKOE / ArcOVK 是音频，ArcSEEN 是
脚本），它解析的是 .g00 的“多帧”容器，结构是：

    u8  type (= 2)
    u16 width / u16 height
    i32 frame_count                  (GARbro 按 Int16 读，这里两种都容忍)
    frame_count * 0x18: i32 x / i32 y / 0x10 未知
    LZ 压缩数据 = i32 frame_count + frame_count * (u32 offset / u32 size) + 帧数据

帧数据在 LZ 解压后的 blob 里，不在文件偏移上，所以这里用一个 Archive 子类
（core.ZipArchive 也是这么干的，README 里也写了“直接 return 一个 Archive 实例也行”）。

.dat 的情况：GARbro 没有 RealLive .dat 的 TOC 解析器（翻遍 ArcFormats/RealLive
只有上面那几个），所以对 .dat 走 G00 文件头扫描——按 ImageG00.cs 的头部规则
（类型 / 宽高 / LZ 头长度）在文件里找内嵌的 g00，条目直接指向真实偏移，
命名 0001.g00、0002.g00……，原始 id / 类型 / 宽高都留在 meta 里。
"""
import os
import re
import struct

from ..core import Archive, Entry
from ..images_g00 import frame_image, lz_decompress, valid_canvas, MAX_OUT
from ..registry import register_archive

MAX_FRAMES = 0x1000
SCAN_EXTS = (".dat", ".g00")

_MAGIC = re.compile(rb"[\x00\x01\x02]")


class G00Archive(Archive):
    """预览直接渲染帧。原样导出保留原始 tile 和坐标，补齐单帧 G00 文件头。"""

    def __init__(self, view, entries, blob, width, height, frame_count):
        super().__init__(view, "G00", entries)
        self.blob = blob
        self.width = width
        self.height = height
        self.frame_count = frame_count

    def decode_frame(self, entry: Entry):
        img = frame_image(self.blob, entry.offset, entry.size, entry.meta["x"],
                          entry.meta["y"], self.width, self.height)
        if img is None:
            raise ValueError(f"G00 帧数据损坏：{entry.name}")
        return img

    def read(self, entry: Entry) -> bytes:
        descriptor = self.view.bytes(9 + entry.meta["id"] * 0x18, 0x18)
        frame = memoryview(self.blob)[entry.offset:entry.offset + entry.size]
        raw = b"".join((struct.pack("<iII", 1, 12, len(frame)), frame))
        # 每八个原字节用一个标记字节说明这组全是字面量。tile 本身逐字节保留。
        groups = (len(raw) + 7) // 8
        packed = bytearray(len(raw) + groups)
        packed[::9] = b"\xff" * groups
        for i in range(8):
            packed[i + 1::9] = raw[i::8]
        if len(raw) % 8:
            packed[(groups - 1) * 9] = (1 << (len(raw) % 8)) - 1
        header = struct.pack("<BHHi", 2, self.width, self.height, 1)
        return b"".join((header, descriptor, struct.pack("<II", 8 + len(packed), len(raw)), packed))


def _open_container(view):
    """按 ArcG00.cs 解析 G00 多帧容器，成功返回 G00Archive"""
    if view.u8(0) != 2:
        return None
    width = view.u16(1)
    height = view.u16(3)
    if not valid_canvas(width, height):
        return None
    count = view.i32(5)
    if count <= 1 or count > MAX_FRAMES:
        # GARbro 读的是 Int16，也兼容一下高 16 位是垃圾的包
        count = struct.unpack_from("<h", view.bytes(5, 2), 0)[0]
        if count <= 1 or count > MAX_FRAMES:
            return None
    index_offset = 9 + count * 0x18
    if index_offset + 8 > view.size:
        return None

    packed = view.u32(index_offset)
    raw_size = view.u32(index_offset + 4)
    if (packed < 8 or index_offset + packed > view.size
            or not 4 + count * 8 <= raw_size <= MAX_OUT):
        return None
    blob = lz_decompress(view.bytes(index_offset, packed), 0, 2, 1)
    if blob is None or len(blob) < 4 + count * 8:
        return None
    if struct.unpack_from("<i", blob, 0)[0] != count:
        return None

    entries: list[Entry] = []
    for i in range(count):
        x = view.i32(9 + i * 0x18)
        y = view.i32(13 + i * 0x18)
        offset = struct.unpack_from("<I", blob, 4 + i * 8)[0]
        size = struct.unpack_from("<I", blob, 8 + i * 8)[0]
        if size == 0 or offset < 4 + count * 8 or offset + size > len(blob):
            continue
        entries.append(Entry(
            f"{i + 1:04d}.g00", offset, size, unpacked=size, kind="frame",
            meta={"id": i, "x": x, "y": y, "width": width, "height": height,
                  "frame_count": count, "blob_offset": offset},
        ))
    if not entries:
        return None
    return G00Archive(view, entries, blob, width, height, count)


def _g00_at(data: bytes, offset: int):
    """offset 处如果是一个完整的 g00，返回 (size, type, width, height)，否则 None"""
    total = len(data)
    if offset < 0 or offset + 9 > total:
        return None
    gtype = data[offset]
    width = struct.unpack_from("<H", data, offset + 1)[0]
    height = struct.unpack_from("<H", data, offset + 3)[0]
    if not valid_canvas(width, height):
        return None

    if gtype == 2:
        count = struct.unpack_from("<i", data, offset + 5)[0]
        if count <= 0 or count > MAX_FRAMES:
            return None
        index_end = offset + 9 + count * 0x18
        if index_end + 8 > total:
            return None
        packed = struct.unpack_from("<I", data, index_end)[0]
        out_size = struct.unpack_from("<I", data, index_end + 4)[0]
        if packed < 8 or out_size < 4 + count * 8 or out_size > MAX_OUT:
            return None
        size = index_end - offset + packed
        if offset + size > total:
            return None
        return size, gtype, width, height

    length = struct.unpack_from("<I", data, offset + 5)[0]
    if length < 8:
        return None
    size = 5 + length
    if offset + size > total:
        return None
    out_size = struct.unpack_from("<I", data, offset + 9)[0]
    if out_size > MAX_OUT:
        return None
    if gtype == 0:
        if out_size < width * height * 3:
            return None
    else:
        need = width * height
        if out_size < need + 2 or out_size > need + 2 + 256 * 4 + 0x1000:
            return None
    return size, gtype, width, height


def _scan_dat(view) -> list[Entry]:
    """在 .dat 里扫内嵌的 g00；找到的条目指向文件里的真实偏移"""
    data = view.bytes(0, view.size)
    entries: list[Entry] = []
    next_pos = 0
    for match in _MAGIC.finditer(data):
        offset = match.start()
        if offset < next_pos:
            continue
        hit = _g00_at(data, offset)
        if hit is None:
            continue
        size, gtype, width, height = hit
        entries.append(Entry(
            f"{len(entries) + 1:04d}.g00", offset, size, kind="g00",
            meta={"id": len(entries), "offset": offset, "g00_type": gtype,
                  "width": width, "height": height, "image": True},
        ))
        next_pos = offset + size
    return entries


@register_archive("G00", "RealLive .dat")
def detect(view):
    if view.size >= 9:
        archive = _open_container(view)
        if archive is not None:
            return archive
    if view.size < 5 or os.path.splitext(view.path)[1].lower() not in SCAN_EXTS:
        return None
    return _scan_dat(view) or None


# ---------------------------------------------------------------- 自测
if __name__ == "__main__":
    from ..core import FileView
    from ..extract import decode_image
    from ..registry import open_archive

    def _store(raw: bytes, bytes_pp: int) -> bytes:
        """把数据编成 G00 的 LZ 流（全部按字面量存，自测用）"""
        body = bytearray()
        pos = 0
        while pos < len(raw):
            n = min(8, (len(raw) - pos) // bytes_pp)
            if n <= 0:
                break
            body.append((1 << n) - 1)
            body += raw[pos:pos + n * bytes_pp]
            pos += n * bytes_pp
        return struct.pack("<II", len(body) + 8, len(raw)) + bytes(body)

    def _tile_frame(width: int, height: int, color) -> bytes:
        """一个铺满 width x height 的帧（单个 tile）"""
        head = struct.pack("<HH", 1, 1) + b"\x00" * 0x70
        tile = struct.pack("<HHHHH", 0, 0, 0, width, height) + b"\x00" * 0x52
        return head + tile + bytes(color) * (width * height)

    def _container(width: int, height: int, frames) -> bytes:
        """frames: [(x, y, 帧数据)]，按 ArcG00.cs 的布局拼一个 .g00"""
        head = bytes([2]) + struct.pack("<HH", width, height) + struct.pack("<i", len(frames))
        for fx, fy, _ in frames:
            head += struct.pack("<ii", fx, fy) + b"\x00" * 0x10
        table = b""
        payload = b""
        for _, _, frame in frames:
            table += struct.pack("<II", 4 + len(frames) * 8 + len(payload), len(frame))
            payload += frame
        blob = struct.pack("<i", len(frames)) + table + payload
        return head + _store(blob, 1)

    def _g00_v0(width: int, height: int, bgr: bytes) -> bytes:
        return bytes([0]) + struct.pack("<HH", width, height) + _store(bgr, 3)

    def _g00_v1(width: int, height: int, palette: bytes, indices: bytes) -> bytes:
        raw = struct.pack("<H", len(palette) // 4) + palette + indices
        return bytes([1]) + struct.pack("<HH", width, height) + _store(raw, 1)

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    out_dir = os.path.join(root, "_g00_test")
    os.makedirs(out_dir, exist_ok=True)

    # ---- 1) G00 多帧容器（ArcG00.cs） ----
    container_path = os.path.join(out_dir, "sample.dat")
    with open(container_path, "wb") as f:
        f.write(_container(4, 4, [
            (0, 0, _tile_frame(4, 4, (30, 20, 10, 255))),
            (2, 2, _tile_frame(2, 2, (60, 50, 40, 255))),
        ]))

    print("== G00 多帧容器 ==")
    view = FileView(container_path)
    try:
        arc = detect(view)
        print(f"文件: {os.path.basename(container_path)}  识别: {type(arc).__name__}"
              f"  {arc.format}  {len(arc.entries)} 帧  画布 {arc.width}x{arc.height}")
        for entry in arc.entries:
            data = arc.read(entry)
            print(f"  {entry.name}  blob_off={entry.offset:#x}  size={entry.size}"
                  f"  id={entry.meta['id']}  pos=({entry.meta['x']},{entry.meta['y']})"
                  f"  读出 {len(data)} 字节  头两字节={data[:2].hex()}")
    finally:
        view.close()

    # ---- 2) 塞满 .g00 的 .dat（扫描） ----
    bundle_path = os.path.join(out_dir, "bundle.dat")
    v0 = _g00_v0(3, 2, bytes([10, 20, 30, 40, 50, 60, 70, 80, 90,
                              100, 110, 120, 130, 140, 150, 160, 170, 180]))
    v1 = _g00_v1(4, 4, bytes([0, 0, 0, 255, 255, 255, 255, 255]),
                 bytes([0, 1, 1, 0, 1, 0, 0, 1, 1, 0, 0, 1, 0, 1, 1, 0]))
    with open(bundle_path, "wb") as f:
        f.write(b"REALLIVE-TOC\x00\x00\x00\x00" + v0 + v1)

    print("\n== .dat 内嵌 g00 扫描 ==")
    archive = open_archive(bundle_path)
    if archive is None:
        print("[失败] 没识别出来")
    else:
        print(f"文件: {os.path.basename(bundle_path)}  识别: {archive.format}"
              f"  {len(archive.entries)} 个 g00")
        for entry in archive.entries:
            data = archive.read(entry)
            img, _desc = decode_image(data, None)
            head = (f"  {entry.name}  off={entry.offset:#x}  size={entry.size}"
                    f"  id={entry.meta['id']}  type={entry.meta['g00_type']}"
                    f"  {entry.meta['width']}x{entry.meta['height']}")
            if img is None:
                print(head + "  -> 解码失败")
            else:
                print(head + f"  -> {img.size[0]}x{img.size[1]} {img.mode} 像素(0,0)={img.getpixel((0, 0))}")
        archive.close()

    # ---- 3) 不是 G00 的数据不能被认领 ----
    junk_path = os.path.join(out_dir, "junk.dat")
    with open(junk_path, "wb") as f:
        f.write(b"\x00\x01\x02" + bytes(range(3, 256)) * 4)
    print("\n== 反例 ==")
    view = FileView(junk_path)
    try:
        print(f"junk.dat -> {detect(view)}")
    finally:
        view.close()
    print(f"样本目录: {out_dir}")
