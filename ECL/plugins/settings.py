# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：统一维护第三方插件的设置定义、并发版本和原子持久化，不授予插件业务权限。
#
# 公开接口：
#   - class PluginSettingsService — 按安装身份管理设置及当前运行的变更通知。
# ============================================================

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ECL.plugin_api.contracts import JsonValue, ProtocolError, _json_value
from ECL.plugins.environment_pool import _exclusive_lock
from ECL.plugins.runtime.models import SettingDefinition
from ECL.utils import atomic_write_text


class _SettingsSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    definitions: list[SettingDefinition] = Field(max_length=64)


@dataclass(frozen=True, slots=True)
class _RunningSettings:
    schema: _SettingsSchema
    notify: Callable[[dict[str, JsonValue], int], None]


class PluginSettingsService:
    """
    以安装为唯一配置归属，协调 Worker 和可信管理界面的写入。

    用户值沿用既有文件；schema 单独发布，失败或禁用不会删除用户配置。
    """

    def __init__(self, data_path: Path, *, changed: Callable[[str], None] | None = None) -> None:
        """
        绑定应用私有目录，不读取或初始化用户配置。

        :param data_path: 宿主分配的数据根目录
        :param changed: 可信管理界面的值失效通知，不携带配置内容
        """
        self._data_path = data_path.resolve()
        self._changed = changed or (lambda installation_id: None)
        self._lock = RLock()
        self._locks_by_installation: dict[str, RLock] = {}
        self._runs: dict[tuple[str, str], _RunningSettings] = {}

    def _installation_lock(self, installation_id: str) -> RLock:
        if re.fullmatch(r"[0-9a-f]{32}", installation_id) is None:
            raise ProtocolError("插件安装身份无效", "PLUGIN_RESOURCE_NOT_FOUND")
        with self._lock:
            return self._locks_by_installation.setdefault(installation_id, RLock())

    def _file_path(self, installation_id: str, *, schema: bool = False) -> Path:
        """
        只解析宿主生成的安装标识，拒绝链接祖先和越界配置文件。
        """
        directory = "plugin_settings_schemas" if schema else "plugin_settings_v2"
        target_path = self._data_path / directory / f"{installation_id}.json"
        if any(
            item.is_symlink() or not item.resolve().is_relative_to(self._data_path)
            for item in (target_path.parent, target_path)
        ):
            raise ProtocolError("插件设置存储路径无效", "PLUGIN_STORAGE_FAILED")
        return target_path

    def define(
        self,
        installation_id: str,
        content_digest: str,
        run_id: str,
        definitions: list[SettingDefinition],
        notify: Callable[[dict[str, JsonValue], int], None],
        accept: Callable[[Callable[[], None]], None],
    ) -> str:
        """
        登记当前运行的完整定义，成功加载后再单独发布持久 schema。

        :param installation_id: 已验证安装身份
        :param content_digest: 本运行代码及授权内容身份
        :param run_id: 宿主运行身份
        :param definitions: 已校验的完整设置定义
        :param notify: 本运行自己的设置通知入口
        :param accept: 在配置锁内执行的授权接受边界
        :return: 绑定内容和定义的版本摘要
        :raises ProtocolError: 定义重复或超过容量时抛出
        """
        if len(definitions) > 64 or len({item.key for item in definitions}) != len(definitions):
            raise ProtocolError("设置定义重复或超过上限", "PLUGIN_SCHEMA_INVALID")
        payload = json.dumps(
            [item.model_dump(mode="json") for item in definitions], sort_keys=True, separators=(",", ":")
        )
        revision = hashlib.sha256(f"{content_digest}:{payload}".encode()).hexdigest()
        record = _SettingsSchema(content_digest=content_digest, schema_revision=revision, definitions=list(definitions))
        with self._installation_lock(installation_id):

            def register() -> None:
                with self._lock:
                    self._runs[(installation_id, run_id)] = _RunningSettings(record, notify)

            accept(register)
        return revision

    def publish(self, installation_id: str, run_id: str) -> None:
        """
        在运行加载成功后发布 schema，配置值不随代码重载重建。

        :param installation_id: 安装身份
        :param run_id: 已加载的运行
        :raises ProtocolError: schema 无法原子保存时抛出
        """
        with self._installation_lock(installation_id):
            running = self._runs.get((installation_id, run_id))
            if running is None:
                return
            try:
                atomic_write_text(self._file_path(installation_id, schema=True), running.schema.model_dump_json())
            except OSError as exc:
                raise ProtocolError("插件设置定义无法保存", "PLUGIN_STORAGE_FAILED") from exc

    def release(self, installation_id: str, run_id: str) -> None:
        """
        移除实际运行通知，不删除已发布定义和用户值。

        :param installation_id: 安装身份
        :param run_id: 要移除的运行
        """
        with self._lock:
            self._runs.pop((installation_id, run_id), None)

    def _schema(self, installation_id: str, content_digest: str, run_id: str | None) -> _SettingsSchema | None:
        """
        运行只使用自己登记的定义，可信界面可读取匹配当前内容的已发布定义。
        """
        if run_id is not None:
            with self._lock:
                running = self._runs.get((installation_id, run_id))
            return running.schema if running and running.schema.content_digest == content_digest else None
        schema_path = self._file_path(installation_id, schema=True)
        if not schema_path.exists():
            return None
        try:
            if schema_path.stat().st_size > 65536:
                raise ValueError("设置定义超过上限")
            record = _SettingsSchema.model_validate_json(schema_path.read_bytes())
            payload = json.dumps(
                [item.model_dump(mode="json") for item in record.definitions], sort_keys=True, separators=(",", ":")
            )
            expected = hashlib.sha256(f"{record.content_digest}:{payload}".encode()).hexdigest()
            if record.schema_revision != expected or len({item.key for item in record.definitions}) != len(
                record.definitions
            ):
                raise ValueError("设置定义摘要无效或重复")
            return record if record.content_digest == content_digest else None
        except (OSError, ValueError, ValidationError) as exc:
            raise ProtocolError("插件设置定义无法读取", "PLUGIN_STORAGE_FAILED") from exc

    def definitions(self, installation_id: str, content_digest: str, run_id: str) -> dict[str, SettingDefinition]:
        """
        返回当前运行已登记定义的独立映射，供面板校验使用。

        :param installation_id: 安装身份
        :param content_digest: 当前内容身份
        :param run_id: 当前运行
        :return: 设置键到只读定义的映射副本
        """
        with self._installation_lock(installation_id):
            schema = self._schema(installation_id, content_digest, run_id)
            return {item.key: item for item in schema.definitions} if schema else {}

    def _read(self, installation_id: str) -> tuple[dict[str, JsonValue], int]:
        """
        区分首次运行和损坏值文件，保留所有尚未显示的旧键。
        """
        target_path = self._file_path(installation_id)
        if not target_path.exists():
            return {}, 0
        try:
            if target_path.stat().st_size > 65536:
                raise ValueError("设置文件超过上限")
            record = _json_value(json.loads(target_path.read_bytes()))
            if (
                not isinstance(record, dict)
                or set(record) != {"schema_version", "revision", "values"}
                or type(record["schema_version"]) is not int
                or record["schema_version"] != 1
                or type(record["revision"]) is not int
                or record["revision"] < 0
                or not isinstance(record["values"], dict)
            ):
                raise ValueError("设置文件格式无效")
            return record["values"], record["revision"]
        except (OSError, ValueError) as exc:
            raise ProtocolError("插件设置文件无法读取", "PLUGIN_STORAGE_FAILED") from exc

    def view(self, installation_id: str, content_digest: str, run_id: str | None = None) -> dict[str, JsonValue]:
        """
        读取统一定义、值和版本；未知定义不可编辑，也不执行插件探测。

        :param installation_id: 安装身份
        :param content_digest: 当前内容身份
        :param run_id: Worker 自读时必须传入自身运行，管理入口省略
        :return: 设置视图及可编辑状态
        :raises ProtocolError: 存储损坏或当前定义与持久值冲突时抛出
        """
        with self._installation_lock(installation_id):
            schema = self._schema(installation_id, content_digest, run_id)
            stored, revision = self._read(installation_id)
            values: dict[str, JsonValue] = {}
            for definition in schema.definitions if schema else []:
                value = stored.get(definition.key, definition.default)
                if not definition.accepts(value):
                    raise ProtocolError("持久化设置不符合当前定义", "PLUGIN_SETTING_TYPE_MISMATCH")
                values[definition.key] = value
            return {
                "installation_id": installation_id,
                "content_digest": content_digest,
                "schema_revision": schema.schema_revision if schema else None,
                "schema": [_json_value(item.model_dump(mode="json")) for item in schema.definitions] if schema else [],
                "values": values,
                "readonly_files": self._selected_files(installation_id, content_digest),
                "revision": revision,
                "editable": schema is not None,
                "reason": None if schema else "PLUGIN_SETTING_SCHEMA_UNAVAILABLE",
            }

    def _selection_path(self, installation_id: str) -> Path:
        """
        选择记录由宿主独立维护，不从 Worker 的设置定义推导授权。
        """
        self._installation_lock(installation_id)
        target = self._data_path / "plugin_file_selections" / f"{installation_id}.json"
        if any(
            item.is_symlink() or not item.resolve().is_relative_to(self._data_path) for item in (target.parent, target)
        ):
            raise ProtocolError("文件选择存储路径无效", "PLUGIN_STORAGE_FAILED")
        return target

    def _selected_files(self, installation_id: str, content_digest: str) -> dict[str, str]:
        """
        只恢复由可信管理界面写入并绑定当前内容的文件选择。
        """
        target = self._selection_path(installation_id)
        if not target.exists():
            return {}
        try:
            if target.stat().st_size > 65536:
                raise ValueError("文件选择超过上限")
            record = json.loads(target.read_bytes())
            if not isinstance(record, dict) or set(record) != {"content_digest", "values"}:
                raise ValueError("文件选择结构无效")
            values = record["values"]
            if (
                not isinstance(values, dict)
                or len(values) > 64
                or any(not isinstance(key, str) or not isinstance(value, str) for key, value in values.items())
            ):
                raise ValueError("文件选择内容无效")
            return values if record["content_digest"] == content_digest else {}
        except (OSError, ValueError) as exc:
            raise ProtocolError("文件选择无法读取", "PLUGIN_STORAGE_FAILED") from exc

    def _write_values_and_selections(
        self, target_path: Path, candidate: str, installation_id: str, content_digest: str, selections: dict[str, str]
    ) -> None:
        """
        文件选择和普通设置一起提交；设置写入失败则恢复旧选择记录。
        """
        if not selections:
            atomic_write_text(target_path, candidate)
            return
        selection_path = self._selection_path(installation_id)
        previous = selection_path.read_text(encoding="utf-8") if selection_path.exists() else None
        values = self._selected_files(installation_id, content_digest) | selections
        atomic_write_text(selection_path, json.dumps({"content_digest": content_digest, "values": values}))
        try:
            atomic_write_text(target_path, candidate)
        except OSError:
            if previous is None:
                selection_path.unlink(missing_ok=True)
            else:
                atomic_write_text(selection_path, previous)
            raise

    def update(
        self,
        installation_id: str,
        content_digest: str,
        patch: dict[str, JsonValue],
        expected_revision: int,
        accept: Callable[[], None],
        *,
        run_id: str | None = None,
        schema_revision: str | None = None,
    ) -> int:
        """
        在统一文件锁内校验定义与并发版本，接受后原子保存，再向有效运行通知。

        :param installation_id: 安装身份
        :param content_digest: 调用方看到的内容身份
        :param patch: 明确提交的设置键值
        :param expected_revision: 读取时的值版本
        :param accept: 提交前的运行授权或可信管理资格复核
        :param run_id: Worker 必须指定自身运行
        :param schema_revision: 可信界面必须提供读取时的定义版本
        :return: 实际持久化的新版本
        :raises ProtocolError: 版本冲突、类型无效或保存失败时抛出
        """
        with self._installation_lock(installation_id):
            schema = self._schema(installation_id, content_digest, run_id)
            if schema is None or (schema_revision is not None and schema_revision != schema.schema_revision):
                raise ProtocolError("插件设置定义已经变化", "PLUGIN_SETTING_SCHEMA_CHANGED")
            definitions = {item.key: item for item in schema.definitions}
            if run_id is not None and any(
                key in definitions and definitions[key].type == "readonly_file" for key in patch
            ):
                raise ProtocolError("外部文件只能由用户在插件设置中选择", "PLUGIN_PERMISSION_DENIED")
            if not patch or any(
                key not in definitions or not definitions[key].accepts(value) for key, value in patch.items()
            ):
                raise ProtocolError("设置值不符合定义", "PLUGIN_SETTING_TYPE_MISMATCH")
            target_path = self._file_path(installation_id)
            try:
                with _exclusive_lock(target_path.with_suffix(".lock"), wait_seconds=2):
                    values, revision = self._read(installation_id)
                    if type(expected_revision) is not int or revision != expected_revision:
                        raise ProtocolError("设置已由其他操作修改", "PLUGIN_REVISION_CONFLICT")
                    candidate = json.dumps(
                        {"schema_version": 1, "revision": revision + 1, "values": values | patch}, ensure_ascii=False
                    )
                    if len(candidate.encode("utf-8")) > 65536:
                        raise ProtocolError("插件设置超过上限", "PLUGIN_SCHEMA_INVALID")
                    accept()
                    selections = {
                        key: str(value)
                        for key, value in patch.items()
                        if run_id is None and definitions[key].type == "readonly_file"
                    }
                    self._write_values_and_selections(
                        target_path, candidate, installation_id, content_digest, selections
                    )
            except OSError as exc:
                raise ProtocolError("插件设置保存失败", "PLUGIN_STORAGE_FAILED") from exc
            with self._lock:
                notifications = [item.notify for (owner, _), item in self._runs.items() if owner == installation_id]
        for notify in notifications:
            notify(patch, revision + 1)
        self._changed(installation_id)
        return revision + 1
