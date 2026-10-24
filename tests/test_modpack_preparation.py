from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from test_modpack import (
    _await_operation,
    _build_pack_service,
    _make_base_version,
    _make_pack_zip,
    _pack_downloader_factory,
)

from ECL.api.models import ModpackInstallRequest, ModpackPrepareRequest
from ECL.game import LaunchConfig, build_minecraft_cmd
from ECL.services.game.modpack import ModpackCoordinator, ModpackPlan, PackFileEntry, build_pack_plan
from ECL.services.game.modpack_preparation import ModpackPreparationService, PreparedModpackFile
from ECL.utils.errors import GameServiceError


def test_mcbbs_curseforge_references_are_resolved(tmp_path: Path) -> None:
    (tmp_path / "mcbbs.packmeta").write_text(
        json.dumps(
            {
                "addons": [{"id": "net.minecraft", "version": "1.21.1"}],
                "files": [{"projectID": 123, "fileID": 456}],
            }
        ),
        encoding="utf-8",
    )
    plan = build_pack_plan(tmp_path)
    calls = []

    def fetch(project_id: str, file_id: str) -> dict[str, object]:
        calls.append((project_id, file_id))
        return {"filename": "demo.jar", "url": "https://example.com/demo.jar", "hashes": {}}

    service = SimpleNamespace(_fetch_curseforge_file=fetch)
    context = SimpleNamespace(check_cancelled=lambda: None, progress=lambda *_args: None)
    entries, skipped = ModpackCoordinator._resolve_pack_entries(service, plan, context)
    assert calls == [("123", "456")]
    assert entries[0].target_relative == "mods/demo.jar"
    assert skipped == 0


def test_mcbbs_force_is_not_optional(tmp_path: Path) -> None:
    (tmp_path / "mcbbs.packmeta").write_text(
        json.dumps(
            {
                "addons": [{"id": "net.minecraft", "version": "1.21.1"}],
                "files": [{"projectID": 1, "fileID": 2, "force": False}],
            }
        ),
        encoding="utf-8",
    )
    assert build_pack_plan(tmp_path).files[0].env_client == "required"


@pytest.fixture
def preparations():
    service = ModpackPreparationService()
    yield service
    service.close()


def _ready(service: ModpackPreparationService):
    record = service.create("local")
    service.begin(record, Event())
    files = (
        PreparedModpackFile("a" * 32, PackFileEntry("mods/required.jar")),
        PreparedModpackFile("b" * 32, PackFileEntry("resourcepacks/optional.zip", env_client="optional")),
        PreparedModpackFile("c" * 32, PackFileEntry("mods/server.jar", env_client="unsupported")),
    )
    service.complete(record, ModpackPlan("mrpack", "Test", minecraft_version="1.21.1"), files)
    service.finish_worker(record, prepared=True)
    return record


@pytest.mark.parametrize("excluded", [["a" * 32], ["c" * 32], ["unknown"], ["b" * 32, "b" * 32]])
def test_invalid_selections_cannot_claim_preview(preparations, tmp_path, excluded):
    record = _ready(preparations)
    with pytest.raises(GameServiceError, match="只能取消"):
        preparations.claim(record.preparation_id, excluded, tmp_path / "Instance")
    assert preparations.get(record.preparation_id)["status"] == "ready"


def test_release_during_preparation_waits_for_worker(preparations):
    record = preparations.create("local")
    cancellation = Event()
    preparations.begin(record, cancellation)
    preparations.release(record.preparation_id)
    assert cancellation.is_set()
    assert record.directory.exists()
    with pytest.raises(GameServiceError):
        preparations.complete(record, ModpackPlan("mrpack", "Test"), ())
    preparations.finish_worker(record)
    assert not record.directory.exists()
    assert preparations.release(record.preparation_id)


def test_release_before_worker_start_is_safe(preparations):
    record = preparations.create("local")
    preparations.release(record.preparation_id)
    with pytest.raises(GameServiceError):
        preparations.begin(record, Event())
    assert not record.directory.exists()


def test_install_owns_resources_after_preview_closed(preparations, tmp_path):
    record = _ready(preparations)
    preparations.claim(record.preparation_id, ["b" * 32], tmp_path / "Instance")
    cancellation = Event()
    preparations.begin(record, cancellation)
    preparations.release(record.preparation_id)
    assert record.directory.exists()
    assert not cancellation.is_set()
    preparations.close()
    assert cancellation.is_set()
    assert record.directory.exists()
    preparations.finish_worker(record)
    assert not record.directory.exists()


def test_expired_preview_and_limit_do_not_delete_user_files(preparations, tmp_path):
    original = tmp_path / "source.zip"
    original.write_bytes(b"original")
    record = _ready(preparations)
    for _ in range(3):
        preparations.create("local")
    with pytest.raises(GameServiceError) as error:
        preparations.create("local")
    assert error.value.error_code == "PACK_PREPARATION_LIMIT_REACHED"
    record.expires_at = -1
    with pytest.raises(GameServiceError) as error:
        preparations.get(record.preparation_id)
    assert error.value.error_code == "PACK_PREPARATION_EXPIRED"
    assert not record.directory.exists()
    assert original.read_bytes() == b"original"


def test_two_previews_cannot_install_same_target(preparations, tmp_path):
    records = [_ready(preparations), _ready(preparations)]

    def claim(record):
        try:
            preparations.claim(record.preparation_id, [], tmp_path / "Instance")
            return "accepted"
        except GameServiceError as error:
            return error.error_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, records))
    assert sorted(results) == ["PACK_TARGET_BUSY", "accepted"]
    accepted = next(record for record in records if record.status == "installing")
    preparations.rollback_claim(accepted)
    assert preparations.get(accepted.preparation_id)["status"] == "ready"


def test_cleanup_failure_is_visible_and_retries_are_bounded(preparations, monkeypatch):
    record = _ready(preparations)
    calls = []

    def fail(_directory):
        calls.append(1)
        raise PermissionError("locked")

    with monkeypatch.context() as patch:
        patch.setattr("ECL.services.game.modpack_preparation.shutil.rmtree", fail)
        preparations.release(record.preparation_id)
        for _ in range(8):
            preparations._expire()
        assert preparations.get(record.preparation_id)["cleanupPending"]
        assert len(calls) == preparations.cleanup_limit
    preparations.close()


def test_prepared_pack_selection_filters_downloads_and_overrides(tmp_path, monkeypatch):
    downloads = []
    factory = _pack_downloader_factory({})

    def tracked(entries, **kwargs):
        downloads.extend(url for url, _path in entries)
        return factory(entries, **kwargs)

    service = _build_pack_service(tracked)
    try:
        game_path = tmp_path / "game"
        _make_base_version(game_path, "1.21.1")
        pack = tmp_path / "test.mrpack"
        index = {
            "name": "Selection",
            "dependencies": {"minecraft": "1.21.1"},
            "files": [
                {"path": "mods/required.jar", "downloads": ["https://example.com/required"]},
                {
                    "path": "resourcepacks/optional.zip",
                    "downloads": ["https://example.com/optional"],
                    "env": {"client": "optional"},
                },
                {
                    "path": "mods/server.jar",
                    "downloads": ["https://example.com/server"],
                    "env": {"client": "unsupported"},
                },
            ],
        }
        _make_pack_zip(
            pack,
            {
                "modrinth.index.json": json.dumps(index).encode(),
                "overrides/resourcepacks/optional.zip": b"must-not-reintroduce",
                "overrides/mods/server.jar": b"must-not-reintroduce",
                "overrides/config/settings.cfg": b"keep",
            },
        )
        submitted = service.prepare_modpack("local", source_path=pack)
        assert _await_operation(service, submitted)["status"] == "completed"
        preview = service.get_modpack_preparation(submitted["preparationId"])
        assert downloads == []
        assert "https://" not in json.dumps(preview)
        assert str(tmp_path) not in json.dumps(preview)
        files = preview["preview"]["files"]
        optional_id = next(item["fileId"] for item in files if item["requirement"] == "optional")
        # 安装必须使用私有快照，不能重读原始包。
        pack.write_bytes(b"source changed")
        state = _await_operation(
            service, service.install_prepared_modpack(submitted["preparationId"], game_path, "Selected", [optional_id])
        )
        assert state["status"] == "completed", state
        assert downloads == ["https://example.com/required"]
        target = game_path / "versions/Selected"
        assert (target / "mods/required.jar").exists()
        assert not (target / "mods/server.jar").exists()
        assert not (target / "resourcepacks/optional.zip").exists()
        assert (target / "config/settings.cfg").read_bytes() == b"keep"
        assert state["result"]["excludedOptionalFiles"] == 1
        assert state["result"]["unsupportedFiles"] == 1
    finally:
        service.close()


def test_curseforge_files_get_distinct_ids_without_duplicate_metadata_requests(tmp_path, monkeypatch):
    service = _build_pack_service(_pack_downloader_factory({}))
    calls = []
    try:
        game_path = tmp_path / "game"
        _make_base_version(game_path, "1.21.1")
        pack = tmp_path / "cf.zip"
        _make_pack_zip(
            pack,
            {
                "manifest.json": json.dumps(
                    {
                        "minecraft": {"version": "1.21.1"},
                        "files": [
                            {"projectID": 1, "fileID": 11, "required": False},
                            {"projectID": 2, "fileID": 22, "required": False},
                        ],
                    }
                ).encode()
            },
        )

        def fetch(project_id, file_id):
            calls.append((project_id, file_id))
            return {"filename": f"{project_id}.jar", "url": f"https://example.com/{file_id}", "hashes": {}}

        monkeypatch.setattr(service, "_fetch_curseforge_file", fetch)
        prepared = service.prepare_modpack("local", source_path=pack)
        assert _await_operation(service, prepared)["status"] == "completed"
        files = service.get_modpack_preparation(prepared["preparationId"])["preview"]["files"]
        assert len({item["fileId"] for item in files}) == 2
        assert [item["path"] for item in files] == ["mods/1.jar", "mods/2.jar"]
        assert (
            _await_operation(service, service.install_prepared_modpack(prepared["preparationId"], game_path, "CF", []))[
                "status"
            ]
            == "completed"
        )
        assert calls == [("1", "11"), ("2", "22")]
    finally:
        service.close()


def test_prepare_requests_are_discriminated_and_selection_is_explicit():
    from pydantic import ValidationError

    assert (
        ModpackPrepareRequest.model_validate({"source": "ftb", "project_id": "1", "file_id": "2"}).root.source == "ftb"
    )
    with pytest.raises(ValidationError):
        ModpackPrepareRequest.model_validate({"source": "local", "source_path": "test.zip", "project_id": "1"})
    with pytest.raises(ValidationError):
        ModpackInstallRequest.model_validate(
            {"preparation_id": "a" * 32, "game_path": "game", "new_version_id": "Test"}
        )


@pytest.mark.parametrize("loader", [False, True])
def test_imported_instance_builds_core_launch_command_with_complete_metadata(tmp_path, loader):
    game_path = tmp_path / "game"
    base_name = "fabric-0.16-1.21.1" if loader else "1.21.1"
    base = game_path / "versions" / base_name
    base.mkdir(parents=True)
    vanilla = {
        "id": "1.21.1",
        "mainClass": "net.minecraft.client.main.Main",
        "libraries": [],
        "arguments": {"jvm": ["-cp", "${classpath}"], "game": ["--username", "${auth_player_name}"]},
    }
    main = vanilla
    if loader:
        (base / "1.21.1.json").write_text(json.dumps(vanilla), encoding="utf-8")
        (base / "1.21.1.jar").write_bytes(b"client-jar")
        main = {
            "id": base_name,
            "mainClass": "net.fabricmc.loader.impl.launch.knot.KnotClient",
            "inheritsFrom": "1.21.1",
            "libraries": [],
            "arguments": {"jvm": [], "game": []},
        }
    else:
        (base / f"{base_name}.jar").write_bytes(b"client-jar")
    (base / f"{base_name}.json").write_text(json.dumps(main), encoding="utf-8")
    service = _build_pack_service(_pack_downloader_factory({}))
    try:
        target = service.resolve_instance(game_path, "Imported")
        staging = tmp_path / "staging"
        staging.mkdir()
        service._write_pack_version_json(base_name, target, staging)
        staging.replace(target.instance_path)
        command = build_minecraft_cmd(
            LaunchConfig(
                java_path="java",
                game_path=game_path,
                version_name="Imported",
                use_ram=1024,
                player_name="TestPlayer",
                auth_uuid="0" * 32,
            )
        )
        assert main["mainClass"] in command
        assert command.count("--username") == 1
        client_name = "1.21.1.jar" if loader else "Imported.jar"
        assert (target.instance_path / client_name).read_bytes() == b"client-jar"
    finally:
        service.close()
