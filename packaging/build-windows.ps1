<#
.SYNOPSIS
    使用 PyInstaller 打包 EuoraCraft Launcher（Windows 单文件）。
.DESCRIPTION
    前置条件：
      1. 前端产物已构建：cd frontend && pnpm build
      2. .venv 已安装运行与打包依赖：.venv\Scripts\python -m pip install -e ".[dev]"
    产物：dist\EuoraCraft Launcher.exe（单文件）。
    说明：
      - 打包内容、排除项与控制台策略统一由 EuoraCraft-Launcher.spec 定义，
        与 CI 使用同一条 PyInstaller 命令，不再单独维护一套参数。
      - beta/rc/release 版本自动隐藏控制台窗口；alpha/dev 保留控制台便于排错，
        可用 -ConsoleMode force/disable 覆盖。
      - 需要 UPX 时把解压后的 upx 目录传给 -UpxDir，spec 会把它加入 PATH。
.EXAMPLE
    .\packaging\build-windows.ps1
    .\packaging\build-windows.ps1 -UpxDir "C:\app\upx-5.2.0-win64" -ConsoleMode force
#>
param(
    [string]$UpxDir = "",
    [ValidateSet("", "force", "disable")]
    [string]$ConsoleMode = ""
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

if ($UpxDir) {
    if (-not (Test-Path $UpxDir)) { throw "未找到 UPX 目录：$UpxDir" }
    $env:ECL_UPX_DIR = $UpxDir
}
if ($ConsoleMode) {
    $env:ECL_CONSOLE = if ($ConsoleMode -eq "force") { "1" } else { "0" }
}

& $Py -m PyInstaller --noconfirm --clean "EuoraCraft-Launcher.spec"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败" }

$output = Join-Path $Root "dist\EuoraCraft Launcher.exe"
if (-not (Test-Path $output)) { throw "未找到打包产物：$output" }
$size = [math]::Round((Get-Item $output).Length / 1MB, 1)
Write-Host ""
Write-Host "打包完成：$output（$size MB）"
