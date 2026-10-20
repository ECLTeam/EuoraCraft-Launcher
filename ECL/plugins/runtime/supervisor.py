# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：创建、认证、许可加载和回收受控 Worker，不自动发现或授权用户插件。
#
# 公开接口：
#   - class WorkerSupervisor — 同一发行入口的受信进程引导及测试验收边界。
# ============================================================

from __future__ import annotations

import os
import platform
from collections.abc import Callable
from pathlib import Path
from threading import RLock
from time import monotonic, sleep
from uuid import uuid4

from ECL.plugin_api._imports import DependencySnapshot
from ECL.plugin_api._rpc import RpcPeer, RpcRequest
from ECL.plugin_api._transport import PipeChannel, PipeServer
from ECL.plugin_api._windows import WindowsRuntimeError
from ECL.plugin_api.contracts import JsonValue, ProtocolError, WireMessage
from ECL.plugin_api.snapshot import CodeSnapshot
from ECL.plugins.environment_pool import PluginDependencyDirectory, PluginEnvironmentPool
from ECL.plugins.grant_store import PluginGrantError, PluginGrantSession

from .executable import ExecutableLaunchSpec
from .windows import WindowsJob, WindowsProcess


class WorkerSupervisor:
    """
    先确认 Job 和真实连接进程，再接受显式的宿主加载许可。

    本类尚不接入用户插件管理器。调用方必须在业务集成层完成用户授权和内容绑定。
    同步方法须在后台边界调用，不得直接阻塞桌面事件循环。
    """

    def __init__(
        self,
        executable: ExecutableLaunchSpec | None = None,
        *,
        host_handler: Callable[[RpcRequest], dict[str, JsonValue]] | None = None,
    ) -> None:
        """
        建立尚未创建进程的监管对象。

        :param executable: 测试指定的真实发行入口；None解析当前宿主
        :param host_handler: 绑定本连接主体的白名单 Host API 分派；默认全部拒绝
        """
        self.executable: ExecutableLaunchSpec = executable or ExecutableLaunchSpec.current()
        self.run_id: str = uuid4().hex
        self.state: str = "stopped"
        self.pid: int | None = None
        self.created_at: int | None = None
        self.image_path: Path | None = None
        self.root_process: WindowsProcess | None = None
        self._worker_handle: int | None = None
        self._job: WindowsJob | None = None
        self._channel: PipeChannel | None = None
        self._lock = RLock()
        self._host_handler = host_handler
        self._peer: RpcPeer | None = None
        self._has_started = False

    def start(self, timeout: float = 30.0) -> dict[str, JsonValue]:
        """
        挂起创建并分配 Job，恢复后核实实际 Worker 身份。

        :param timeout: 包含冻结冷解包的启动等待上限
        :return: 受信引导摘要，尚未执行插件入口
        :raises WindowsRuntimeError: 平台、进程或身份检查失败时抛出
        """
        if platform.system() != "Windows" or platform.machine().casefold() not in {"amd64", "x86_64"}:
            raise WindowsRuntimeError("当前平台尚未验收插件 Worker", "PLUGIN_RUNTIME_UNSUPPORTED")
        with self._lock:
            if self.state != "stopped" or self._has_started:
                raise WindowsRuntimeError("Worker 运行会话已使用", "PLUGIN_SESSION_EXPIRED")
            self.state = "starting"
            self._has_started = True
            server: PipeServer | None = None
            try:
                self._job = WindowsJob()
                address = f"\\\\.\\pipe\\ecl-worker-{self.run_id}"
                server = PipeServer(address)
                self.root_process = self._job.spawn_suspended(
                    self.executable.arguments(address, os.getpid(), self.run_id),
                    self.executable.working_directory(),
                    self.executable.environment(),
                )
                self.root_process.resume()
                self._channel = server.accept(timeout)
                self._authenticate()
                hello = self._channel.receive(5)
                if (
                    hello.kind != "hello"
                    or hello.params.get("run_id") != self.run_id
                    or hello.params.get("api_version") != 2
                ):
                    raise ProtocolError("Worker 引导握手不兼容")
                if hello.params.get("has_host_modules") is not False or hello.params.get("is_in_job") is not True:
                    raise ProtocolError("Worker 引导未保持宿主导入及监管边界")
                if hello.params.get("pid") != self.pid:
                    raise ProtocolError("Worker 自报身份与系统连接身份不一致")
                self.state = "awaiting_authorization"
                return hello.params
            except BaseException:
                self.state = "failed"
                self.close()
                raise
            finally:
                if server is not None:
                    server.close()

    def _authenticate(self) -> None:
        """
        保留原生进程句柄、创建时间与 Job 归属，拒绝可伪造的 PID 字段。
        """
        assert self._channel is not None and self._job is not None
        pid = self._channel.peer_pid()
        handle = self._job.api.open_process(pid)
        self._worker_handle = handle
        created_at, image_path = self._job.api.process_identity(handle)
        if not self._job.contains(handle) or image_path != self.executable.executable_path.resolve():
            raise WindowsRuntimeError("连接进程不属于预期 Worker", "PLUGIN_PIPE_AUTH_FAILED")
        self.pid, self.created_at, self.image_path = pid, created_at, image_path

    def authorize_load(
        self,
        session: PluginGrantSession,
        entry_file: str = "main.py",
        dependency: PluginDependencyDirectory | None = None,
        *,
        entry_class: str = "Plugin",
    ) -> None:
        """
        只为本运行已经审阅且必要权限满足的内容发出加载许可。

        :param session: 宿主持有的授权会话，不能由 Worker 自报
        :param entry_file: 快照内的 Python 入口
        :param dependency: 现有共享依赖池中已就绪的目录
        :param entry_class: v2 Plugin 的实际类名
        :raises PluginGrantError: 未审阅、缺少必要许可或运行身份不匹配时抛出
        """
        if session.run_id != self.run_id:
            raise PluginGrantError("授权不属于当前插件运行", "PLUGIN_SESSION_EXPIRED")
        session.binding.verify()
        view = session.view()
        if view.error_code:
            raise PluginGrantError("插件授权记录不可用", view.error_code)
        if view.revision == 0:
            raise PluginGrantError("插件内容尚未完成用户审阅", "PLUGIN_GRANT_REVIEW_REQUIRED")
        if not view.required_satisfied:
            raise PluginGrantError("插件缺少必要授权", "PLUGIN_REQUIRED_PERMISSION_DENIED")
        self._authorize_snapshot(session.binding.snapshot, entry_file, dependency, entry_class=entry_class)

    def _authorize_snapshot(
        self,
        snapshot: CodeSnapshot,
        entry_file: str = "main.py",
        dependency: PluginDependencyDirectory | None = None,
        *,
        entry_class: str = "Plugin",
    ) -> None:
        """
        复核宿主批准的完整快照并发出唯一一次入口加载许可。

        本接口是内部引导边界，不向前端或插件暴露；用户权限检查由上层集成负责。

        :param snapshot: 宿主持有并经过审阅的不可变代码清单
        :param entry_file: 快照内的 Python 入口
        :param dependency: 现有安装池准备的共享依赖，不重新下载解释器
        :raises ProtocolError: 代码变化或状态不允许加载时抛出
        """
        with self._lock:
            if self.state != "awaiting_authorization" or self._channel is None or self._job is None:
                raise ProtocolError("Worker 尚未完成加载前认证")
            snapshot.verify(snapshot.digest())
            if entry_file not in {item.name for item in snapshot.files}:
                raise ProtocolError("插件入口不在批准快照内")
            assert self._worker_handle is not None
            if not self._job.contains(self._worker_handle):
                raise ProtocolError("Worker 监管归属已经失效")
            try:
                dependency_payload = self._dependency_payload(dependency)
                self._channel.send(
                    WireMessage(
                        "load_authorized",
                        {
                            "root_path": str(snapshot.root_path),
                            "code_digest": snapshot.digest(),
                            "entry_file": entry_file,
                            "entry_class": entry_class,
                            "dependency": dependency_payload,
                        },
                    )
                )
                reply = self._channel.receive(15)
                if reply.kind == "error":
                    code = reply.params.get("code")
                    allowed = {"PLUGIN_CONTENT_CHANGED", "PLUGIN_DEPENDENCY_CONFLICT", "PLUGIN_PROTOCOL_ERROR"}
                    raise WindowsRuntimeError(
                        "插件入口加载失败", code if code in allowed else "PLUGIN_WORKER_EXECUTION_FAILED"
                    )
                if reply.kind != "loaded" or reply.params.get("pid") != self.pid:
                    raise ProtocolError("Worker 没有确认许可加载结果")
                self.state = "loaded"
                self._peer = RpcPeer(self._channel, "host", self._dispatch_host)
                self._peer.start()
            except BaseException:
                self.state = "failed"
                self.close()
                raise

    @staticmethod
    def _dependency_payload(directory: PluginDependencyDirectory | None) -> JsonValue:
        """
        从现有池复核安装结果，绑定全部库文件而不是信任裸 site-packages 路径。
        """
        if directory is None:
            return None
        root_path = directory.site_path.parent
        pool = PluginEnvironmentPool(root_path.parents[1])
        if pool.ready_directory(directory.key) != directory:
            raise ProtocolError("插件依赖目录已经变化", "PLUGIN_CONTENT_CHANGED")
        snapshot = DependencySnapshot.capture(root_path)
        return {
            "root_path": str(root_path),
            "digest": snapshot.digest(),
            "imports": sorted({name for package in directory.packages for name in package.imports}),
            "packages": sorted(package.name for package in directory.packages),
        }

    def request(
        self, method: str, params: dict[str, JsonValue] | None = None, timeout: float = 5.0
    ) -> dict[str, JsonValue]:
        """
        调用已接线的方法，等待期间接收线程继续处理反向请求。

        :param method: 已定义的 Worker 方法
        :param params: 方法的 JSON 参数
        :param timeout: 等待上限
        :return: 校验后的响应内容
        :raises ProtocolError: 状态或回复标识不符时抛出
        """
        with self._lock:
            if self.state != "loaded" or self._peer is None:
                raise ProtocolError("Worker 尚未完成许可加载")
            peer = self._peer
        return peer.call(method, params if params is not None else {}, timeout)

    def _dispatch_host(self, request: RpcRequest) -> dict[str, JsonValue]:
        """
        只调用宿主明确绑定的白名单，不把 Worker 方法名反射到完整服务对象。
        """
        if self._host_handler is None:
            raise ProtocolError("此 Host API 尚未接线", "PLUGIN_API_METHOD_NOT_FOUND")
        return self._host_handler(request)

    def active_count(self) -> int:
        """
        查询实际监管成员数，包含单文件引导和普通后代。

        :return: 当前 Job 中的活跃进程数
        """
        return self._job.active_count() if self._job is not None else 0

    def close(self) -> None:
        """
        有界请求正常退出，再按 Job 回收全部成员并关闭身份句柄。

        不把普通管道超时解释为线程或进程已经结束。
        """
        with self._lock:
            if self._channel is not None:
                try:
                    if self._peer is not None:
                        self._peer.shutdown(1)
                    else:
                        self._channel.send(WireMessage("shutdown", {}), 1)
                        self._channel.receive(1)
                except (OSError, ProtocolError, WindowsRuntimeError):
                    # 关闭阶段允许对端已退出，实际停止仍由下方 Job 查询确认。
                    self.state = "stopping"
                finally:
                    self._channel.close()
                    self._channel = None
                    self._peer = None
            if self._job is not None:
                deadline = monotonic() + 1
                while self._job.active_count() and monotonic() < deadline:
                    sleep(0.01)
                self._job.close()
                if self._worker_handle is not None:
                    self._job.api.close(self._worker_handle)
                    self._worker_handle = None
                self._job = None
            self.state = "stopped"

    def __enter__(self) -> WorkerSupervisor:
        self.start()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()
