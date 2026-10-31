# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：约束对外权限名与内部细项名的映射不漂移。
#
# 公开接口：
#   - test_author_facing_names_are_declarable() -> None
#   - test_expansion_targets_are_implemented() -> None
#   - test_expansion_has_no_duplicates() -> None
#   - test_unknown_capability_is_rejected() -> None
# ============================================================

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ECL.plugins.grant_models import PermissionDeclaration, expanded_capabilities
from ECL.plugins.runtime.broker import PluginHostBroker


def test_author_facing_names_are_declarable() -> None:
    """清单里写合并名必须能通过声明校验，否则作者侧文档与实际行为不一致。"""
    for capability in expanded_capabilities:
        assert PermissionDeclaration(capability=capability).capability == capability


def test_expansion_targets_are_implemented() -> None:
    """合并名展开出的细项必须都是宿主已实现的能力，避免映射指向不存在的项。"""
    implemented = set(PluginHostBroker.implemented_capabilities)
    for name, targets in expanded_capabilities.items():
        assert targets, f"{name} 没有展开目标"
        assert set(targets) <= implemented, f"{name} 展开出未实现的能力：{set(targets) - implemented}"


def test_expansion_has_no_duplicates() -> None:
    """同一细项不能被两个合并名重复覆盖，否则授权视图会出现重复条目。"""
    covered: list[str] = [item for targets in expanded_capabilities.values() for item in targets]
    assert len(covered) == len(set(covered))


def test_unknown_capability_is_rejected() -> None:
    """未知能力名必须在校验阶段失败，不能静默降级为无权限。"""
    with pytest.raises(ValidationError):
        PermissionDeclaration(capability="game.everything")
