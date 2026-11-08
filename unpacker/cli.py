"""命令行用法：
    python -m unpacker.cli 包.afa -o 输出目录
    python -m unpacker.cli 包.afa --list
    python -m unpacker.cli *.afa -o 输出 --all
"""
import argparse
import glob
import os
import sys

from .extract import ExtractOptions, extract_file
from .registry import archive_formats, open_archive_ex


def main(argv=None):
    ap = argparse.ArgumentParser("unpacker", description="CG Unpacker（命令行）")
    ap.add_argument("files", nargs="*", help="归档文件，支持通配符")
    ap.add_argument("-o", "--out", default="output", help="输出目录")
    ap.add_argument("--list", action="store_true", help="只列出内容，不解包")
    ap.add_argument("--all", action="store_true", help="导出全部文件（默认只导图片）")
    ap.add_argument("--no-png", action="store_true", help="不做 PNG 转换，原样导出")
    ap.add_argument("--flat", action="store_true", help="平铺到一层目录")
    ap.add_argument("--formats", action="store_true", help="列出支持的格式")
    args = ap.parse_args(argv)

    if args.formats:
        for name, desc in archive_formats():
            print(f"{name:<8} {desc}")
        return 0

    if not args.files:
        ap.error("至少给一个归档文件（或者用 --formats 看看支持什么）")

    paths = []
    for pattern in args.files:
        matched = glob.glob(pattern)
        paths.extend(matched or [pattern])

    rc = 0
    for path in paths:
        if not os.path.isfile(path):
            print(f"[跳过] 不是文件: {path}", file=sys.stderr)
            continue
        try:
            archive, err = open_archive_ex(path)
        except Exception as e:
            print(f"[失败] {path}: {e}", file=sys.stderr)
            rc = 1
            continue
        if archive is None:
            print(f"[无法识别] {path}: {err}", file=sys.stderr)
            rc = 1
            continue
        try:
            print(f"== {os.path.basename(path)}  [{archive.format}]  {len(archive.entries)} 个文件")
            if args.list:
                for e in archive.entries:
                    print(f"  {e.display_size:>10}  {e.name}")
                continue
            opts = ExtractOptions(
                out_dir=os.path.join(args.out, os.path.splitext(os.path.basename(path))[0]),
                only_images=not args.all,
                convert_png=not args.no_png,
                flatten=args.flat,
            )

            def report(done, total, name):
                if total and done % 25 == 0:
                    print(f"\r  {done}/{total}", end="", file=sys.stderr)

            stats = extract_file(path, opts, report)
            print(f"  -> {opts.out_dir}")
            print(f"  {stats.summary()}")
            for err in stats.errors[:5]:
                print(f"  ! {err}", file=sys.stderr)
        finally:
            archive.close()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
