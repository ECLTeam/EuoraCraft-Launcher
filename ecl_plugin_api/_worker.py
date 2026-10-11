# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：在受信引导模式等待宿主加载许可，跳过桌面和业务初始化。
#
# 公开接口：
#   - run_worker() — 执行只由宿主创建和认证的 Worker 引导。
# ============================================================

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path
from types import ModuleType

from ecl_plugin_api._imports import DependencySnapshot, LockedImports
from ecl_plugin_api._transport import PipeChannel, connect_pipe
from ecl_plugin_api._windows import WindowsApi, WindowsRuntimeError
from ecl_plugin_api.contracts import JsonValue, ProtocolError, ProtocolPolicy, WireMessage
from ecl_plugin_api.snapshot import CodeSnapshot


def _load(message: WireMessage, run_id: str) -> tuple[ModuleType, LockedImports]:
    """
    只有宿主许可到达并通过完整快照复核，才执行插件入口。
    """
    params = message.params
    if message.kind != "load_authorized" or set(params) != {"root_path", "content_digest", "entry_file", "dependency"}:
        raise ProtocolError("Worker 尚未得到合法加载许可")
    root, digest, entry = params["root_path"], params["content_digest"], params["entry_file"]
    if (
        not isinstance(root, str)
        or not Path(root).is_absolute()
        or not isinstance(digest, str)
        or not isinstance(entry, str)
    ):
        raise ProtocolError("Worker 加载许可字段无效")
    snapshot = CodeSnapshot.capture(Path(root))
    snapshot.verify(digest)
    if entry not in {item.name for item in snapshot.files} or not entry.endswith(".py"):
        raise ProtocolError("Worker 入口不在批准的代码清单内")
    entry_path = snapshot.root_path / entry
    source = entry_path.read_bytes()
    record = next(item for item in snapshot.files if item.name == entry)
    if len(source) != record.size or hashlib.sha256(source).hexdigest() != record.sha256:
        raise ProtocolError("入口内容与许可不一致", "PLUGIN_CONTENT_CHANGED")
    roots = _code_imports(snapshot)
    site_path, packages = _dependency_imports(params["dependency"], roots)
    imports = LockedImports(roots, site_path, packages)
    imports.install()
    module = ModuleType(f"_ecl_worker_{run_id}")
    module.__file__ = str(entry_path)
    sys.modules[module.__name__] = module
    try:
        exec(compile(source, str(entry_path), "exec", dont_inherit=True), module.__dict__)
    except BaseException:
        imports.close()
        raise
    return module, imports


def _code_imports(snapshot: CodeSnapshot) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    for item in snapshot.files:
        if "/" not in item.name and item.name.endswith(".py"):
            name = item.name[:-3]
        elif item.name.endswith("/__init__.py"):
            name = item.name.split("/", 1)[0]
        else:
            continue
        if name.isidentifier():
            roots[name] = snapshot.root_path
    return roots


def _dependency_imports(value: JsonValue, roots: dict[str, Path]) -> tuple[Path | None, frozenset[str]]:
    """
    再次复核宿主批准的共享依赖字节，不执行 site 或 .pth。
    """
    if value is None:
        return None, frozenset()
    if not isinstance(value, dict) or set(value) != {"root_path", "digest", "imports", "packages"}:
        raise ProtocolError("依赖加载许可无效")
    root, digest = value["root_path"], value["digest"]
    names, packages = value["imports"], value["packages"]
    if not isinstance(root, str) or not Path(root).is_absolute() or not isinstance(digest, str):
        raise ProtocolError("依赖加载目录无效")
    if not isinstance(names, list) or not all(isinstance(name, str) and name.isidentifier() for name in names):
        raise ProtocolError("依赖导入清单无效")
    if not isinstance(packages, list) or not all(isinstance(name, str) and name for name in packages):
        raise ProtocolError("依赖包名称无效")
    snapshot = DependencySnapshot.capture(Path(root))
    snapshot.verify(digest)
    site_path = snapshot.root_path / "site-packages"
    if not site_path.is_dir() or set(names) & set(roots):
        raise ProtocolError("依赖与插件代码模块冲突", "PLUGIN_DEPENDENCY_CONFLICT")
    roots.update(dict.fromkeys(names, site_path))
    return site_path, frozenset(packages)


def _serve(channel: PipeChannel, run_id: str) -> None:
    """
    提供受信引导诊断与关闭，未接线的业务方法明确拒绝。
    """
    api = WindowsApi()
    is_in_job = api.is_in_job(None, api.kernel.GetCurrentProcess())
    if not is_in_job:
        raise WindowsRuntimeError("Worker 不在受控 Job 内", "PLUGIN_JOB_ASSIGN_FAILED")
    channel.send(
        WireMessage(
            "hello",
            {
                "api_version": ProtocolPolicy.api_version,
                "pid": os.getpid(),
                "run_id": run_id,
                "is_in_job": is_in_job,
                "has_host_modules": any(name == "ECL" or name.startswith("ECL.") for name in sys.modules),
            },
        )
    )
    message = channel.receive(15)
    if message.kind == "shutdown":
        channel.send(WireMessage("stopped", {}))
        return
    module, imports = _load(message, run_id)
    try:
        _serve_loaded(channel, module)
    finally:
        imports.close()


def _serve_loaded(channel: PipeChannel, module: ModuleType) -> None:
    channel.send(WireMessage("loaded", {"pid": os.getpid(), "module": module.__name__}))
    while True:
        try:
            message = channel.receive(30)
        except TimeoutError:
            continue
        if message.kind == "shutdown":
            channel.send(WireMessage("stopped", {}))
            return
        if message.kind != "request":
            raise ProtocolError("Worker 收到不允许的消息类型")
        if message.method == "runtime.info" and not message.params:
            data: dict[str, JsonValue] = {"api_version": 2, "pid": os.getpid(), "run_state": "loaded"}
            channel.send(WireMessage("response", {"ok": True, "data": data}, message.request_id))
        else:
            channel.send(
                WireMessage(
                    "response",
                    {
                        "ok": False,
                        "error": {
                            "code": "PLUGIN_API_METHOD_NOT_FOUND",
                            "message": "此业务接口尚未接线",
                            "retryable": False,
                        },
                    },
                    message.request_id,
                )
            )


def run_worker(argv: list[str]) -> int:
    """
    运行内部 Worker，任何失败均退出，不进入普通启动器。

    :param argv: 仅公开管道、宿主 PID 和会话参数
    :return: 正常退出为零；平台、协议或连接失败为二
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--pipe-address", required=True)
    parser.add_argument("--host-pid", required=True, type=int)
    parser.add_argument("--run-id", required=True)
    options = parser.parse_args(argv)
    if (
        not options.pipe_address.startswith("\\\\.\\pipe\\ecl-worker-")
        or not (len(options.run_id) == 32 and all(char in "0123456789abcdef" for char in options.run_id))
        or not 0 < options.host_pid <= 0xFFFFFFFF
    ):
        return 2
    sys.dont_write_bytecode = True
    channel: PipeChannel | None = None
    try:
        channel = connect_pipe(options.pipe_address, options.host_pid)
        _serve(channel, options.run_id)
        return 0
    except (WindowsRuntimeError, ProtocolError) as exc:
        _report_failure(channel, exc.error_code)
        return 2
    except (OSError, EOFError, SystemExit, KeyboardInterrupt):
        _report_failure(channel, "PLUGIN_WORKER_EXECUTION_FAILED")
        return 2
    except Exception:
        # 第三方入口是异常边界；只返回固定类别，避免冻结窗口模式弹出宿主错误窗或泄漏输入。
        _report_failure(channel, "PLUGIN_WORKER_EXECUTION_FAILED")
        return 2
    finally:
        if channel is not None:
            channel.close()


def _report_failure(channel: PipeChannel | None, code: str) -> None:
    """
    只传递白名单错误类别，不转发插件可控制的异常文本。
    """
    if channel is None:
        return
    allowed = {
        "PLUGIN_CONTENT_CHANGED",
        "PLUGIN_DEPENDENCY_CONFLICT",
        "PLUGIN_PROTOCOL_ERROR",
        "PLUGIN_WORKER_EXECUTION_FAILED",
    }
    try:
        channel.send(WireMessage("error", {"code": code if code in allowed else "PLUGIN_WORKER_EXECUTION_FAILED"}), 1)
    except (OSError, ProtocolError, WindowsRuntimeError):
        # 对端已关闭时没有可交付对象；执行方仍退出，由宿主 Job 负责最后回收。
        return
