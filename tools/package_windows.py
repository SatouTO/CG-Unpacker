"""Assemble a Windows release from the PyInstaller onedir output."""
import hashlib
from importlib import metadata
from pathlib import Path
import re
import shutil
import struct
import sys
import zipfile


def package(folder: Path, version: str, guide: Path) -> Path:
    if not re.fullmatch(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?", version):
        raise ValueError("Invalid release version")
    exe = folder / "CG Unpacker.exe"
    with exe.open("rb") as stream:
        if stream.read(2) != b"MZ":
            raise ValueError("Invalid Windows EXE")
        stream.seek(60)
        stream.seek(struct.unpack("<I", stream.read(4))[0])
        if stream.read(6) != b"PE\x00\x00\x64\x86":
            raise ValueError("Expected Windows x64 EXE")
    required = ("python312.dll", "_tkinter.pyd", "_tcl_data/init.tcl", "_tk_data/tk.tcl")
    for member in required:
        if not (folder / "_internal" / member).is_file():
            raise ValueError(f"Missing Windows GUI runtime file: {member}")
    shutil.copyfile(guide, folder / "使用说明.txt")
    licenses = folder / "licenses"
    licenses.mkdir(exist_ok=True)
    shutil.copyfile(Path(sys.base_prefix) / "LICENSE.txt", licenses / "Python.txt")
    for name in ("Pillow", "tkinterdnd2", "PyInstaller"):
        dist = metadata.distribution(name)
        files = [p for p in (dist.files or ()) if ".dist-info/" in p.as_posix()
                 and p.name.lower() in ("license", "license.txt", "copying.txt")]
        if not files:
            raise ValueError(f"Missing {name} license")
        for i, license_file in enumerate(files):
            dest = f"{name}.txt" if not i else f"{name}-{i}.txt"
            shutil.copyfile(dist.locate_file(license_file), licenses / dest)
    bundle = folder.parent / f"CG-Unpacker-v{version}-Windows-x64.zip"
    paths = [exe, folder / "使用说明.txt"]
    for subfolder in (folder / "_internal", licenses):
        paths.extend(sorted(p for p in subfolder.rglob("*") if p.is_file()))
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as z:
        for path in paths:
            z.write(path, "CG Unpacker/" + path.relative_to(folder).as_posix())
    with zipfile.ZipFile(bundle) as z:
        if z.testzip() is not None:
            raise ValueError("Invalid release ZIP")
    with bundle.open("rb") as f:
        checksum = hashlib.file_digest(f, "sha256").hexdigest()
    Path(str(bundle) + ".sha256").write_text(f"{checksum}  {bundle.name}\n", encoding="ascii")
    return bundle


if __name__ == "__main__":
    print(package(Path(sys.argv[1]), sys.argv[2], Path(__file__).resolve().parents[1] / "使用说明.txt"))
