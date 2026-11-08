"""健壮性冒烟测试：随便喂垃圾数据，任何探测器都不许崩"""
import os
import random
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unpacker.core import FileView, NotThisFormat  # noqa: E402
from unpacker.registry import _ARCHIVES, archive_formats, image_decoders  # noqa: E402

MAGICS = [b"", b"YPF0", b"NPA\x00", b"pf8", b"XP3\r\n \n\x1a\x8b\x67\x01",
          b"AFAH", b"PK\x03\x04", b"dcf ", b"QNT\x00", b"AJP\x00", b"TLG6.0\x00"]


def run_case(payload: bytes, label: str) -> int:
    fd, path = tempfile.mkstemp(suffix=".bin")
    os.close(fd)
    try:
        with open(path, "wb") as f:
            f.write(payload)
        view = FileView(path)
        crashes = 0
        try:
            for name, _desc, detector in _ARCHIVES:
                try:
                    detector(view)
                except NotThisFormat:
                    pass
                except Exception as e:  # noqa: BLE001
                    crashes += 1
                    print(f"  [CRASH] {label} -> {name}: {type(e).__name__}: {e}")
            for desc, fn in image_decoders():
                try:
                    fn(payload, None)
                except Exception as e:  # noqa: BLE001
                    crashes += 1
                    print(f"  [CRASH-IMG] {label} -> {desc}: {type(e).__name__}: {e}")
        finally:
            view.close()
        return crashes
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def main():
    random.seed(1234)
    total = 0
    cases = 0
    for magic in MAGICS:
        for size in (0, 1, 7, 32, 300, 4096):
            junk = bytes(random.getrandbits(8) for _ in range(size))
            total += run_case(magic + junk, f"{magic!r}+{size}")
            cases += 1
    # 一些"半像"的情况：魔数对、字段是 0 或 0xFFFFFFFF
    for magic in MAGICS:
        if not magic:
            continue
        for fill in (b"\x00" * 64, b"\xff" * 64):
            total += run_case(magic + fill, f"{magic!r}+fill")
            cases += 1
    print(f"\nformats: {[n for n, _ in archive_formats()]}")
    print(f"image decoders: {[d for d, _ in image_decoders()]}")
    print(f"cases: {cases}, crashes: {total}")
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
