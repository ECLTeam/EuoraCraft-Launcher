# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：原子保存 Java 登记清单，在线程与进程互斥下核对运行时拥有权。
#
# 公开接口：
#   - class JavaRegistry — 管理已发现及已登记 Java 的持久状态。
# ============================================================
from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from time import monotonic, sleep

from ECL.game import JavaScanner
from ECL.utils import atomic_write_text

from .models import JavaError, JavaPolicy, RegistryState, RuntimeRecord


class JavaRegistry:
    """
    用可校验的不可变清单保存登记状态，损坏清单不会被静默覆盖。

    公共修改在同一线程锁与 OS 文件锁下重新读取磁盘，以免多个启动器覆盖
    启用状态或安装拥有权；网络和全系统扫描不持有文件锁。
    """

    root_path: Path
    registry_path: Path

    def __init__(self, root_path: Path) -> None:
        """
        创建私有管理目录，登记和临时安装均归属该根目录。

        :param root_path: 后端确定的 Java 管理根目录
        """
        self.root_path = root_path.resolve()
        self.root_path.mkdir(parents=True, exist_ok=True)
        self.registry_path = self.root_path / "registry.json"
        self._thread_lock = RLock()

    @contextmanager
    def locked(self) -> Iterator[None]:
        """
        有界取得线程和文件互斥，退出时始终释放锁和句柄。

        :return: 仅用于短时清单或使用证据变更的互斥作用域
        :raises JavaError: 五秒内无法取得文件锁时抛出
        """
        with self._thread_lock:
            lock_path = self.root_path / ".registry.lock"
            if lock_path.is_symlink():
                raise JavaError("Java 管理锁文件无效", "JAVA_REGISTRY_INVALID")
            with lock_path.open("a+b") as handle:
                handle.seek(0, 2)
                if not handle.tell():
                    handle.write(b"0")
                    handle.flush()
                deadline = monotonic() + 5
                while True:
                    try:
                        handle.seek(0)
                        if sys.platform == "win32":
                            import msvcrt

                            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl

                            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError as exc:
                        if monotonic() >= deadline:
                            raise JavaError("Java 管理正在处理另一项操作，请稍后重试", "JAVA_BUSY") from exc
                        sleep(0.05)
                try:
                    yield
                finally:
                    handle.seek(0)
                    if sys.platform == "win32":
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def read(self) -> RegistryState:
        """
        读取并校验清单，调用方修改前必须取得 locked 作用域。

        :return: 当前不可变清单
        :raises JavaError: 清单损坏、过大或链接替换时抛出并保留原文件
        """
        if not self.registry_path.exists():
            if self.registry_path.is_symlink():
                raise JavaError("Java 登记清单已被替换", "JAVA_REGISTRY_INVALID")
            return RegistryState()
        try:
            if self.registry_path.is_symlink() or self.registry_path.stat().st_size > 8 * 1024 * 1024:
                raise ValueError("invalid registry")
            return RegistryState.model_validate_json(self.registry_path.read_bytes())
        except (OSError, ValueError) as exc:
            raise JavaError("Java 登记清单无法读取，原文件已保留", "JAVA_REGISTRY_INVALID") from exc

    def write(self, state: RegistryState) -> None:
        """
        在调用方已持有 locked 时原子替换清单，失败不提交内存状态。

        :param state: 已校验的完整清单
        :raises JavaError: 持久化失败时抛出
        """
        try:
            content = state.model_dump_json(indent=2)
            if len(content.encode()) > 8 * 1024 * 1024:
                raise JavaError("Java 登记清单超过容量限制", "JAVA_REGISTRY_LIMIT")
            atomic_write_text(self.registry_path, content)
        except OSError as exc:
            raise JavaError("Java 登记保存失败", "JAVA_REGISTRY_WRITE_FAILED") from exc

    def register(self, executable_path: Path) -> RuntimeRecord:
        """
        验证手动选择的 Java 并长期登记，不改变其目录或当前启动配置。

        :param executable_path: 用户选择的 Java 或 javaw 可执行文件
        :return: 已确认落盘的运行时记录
        :raises JavaError: 探测失败或保存失败时抛出
        """
        runtime = JavaScanner.probe(executable_path)
        if runtime is None:
            raise JavaError("所选文件不是可用的 Java 运行时", "JAVA_INVALID_RUNTIME")
        candidate = JavaPolicy.record(runtime, "manual")
        if any(candidate.java_home_path.is_relative_to(self.root_path / folder) for folder in (".staging", ".trash")):
            raise JavaError("该 Java 仍在安装或清理中，请等待任务结束", "JAVA_BUSY")
        with self.locked():
            state = self.read()
            existing = next((record for record in state.runtimes if record.runtime_id == candidate.runtime_id), None)
            if existing and existing.origin == "managed":
                candidate = existing.model_copy(update={"validation_status": "valid"})
            else:
                candidate = candidate.model_copy(
                    update={
                        "is_enabled": existing.is_enabled if existing else True,
                        "registered_at": existing.registered_at if existing else datetime.now(UTC).isoformat(),
                    }
                )
            self.replace(state, candidate)
        return candidate

    def replace(self, state: RegistryState, record: RuntimeRecord) -> None:
        """
        在已持锁且已读取最新清单时，替换一个运行时并原子持久化。

        :param state: 最新的已校验登记快照
        :param record: 所属运行时的不可变登记记录
        """
        records = {item.runtime_id: item for item in state.runtimes}
        records[record.runtime_id] = record
        self.write(state.model_copy(update={"runtimes": tuple(records.values())}))

    def set_enabled(self, runtime_id: str, is_enabled: bool) -> RuntimeRecord:
        """
        修改后续启动的可用状态，不终止已经启动的游戏。

        :param runtime_id: 登记的稳定运行时身份
        :param is_enabled: 是否允许后续使用
        :return: 已保存的记录
        :raises JavaError: 条目不存在或启用目标失效时抛出
        """
        with self.locked():
            state = self.read()
            record = self.find(state, runtime_id)
            if is_enabled and JavaScanner.probe(record.executable_path) is None:
                raise JavaError("Java 已失效，请重新定位或安装", "JAVA_INVALID_RUNTIME")
            changed = record.model_copy(update={"is_enabled": is_enabled})
            self.replace(state, changed)
            return changed

    @staticmethod
    def find(state: RegistryState, runtime_id: str) -> RuntimeRecord:
        """
        从已读取的清单查找运行时，不接受目录路径作为删除身份。

        :param state: 最新的已校验登记快照
        :param runtime_id: 已登记的运行时身份
        """
        for record in state.runtimes:
            if record.runtime_id == runtime_id:
                return record
        raise JavaError("Java 登记条目不存在", "JAVA_RUNTIME_NOT_FOUND")

    def validate_owned_directory(self, record: RuntimeRecord) -> Path:
        """
        核对托管路径与原安装文件清单，拒绝外部目录和未知新增文件。

        :param record: 所属运行时的不可变登记记录
        """
        if record.origin != "managed" or record.install_path is None or not record.package_checksum:
            raise JavaError("该 Java 不属于启动器安装", "JAVA_NOT_MANAGED")
        expected = self.root_path / "installed" / f"temurin-{record.major_version}-{record.package_checksum[:16]}"
        path = record.install_path
        if path != expected or path.is_symlink() or path.resolve() != expected.absolute() or not path.is_dir():
            raise JavaError("Java 安装目录的拥有权无法确认", "JAVA_DIRECTORY_CHANGED")
        expected_files = set(record.installed_files)
        for child in path.rglob("*"):
            if not child.resolve().is_relative_to(path) or child.relative_to(path).as_posix() not in expected_files:
                raise JavaError("Java 目录含未登记内容，请保留并手动检查", "JAVA_DIRECTORY_CHANGED")
        return path
