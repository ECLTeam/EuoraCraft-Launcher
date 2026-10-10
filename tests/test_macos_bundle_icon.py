# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 macOS 应用包图标与 Dock 激活策略的自动化测试。
#
# 公开接口：
#   - test_committed_icns_covers_required_sizes_from_logo_ico() -> None
#   - test_macos_bundle_uses_project_icon_and_foreground_activation(monkeypatch, tmp_path) -> None
#   - test_macos_build_fails_when_project_icon_is_missing(monkeypatch, tmp_path) -> None
#   - test_windows_build_keeps_windows_icon(monkeypatch, tmp_path) -> None
# ============================================================

from __future__ import annotations

import io
import shutil
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

import pytest
from PIL import Image, ImageChops
from PIL.IcoImagePlugin import IcoImageFile

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_FILE = REPO_ROOT / "EuoraCraft-Launcher.spec"
LOGO_ICO = REPO_ROOT / "resources" / "img" / "logo.ico"
LOGO_ICNS = REPO_ROOT / "resources" / "img" / "logo.icns"


@dataclass
class _BuildCall:
    """记录一次 PyInstaller 构建对象的构造参数。"""

    name: str
    arguments: dict[str, object] = field(default_factory=dict)


class _RecordedBuildObject:
    """
    代替 PyInstaller 的构建对象，捕获构造参数而不真正打包。

    spec 在构造顺序上依赖 Analysis 的分析结果、PYZ 的返回值以及前一个构建对象，
    因此这里保留同名占位属性，让 spec 的控制流与真实构建保持一致。
    """

    def __init__(self, calls: list[_BuildCall], name: str) -> None:
        self._calls = calls
        self._name = name
        self.pure: object = None
        self.scripts: list[object] = []
        self.binaries: list[object] = []
        self.datas: list[object] = []

    def __call__(self, *args: object, **kwargs: object) -> _RecordedBuildObject:
        self._calls.append(_BuildCall(self._name, dict(kwargs)))
        return self


def _build_call(calls: list[_BuildCall], name: str) -> _BuildCall:
    """
    按名称取出唯一一次构建调用。

    :param calls: 已记录的构建调用
    :param name: 构建对象名称
    :return: 对应的构建调用
    :raises AssertionError: 该构建对象未被调用或被调用多次时抛出
    """
    matched = [call for call in calls if call.name == name]
    assert len(matched) == 1, f"期望恰好一次 {name} 调用，实际 {len(matched)} 次"
    return matched[0]


def _stub_pyinstaller_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    用最小实现替换 PyInstaller 与宿主依赖清单模块。

    spec 只使用 collect_all、collect_submodules、copy_metadata 三个打包钩子以及宿主
    依赖清单生成函数；这些实现依赖本机安装环境，替换后测试只覆盖打包决策本身。

    :param monkeypatch: pytest 夹具提供的补丁器
    """
    hooks = ModuleType("PyInstaller.utils.hooks")
    hooks.collect_all = lambda package: ([], [], [])  # type: ignore[attr-defined]
    hooks.collect_submodules = lambda package: []  # type: ignore[attr-defined]
    hooks.copy_metadata = lambda name: []  # type: ignore[attr-defined]
    utils = ModuleType("PyInstaller.utils")
    utils.hooks = hooks  # type: ignore[attr-defined]
    pyinstaller = ModuleType("PyInstaller")
    pyinstaller.utils = utils  # type: ignore[attr-defined]
    host_dependencies = ModuleType("ECL.plugins.host_dependencies")
    host_dependencies.build_host_dependency_manifest = lambda output_path: {}  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "PyInstaller", pyinstaller)
    monkeypatch.setitem(sys.modules, "PyInstaller.utils", utils)
    monkeypatch.setitem(sys.modules, "PyInstaller.utils.hooks", hooks)
    monkeypatch.setitem(sys.modules, "ECL.plugins.host_dependencies", host_dependencies)


def _prepare_project(root: Path, *, include_icns: bool) -> Path:
    """
    在临时目录搭建 spec 运行所需的最小项目骨架。

    只补齐 ``_ensure_datasets_exist()`` 校验的文件与两份图标，不复制真实前端产物。

    :param root: 临时项目根目录
    :param include_icns: 是否放入 macOS 图标
    :return: 与 ``root`` 相同的项目根目录
    """
    (root / "frontend" / "dist").mkdir(parents=True)
    (root / "frontend" / "dist" / "index.html").write_text("<!doctype html>", encoding="utf-8")
    (root / "capabilities").mkdir()
    (root / "capabilities" / "default.json").write_text("{}", encoding="utf-8")
    (root / "resources" / "img").mkdir(parents=True)
    shutil.copy(LOGO_ICO, root / "resources" / "img" / "logo.ico")
    if include_icns:
        shutil.copy(LOGO_ICNS, root / "resources" / "img" / "logo.icns")
    (root / "Tauri.toml").write_text('productName = "EuoraCraft Launcher"\n', encoding="utf-8")
    (root / "main.py").write_text("", encoding="utf-8")
    return root


def _run_spec(monkeypatch: pytest.MonkeyPatch, root: Path, *, platform_name: str) -> list[_BuildCall]:
    """
    在指定平台上执行真实 spec，返回记录到的构建调用。

    spec 由 PyInstaller 以模块级全局变量（SPECPATH、Analysis、EXE 等）驱动，这里
    提供等价替身，使断言可以覆盖真实打包决策而不是复制其中的判断。

    :param monkeypatch: pytest 夹具提供的补丁器
    :param root: 临时项目根目录
    :param platform_name: 目标平台标识，例如 ``darwin`` 或 ``win32``
    :return: 按执行顺序排列的构建调用
    """
    _stub_pyinstaller_modules(monkeypatch)
    monkeypatch.setattr(sys, "platform", platform_name)
    calls: list[_BuildCall] = []
    namespace: dict[str, object] = {
        "SPECPATH": str(root),
        "DISTPATH": str(root / "dist"),
        "HOMEPATH": str(root),
        "WARNFILE": str(root / "build" / "warn.txt"),
        "workpath": str(root / "build"),
        "Analysis": _RecordedBuildObject(calls, "Analysis"),
        "BUNDLE": _RecordedBuildObject(calls, "BUNDLE"),
        "COLLECT": _RecordedBuildObject(calls, "COLLECT"),
        "EXE": _RecordedBuildObject(calls, "EXE"),
        "PYZ": _RecordedBuildObject(calls, "PYZ"),
    }
    exec(compile(SPEC_FILE.read_text(encoding="utf-8"), str(SPEC_FILE), "exec"), namespace)
    return calls


def _read_icns_entries(icns_path: Path) -> list[tuple[bytes, bytes]]:
    """
    解析 icns 容器，逐条返回条目类型与负载。

    :param icns_path: icns 文件路径
    :return: 按文件顺序排列的「条目类型 + 负载」列表
    :raises AssertionError: 文件头、总长度或图标条目位置无效时抛出
    """
    data = icns_path.read_bytes()
    magic, total_length = struct.unpack_from(">4sI", data, 0)
    assert magic == b"icns"
    assert total_length == len(data)
    entries: list[tuple[bytes, bytes]] = []
    offset = 8
    while offset < total_length:
        entry_type, entry_length = struct.unpack_from(">4sI", data, offset)
        assert entry_length > 8
        entries.append((entry_type, data[offset + 8 : offset + entry_length]))
        offset += entry_length
    assert offset == total_length
    return entries


def _load_ico_master() -> Image.Image:
    """
    读取 Windows 图标中像素最大的图层，作为 icns 的比对母版。

    :return: 已转换为 RGBA 的母版图像
    """
    with Image.open(LOGO_ICO) as ico_image:
        if not isinstance(ico_image, IcoImageFile):
            raise AssertionError(f"图标源文件不是 Windows 图标：{LOGO_ICO}")
        sizes = sorted(ico_image.ico.sizes())
        return ico_image.ico.getimage(sizes[-1]).convert("RGBA")


def test_committed_icns_covers_required_sizes_from_logo_ico() -> None:
    """提交的 macOS 图标覆盖系统需要的全部尺寸，且内容与 Windows 图标母版一致。"""
    expected_sizes = {b"icp4": 16, b"icp5": 32, b"ic11": 32, b"ic12": 64, b"ic07": 128, b"ic13": 256, b"ic08": 256}
    entries = dict(_read_icns_entries(LOGO_ICNS))
    master = _load_ico_master()

    assert sorted(entries) == sorted(expected_sizes)
    for entry_type, size in expected_sizes.items():
        with Image.open(io.BytesIO(entries[entry_type])) as entry_image:
            entry = entry_image.convert("RGBA")
        assert entry.size == (size, size)
        if size == master.size[0]:
            # 与母版同尺寸的条目必须是原样拷贝，换标后忘记重新生成会在这里暴露。
            assert ImageChops.difference(master, entry).getbbox() is None
            continue
        # 缩放条目允许 Pillow 版本间的重采样差异，超出容差说明图标与母版已经脱节。
        expected = master.resize((size, size), Image.Resampling.LANCZOS)
        difference = ImageChops.difference(expected, entry)
        # 差值直方图首个桶对应差值为 0，其余桶的最后一个下标即该通道的最大差值。
        max_difference = max(max(channel.histogram()[1:]) for channel in difference.split())
        assert max_difference <= 12, f"{entry_type!r} 与母版差异过大，logo.ico 变更后需重新生成 logo.icns"


def test_macos_bundle_uses_project_icon_and_foreground_activation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """macOS 构建使用项目图标，并显式关闭后台应用模式以保留 Dock 与菜单栏。"""
    root = _prepare_project(tmp_path / "project", include_icns=True)
    calls = _run_spec(monkeypatch, root, platform_name="darwin")

    bundle = _build_call(calls, "BUNDLE")
    executable = _build_call(calls, "EXE")
    expected_icon = root / "resources" / "img" / "logo.icns"
    assert Path(str(bundle.arguments["icon"])) == expected_icon
    assert Path(str(executable.arguments["icon"])) == expected_icon
    info_plist = bundle.arguments["info_plist"]
    assert isinstance(info_plist, dict)
    assert info_plist["LSBackgroundOnly"] is False


def test_macos_build_fails_when_project_icon_is_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """macOS 构建缺少项目图标时立即失败，不允许回退到 PyInstaller 默认图标。"""
    root = _prepare_project(tmp_path / "project", include_icns=False)

    with pytest.raises(SystemExit) as failure:
        _run_spec(monkeypatch, root, platform_name="darwin")

    assert "build-macos-icon.py" in str(failure.value)


def test_windows_build_keeps_windows_icon(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Windows 构建仍使用 logo.ico，不受 macOS 图标改动影响。"""
    root = _prepare_project(tmp_path / "project", include_icns=True)
    calls = _run_spec(monkeypatch, root, platform_name="win32")

    assert [call.name for call in calls] == ["Analysis", "PYZ", "EXE"]
    executable = _build_call(calls, "EXE")
    assert Path(str(executable.arguments["icon"])) == root / "resources" / "img" / "logo.ico"
