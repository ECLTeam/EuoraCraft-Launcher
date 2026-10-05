from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from threading import Event
from time import monotonic

import httpx
import pytest

from ECL.events import EventBus
from ECL.game import JavaRuntime, JavaScanner
from ECL.services.java.archives import JavaArchive
from ECL.services.java.manager import JavaManager
from ECL.services.java.models import JavaError, JavaPolicy, JavaReferenceConfig
from ECL.services.operations import OperationContext, OperationManager


def _java_path(home: Path, version="21.0.2") -> Path:
    (home / "bin").mkdir(parents=True)
    executable = home / "bin" / ("java.exe" if sys.platform == "win32" else "java")
    executable.write_bytes(b"test Java")
    (home / "release").write_text(version, encoding="utf-8")
    return executable


@pytest.fixture
def manager(tmp_path, monkeypatch):
    def probe(path):
        release = Path(path).parent.parent / "release"
        if not Path(path).is_file() or not release.is_file():
            return None
        return JavaRuntime(
            Path(path).resolve(), release.read_text(encoding="utf-8"), "Eclipse Adoptium", "amd64", False
        )

    monkeypatch.setattr(JavaScanner, "probe", probe)
    monkeypatch.setattr(JavaScanner, "scan", lambda self: [])
    events = EventBus()
    operations = OperationManager(tmp_path, events)
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))
    service = JavaManager(tmp_path, client, operations, events)
    yield service
    service.close()
    operations.close()
    client.close()


def _context():
    return OperationContext("test-operation", EventBus(), Event())


def _archive_bytes():
    content = io.BytesIO()
    executable = "java.exe" if sys.platform == "win32" else "java"
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr(f"runtime/bin/{executable}", b"test Java")
        archive.writestr("runtime/release", "21.0.2")
    return content.getvalue()


def _plan(manager, archive_bytes):
    from ECL.services.java.models import JavaPackage

    system, architecture = JavaPolicy.host()
    checksum = hashlib.sha256(archive_bytes).hexdigest()
    package = JavaPackage(
        package_id="a" * 24,
        release_name="jdk-21.0.2+1",
        major_version=21,
        runtime_kind="JRE",
        platform=system,
        architecture=architecture,
        filename="java.zip",
        download_url="https://github.com/adoptium/test/releases/java.zip",
        download_bytes=len(archive_bytes),
        checksum=checksum,
    )
    manager.catalog._cache[(21, "JRE", system, architecture)] = (monotonic(), (package,))
    manager.catalog._package_by_id[package.package_id] = package
    return manager.install_plan(package.package_id)


def test_register_persists_disabled_and_missing_entries_and_preserves_external_files(manager, tmp_path):
    executable = _java_path(tmp_path / "external")
    record = manager.register(executable)
    manager.set_enabled(record.runtime_id, False)
    assert not manager.inventory().runtimes[0].is_enabled
    executable.unlink()
    inventory = manager.inventory()
    assert inventory.runtimes[0].validation_status == "missing"
    assert manager.registry.read().runtimes[0].origin == "manual"
    manager.forget(record.runtime_id)
    assert not manager.inventory().runtimes
    assert (tmp_path / "external" / "release").is_file()


def test_stale_scan_does_not_register_a_removed_executable(manager, tmp_path, monkeypatch):
    executable = _java_path(tmp_path / "removed")
    scanned_runtime = JavaScanner.probe(executable)
    monkeypatch.setattr(JavaScanner, "scan", lambda self: [scanned_runtime])
    executable.unlink()
    assert not manager.inventory(force=True).runtimes
    assert not manager.registry.read().runtimes


def test_selection_prefers_new_patch_and_disabled_manual_selection_is_rejected(manager, tmp_path):
    old = manager.register(_java_path(tmp_path / "a-old", "21.0.1"))
    newer = manager.register(_java_path(tmp_path / "z-new", "21.0.12"))
    manager.register(_java_path(tmp_path / "java25", "25.0.1"))
    assert manager.select(21) == newer.executable_path
    manager.set_enabled(newer.runtime_id, False)
    assert manager.select(21) == old.executable_path
    with pytest.raises(JavaError, match="停用"):
        manager.validate_selection(newer.runtime_id, 21)


def test_global_and_instance_bindings_block_forget(manager, tmp_path):
    record = manager.register(_java_path(tmp_path / "external"))
    manager._config_provider = lambda: JavaReferenceConfig(java_auto=False, java_path=str(record.executable_path))
    with pytest.raises(JavaError, match="设置使用"):
        manager.forget(record.runtime_id)
    game_root = tmp_path / "minecraft"
    settings = game_root / "versions" / "instance" / ".ecl" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"customJava": True, "javaPath": str(record.executable_path)}), encoding="utf-8")
    manager._config_provider = lambda: JavaReferenceConfig(minecraft_paths=[{"path": str(game_root)}])
    with pytest.raises(JavaError, match="设置使用"):
        manager.forget(record.runtime_id)
    settings.write_text("invalid", encoding="utf-8")
    with pytest.raises(JavaError, match="无法确认"):
        manager.forget(record.runtime_id)


def test_actual_game_lease_survives_manager_close_until_child_exits(manager, tmp_path):
    record = manager.register(_java_path(tmp_path / "external"))
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}),
    )
    try:
        lease = manager.acquire(record.executable_path)
        lease.attach_process(process.pid)
        manager.close()
        with manager.registry.locked():
            assert manager.lifecycle.is_in_use(record.runtime_id)
        with pytest.raises(JavaError, match="游戏使用"):
            manager._check_removable(record)
    finally:
        process.terminate()
        process.wait(timeout=5)
    with manager.registry.locked():
        assert not manager.lifecycle.is_in_use(record.runtime_id)


@pytest.mark.parametrize("format_name", ["zip", "tar"])
@pytest.mark.parametrize("entry_name", ["../outside", "/absolute", "C:/outside", "safe/../../outside"])
def test_archive_rejects_traversal_before_writing(tmp_path, format_name, entry_name):
    archive_path = tmp_path / ("bad.zip" if format_name == "zip" else "bad.tar.gz")
    if format_name == "zip":
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr(entry_name, b"bad")
    else:
        with tarfile.open(archive_path, "w:gz") as archive:
            member = tarfile.TarInfo(entry_name)
            member.size = 3
            archive.addfile(member, io.BytesIO(b"bad"))
    with pytest.raises(JavaError):
        JavaArchive.extract(archive_path, tmp_path / "unpacked", lambda: None)
    assert not (tmp_path / "outside").exists()
    assert not list((tmp_path / "unpacked").rglob("*"))


def test_tar_links_cannot_escape_and_internal_link_is_preserved(tmp_path):
    archive_path = tmp_path / "links.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        member = tarfile.TarInfo("runtime/legal/license")
        member.size = 3
        archive.addfile(member, io.BytesIO(b"GPL"))
        link = tarfile.TarInfo("runtime/legal/alias")
        link.type = tarfile.SYMTYPE
        link.linkname = "license"
        archive.addfile(link)
    try:
        JavaArchive.extract(archive_path, tmp_path / "valid", lambda: None)
    except JavaError as exc:
        if sys.platform == "win32" and isinstance(exc.__cause__, OSError):
            pytest.skip("Windows symlink privilege unavailable")
        raise
    assert (tmp_path / "valid/runtime/legal/alias").read_bytes() == b"GPL"
    with tarfile.open(archive_path, "w:gz") as archive:
        link.linkname = "../../../../outside"
        archive.addfile(link)
    with pytest.raises(JavaError):
        JavaArchive.extract(archive_path, tmp_path / "invalid", lambda: None)


def test_install_uses_checksum_and_registry_write_failure_rolls_back_only_new_files(manager, tmp_path, monkeypatch):
    archive_bytes = _archive_bytes()
    plan = _plan(manager, archive_bytes)
    real_client = httpx.Client
    monkeypatch.setattr(
        "ECL.services.java.installer.httpx.Client",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=archive_bytes))
        ),
    )
    original = manager.registry.write
    monkeypatch.setattr(
        manager.registry,
        "write",
        lambda state: (_ for _ in ()).throw(JavaError("保存失败", "JAVA_REGISTRY_WRITE_FAILED")),
    )
    with pytest.raises(JavaError, match="保存失败"):
        manager._installer.install(plan, _context())
    assert not plan.install_path.exists()
    assert not manager.registry.read().runtimes
    monkeypatch.setattr(manager.registry, "write", original)
    installed = manager._installer.install(plan, _context())
    assert installed.origin == "managed" and installed.runtime_kind == "JRE"
    assert plan.install_path.is_dir()
    assert manager._installer.install(plan, _context()).runtime_id == installed.runtime_id
    unknown_file = plan.install_path / "user-save.txt"
    unknown_file.write_text("keep", encoding="utf-8")
    with pytest.raises(JavaError, match="未登记内容"):
        manager.registry.validate_owned_directory(installed)
    assert unknown_file.read_text(encoding="utf-8") == "keep"


def test_hash_mismatch_and_cancellation_keep_existing_runtime(manager, tmp_path, monkeypatch):
    old = manager.register(_java_path(tmp_path / "old"))
    archive_bytes = _archive_bytes()
    plan = _plan(manager, archive_bytes)
    real_client = httpx.Client
    monkeypatch.setattr(
        "ECL.services.java.installer.httpx.Client",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"wrong"))
        ),
    )
    with pytest.raises(JavaError, match="哈希或大小"):
        manager._installer.install(plan, _context())
    context = _context()
    context.cancel_event.set()
    with pytest.raises(Exception, match="取消"):
        manager._installer.install(plan, context)
    assert manager.registry.read().runtimes[0].runtime_id == old.runtime_id
    assert old.executable_path.exists()
    assert not plan.install_path.exists()


def test_same_target_submissions_share_receipt_and_expired_plan_is_rejected(manager, monkeypatch):
    plan = _plan(manager, _archive_bytes())
    started, finish = Event(), Event()

    def install(saved_plan, context):
        started.set()
        finish.wait(3)
        raise JavaError("测试取消", "JAVA_TEST")

    monkeypatch.setattr(manager._installer, "install", install)
    first = manager.install(plan.plan_id)
    assert started.wait(1)
    second = manager.install(plan.plan_id)
    assert first["operationId"] == second["operationId"]
    finish.set()
    manager._plans[plan.plan_id] = plan.model_copy(update={"expires_at": 0})
    with pytest.raises(JavaError, match="过期"):
        manager.install(plan.plan_id)


def test_catalog_validates_actual_platform_and_never_invents_missing_packages(manager):
    payload = {"available_releases": [8, 17, 21, 25], "most_recent_lts": 25}
    manager.catalog._client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=payload if request.url.path.endswith("available_releases") else [])
        )
    )
    try:
        assert manager.catalog.releases() == (8, 17, 21, 25)
        assert manager.catalog.recommended_major == 25
        assert manager.catalog.packages(25, "JRE") == ()
    finally:
        manager.catalog._client.close()


def test_selection_reads_current_instance_requirement_instead_of_frontend_snapshot(manager, tmp_path):
    import asyncio
    from types import SimpleNamespace

    from ECL.api.java import JavaHandlers

    record = manager.register(_java_path(tmp_path / "java21", "21.0.2"))
    api = object.__new__(JavaHandlers)
    api.context = SimpleNamespace(java=manager)
    api.game = SimpleNamespace(java_requirement=lambda _root, _version: 25)
    with pytest.raises(JavaError, match="Java 25"):
        asyncio.run(
            JavaHandlers.game_java_select.__wrapped__(
                api,
                {
                    "runtime_id": record.runtime_id,
                    "required_major": 21,
                    "game_path": str(tmp_path),
                    "version_id": "instance",
                },
            )
        )
