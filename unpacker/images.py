"""图片解码器：QNT / DCF / AJP + 通用格式直读"""
import io
import struct
import zlib

from PIL import Image

from .registry import register_image

AJP_KEY = bytes([0x5D, 0x91, 0xAE, 0x87, 0x4A, 0x56, 0x41, 0xCD,
                 0x83, 0xEC, 0x4C, 0x92, 0xB5, 0xCB, 0x16, 0x34])


def bgra_to_image(buf: bytes, w: int, h: int, bpp: int) -> Image.Image:
    """C# 那边给的是 BGR/BGRA 字节序，直接喂给 Pillow"""
    if bpp == 3:
        return Image.frombuffer("RGB", (w, h), bytes(buf), "raw", "BGR", w * 3, 1)
    return Image.frombuffer("RGBA", (w, h), bytes(buf), "raw", "BGRA", w * 4, 1)


def _i32(b, o):
    return struct.unpack_from("<i", b, o)[0]


def _u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


# ---------------------------------------------------------------- QNT
def qnt_decode(d: bytes):
    """返回 (width, height, bpp, bgra_bytes)，bpp 为 3 或 4"""
    if d[:4] != b"QNT\x00" or len(d) < 0x30:
        return None
    ver = _i32(d, 4)
    if ver == 0:
        w, h = _i32(d, 0x10), _i32(d, 0x14)
        rgb_sz, a_sz, hs = _u32(d, 0x20), _u32(d, 0x24), 0x30
    else:
        hs = _i32(d, 8)
        w, h = _i32(d, 0x14), _i32(d, 0x18)
        rgb_sz, a_sz = _u32(d, 0x24), _u32(d, 0x28)
    if not (0 < w <= 20000 and 0 < h <= 20000):
        return None
    wp, hp = (w + 1) & ~1, (h + 1) & ~1
    need = hp * wp * 3
    z = zlib.decompressobj()
    inp = z.decompress(d[hs:hs + rgb_sz], need)
    if len(inp) < need:
        raise ValueError("QNT RGB 数据不完整")
    mbpp = 4 if a_sz else 3
    alpha = None
    if a_sz:
        z2 = zlib.decompressobj()
        alpha = z2.decompress(d[hs + rgb_sz:hs + rgb_sz + a_sz], wp * h)
        if len(alpha) < wp * h:
            alpha = None
            mbpp = 3

    out = bytearray(w * h * mbpp)
    stride = mbpp * w
    src = 0
    for ch in range(3):
        dst = ch
        for _ in range(h >> 1):
            for _ in range(w):
                out[dst] = inp[src]
                out[dst + stride] = inp[src + 1]
                src += 2
                dst += mbpp
            dst += stride
            src += 2 * (w & 1)
        if h & 1:
            for _ in range(w):
                out[dst] = inp[src]
                src += 2
                dst += mbpp
            src += 2 * (w & 1)
    if mbpp == 4:
        src = 0
        dst = 3
        for _ in range(h):
            for _ in range(w):
                out[dst] = alpha[src]
                src += 1
                dst += 4
            src += w & 1

    dst = mbpp
    n = stride - mbpp
    while n:
        out[dst] = (out[dst - mbpp] - out[dst]) & 0xFF
        dst += 1
        n -= 1
    for _ in range(h - 1, 0, -1):
        for _ in range(mbpp):
            out[dst] = (out[dst - stride] - out[dst]) & 0xFF
            dst += 1
        for _ in range(stride - mbpp):
            out[dst] = (((out[dst - stride] + out[dst - mbpp]) >> 1) - out[dst]) & 0xFF
            dst += 1
    return w, h, mbpp, bytes(out)


def qnt_to_image(d: bytes) -> Image.Image | None:
    res = qnt_decode(d)
    if not res:
        return None
    w, h, bpp, buf = res
    return bgra_to_image(buf, w, h, bpp)


@register_image("QNT (AliceSoft 位图)")
def _qnt(data, ctx):
    return qnt_to_image(data)


# ---------------------------------------------------------------- DCF
def _rot_l(v: int, s: int) -> int:
    s %= 8
    return ((v << s) | (v >> (8 - s))) & 0xFF


def dcf_decode(d: bytes, base_lookup=None):
    """差分图：叠加在底图 QNT 上，返回 (w, h, bpp, bgra)"""
    if d[:4] != b"dcf " or len(d) < 0x20:
        return None
    hs = _u32(d, 4)
    dp = 8 + hs
    if _i32(d, 8) != 1:
        return None
    w, h = _u32(d, 0x0C), _u32(d, 0x10)
    nl = _i32(d, 0x18)
    if nl <= 0 or nl > 0x400:
        return None
    shift = (nl % 7) + 1
    nb = bytes(_rot_l(d[0x1C + i], shift) for i in range(nl))
    try:
        base_name = nb.decode("gbk")
    except UnicodeDecodeError:
        base_name = nb.decode("cp932", "replace")

    mask = None
    pos = dp
    qnt_pos = None
    while pos + 8 <= len(d):
        cid = _u32(d, pos)
        csz = _u32(d, pos + 4)
        payload = pos + 8
        if payload + csz > len(d):
            break
        if cid == 0x6C646664:            # dfdl
            usz = _i32(d, payload)
            if usz > 0:
                mask = zlib.decompressobj().decompress(d[payload + 4:payload + csz], usz)
        elif cid in (0x64676364, 0x64676370):   # dcgd / pcgd
            qnt_pos = payload
            break
        pos = payload + csz
    if qnt_pos is None:
        return None

    overlay = qnt_decode(d[qnt_pos:])
    if not overlay:
        return None
    ow, oh, obpp, obuf = overlay

    stem = base_name
    for ext in (".png", ".qnt", ".dcf", ".pcf"):
        if stem.lower().endswith(ext):
            stem = stem[:-len(ext)]
            break
    base = base_lookup(stem) if base_lookup else None
    if base is None or mask is None or (base[0], base[1]) != (ow, oh):
        return ow, oh, obpp, obuf

    bw, bh, bbpp, bbuf = base
    ov = bytearray(obuf)
    bs, os_ = bbpp, obpp
    bstride, ostride = bw * bs, ow * os_
    mpos = 4
    for by in range(bh // 16):
        bpos = by * 16 * bstride
        dpos = by * 16 * ostride
        for bx in range(bw // 16):
            if mpos >= len(mask):
                break
            flag = mask[mpos]
            mpos += 1
            if flag == 0:
                continue
            for r in range(16):
                s = bpos + r * bstride + bx * 16 * bs
                t = dpos + r * ostride + bx * 16 * os_
                for _ in range(16):
                    ov[t] = bbuf[s]
                    ov[t + 1] = bbuf[s + 1]
                    ov[t + 2] = bbuf[s + 2]
                    if os_ == 4:
                        ov[t + 3] = bbuf[s + 3] if bs == 4 else 0xFF
                    s += bs
                    t += os_
    return ow, oh, obpp, bytes(ov)


@register_image("DCF (AliceSoft 差分图)")
def _dcf(data, ctx):
    def lookup(stem):
        if ctx is None:
            return None
        cached = ctx.memo.get("qnt:" + stem.lower())
        if cached is not None:
            return cached
        for ext in (".qnt", ".dcf", ".pcf"):
            raw = ctx.get(stem + ext)
            if not raw:
                continue
            try:
                if ext == ".qnt":
                    res = qnt_decode(raw)
                    ctx.memo["qnt:" + stem.lower()] = res
                    return res
            except Exception:
                return None
        return None

    res = dcf_decode(data, lookup)
    if not res:
        return None
    w, h, bpp, buf = res
    return bgra_to_image(buf, w, h, bpp)


# ---------------------------------------------------------------- AJP
@register_image("AJP (AliceSoft JPEG)")
def _ajp(data, ctx):
    if data[:4] != b"AJP\x00" or len(data) < 0x28:
        return None
    ver = _i32(data, 4)
    w, h = _u32(data, 0x0C), _u32(data, 0x10)
    ioff, isize = _u32(data, 0x14), _u32(data, 0x18)
    aoff, asize = _u32(data, 0x1C), _u32(data, 0x20)
    aunc = _u32(data, 0x24) if ver > 0 else 0
    if not (0 < w <= 20000 and 0 < h <= 20000) or ioff + isize > len(data):
        return None
    img = bytearray(data[ioff:ioff + isize])
    for i in range(min(16, len(img))):
        img[i] ^= AJP_KEY[i]
    im = Image.open(io.BytesIO(bytes(img))).convert("RGB")
    if aoff and asize and aunc and aoff + asize <= len(data):
        m = bytearray(data[aoff:aoff + asize])
        for i in range(min(16, len(m))):
            m[i] ^= AJP_KEY[i]
        try:
            alpha = zlib.decompressobj().decompress(bytes(m), w * h)
        except zlib.error:
            alpha = b""
        if len(alpha) >= w * h:
            im = im.convert("RGBA")
            im.putalpha(Image.frombytes("L", (w, h), alpha[:w * h]))
    return im


# ---------------------------------------------------------------- 通用图片
@register_image("通用图片 (PNG/JPG/BMP/WebP/GIF)")
def _generic(data, ctx):
    if len(data) < 16:
        return None
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception:
        return None
    if im.width < 2 or im.height < 2:
        return None
    if im.mode not in ("RGB", "RGBA", "L", "LA", "P", "I;16"):
        im = im.convert("RGBA")
    return im
