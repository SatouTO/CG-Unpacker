# CG Unpacker v1.3

Windows 游戏资源包图片预览与解包工具。下载 Windows x64 压缩包，完整解压后运行 `CG Unpacker.exe`，并保持 `_internal` 文件夹与它同级。[下载 v1.3](https://github.com/SatouTO/CG-Unpacker/releases/download/v1.3/CG-Unpacker-v1.3-Windows-x64.zip) · [更新记录](CHANGELOG.md)

## 使用

1. 点“打开文件”或按 Ctrl+O 选择资源包，也可以拖一个文件进窗口。
2. 选中条目可预览图片。用 Ctrl 或 Shift 多选后点“解包选中”，或者直接双击某一张导出。
3. 解包按钮下方显示保存路径。勾选“解包时问保存位置”可在每次导出前选目录；关闭后，单包会导出到默认目录下与资源包同名的子文件夹。
4. 用“批量解包文件夹”处理多个资源包。每个包有独立的输出子文件夹。[使用说明](使用说明.txt)

## 支持的格式

| 类型 | 格式 |
| --- | --- |
| 资源包 | AFA、ALD、XP3（含 EXE 内嵌）、ZIP、YPF、NPA、PFS、RealLive G00 |
| 图片 | QNT、DCF、AJP、G00、TLG5、部分 TLG6，以及 PNG、JPEG、BMP、WebP、GIF |

加密支持依格式与游戏而异。可在“工具 → 支持的格式”查看当前版本可识别的资源包。

## 从源码运行

使用带 Tk 的 64 位 Python 3.12，在项目目录执行以下命令。

```powershell
py -3.12 -m pip install Pillow tkinterdnd2
py -3.12 main.pyw
py -3.12 -m unpacker.cli --formats
py -3.12 -m unpacker.cli "CG.afa" --list
```

## 构建 Windows 下载包

在已装好上述依赖、可直接执行 `python` 的环境中运行以下命令。

```powershell
python -m pip install PyInstaller
.\build.ps1
```

`dist` 目录内会生成 Windows x64 ZIP 和同名的 `.sha256` 校验文件。

