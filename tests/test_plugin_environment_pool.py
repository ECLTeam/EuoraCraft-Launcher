from __future__ import annotations

import hashlib
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from plugin_wheel_helpers import make_wheel, wheel_lock

from ECL.plugins.dependency_lock import PluginDependencyError, PluginDependencyLock, plugin_target
from ECL.plugins.environment_pool import PluginEnvironmentPool, PluginEnvironmentSpec
from ECL.plugins.wheel_installation import PluginWheelRepository


def spec_for(tmp_path: Path, *wheels: Path, direct: tuple[str, ...] = ()) -> PluginEnvironmentSpec:
    lock_path = tmp_path / "requirements.lock"
    lock_path.write_bytes(wheel_lock(*wheels))
    return PluginEnvironmentSpec(plugin_target(), lock_path, wheels[0].parent if wheels else None, direct)


def test_offline_install_reuses_directory_and_cache_without_interpreter(tmp_path: Path, monkeypatch) -> None:
    wheel = make_wheel(tmp_path / "wheels")
    pool = PluginEnvironmentPool(tmp_path / "data")
    spec = spec_for(tmp_path, wheel)
    before_path = list(sys.path)
    monkeypatch.setattr("subprocess.run", lambda *_args, **_kwargs: pytest.fail("不能启动安装子进程"))
    installed = pool.ensure(spec)
    assert (installed.site_path / "ecl_test_dependency.py").is_file()
    assert sys.path == before_path
    assert "ecl_test_dependency" not in sys.modules
    assert not list((tmp_path / "data").rglob("python.exe"))
    assert not (tmp_path / "data" / "plugin_envs").exists()
    assert pool.ensure(spec) == installed
    assert list(pool.cache_path.rglob("*.whl"))
    other_pool = PluginEnvironmentPool(tmp_path / "data")
    assert other_pool.ready_directory(installed.key) == installed


def test_identical_locks_share_and_different_locks_separate(tmp_path: Path) -> None:
    first = make_wheel(tmp_path / "one")
    second = make_wheel(tmp_path / "two", version="2.0.0")
    pool = PluginEnvironmentPool(tmp_path / "data")
    spec = spec_for(tmp_path, first)
    installed = pool.ensure(spec)
    assert pool.ensure(spec).key == installed.key
    other_spec = spec_for(tmp_path, second)
    assert pool.ensure(other_spec).key != installed.key


def test_concurrent_install_commits_once(tmp_path: Path) -> None:
    wheel = make_wheel(tmp_path / "wheels")
    pool = PluginEnvironmentPool(tmp_path / "data")
    spec = spec_for(tmp_path, wheel)
    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(lambda _: pool.ensure(spec), range(3)))
    assert len({item.key for item in results}) == 1
    assert not list(pool.environments_path.glob("*.staging"))


@pytest.mark.parametrize("mutation", ["file", "extra", "symlink", "inventory"])
def test_corrupt_directory_is_never_overwritten(tmp_path: Path, mutation: str) -> None:
    wheel = make_wheel(tmp_path / "wheels")
    pool = PluginEnvironmentPool(tmp_path / "data")
    spec = spec_for(tmp_path, wheel)
    installed = pool.ensure(spec)
    if mutation == "file":
        (installed.site_path / "ecl_test_dependency.py").write_bytes(b"changed")
    elif mutation == "extra":
        (installed.site_path / "extra.py").write_bytes(b"added")
    elif mutation == "inventory":
        (installed.site_path.parent / "installed.json").write_bytes(b"{}")
    else:
        try:
            (installed.site_path / "outside").symlink_to(tmp_path, target_is_directory=True)
        except OSError:
            pytest.skip("当前环境不支持符号链接")
    assert pool.ready_directory(installed.key) is None
    with pytest.raises(PluginDependencyError, match="不能覆盖"):
        pool.ensure(spec)


@pytest.mark.parametrize(
    "bad_line",
    [
        b"--index-url https://example.invalid",
        b"demo>=1 --hash=sha256:" + b"0" * 64,
        b"demo==1 --hash=sha256:bad",
        b"demo==1",
        b"demo[x]==1 --hash=sha256:" + b"0" * 64,
    ],
)
def test_lock_rejects_non_exact_or_unsafe_requirements(bad_line: bytes) -> None:
    with pytest.raises(PluginDependencyError):
        PluginDependencyLock.parse(bad_line)


@pytest.mark.parametrize(
    "members",
    [
        {"../outside.py": b"bad"},
        {"bad.pth": b"import os"},
        {"sitecustomize.py": b"pass"},
        {"ecl_test_dependency-1.0.0.data/scripts/demo": b"#!python\n"},
        {"C:/outside.py": b"bad"},
    ],
)
def test_unsupported_wheel_leaves_no_ready_directory(tmp_path: Path, members: dict[str, bytes]) -> None:
    wheel = make_wheel(tmp_path / "wheels", members=members)
    pool = PluginEnvironmentPool(tmp_path / "data")
    with pytest.raises(PluginDependencyError):
        pool.ensure(spec_for(tmp_path, wheel))
    assert not list(pool.environments_path.rglob("ready.json"))
    assert not (tmp_path / "outside.py").exists()


@pytest.mark.parametrize("kwargs", [{"metadata_name": "other"}, {"requires_python": ">=99"}])
def test_metadata_mismatch_rejected(tmp_path: Path, kwargs) -> None:
    wheel = make_wheel(tmp_path / "wheels", **kwargs)
    with pytest.raises(PluginDependencyError):
        PluginEnvironmentPool(tmp_path / "data").ensure(spec_for(tmp_path, wheel))


def test_missing_transitive_dependency_is_not_silently_ignored(tmp_path: Path) -> None:
    wheel = make_wheel(tmp_path / "wheels", requirements=("ecl_missing>=1",))
    with pytest.raises(PluginDependencyError, match="传递依赖"):
        PluginEnvironmentPool(tmp_path / "data").ensure(spec_for(tmp_path, wheel))


def test_extra_dependencies_and_markers_are_checked(tmp_path: Path) -> None:
    wheel = make_wheel(
        tmp_path / "wheels",
        requirements=("ecl_extra==1; extra == 'feature'", "other==1; python_version > '99'"),
        extras=("feature",),
    )
    pool = PluginEnvironmentPool(tmp_path / "data")
    assert pool.ensure(spec_for(tmp_path, wheel))
    with pytest.raises(PluginDependencyError, match="传递依赖"):
        pool.ensure(spec_for(tmp_path, wheel, direct=("ecl_test_dependency[feature]",)))
    # 直接需求不同不会改变锁键，但即使复用也必须校验 extras 的依赖闭包。
    fresh_pool = PluginEnvironmentPool(tmp_path / "other-data")
    with pytest.raises(PluginDependencyError, match="传递依赖"):
        fresh_pool.ensure(spec_for(tmp_path, wheel, direct=("ecl_test_dependency[feature]",)))


def test_console_scripts_have_warning_and_no_executable_wrapper(tmp_path: Path) -> None:
    wheel = make_wheel(
        tmp_path / "wheels",
        members={
            "ecl_test_dependency-1.0.0.dist-info/entry_points.txt": b"[console_scripts]\ndemo = ecl_test_dependency:main\n"
        },
    )
    installed = PluginEnvironmentPool(tmp_path / "data").ensure(spec_for(tmp_path, wheel))
    assert installed.warnings
    assert not list(installed.site_path.parent.rglob("*.exe"))
    assert (installed.site_path.parent / "data" / "unsupported-scripts" / "demo.txt").is_file()


def test_network_download_is_locked_and_then_available_offline(tmp_path: Path) -> None:
    wheel = make_wheel(tmp_path / "source")
    wheel_bytes = wheel.read_bytes()
    digest = hashlib.sha256(wheel_bytes).hexdigest()
    requests: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.host == "pypi.org":
            return httpx.Response(
                200,
                json={
                    "files": [
                        {
                            "filename": wheel.name,
                            "url": f"https://files.pythonhosted.org/{wheel.name}",
                            "hashes": {"sha256": digest},
                        }
                    ]
                },
            )
        return httpx.Response(200, content=wheel_bytes)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        pool = PluginEnvironmentPool(
            tmp_path / "data", PluginWheelRepository(tmp_path / "data" / "plugin_cache" / "wheels", client)
        )
        spec = spec_for(tmp_path, wheel)
        spec = PluginEnvironmentSpec(spec.target_tag, spec.lock_path)
        with pytest.raises(PluginDependencyError, match="未允许联网"):
            pool.ensure(spec)
        assert requests == []
        installed = pool.ensure(spec, allow_network=True)
        assert len(requests) == 2
        assert pool.ensure(spec) == installed
        assert len(requests) == 2


def test_cache_can_supply_new_environment_without_network(tmp_path: Path) -> None:
    wheel = make_wheel(tmp_path / "source")
    pool = PluginEnvironmentPool(tmp_path / "data")
    spec = spec_for(tmp_path, wheel)
    pool.ensure(spec)
    spec.lock_path.write_bytes(spec.lock_path.read_bytes() + b"# different lock bytes\n")
    assert pool.ensure(PluginEnvironmentSpec(spec.target_tag, spec.lock_path))


def test_download_redirect_to_untrusted_origin_rejected(tmp_path: Path) -> None:
    wheel = make_wheel(tmp_path / "source")

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://example.invalid/bad"})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        pool = PluginEnvironmentPool(tmp_path / "data", PluginWheelRepository(tmp_path / "cache", client))
        spec = spec_for(tmp_path, wheel)
        with pytest.raises(PluginDependencyError, match="来源无效"):
            pool.ensure(PluginEnvironmentSpec(spec.target_tag, spec.lock_path), allow_network=True)


def test_network_timeout_is_retried_once_then_reported(tmp_path: Path) -> None:
    wheel = make_wheel(tmp_path / "source")
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ReadTimeout("test timeout", request=request)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        pool = PluginEnvironmentPool(tmp_path / "data", PluginWheelRepository(tmp_path / "cache", client))
        spec = spec_for(tmp_path, wheel)
        with pytest.raises(PluginDependencyError, match="联网取得"):
            pool.ensure(PluginEnvironmentSpec(spec.target_tag, spec.lock_path), allow_network=True)
    assert len(requests) == 2
    assert not list(pool.environments_path.glob("*/ready.json"))


def test_extras_use_normalized_names(tmp_path: Path) -> None:
    wheel = make_wheel(tmp_path / "source", extras=("fast-mode",))
    pool = PluginEnvironmentPool(tmp_path / "data")
    assert pool.ensure(spec_for(tmp_path, wheel, direct=("ecl-test-dependency[FAST_mode]==1.0.0",)))


def test_ready_directory_does_not_ignore_payload_named_installed_json(tmp_path: Path) -> None:
    wheel = make_wheel(tmp_path / "source", members={"ecl_extra/installed.json": b"{}"})
    pool = PluginEnvironmentPool(tmp_path / "data")
    spec = spec_for(tmp_path, wheel)
    installed = pool.ensure(spec)
    assert pool.ready_directory(installed.key) == installed


def test_readme_is_not_mistaken_for_an_import_and_abi_is_checked(tmp_path: Path) -> None:
    wheel = make_wheel(tmp_path / "source", members={"README.txt": b"docs"})
    pool = PluginEnvironmentPool(tmp_path / "data")
    installed = pool.ensure(spec_for(tmp_path, wheel))
    assert "README" not in installed.packages[0].imports
    incompatible = make_wheel(tmp_path / "bad", tag="cp39-cp39-win32")
    with pytest.raises(PluginDependencyError, match="缺少离线"):
        pool.ensure(spec_for(tmp_path, incompatible))


@pytest.mark.parametrize(
    "system,machine,target",
    [("Windows", "AMD64", "windows-x86_64"), ("Linux", "aarch64", "linux-arm64"), ("Darwin", "arm64", "darwin-arm64")],
)
def test_target_follows_host_platform_and_interpreter(
    monkeypatch: pytest.MonkeyPatch, system: str, machine: str, target: str
) -> None:
    monkeypatch.setattr("platform.system", lambda: system)
    monkeypatch.setattr("platform.machine", lambda: machine)
    assert plugin_target() == f"{target}-cp{sys.version_info.major}{sys.version_info.minor}"
