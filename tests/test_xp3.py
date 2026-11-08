"""XP3 回归测试：普通 xp3 + 塞进 exe 里的 xp3

自己按规范造一个最小的 xp3，验证能列出来、能读出内容。
"""
import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unpacker.core import FileView  # noqa: E402
from unpacker.formats.xp3 import MAGIC, detect  # noqa: E402


def u32(v):
    return struct.pack("<I", v)


def u64(v):
    return struct.pack("<Q", v)


def build_xp3(payload: bytes, name: str) -> bytes:
    # 布局：magic(11) + dir_offset(8) + payload + index
    head = MAGIC + u64(0x13)          # 数据紧跟头后
    data_off = 0x13
    index = bytearray()

    name_bytes = name.encode("utf-16-le")
    info_payload = (u32(0) + u64(len(payload)) + u64(len(payload))
                    + struct.pack("<H", len(name)) + name_bytes)
    segm_payload = struct.pack("<i", 0) + u64(data_off) + u64(len(payload)) + u64(len(payload))
    adlr_payload = u32(0x12345678)

    inner = bytearray()
    inner += u32(0x6F666E69) + u64(len(info_payload)) + info_payload      # info
    inner += u32(0x6D676573) + u64(len(segm_payload)) + segm_payload      # segm
    inner += u32(0x726C6461) + u64(len(adlr_payload)) + adlr_payload      # adlr

    index += u32(0x656C6946) + u64(len(inner)) + inner                     # File

    assert len(head) == data_off, (len(head), data_off)
    dir_off = data_off + len(payload)
    body = head + payload + bytes([0]) + u64(len(index)) + bytes(index)
    body = body[:0x0B] + u64(dir_off) + body[0x13:]
    return body


def check(blob: bytes, tag: str):
    fd, path = tempfile.mkstemp(suffix=".xp3")
    os.close(fd)
    try:
        with open(path, "wb") as f:
            f.write(blob)
        view = FileView(path)
        try:
            entries = detect(view)
            assert entries and len(entries) == 1, f"{tag}: {entries}"
            e = entries[0]
            assert e.name == "test.png", e.name
            data = view.bytes(e.offset, e.size)
            assert data == b"HELLO-XP3", data
            print(f"{tag}: OK  name={e.name} size={e.size}")
        finally:
            view.close()
    finally:
        os.remove(path)


def main():
    blob = build_xp3(b"HELLO-XP3", "test.png")
    check(blob, "standalone")

    prefix = b"MZ" + os.urandom(0x2000)
    check(prefix + blob, "embedded-in-exe")


if __name__ == "__main__":
    main()
