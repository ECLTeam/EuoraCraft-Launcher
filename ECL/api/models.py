# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：IPC 请求/响应数据模型：Pydantic 模型与枚举定义。
#
# 公开接口：
#   - class RequestModel
#   - class DownloadSource
#   - class LoaderType
#   - class WardrobeKind
#   - class SkinModel
#   - class ImagePurpose
#   - class InstanceIconType
#   - class FileSelectionPurpose
#   - class FileSavePurpose
#   - class WindowOpenRequest
#   - class WindowLabelRequest
#   - class WindowBoundsRequest
#   - class SettingsQuery
#       - validate_section(value) -> str | None
#       - validate_sections(value) -> list[str] | None
#   - class SettingsUpdate
#   - class FrontendLogLevel
#   - class FrontendLogRequest
#   - class ProcessInputRequest
#   - class ProcessStopRequest
#   - class DebugProcessSpawnRequest
#   - class GameCatalogRequest
#   - class LoaderCatalogRequest
#   - class GameScanRequest
#   - class JavaScanRequest
#   - class GamePathRequest
#   - class GameConfigUpdate
#   - class GameConfigPatch
#   - class GameUninstallRequest
#   - class GameVersionRequest
#   - class GameVersionSettingsUpdate
#   - class InstanceProfilePatchData
#       - validate_tags(value) -> list[str] | None
#   - class InstanceProfilePatchRequest
#   - class InstanceProfileResetRequest
#   - class InstanceIconRequest
#       - validate_icon_input() -> Any
#   - class InstancePinEntry
#   - class InstancePinOrderRequest
#   - class InstanceCategoryUpsertRequest
#   - class InstanceCategoryDeleteRequest
#   - class GameInstanceRequest
#   - class CrashAnalyzeRequest
#       - validate_file_path(value) -> Any
#   - class CrashReportRequest
#   - class CrashExportRequest
#       - validate_output_path(value) -> Any
#   - class InstallRequest
#   - class WorldQuickTarget
#   - class ServerQuickTarget
#   - class LaunchRequest
#   - class InstanceTarget
#       - validate_version_id(value) -> str
#   - class InstanceFolderRequest
#   - class InstanceCloneRequest
#   - class InstancePackImportRequest
#   - class InstancePackExportRequest
#   - class ModpackOnlineInstallRequest
#   - class OperationRequest
#   - class WorldRequest
#   - class WorldPatchData
#   - class WorldPatchRequest
#   - class WorldCopyRequest
#   - class WorldTransferRequest
#   - class WorldIconRequest
#   - class WorldImportRequest
#   - class WorldBackupRequest
#   - class OptionsPatchRequest
#   - class ScreenshotRequest
#   - class ScreenshotThumbnailRequest
#   - class ScreenshotSaveRequest
#   - class ServerUpsertRequest
#   - class ServerIdRequest
#   - class ServerOrderRequest
#   - class ServerStatusRequest
#   - class ResourceQuery
#   - class ResourceInstallRequest
#   - class ResourceToggleRequest
#   - class SchematicPreviewRequest
#   - class SchematicAssetsRequest
#   - class SchematicMaterialManifestRequest
#   - class SchematicSessionChunksRequest
#   - class SchematicSessionCloseRequest
#   - class ResourceDeleteRequest
#   - class ResourceManifestExportRequest
#   - class ResourceSearchRequest
#   - class ResourceHashRequest
#   - class ResourceUpdateCheckRequest
#   - class ResourceUpdateRequest
#   - class WardrobeImportRequest
#       - validate_path(value) -> Any
#       - validate_model() -> Any
#   - class WardrobeItemRequest
#   - class WardrobeUpdateRequest
#   - class WardrobeApplySkinRequest
#   - class AccountTextureRequest
#   - class MicrosoftCapeRequest
#   - class ImageSelectionRequest
#   - class FileSelectionRequest
#   - class FileSaveRequest
#       - validate_default_directory(value) -> str | None
#       - validate_default_name(value) -> str | None
#   - class PortRequest — 指定端口号的请求体。
#   - class PortsRequest — 候选端口列表请求体。
#   - class RoomCodeRequest — 房间码请求体。
#   - class KickRequest — 踢出玩家请求体。
#   - request_schemas() -> dict[str, dict] — 返回前端集成所需的请求模型 JSON Schema 文档。
# ============================================================

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, JsonValue, field_validator, model_validator

from ECL.utils.config import default_config


def _validate_safe_path(value: Any) -> Any:
    # 拒绝包含 NUL 字符的路径字符串。
    if isinstance(value, str) and "\0" in value:
        raise ValueError("路径包含非法字符")
    return value


def _validate_non_empty_path(value: Any, message: str) -> Any:
    # 拒绝空白或包含 NUL 字符的路径字符串。
    if isinstance(value, str) and (not value.strip() or "\0" in value):
        raise ValueError(message)
    return value


SafePath = Annotated[Path, BeforeValidator(_validate_safe_path)]


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class DownloadSource(StrEnum):
    OFFICIAL = "official"
    BMCLAPI = "bmclapi"


class LoaderType(StrEnum):
    FABRIC = "fabric"
    FORGE = "forge"
    NEOFORGE = "neoforge"
    QUILT = "quilt"


class WardrobeKind(StrEnum):
    SKIN = "skin"
    CAPE = "cape"


class SkinModel(StrEnum):
    CLASSIC = "classic"
    SLIM = "slim"


class ImagePurpose(StrEnum):
    BACKGROUND = "background"
    SKIN = "skin"
    CAPE = "cape"
    INSTANCE_ICON = "instance_icon"


class InstanceIconType(StrEnum):
    AUTO = "auto"
    BUILTIN = "builtin"
    LOADER = "loader"
    LOCAL = "local"


class FileSelectionPurpose(StrEnum):
    CRASH_ANALYSIS = "crash-analysis"
    RESOURCE_FILES = "resource-files"
    MODPACK = "modpack"
    PLUGIN_PACKAGE = "plugin-package"
    PLUGIN_RUNTIME_PACK = "plugin-runtime-pack"
    WORLD_IMPORT = "world-import"


class FileSavePurpose(StrEnum):
    CRASH_REPORT = "crash-report"
    LAUNCHER_LOGS = "launcher-logs"
    WORLD_EXPORT = "world-export"
    INSTANCE_EXPORT = "instance-export"
    RESOURCE_MANIFEST = "resource-manifest"
    SCHEMATIC_MATERIAL_MANIFEST = "schematic-material-manifest"
    SCREENSHOT = "screenshot"
    MOD_FILE = "mod-file"


class WindowOpenRequest(RequestModel):
    descriptor_id: str = Field(min_length=1, max_length=120, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
    session_id: str | None = Field(default=None, max_length=64, pattern=r"^[a-zA-Z0-9._-]+$")
    instance_key: str | None = Field(default=None, max_length=80, pattern=r"^[a-zA-Z0-9._-]+$")


class WindowLabelRequest(RequestModel):
    label: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_:/.-]+$")


class WindowBoundsRequest(WindowLabelRequest):
    x: int | None = Field(default=None, ge=-100000, le=100000)
    y: int | None = Field(default=None, ge=-100000, le=100000)
    width: int = Field(ge=320, le=7680)
    height: int = Field(ge=240, le=4320)


class SettingsQuery(RequestModel):
    section: str | None = None
    sections: list[str] | None = None

    @field_validator("section")
    @classmethod
    def validate_section(cls, value: str | None) -> str | None:
        if value == "":
            raise ValueError("配置分区名称不能为空")
        return value

    @field_validator("sections")
    @classmethod
    def validate_sections(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and any(not section for section in value):
            raise ValueError("配置分区名称不能为空")
        return value


class SettingsUpdate(RequestModel):
    section: str = Field(min_length=1)
    data: JsonValue


class FrontendLogLevel(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    WARN = "warn"
    ERROR = "error"
    CRITICAL = "critical"


class FrontendLogRequest(RequestModel):
    level: FrontendLogLevel
    message: str = Field(min_length=1, max_length=20000)
    detail: str | None = Field(default=None, max_length=100000)
    logger: str | None = Field(default=None, max_length=200)


class ProcessInputRequest(RequestModel):
    instance_id: str = Field(min_length=1)
    data: str = Field(max_length=100000)


class ProcessStopRequest(RequestModel):
    instance_id: str = Field(min_length=1)


class DebugProcessSpawnRequest(RequestModel):
    name: str = Field(min_length=1, max_length=200)
    type: str = Field(min_length=1, max_length=200)
    args: list[str] = Field(min_length=1, max_length=100)
    cwd: str | None = Field(default=None, max_length=2048)
    stdin: bool = False


class GameCatalogRequest(RequestModel):
    filter_type: str | None = None
    classified: bool = False
    source: DownloadSource | None = None


class LoaderCatalogRequest(RequestModel):
    loader: LoaderType
    game_version: str = Field(min_length=1)
    source: DownloadSource | None = None


class GameScanRequest(RequestModel):
    paths: list[SafePath] | None = None
    force: bool = False


class JavaScanRequest(RequestModel):
    paths: list[Path] | None = None


class GamePathRequest(RequestModel):
    game_path: SafePath


class GameConfigUpdate(GamePathRequest):
    data: dict[str, JsonValue]


class GameConfigPatch(GamePathRequest):
    patch: dict[str, JsonValue]


class GameUninstallRequest(GamePathRequest):
    version_id: str = Field(min_length=1)


class GameVersionRequest(GamePathRequest):
    version_id: str = Field(min_length=1)


class GameVersionSettingsUpdate(GameVersionRequest):
    data: dict[str, JsonValue]


class InstanceProfilePatchData(RequestModel):
    alias: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    favorite: bool | None = None
    pinned: bool | None = None
    hidden: bool | None = None
    category_id: str | None = Field(default=None, alias="categoryId", min_length=1, max_length=64)
    tags: list[str] | None = Field(default=None, max_length=20)
    pin_order: int | None = Field(default=None, alias="pinOrder", ge=0)
    preferred_external_source: str | None = Field(
        default=None,
        alias="preferredExternalSource",
        pattern=r"^(auto|[a-z][a-z0-9_-]{1,63})$",
    )

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and any(not tag.strip() or len(tag.strip()) > 40 for tag in value):
            raise ValueError("标签不能为空且不能超过 40 个字符")
        return value


class InstanceProfilePatchRequest(GameVersionRequest):
    patch: InstanceProfilePatchData


class InstanceProfileResetRequest(GameVersionRequest):
    fields: list[str] = Field(min_length=1)


class InstanceIconRequest(GameVersionRequest):
    icon_type: InstanceIconType
    value: str | None = Field(default=None, max_length=80)
    source_path: Path | None = None

    @model_validator(mode="after")
    def validate_icon_input(self):
        if self.icon_type == InstanceIconType.LOCAL and self.source_path is None:
            raise ValueError("本地图标需要 source_path")
        if self.icon_type in {InstanceIconType.BUILTIN, InstanceIconType.LOADER} and not self.value:
            raise ValueError("内置或加载器图标需要 value")
        return self


class InstancePinEntry(RequestModel):
    game_path: Path
    version_id: str = Field(min_length=1)


class InstancePinOrderRequest(RequestModel):
    entries: list[InstancePinEntry]


class InstanceCategoryUpsertRequest(RequestModel):
    category_id: str | None = Field(default=None, min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=40)
    color: str = Field(pattern=r"^#[0-9a-fA-F]{3}([0-9a-fA-F]{3})?$")
    order: int = Field(default=50, ge=0, le=100000)


class InstanceCategoryDeleteRequest(RequestModel):
    category_id: str = Field(min_length=1, max_length=64)


class GameInstanceRequest(RequestModel):
    instance_id: str = Field(min_length=1)


class CrashAnalyzeRequest(GameVersionRequest):
    file_path: SafePath

    @field_validator("file_path", mode="before")
    @classmethod
    def validate_file_path(cls, value):
        return _validate_non_empty_path(value, "崩溃日志路径无效")


class CrashReportRequest(RequestModel):
    report_id: str = Field(min_length=1, pattern=r"^[a-f0-9]{32}$")


class CrashExportRequest(CrashReportRequest):
    output_path: SafePath | None = None

    @field_validator("output_path", mode="before")
    @classmethod
    def validate_output_path(cls, value):
        return _validate_non_empty_path(value, "导出路径无效")


class InstallRequest(RequestModel):
    version_id: str = Field(min_length=1)
    version_name: str | None = None
    loader_type: LoaderType | None = None
    loader_version: str | None = None
    fabric_api_version: str | None = None
    game_path: SafePath
    java_path: SafePath | None = None
    source: DownloadSource | None = None
    task_id: str | None = None


class WorldQuickTarget(RequestModel):
    type: Literal["world"]
    world_id: str = Field(min_length=1, max_length=255)


class ServerQuickTarget(RequestModel):
    type: Literal["server"]
    address: str = Field(min_length=1, max_length=255)


class LaunchRequest(RequestModel):
    version_id: str = Field(min_length=1)
    game_path: SafePath
    java_path: SafePath | None = None
    source: DownloadSource | None = None
    memory: int = Field(default=default_config["game"]["memory_size"], ge=256, le=131072)
    # 是否锁定 JVM 初始堆与最大堆一致（-Xms=-Xmx）。
    lock_memory: bool = False
    # 游戏进程优先级: idle / below_normal / normal / above_normal / high。
    process_priority: Literal["idle", "below_normal", "normal", "above_normal", "high"] = "normal"
    # ``None`` 表示调用方没有指定，由游戏全局设置提供兜底值。
    width: int | None = Field(default=None, ge=320, le=16384)
    height: int | None = Field(default=None, ge=240, le=16384)
    fullscreen: bool | None = None
    jvm_args: list[str] = Field(default_factory=list)
    game_args: list[str] = Field(default_factory=list)
    # None 表示由后端读取实例 .ecl/settings.json；显式布尔值仅用于调用方覆盖。
    version_isolation: bool | None = None
    quick_target: Annotated[WorldQuickTarget | ServerQuickTarget, Field(discriminator="type")] | None = None


class InstanceTarget(RequestModel):
    game_path: Path
    version_id: str = Field(min_length=1, max_length=255)
    # 不传时使用该实例保存的隔离设置，避免前端缺省值覆盖实例配置。
    version_isolation: bool | None = None

    @field_validator("version_id")
    @classmethod
    def validate_version_id(cls, value: str) -> str:
        if value in {".", ".."} or Path(value).name != value or any(char in value for char in ("/", "\\", "\0")):
            raise ValueError("实例 ID 格式无效")
        return value


class InstanceFolderRequest(InstanceTarget):
    folder: Literal["instance", "mods", "saves", "screenshots", "logs", "crash-reports"]


class InstanceModFileRequest(InstanceTarget):
    filename: str = Field(min_length=1, max_length=255)


class InstanceModAddRequest(InstanceTarget):
    source_path: Path


class InstanceCloneRequest(InstanceTarget):
    new_version_id: str = Field(min_length=1, max_length=255)


class InstancePackImportRequest(RequestModel):
    game_path: Path
    source_path: Path
    new_version_id: str = Field(min_length=1, max_length=255)


class InstancePackExportRequest(InstanceTarget):
    output_path: Path
    pack_format: Literal["modrinth"]


class ModpackOnlineInstallRequest(RequestModel):
    source: Literal["modrinth", "curseforge", "ftb"]
    project_id: str = Field(min_length=1, max_length=64)
    file_id: str = Field(min_length=1, max_length=64)
    game_path: Path
    new_version_id: str = Field(min_length=1, max_length=255)


class OperationRequest(RequestModel):
    operation_id: str = Field(min_length=32, max_length=64, pattern=r"^[a-f0-9]+$")


class WorldRequest(InstanceTarget):
    world_id: str = Field(min_length=1, max_length=255)


class WorldPatchData(RequestModel):
    difficulty: int | None = Field(default=None, ge=0, le=3)
    allow_commands: bool | None = Field(default=None, alias="allowCommands")
    difficulty_locked: bool | None = Field(default=None, alias="difficultyLocked")
    game_mode: int | None = Field(default=None, ge=0, le=3, alias="gameMode")
    raining: bool | None = None
    thundering: bool | None = None
    seed: int | None = None
    spawn: dict[str, int] | None = None


class WorldPatchRequest(WorldRequest):
    patch: WorldPatchData


class WorldCopyRequest(WorldRequest):
    new_world_id: str = Field(min_length=1, max_length=255)


class WorldTransferRequest(WorldRequest):
    output_path: Path


class WorldIconRequest(WorldRequest):
    source_path: Path


class WorldImportRequest(InstanceTarget):
    source_path: Path


class WorldBackupRequest(WorldRequest):
    backup_id: str | None = Field(default=None, max_length=80)
    locked: bool | None = None


class OptionsPatchRequest(InstanceTarget):
    patch: dict[str, Any]


class ScreenshotRequest(InstanceTarget):
    screenshot_id: str = Field(min_length=1, max_length=255)


class ScreenshotThumbnailRequest(ScreenshotRequest):
    size: int = Field(default=360, ge=64, le=1024)


class ScreenshotSaveRequest(ScreenshotRequest):
    output_path: Path


class ServerUpsertRequest(InstanceTarget):
    server_id: str | None = None
    name: str = Field(min_length=1, max_length=120)
    address: str = Field(min_length=1, max_length=255)
    favorite: bool = False


class ServerIdRequest(InstanceTarget):
    server_id: str = Field(min_length=1, max_length=20)


class ServerOrderRequest(InstanceTarget):
    server_ids: list[str]


class ServerStatusRequest(RequestModel):
    addresses: list[str] = Field(max_length=64)
    timeout: float = Field(default=3.0, ge=0.5, le=10.0)


class ResourceQuery(InstanceTarget):
    resource_type: Literal["mod", "resourcepack", "shaderpack", "datapack", "schematic"]
    world_id: str | None = Field(default=None, max_length=255)


class ResourceInstallRequest(ResourceQuery):
    source_paths: list[Path] = Field(min_length=1, max_length=200)


class ResourceToggleRequest(ResourceQuery):
    resource_id: str = Field(min_length=1, max_length=255)
    enabled: bool


class SchematicPreviewRequest(ResourceQuery):
    resource_type: Literal["schematic"]
    resource_id: str = Field(min_length=1, max_length=255)


class SchematicAssetsRequest(InstanceTarget):
    blocks: list[str] = Field(min_length=1, max_length=512)
    locale: Literal["zh-CN", "zh-TW", "en-US", "ja-JP", "ru-RU", "de-DE"] = "zh-CN"


class SchematicMaterialManifestRequest(InstanceTarget):
    """
    校验原理图材料审计清单的导出请求。

    输出路径由系统保存对话框选择；会话标识只能引用当前进程内的短期预览快照。
    """

    session_id: str = Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")
    output_path: Path
    output_format: Literal["json", "csv"]
    locale: Literal["zh-CN", "zh-TW", "en-US", "ja-JP", "ru-RU", "de-DE"] = "zh-CN"
    missing_blocks: list[str] = Field(default_factory=list, max_length=512)


class SchematicSessionChunksRequest(RequestModel):
    """
    校验一次原理图分块读取的会话和区块坐标。

    单次最多读取 24 个区块，避免 IPC 响应过大。
    """

    session_id: str = Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")
    coords: list[tuple[int, int, int]] = Field(min_length=1, max_length=24)


class SchematicSessionCloseRequest(RequestModel):
    """
    校验关闭原理图预览会话的标识。

    仅允许由打开接口生成的十六进制会话标识。
    """

    session_id: str = Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")


class ResourceDeleteRequest(ResourceQuery):
    resource_ids: list[str] = Field(min_length=1, max_length=200)


class ResourceManifestExportRequest(ResourceQuery):
    output_path: Path
    output_format: Literal["json", "csv"]


class ResourceSearchRequest(RequestModel):
    query: str = Field(min_length=1, max_length=120)
    game_version: str = Field(min_length=1, max_length=80)
    loader: str = Field(min_length=1, max_length=40)
    source: Literal["modrinth", "curseforge"] = "modrinth"
    limit: int = Field(default=20, ge=1, le=50)


class ResourceHashRequest(RequestModel):
    sha512: str = Field(min_length=128, max_length=128, pattern=r"^[a-fA-F0-9]+$")


class ResourceUpdateCheckRequest(ResourceQuery):
    game_version: str = Field(min_length=1, max_length=80)
    loader: str = Field(min_length=1, max_length=40)


class ResourceUpdateRequest(ResourceQuery):
    resource_id: str = Field(min_length=1, max_length=255)
    update: dict[str, JsonValue]


class WardrobeImportRequest(RequestModel):
    path: SafePath
    kind: WardrobeKind
    name: str | None = Field(default=None, max_length=80)
    model: SkinModel | None = None

    @field_validator("path", mode="before")
    @classmethod
    def validate_path(cls, value):
        return _validate_non_empty_path(value, "纹理路径无效")

    @model_validator(mode="after")
    def validate_model(self):
        if self.kind == WardrobeKind.CAPE and self.model is not None:
            raise ValueError("披风不能指定手臂模型")
        return self


class WardrobeItemRequest(RequestModel):
    item_id: str = Field(min_length=1)


class WardrobeUpdateRequest(WardrobeItemRequest):
    name: str | None = Field(default=None, max_length=80)
    model: SkinModel | None = None
    favorite: bool | None = None


class WardrobeApplySkinRequest(WardrobeItemRequest):
    account_id: str = Field(min_length=1)


class AccountTextureRequest(RequestModel):
    account_id: str = Field(min_length=1)


class MicrosoftCapeRequest(AccountTextureRequest):
    cape_id: str = Field(min_length=1)


class ImageSelectionRequest(RequestModel):
    purpose: ImagePurpose = ImagePurpose.BACKGROUND


class FileSelectionRequest(RequestModel):
    purpose: FileSelectionPurpose | None = None


class FileSaveRequest(RequestModel):
    purpose: FileSavePurpose
    default_directory: str | None = Field(default=None, max_length=4096)
    default_name: str | None = Field(default=None, max_length=255)

    @field_validator("default_directory")
    @classmethod
    def validate_default_directory(cls, value: str | None) -> str | None:
        if value is not None and "\0" in value:
            raise ValueError("默认目录不能包含空字符")
        return value

    @field_validator("default_name")
    @classmethod
    def validate_default_name(cls, value: str | None) -> str | None:
        if value is not None and ("\0" in value or Path(value).name != value or value in {".", ".."}):
            raise ValueError("默认文件名格式无效")
        return value


class PortRequest(RequestModel):
    """
    指定端口号的请求体。
    """

    port: int = Field(ge=1, le=65535)


class PortsRequest(RequestModel):
    """
    候选端口列表请求体。
    """

    ports: list[int] = Field(min_length=1, max_length=64)


class RoomCodeRequest(RequestModel):
    """
    房间码请求体。
    """

    code: str = Field(min_length=1, max_length=32)


class KickRequest(RequestModel):
    """
    踢出玩家请求体。
    """

    machine_id: str = Field(min_length=1, max_length=128)


class RequestModelRegistry:
    """
    保存 IPC 命令到请求模型的映射。
    """

    models: dict[str, type[RequestModel]] = {
        "settings_get": SettingsQuery,
        "settings_set": SettingsUpdate,
        "frontend_log": FrontendLogRequest,
        "window_open": WindowOpenRequest,
        "window_focus": WindowLabelRequest,
        "window_close": WindowLabelRequest,
        "window_update_bounds": WindowBoundsRequest,
        "game_versions": GameCatalogRequest,
        "game_loader_versions": LoaderCatalogRequest,
        "game_scan": GameScanRequest,
        "game_java_scan": JavaScanRequest,
        "game_install": InstallRequest,
        "game_launch": LaunchRequest,
        "game_uninstall": GameUninstallRequest,
        "game_config_get": GamePathRequest,
        "game_config_set": GameConfigUpdate,
        "game_config_patch": GameConfigPatch,
        "game_version_stats": GameVersionRequest,
        "game_version_settings_get": GameVersionRequest,
        "game_version_settings_set": GameVersionSettingsUpdate,
        "game_instance_profile_get": GameVersionRequest,
        "game_instance_profile_patch": InstanceProfilePatchRequest,
        "game_instance_profile_reset": InstanceProfileResetRequest,
        "game_instance_icon_set": InstanceIconRequest,
        "game_instance_pin_order_set": InstancePinOrderRequest,
        "game_instance_categories_upsert": InstanceCategoryUpsertRequest,
        "game_instance_categories_delete": InstanceCategoryDeleteRequest,
        "game_instance_stop": GameInstanceRequest,
        "game_crash_list": GameVersionRequest,
        "game_crash_analyze": CrashAnalyzeRequest,
        "game_crash_output": CrashReportRequest,
        "game_crash_export": CrashExportRequest,
        "wardrobe_import": WardrobeImportRequest,
        "wardrobe_sync_account_skin": AccountTextureRequest,
        "wardrobe_update": WardrobeUpdateRequest,
        "wardrobe_delete": WardrobeItemRequest,
        "wardrobe_texture": WardrobeItemRequest,
        "wardrobe_export": WardrobeItemRequest,
        "wardrobe_apply_skin": WardrobeApplySkinRequest,
        "accounts_texture_urls": AccountTextureRequest,
        "microsoft_reset_skin": AccountTextureRequest,
        "microsoft_set_cape": MicrosoftCapeRequest,
        "microsoft_reset_cape": AccountTextureRequest,
        "select_image": ImageSelectionRequest,
        "select_file": FileSelectionRequest,
        "select_save_file": FileSaveRequest,
        "connector_host_port": PortRequest,
        "connector_join": RoomCodeRequest,
        "connector_kick": KickRequest,
        "connector_search_mc_port": PortsRequest,
    }


def request_schemas() -> dict[str, dict]:
    """
    返回前端集成所需的请求模型 JSON Schema 文档。

    RequestModelRegistry.models 的键（IPC 命令名）必须是
    `ECL.api.registry.IpcCommandRegistry.command_names`
    中已注册的命令，否则抛错，防止请求模型与正式命令表脱钩。
    （此处延迟导入 registry 以避免模块级循环依赖：registry -> bridge -> models。）

    :return: 命令名到 JSON Schema 的映射
    :raises RuntimeError: 存在未在 registry 注册的命令名时抛出
    """
    from ECL.api.registry import IpcCommandRegistry  # 延迟导入，避免循环依赖

    unregistered = sorted(set(RequestModelRegistry.models) - set(IpcCommandRegistry.command_names))
    if unregistered:
        raise RuntimeError(
            "RequestModelRegistry.models 包含未在 IpcCommandRegistry.command_names 中注册的命令: "
            + ", ".join(unregistered)
        )
    return {command: model.model_json_schema() for command, model in RequestModelRegistry.models.items()}


__all__ = [
    "AccountTextureRequest",
    "CrashAnalyzeRequest",
    "CrashExportRequest",
    "CrashReportRequest",
    "FileSavePurpose",
    "FileSaveRequest",
    "FileSelectionPurpose",
    "FileSelectionRequest",
    "FrontendLogRequest",
    "GameCatalogRequest",
    "GameConfigPatch",
    "GameConfigUpdate",
    "GameInstanceRequest",
    "GamePathRequest",
    "GameScanRequest",
    "GameUninstallRequest",
    "GameVersionRequest",
    "GameVersionSettingsUpdate",
    "ImagePurpose",
    "ImageSelectionRequest",
    "InstallRequest",
    "InstanceCategoryDeleteRequest",
    "InstanceCategoryUpsertRequest",
    "InstanceIconRequest",
    "InstancePinOrderRequest",
    "InstanceProfilePatchRequest",
    "InstanceProfileResetRequest",
    "JavaScanRequest",
    "KickRequest",
    "LaunchRequest",
    "LoaderCatalogRequest",
    "MicrosoftCapeRequest",
    "PortRequest",
    "PortsRequest",
    "RequestModelRegistry",
    "RoomCodeRequest",
    "SettingsQuery",
    "SettingsUpdate",
    "SkinModel",
    "WardrobeApplySkinRequest",
    "WardrobeImportRequest",
    "WardrobeItemRequest",
    "WardrobeKind",
    "WardrobeUpdateRequest",
    "WindowBoundsRequest",
    "WindowLabelRequest",
    "WindowOpenRequest",
    "request_schemas",
]
