# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：插件宿主 API 门面目录。将 broker 可路由的方法按业务域
#   归入六个承诺门面与一个弱承诺扩展面，作为 ADR-005 版本契约的
#   挂载点与契约快照测试的单一来源。门面只承载 API 承诺分组，
#   不参与运行时路由（路由见 broker 的注册表）。
#
# 公开接口：
#   - class PluginFacadeCatalog — 门面与方法的双向映射、承诺集合与扩展面。
# ============================================================

from __future__ import annotations

from types import MappingProxyType
from typing import ClassVar


class PluginFacadeCatalog:
    """
    插件宿主方法的门面归属目录。

    ``method_by_facade`` 枚举的是具体方法名而非命名空间前缀：门面承诺
    以"可调用的方法"为单位，命名空间内部混入查询与变更（如 ``game.*``
    既有 ``game.launch`` 也有 ``game.catalog``），按前缀承诺会连带承诺
    尚不存在的方法。``facade_by_method`` 由 ``method_by_facade`` 推导，
    不手写两份。

    ``extensions`` 收录弱承诺面（tools/crash/compat/plugins）：只承诺
    "存在且不静默失败"，不承诺跨版本稳定，因此不入 ``promised_facades``。

    非方法标识（宿主到 Worker 的反向通道 ``callbacks.invoke``、权限标识
    ``plugin.base`` 与 ``java.base``、出站事件 ``settings.changed``）不是
    插件可调用方法，一律不入目录；契约测试对这三类有显式排除断言。
    """

    method_by_facade: ClassVar[MappingProxyType[str, frozenset[str]]] = MappingProxyType(
        {
            # 实例、档案与公开配置。
            "instances": frozenset(
                {
                    "instances.get",
                    "instances.scan",
                    "instances.profile_patch",
                    "config.public",
                }
            ),
            # 启动链路（含链路自身的查询），永不出内核。
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
            # 账户、登录与外部认证。
            "accounts": frozenset(
                {
                    "accounts.get",
                    "accounts.list",
                    "accounts.read",
                    "accounts.skin",
                    "auth.register",
                }
            ),
            # 内容安装、查询与来源。
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
            # 插件自身运行时能力（任务、事件、设置、回执、私有文件）。
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
            # 界面注入与协作（面板、自定义界面、命令、启动钩子）。
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
    )

    extensions: ClassVar[MappingProxyType[str, frozenset[str]]] = MappingProxyType(
        {
            # 弱承诺扩展面：只承诺存在且不静默失败，不承诺跨版本稳定。
            "tools": frozenset({"tools.register", "tools.run", "tools.stop"}),
            "crash": frozenset({"crash.register"}),
            "compat": frozenset({"compat.read_index", "compat.register"}),
            "plugins": frozenset({"plugins.call", "plugins.list"}),
        }
    )

    promised_facades: ClassVar[frozenset[str]] = frozenset(method_by_facade)

    facade_by_method: ClassVar[MappingProxyType[str, str]] = MappingProxyType(
        {method: facade for facade, methods in {**method_by_facade, **extensions}.items() for method in methods}
    )
