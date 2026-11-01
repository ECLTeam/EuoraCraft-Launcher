param(
    [string] $Python = 'python',
    [string] $OutputDirectory = 'build/native-runtime',
    [string] $SourceRepository = ''
)

$ErrorActionPreference = 'Stop'
$taskRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$taskOutput = if ([IO.Path]::IsPathRooted($OutputDirectory)) { $OutputDirectory } else { Join-Path $taskRoot $OutputDirectory }
$taskOutput = [IO.Path]::GetFullPath($taskOutput)
$manifest = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'source.json') -Raw | ConvertFrom-Json
$taskSource = Join-Path $taskOutput 'source'
$taskTools = Join-Path $taskOutput 'tools'
$taskWheels = Join-Path $taskOutput 'wheels'
$taskPython = (& $Python -c 'import sys; print(sys.executable)')
if ($LASTEXITCODE -ne 0) { throw '无法执行目标 Python' }
$taskAbi = (& $taskPython -c 'import platform,sys; print(f"{sys.version_info.major}.{sys.version_info.minor}:{platform.machine()}")')
if ($taskAbi -notmatch '^3\.(11|12):AMD64$') { throw '本构建当前仅验收 Windows x64 Python 3.11/3.12' }
$compiler = (& rustc --version)
if ($LASTEXITCODE -ne 0 -or -not $compiler.StartsWith("rustc $($manifest.rust_version) ")) {
    throw "需要固定 Rust $($manifest.rust_version)，可用 rustup toolchain install 安装后选择该工具链"
}
if (Test-Path -LiteralPath $taskSource) { throw '源码目录已存在；请指定新的 OutputDirectory，脚本不会重置或覆盖工作目录' }

function Assert-TaskHash([string] $File, [string] $Expected) {
    if ((Get-FileHash -LiteralPath $File -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Expected) {
        throw "构建输入摘要不一致：$([IO.Path]::GetFileName($File))"
    }
}

$patch = Join-Path $PSScriptRoot 'runtime.patch'
$policy = Join-Path $PSScriptRoot 'plugin_isolation.rs'
Assert-TaskHash $patch $manifest.patch_sha256
Assert-TaskHash $policy $manifest.policy_sha256
New-Item -ItemType Directory -Path $taskOutput, $taskWheels -Force | Out-Null
if (-not $SourceRepository) { $SourceRepository = $manifest.upstream }
& git clone -c core.autocrlf=false -c core.eol=lf --no-checkout $SourceRepository $taskSource
if ($LASTEXITCODE -ne 0) { throw '固定源码获取失败' }
& git -C $taskSource checkout --detach $manifest.commit
if ($LASTEXITCODE -ne 0) { throw '固定源码提交不存在' }
$commit = (& git -C $taskSource rev-parse HEAD)
if ($commit -ne $manifest.commit) { throw '源码提交身份不一致' }
& git -C $taskSource apply --check $patch
if ($LASTEXITCODE -ne 0) { throw '固定补丁预检失败' }
& git -C $taskSource apply $patch
if ($LASTEXITCODE -ne 0) { throw '固定补丁应用失败' }
Copy-Item -LiteralPath $policy -Destination (Join-Path $taskSource 'crates/pytauri-core/src/plugin_isolation.rs')
Assert-TaskHash (Join-Path $taskSource 'Cargo.lock') $manifest.cargo_lock_sha256
Copy-Item -LiteralPath (Join-Path $taskSource 'LICENSE') -Destination (Join-Path $taskSource 'python/pytauri-wheel/LICENSE')
Copy-Item -LiteralPath (Join-Path $taskRoot 'LICENSE') -Destination (Join-Path $taskSource 'python/pytauri-wheel/ECL-LICENSE')

if (-not (Test-Path -LiteralPath $taskTools)) {
    & $taskPython -m venv $taskTools
    if ($LASTEXITCODE -ne 0) { throw '独立构建环境创建失败' }
}
$toolPython = Join-Path $taskTools 'Scripts/python.exe'
$toolAbi = (& $toolPython -c 'import platform,sys; print(f"{sys.version_info.major}.{sys.version_info.minor}:{platform.machine()}")')
if ($taskAbi -ne $toolAbi) { throw '构建工具解释器 ABI 不一致，请使用新的输出目录' }
& $toolPython -m pip install --require-hashes --no-deps -r (Join-Path $PSScriptRoot 'build-tools.txt')
if ($LASTEXITCODE -ne 0) { throw '固定构建工具安装失败' }

$taskEnvironment = @{}
foreach ($key in @('PYO3_PYTHON', 'CARGO_TARGET_DIR', 'CARGO_BUILD_JOBS', 'ECL_EXPECTED_WEBVIEW2_VERSION')) {
    $taskEnvironment[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
}
try {
    $env:PYO3_PYTHON = $taskPython
    $env:ECL_EXPECTED_WEBVIEW2_VERSION = $manifest.webview2_version
    if (-not $env:CARGO_TARGET_DIR) { $env:CARGO_TARGET_DIR = Join-Path $taskOutput 'target' }
    $env:CARGO_BUILD_JOBS = '2'
    Push-Location $taskSource
    try {
        & $toolPython -m pip wheel --no-deps --no-build-isolation python/pytauri-wheel python/pytauri --wheel-dir $taskWheels
        if ($LASTEXITCODE -ne 0) { throw '固定原生运行时构建失败' }
    } finally { Pop-Location }
} finally {
    foreach ($key in $taskEnvironment.Keys) { [Environment]::SetEnvironmentVariable($key, $taskEnvironment[$key], 'Process') }
}

$abiTag = 'cp' + ($taskAbi.Split(':')[0].Replace('.', ''))
$nativeWheels = @(Get-ChildItem -LiteralPath $taskWheels -Filter "pytauri_wheel-$($manifest.version)-$abiTag-*.whl")
$pythonWheels = @(Get-ChildItem -LiteralPath $taskWheels -Filter "pytauri-$($manifest.version)-py3-none-any.whl")
if ($nativeWheels.Count -ne 1 -or $pythonWheels.Count -ne 1) { throw '实际产物数量或 ABI 不符' }
$wheels = @($nativeWheels[0], $pythonWheels[0])
$records = @($wheels | ForEach-Object { @{name=$_.Name; sha256=(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()} })
$requirements = @(
    "pytauri-wheel==$($manifest.version) --hash=sha256:$($records[0].sha256)",
    "pytauri==$($manifest.version) --hash=sha256:$($records[1].sha256)"
)
[IO.File]::WriteAllText((Join-Path $taskOutput 'install.txt'), ($requirements -join "`n") + "`n", [Text.UTF8Encoding]::new($false))
$result = @{upstream_commit=$commit; patch_sha256=$manifest.patch_sha256; policy_sha256=$manifest.policy_sha256; webview2_version=$manifest.webview2_version;
    cargo_lock_sha256=$manifest.cargo_lock_sha256; compiler=$compiler; python_abi=$taskAbi; wheels=$records}
[IO.File]::WriteAllText((Join-Path $taskOutput 'build-manifest.json'), ($result | ConvertTo-Json -Depth 8) + "`n", [Text.UTF8Encoding]::new($false))
Write-Output "固定运行时构建完成；安装清单位于：$taskOutput/install.txt"
