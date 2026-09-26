# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：在插件发现目录外暂存安装内容，并用恢复日志保护目录切换。
#
# 公开接口：
#   - class PluginInstallTransactionError — 插件安装文件事务失败。
#   - class PluginInstallTransaction — 暂存、切换、提交和回滚单个插件目录。
#   - recover_plugin_install_transactions(data_path, plugin_dir) — 启动时恢复未完成的安装。
# ============================================================

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from uuid import uuid4

from ECL.plugins.environment_pool import PluginEnvironmentError, _exclusive_lock
from ECL.utils import atomic_write_text, get_logger

plugin_name_pattern = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
transaction_id_pattern = re.compile(r"[0-9a-f]{32}\Z")


class PluginInstallTransactionError(RuntimeError):
    """
    表示插件目录事务无法安全切换或恢复。

    失败时保留恢复日志与备份，避免下次启动发现半成品目录。
    """


class PluginInstallTransaction:
    """
    管理一个插件的磁盘安装事务，不执行插件代码。

    暂存与备份均放在插件发现目录外。恢复日志在移动旧目录前原子写入；
    启动时若日志尚未提交，就恢复旧目录并丢弃新目录。
    """

    def __init__(self, data_path: Path, plugin_dir: Path, name: str, transaction_id: str | None = None) -> None:
        """
        根据经过校验的插件名与事务标识确定固定目录。

        :param data_path: 启动器持久化数据目录
        :param plugin_dir: 当前管理器的用户插件发现目录
        :param name: 插件清单中的安全名称
        :param transaction_id: 恢复已有事务时提供的 32 位十六进制标识
        :raises PluginInstallTransactionError: 名称或标识不符合路径边界时抛出
        """
        if plugin_name_pattern.fullmatch(name) is None:
            raise PluginInstallTransactionError("插件名包含非法字符")
        identifier = transaction_id or uuid4().hex
        if transaction_id_pattern.fullmatch(identifier) is None:
            raise PluginInstallTransactionError("插件安装事务标识无效")
        self.data_path = Path(data_path)
        self.plugin_dir = Path(plugin_dir)
        self.name = name
        self.transaction_id = identifier
        self.target_path = self.plugin_dir / name
        self.transaction_path = self.data_path / "plugin_install_transactions"
        self.journal_path = self.transaction_path / f"{name}.json"
        self.lock_path = self.transaction_path / f"{name}.lock"
        self.stage_path = self.data_path / "plugin_install_staging" / identifier
        self.backup_path = self.data_path / "plugin_install_backups" / identifier
        self.had_old = False

    def stage(self, source_path: Path) -> None:
        """
        在发现目录外复制插件源内容，失败时只清理本次暂存。

        :param source_path: 已通过清单预检的目录
        :raises PluginInstallTransactionError: 源目录复制失败时抛出
        """
        self.stage_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copytree(source_path, self.stage_path)
        except OSError as exc:
            if self.stage_path.exists():
                shutil.rmtree(self.stage_path)
            raise PluginInstallTransactionError("插件暂存复制失败") from exc

    def begin(self) -> None:
        """
        在原子恢复日志保护下将暂存目录切换为可发现的插件目录。

        :raises PluginInstallTransactionError: 日志写入或目录切换失败时抛出
        """
        self.had_old = self.target_path.exists()
        self.transaction_path.mkdir(parents=True, exist_ok=True)
        self.backup_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_journal("pending")
        try:
            if self.had_old:
                self.target_path.replace(self.backup_path)
            self.stage_path.replace(self.target_path)
        except OSError as exc:
            raise PluginInstallTransactionError("插件目录切换失败") from exc

    def commit(self) -> bool:
        """
        标记新目录已通过加载与启用验证，再清理旧目录备份。

        :return: 旧目录已清理为 True；清理待重试为 False
        :raises PluginInstallTransactionError: 提交标记无法持久化时抛出
        """
        self._write_journal("committed")
        try:
            self.cleanup_committed()
        except OSError:
            return False
        return True

    def cleanup_committed(self) -> None:
        """
        删除已提交事务的备份和暂存；清理失败时保留日志供下次启动重试。

        :raises OSError: 文件系统拒绝清理时抛出
        """
        if self.backup_path.exists():
            shutil.rmtree(self.backup_path)
        if self.stage_path.exists():
            shutil.rmtree(self.stage_path)
        self.journal_path.unlink(missing_ok=True)

    def rollback(self) -> None:
        """
        恢复原目录或移除首次安装的新目录，成功后清理恢复日志。

        :raises PluginInstallTransactionError: 旧目录缺失或目录操作失败时抛出
        """
        try:
            if self.had_old:
                if self.backup_path.exists():
                    if self.target_path.exists():
                        shutil.rmtree(self.target_path)
                    self.backup_path.replace(self.target_path)
                elif not self.target_path.exists() or not self.stage_path.exists():
                    raise PluginInstallTransactionError("插件旧版备份缺失，无法确认活动目录是否安全")
            elif self.target_path.exists():
                shutil.rmtree(self.target_path)
            if self.stage_path.exists():
                shutil.rmtree(self.stage_path)
            self.journal_path.unlink(missing_ok=True)
        except OSError as exc:
            raise PluginInstallTransactionError("插件安装回滚失败") from exc

    def _write_journal(self, phase: str) -> None:
        payload = {"name": self.name, "transaction_id": self.transaction_id, "had_old": self.had_old, "phase": phase}
        try:
            atomic_write_text(self.journal_path, json.dumps(payload, ensure_ascii=False))
        except OSError as exc:
            raise PluginInstallTransactionError("插件安装恢复日志写入失败") from exc


def recover_plugin_install_transactions(data_path: Path, plugin_dir: Path) -> list[str]:
    """
    在插件发现前恢复所有未提交目录切换，并完成已提交事务的清理。

    无效或无法恢复的日志会保留并返回错误；调用方不得静默加载其同名目录。

    :param data_path: 启动器持久化数据目录
    :param plugin_dir: 用户插件发现目录
    :return: 无法恢复的插件名称列表
    """
    transaction_path = Path(data_path) / "plugin_install_transactions"
    if not transaction_path.is_dir():
        return []
    logger = get_logger("PluginInstallTransaction")
    failed_names: list[str] = []
    for journal_path in sorted(transaction_path.glob("*.json")):
        name = journal_path.stem
        try:
            payload = json.loads(journal_path.read_text(encoding="utf-8"))
            if (
                not isinstance(payload, dict)
                or payload.get("name") != name
                or not isinstance(payload.get("transaction_id"), str)
                or not isinstance(payload.get("had_old"), bool)
                or payload.get("phase") not in {"pending", "committed"}
            ):
                raise PluginInstallTransactionError("插件安装恢复日志无效")
            transaction = PluginInstallTransaction(data_path, plugin_dir, name, payload["transaction_id"])
            transaction.had_old = payload["had_old"]
            with _exclusive_lock(transaction.lock_path):
                if payload["phase"] == "committed":
                    if transaction.target_path.exists():
                        transaction.cleanup_committed()
                    else:
                        transaction.rollback()
                else:
                    transaction.rollback()
        except (OSError, ValueError, PluginEnvironmentError, PluginInstallTransactionError):
            logger.exception("插件 %s 的安装恢复日志无法处理: %s", name, journal_path)
            failed_names.append(name)
    return failed_names
