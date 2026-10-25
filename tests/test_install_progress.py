from __future__ import annotations

import asyncio
from itertools import pairwise
from threading import Event
from types import SimpleNamespace

import pytest

from ECL.game.Core.InstallProgress import InstallEvent
from ECL.services.game import GameService, GameServiceError
from ECL.services.game.install_progress import InstallationProgress


@pytest.mark.parametrize("loader", ["vanilla", "fabric", "quilt", "forge", "neoforge"])
def test_install_exposes_stages_before_core_downloads(tmp_path, monkeypatch, loader):
    snapshots = []

    def report(*_args, **details):
        snapshots.append(details)

    def build(*_args, **_kwargs):
        assert snapshots and "installation" in snapshots[-1]
        assert snapshots[-1]["installation"]["currentStageId"] == "resolve_metadata"
        return []

    service = GameService(SimpleNamespace(), enable_version_watcher=False)
    games = SimpleNamespace(
        **{f"build_{name}_download_list": build for name in ["minecraft", "fabric", "quilt", "forge", "neoforged"]}
    )
    monkeypatch.setattr(service, "_context", lambda *_: SimpleNamespace(games=games))
    try:
        service.install_blocking(
            "progress-test",
            "1.20.1",
            "Test",
            loader,
            "1",
            None,
            tmp_path,
            "official",
            "java",
            report,
            include_fabric_api=False,
        )
        final = snapshots[-1]["installation"]
        assert final["status"] == "completed"
        assert not any(stage["status"] == "running" for stage in final["stages"])
    finally:
        service.close()


def test_stage_switches_cancel_request_and_terminal_ignore_late_events():
    snapshots = []
    progress = InstallationProgress("forge", False, lambda *_, **details: snapshots.append(details["installation"]))
    progress.accept(InstallEvent("resolve_metadata"))
    progress.accept(InstallEvent("download_client", unit="bytes", completed=60, total=100, speed=10))
    download = snapshots[-1]
    assert download["stages"][0]["status"] == "completed"
    progress.request_cancel()
    assert snapshots[-1]["status"] == "running"
    assert snapshots[-1]["cancellationRequested"]
    progress.finish("canceled", "安装已取消", "INSTALL_CANCELLED")
    final = snapshots[-1]
    progress.accept(InstallEvent("download_loader"))
    assert snapshots[-1] == final
    assert all(stage["download"] is None or stage["download"]["speedBytesPerSecond"] == 0 for stage in final["stages"])
    assert download["status"] == "running"  # 已投递快照不会被后续原地修改。
    assert all(a["revision"] < b["revision"] for a, b in pairwise(snapshots))


def test_independent_install_records_do_not_share_stages():
    first, second = [], []
    record_a = InstallationProgress("vanilla", False, lambda *_, **d: first.append(d["installation"]))
    record_b = InstallationProgress("fabric", True, lambda *_, **d: second.append(d["installation"]))
    record_a.accept(InstallEvent("resolve_metadata"))
    record_b.accept(InstallEvent("resolve_fabric_api"))
    record_a.finish("error", "网络失败")
    assert second[-1]["status"] == "running"
    assert second[-1]["currentStageId"] == "resolve_fabric_api"
    assert first[-1]["currentStageId"] == "resolve_metadata"


async def test_cancelling_waiter_keeps_task_running_until_worker_exits(tmp_path, monkeypatch):
    started, release = Event(), Event()
    events = []
    service = GameService(SimpleNamespace(), enable_version_watcher=False)
    service.events.subscribe("game:install_progress", events.append)

    def build(*_args, observer):
        started.set()
        assert release.wait(5)
        observer.check_cancelled()
        return []

    monkeypatch.setattr(
        service, "_context", lambda *_: SimpleNamespace(games=SimpleNamespace(build_minecraft_download_list=build))
    )
    try:
        result = service.install_version({"version_id": "1.20.1"}, game_path=tmp_path)
        task = service._install_tasks[result["taskId"]]
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done()
        assert events[-1]["installation"]["status"] == "running"
        assert events[-1]["installation"]["cancellationRequested"]
        release.set()
        await asyncio.wait_for(task, 5)
        assert events[-1]["installation"]["status"] == "canceled"
    finally:
        release.set()
        service.close()


def test_completed_shared_files_do_not_leave_install_marked_failed(tmp_path, monkeypatch):
    """其他安装已补齐同一缓存文件后，以 Core 的实际校验结果确认最终状态。"""
    files = [("https://example.test/file", str(tmp_path / "libraries/file.jar"))]
    snapshots = []

    class DisconnectedDownloader:
        def __init__(self, entries, **_kwargs):
            self.failed_entries = set(entries)
            self.completed_entries = set()
            self.local_failed_paths = set()
            self.bytes_downloaded_for_speed = 0

        async def run(self):
            pass

    service = GameService(SimpleNamespace(), downloader_factory=DisconnectedDownloader, enable_version_watcher=False)
    # 检查器已按版本清单和哈希确认没有缺失文件；不能只按文件存在判断成功。
    context = SimpleNamespace(
        games=SimpleNamespace(build_minecraft_download_list=lambda *a, **k: files),
        files_checker=SimpleNamespace(check_files=lambda *a, **k: []),
    )
    monkeypatch.setattr(service, "_context", lambda *_: context)
    try:
        service.install_blocking(
            "shared",
            "1.20.1",
            "Test",
            "vanilla",
            None,
            None,
            tmp_path,
            "official",
            None,
            lambda *a, **k: snapshots.append(k["installation"]),
        )
        assert snapshots[-1]["status"] == "completed"
    finally:
        service.close()


def test_fabric_api_failure_is_preserved_when_game_metadata_is_complete(tmp_path, monkeypatch):
    class FailedApiDownloader:
        def __init__(self, entries, **_kwargs):
            self.failed_entries = set(entries)
            self.completed_entries = set()
            self.local_failed_paths = set()
            self.bytes_downloaded_for_speed = 0

        async def run(self):
            pass

    service = GameService(SimpleNamespace(), downloader_factory=FailedApiDownloader, enable_version_watcher=False)
    context = SimpleNamespace(
        games=SimpleNamespace(build_fabric_download_list=lambda *a, **k: []),
        files_checker=SimpleNamespace(check_files=lambda *a, **k: []),
    )
    monkeypatch.setattr(service, "_context", lambda *_: context)
    monkeypatch.setattr(service, "_resolve_fabric_api", lambda *_: ("https://example.test/api.jar", "api.jar"))
    monkeypatch.setattr(service, "resolve_version_isolation", lambda *_: True)
    monkeypatch.setattr(service, "resolve_instance", lambda *_: SimpleNamespace(game_data_path=tmp_path))
    try:
        with pytest.raises(GameServiceError) as raised:
            service.install_blocking(
                "api", "1.20.1", "Test", "fabric", "1", None, tmp_path, "official", None, lambda *a, **k: None
            )
        assert raised.value.error_code == "GAME_DOWNLOAD_FAILED"
    finally:
        service.close()
