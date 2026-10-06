<#
.SYNOPSIS
    使用 Nuitka（onefile + UPX）打包 EuoraCraft Launcher。
.DESCRIPTION
    前置条件：
      1. 前端产物已构建：cd frontend && pnpm build
      2. .venv 已安装运行依赖：.venv\Scripts\python -m pip install -e .
      3. 已安装 Nuitka（含 onefile 压缩依赖）：.venv\Scripts\python -m pip install "nuitka[onefile]"
    产物：build-nuitka\main.exe（单文件）。
    说明：
      - 使用 --onefile-no-compression + UPX 而非 onefile 内置 zstd 压缩，
        实测前者产物更小（UPX 对 PE 压缩更有效，zstd 二次压缩反而更大）。
      - beta/release 版本自动隐藏控制台窗口；alpha/dev 保留控制台便于排错。
      - 构建前临时精简 PIL 原生插件（仅保留 _imaging 核心与截图必需的 _webp），
        构建后自动恢复，缩小最终体积。
.EXAMPLE
    .\packaging\build-nuitka.ps1
    .\packaging\build-nuitka.ps1 -UpxDir "C:\path\to\upx-5.2.0-win64"
#>
param(
    [string]$UpxDir = "C:\app\upx-5.2.0-win64",
    [switch]$Lto
)

$ErrorActionPreference = "Stop"
# 某些宿主（如通过 & 调用）下 $PSScriptRoot 可能为空，改用 $MyInvocation 解析脚本路径
$scriptPath = $MyInvocation.MyCommand.Path
if ($scriptPath) {
    $Root = Split-Path -Parent (Split-Path -Parent $scriptPath)
} else {
    $Root = (Get-Location).Path
}
Set-Location $Root

$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { throw "未找到 .venv\Scripts\python.exe，请先创建虚拟环境并安装依赖" }

# 与 PyInstaller spec 保持一致的窗口策略：beta/release 隐藏控制台，其余保留
$consoleMode = "force"
$versionFile = Join-Path $Root "ECL\foundation\version.py"
if (Test-Path $versionFile) {
    $text = Get-Content $versionFile -Raw -Encoding UTF8
    if ($text -match '__version_type__\s*=\s*["'']([^"'']+)["'']') {
        if ($Matches[1] -in @("beta", "release")) { $consoleMode = "disable" }
    }
}
if ($env:ECL_CONSOLE) { $consoleMode = if ($env:ECL_CONSOLE -eq "1") { "force" } else { "disable" } }

$upxArgs = @()
if (Test-Path $UpxDir) { $upxArgs = @("--enable-plugin=upx", "--upx-binary=$UpxDir") }
$ltoArgs = @()
if ($Lto) { $ltoArgs = @("--lto=yes") }

# 临时精简 PIL 原生插件，构建结束后恢复（ECL 只用 _imaging 核心 + 截图 WEBP 的 _webp）
$pilDir = Join-Path $Root ".venv\Lib\site-packages\PIL"
$pilBackup = Join-Path $env:TEMP ("pil-build-backup-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $pilBackup -Force | Out-Null
$pilRemoved = @()

try {
    if (Test-Path $pilDir) {
        Get-ChildItem $pilDir -File -Filter "*.pyd" | Where-Object {
            $_.BaseName -match '^_avif|^_imagingft|^_imagingcms|^_imagingmath|^_imagingmorph|^_imagingtk'
        } | ForEach-Object {
            Move-Item -LiteralPath $_.FullName -Destination (Join-Path $pilBackup $_.Name) -Force
            $pilRemoved += $_.Name
        }
    }
    if ($pilRemoved.Count -gt 0) {
        Write-Host "已临时精简 PIL 插件：$($pilRemoved -join ', ')"
    }

    & $Py packaging/build-host-dependencies.py
    if ($LASTEXITCODE -ne 0) { throw "生成宿主依赖清单失败" }

    & $Py -m nuitka `
        --mode=onefile `
        --onefile-no-compression `
        @ltoArgs `
        --windows-console-mode=$consoleMode `
        --assume-yes-for-downloads `
        --output-dir=build-nuitka `
        --windows-icon-from-ico=resources/img/logo.ico `
        @upxArgs `
        --include-package=ECL `
        --include-package=pytauri `
        --include-package=pytauri_plugins `
        --include-package=pytauri_utils `
        --include-package=pytauri_wheel `
        --include-module=pytauri_wheel.ext_mod `
        --include-distribution-metadata=pytauri-wheel `
        --include-package-data=easytier_pyo3 `
        --include-data-files=.venv/Lib/site-packages/easytier_pyo3/Packet.dll=easytier_pyo3/Packet.dll `
        --include-data-files=.venv/Lib/site-packages/easytier_pyo3/wintun.dll=easytier_pyo3/wintun.dll `
        --include-raw-dir=resources=resources `
        --include-data-dir=frontend/dist=frontend/dist `
        --include-data-dir=capabilities=capabilities `
        --include-data-files=Tauri.toml=Tauri.toml `
        main.py
} finally {
    if ($pilRemoved.Count -gt 0) {
        foreach ($name in $pilRemoved) {
            Move-Item -LiteralPath (Join-Path $pilBackup $name) -Destination (Join-Path $pilDir $name) -Force
        }
        Write-Host "已恢复 PIL 插件：$($pilRemoved -join ', ')"
    }
    Remove-Item $pilBackup -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "打包完成：$Root\build-nuitka\main.exe"
