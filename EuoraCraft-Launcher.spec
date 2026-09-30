import os
import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

from ECL.plugins.host_dependencies import build_host_dependency_manifest

SPEC_DIR = Path(SPECPATH).resolve()

APP_NAME = "EuoraCraft Launcher"
BUNDLE_IDENTIFIER = "top.eclteam.euoracraft-launcher"


def _resolve_console_mode() -> bool:
    """根据构建类型决定是否显示命令行窗口。

    beta/release 构建面向普通用户，隐藏控制台避免弹出黑色命令行窗口；
    alpha 构建与源码启动（dev）保留控制台便于排查启动问题。显式设置 ECL_CONSOLE 时以其为准。
    """
    explicit = os.environ.get("ECL_CONSOLE")
    if explicit is not None:
        return explicit == "1"
    version_path = SPEC_DIR / "ECL" / "common" / "version.py"
    try:
        text = version_path.read_text(encoding="utf-8")
    except OSError:
        return True
    match = re.search(r'__version_type__\s*=\s*["\']([^"\']+)["\']', text)
    version_type = match.group(1) if match else ""
    return version_type not in {"beta", "release"}


CONSOLE = _resolve_console_mode()
IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
_ECL_UPX_DIR = os.environ.get("ECL_UPX_DIR", "")
if _ECL_UPX_DIR and os.path.isdir(_ECL_UPX_DIR):
    os.environ["PATH"] = _ECL_UPX_DIR + os.pathsep + os.environ.get("PATH", "")
UPX_ENABLED = os.environ.get("ECL_UPX", "1") == "1" and not IS_MACOS
def _collect_all_safe(package: str) -> tuple[list, list, list]:
    try:
        return collect_all(package)
    except Exception:
        return [], [], []


_wheel_datas, _wheel_binaries, _wheel_hiddenimports = _collect_all_safe("pytauri_wheel")
_plugin_datas, _plugin_binaries, _plugin_hiddenimports = _collect_all_safe("pytauri_plugins")
def _resolve_icon() -> str | None:
    if IS_MACOS:
        icns = SPEC_DIR / "resources" / "img" / "logo.icns"
        return str(icns) if icns.is_file() else None
    if IS_WINDOWS:
        ico = SPEC_DIR / "resources" / "img" / "logo.ico"
        return str(ico) if ico.is_file() else None
    return None
def _ensure_datasets_exist() -> None:
    required = {
        "前端构建产物 frontend/dist": SPEC_DIR / "frontend" / "dist",
        "资源目录 resources": SPEC_DIR / "resources",
        "Tauri 配置 capabilities": SPEC_DIR / "capabilities",
        "Tauri 配置 Tauri.toml": SPEC_DIR / "Tauri.toml",
        "入口脚本 main.py": SPEC_DIR / "main.py",
    }
    missing = [name for name, path in required.items() if not path.exists()]
    if missing:
        raise SystemExit(
            "缺少打包所需文件，请先检查：\n  - "
            + "\n  - ".join(missing)
            + "\n前端产物请先执行 `cd frontend && pnpm build`。"
        )


_ensure_datasets_exist()
build_host_dependency_manifest(SPEC_DIR / "resources" / "plugin_host_dependencies.json")


def _collect_msvc_runtime() -> list[tuple[str, str]]:
    """收集微软 VC++ 运行库并随包分发，使 onefile 产物自包含。

    python312.dll 等解释器 DLL 依赖 vcruntime/msvcp 系列，若目标机运行库
    版本过旧或缺失，加载 python DLL 时会报“找不到指定的模块”。这里从
    Python 目录或系统 System32 收集这些 DLL，避免依赖目标机的运行库。
    """
    if not IS_WINDOWS:
        return []
    names = ["vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll"]
    sysroot = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    bases = [Path(sys.base_prefix), sysroot / "System32"]
    result: list[tuple[str, str]] = []
    for name in names:
        for base in bases:
            candidate = base / name
            if candidate.is_file():
                result.append((str(candidate), "."))
                break
    return result


datas = [
    (str(SPEC_DIR / "frontend" / "dist"), "frontend/dist"),
    (str(SPEC_DIR / "resources"), "resources"),
    (str(SPEC_DIR / "capabilities"), "capabilities"),
    (str(SPEC_DIR / "Tauri.toml"), "."),
] + _wheel_datas + _plugin_datas + copy_metadata("pytauri-wheel")

binaries = _wheel_binaries + _plugin_binaries + _collect_msvc_runtime()
hiddenimports = [
    "ecl_plugin_sdk",
    "importlib_metadata",
    "pytauri",
    "pytauri.ffi",
    "pytauri.ffi._ext_mod",
    "pytauri_wheel",
    "pytauri_wheel.ext_mod",
    "anyio",
    "colorama",
    "dotenv",
    "httpx",
    "httpcore",
    "h11",
    "psutil",
    "pydantic",
    "pyperclip",
    "ECL.game",
    "ECL.game.auth",
] + _wheel_hiddenimports + _plugin_hiddenimports + collect_submodules("ECL")
excludes = [
    "scipy",
    "pandas",
    "matplotlib",
    "bcrypt",
    "debugpy",
    "jedi",
    "parso",
    "ipython",
    "IPython",
    "traitlets",
    "prompt_toolkit",
    "pygments",
    "Pygments",
    "rich",
    "wcwidth",
    "pytest",
    "_pytest",
    "py",
    "pip",
    "wheel",
    "build",
    "tomlkit",
    "pycparser",
    "PyInstaller",
    "tkinter",
    "Tkinter",
    "setuptools",
    "pkg_resources",
    "distutils",
    "idlelib",
    "turtledemo",
    "lib2to3",
    "ensurepip",
    "pydoc",
    "pydoc_data",
    "doctest",
    "unittest",
    "test",
    "antigravity",
    "this",
    "xmlrpc",
    "telnetlib",
    "curses",
    "nuitka",
    "click",
    "click_option_group",
    "zstandard",
    "ordered_set",
    "requests",
    "requests_toolbelt",
    "types_requests",
    "urllib3",
    "charset_normalizer",
]
a = Analysis(
    [str(SPEC_DIR / "main.py")],
    pathex=[str(SPEC_DIR)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=2,
)

pyz = PYZ(a.pure)
upx_enabled = UPX_ENABLED
upx_exclude = ["python*.dll", "vcruntime*.dll"] if IS_WINDOWS else []

icon = _resolve_icon()
console_mode = CONSOLE

if IS_MACOS:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=APP_NAME,
        icon=icon,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=upx_enabled,
        upx_exclude=upx_exclude,
        runtime_tmpdir=None,
        console=console_mode,
        disable_windowed_traceback=False,
        argv_emulation=True,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        name=APP_NAME,
        strip=False,
        upx=upx_enabled,
        upx_exclude=upx_exclude,
    )
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=icon,
        bundle_identifier=BUNDLE_IDENTIFIER,
        info_plist={
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": APP_NAME,
            "CFBundleShortVersionString": "1.0.0",
            "NSHighResolutionCapable": True,
        },
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name=APP_NAME,
        icon=icon,
        debug=False,
        bootloader_ignore_signals=False,
        # Windows 的 PE 不支持 strip，过度的符号裁剪会破坏归档内 DLL 导致运行期加载失败，故仅 Linux 开启
        strip=not IS_WINDOWS,
        upx=upx_enabled,
        upx_exclude=upx_exclude,
        runtime_tmpdir=None,
        console=console_mode,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
