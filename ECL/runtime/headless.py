# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：无头有限任务的执行、结果输出与资源关闭，不进入桌面生命周期。
#
# 公开接口：
#   - run_headless — 执行任务并返回可观察的最终退出码。
# ============================================================

from __future__ import annotations

import json
import sys
import traceback
from dataclasses import asdict
from pathlib import Path

from ECL.foundation.files import atomic_write_text
from ECL.foundation.logging import safe_log_text
from ECL.runtime.context import HeadlessContext
from ECL.runtime.contracts import HeadlessExitCode, HeadlessOptions, HeadlessResult, HeadlessRuntimeError
from ECL.runtime.tasks import execute_task


def _write_result(result: HeadlessResult, result_file: Path | None) -> None:
    """
    输出一次 JSON 结果，拒绝覆盖既有文件和丢失无控制台结果。

    文件目标由调用者显式指定；并发调用者须使用不同文件名。最终写入失败不能报告任务成功。
    """
    encoded = json.dumps(asdict(result), ensure_ascii=True) + "\n"
    if result_file is not None:
        if result_file.exists() or result_file.is_symlink():
            raise HeadlessRuntimeError("HEADLESS_RESULT_EXISTS", "结果目标已经存在，未覆盖原文件")
        atomic_write_text(result_file, encoded)
        return
    if sys.stdout is None:
        raise HeadlessRuntimeError("HEADLESS_OUTPUT_UNAVAILABLE", "标准输出不可用，请指定结果文件")
    sys.stdout.write(encoded)
    sys.stdout.flush()


def _execute(context: HeadlessContext) -> tuple[HeadlessResult, HeadlessExitCode]:
    """
    在执行边界将预期失败、未预期异常和请求取消转为最终任务状态。
    """
    options = context.options
    try:
        context.logger.info("开始检查无头运行环境")
        probe = execute_task(context)
        context.logger.info("无头运行环境检查成功")
        return HeadlessResult(options.task, True, probe=probe), HeadlessExitCode.success
    except KeyboardInterrupt:
        context.logger.info("无头运行环境检查已停止")
        return HeadlessResult(options.task, False, "HEADLESS_CANCELLED", "无头任务已停止"), HeadlessExitCode.cancelled
    except HeadlessRuntimeError as error:
        context.logger.warning("无头运行环境检查失败；原因：%s", error)
        return HeadlessResult(options.task, False, error.code, str(error)), HeadlessExitCode.failed
    except Exception:
        context.logger.exception("无头运行环境检查异常")
        return HeadlessResult(options.task, False, "HEADLESS_TASK_FAILED", "无头任务执行异常"), HeadlessExitCode.failed


def run_headless(options: HeadlessOptions) -> HeadlessExitCode:
    """
    执行一次无头任务，只有任务和结果输出均成功才返回成功。

    无输出通道时不执行任务。所有路径关闭自身上下文，不修改桌面实例、账户或设置。

    :param options: 已完成命令行校验的任务参数
    :return: 最终执行、输出或取消的退出码
    """
    context: HeadlessContext | None = None
    try:
        if options.result_file is None:
            if sys.stdout is None or not sys.stdout.writable():
                _report_error("标准输出不可用，请通过 --result-file 指定结果文件")
                return HeadlessExitCode.failed
            # 在执行前探测失效的输出流，避免完成任务后才发现结果无法交付。
            sys.stdout.write("")
            sys.stdout.flush()
        try:
            context = HeadlessContext(options)
        except KeyboardInterrupt:
            result = HeadlessResult(options.task, False, "HEADLESS_CANCELLED", "无头任务已停止")
            exit_code = HeadlessExitCode.cancelled
        except Exception:
            _report_error("无头运行环境初始化失败\n" + traceback.format_exc())
            result = HeadlessResult(options.task, False, "HEADLESS_INITIALIZATION_FAILED", "无头运行环境初始化失败")
            exit_code = HeadlessExitCode.failed
        else:
            result, exit_code = _execute(context)
        _write_result(result, options.result_file)
        return exit_code
    except (OSError, ValueError, HeadlessRuntimeError) as error:
        if context is not None:
            context.logger.warning("无头结果输出失败；原因：%s", type(error).__name__)
        else:
            _report_error("无头结果输出不可用")
        return HeadlessExitCode.failed
    finally:
        if context is not None:
            context.close()


def _report_error(message: str) -> None:
    """
    在上下文尚未建立时输出脱敏诊断，标准流失效时保持最终非零退出语义。
    """
    if sys.stderr is not None:
        try:
            sys.stderr.write(safe_log_text(message) + "\n")
            sys.stderr.flush()
        except (OSError, ValueError):
            return
