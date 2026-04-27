$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$dist = Join-Path $root "dist"

if (-not (Test-Path $dist)) {
    throw "dist 폴더가 없습니다. 먼저 pyinstaller 빌드를 실행하세요."
}

$filesToCopy = @(
    "launcher_config.json",
    "app_version.json",
    "version.json"
)

foreach ($name in $filesToCopy) {
    $src = Join-Path $root $name
    $dst = Join-Path $dist $name
    if (-not (Test-Path $src)) {
        throw "필수 파일이 없습니다: $src"
    }
    Copy-Item -LiteralPath $src -Destination $dst -Force
}

Write-Host "배포 폴더 준비 완료:"
Get-ChildItem -Path $dist | Select-Object Name, Length, LastWriteTime
