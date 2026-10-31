# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：定义最小 Worker 宿主接口的严格输入和基础字段约束。
#
# 公开接口：
#   - class FileRef / FileStatRequest / FileListRequest / FileReadRequest — 私有文件引用与读取请求。
#   - class FileWriteBeginRequest / FileWriteRef / FileWriteChunkRequest / FileWriteCommitRequest — 新建事务请求。
#   - class FileDeleteRequest — 按明确版本删除普通私有文件。
#   - class FileReplaceBeginRequest — 明确选择原版本的替换上传请求。
#   - class CommandDefinition — 命令注册契约。
#   - class SettingDefinition — 非执行式设置定义。
#   - class ResourceListRequest / InstanceScanRequest / InstanceGetRequest — 实例只读输入契约。
#   - class InstanceProfilePatchRequest — 绑定完整资料版本的四字段任务请求。
#   - class OperationGetRequest / OperationListRequest — 自身任务控制输入契约。
#   - validate_fields — 校验命令输入输出的基础字段。
#   - class DownloadSubmitRequest — 私有空间新文件下载任务请求。
#   - class NetworkRequest — GET/HEAD 只读网络请求的严格边界。
#   - class JavaInventoryRequest / JavaValidateRequest / JavaCatalogRequest — Java 查询与校验输入。
# ============================================================

from __future__ import annotations

import math
import re
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator, model_validator
from pydantic import JsonValue as PydanticJsonValue

from ECL.plugin_api.contracts import JsonValue, ProtocolError
from ECL.plugins.network_targets import NetworkOrigin


class RequestModel(BaseModel):
    """
    拒绝额外字段和隐式字符串、布尔、数值转换的边界模型。
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class NetworkRequest(RequestModel):
    """
    拒绝请求体、任意头和超限输入；凭据只允许显式提供给 HTTPS。
    """

    method: Literal["GET", "HEAD"]
    url: str
    headers: dict[str, str] = Field(default_factory=dict, max_length=16)
    allowed_headers: ClassVar[frozenset[str]] = frozenset(
        {"accept", "if-none-match", "if-modified-since", "user-agent", "authorization", "x-api-key"}
    )

    @model_validator(mode="after")
    def _validate_request(self) -> NetworkRequest:
        origin, _ = NetworkOrigin.from_url(self.url)
        names: set[str] = set()
        size = 0
        for name, value in self.headers.items():
            lowered = name.lower()
            if (
                len(name) > 64
                or re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name) is None
                or lowered not in self.allowed_headers
                or lowered in names
                or len(value) > 1024
                or any(not 32 <= ord(char) <= 126 for char in value)
            ):
                raise ValueError("网络请求头无效")
            if lowered in {"authorization", "x-api-key"} and origin.scheme != "https":
                raise ValueError("认证头仅允许 HTTPS")
            names.add(lowered)
            size += len(name.encode()) + len(value.encode())
        if size > 4096:
            raise ValueError("网络请求头超过上限")
        return self


class FieldSpec(RequestModel):
    """
    描述单个基础 JSON 字段，不接受可执行验证器或任意引用。
    """

    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
    type: Literal["string", "number", "integer", "bool"]
    required: StrictBool = True
    max_length: StrictInt = Field(default=4096, ge=1, le=16384)
    minimum: float | None = Field(default=None, allow_inf_nan=False)
    maximum: float | None = Field(default=None, allow_inf_nan=False)
    enum: list[str | int | float | bool] = Field(default_factory=list, max_length=64)

    def accepts(self, value: JsonValue) -> bool:
        """
        检查值的真实类型和已声明范围，不把布尔值当作整数。

        :param value: 经 JSON 校验的字段值
        :return: 是否满足字段约束
        """
        if self.enum and not any(type(item) is type(value) and item == value for item in self.enum):
            return False
        if self.type == "string":
            return isinstance(value, str) and len(value) <= self.max_length
        if self.type == "bool":
            return type(value) is bool
        if type(value) not in ({int} if self.type == "integer" else {int, float}):
            return False
        assert isinstance(value, int | float)
        return (
            math.isfinite(value)
            and (self.minimum is None or value >= self.minimum)
            and (self.maximum is None or value <= self.maximum)
        )


class CommandDefinition(RequestModel):
    """
    只登记本运行回调与基础输入输出，不开放跨插件调用。
    """

    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
    description: str = Field(default="", max_length=1000)
    input_fields: list[FieldSpec] = Field(default_factory=list, max_length=32)
    output_fields: list[FieldSpec] = Field(default_factory=list, max_length=32)
    callback_ref: str = Field(pattern=r"^[0-9a-f]{32}$")
    exported: StrictBool = False

    @field_validator("input_fields", "output_fields")
    @classmethod
    def unique_fields(cls, fields: list[FieldSpec]) -> list[FieldSpec]:
        """
        拒绝覆盖同名字段的模糊 schema。

        :param fields: 基础字段定义
        :return: 无重复的定义
        :raises ValueError: 名称重复时抛出
        """
        if len({item.name for item in fields}) != len(fields):
            raise ValueError("命令字段名称重复")
        return fields


class SettingDefinition(RequestModel):
    """
    定义自身设置的值类型和默认值，不接受可执行内容。
    """

    key: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
    type: Literal["bool", "string", "number", "select"]
    default: str | int | float | bool
    description: str = Field(default="", max_length=1000)
    options: list[str] = Field(default_factory=list, max_length=64)

    def accepts(self, value: JsonValue) -> bool:
        """
        校验持久化与运行时设置值，拒绝不同基础类型间的隐式转换。

        :param value: 待提交设置值
        :return: 是否符合本项定义
        """
        if self.type == "bool":
            return type(value) is bool
        if self.type == "number":
            return type(value) in {int, float} and math.isfinite(value)
        if self.type == "select":
            return isinstance(value, str) and value in self.options
        return isinstance(value, str) and len(value) <= 16384

    @model_validator(mode="after")
    def valid_default(self) -> SettingDefinition:
        """
        定义提交时即校验默认值，避免损坏 schema 进入持久化服务。

        :return: 已校验设置定义
        :raises ValueError: 默认值或选项无效时抛出
        """
        if not self.accepts(self.default) or len(set(self.options)) != len(self.options):
            raise ValueError("设置默认值或选项无效")
        return self


class PanelTextNode(RequestModel):
    """
    面板纯文本段落，不解析 Markdown、HTML 或任何模板。
    """

    type: Literal["text"]
    text: str = Field(min_length=1, max_length=2000)
    tone: Literal["default", "muted", "warning"] = "default"


class PanelGroupNode(RequestModel):
    """
    带可选标题的分组容器，子节点与父节点受同一总上限约束。
    """

    type: Literal["group"]
    title: str = Field(default="", max_length=120)
    children: list[PanelNode] = Field(min_length=1, max_length=16)


class PanelButtonNode(RequestModel):
    """
    按钮只指向本插件已登记命令，不接受回调标识或宿主入口。
    """

    type: Literal["button"]
    label: str = Field(min_length=1, max_length=60)
    command: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
    args: dict[str, str | int | float | bool] = Field(default_factory=dict, max_length=16)
    tone: Literal["default", "primary", "danger"] = "default"
    confirm: str = Field(default="", max_length=200)


class PanelSettingNode(RequestModel):
    """
    设置控件复用插件自己的设置定义，键在注册时与当前 schema 核对。
    """

    type: Literal["setting"]
    key: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")


class PanelColumn(RequestModel):
    """
    列表列的键与显示标题，不携带格式化表达式或渲染指令。
    """

    key: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
    label: str = Field(min_length=1, max_length=60)


class PanelListNode(RequestModel):
    """
    表格节点，行数据只来自面板数据回调的同名列表。
    """

    type: Literal["list"]
    source: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
    columns: list[PanelColumn] = Field(min_length=1, max_length=8)
    empty_text: str = Field(default="", max_length=200)

    @field_validator("columns")
    @classmethod
    def unique_columns(cls, columns: list[PanelColumn]) -> list[PanelColumn]:
        """
        拒绝同名列造成的渲染歧义。

        :param columns: 列定义
        :return: 无重复键的列定义
        :raises ValueError: 键重复时抛出
        """
        if len({item.key for item in columns}) != len(columns):
            raise ValueError("面板列表列名重复")
        return columns


class PanelTaskNode(RequestModel):
    """
    任务进度节点，进度只来自面板数据回调的同名任务。
    """

    type: Literal["task"]
    source: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")


PanelNode = Annotated[
    PanelTextNode | PanelGroupNode | PanelButtonNode | PanelSettingNode | PanelListNode | PanelTaskNode,
    Field(discriminator="type"),
]
PanelGroupNode.model_rebuild()


def _count_panel_nodes(nodes: list[PanelNode]) -> int:
    """
    统计节点树总数，用于在注册前拒绝超限结构。

    :param nodes: 当前层节点
    :return: 含子节点的总数
    """
    total = 0
    for node in nodes:
        total += 1
        if isinstance(node, PanelGroupNode):
            total += _count_panel_nodes(node.children)
    return total


def _panel_node_depth(nodes: list[PanelNode]) -> int:
    """
    计算节点树深度，分组嵌套层数受固定上限约束。

    :param nodes: 当前层节点
    :return: 最大嵌套层数，没有分组时为 1
    """
    deepest = 1
    for node in nodes:
        if isinstance(node, PanelGroupNode):
            deepest = max(deepest, 1 + _panel_node_depth(node.children))
    return deepest


class PanelDefinition(RequestModel):
    """
    声明式面板的完整描述，节点树有界且不含可执行内容。
    """

    key: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    nodes: list[PanelNode] = Field(min_length=1, max_length=48)
    data_callback_ref: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")

    @model_validator(mode="after")
    def bounded_tree(self) -> PanelDefinition:
        """
        在注册入口拒绝超限节点树，避免渲染期才发现结构不可用。

        :return: 已校验面板定义
        :raises ValueError: 节点总数或嵌套深度超过上限时抛出
        """
        if _count_panel_nodes(self.nodes) > 48 or _panel_node_depth(self.nodes) > 3:
            raise ValueError("面板节点数量或嵌套深度超过上限")
        return self


class PanelTaskProgress(RequestModel):
    """
    单个任务进度的只读快照，状态与百分比由插件声明。
    """

    state: Literal["pending", "running", "succeeded", "failed"]
    percent: StrictInt = Field(default=0, ge=0, le=100)
    detail: str = Field(default="", max_length=200)


class PanelData(RequestModel):
    """
    面板列表与任务进度的只读数据，按请求拉取，不做后台推送。
    """

    lists: dict[str, list[dict[str, str]]] = Field(default_factory=dict, max_length=8)
    tasks: dict[str, PanelTaskProgress] = Field(default_factory=dict, max_length=16)

    @field_validator("lists")
    @classmethod
    def bounded_lists(cls, lists: dict[str, list[dict[str, str]]]) -> dict[str, list[dict[str, str]]]:
        """
        拒绝超行数、超列数或超长单元格的数据，避免渲染与传输失控。

        :param lists: 按名称分组的行数据
        :return: 已校验行数据
        :raises ValueError: 行数、列数或单元格长度超限时抛出
        """
        for rows in lists.values():
            if len(rows) > 100:
                raise ValueError("面板列表行数超过上限")
            for row in rows:
                if len(row) > 8 or any(len(key) > 63 or len(value) > 200 for key, value in row.items()):
                    raise ValueError("面板列表列数或单元格长度超过上限")
        return lists


def validate_fields(fields: list[FieldSpec], values: dict[str, JsonValue]) -> None:
    """
    校验完整命令载荷，拒绝额外字段、缺少必要字段或值不符合约束。

    :param fields: 注册时通过校验的字段定义
    :param values: 本次调用或回调结果
    :raises ProtocolError: 载荷不符合定义时抛出
    """
    names = {item.name for item in fields}
    if set(values) - names or any(
        (item.required and item.name not in values) or (item.name in values and not item.accepts(values[item.name]))
        for item in fields
    ):
        raise ProtocolError("插件命令字段不符合定义", "PLUGIN_SCHEMA_INVALID")


class JavaInstallPlanRequest(RequestModel):
    """
    仅接受当前运行通过官方目录获得的包引用。
    """

    package_ref: str = Field(pattern=r"^[0-9a-f]{32}$")


class JavaInstallRequest(RequestModel):
    """
    仅接受当前运行已准备的 Java 安装计划。
    """

    plan_ref: str = Field(pattern=r"^[0-9a-f]{32}$")


class ResourceListRequest(RequestModel):
    """
    仅发现本运行获准的实例资源，不接受其他能力或宿主路径。
    """

    capability: Literal["plugin.base", "instances.read", "instances.manage", "java.read", "accounts.read"]
    cursor: str | None = Field(default=None, max_length=128)
    limit: StrictInt = Field(default=50, ge=1, le=100)


class InstanceScanRequest(RequestModel):
    """
    通过当前运行目录引用扫描，分页条件在游标中绑定。
    """

    root_ref: str = Field(pattern=r"^[0-9a-f]{32}$")
    force: StrictBool = False
    cursor: str | None = Field(default=None, max_length=128)
    limit: StrictInt = Field(default=50, ge=1, le=100)


class InstanceGetRequest(RequestModel):
    """
    只接受本运行实例引用，不接受名称或路径替代。
    """

    instance_ref: str = Field(pattern=r"^[0-9a-f]{32}$")


class InstanceProfilePatchRequest(InstanceGetRequest):
    """
    仅接收当前运行引用、完整资料版本与JSON增量，领域模型继续严格校验四字段。
    """

    expected_profile_etag: str = Field(pattern=r"^[0-9a-f]{64}$")
    patch: dict[str, PydanticJsonValue] = Field(min_length=1, max_length=4)


class OperationGetRequest(RequestModel):
    """
    只接受当前运行任务引用，拒绝主体、路径及内部任务标识。
    """

    operation_ref: str = Field(pattern=r"^[0-9a-f]{32}$")


class OperationListRequest(RequestModel):
    """
    限制自身任务分页大小，不允许客户端指定其他运行或筛选主体。
    """

    cursor: str | None = Field(default=None, max_length=128)
    limit: StrictInt = Field(default=50, ge=1, le=100)


class FileRef(RequestModel):
    """
    私有根中的单个可移植文件名，空名称仅供根 stat/list 使用。
    """

    root_ref: str = Field(pattern=r"^[0-9a-f]{32}$")
    relative_path: str = Field(max_length=120, pattern=r"^(?:[A-Za-z0-9][A-Za-z0-9._-]{0,119})?$")
    reserved_names: ClassVar[frozenset[str]] = frozenset(
        {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    )

    @field_validator("relative_path")
    @classmethod
    def valid_name(cls, value: str) -> str:
        """
        拒绝 Windows 别名，不自动净化或转换用户名称。

        :param value: 已经通过 ASCII 语法检查的组件
        :return: 原始名称
        """
        if value.endswith(".") or value.split(".", 1)[0].upper() in cls.reserved_names:
            raise ValueError("文件名称不能使用设备名或尾点")
        return value


class FileStatRequest(RequestModel):
    """
    查询自身根或普通文件，不接受实际宿主路径。
    """

    target: FileRef


class FileListRequest(RequestModel):
    """
    对当前 run 的根进行有界分页，不开放任意子目录。
    """

    directory: FileRef
    cursor: str | None = Field(default=None, max_length=128)
    limit: StrictInt = Field(default=50, ge=1, le=100)


class FileDeleteRequest(FileStatRequest):
    """
    要求明确版本的平铺文件删除，不接受根目录或可选的版本条件。
    """

    expected_etag: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("target")
    @classmethod
    def named_file(cls, value: FileRef) -> FileRef:
        """
        拒绝根目录删除，保持原始严格引用。

        :param value: 已验证的文件引用
        :return: 非空文件名称引用
        """
        if not value.relative_path:
            raise ValueError("只能删除命名文件")
        return value


class FileReadRequest(FileStatRequest):
    """
    读取有限字节，原始块和 Base64 编码共同受业务消息大小约束。
    """

    offset: StrictInt = Field(ge=0, le=2**53 - 1)
    count: StrictInt = Field(ge=1, le=32768)
    expected_etag: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class FileWriteBeginRequest(FileStatRequest):
    """
    明确声明有限大小且禁止覆盖，不接受尚未实现的目标版本替换。
    """

    total_bytes: StrictInt = Field(ge=0, le=8388608)
    overwrite: StrictBool

    @field_validator("overwrite")
    @classmethod
    def no_overwrite(cls, value: bool) -> bool:
        """
        只接纳显式 false，不将覆盖请求悄悄降级。

        :param value: 严格布尔参数
        :return: False
        """
        if value:
            raise ValueError("私有文件首轮不能覆盖")
        return value


class FileWriteRef(RequestModel):
    """
    当前 run 的上传引用，不接受任务标识或其他 owner。
    """

    write_ref: str = Field(pattern=r"^[0-9a-f]{32}$")


class FileReplaceBeginRequest(FileDeleteRequest):
    """
    显式选择原版本并声明新大小，不支持缺失目标时新建或无条件覆盖。
    """

    total_bytes: StrictInt = Field(ge=0, le=8388608)


class FileWriteChunkRequest(FileWriteRef):
    """
    限制编码前后大小并要求顺序写入。
    """

    offset: StrictInt = Field(ge=0, le=8388608)
    data_b64: str = Field(min_length=4, max_length=43692)


class FileWriteCommitRequest(FileWriteRef):
    """
    提交前必须核对完整字节摘要，不接受缺省或其他算法。
    """

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class DownloadSubmitRequest(FileStatRequest):
    """
    仅允许已批准公网站点到私有平铺新文件，拒绝覆盖与自定义 HTTP 参数。
    """

    url: str
    overwrite: StrictBool
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_download(self) -> DownloadSubmitRequest:
        NetworkOrigin.from_url(self.url)
        if self.overwrite or not self.target.relative_path:
            raise ValueError("下载只允许新建命名文件")
        if "expected_sha256" in self.model_fields_set and self.expected_sha256 is None:
            raise ValueError("摘要不得显式为空")
        return self


class JavaInventoryRequest(RequestModel):
    """
    仅分页已授权 Java，不接受扫描或强制刷新参数。
    """

    cursor: str | None = Field(default=None, max_length=128)
    limit: StrictInt = Field(default=50, ge=1, le=100)


class JavaValidateRequest(RequestModel):
    """
    仅校验当前运行引用，最低主版本不接受空值或伪整数。
    """

    runtime_ref: str = Field(pattern=r"^[0-9a-f]{32}$")
    required_major: StrictInt | None = Field(default=None, ge=1, le=99)

    @model_validator(mode="after")
    def validate_major(self) -> JavaValidateRequest:
        """
        区分未指定最低版本和显式非法空值。

        :return: 完成严格校验的请求
        """
        if "required_major" in self.model_fields_set and self.required_major is None:
            raise ValueError("最低主版本不能为 null")
        return self


class JavaCatalogRequest(JavaInventoryRequest):
    """
    查询固定来源的单个主版本与运行时类型。
    """

    major_version: StrictInt | None = Field(default=None, ge=1, le=99)
    runtime_kind: Literal["JRE", "JDK"] = "JRE"

    @model_validator(mode="after")
    def validate_major(self) -> JavaCatalogRequest:
        """
        显式 null 不代替推荐版本选择。

        :return: 完成校验的请求
        """
        if "major_version" in self.model_fields_set and self.major_version is None:
            raise ValueError("主版本不能为 null")
        return self
