"""RealLive (Key) G00 图片解码：type 0 / 1 / 2

对应 GARbro 的 ArcFormats/RealLive/ImageG00.cs：
  0 = 24bpp BGR（PixelFormats.Bgr24）
  1 = 8bpp 调色板（Indexed8，调色板是 BGRA）
  2 = 32bpp BGRA 分块图（tile）
type 3（加密 JPEG，见 ImageG00Jpeg.cs）不在这里，遇到就返回 None 交给下一个解码器。

G00 的压缩是“按字节分组”的 LZ：每 8 个符号一个标志字节（低位在前），
标志位为 1 就是字面量，为 0 就是 (u16) 回引，长度 = (低 4 位 + min_count) * 每像素字节数，
距离 = (高 12 位) * 每像素字节数。这里的所有越界都按坏数据处理，不往外抛。
"""
import struct

from PIL import Image

from .registry import register_image

MAX_DIM = 0x8000            # GARbro 也只认到 0x8000
MAX_TILES = 0x1000
MAX_OUT = 0x10000000        # 单张图解压上限 256MB，坏数据直接放弃

TILE_HEAD = 0x5C            # 每个 tile 的头部：2+2+2+2+2 + 0x52


def valid_canvas(width: int, height: int) -> bool:
    # Pillow 的 RGB 和 RGBA 图都可能按每像素四字节分配内存。
    return (0 < width <= MAX_DIM and 0 < height <= MAX_DIM
            and width * height * 4 <= MAX_OUT)


def _u16(b, o):
    return struct.unpack_from("<H", b, o)[0]


def _u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def _i32(b, o):
    return struct.unpack_from("<i", b, o)[0]


def _copy_overlapped(out: bytearray, src: int, dst: int, count: int):
    """Binary.CopyOverlapped：允许源和目标重叠的向前复制"""
    while count > 0:
        n = min(dst - src, count)
        out[dst:dst + n] = out[src:src + n]
        dst += n
        src += n
        count -= n


def lz_decompress(buf, pos: int, min_count: int, bytes_pp: int,
                  max_out: int = MAX_OUT) -> bytes | None:
    """G00Reader.LzDecompress 的 Python 版；数据坏了返回 None"""
    if pos < 0 or pos + 8 > len(buf):
        return None
    packed = _u32(buf, pos)
    out_size = _u32(buf, pos + 4)
    if packed < 8:
        return None
    packed -= 8
    if out_size <= 0 or out_size > max_out:
        return None
    # 每两个回引字节最多能生成 (15 + min_count) 个符号。
    if out_size * 2 > packed * (15 + min_count) * bytes_pp:
        return None
    end = pos + 8 + packed
    if end > len(buf):
        return None

    src = pos + 8
    out = bytearray(out_size)
    dst = 0
    bits = 2
    while dst < out_size:
        bits >>= 1
        if bits == 1:
            if src >= end:
                return None
            bits = buf[src] | 0x100
            src += 1
        if bits & 1:
            if src + bytes_pp > end or dst + bytes_pp > out_size:
                return None
            out[dst:dst + bytes_pp] = buf[src:src + bytes_pp]
            src += bytes_pp
            dst += bytes_pp
        else:
            if src + 2 > end:
                return None
            v = _u16(buf, src)
            src += 2
            count = ((v & 0xF) + min_count) * bytes_pp
            dist = (v >> 4) * bytes_pp
            if dist <= 0 or dist > dst:
                return None
            count = min(count, out_size - dst)
            _copy_overlapped(out, dst - dist, dst, count)
            dst += count
    return bytes(out)


def parse_meta(data: bytes) -> dict | None:
    """读 G00 头（对应 G00Format.ReadMetaData），顺便把明显不对的数据挡掉"""
    if len(data) < 9:
        return None
    gtype = data[0]
    if gtype > 2:
        return None
    width = _u16(data, 1)
    height = _u16(data, 3)
    if not valid_canvas(width, height):
        return None
    if gtype == 2:
        count = _i32(data, 5)
        if count <= 0 or count > MAX_TILES:
            return None
        data_offset = 9 + count * 0x18
        if data_offset + 8 > len(data):
            return None
        out_size = _u32(data, data_offset + 4)
        if out_size < 4 + count * 8 or out_size > MAX_OUT:
            return None
        return {"type": gtype, "width": width, "height": height,
                "count": count, "data_offset": data_offset}
    length = _u32(data, 5)
    if length < 8 or 5 + length > len(data):
        return None
    out_size = _u32(data, 9)
    if out_size > MAX_OUT:
        return None
    if gtype == 0:
        # GARbro 按 Bgr24 建图，解压长度至少得够 w*h*3
        if out_size < width * height * 3:
            return None
    else:
        need = width * height
        # 调色板最多 256 色：2 + colors*4 + w*h（留一点对齐余量）
        if out_size < need + 2 or out_size > need + 2 + 256 * 4 + 0x1000:
            return None
    return {"type": gtype, "width": width, "height": height, "length": length}


def _bgra_image(buf, width: int, height: int, bpp: int) -> Image.Image:
    if bpp == 3:
        return Image.frombuffer("RGB", (width, height), buf, "raw", "BGR", width * 3, 1)
    return Image.frombuffer("RGBA", (width, height), buf, "raw", "BGRA", width * 4, 1)


def _decode_v0(data: bytes, meta: dict):
    width, height = meta["width"], meta["height"]
    raw = lz_decompress(data, 5, 1, 3)
    need = width * height * 3
    if raw is None or len(raw) < need:
        return None
    return _bgra_image(raw[:need], width, height, 3)


def _decode_v1(data: bytes, meta: dict):
    width, height = meta["width"], meta["height"]
    raw = lz_decompress(data, 5, 2, 1)
    if raw is None or len(raw) < 2:
        return None
    colors = _u16(raw, 0)
    if colors == 0 or colors > 256:
        return None
    pal_end = 2 + colors * 4
    pix_end = pal_end + width * height
    if len(raw) < pix_end:
        return None

    palette = bytearray(768)
    alpha = bytearray(b"\xff" * 256)
    for i in range(colors):
        b, g, r, a = raw[2 + i * 4:6 + i * 4]
        palette[i * 3:i * 3 + 3] = bytes((r, g, b))
        alpha[i] = a
    # 调色板 alpha 全 0 的包不少见，那就当不透明处理（GARbro 会照抄 alpha）
    if not any(alpha[:colors]):
        alpha[:colors] = b"\xff" * colors

    img = Image.frombytes("P", (width, height), raw[pal_end:pix_end])
    img.putpalette(bytes(palette))
    if any(a != 255 for a in alpha[:colors]):
        img.info["transparency"] = bytes(alpha[:colors])
        return img.convert("RGBA")
    return img.convert("RGB")


def render_frame(blob: bytes, offset: int, size: int, base_x: int, base_y: int,
                 width: int, height: int, canvas: bytearray) -> bool:
    """把完整的 tile 帧画到 BGRA 画布；空帧或完全被裁掉的帧保持透明。

    帧结构（G00TileDecoder）：u16 tile_type(=1) / u16 tile_count / 0x70 字节
    每个 tile：u16 x / u16 y / i16 未知 / u16 w / u16 h / 0x52 字节 + w*h*4 的 BGRA
    """
    end = offset + size
    if offset < 0 or end > len(blob) or size < 4 + 0x70:
        return False
    tile_type = _u16(blob, offset)
    tile_count = _u16(blob, offset + 2)
    if tile_type != 1:
        return False
    pos = offset + 4 + 0x70
    stride = width * 4
    for _ in range(tile_count):
        if pos + TILE_HEAD > end:
            return False
        tx = _u16(blob, pos) + base_x
        ty = _u16(blob, pos + 2) + base_y
        tw = _u16(blob, pos + 6)
        th = _u16(blob, pos + 8)
        pos += TILE_HEAD
        if tw == 0 or th == 0:
            continue
        need = tw * th * 4
        if pos + need > end:
            return False
        x0, x1 = max(0, tx), min(width, tx + tw)
        y0, y1 = max(0, ty), min(height, ty + th)
        if x1 > x0 and y1 > y0:
            row_src = tw * 4
            for yy in range(y0, y1):
                s = pos + (yy - ty) * row_src + (x0 - tx) * 4
                d = yy * stride + x0 * 4
                canvas[d:d + (x1 - x0) * 4] = blob[s:s + (x1 - x0) * 4]
        pos += need
    return True


def frame_image(blob, offset, size, base_x, base_y, width, height):
    if not valid_canvas(width, height) or size < 0x74 or offset < 0 or offset + size > len(blob):
        return None
    canvas = bytearray(width * height * 4)
    if not render_frame(blob, offset, size, base_x, base_y, width, height, canvas):
        return None
    return _bgra_image(canvas, width, height, 4)


def _decode_v2(data: bytes, meta: dict):
    width, height, count = meta["width"], meta["height"], meta["count"]
    blob = lz_decompress(data, meta["data_offset"], 2, 1)
    if blob is None or len(blob) < 4 + count * 8:
        return None
    if _i32(blob, 0) != count:
        return None

    for i in range(count):
        offset = _u32(blob, 4 + i * 8)
        size = _u32(blob, 8 + i * 8)
        if offset < 4 + count * 8:
            continue
        base_x = _i32(data, 9 + i * 0x18)
        base_y = _i32(data, 13 + i * 0x18)
        img = frame_image(blob, offset, size, base_x, base_y, width, height)
        if img is not None:
            return img
    return None


def g00_to_image(data: bytes):
    """整块 .g00 数据 -> PIL.Image；不是 G00 或者数据坏了返回 None"""
    meta = parse_meta(data)
    if meta is None:
        return None
    if meta["type"] == 0:
        return _decode_v0(data, meta)
    if meta["type"] == 1:
        return _decode_v1(data, meta)
    return _decode_v2(data, meta)


@register_image("G00 (RealLive)")
def _g00(data, ctx):
    try:
        return g00_to_image(data)
    except Exception:
        return None


# ---------------------------------------------------------------- 自测
if __name__ == "__main__":
    import os

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

    def g00_v0(width: int, height: int, pixels: bytes) -> bytes:
        return bytes([0]) + struct.pack("<HH", width, height) + _store(pixels, 3)

    def g00_v1(width: int, height: int, palette: bytes, indices: bytes) -> bytes:
        raw = struct.pack("<H", len(palette) // 4) + palette + indices
        return bytes([1]) + struct.pack("<HH", width, height) + _store(raw, 1)

    def g00_v2(width: int, height: int, frames) -> bytes:
        head = bytes([2]) + struct.pack("<HH", width, height) + struct.pack("<i", len(frames))
        for fx, fy, _ in frames:
            head += struct.pack("<ii", fx, fy) + b"\x00" * 0x10
        payload = b""
        table = b""
        for _, _, frame in frames:
            table += struct.pack("<II", len(frames) * 8 + 4 + len(payload), len(frame))
            payload += frame
        blob = struct.pack("<i", len(frames)) + table + payload
        return head + _store(blob, 1)

    def frame_tile(width: int, height: int, color) -> bytes:
        head = struct.pack("<HH", 1, 1) + b"\x00" * 0x70
        tile = struct.pack("<HHHHH", 0, 0, 0, width, height) + b"\x00" * 0x52
        return head + tile + bytes(color) * (width * height)

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.join(root, "_g00_test")
    os.makedirs(out_dir, exist_ok=True)

    samples = {}
    # 3x2，每个像素 BGR
    v0_pixels = bytes([10, 20, 30,  40, 50, 60,  70, 80, 90,
                       100, 110, 120,  130, 140, 150,  160, 170, 180])
    samples["v0.g00"] = (g00_v0(3, 2, v0_pixels), (3, 2), "RGB", (0, 0), (30, 20, 10))
    # 2x2 调色板：0=黑不透明，1=红半透明（BGRA）
    pal = bytes([0, 0, 0, 255,  0, 0, 255, 128])
    samples["v1.g00"] = (g00_v1(2, 2, pal, bytes([0, 1, 1, 0])), (2, 2), "RGBA", (1, 0), (255, 0, 0, 128))
    # 4x4 分块图，第 0 帧铺满
    samples["v2.g00"] = (g00_v2(4, 4, [(0, 0, frame_tile(4, 4, (30, 20, 10, 255))),
                                       (2, 2, frame_tile(2, 2, (60, 50, 40, 255)))]),
                         (4, 4), "RGBA", (0, 0), (10, 20, 30, 255))

    print("== G00 解码自测 ==")
    for name, (blob, size, mode, pos, pixel) in samples.items():
        path = os.path.join(out_dir, name)
        with open(path, "wb") as f:
            f.write(blob)
        img = g00_to_image(blob)
        if img is None:
            print(f"[失败] {name}: 没解出来")
            continue
        got = img.getpixel(pos)
        ok = img.size == size and img.mode == mode and tuple(got) == tuple(pixel)
        print(f"[{'通过' if ok else '失败'}] {name}: {img.size[0]}x{img.size[1]} {img.mode} "
              f"像素{pos}={got} 期望={size} {mode} {tuple(pixel)}")

    print("-- 坏数据 --")
    cases = {
        "空数据": b"",
        "截断的头": samples["v0.g00"][0][:0x10],
        "type 3": bytes([3]) + struct.pack("<HH", 4, 4) + b"\x00" * 16,
        "像素不够": g00_v0(3, 2, v0_pixels)[:20],
        "乱码": bytes(range(1, 64)),
    }
    for label, bad in cases.items():
        try:
            img = g00_to_image(bad)
            print(f"[{'通过' if img is None else '失败'}] {label}: {img}")
        except Exception as e:      # 自测里直接崩了也算失败
            print(f"[失败] {label}: {type(e).__name__}: {e}")

    print("-- 回引（match）分支 --")
    # 1 个字面量 + 1 个回引：b"A" * 8
    stream = struct.pack("<II", 12, 8) + bytes([0x01, 0x41, 0x15, 0x00])
    got = lz_decompress(stream, 0, 2, 1)
    print(f"[{'通过' if got == b'A' * 8 else '失败'}] 距离 1 回引: {got}")

    print("-- 越界的 tile（应该只裁掉，不崩）--")
    clipped = g00_v2(4, 4, [(3, 3, frame_tile(4, 4, (1, 2, 3, 255)))])
    img = g00_to_image(clipped)
    print(f"[{'通过' if img is not None and img.size == (4, 4) else '失败'}] 3,3 起 4x4 的 tile: "
          f"{img.size if img else None} 右下角={img.getpixel((3, 3)) if img else None}")

    print("-- 帧数据被截断 --")
    cut = g00_v2(8, 8, [(0, 0, frame_tile(8, 8, (1, 2, 3, 255)))])
    img = g00_to_image(cut[:-40])
    print(f"[{'通过' if img is None else '失败'}] 截断帧: {img}")
    print(f"样本目录: {out_dir}")
