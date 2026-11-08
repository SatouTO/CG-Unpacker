"""回归：注册了解码器的后缀必须都在 IMAGE_EXTS 里，否则「只要图片」会把它们整个跳过"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unpacker.core import Entry  # noqa: E402
from unpacker.extract import IMAGE_EXTS, is_image_entry  # noqa: E402

# 每个内置图片解码器对应的典型后缀
REQUIRED = {
    "QNT": [".qnt"],
    "DCF": [".dcf"],
    "AJP": [".ajp"],
    "TLG": [".tlg"],
    "G00": [".g00"],
    "通用": [".png", ".jpg", ".bmp", ".webp", ".gif"],
}


def main():
    bad = []
    for label, exts in REQUIRED.items():
        for ext in exts:
            if ext not in IMAGE_EXTS:
                bad.append(f"{label}: {ext} 不在 IMAGE_EXTS 里")
                continue
            if not is_image_entry(Entry("x" + ext, 0, 1)):
                bad.append(f"{label}: is_image_entry(x{ext}) 是 False")

    for name in ("readme.txt", "script.nss", "audio.ogg"):
        if is_image_entry(Entry(name, 0, 1)):
            bad.append(f"{name} 被误判成图片")

    print("IMAGE_EXTS:", sorted(IMAGE_EXTS))
    if bad:
        print("FAIL")
        for b in bad:
            print("  -", b)
        return 1
    print("图片后缀回归 OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
