# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：前端能力 API：向前端发出事件、弹窗等宿主侧调用。
#
# 公开接口：
#   - class FrontendApi — 聚合正式 IPC 域处理器，并共享唯一的前端事件桥接状态。
# ============================================================

from ECL.api.accounts import AccountHandlers
from ECL.api.bridge import (
    ImagePolicy,
    _Emitter,
    _FrontendState,
    _guess_image_extension,
    _normalize_image_url,
)
from ECL.api.connector import ConnectorHandlers
from ECL.api.files import FileHandlers
from ECL.api.game import GameHandlers
from ECL.api.java import JavaHandlers
from ECL.api.mods import ModHandlers
from ECL.api.plugins import PluginHandlers
from ECL.api.settings import SettingsHandlers
from ECL.api.system import SystemHandlers
from ECL.api.windows import WindowHandlers
from ECL.api.workspace import WorkspaceHandlers


class FrontendApi(
    ConnectorHandlers,
    WorkspaceHandlers,
    PluginHandlers,
    ModHandlers,
    FileHandlers,
    AccountHandlers,
    GameHandlers,
    SettingsHandlers,
    JavaHandlers,
    SystemHandlers,
    WindowHandlers,
):
    """
    聚合正式 IPC 域处理器，并共享唯一的前端事件桥接状态。

    此对象只向 PyTauri 注册命令；命令调用的业务操作由后端服务执行。
    """


__all__ = [
    "FrontendApi",
    "ImagePolicy",
    "_Emitter",
    "_FrontendState",
    "_guess_image_extension",
    "_normalize_image_url",
]
