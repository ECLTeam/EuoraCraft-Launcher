# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：broker 路由注册表的行为契约。锁定改造前的落点等价性、
#   未知方法的显式失败与注册表的顺序无关性。
# ============================================================

from __future__ import annotations

import random

import pytest

from ECL.plugins.api.contracts import ProtocolError
from ECL.plugins.api.facades import PluginFacadeCatalog
from ECL.plugins.runtime.broker import PluginHostBroker

# 改造前前缀分支链的落点基准，键为命名空间前缀，值为服务实现名。
_expected_prefix_targets = {
    "game.": "_game_service",
    "mods.": "_game_service",
    "resourcepacks.": "_game_service",
    "shaders.": "_game_service",
    "modpacks.": "_game_service",
    "files.": "_query_service",
    "java.": "_query_service",
    "accounts.": "_query_service",
    "config.": "_query_service",
    "panels.": "_panel_service",
    "ui.": "_ui_service",
    "plugins.": "_cross_plugin_service",
    "hooks.": "_hook_service",
    "crash.": "_crash_service",
    "compat.": "_compat_service",
    "tools.": "_tool_service",
    "auth.": "_auth_service",
}

# 改造前显式方法（含控制面）的落点基准。
_expected_method_targets = {
    "operations.get": "_query_service",
    "operations.list": "_query_service",
    "operations.cancel": "_query_service",
    "resources.list": "_query_service",
    "instances.scan": "_query_service",
    "instances.get": "_query_service",
    "instances.profile_patch": "_query_service",
    "network.request": "_query_service",
    "downloads.submit": "_query_service",
    "runtime.info": "_control_service",
    "mutations.get": "_control_service",
    "commands.register": "_control_service",
    "commands.unregister": "_control_service",
    "commands.call": "_control_service",
    "settings.define": "_control_service",
    "settings.get": "_control_service",
    "settings.set": "_control_service",
    "events.subscribe": "_control_service",
    "events.unsubscribe": "_control_service",
}


def test_route_targets_match_prefactor_dispatch_chain() -> None:
    """
    注册表落点与改造前的分支链逐条一致，不允许路由行为漂移。
    """
    assert dict(PluginHostBroker.prefix_routes) == _expected_prefix_targets
    assert dict(PluginHostBroker.method_routes) == _expected_method_targets


def test_route_targets_are_existing_handlers() -> None:
    """
    注册表值必须是 broker 上真实存在的可调用实现，防止改名后悬空。
    """
    for target in {*PluginHostBroker.prefix_routes.values(), *PluginHostBroker.method_routes.values()}:
        assert callable(getattr(PluginHostBroker, target, None)), target


def test_catalog_methods_resolve_to_declared_handler() -> None:
    """
    门面目录的每个方法都解析到其命名空间对应的实现，无 404。
    """
    for method in PluginFacadeCatalog.facade_by_method:
        resolved = PluginHostBroker._resolve_handler(method)
        assert resolved is not None, method
        prefix = method.split(".", 1)[0] + "."
        expected = _expected_prefix_targets.get(prefix) or _expected_method_targets.get(method)
        assert resolved == expected, (method, resolved)


def test_unknown_methods_fail_without_falling_into_control_service() -> None:
    """
    落在任何命名空间之外的方法在入口即解析失败，不进入控制服务；
    命名空间内的未知子方法由委托实现按自身契约拒绝，不在入口拦截。
    """
    for method in ("foo.bar", "no_such_namespace.call", "runtimeinfo"):
        assert PluginHostBroker._resolve_handler(method) is None, method


def test_resolution_is_independent_of_registration_order() -> None:
    """
    注册表条目顺序打乱后，全目录方法的解析结果不变。
    """
    original = dict(PluginHostBroker.prefix_routes)
    for _attempt in range(5):
        shuffled = list(original.items())
        random.shuffle(shuffled)
        PluginHostBroker.prefix_routes = type(PluginHostBroker.prefix_routes)(dict(shuffled))
        try:
            for method in PluginFacadeCatalog.facade_by_method:
                assert PluginHostBroker._resolve_handler(method) is not None, method
        finally:
            PluginHostBroker.prefix_routes = type(PluginHostBroker.prefix_routes)(original)


def test_dispatch_raises_method_not_found_for_unknown_method() -> None:
    """
    _dispatch 对未知方法抛出的错误码与改造前兜底尾行一致。
    """
    broker = PluginHostBroker.__new__(PluginHostBroker)
    request = type("_Request", (), {"method": "no.such_method"})()
    with pytest.raises(ProtocolError) as error:
        broker._dispatch(request)  # type: ignore[arg-type]
    assert error.value.error_code == "PLUGIN_API_METHOD_NOT_FOUND"
