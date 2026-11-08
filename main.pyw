import sys

if "--formats-dump" in sys.argv:
    # 打包自检：把已注册的格式写进文件，确认动态导入在 frozen 环境里也生效
    import os

    import unpacker
    from app import app_dir

    out = os.path.join(app_dir(), "_formats.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write("FORMATS " + ", ".join(n for n, _ in unpacker.archive_formats()) + "\n")
        f.write("IMAGES  " + ", ".join(d for d, _ in unpacker.image_decoders()) + "\n")
    sys.exit(0)

from app import main

main()
