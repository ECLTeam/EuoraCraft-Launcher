# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：把公网站点读取、私有文件事务和自身任务接成有限下载，不拥有执行器或任务状态。
#
# 公开接口：
#   - class RuntimeDownloads — 提交只新建私有文件的受控下载任务。
# ============================================================

from __future__ import annotations

import hashlib
import traceback
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import ClassVar

import httpcore

from ECL.plugin_api.contracts import JsonValue, ProtocolError
from ECL.plugins.grant_models import GrantLease
from ECL.plugins.grant_store import PluginGrantError, PluginGrantSession
from ECL.plugins.network_targets import NetworkOrigin
from ECL.plugins.runtime.file_api import RuntimeFiles
from ECL.plugins.runtime.file_storage import PluginFileStorage, StagedFile
from ECL.plugins.runtime.models import DownloadSubmitRequest, NetworkRequest
from ECL.plugins.runtime.network_api import NetworkCall, RuntimeNetwork
from ECL.plugins.runtime.operation_adapters import RuntimeOperations
from ECL.services.operations import OperationContext
from ECL.utils import get_logger
from ECL.utils.errors import GameServiceError
from ECL.utils.operation_logging import private_transport_scope


@dataclass(slots=True)
class _DownloadResources:
    staged: StagedFile
    call: NetworkCall | None = None


class RuntimeDownloads:
    """
    接纳当前运行的双权限下载，状态由任务服务维护，资源由现有网络和文件服务持有。
    """

    task_seconds: ClassVar[float] = 1800

    def __init__(
        self, session: PluginGrantSession, files: RuntimeFiles, network: RuntimeNetwork, operations: RuntimeOperations
    ) -> None:
        """
        绑定宿主共享领域入口，不接受 Worker 提供的路径、执行器或网络后端。

        :param session: 当前运行的授权会话
        :param files: 私有文件引用和原生事务边界
        :param network: 当前运行共享的查询及下载网络归属
        :param operations: 应用唯一任务服务的本运行门面
        """
        self._session = session
        self._files = files
        self._network = network
        self._operations = operations

    def submit(self, params: dict[str, JsonValue], deadline: float) -> dict[str, JsonValue]:
        """
        准备有限暂存并接纳自身任务，RPC 不等待实际网络请求。

        :param params: 严格的新文件下载请求
        :param deadline: 提交 RPC 截止，最多五秒
        :return: queued 状态的自身任务引用
        :raises ProtocolError: 请求、文件边界、截止或任务接纳失败时抛出
        :raises PluginGrantError: 原始网络和文件准入不同时有效时抛出
        """
        task_deadline = monotonic() + self.task_seconds
        deadline = min(deadline, monotonic() + 5)
        query = DownloadSubmitRequest.model_validate(params)
        view = self._session.view()
        permission = next((item for item in view.items if item.capability == "network.read"), None)
        network_lease = self._session.authorize("network.read", permission.resource_refs if permission else ())
        write_lease = self._session.authorize("plugin.base")
        self._session.check_all((write_lease, network_lease))
        origin, _ = NetworkOrigin.from_url(query.url)
        if origin.reference not in network_lease.resource_refs:
            raise ProtocolError("插件下载站点未获准", "PLUGIN_RESOURCE_DENIED")
        read_lease = write_lease
        staged = self._files.prepare(
            query.target,
            write_lease,
            PluginFileStorage.max_file_bytes,
            None,
            deadline,
            additional_leases=(network_lease,),
        )
        resources = _DownloadResources(staged)
        with staged.io_lock:
            staged.owner = "task"
            try:
                reference = self._operations.submit(
                    "plugin_download",
                    lambda context: self._execute(query, network_lease, resources, task_deadline, context),
                    write_lease,
                    additional_leases=(network_lease,),
                    deadline=task_deadline,
                    submission_deadline=deadline,
                    project_result=self._files.project,
                    result_lease=read_lease,
                    cleanup=lambda: self._cleanup(resources),
                    initial_effect="staged",
                )
            except BaseException:
                self._files.cleanup(staged)
                raise
        return {"operation_ref": reference, "state": "queued"}

    def _cleanup(self, resources: _DownloadResources) -> bool:
        """
        两个领域分别尝试释放，任何真实工作未结束时保留任务的 cleanup pending。
        """
        is_network_idle = True
        if resources.call is not None:
            resources.call.finish()
            is_network_idle = resources.call.is_idle()
        is_file_idle = self._files.cleanup(resources.staged)
        return is_network_idle and is_file_idle

    def _execute(
        self,
        query: DownloadSubmitRequest,
        lease: GrantLease,
        resources: _DownloadResources,
        deadline: float,
        context: OperationContext,
    ) -> object:
        """
        在唯一任务线程内流式写入，网络真实关闭并完成完整性校验后才接受原子发布。
        """
        with private_transport_scope():
            try:
                context.check_cancelled()
                call = self._network.service.begin(
                    self._network,
                    lease,
                    deadline,
                    additional_leases=(resources.staged.lease,),
                    cancel_event=context.cancel_event,
                    wire_limit=9 * 1024 * 1024,
                )
                resources.call = call
                try:
                    with self._network.stream(call, NetworkRequest(method="GET", url=query.url)) as (_, response):
                        digest = self._receive(response, call, resources.staged, context)
                finally:
                    call.finish()
                call.check()
                if not call.is_idle():
                    raise ProtocolError("插件下载网络资源尚未关闭", "PLUGIN_NETWORK_FAILED")
                if query.expected_sha256 is not None and digest != query.expected_sha256:
                    raise ProtocolError("插件下载文件摘要不符", "PLUGIN_HASH_MISMATCH")
                context.progress(95, "正在校验文件")
                return self._files.publish(resources.staged, digest, context, deadline=deadline)
            except (ProtocolError, PluginGrantError, GameServiceError):
                raise
            except (httpcore.NetworkError, httpcore.ProtocolError, httpcore.TimeoutException, OSError):
                # 取消或撤销唤醒 socket 时使用已经存在的实际信号，而非猜测异常文本。
                if resources.call is not None:
                    resources.call.check()
                raise ProtocolError("插件下载连接或响应失败", "PLUGIN_NETWORK_FAILED") from None
            except Exception as exc:
                frames = traceback.extract_tb(exc.__traceback__)
                locations = "; ".join(f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}" for frame in frames)
                get_logger("PluginDownloads").error(
                    "插件私有文件下载异常；类型：%s；调用位置：%s", type(exc).__name__, locations
                )
                raise ProtocolError("插件下载未完成", "PLUGIN_NETWORK_FAILED") from None

    def _receive(
        self, response: httpcore.Response, call: NetworkCall, staged: StagedFile, context: OperationContext
    ) -> str:
        """
        只接收完整的 HTTP 200 实体，每个有限块单独核验准入，不保留整份响应。
        """
        if response.status != 200:
            raise ProtocolError("插件下载响应状态不支持", "PLUGIN_DOWNLOAD_FAILED")
        lengths = [value for name, value in response.headers if name.lower() == b"content-length"]
        encodings = [value.strip().lower() for name, value in response.headers if name.lower() == b"content-encoding"]
        if encodings and encodings != [b"identity"]:
            raise ProtocolError("插件下载响应编码不支持", "PLUGIN_NETWORK_ENCODING_UNSUPPORTED")
        expected: int | None = None
        if lengths:
            value = lengths[0]
            if len(lengths) != 1 or not value or not value.isdigit() or len(value) > 20:
                raise ProtocolError("插件下载响应长度无效", "PLUGIN_NETWORK_FAILED")
            expected = int(value)
            if expected > staged.reserved_bytes:
                raise ProtocolError("插件下载文件超过上限", "PLUGIN_RESPONSE_TOO_LARGE")
        digest = hashlib.sha256()
        for chunk in response.iter_stream():
            for offset in range(0, len(chunk), 32768):
                block = chunk[offset : offset + 32768]
                call.accept()
                self._files.write_staged(staged, block)
                digest.update(block)
                call.check()
                percent = staged.received_bytes * 90 / expected if expected else 0
                context.progress(percent, "正在接收文件")
        call.check()
        if expected is not None and staged.received_bytes != expected:
            raise ProtocolError("插件下载响应不完整", "PLUGIN_NETWORK_FAILED")
        context.progress(90, "文件接收完成")
        return digest.hexdigest()
