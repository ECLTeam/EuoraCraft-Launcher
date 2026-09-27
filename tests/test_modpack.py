# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对整合包格式识别与解析模块的自动化测试。
#
# 公开接口：
#   - test_detect_pack_formats(tmp_path) -> None
#   - test_parse_modrinth_plan(tmp_path) -> None
#   - test_parse_modrinth_missing_minecraft_raises(tmp_path) -> None
#   - test_parse_modrinth_sanitizes_unsafe_paths(tmp_path) -> None
#   - test_parse_modrinth_unknown_dependency_warns(tmp_path) -> None
#   - test_parse_curseforge_plan(tmp_path) -> None
#   - test_parse_curseforge_missing_minecraft_raises(tmp_path) -> None
#   - test_parse_curseforge_uses_primary_loader(tmp_path) -> None
#   - test_parse_curseforge_unexpected_manifest_type_warns(tmp_path) -> None
#   - test_build_pack_plan_rejects_unknown(tmp_path) -> None
#   - test_build_pack_plan_rejects_ecl_legacy(tmp_path) -> None
#   - test_build_pack_plan_rejects_missing_directory(tmp_path) -> None
#   - test_import_modrinth_pack_downloads_and_assembles(tmp_path) -> None
#   - test_import_modrinth_pack_auto_installs_loader_base(tmp_path, monkeypatch) -> None
#   - test_import_pack_reports_missing_loader_versions(tmp_path, monkeypatch) -> None
#   - test_import_pack_hash_mismatch_rolls_back(tmp_path) -> None
#   - test_import_legacy_ecl_pack(tmp_path) -> None
#   - test_import_unknown_format_fails(tmp_path) -> None
#   - test_curseforge_fingerprint_matches_reference_vector(tmp_path) -> None
#   - test_curseforge_fingerprint_skips_whitespace_bytes(tmp_path) -> None
#   - test_curseforge_fingerprint_chunk_boundaries(tmp_path, monkeypatch) -> None
#   - test_export_pack_resolves_online_mods(tmp_path, monkeypatch) -> None
#   - test_export_pack_degrades_without_curseforge_key(tmp_path, monkeypatch) -> None
#   - test_modpack_online_install_rejects_invalid_source() -> None
#   - test_modpack_online_install_downloads_and_imports(tmp_path, monkeypatch) -> None
# ============================================================

import hashlib
import json
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from ECL.events import EventBus
from ECL.services.game import GameService
from ECL.services.game import modpack as modpack_module
from ECL.services.game.modpack import ModpackPlan, build_pack_plan, curseforge_fingerprint, detect_pack_format
from ECL.utils.errors import GameServiceError


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_detect_pack_formats(tmp_path) -> None:
    """各格式特征文件应返回对应标签，无特征时返回 unknown。"""
    markers = {
        "modrinth.index.json": "mrpack",
        "manifest.json": "curseforge",
        "mcbbs.packmeta": "mcbbs",
        "modpack.json": "hmcl",
        "mmc-pack.json": "multimc",
        "ecl-pack.json": "ecl-legacy",
    }
    for marker, expected in markers.items():
        root = tmp_path / expected
        root.mkdir()
        (root / marker).write_text("{}", encoding="utf-8")
        assert detect_pack_format(root) == expected

    empty = tmp_path / "empty"
    empty.mkdir()
    assert detect_pack_format(empty) == "unknown"


def test_parse_modrinth_plan(tmp_path) -> None:
    """mrpack 清单应完整映射依赖、加载器、哈希与客户端适用性声明。"""
    root = tmp_path / "pack"
    root.mkdir()
    (root / "overrides").mkdir()
    _write_json(
        root / "modrinth.index.json",
        {
            "formatVersion": 1,
            "game": "minecraft",
            "name": "My Pack",
            "summary": "A demo pack",
            "dependencies": {"minecraft": "1.21.1", "fabric-loader": "0.16.14"},
            "files": [
                {
                    "path": "mods/a.jar",
                    "hashes": {"sha1": "a" * 40, "sha512": "b" * 128},
                    "env": {"client": "required", "server": "required"},
                    "downloads": ["https://example.com/a.jar"],
                    "fileSize": 1024,
                },
                {
                    "path": "mods/b.jar",
                    "hashes": {"sha512": "c" * 128},
                    "env": {"client": "unsupported", "server": "required"},
                    "downloads": ["https://example.com/b.jar"],
                },
                {
                    "path": "mods/c.jar",
                    "downloads": ["https://example.com/c.jar"],
                },
            ],
        },
    )

    plan = build_pack_plan(root)

    assert isinstance(plan, ModpackPlan)
    assert plan.format_name == "mrpack"
    assert plan.pack_name == "My Pack"
    assert plan.summary == "A demo pack"
    assert plan.minecraft_version == "1.21.1"
    assert plan.loader_type == "fabric"
    assert plan.loader_version == "0.16.14"
    assert plan.overrides_dir == root / "overrides"
    assert [entry.target_relative for entry in plan.files] == ["mods/a.jar", "mods/b.jar", "mods/c.jar"]
    assert plan.files[0].sha1 == "a" * 40
    assert plan.files[0].sha512 == "b" * 128
    assert plan.files[0].size == 1024
    assert plan.files[0].env_client == "required"
    assert plan.files[1].env_client == "unsupported"
    # 未声明 env 的条目按规范默认 required
    assert plan.files[2].env_client == "required"
    assert plan.warnings == ()


def test_parse_modrinth_missing_minecraft_raises(tmp_path) -> None:
    """缺少 Minecraft 版本声明的 mrpack 应拒绝解析。"""
    root = tmp_path / "pack"
    root.mkdir()
    _write_json(root / "modrinth.index.json", {"dependencies": {"fabric-loader": "0.16.14"}})

    with pytest.raises(GameServiceError) as error:
        build_pack_plan(root)
    assert error.value.error_code == "INVALID_PACK_ARCHIVE"


def test_parse_modrinth_sanitizes_unsafe_paths(tmp_path) -> None:
    """绝对路径与目录穿越声明应被忽略并记录警告，反斜杠应归一为 POSIX 分隔。"""
    root = tmp_path / "pack"
    root.mkdir()
    _write_json(
        root / "modrinth.index.json",
        {
            "dependencies": {"minecraft": "1.21.1"},
            "files": [
                {"path": "../escape.jar", "downloads": ["https://example.com/x.jar"]},
                {"path": "C:\\abs\\path.jar", "downloads": ["https://example.com/x.jar"]},
                {"path": "config\\opts.txt", "downloads": ["https://example.com/x.jar"]},
            ],
        },
    )

    plan = build_pack_plan(root)

    assert [entry.target_relative for entry in plan.files] == ["config/opts.txt"]
    assert len(plan.warnings) == 2


def test_parse_modrinth_unknown_dependency_warns(tmp_path) -> None:
    """未收录的依赖键应忽略并记录警告，不得中断解析。"""
    root = tmp_path / "pack"
    root.mkdir()
    _write_json(
        root / "modrinth.index.json",
        {"dependencies": {"minecraft": "1.21.1", "mystery-loader": "1.0"}, "files": []},
    )

    plan = build_pack_plan(root)

    assert plan.loader_type == "vanilla"
    assert any("mystery-loader" in warning for warning in plan.warnings)


def test_parse_curseforge_plan(tmp_path) -> None:
    """CurseForge manifest 应映射加载器与 projectID/fileID 文件条目。"""
    root = tmp_path / "pack"
    root.mkdir()
    (root / "overrides").mkdir()
    _write_json(
        root / "manifest.json",
        {
            "manifestType": "minecraftModpack",
            "manifestVersion": 1,
            "name": "CF Pack",
            "version": "1.2.3",
            "overrides": "overrides",
            "minecraft": {
                "version": "1.20.1",
                "modLoaders": [{"id": "forge-47.3.0", "primary": True}],
            },
            "files": [
                {"projectID": 1, "fileID": 11, "required": True},
                {"projectID": 2, "fileID": 22, "required": False},
                {"projectID": "bad", "fileID": 33},
            ],
        },
    )

    plan = build_pack_plan(root)

    assert plan.format_name == "curseforge"
    assert plan.pack_name == "CF Pack"
    assert plan.minecraft_version == "1.20.1"
    assert plan.loader_type == "forge"
    assert plan.loader_version == "47.3.0"
    assert plan.overrides_dir == root / "overrides"
    assert len(plan.files) == 2
    assert plan.files[0].project_id == "1"
    assert plan.files[0].file_id == "11"
    assert plan.files[0].env_client == "required"
    assert plan.files[1].env_client == "optional"
    assert len(plan.warnings) == 1


def test_parse_curseforge_missing_minecraft_raises(tmp_path) -> None:
    """缺少 Minecraft 版本声明的 CurseForge 包应拒绝解析。"""
    root = tmp_path / "pack"
    root.mkdir()
    _write_json(root / "manifest.json", {"manifestType": "minecraftModpack", "minecraft": {}})

    with pytest.raises(GameServiceError) as error:
        build_pack_plan(root)
    assert error.value.error_code == "INVALID_PACK_ARCHIVE"


def test_parse_curseforge_uses_primary_loader(tmp_path) -> None:
    """仅 primary 的 modLoader 生效；无 modLoaders 时按 vanilla 处理。"""
    root_primary = tmp_path / "primary"
    root_primary.mkdir()
    _write_json(
        root_primary / "manifest.json",
        {
            "minecraft": {
                "version": "1.21.1",
                "modLoaders": [
                    {"id": "forge-1.0", "primary": False},
                    {"id": "fabric-0.16.0", "primary": True},
                ],
            },
        },
    )
    plan = build_pack_plan(root_primary)
    assert (plan.loader_type, plan.loader_version) == ("fabric", "0.16.0")

    root_vanilla = tmp_path / "vanilla"
    root_vanilla.mkdir()
    _write_json(root_vanilla / "manifest.json", {"minecraft": {"version": "1.21.1"}})
    plan = build_pack_plan(root_vanilla)
    assert (plan.loader_type, plan.loader_version) == ("vanilla", None)


def test_parse_curseforge_unexpected_manifest_type_warns(tmp_path) -> None:
    """manifestType 缺失或未知时按 CurseForge 兼容解析并记录警告。"""
    root = tmp_path / "pack"
    root.mkdir()
    _write_json(
        root / "manifest.json",
        {"manifestType": "somethingElse", "minecraft": {"version": "1.21.1"}},
    )

    plan = build_pack_plan(root)

    assert plan.format_name == "curseforge"
    assert any("somethingElse" in warning for warning in plan.warnings)


def test_build_pack_plan_rejects_unknown(tmp_path) -> None:
    """无特征文件的目录应报无法识别。"""
    root = tmp_path / "unknown"
    root.mkdir()
    with pytest.raises(GameServiceError) as error:
        build_pack_plan(root)
    assert error.value.error_code == "INVALID_PACK_ARCHIVE"


def test_build_pack_plan_rejects_ecl_legacy(tmp_path) -> None:
    """ECL 旧包由导入编排按旧流程处理，统一计划入口应明确拒绝。"""
    root = tmp_path / "legacy"
    root.mkdir()
    (root / "ecl-pack.json").write_text("{}", encoding="utf-8")
    with pytest.raises(GameServiceError) as error:
        build_pack_plan(root)
    assert error.value.error_code == "INVALID_PACK_ARCHIVE"


def test_build_pack_plan_rejects_missing_directory(tmp_path) -> None:
    """不存在的目录应拒绝解析。"""
    with pytest.raises(GameServiceError) as error:
        build_pack_plan(tmp_path / "missing")
    assert error.value.error_code == "INVALID_PACK_ARCHIVE"


# ---------- 导入编排（ModpackCoordinator） ----------


def _pack_downloader_factory(content_by_url: dict[str, bytes], corrupt_urls: set[str] | None = None):
    class _Downloader:
        def __init__(self, download_list, progress_callback=None, **_options):
            self.download_list = list(download_list)
            self.progress_callback = progress_callback
            self.completed_entries: set[tuple[str, str]] = set()
            self.failed_entries: set[tuple[str, str]] = set()
            self.local_failed_paths: set[str] = set()
            self.total_bytes = 0
            self.downloaded_bytes = 0
            self.use_byte_progress = False
            self.total_files = len(download_list)

        async def run(self):
            for url, path in self.download_list:
                target = Path(path)
                target.parent.mkdir(parents=True, exist_ok=True)
                content = b"corrupted" if corrupt_urls and url in corrupt_urls else content_by_url.get(url, b"payload")
                target.write_bytes(content)
                self.completed_entries.add((url, str(target)))
            if self.progress_callback:
                self.progress_callback(len(self.download_list), len(self.download_list))

        def stop(self):
            return None

    return _Downloader


def _build_pack_service(downloader_factory) -> GameService:
    return GameService(
        SimpleNamespace(current_account=lambda: None),
        search_factory=lambda _path: SimpleNamespace(search_minecraft=lambda: {}),
        downloader_factory=downloader_factory,
        event_bus=EventBus(),
        enable_version_watcher=False,
    )


def _await_operation(service: GameService, submitted: dict) -> dict:
    operation_id = submitted["operationId"]
    deadline = time.time() + 10
    while time.time() < deadline:
        state = service._game_operations.get(operation_id)
        if state["status"] in {"completed", "failed", "cancelled"}:
            return state
        time.sleep(0.02)
    raise AssertionError("长任务执行超时")


def _run_pack_operation(service: GameService, **kwargs) -> dict:
    return _await_operation(service, service.import_instance_pack(**kwargs))


def _make_pack_zip(path: Path, files: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)


def _make_base_version(game_path: Path, version: str) -> None:
    base = game_path / "versions" / version
    base.mkdir(parents=True, exist_ok=True)
    (base / f"{version}.json").write_text("{}", encoding="utf-8")


def test_import_modrinth_pack_downloads_and_assembles(tmp_path) -> None:
    """mrpack 导入应下载清单文件、跳过客户端不适用条目并装配继承实例。"""
    game_path = tmp_path / ".minecraft"
    _make_base_version(game_path, "1.21.1")
    mod_bytes = b"mod-a-content"
    url = "https://example.com/a.jar"
    index = {
        "formatVersion": 1,
        "game": "minecraft",
        "name": "Demo",
        "dependencies": {"minecraft": "1.21.1"},
        "files": [
            {
                "path": "mods/a.jar",
                "hashes": {"sha1": hashlib.sha1(mod_bytes).hexdigest()},
                "downloads": [url],
                "fileSize": len(mod_bytes),
            },
            {
                "path": "mods/server-only.jar",
                "downloads": ["https://example.com/s.jar"],
                "env": {"client": "unsupported", "server": "required"},
            },
        ],
    }
    pack = tmp_path / "demo.mrpack"
    _make_pack_zip(
        pack,
        {
            "modrinth.index.json": json.dumps(index).encode(),
            "overrides/config/opts.txt": b"fov=1\n",
        },
    )
    service = _build_pack_service(_pack_downloader_factory({url: mod_bytes}))

    state = _run_pack_operation(service, game_path=game_path, source_path=pack, new_version_id="Demo Pack")

    assert state["status"] == "completed", state
    result = state["result"]
    assert result["format"] == "mrpack"
    assert result["downloadedFiles"] == 1
    assert result["skippedFiles"] == 1
    assert result["baseVersion"] == "1.21.1"
    instance = game_path / "versions" / "Demo Pack"
    assert (instance / "mods" / "a.jar").read_bytes() == mod_bytes
    assert (instance / "config" / "opts.txt").read_bytes() == b"fov=1\n"
    version_json = json.loads((instance / "Demo Pack.json").read_text(encoding="utf-8"))
    assert version_json == {"id": "Demo Pack", "inheritsFrom": "1.21.1"}
    assert not (game_path / "versions" / ".Demo Pack.ecl-import").exists()


def test_import_modrinth_pack_auto_installs_loader_base(tmp_path, monkeypatch) -> None:
    """加载器整合包缺少基础版本时应自动安装并继承加载器实例名。"""
    game_path = tmp_path / ".minecraft"
    (game_path / "versions").mkdir(parents=True)
    created = []

    class FakeGames:
        def build_fabric_download_list(self, version_id, loader_version, save_name, fabric_api):
            created.append((save_name, loader_version, fabric_api))
            directory = game_path / "versions" / save_name
            directory.mkdir(parents=True, exist_ok=True)
            (directory / f"{save_name}.json").write_text("{}", encoding="utf-8")
            return []

    service = _build_pack_service(_pack_downloader_factory({}))
    monkeypatch.setattr(service, "_context", lambda *_args: SimpleNamespace(games=FakeGames()))
    index = {
        "name": "Fabric Demo",
        "dependencies": {"minecraft": "1.21.1", "fabric-loader": "0.16.14"},
        "files": [],
    }
    pack = tmp_path / "demo.mrpack"
    _make_pack_zip(
        pack,
        {
            "modrinth.index.json": json.dumps(index).encode(),
            "overrides/mods/keep.txt": b"x",
        },
    )

    state = _run_pack_operation(service, game_path=game_path, source_path=pack, new_version_id="FabricPack")

    assert state["status"] == "completed", state
    # 整合包基础安装不附带 Fabric API（fabric_api 为 None），避免与包内模组重复
    assert created == [("fabric-0.16.14-1.21.1", "0.16.14", None)]
    result = state["result"]
    assert result["baseVersion"] == "fabric-0.16.14-1.21.1"
    version_json = json.loads((game_path / "versions" / "FabricPack" / "FabricPack.json").read_text(encoding="utf-8"))
    assert version_json["inheritsFrom"] == "fabric-0.16.14-1.21.1"


def test_import_pack_reports_missing_loader_versions(tmp_path, monkeypatch) -> None:
    """加载器版本无法解析时应报 PACK_LOADER_VERSION_REQUIRED。"""
    service = _build_pack_service(_pack_downloader_factory({}))
    monkeypatch.setattr(service, "loader_versions", lambda *_args: [])
    index = {"dependencies": {"minecraft": "1.21.1", "quilt-loader": ""}, "files": []}
    pack = tmp_path / "demo.mrpack"
    _make_pack_zip(pack, {"modrinth.index.json": json.dumps(index).encode()})

    state = _run_pack_operation(service, game_path=tmp_path / ".minecraft", source_path=pack, new_version_id="Q")

    assert state["status"] == "failed"
    assert state["errorCode"] == "PACK_LOADER_VERSION_REQUIRED"
    assert not (tmp_path / ".minecraft" / "versions" / "Q").exists()


def test_import_pack_hash_mismatch_rolls_back(tmp_path) -> None:
    """哈希校验失败（含重试）应报错并清理 staging，不残留半成品实例。"""
    game_path = tmp_path / ".minecraft"
    _make_base_version(game_path, "1.21.1")
    url = "https://example.com/a.jar"
    index = {
        "dependencies": {"minecraft": "1.21.1"},
        "files": [
            {"path": "mods/a.jar", "hashes": {"sha512": "f" * 128}, "downloads": [url]},
        ],
    }
    pack = tmp_path / "demo.mrpack"
    _make_pack_zip(pack, {"modrinth.index.json": json.dumps(index).encode()})
    service = _build_pack_service(_pack_downloader_factory({}, corrupt_urls={url}))

    state = _run_pack_operation(service, game_path=game_path, source_path=pack, new_version_id="BadPack")

    assert state["status"] == "failed"
    assert state["errorCode"] == "PACK_FILE_HASH_MISMATCH"
    assert not (game_path / "versions" / "BadPack").exists()
    assert not (game_path / "versions" / ".BadPack.ecl-import").exists()


def test_import_legacy_ecl_pack(tmp_path) -> None:
    """ECL 旧格式包应按整体实例导入并重命名主版本描述。"""
    pack = tmp_path / "legacy.zip"
    _make_pack_zip(
        pack,
        {
            "ecl-pack.json": b"{}",
            "old-version.json": b"{}",
            "mods/legacy.jar": b"legacy",
        },
    )
    service = _build_pack_service(_pack_downloader_factory({}))

    state = _run_pack_operation(service, game_path=tmp_path / ".minecraft", source_path=pack, new_version_id="Legacy")

    assert state["status"] == "completed", state
    instance = tmp_path / ".minecraft" / "versions" / "Legacy"
    assert (instance / "mods" / "legacy.jar").read_bytes() == b"legacy"
    assert (instance / "Legacy.json").is_file()
    assert state["result"]["format"] == "ecl-legacy"


def test_import_unknown_format_fails(tmp_path) -> None:
    """无法识别的压缩包应报 INVALID_PACK_ARCHIVE。"""
    pack = tmp_path / "mystery.zip"
    _make_pack_zip(pack, {"readme.txt": b"not a pack"})
    service = _build_pack_service(_pack_downloader_factory({}))

    state = _run_pack_operation(service, game_path=tmp_path / ".minecraft", source_path=pack, new_version_id="X")

    assert state["status"] == "failed"
    assert state["errorCode"] == "INVALID_PACK_ARCHIVE"


# ---------- 在线整合包安装 ----------


def test_modpack_online_install_rejects_invalid_source() -> None:
    """不支持的在线来源应同步拒绝。"""
    service = _build_pack_service(_pack_downloader_factory({}))
    with pytest.raises(GameServiceError) as error:
        service.install_modpack_online("ftb", "1", "2", "game", "Y")
    assert error.value.error_code == "INVALID_RESOURCE_SOURCE"


def test_modpack_online_install_downloads_and_imports(tmp_path, monkeypatch) -> None:
    """在线安装应下载包文件后走统一导入编排，装配继承实例。"""
    game_path = tmp_path / ".minecraft"
    _make_base_version(game_path, "1.21.1")
    mod_bytes = b"online-mod"
    index = {
        "name": "Online Demo",
        "dependencies": {"minecraft": "1.21.1"},
        "files": [
            {
                "path": "mods/online.jar",
                "hashes": {"sha1": hashlib.sha1(mod_bytes).hexdigest()},
                "downloads": ["https://example.com/online.jar"],
            }
        ],
    }
    pack_zip = tmp_path / "demo.mrpack"
    _make_pack_zip(pack_zip, {"modrinth.index.json": json.dumps(index).encode(), "overrides/config/a.txt": b"1"})
    pack_bytes = pack_zip.read_bytes()

    service = _build_pack_service(_pack_downloader_factory({"https://example.com/online.jar": mod_bytes}))
    monkeypatch.setattr(
        service,
        "_select_online_file",
        lambda *_args: {"filename": "demo.mrpack", "url": "https://example.com/pack.mrpack"},
    )

    captured = {}

    def fake_download(url, temp, filename, task_id=None):
        captured["url"] = url
        Path(temp).write_bytes(pack_bytes)

    monkeypatch.setattr(service, "_download_online_file", fake_download)

    state = _await_operation(
        service,
        service.install_modpack_online("modrinth", "proj-1", "ver-1", game_path, "Online"),
    )

    assert state["status"] == "completed", state
    assert captured["url"] == "https://example.com/pack.mrpack"
    result = state["result"]
    assert result["format"] == "mrpack"
    assert result["baseVersion"] == "1.21.1"
    instance = game_path / "versions" / "Online"
    assert (instance / "mods" / "online.jar").read_bytes() == mod_bytes
    assert (instance / "config" / "a.txt").read_bytes() == b"1"


# ---------- CurseForge 指纹与导出反查 ----------


def test_curseforge_fingerprint_matches_reference_vector(tmp_path) -> None:
    """无空白字节的输入应与参考实现（seed=1 MurmurHash2）结果一致。"""
    data = b"aklerfdhvkore;fhjbgoiwrfgbuio34htgb889rguiyufgvueirefvrvu9vhgg9wygf94u8fgw249fyhuwygf293ghf8h"
    target = tmp_path / "sample.bin"
    target.write_bytes(data)
    assert curseforge_fingerprint(target) == 2672531333


def test_curseforge_fingerprint_skips_whitespace_bytes(tmp_path) -> None:
    """空白字节应被归一化过滤，过滤后与紧凑内容的指纹一致。"""
    compact = tmp_path / "compact.bin"
    compact.write_bytes(b"abcdefgh" * 100)
    spaced = tmp_path / "spaced.bin"
    spaced.write_bytes(b"ab cd\tef\rgh\n" * 100)
    assert curseforge_fingerprint(spaced) == curseforge_fingerprint(compact)


def test_curseforge_fingerprint_chunk_boundaries(tmp_path, monkeypatch) -> None:
    """缩小读取块尺寸强制跨块携带字节，结果必须与整块计算一致。"""
    data = bytes(range(256)) * 40
    small = tmp_path / "small.bin"
    small.write_bytes(data)
    monkeypatch.setattr(modpack_module, "_fingerprint_chunk_bytes", 7)
    small_hash = curseforge_fingerprint(small)
    monkeypatch.setattr(modpack_module, "_fingerprint_chunk_bytes", 1024 * 1024)
    large = tmp_path / "large.bin"
    large.write_bytes(data)
    assert small_hash == curseforge_fingerprint(large)


def _fake_online_post(modrinth_versions: dict, curseforge_matches: list | None = None):
    def fake_post(url: str, **_kwargs):
        payload = modrinth_versions if "modrinth.com" in url else {"data": {"exactMatches": curseforge_matches or []}}
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)

    return fake_post


def _make_instance_with_mods(game_path: Path) -> tuple[Path, bytes, bytes]:
    instance = game_path / "versions" / "Exported 1.21.1"
    mods = instance / "mods"
    mods.mkdir(parents=True, exist_ok=True)
    (instance / "config").mkdir()
    (instance / "config" / "opts.txt").write_bytes(b"fov=1\n")
    (instance / "saves").mkdir()
    (instance / "saves" / "world.dat").write_bytes(b"world")
    (instance / "servers.dat").write_bytes(b"servers")
    mod_a = b"mod-a-online"
    mod_b = b"mod-b-local"
    (mods / "a.jar").write_bytes(mod_a)
    (mods / "b.jar").write_bytes(mod_b)
    return instance, mod_a, mod_b


def test_export_pack_resolves_online_mods(tmp_path, monkeypatch) -> None:
    """导出应把 Modrinth/CF 命中的模组写入 files[] 并从 overrides 剔除。"""
    monkeypatch.delenv("CURSEFORGE_API_KEY", raising=False)
    game_path = tmp_path / ".minecraft"
    instance, mod_a, _mod_b = _make_instance_with_mods(game_path)
    service = _build_pack_service(_pack_downloader_factory({}))
    service._curseforge_api_key = "test-key"

    modrinth_versions = {
        hashlib.sha1(mod_a).hexdigest(): {
            "files": [{"primary": True, "url": "https://dl.example.com/a.jar", "size": len(mod_a)}]
        }
    }

    def fake_post(url: str, **kwargs):
        if "modrinth.com" in url:
            payload = modrinth_versions
        else:
            fingerprints = json.loads(json.dumps(kwargs["json"]))["fingerprints"]
            b_fingerprint = curseforge_fingerprint(instance / "mods" / "b.jar")
            assert b_fingerprint in fingerprints
            payload = {
                "data": {
                    "exactMatches": [
                        {
                            "file": {
                                "downloadUrl": "https://cf.example.com/b.jar",
                                "fileFingerprint": b_fingerprint,
                            }
                        }
                    ]
                }
            }
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)

    monkeypatch.setattr(modpack_module, "_proxied_post", fake_post)
    output = tmp_path / "out.mrpack"

    state = _await_operation(service, service.export_instance_pack(game_path, "Exported 1.21.1", output, "modrinth"))

    assert state["status"] == "completed", state
    result = state["result"]
    assert result["format"] == "modrinth"
    assert result["onlineFiles"] == 2
    assert result["warnings"] == []
    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        index = json.loads(archive.read("modrinth.index.json"))
    assert "overrides/mods/a.jar" not in names
    assert "overrides/mods/b.jar" not in names
    assert "overrides/config/opts.txt" in names
    assert "overrides/saves/world.dat" not in names
    assert "overrides/servers.dat" not in names
    assert index["dependencies"] == {"minecraft": "1.21.1"}
    assert len(index["files"]) == 2
    entry_a = next(item for item in index["files"] if item["path"] == "mods/a.jar")
    assert entry_a["downloads"] == ["https://dl.example.com/a.jar"]
    assert entry_a["hashes"]["sha1"] == hashlib.sha1(mod_a).hexdigest()
    assert entry_a["fileSize"] == len(mod_a)
    entry_b = next(item for item in index["files"] if item["path"] == "mods/b.jar")
    assert entry_b["downloads"] == ["https://cf.example.com/b.jar"]


def test_export_pack_degrades_without_curseforge_key(tmp_path, monkeypatch) -> None:
    """未配置 CurseForge Key 时导出应降级为 overrides 打包并记录警告。"""
    monkeypatch.delenv("CURSEFORGE_API_KEY", raising=False)
    game_path = tmp_path / ".minecraft"
    _instance, mod_a, _mod_b = _make_instance_with_mods(game_path)
    service = _build_pack_service(_pack_downloader_factory({}))
    service._curseforge_api_key = None

    modrinth_versions = {
        hashlib.sha1(mod_a).hexdigest(): {
            "files": [{"primary": True, "url": "https://dl.example.com/a.jar", "size": len(mod_a)}]
        }
    }
    monkeypatch.setattr(modpack_module, "_proxied_post", _fake_online_post(modrinth_versions))
    output = tmp_path / "out.mrpack"

    state = _await_operation(service, service.export_instance_pack(game_path, "Exported 1.21.1", output, "modrinth"))

    assert state["status"] == "completed", state
    result = state["result"]
    assert result["onlineFiles"] == 1
    assert any("CurseForge" in warning for warning in result["warnings"])
    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        index = json.loads(archive.read("modrinth.index.json"))
    assert "overrides/mods/b.jar" in names
    assert [item["path"] for item in index["files"]] == ["mods/a.jar"]
