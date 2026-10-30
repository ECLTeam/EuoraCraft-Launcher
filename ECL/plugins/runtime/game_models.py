# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：严格校验第三方游戏查询、安装及进程控制请求，拒绝本机路径与任意启动参数。
#
# 公开接口：
#   - class GameCatalogRequest / GameLoadersRequest — 固定来源的有界版本查询。
#   - class GameInstallRequest — 获准根目录内的安装请求。
#   - class GameLaunchOptions / GameLaunchRequest — 有限启动选项和具体实例引用。
#   - class GameRunningRequest / GameStopRequest — 自身进程查询与停止。
# ============================================================

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, StrictBool, StrictInt, field_validator, model_validator

from ECL.plugins.runtime.models import InstanceGetRequest, RequestModel


class GameCatalogRequest(RequestModel):
    """
    查询正式版或快照；游标只对当前查询和运行有效。
    """

    version_type: Literal["release", "snapshot"] | None = Field(default=None, alias="type")
    cursor: str | None = Field(default=None, max_length=128)
    limit: StrictInt = Field(default=50, ge=1, le=100)


class GameLoadersRequest(RequestModel):
    """
    只查询受支持加载器的版本，不接收下载源或网络地址。
    """

    game_version: str = Field(min_length=1, max_length=64)
    loader: Literal["fabric", "forge", "neoforge", "quilt"]
    cursor: str | None = Field(default=None, max_length=128)
    limit: StrictInt = Field(default=50, ge=1, le=100)


class GameInstallRequest(RequestModel):
    """
    通过运行引用选择安装根目录，实例名称必须在所有支持平台上安全。
    """

    root_ref: str = Field(pattern=r"^[0-9a-f]{32}$")
    version_id: str = Field(min_length=1, max_length=64)
    version_name: str | None = Field(default=None, min_length=1, max_length=64)
    loader: Literal["vanilla", "fabric", "forge", "neoforge", "quilt"] = "vanilla"
    loader_version: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("version_id", "version_name")
    @classmethod
    def safe_version_name(cls, value: str | None) -> str | None:
        """
        拒绝路径、ADS、设备名及跨平台会被归一化为其他对象的名称。

        :param value: 版本标识或新实例目录名称
        :return: 保持原样的合法名称
        :raises ValueError: 不能安全作为单个目录组件时抛出
        """
        if value is None:
            return None
        reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)), *(f"LPT{n}" for n in range(1, 10))}
        reserved.update({"CONIN$", "CONOUT$", *(f"COM{n}" for n in "¹²³"), *(f"LPT{n}" for n in "¹²³")})
        if (
            value != value.strip()
            or value in {".", ".."}
            or value.endswith(".")
            or any(ord(character) < 32 or character in '<>:"/\\|?*' for character in value)
            or value.split(".", 1)[0].upper() in reserved
        ):
            raise ValueError("实例名称不是安全的目录组件")
        return value

    @model_validator(mode="after")
    def loader_options(self) -> Self:
        """
        原版安装不能夹带加载器版本，避免忽略有业务意义的输入。

        :return: 已验证请求
        :raises ValueError: 原版请求包含加载器参数时抛出
        """
        if self.loader == "vanilla" and self.loader_version is not None:
            raise ValueError("原版不接收加载器版本")
        return self


class GameLaunchOptions(RequestModel):
    """
    仅开放内存和窗口配置；Java由宿主选择，不接受租约、命令或环境变量。
    """

    memory_mb: StrictInt = Field(default=4096, ge=512, le=65536)
    width: StrictInt = Field(default=854, ge=320, le=16384)
    height: StrictInt = Field(default=480, ge=240, le=16384)
    fullscreen: StrictBool = False


class GameLaunchRequest(InstanceGetRequest):
    """
    只启动当前运行获准的具体实例。
    """

    options: GameLaunchOptions = Field(default_factory=GameLaunchOptions)


class GameRunningRequest(InstanceGetRequest):
    """
    只查询同一实例中本运行创建的进程记录。
    """


class GameStopRequest(RequestModel):
    """
    只停止当前运行创建并持有引用的确切游戏进程。
    """

    game_run_ref: str = Field(pattern=r"^[0-9a-f]{32}$")
