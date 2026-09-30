# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：原子提交归档安装版本并迁移旧 Worker 指针，不创建或加载插件进程。
#
# 公开接口：
#   - class PluginPackageActivationError — 安装指针或恢复失败。
#   - class PluginPackageActivation — 磁盘安装版本及依赖目录。
#   - class PluginPackageActivationStore — 提交、恢复及移除安装资格。
# ============================================================

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path

from ECL.plugins.dependency_lock import PluginDependencyError, PluginDependencyLock, digest_pattern
from ECL.plugins.environment_pool import (
    PluginDependencyDirectory,
    PluginEnvironmentPool,
    PluginEnvironmentSpec,
    _exclusive_lock,
)
from ECL.plugins.package_preparation import (
    PluginPackagePreparer,
    PluginPreparedPackage,
    current_plugin_target,
    verify_prepared_code,
)
from ECL.utils import atomic_write_text

plugin_name_pattern = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


class PluginPackageActivationError(PluginDependencyError):
    """
    表示活动指针、安装文件或依赖目录不满足恢复条件。

    恢复不执行插件，失败时保留旧指针和旧运行时文件。
    """


@dataclass(frozen=True, slots=True)
class PluginPackageActivation:
    """
    保存磁盘上已提交的归档版本，不等同于本进程已经加载的版本。

    依赖目录可以共享，但不存在私有解释器或 Worker 实例。
    """

    name: str
    version: str
    manifest_sha256: str
    code_path: Path
    environment_key: str
    enabled: bool
    dependency_directory: PluginDependencyDirectory | None


class PluginPackageActivationStore:
    """
    管理不执行代码的归档安装指针，并在需要时离线迁移旧记录。

    同步文件操作由调用方安排到后台线程；写入使用文件锁及原子替换。
    """

    def __init__(self, data_path: Path) -> None:
        """
        绑定持久化数据目录，不要求任何额外运行时资产。

        :param data_path: 启动器可写数据目录
        """
        self.data_path = Path(data_path)
        self.environment_pool = PluginEnvironmentPool(self.data_path)

    def _pointer_path(self, name: str) -> Path:
        """
        校验名称与所有安装目录祖先，防止记录逃出数据目录。
        """
        if not isinstance(name, str) or plugin_name_pattern.fullmatch(name) is None:
            raise PluginPackageActivationError("插件名包含非法字符")
        root_path = self.data_path / "plugin_packages" / name
        pointer_path = root_path / "active.json"
        if (
            root_path.parent.is_symlink()
            or root_path.is_symlink()
            or pointer_path.is_symlink()
            or not root_path.resolve().is_relative_to(self.data_path.resolve())
        ):
            raise PluginPackageActivationError("插件活动指针路径越界或包含符号链接")
        return pointer_path

    def _payload(self, active: PluginPackageActivation) -> dict[str, object]:
        return {
            "record_version": 2,
            "install_mode": "host-directory",
            "name": active.name,
            "version": active.version,
            "manifest_sha256": active.manifest_sha256,
            "environment_key": active.environment_key,
            "target_tag": current_plugin_target(),
            "enabled": active.enabled,
        }

    def _write(self, active: PluginPackageActivation) -> None:
        atomic_write_text(
            self._pointer_path(active.name), json.dumps(self._payload(active), ensure_ascii=False, sort_keys=True)
        )

    def activate(self, prepared: PluginPreparedPackage, *, enabled: bool = True) -> PluginPackageActivation:
        """
        校验已准备结果并提交磁盘版本，不在安装时执行新插件。

        :param prepared: 已校验归档代码和依赖目录
        :param enabled: 下次启动时的启用意图
        :return: 已提交安装版本
        :raises PluginPackageActivationError: 目录、身份或依赖未准备好
        """
        info = prepared.preflight.package
        pointer_path = self._pointer_path(info.name)
        expected_path = pointer_path.parent / f"pkg-{info.manifest_sha256[:20]}"
        if prepared.code_path != expected_path:
            raise PluginPackageActivationError("已准备代码目录与安装指针不匹配")
        verify_prepared_code(expected_path, info.manifest_sha256)
        directory = (
            self.environment_pool.ready_directory(prepared.environment_key) if prepared.environment_key else None
        )
        if prepared.environment_key and directory is None:
            raise PluginPackageActivationError("插件依赖目录尚未就绪")
        active = PluginPackageActivation(
            info.name, info.version, info.manifest_sha256, expected_path, prepared.environment_key, enabled, directory
        )
        with _exclusive_lock(pointer_path.parent / "activation.lock"):
            if pointer_path.exists():
                atomic_write_text(pointer_path.parent / "previous.json", pointer_path.read_text(encoding="utf-8"))
            self._write(active)
        return active

    def restore(self, name: str) -> PluginPackageActivation | None:
        """
        复核安装代码和依赖，必要时离线迁移旧 Worker 记录。

        缺少离线 wheel 时保留旧记录并明确失败，不恢复旧 Worker。

        :param name: 已安装归档插件名
        :return: 可供宿主发现的安装版本；没有指针时返回 None
        :raises PluginPackageActivationError: 记录无效、目标不符或迁移失败
        """
        pointer_path = self._pointer_path(name)
        if not pointer_path.exists():
            return None
        with _exclusive_lock(pointer_path.parent / "activation.lock"):
            return self._restore_locked(name, pointer_path)

    def _restore_locked(self, name: str, pointer_path: Path) -> PluginPackageActivation:
        """
        在文件锁内读取并校验版本，迁移只在文件与依赖都就绪后提交。
        """
        try:
            raw = pointer_path.read_text(encoding="utf-8")
            payload = json.loads(raw)
            legacy = isinstance(payload, dict) and "runtime_id" in payload and "record_version" not in payload
            fields = {"name", "version", "manifest_sha256", "environment_key", "target_tag", "enabled"}
            if not isinstance(payload, dict) or set(payload) != fields | (
                {"runtime_id"} if legacy else {"record_version", "install_mode"}
            ):
                raise PluginPackageActivationError("插件活动指针字段无效")
            if not legacy and (
                type(payload["record_version"]) is not int
                or payload["record_version"] != 2
                or payload["install_mode"] != "host-directory"
            ):
                raise PluginPackageActivationError("插件活动指针记录版本无效")
            if (
                payload["name"] != name
                or (legacy and not isinstance(payload["runtime_id"], str))
                or not isinstance(payload["version"], str)
                or not isinstance(payload["manifest_sha256"], str)
                or digest_pattern.fullmatch(payload["manifest_sha256"]) is None
                or not isinstance(payload["environment_key"], str)
                or (payload["environment_key"] and digest_pattern.fullmatch(payload["environment_key"]) is None)
                or type(payload["enabled"]) is not bool
                or payload["target_tag"] != current_plugin_target()
            ):
                raise PluginPackageActivationError("插件活动指针内容或目标无效")
            code_path = pointer_path.parent / f"pkg-{payload['manifest_sha256'][:20]}"
            verify_prepared_code(code_path, payload["manifest_sha256"])
            metadata = json.loads((code_path / "plugin.json").read_text(encoding="utf-8"))
            if metadata.get("name") != name or metadata.get("version", "0.0.0") != payload["version"]:
                raise PluginPackageActivationError("插件活动版本与指针不匹配")
            key = payload["environment_key"]
            if legacy:
                directory = PluginPackagePreparer(self.data_path).prepare_dependencies(
                    code_path, tuple(metadata.get("pythonDependencies", []))
                )
                key = directory.key if directory else ""
            else:
                directory = self._restore_directory(key, code_path, metadata)
            active = PluginPackageActivation(
                name, payload["version"], payload["manifest_sha256"], code_path, key, payload["enabled"], directory
            )
            if legacy:
                backup_path = pointer_path.parent / "active.worker-v1.json"
                if not backup_path.exists():
                    atomic_write_text(backup_path, raw)
                self._write(active)
            return active
        except PluginPackageActivationError:
            raise
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise PluginPackageActivationError(f"插件安装记录恢复或迁移失败: {exc}") from exc

    def _restore_directory(
        self, key: str, code_path: Path, metadata: dict[str, object]
    ) -> PluginDependencyDirectory | None:
        """
        将活动键绑定到归档的完整锁，拒绝遗漏依赖或复用错误目录。
        """
        lock_path = code_path / "locks" / f"{current_plugin_target()}.txt"
        lock = PluginDependencyLock.parse(lock_path.read_bytes() if lock_path.is_file() else b"")
        direct = lock.validate_direct(metadata.get("pythonDependencies", []))
        directory = self.environment_pool.ready_directory(key) if key else None
        if key:
            if directory is None:
                raise PluginPackageActivationError("插件依赖目录尚未就绪或已损坏")
            if self.environment_pool.environment_key(PluginEnvironmentSpec(current_plugin_target(), lock_path)) != key:
                raise PluginPackageActivationError("活动指针依赖键与归档完整锁不匹配")
            # 复用仍需检查当前插件激活的 extras，不能只校验目录存在。
            self.environment_pool.ensure(
                PluginEnvironmentSpec(
                    current_plugin_target(), lock_path, direct_dependencies=tuple(str(item) for item in direct)
                )
            )
        elif lock.packages:
            raise PluginPackageActivationError("活动指针遗漏了归档依赖目录")
        return directory

    def set_enabled(self, name: str, enabled: bool) -> PluginPackageActivation:
        """
        原子更新下次启动的启用意图，不自行执行生命周期钩子。

        :param name: 已安装插件名
        :param enabled: 下次启动是否启用
        :return: 更新后的安装记录
        :raises PluginPackageActivationError: 安装记录不存在或无法验证
        """
        pointer_path = self._pointer_path(name)
        with _exclusive_lock(pointer_path.parent / "activation.lock"):
            active = replace(self._restore_locked(name, pointer_path), enabled=enabled)
            self._write(active)
            return active

    def uninstall(self, name: str) -> None:
        """
        撤销归档安装指针，保留代码和依赖供进程结束后安全回收。

        :param name: 要撤销的插件名
        """
        pointer_path = self._pointer_path(name)
        with _exclusive_lock(pointer_path.parent / "activation.lock"):
            if pointer_path.exists():
                pointer_path.replace(pointer_path.parent / "uninstalled.json")
