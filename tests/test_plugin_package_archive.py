from __future__ import annotations

import json
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from ECL.plugins.package_archive import (
    PluginPackageError,
    build_plugin_package,
    extract_plugin_package,
    inspect_plugin_package,
)


def _plugin_source(source_path: Path) -> None:
    source_path.mkdir()
    (source_path / "plugin.json").write_text(
        json.dumps({"name": "sample", "version": "1.0.0", "entry_point": "main:Plugin"}), encoding="utf-8"
    )
    (source_path / "main.py").write_text("class Plugin: pass\n", encoding="utf-8")


def _rewrite_archive(archive_path: Path, replacements: dict[str, bytes]) -> None:
    with zipfile.ZipFile(archive_path) as archive:
        original = [(entry.filename, archive.read(entry)) for entry in archive.infolist()]
    with zipfile.ZipFile(archive_path, "w") as archive:
        for name, content in original:
            archive.writestr(name, replacements.get(name, content))


def test_build_inspect_extract_roundtrip(tmp_path: Path) -> None:
    """
    相同源目录生成一致的归档，并只向新目录解包已校验文件。
    """
    source_path = tmp_path / "source"
    _plugin_source(source_path)
    first_path = tmp_path / "first.eclplugin"
    second_path = tmp_path / "second.eclplugin"
    first_info = build_plugin_package(source_path, first_path)
    second_info = build_plugin_package(source_path, second_path)

    assert first_path.read_bytes() == second_path.read_bytes()
    assert first_info == second_info == inspect_plugin_package(first_path)
    assert first_info.name == "sample"
    assert first_info.file_count == 2

    target_path = tmp_path / "installed"
    assert extract_plugin_package(first_path, target_path) == first_info
    assert (target_path / "main.py").read_bytes() == (source_path / "main.py").read_bytes()
    assert (target_path / "package-manifest.json").is_file()
    with pytest.raises(PluginPackageError, match="目标已存在"):
        extract_plugin_package(first_path, target_path)


def test_rejects_changed_file_without_updated_manifest(tmp_path: Path) -> None:
    """
    文件被修改而清单未变时，归档不能进入安装阶段。
    """
    source_path = tmp_path / "source"
    _plugin_source(source_path)
    archive_path = tmp_path / "sample.eclplugin"
    build_plugin_package(source_path, archive_path)
    _rewrite_archive(archive_path, {"main.py": b"class Plugin: fail\r\n"})

    with pytest.raises(PluginPackageError, match="哈希"):
        inspect_plugin_package(archive_path)


@pytest.mark.parametrize("unsafe_name", ["../outside.py", "/absolute.py", "C:/drive.py", "a/./b.py"])
def test_rejects_unsafe_archive_paths(tmp_path: Path, unsafe_name: str) -> None:
    """
    预检先拒绝路径穿越与跨平台歧义，不能落地文件。
    """
    archive_path = tmp_path / "sample.eclplugin"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(unsafe_name, "payload")
    with pytest.raises(PluginPackageError, match="路径"):
        extract_plugin_package(archive_path, tmp_path / "target")
    assert not (tmp_path / "target").exists()


def test_rejects_case_insensitive_duplicate_path(tmp_path: Path) -> None:
    """
    Windows 上会碰撞的文件名在任意平台都不能通过预检。
    """
    archive_path = tmp_path / "sample.eclplugin"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("main.py", "a")
        archive.writestr("MAIN.py", "b")
    with pytest.raises(PluginPackageError, match="重复"):
        inspect_plugin_package(archive_path)


def test_rejects_symlink_entry(tmp_path: Path) -> None:
    """
    ZIP 内符号链接不能绕过常规文件路径边界。
    """
    archive_path = tmp_path / "sample.eclplugin"
    symlink = zipfile.ZipInfo("linked.py")
    symlink.create_system = 3
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(symlink, "../outside.py")
    with pytest.raises(PluginPackageError, match="非普通文件"):
        inspect_plugin_package(archive_path)


def test_rejects_signature_metadata_in_v1(tmp_path: Path) -> None:
    """
    无验签能力的 v1 不能把签名声明误展示为已验证来源。
    """
    archive_path = tmp_path / "sample.eclplugin"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("package-signature.json", "{}")
    with pytest.raises(PluginPackageError, match="不支持"):
        inspect_plugin_package(archive_path)


def test_rejects_extreme_compression_ratio(tmp_path: Path) -> None:
    """
    高压缩比的归档应在解压前停止，避免消耗过量磁盘。
    """
    archive_path = tmp_path / "sample.eclplugin"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("large.txt", b"0" * 2_000_000)
    with pytest.raises(PluginPackageError, match="压缩比"):
        inspect_plugin_package(archive_path)


def test_rejects_output_inside_source(tmp_path: Path) -> None:
    """
    输出文件不得被再次递归收集进自身的插件源目录。
    """
    source_path = tmp_path / "source"
    _plugin_source(source_path)
    with pytest.raises(PluginPackageError, match="源目录内"):
        build_plugin_package(source_path, source_path / "sample.eclplugin")


def test_author_cli_builds_valid_archive(tmp_path: Path) -> None:
    """
    作者侧命令能直接生成可由安装器预检的归档。
    """
    source_path = tmp_path / "source"
    _plugin_source(source_path)
    archive_path = tmp_path / "sample.eclplugin"
    script_path = Path(__file__).resolve().parent.parent / "packaging" / "build-eclplugin.py"
    result = subprocess.run(
        [sys.executable, str(script_path), str(source_path), str(archive_path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert inspect_plugin_package(archive_path).name == "sample"
