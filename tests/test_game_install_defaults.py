from __future__ import annotations

import asyncio
from threading import Event, get_ident
from types import SimpleNamespace
from unittest.mock import ANY, Mock

import httpx
import pytest

from ECL.services.game import GameService, GameServiceError


@pytest.fixture
def install_service():
    service = GameService(SimpleNamespace(), enable_version_watcher=False)
    try:
        yield service
    finally:
        service.close()


@pytest.fixture
def core_builders(install_service, monkeypatch):
    builders = {
        name: Mock(return_value=[])
        for name in (
            "build_minecraft_download_list",
            "build_fabric_download_list",
            "build_forge_download_list",
            "build_neoforged_download_list",
            "build_quilt_download_list",
        )
    }
    games = SimpleNamespace(**builders)
    monkeypatch.setattr(install_service, "_context", lambda *_: SimpleNamespace(games=games))
    return builders


def _install_blocking(service, game_path, loader, loader_version, cancel_event=None):
    service.install_blocking(
        "default-test",
        "1.20.1",
        "Chosen Name",
        loader,
        loader_version,
        None,
        game_path,
        "official",
        "java",
        lambda *_args, **_kwargs: None,
        cancel_event,
        include_fabric_api=False,
    )


@pytest.mark.parametrize(
    ("loader", "query_name", "builder_name", "versions", "latest"),
    [
        ("forge", "get_forge_versions", "build_forge_download_list", ["47.4.9", "47.4.10", "47.4.2"], "47.4.10"),
        (
            "neoforge",
            "get_neoforged_versions",
            "build_neoforged_download_list",
            ["21.1.9", "21.1.10-beta", "21.1.2"],
            "21.1.10-beta",
        ),
        (
            "neoforge",
            "get_neoforged_versions",
            "build_neoforged_download_list",
            ["1.20.1-47.1.99", "1.20.1-47.1.106", "1.20.1-47.1.9"],
            "1.20.1-47.1.106",
        ),
        ("fabric", "get_fabric_versions", "build_fabric_download_list", ["0.16.9", "0.16.14", "0.16.2"], "0.16.14"),
        (
            "quilt",
            "get_quilt_versions",
            "build_quilt_download_list",
            ["0.27.0-beta.1", "0.27.0", "0.26.4"],
            "0.27.0",
        ),
    ],
)
@pytest.mark.parametrize("source", ["official", "bmclapi"])
@pytest.mark.parametrize("default_value", [None, "", " \t "])
def test_default_loader_selects_semantic_latest_before_building(
    install_service,
    core_builders,
    tmp_path,
    monkeypatch,
    loader,
    query_name,
    builder_name,
    versions,
    latest,
    source,
    default_value,
):
    candidates = [{"LoaderVersion": version} for version in versions]
    query = Mock(return_value=candidates if loader == "forge" else {"All": candidates})
    query_context = Mock(return_value=SimpleNamespace(games=SimpleNamespace(**{query_name: query})))
    monkeypatch.setattr(install_service, "_query_context", query_context)

    install_service.install_blocking(
        "default-test",
        "1.20.1",
        "Chosen Name",
        loader,
        default_value,
        None,
        tmp_path,
        source,
        "java",
        lambda *_a, **_k: None,
        include_fabric_api=False,
    )

    query_context.assert_called_once_with(source)
    query.assert_called_once_with("1.20.1")
    expected = (
        ("1.20.1", latest, "java", "Chosen Name")
        if loader in {"forge", "neoforge"}
        else ("1.20.1", latest, "Chosen Name")
    )
    core_builders[builder_name].assert_called_once_with(*expected, observer=ANY)


@pytest.mark.parametrize("loader", ["fabric", "forge", "neoforge", "quilt"])
def test_explicit_loader_version_skips_query(install_service, core_builders, tmp_path, monkeypatch, loader):
    query = Mock(side_effect=AssertionError("手选版本不得查询最新版"))
    monkeypatch.setattr(install_service, "loader_versions", query)
    _install_blocking(install_service, tmp_path, loader, " 0.15.9 ")
    builder_name = "build_neoforged_download_list" if loader == "neoforge" else f"build_{loader}_download_list"
    assert core_builders[builder_name].call_args.args[1] == "0.15.9"
    query.assert_not_called()


@pytest.mark.parametrize("value", [0, False, [], {}])
def test_invalid_loader_input_is_not_default(install_service, core_builders, tmp_path, value):
    with pytest.raises(GameServiceError) as raised:
        install_service.install_version(
            {"version_id": "1.20.1", "loader_type": "forge", "forge_version": value},
            game_path=tmp_path,
        )
    assert raised.value.error_code == "INVALID_LOADER_VERSION"
    assert not list(tmp_path.iterdir())
    assert not install_service._install_tasks
    assert all(not builder.called for builder in core_builders.values())


@pytest.mark.parametrize(
    ("versions", "error_code"),
    [([], "LOADER_VERSION_NOT_FOUND"), (["47.4.9", "unknown"], "INVALID_LOADER_VERSION_DATA")],
)
def test_default_loader_query_data_failure_does_not_build(
    install_service,
    core_builders,
    tmp_path,
    monkeypatch,
    versions,
    error_code,
):
    monkeypatch.setattr(install_service, "loader_versions", lambda *_: versions)
    with pytest.raises(GameServiceError) as raised:
        _install_blocking(install_service, tmp_path, "forge", None)
    assert raised.value.error_code == error_code
    assert all(not builder.called for builder in core_builders.values())
    assert not list(tmp_path.iterdir())


def test_default_loader_network_failure_is_reported(install_service, core_builders, tmp_path, monkeypatch):
    error = httpx.ReadTimeout("query timed out")
    monkeypatch.setattr(install_service, "loader_versions", Mock(side_effect=error))
    with pytest.raises(GameServiceError) as raised:
        _install_blocking(install_service, tmp_path, "forge", None)
    assert raised.value.error_code == "LOADER_VERSION_QUERY_FAILED"
    assert raised.value.__cause__ is error
    assert all(not builder.called for builder in core_builders.values())
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("cancel_before_query", [True, False])
def test_cancelled_default_loader_does_not_build(
    install_service,
    core_builders,
    tmp_path,
    monkeypatch,
    cancel_before_query,
):
    cancel_event = Event()
    if cancel_before_query:
        cancel_event.set()

    def query(*_):
        cancel_event.set()
        return ["47.4.10"]

    query_mock = Mock(side_effect=query)
    monkeypatch.setattr(install_service, "loader_versions", query_mock)
    with pytest.raises(GameServiceError) as raised:
        _install_blocking(install_service, tmp_path, "forge", None, cancel_event)
    assert raised.value.error_code == "INSTALL_CANCELLED"
    assert query_mock.call_count == (0 if cancel_before_query else 1)
    assert all(not builder.called for builder in core_builders.values())
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("loader", ["fabric", "forge", "neoforge", "quilt"])
@pytest.mark.parametrize("request_value", ["omitted", None, "", "  "])
def test_install_request_accepts_defaults_and_queries_on_worker(
    install_service,
    core_builders,
    tmp_path,
    monkeypatch,
    loader,
    request_value,
):
    request_thread = get_ident()
    query_threads = []

    def query(*_):
        query_threads.append(get_ident())
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        return ["0.16.9", "0.16.14"]

    monkeypatch.setattr(install_service, "loader_versions", query)
    monkeypatch.setattr(install_service, "_resolve_java_path", lambda _: "java")
    monkeypatch.setattr(install_service, "_resolve_fabric_api", lambda *_: ("https://example.test/api.jar", "api.jar"))

    # Fabric 仍经过原有 API 处理，下载器只用于观察流程，不访问网络。
    async def run():
        return None

    downloader = SimpleNamespace(run=run, stop=Mock(), failed_entries=set(), completed_entries=set())
    monkeypatch.setattr(install_service, "_downloader_factory", lambda *_a, **_k: downloader)
    events = []
    install_service.events.subscribe("game:install_progress", events.append)
    body = {"version_id": "1.20.1", "version_name": "Chosen Name", "loader_type": loader}
    if request_value != "omitted":
        body[f"{loader}_version"] = request_value

    async def install():
        result = install_service.install_version(body, game_path=tmp_path)
        assert result["versionName"] == "Chosen Name"
        await install_service._install_tasks[result["taskId"]]

    asyncio.run(install())
    assert query_threads and query_threads[0] != request_thread
    assert events[-1]["installation"]["status"] == "completed", events
    assert not install_service._install_tasks
    assert not install_service._active_downloads
    builder_name = "build_neoforged_download_list" if loader == "neoforge" else f"build_{loader}_download_list"
    assert core_builders[builder_name].call_args.args[1] == "0.16.14"
    assert core_builders[builder_name].call_args.args[-1] == "Chosen Name"


def test_async_default_query_failure_emits_error(install_service, core_builders, tmp_path, monkeypatch):
    monkeypatch.setattr(install_service, "_resolve_java_path", lambda _: "java")
    monkeypatch.setattr(install_service, "loader_versions", lambda *_: [])
    events = []
    install_service.events.subscribe("game:install_progress", events.append)

    async def install():
        result = install_service.install_version(
            {"version_id": "1.20.1", "loader_type": "forge", "forge_version": ""},
            game_path=tmp_path,
        )
        await install_service._install_tasks[result["taskId"]]

    asyncio.run(install())
    assert events[-1]["installation"]["status"] == "error"
    assert events[-1]["installation"]["errorCode"] == "LOADER_VERSION_NOT_FOUND"
    assert all(not builder.called for builder in core_builders.values())
    assert not list((tmp_path / "versions").iterdir())


def test_vanilla_skips_loader_query(install_service, core_builders, tmp_path, monkeypatch):
    query = Mock(side_effect=AssertionError("原版不得查询加载器"))
    monkeypatch.setattr(install_service, "loader_versions", query)
    _install_blocking(install_service, tmp_path, "vanilla", None)
    core_builders["build_minecraft_download_list"].assert_called_once_with("1.20.1", "Chosen Name", observer=ANY)
    query.assert_not_called()


def test_cancellation_during_failed_query_wins(install_service, core_builders, tmp_path, monkeypatch):
    cancel_event = Event()

    def query(*_):
        cancel_event.set()
        raise httpx.ReadTimeout("cancelled query")

    monkeypatch.setattr(install_service, "loader_versions", query)
    with pytest.raises(GameServiceError) as raised:
        _install_blocking(install_service, tmp_path, "forge", None, cancel_event)
    assert raised.value.error_code == "INSTALL_CANCELLED"
    assert all(not builder.called for builder in core_builders.values())
