# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：验证已准备的插件包，用原子活动指针切换独立 Worker 版本。
#
# 公开接口：
#   - class PluginPackageActivationError — 插件包激活或恢复失败。
#   - class PluginPackageActivation — 活动插件版本与 Worker 状态。
#   - class PluginPackageActivationStore — 激活、恢复和撤销已准备的插件包。
# ============================================================

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from threading import RLock

from ECL.plugins.environment_pool import PluginEnvironmentError, PluginEnvironmentPool, _exclusive_lock
from ECL.plugins.package_preparation import (
    PluginPreparationError,
    PluginPreparedPackage,
    current_plugin_target,
    verify_prepared_code,
)
from ECL.plugins.runtime_assets import PluginRuntimeAsset, PluginRuntimeError, PluginRuntimeStore
from ECL.plugins.worker_process import PluginWorkerError, PluginWorkerProcess
from ECL.utils import atomic_write_text, get_logger

plugin_name_pattern = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
digest_pattern = re.compile(r"[0-9a-f]{64}\Z")


class PluginPackageActivationError(RuntimeError):
    """
    表示活动指针、已准备代码或 Worker 验证未达到可激活条件。

    激活失败不会替换此前已提交的活动指针或运行中的旧 Worker。
    """


@dataclass(frozen=True, slots=True)
class PluginPackageActivation:
    """
    保存一个归档插件的当前活动版本及独立 Worker。

    禁用状态下仍保留活动指针，但不占用 Worker 进程。
    """

    name: str
    version: str
    manifest_sha256: str
    code_path: Path
    environment_key: str
    enabled: bool
    worker: PluginWorkerProcess | None


@dataclass(frozen=True, slots=True)
class _ActiveRecord:
    name: str
    version: str
    manifest_sha256: str
    environment_key: str
    runtime_id: str
    target_tag: str
    enabled: bool

    def to_json(self) -> str:
        return json.dumps(
            {
                "name": self.name,
                "version": self.version,
                "manifest_sha256": self.manifest_sha256,
                "environment_key": self.environment_key,
                "runtime_id": self.runtime_id,
                "target_tag": self.target_tag,
                "enabled": self.enabled,
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    @classmethod
    def from_path(cls, pointer_path: Path, expected_name: str) -> _ActiveRecord:
        if pointer_path.is_symlink():
            raise PluginPackageActivationError("插件活动指针不能是符号链接")
        try:
            payload = json.loads(pointer_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PluginPackageActivationError("插件活动指针无法读取") from exc
        if not isinstance(payload, dict) or set(payload) != {
            "name",
            "version",
            "manifest_sha256",
            "environment_key",
            "runtime_id",
            "target_tag",
            "enabled",
        }:
            raise PluginPackageActivationError("插件活动指针字段无效")
        if (
            payload["name"] != expected_name
            or not isinstance(payload["version"], str)
            or not isinstance(payload["manifest_sha256"], str)
            or digest_pattern.fullmatch(payload["manifest_sha256"]) is None
            or not isinstance(payload["environment_key"], str)
            or digest_pattern.fullmatch(payload["environment_key"]) is None
            or not isinstance(payload["runtime_id"], str)
            or not isinstance(payload["target_tag"], str)
            or not isinstance(payload["enabled"], bool)
        ):
            raise PluginPackageActivationError("插件活动指针内容无效")
        return cls(**payload)


class PluginPackageActivationStore:
    """
    管理已准备插件包的活动版本指针和独立 Worker。

    新 Worker 在写入活动指针前完成加载与启用验证；已提交的指针只引用
    已就绪的运行时、环境和逐文件复核过的代码。调用方须在后台线程使用此同步服务。
    """

    def __init__(self, data_path: Path, runtime_asset: PluginRuntimeAsset) -> None:
        """
        绑定数据目录和发行时固定摘要的插件运行时。

        :param data_path: 启动器持久化数据目录
        :param runtime_asset: 不可从插件包读取的固定运行时资产描述
        """
        self.data_path = Path(data_path)
        self.runtime_asset = runtime_asset
        self.environment_pool = PluginEnvironmentPool(self.data_path)
        self.runtime_store = PluginRuntimeStore(self.data_path)
        self._active_by_name: dict[str, PluginPackageActivation] = {}
        self._lock = RLock()
        self.logger = get_logger("PluginPackageActivation")

    def _pointer_path(self, name: str) -> Path:
        """
        校验插件名称后确定活动指针路径，避免持久状态逃出数据目录。
        """
        if not isinstance(name, str) or plugin_name_pattern.fullmatch(name) is None:
            raise PluginPackageActivationError("插件名包含非法字符")
        return self.data_path / "plugin_packages" / name / "active.json"

    def _code_path(self, name: str, manifest_sha256: str) -> Path:
        return self._pointer_path(name).parent / f"pkg-{manifest_sha256[:20]}"

    def _ready_worker_paths(self, environment_key: str) -> tuple[Path, Path]:
        """
        只接受已就绪的运行时和环境，恢复时绝不隐式下载或重建。
        """
        runtime_paths = self.runtime_store.ready_paths(self.runtime_asset)
        python_path = self.environment_pool.ready_python(environment_key)
        if runtime_paths is None or python_path is None:
            raise PluginPackageActivationError("插件运行时或依赖环境尚未就绪")
        return python_path, runtime_paths.worker_path

    def _verify_code(self, code_path: Path, record: _ActiveRecord) -> None:
        """
        复核版本目录和插件身份，防止活动指针指向其他归档载荷。
        """
        verify_prepared_code(code_path, record.manifest_sha256)
        try:
            metadata = json.loads((code_path / "plugin.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PluginPackageActivationError("插件活动版本清单无法读取") from exc
        if (
            not isinstance(metadata, dict)
            or metadata.get("name") != record.name
            or metadata.get("version", "0.0.0") != record.version
        ):
            raise PluginPackageActivationError("插件活动版本与指针不匹配")

    def _start_worker(
        self, python_path: Path, worker_path: Path, code_path: Path, enabled: bool
    ) -> PluginWorkerProcess:
        """
        在代码目录外创建插件工作目录，启动独立 Worker 并验证启用状态。
        """
        working_path = self.data_path / "plugin_data" / code_path.parent.name
        worker = PluginWorkerProcess(python_path, worker_path, code_path, working_path=working_path)
        worker.start()
        if enabled:
            worker.enable()
        return worker

    def _validated_previous_activation(self, name: str, pointer_path: Path) -> PluginPackageActivation | None:
        """
        确认磁盘指针仍引用本进程恢复的旧版本，拒绝跨进程覆盖。
        """
        old = self._active_by_name.get(name)
        if not pointer_path.exists() and not pointer_path.is_symlink():
            if old is not None:
                raise PluginPackageActivationError("插件活动指针已被其他进程删除")
            return None
        if old is None:
            raise PluginPackageActivationError("已有插件活动版本尚未恢复，不能覆盖")
        current = _ActiveRecord.from_path(pointer_path, name)
        if (
            current.manifest_sha256 != old.manifest_sha256
            or self._code_path(name, current.manifest_sha256) != old.code_path
            or current.version != old.version
            or current.environment_key != old.environment_key
            or current.enabled != old.enabled
            or current.runtime_id != self.runtime_asset.runtime_id
            or current.target_tag != current_plugin_target()
        ):
            raise PluginPackageActivationError("插件活动指针已被其他进程修改")
        return old

    def activate(self, prepared: PluginPreparedPackage, *, enabled: bool = True) -> PluginPackageActivation:
        """
        验证准备结果并切换活动指针，失败时保留旧 Worker 与旧指针。

        禁用插件只运行 `on_load` 验证后即关闭测试 Worker，不触发 `on_enable`。
        完成后历史代码和环境暂保留，待独立引用清理流程处理。

        :param prepared: 归档预检和环境创建得到的未激活结果
        :param enabled: 是否在提交前启用新 Worker
        :return: 新活动版本，禁用状态的 worker 为 None
        :raises PluginPackageActivationError: 代码、运行时或 Worker 验证失败时抛出
        """
        package = prepared.preflight.package
        record = _ActiveRecord(
            package.name,
            package.version,
            package.manifest_sha256,
            prepared.environment_key,
            self.runtime_asset.runtime_id,
            prepared.preflight.target_tag,
            enabled,
        )
        if record.target_tag != current_plugin_target():
            raise PluginPackageActivationError("插件准备结果不属于当前平台")
        pointer_path = self._pointer_path(package.name)
        code_path = self._code_path(package.name, package.manifest_sha256)
        with self._lock, _exclusive_lock(pointer_path.with_suffix(".lock")):
            old = self._validated_previous_activation(package.name, pointer_path)
            candidate: PluginWorkerProcess | None = None
            try:
                if prepared.code_path.resolve() != code_path.resolve():
                    raise PluginPackageActivationError("插件准备目录不在版本存储路径内")
                self._verify_code(code_path, record)
                python_path, worker_path = self._ready_worker_paths(record.environment_key)
                if prepared.python_path.resolve() != python_path.resolve():
                    raise PluginPackageActivationError("准备结果引用的环境与已就绪环境不一致")
                candidate = self._start_worker(python_path, worker_path, code_path, enabled)
                if not enabled:
                    candidate.close()
                    candidate = None
                pointer_path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_text(pointer_path, record.to_json())
            except (
                OSError,
                PluginEnvironmentError,
                PluginPreparationError,
                PluginRuntimeError,
                PluginWorkerError,
            ) as exc:
                if candidate is not None:
                    candidate.close()
                raise PluginPackageActivationError(f"插件激活失败: {exc}") from exc
            active = PluginPackageActivation(
                package.name,
                package.version,
                package.manifest_sha256,
                code_path,
                record.environment_key,
                enabled,
                candidate,
            )
            self._active_by_name[package.name] = active
            if old is not None and old.worker is not None:
                try:
                    old.worker.close()
                except OSError:
                    self.logger.exception("新版已提交，但旧插件 Worker 关闭失败: %s", package.name)
            return active

    def restore(self, name: str) -> PluginPackageActivation | None:
        """
        从原子活动指针恢复插件，不联网也不创建新的虚拟环境。

        无法复核的指针保持原样供诊断，不会把相应代码导入宿主进程。

        :param name: 已安装归档插件的名称
        :return: 已恢复活动版本；没有活动指针时返回 None
        :raises PluginPackageActivationError: 活动指针或 Worker 无法安全恢复时抛出
        """
        pointer_path = self._pointer_path(name)
        with self._lock, _exclusive_lock(pointer_path.with_suffix(".lock")):
            if name in self._active_by_name:
                return self._active_by_name[name]
            if not pointer_path.is_file():
                return None
            record = _ActiveRecord.from_path(pointer_path, name)
            if record.runtime_id != self.runtime_asset.runtime_id or record.target_tag != current_plugin_target():
                raise PluginPackageActivationError("插件活动版本需要其他平台或运行时")
            code_path = self._code_path(name, record.manifest_sha256)
            try:
                self._verify_code(code_path, record)
                python_path, worker_path = self._ready_worker_paths(record.environment_key)
                worker = self._start_worker(python_path, worker_path, code_path, True) if record.enabled else None
            except (
                OSError,
                PluginEnvironmentError,
                PluginPreparationError,
                PluginRuntimeError,
                PluginWorkerError,
            ) as exc:
                raise PluginPackageActivationError(f"插件恢复失败: {exc}") from exc
            active = PluginPackageActivation(
                name, record.version, record.manifest_sha256, code_path, record.environment_key, record.enabled, worker
            )
            self._active_by_name[name] = active
            return active

    def set_enabled(self, name: str, enabled: bool) -> PluginPackageActivation:
        """
        原子持久化归档插件启用状态，并在需要时切换 Worker。

        启用失败保留禁用指针；禁用先提交指针，再回收 Worker，避免重启时意外启用。

        :param name: 已恢复的归档插件名称
        :param enabled: 目标启用状态
        :return: 更新后的活动版本
        :raises PluginPackageActivationError: 指针、代码或 Worker 状态不一致时抛出
        """
        pointer_path = self._pointer_path(name)
        with self._lock, _exclusive_lock(pointer_path.with_suffix(".lock")):
            active = self._active_by_name.get(name)
            if active is None or not pointer_path.is_file():
                raise PluginPackageActivationError("归档插件尚未恢复")
            record = _ActiveRecord.from_path(pointer_path, name)
            if (
                record.enabled != active.enabled
                or record.manifest_sha256 != active.manifest_sha256
                or record.version != active.version
                or record.environment_key != active.environment_key
                or record.runtime_id != self.runtime_asset.runtime_id
                or record.target_tag != current_plugin_target()
            ):
                raise PluginPackageActivationError("归档插件活动状态已被其他进程修改")
            if active.enabled == enabled:
                return active
            candidate: PluginWorkerProcess | None = None
            try:
                if enabled:
                    self._verify_code(active.code_path, record)
                    python_path, worker_path = self._ready_worker_paths(record.environment_key)
                    candidate = self._start_worker(python_path, worker_path, active.code_path, True)
                atomic_write_text(pointer_path, replace(record, enabled=enabled).to_json())
            except (
                OSError,
                PluginEnvironmentError,
                PluginPreparationError,
                PluginRuntimeError,
                PluginWorkerError,
            ) as exc:
                if candidate is not None:
                    candidate.close()
                raise PluginPackageActivationError(f"归档插件状态切换失败: {exc}") from exc
            updated = replace(active, enabled=enabled, worker=candidate)
            self._active_by_name[name] = updated
            if active.worker is not None:
                try:
                    active.worker.close()
                except OSError:
                    self.logger.exception("归档插件已禁用，但 Worker 关闭失败: %s", name)
            return updated

    def reload(self, name: str) -> PluginPackageActivation:
        """
        在相同活动版本上验证新 Worker 后替换旧 Worker。

        失败时旧进程继续运行，不改变持久活动指针。

        :param name: 已启用的归档插件名称
        :return: 持有新 Worker 的活动版本
        :raises PluginPackageActivationError: 插件未启用或新 Worker 验证失败时抛出
        """
        pointer_path = self._pointer_path(name)
        with self._lock, _exclusive_lock(pointer_path.with_suffix(".lock")):
            active = self._active_by_name.get(name)
            if active is None or not active.enabled or active.worker is None:
                raise PluginPackageActivationError("归档插件未启用")
            record = _ActiveRecord.from_path(pointer_path, name)
            if (
                not record.enabled
                or record.manifest_sha256 != active.manifest_sha256
                or record.version != active.version
                or record.environment_key != active.environment_key
                or record.runtime_id != self.runtime_asset.runtime_id
                or record.target_tag != current_plugin_target()
            ):
                raise PluginPackageActivationError("归档插件活动状态已被其他进程修改")
            try:
                self._verify_code(active.code_path, record)
                python_path, worker_path = self._ready_worker_paths(record.environment_key)
                candidate = self._start_worker(python_path, worker_path, active.code_path, True)
            except (
                OSError,
                PluginEnvironmentError,
                PluginPreparationError,
                PluginRuntimeError,
                PluginWorkerError,
            ) as exc:
                raise PluginPackageActivationError(f"归档插件重载失败: {exc}") from exc
            updated = replace(active, worker=candidate)
            self._active_by_name[name] = updated
            try:
                active.worker.close()
            except OSError:
                self.logger.exception("归档插件已重载，但旧 Worker 关闭失败: %s", name)
            return updated

    def uninstall(self, name: str) -> bool:
        """
        删除活动指针并关闭该插件 Worker，不清理仍可能被引用的共享环境。

        :param name: 归档插件名称
        :return: 曾存在活动指针或 Worker 时返回 True
        :raises PluginPackageActivationError: 活动指针无法删除时抛出
        """
        pointer_path = self._pointer_path(name)
        with self._lock, _exclusive_lock(pointer_path.with_suffix(".lock")):
            had_pointer = pointer_path.exists()
            try:
                pointer_path.unlink(missing_ok=True)
            except OSError as exc:
                raise PluginPackageActivationError("插件活动指针无法删除") from exc
            active = self._active_by_name.pop(name, None)
            if active is not None and active.worker is not None:
                try:
                    active.worker.close()
                except OSError:
                    self.logger.exception("插件已卸载，但 Worker 关闭失败: %s", name)
            return had_pointer or active is not None

    def close(self) -> None:
        """
        有界关闭当前进程持有的 Worker，不删除持久活动指针。

        :return: 无
        """
        with self._lock:
            active_plugins = tuple(self._active_by_name.values())
            self._active_by_name.clear()
        for active in active_plugins:
            if active.worker is not None:
                try:
                    active.worker.close()
                except OSError:
                    self.logger.exception("关闭插件 Worker 失败: %s", active.name)
