param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath $PSScriptRoot
try {
    $versionMatch = Select-String -Path 'app.py' -Pattern '^APP_VER = "([0-9.]+)"$'
    if (!$versionMatch) { throw 'Missing application version' }
    $version = $versionMatch.Matches[0].Groups[1].Value
    & $Python -m PyInstaller --noconfirm --clean --onedir --windowed --name 'CG Unpacker' --icon icon.ico --specpath build --collect-submodules unpacker --collect-data tkinterdnd2 main.pyw
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed' }
    & $Python 'tools/package_windows.py' 'dist/CG Unpacker' $version '使用说明.txt'
    if ($LASTEXITCODE -ne 0) { throw 'Release packaging failed' }
}
finally { Pop-Location }
