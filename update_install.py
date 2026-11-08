"""在原位置更新 Windows 便携版。新旧运行库按目录整体切换。"""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time
import zipfile


_PREFIX = ".cg-update-"
_EXES = {"cg unpacker.exe", "cg-unpacker.exe", "图片解包器.exe"}
_WRAPPERS = {"cg unpacker", "cg-unpacker", "图片解包器"}
_DOCS = {"使用说明.txt", "licenses", "third-party-notices", "third-party-notices.txt"}
_MAX_EXPANDED = 1024 * 1024 * 1024
_MAX_FILE = 512 * 1024 * 1024
_RESERVED = re.compile(r"^(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|(?:COM|LPT)[0-9¹²³])$", re.I)


@dataclass(frozen=True)
class PreparedUpdate:
    staging_dir: Path


def _valid_name(part):
    return (part and len(part) <= 255 and not part.endswith((".", " "))
            and not any(ord(c) < 32 or c in '<>:"|?*' for c in part)
            and not _RESERVED.match(part.partition(".")[0].rstrip()))


def _parts(info):
    name = info.orig_filename.replace("\\", "/")
    parts = name.removesuffix("/").split("/")
    if (len(parts) > 48 or len(name) > 4096 or any(not _valid_name(part) for part in parts)
            or info.flag_bits & 1 or info.external_attr & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or stat.S_IFMT(info.external_attr >> 16) not in (0, stat.S_IFREG, stat.S_IFDIR)):
        raise ValueError("更新包包含不安全的文件路径或链接")
    return parts


def _unpack(z, dest, exe_name):
    infos = z.infolist()
    if (not 1 <= len(infos) <= 25000 or sum(i.file_size for i in infos) > _MAX_EXPANDED
            or any(i.file_size > _MAX_FILE for i in infos)):
        raise ValueError("更新包大小或文件数量超出限制")
    members = [(info, _parts(info)) for info in infos]
    apps = [parts for info, parts in members if not info.is_dir() and len(parts) <= 2
            and parts[-1].casefold() in _EXES]
    if len(apps) != 1:
        raise ValueError("更新包没有唯一的 CG Unpacker 程序")
    prefix = apps[0][:-1]
    if prefix and prefix[0].casefold() not in _WRAPPERS:
        raise ValueError("无法识别更新包的目录结构")

    used = set()
    for info, parts in members:
        if parts[:len(prefix)] != prefix:
            raise ValueError("更新包包含多个根目录")
        rel = parts[len(prefix):]
        if not rel:
            continue
        first = rel[0].casefold()
        if parts == apps[0]:
            rel = [exe_name]
        elif first not in (_DOCS | {"_internal"}):
            # 配置、output 等本机文件由用户管理。
            continue
        elif first in {"使用说明.txt", "third-party-notices.txt"} and (len(rel) != 1 or info.is_dir()):
            raise ValueError("更新包中的说明文件类型异常")
        elif first in {"_internal", "licenses", "third-party-notices"} and len(rel) == 1 and not info.is_dir():
            raise ValueError("更新包的运行库或许可目录类型异常")
        path = dest.joinpath(*rel)
        key = tuple(part.casefold() for part in rel)
        if key in used:
            raise ValueError("更新包包含重名文件")
        used.add(key)
        if info.is_dir():
            path.mkdir(parents=True, exist_ok=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        with z.open(info) as stream, path.open("xb") as output:
            shutil.copyfileobj(stream, output, 1024 * 1024)
        if path.stat().st_size != info.file_size:
            raise ValueError("更新包中的文件不完整")
    runtime = dest / "_internal"
    if (not (runtime / "base_library.zip").is_file()
            or not any(p.is_file() for p in runtime.glob("python3[0-9]*.dll"))):
        raise ValueError("更新包缺少必需的 Python 运行库")
    with (dest / exe_name).open("rb") as exe:
        if exe.read(2) != b"MZ":
            raise ValueError("更新包中的 EXE 格式错误")


def prepare_update(zip_path, install_dir, exe_name):
    """验证布局并解压到安装目录所在卷。返回后即可清理下载的 ZIP。"""
    if (not _valid_name(exe_name) or Path(exe_name).name != exe_name
            or not exe_name.lower().endswith(".exe") or "/" in exe_name or "\\" in exe_name):
        raise ValueError("当前程序文件名无效")
    root = Path(install_dir).resolve(strict=True)
    installed = root / exe_name
    if installed.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError("程序指向链接，无法直接在此位置更新")
    with installed.open("rb") as stream:
        original_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    staging = Path(tempfile.mkdtemp(prefix=_PREFIX, dir=root))
    job = PreparedUpdate(staging)
    try:
        payload = staging / "payload"
        payload.mkdir()
        with zipfile.ZipFile(zip_path) as bundle:
            _unpack(bundle, payload, exe_name)
        (staging / "job.json").write_text(json.dumps(
            {"exe": exe_name, "original": original_hash}, ensure_ascii=False), encoding="utf-8-sig")
        (staging / "install.ps1").write_text(_HELPER, encoding="utf-8-sig")
    except BaseException:
        discard_update(job)
        raise
    return job


def discard_update(prepared):
    """放弃本次创建的暂存目录。启动安装器后由安装器清理。"""
    path = prepared.staging_dir
    if not re.fullmatch(r"\.cg-update-[a-zA-Z0-9_-]+", path.name):
        raise ValueError("暂存目录名无效")
    if path.exists():
        if path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("暂存目录已变为链接")
        shutil.rmtree(path)


def launch_update(prepared):
    """安装器就绪后返回。调用方此时应正常关闭程序，让它完成文件切换。"""
    stage = prepared.staging_dir
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
    proc = subprocess.Popen([str(powershell), "-NoProfile", "-NonInteractive", "-NoLogo",
                             "-ExecutionPolicy", "Bypass", "-File", str(stage / "install.ps1"),
                             "-ParentId", str(os.getpid())],
                            cwd=stage.parent, env=env, creationflags=subprocess.CREATE_NO_WINDOW,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if (stage / "ready").is_file():
            return
        if proc.poll() is not None:
            raise OSError("更新安装器未能启动，请检查安装目录权限或从发布页下载")
        time.sleep(0.05)
    proc.terminate()
    proc.wait()
    raise OSError("更新安装器启动超时，当前版本仍可继续使用")


_HELPER = r'''param([int]$ParentId)
$ErrorActionPreference = 'Stop'

function Move-Entry($from, $to) {
    if ([IO.Directory]::Exists($from)) { [IO.Directory]::Move($from, $to) }
    else { [IO.File]::Move($from, $to) }
}

function Assert-PlainTree([string]$path) {
    $attributes = [IO.File]::GetAttributes($path)
    if ($attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "更新范围内含有链接: $path"
    }
    if ($attributes -band [IO.FileAttributes]::Directory) {
        foreach ($entry in [IO.Directory]::EnumerateFileSystemEntries($path)) {
            Assert-PlainTree $entry
        }
    }
}

function Start-NewVersion($path, $root, [int]$timeoutMs = 25000) {
    $process = Start-Process -FilePath $path -WorkingDirectory $root -PassThru
    $elapsed = [Diagnostics.Stopwatch]::StartNew()
    try {
        while ($elapsed.ElapsedMilliseconds -lt $timeoutMs) {
            $process.Refresh()
            if ($process.HasExited) { throw "新版提前退出，代码 $($process.ExitCode)" }
            $title = $process.MainWindowTitle
            if ($process.MainWindowHandle -ne [IntPtr]::Zero -and
                $title -match '^(CG Unpacker|游戏图片解包器|图片解包器) v\d+(?:\.\d+)+(?:\s|$)') {
                return
            }
            if ($title -match 'Unhandled exception in script|Failed to execute script|Fatal error detected') {
                throw "新版启动失败: $title"
            }
            [Threading.Thread]::Sleep(100)
        }
        throw '新版没有在规定时间内打开主窗口'
    } catch {
        # 使用 Start-Process 返回的进程对象，其他用户窗口或同名程序不受影响。
        if (-not $process.HasExited) {
            $process.Kill()
            if (-not $process.WaitForExit(10000)) { throw '新版进程未能退出' }
        }
        throw
    } finally {
        $process.Dispose()
    }
}

function Install-Update([string]$stage, $job) {
    $root = [IO.Directory]::GetParent($stage).FullName
    $exe = [string]$job.exe
    if ([IO.Path]::GetFileName($exe) -ne $exe -or [IO.Path]::GetExtension($exe) -ne '.exe') {
        throw '无效的程序文件名'
    }
    $payload = Join-Path $stage 'payload'
    $backup = Join-Path $stage 'previous'
    $target = Join-Path $root $exe
    $names = @(Get-ChildItem -LiteralPath $payload -Force | ForEach-Object Name)
    if ($exe -notin $names -or '_internal' -notin $names) { throw '暂存文件不完整' }
    $allowed = @($exe, '_internal', '使用说明.txt', 'licenses', 'third-party-notices', 'third-party-notices.txt')
    foreach ($name in $names) {
        if ($name -notin $allowed) { throw '暂存文件包含未管理的项目' }
    }
    Assert-PlainTree $stage
    $stream = [IO.File]::OpenRead($target)
    $hasher = [Security.Cryptography.SHA256]::Create()
    try { $hash = [BitConverter]::ToString($hasher.ComputeHash($stream)).Replace('-', '') }
    finally { $stream.Dispose(); $hasher.Dispose() }
    if ($hash -ne $job.original) {
        throw '程序已在更新期间被更改，请重新检查更新'
    }
    $processNames = @([IO.Path]::GetFileNameWithoutExtension($exe), 'CG Unpacker', 'CG-Unpacker', '图片解包器') | Sort-Object -Unique
    foreach ($proc in @(Get-Process -Name $processNames -ErrorAction SilentlyContinue)) {
        if ($proc.Path -and [IO.Path]::GetDirectoryName($proc.Path) -eq $root) {
            throw '还有本目录中的解包器正在运行，请关闭其他窗口后重试'
        }
    }
    # 不跟随旧运行库中的联接，也不递归删除用户目录。
    $current = @(Get-ChildItem -LiteralPath $root -Force | Where-Object { $_.Name -in $names })
    foreach ($item in $current) {
        Assert-PlainTree $item.FullName
        if ($item.PSIsContainer -ne [IO.Directory]::Exists((Join-Path $payload $item.Name))) {
            throw "文件与目录类型冲突: $($item.Name)"
        }
    }
    [void][IO.Directory]::CreateDirectory($backup)
    $saved = [Collections.Generic.List[string]]::new()
    $added = [Collections.Generic.List[string]]::new()
    $others = @($names | Where-Object { $_ -ne $exe })
    try {
        # 先撤下旧启动入口，最后就位新入口。
        foreach ($name in (@($exe) + $others)) {
            $path = Join-Path $root $name
            if (Test-Path -LiteralPath $path) {
                Move-Entry $path (Join-Path $backup $name)
                $saved.Add($name)
            }
        }
        foreach ($name in ($others + @($exe))) {
            Move-Entry (Join-Path $payload $name) (Join-Path $root $name)
            $added.Add($name)
        }
        Start-NewVersion $target $root
    } catch {
        $failure = $_.Exception.Message
        try {
            $added.Reverse()
            foreach ($name in $added) { Move-Entry (Join-Path $root $name) (Join-Path $payload $name) }
            $saved.Reverse()
            foreach ($name in $saved) { Move-Entry (Join-Path $backup $name) (Join-Path $root $name) }
        } catch {
            [IO.File]::WriteAllText((Join-Path $stage 'keep-backup'), $failure)
            throw "还原失败；旧文件保存在 $backup。$($_.Exception.Message)"
        }
        throw "本次更新未能安装，已还原旧版。$failure"
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    $stage = $PSScriptRoot
    $root = [IO.Directory]::GetParent($stage).FullName
    $mutex = $null
    $locked = $false
    $ready = $false
    try {
        if ([IO.Path]::GetFileName($stage) -notmatch '^\.cg-update-[\w-]+$') { throw '暂存目录名无效' }
        $job = Get-Content -LiteralPath (Join-Path $stage 'job.json') -Raw -Encoding UTF8 | ConvertFrom-Json
        $bytes = [Text.Encoding]::UTF8.GetBytes($root.ToUpperInvariant())
        $id = [BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($bytes)).Replace('-', '')
        $mutex = [Threading.Mutex]::new($false, "Local\CGUnpacker-Update-$id")
        try { $locked = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $locked = $true }
        if (-not $locked) { throw '另一个更新正在进行' }
        $parent = Get-Process -Id $ParentId -ErrorAction SilentlyContinue
        [IO.File]::WriteAllText((Join-Path $stage 'ready'), '1')
        $ready = $true
        if ($null -ne $parent -and -not $parent.WaitForExit(120000)) {
            throw '旧程序尚未退出，请关闭解包器后重试'
        }
        Install-Update $stage $job
    } catch {
        $failure = $_.Exception.Message
        if ($ready) {
            Add-Type -AssemblyName System.Windows.Forms
            [void][Windows.Forms.MessageBox]::Show($failure, 'CG Unpacker 更新', 'OK', 'Error')
        }
    } finally {
        if ($locked) { $mutex.ReleaseMutex() }
        if ($mutex) { $mutex.Dispose() }
        if ($ready -and -not (Test-Path -LiteralPath (Join-Path $stage 'keep-backup'))) {
            $resolved = [IO.Path]::GetFullPath($stage)
            if ([IO.Directory]::GetParent($resolved).FullName -eq $root -and
                [IO.Path]::GetFileName($resolved) -match '^\.cg-update-[\w-]+$') {
                Assert-PlainTree $resolved
                Remove-Item -LiteralPath $resolved -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
    }
}
'''
