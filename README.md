# CG Unpacker v1.2

在 Windows 上打开游戏资源包、预览图片，挑选单张或多张导出。可以保留原文件，也可以将支持的图片转换为 PNG。

## 下载 Windows 版

下载 `CG-Unpacker-v1.2-Windows-x64.zip`，完整解压后双击 `CG Unpacker.exe`。程序自带运行环境，请将 `_internal` 文件夹与 EXE 放在同一目录。[下载 Windows x64 版](https://github.com/SatouTO/CG-Unpacker/releases/download/v1.2/CG-Unpacker-v1.2-Windows-x64.zip) · [所有版本](https://github.com/SatouTO/CG-Unpacker/releases)

## 打开与导出

1. 点 **打开文件** 或按 **Ctrl+O** 选择一个资源包，也可以拖一个资源包进窗口。左侧会列出包内文件，选中图片可在右侧预览。
2. 按住 **Ctrl** 加选，或按住 **Shift** 连选，然后点 **解包选中**。双击某一行会导出该文件。需要导出整包时，点 **全部解包**。
3. 查看解包按钮下方的保存路径。勾选 **解包时问保存位置** 后，单个资源包会直接导出到本次选择的目录。取消勾选后，路径栏会显示默认目录下以包名命名的子文件夹。点 **更改目录…** 可选择新的默认位置。
4. 点 **批量解包文件夹** 可扫描一个目录里的多个资源包。确认后，每个包会导出到批量保存目录下各自的子文件夹。

勾选 **只要图片** 可过滤其他文件。勾选 **转成 PNG** 会转换可解码的图片。更多操作见[使用说明](使用说明.txt)。

## 支持的格式

| 资源包 | 支持范围 |
| --- | --- |
| AFA | AliceSoft AFA v1、v2、v3 |
| ALD | AliceSoft System 3/4 |
| XP3 | KiriKiri XP3，支持未加密索引 |
| ZIP | 普通 ZIP 和使用其他扩展名的 ZIP |

可预览和转换的图片包括 AliceSoft 的 QNT、DCF、AJP，以及 PNG、JPG、BMP、WebP、GIF。DCF 可使用同包里的底图合成完整画面。

## 从源码运行

Windows 上可使用带 Tk 的 Python 3.12。在源码目录执行以下命令。Pillow 用于图片解码，tkinterdnd2 提供拖拽支持。

```powershell
py -3.12 -m pip install Pillow tkinterdnd2
py -3.12 main.pyw
```

命令行提供格式查询、文件列表和导出操作。

```powershell
py -3.12 -m unpacker.cli --formats
py -3.12 -m unpacker.cli "CG.afa" --list
py -3.12 -m unpacker.cli "CG.afa" -o "输出目录"
py -3.12 -m unpacker.cli "*.afa" -o "输出目录" --all
```

## 构建 Windows x64 程序

在安装了上述依赖的 64 位 Python 环境中执行以下命令。

```powershell
py -3.12 -m pip install PyInstaller
py -3.12 -m PyInstaller --noconfirm --onedir --windowed --name "CG Unpacker" --icon icon.ico --collect-data tkinterdnd2 main.pyw
```

构建结果在 `dist\CG Unpacker\` 中。分发时将整个目录连同[使用说明](使用说明.txt)一起压缩。
