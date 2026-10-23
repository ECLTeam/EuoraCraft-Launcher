# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：将实例资料四字段修改接入原始双许可和应用唯一任务执行器，不向Worker暴露路径。
#
# 公开接口：
#   - class RuntimeInstanceProfiles — 本次运行的资料任务提交适配器。
# ============================================================

from __future__ import annotations

from time import monotonic
from typing import TYPE_CHECKING

from ECL.plugin_api.contracts import JsonValue
from ECL.plugins.grant_store import PluginGrantSession
from ECL.plugins.runtime.models import InstanceProfilePatchRequest
from ECL.plugins.runtime.operation_adapters import RuntimeOperations
from ECL.plugins.runtime.resources import RuntimeResources

if TYPE_CHECKING:
    from ECL.services.game.instance_profile_transactions import InstanceProfileTransactions


class RuntimeInstanceProfiles:
    """
    只创建当前安装及run自己的资料任务，真实资料资源由唯一游戏领域服务拥有。
    """

    def __init__(
        self,
        session: PluginGrantSession,
        resources: RuntimeResources,
        transactions: InstanceProfileTransactions,
        operations: RuntimeOperations,
    ) -> None:
        """
        绑定宿主现有服务，不创建资料存储、执行器或任务状态表。

        :param session: 当前运行授权
        :param resources: 当前运行共享实例引用
        :param transactions: 应用唯一实例资料事务资源
        :param operations: 当前运行自己的任务控制适配器
        """
        self._session = session
        self._resources = resources
        self._transactions = transactions
        self._operations = operations

    def submit(self, params: dict[str, JsonValue], submission_deadline: float) -> dict[str, JsonValue]:
        """
        严格校验后登记一次有界任务，版本和物理对象在执行及提交前再次核对。

        :param params: 只含实例引用、完整版本及四字段增量的请求
        :param submission_deadline: 当前RPC提交截止
        :return: 当前run自身任务引用，不表示已保存资料
        :raises ProtocolError: 权限、资源或执行器不满足提交条件
        """
        from ECL.services.game.instance_profile_models import InstanceProfilePatch, InstanceProfileSnapshot

        deadline = monotonic() + 30
        query = InstanceProfilePatchRequest.model_validate(params)
        patch = InstanceProfilePatch.model_validate(query.patch)
        target, read, write = self._resources.profile_target(query.instance_ref, submission_deadline)
        item = self._transactions.reserve(target, self._session.view().installation_id)

        def admit() -> None:
            self._resources.verify_profile_target(target, read, write, deadline)
            self._session.accept_all((read, write), lambda: None)

        def project(result: object) -> JsonValue:
            if not isinstance(result, InstanceProfileSnapshot):
                return None
            # 原write在提交前受控；交付已提交内容只要求原read和同一实际对象。
            self._resources.verify_profile_read(target, read, monotonic() + 5)
            from ECL.services.game.instance_profiles import InstanceProfileStore

            fields = InstanceProfileStore.parse_local_fields(result.document)
            return {
                "instance_ref": query.instance_ref,
                "alias": fields.get("alias", target.version_id),
                "profile": {
                    "description": fields.get("description", ""),
                    "favorite": fields.get("favorite", False),
                    "tags": fields.get("tags", []),
                },
                "profile_etag": result.etag,
            }

        try:
            reference = self._operations.submit(
                "plugin_instance_profile_patch",
                lambda context: self._transactions.patch(
                    item, query.expected_profile_etag, patch, context, deadline, admit
                ),
                write,
                additional_leases=(read,),
                result_lease=read,
                project_result=project,
                cleanup=lambda: self._transactions.cleanup(item),
                deadline=deadline,
                submission_deadline=submission_deadline,
            )
        except BaseException:
            self._transactions.cleanup(item)
            raise
        return {"operation_ref": reference, "state": "queued"}
