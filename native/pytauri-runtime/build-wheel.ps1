param(
    [string] $Python = 'python',
    [string] $OutputDirectory = 'build/native-runtime',
    [string] $SourceRepository = ''
)

$ErrorActionPreference = 'Stop'
$taskArguments = @((Join-Path $PSScriptRoot 'build-wheel.py'), '--output-directory', $OutputDirectory)
if ($SourceRepository) { $taskArguments += @('--source-repository', $SourceRepository) }
& $Python @taskArguments
if ($LASTEXITCODE -ne 0) { throw '固定原生窗口运行时构建失败' }
