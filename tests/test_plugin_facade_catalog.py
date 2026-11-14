# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：插件门面目录契约快照。任何门面归属或方法面的变化都必须
#   在此显式更新，防止无意识扩面或缩面。
# ============================================================

from __future__ import annotations

import re

from ECL.plugins.api.facades import PluginFacadeCatalog

# broker 当前 _dispatch 的路由知识基线（批 2 落地后改为对路由表键集断言）。
_dispatch_prefixes = (
    "game.",
    "mods.",
    "resourcepacks.",
    "shaders.",
    "modpacks.",
    "files.",
    "java.",
    "accounts.",
    "config.",
    "panels.",
    "ui.",
    "plugins.",
    "hooks.",
    "crash.",
    "compat.",
    "tools.",
    "auth.",
)
_dispatch_explicit_methods = frozenset(
    {
        "operations.get",
        "operations.list",
        "operations.cancel",
        "resources.list",
        "instances.scan",
        "instances.get",
        "instances.profile_patch",
        "network.request",
        "downloads.submit",
        "runtime.info",
        "mutations.get",
        "commands.register",
        "commands.unregister",
        "commands.call",
        "settings.define",
        "settings.get",
        "settings.set",
        "events.subscribe",
        "events.unsubscribe",
    }
)

_promise_snapshot = {
    "instances": frozenset({"instances.get", "instances.scan", "instances.profile_patch", "config.public"}),
    "launch": frozenset(
        {
            "game.catalog",
            "game.install",
            "game.launch",
            "game.loaders",
            "game.running",
            "game.stop",
            "java.catalog",
            "java.install",
            "java.install_plan",
            "java.inventory",
            "java.read",
            "java.validate",
        }
    ),
    "accounts": frozenset({"accounts.get", "accounts.list", "accounts.read", "accounts.skin", "auth.register"}),
    "resources": frozenset(
        {
            "resources.list",
            "mods.delete",
            "mods.install",
            "mods.set_enabled",
            "resourcepacks.delete",
            "resourcepacks.install",
            "shaders.delete",
            "shaders.install",
            "modpacks.export",
            "modpacks.export_files",
            "modpacks.export_preview",
            "modpacks.get",
            "modpacks.install",
            "modpacks.prepare",
            "downloads.submit",
            "network.request",
        }
    ),
    "runtime": frozenset(
        {
            "operations.get",
            "operations.list",
            "operations.cancel",
            "events.subscribe",
            "events.unsubscribe",
            "settings.define",
            "settings.get",
            "settings.set",
            "mutations.get",
            "runtime.info",
            "files.delete",
            "files.read",
            "files.replace_begin",
            "files.write_abort",
            "files.write_begin",
            "files.write_chunk",
            "files.write_commit",
        }
    ),
    "ui": frozenset(
        {
            "panels.data",
            "panels.list",
            "panels.register",
            "ui.register_html",
            "ui.register_page",
            "ui.register_vue",
            "commands.register",
            "commands.unregister",
            "commands.call",
            "hooks.register",
        }
    ),
}

_extension_snapshot = {
    "tools": frozenset({"tools.register", "tools.run", "tools.stop"}),
    "crash": frozenset({"crash.register"}),
    "compat": frozenset({"compat.read_index", "compat.register"}),
    "plugins": frozenset({"plugins.call", "plugins.list"}),
}


def test_bidirectional_mapping_is_consistent() -> None:
    """
    facade_by_method 必须与两张目录展平后的方法全集完全一致，且无重复归属。
    """
    all_facade_entries = {**PluginFacadeCatalog.method_by_facade, **PluginFacadeCatalog.extensions}
    flattened = {method for methods in all_facade_entries.values() for method in methods}
    assert flattened == set(PluginFacadeCatalog.facade_by_method)
    assert sum(len(methods) for methods in all_facade_entries.values()) == len(flattened)


def test_non_method_identifiers_are_excluded() -> None:
    """
    反向通道、权限标识与出站事件不得混入目录；键必须是两段式方法名。
    """
    excluded = {"callbacks.invoke", "plugin.base", "java.base", "settings.changed"}
    assert not (set(PluginFacadeCatalog.facade_by_method) & excluded)
    pattern = re.compile(r"^[a-z][a-z_]*\.[a-z][a-z_]*$")
    assert all(pattern.fullmatch(method) for method in PluginFacadeCatalog.facade_by_method)


def test_promise_snapshot_is_explicit() -> None:
    """
    门面内容快照：目录与快照任何一边多出方法都必须显式更新两边。
    """
    assert dict(PluginFacadeCatalog.method_by_facade) == _promise_snapshot
    assert dict(PluginFacadeCatalog.extensions) == _extension_snapshot
    assert PluginFacadeCatalog.promised_facades == frozenset(_promise_snapshot)


def test_catalog_methods_are_routable_by_broker() -> None:
    """
    目录全集必须落在 broker 的路由知识内：要么命中前缀命名空间，
    要么是显式登记的方法。漏登记的方法会在批 2 后变成入口 404。
    """
    for method in PluginFacadeCatalog.facade_by_method:
        assert method.startswith(_dispatch_prefixes) or method in _dispatch_explicit_methods


def test_extension_methods_are_weakly_promised_only() -> None:
    """
    扩展面方法不得出现在承诺门面，扩展面名不得进入 promised_facades。
    """
    promised_methods = {method for methods in PluginFacadeCatalog.method_by_facade.values() for method in methods}
    extension_methods = {method for methods in PluginFacadeCatalog.extensions.values() for method in methods}
    assert not (promised_methods & extension_methods)
    assert not (frozenset(PluginFacadeCatalog.extensions) & PluginFacadeCatalog.promised_facades)
