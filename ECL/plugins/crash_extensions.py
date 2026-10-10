# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：插件崩溃分析回调：允许插件补充或修改分析结果。
#
# 公开接口：
#   - class CrashAnalysisContext — 保存传给插件的崩溃报告、游戏版本和进程输出等信息。
#   - class CrashAnalysisExtensionRegistry — 保存并调用插件注册的崩溃分析回调。
#       - register(owner, name, enrich) -> None — 注册或原位更新一个插件分析回调。
#       - unregister_owner(owner) -> None — 撤销指定插件的全部分析回调。
#       - enrich(context, result) -> dict[str, Any] — 应用全部插件分析回调，返回合并后的结果字典。
# ============================================================

"""
插件崩溃分析回调：允许插件补充或修改分析结果。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ECL.utils import get_logger


@dataclass(frozen=True)
class CrashAnalysisContext:
    """
    保存传给插件的崩溃报告、游戏版本和进程输出等信息。
    """

    report_id: str  # 崩溃报告唯一标识。
    version_id: str  # 发生崩溃的游戏版本。
    game_path: Path  # 游戏实例根目录。
    game_directory: Path  # 实际启动工作目录。
    exit_code: int | None  # 游戏进程退出码。
    detected_by: list[str]  # 已命中的分析器标识。
    reasons: list[dict[str, Any]]  # 当前收集到的崩溃原因。
    output: str  # 游戏进程输出。
    source_files: list[str]  # 参与分析的文件。


class CrashAnalysisExtensionRegistry:
    """
    保存并调用插件注册的崩溃分析回调。

    ``enrich(context, result)`` 返回的字典浅合并进最终结果；键 ``reasons``
    会被追加到已有原因列表而不是替换。单次回调异常被隔离，不会终止分析。
    """

    def __init__(self) -> None:
        self._extensions: list[dict[str, Any]] = []  # 按注册顺序保存的分析回调。
        self._logger = get_logger("CrashAnalysisExtensionRegistry")  # 扩展点日志器。

    def register(self, owner: str, name: str, enrich: Callable[[CrashAnalysisContext, dict[str, Any]], Any]) -> None:
        """
        注册或原位更新一个插件分析回调。

        :param owner: 插件名
        :param name: 稳定的回调标识
        :param enrich: 接收崩溃报告信息和当前分析结果的回调
        """
        entry = {"owner": owner, "name": name, "enrich": enrich}
        for existing in self._extensions:
            if existing["owner"] == owner and existing["name"] == name:
                existing.update(entry)
                return
        self._extensions.append(entry)

    def unregister_owner(self, owner: str) -> None:
        """
        撤销指定插件的全部分析回调。
        """
        self._extensions = [entry for entry in self._extensions if entry["owner"] != owner]

    def enrich(self, context: CrashAnalysisContext, result: dict[str, Any]) -> dict[str, Any]:
        """
        应用全部插件分析回调，返回合并后的结果字典。

        :param context: 崩溃报告、游戏版本和进程输出等信息
        :param result: 宿主基础分析结果，会被原地修改
        :return: 合并后的结果字典
        """
        for entry in list(self._extensions):
            try:
                extra = entry["enrich"](context, dict(result)) or {}
                if not isinstance(extra, dict):
                    continue
                extra_reasons = extra.pop("reasons", None)
                if isinstance(extra_reasons, list):
                    result["reasons"].extend(extra_reasons)
                result.update(extra)
            except Exception:
                self._logger.exception("插件崩溃富化回调失败：插件：%s；扩展：%s", entry["owner"], entry["name"])
        return result
