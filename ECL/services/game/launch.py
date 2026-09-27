# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：启动协调器：启动流程、进程监视、崩溃候选收集与分析。
#
# 公开接口：
#   - class LaunchCoordinator
#       - launch_instance(body, game_path, source, java_path, memory, width, height, fullscreen, jvm_args, game_args, version_isolation, lock_memory, process_priority) -> dict[str, str] — 检查游戏文件并启动实例。
#       - cancel_launch() -> bool — 取消正在执行的启动或文件补全任务。
#       - list_crash_candidates(game_path, version_id) -> list[dict[str, Any]] — 列出指定实例文件夹内可分析的候选日志文件。
#       - analyze_crash_file(file_path, game_path, version_id) -> dict[str, Any] — 在指定版本上下文中分析用户选择的日志或 ZIP 文件。
#       - get_crash_output(report_id) -> dict[str, str] — 返回当前会话崩溃报告中的脱敏游戏输出。
#       - export_crash_report(report_id, output_path=…) -> dict[str, str] — 导出当前会话内的一份崩溃报告。
#       - get_version_stats(game_path, version_id) -> dict[str, int] — 返回指定版本目录中的运行统计。
#       - list_instances() -> list[dict[str, Any]] — 返回由启动器管理的运行中 Minecraft 实例。
#       - stop_instance(instance_id) -> None — 通知指定的运行中 Minecraft 实例退出，超时后才强制结束。
# ============================================================

import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from threading import Event
from time import monotonic, time
from typing import Any
from uuid import uuid4

import httpx
from anyio import to_thread

from ECL.game import LaunchConfig
from ECL.plugins.launch_hooks import LaunchContext
from ECL.services.authlib import AuthlibError
from ECL.utils.files import atomic_write_text

from .base import GameServiceError, _GameState, _RunningGame

if sys.platform == "win32":
    import winreg
else:
    winreg = None


def _parse_env_vars(text: str) -> tuple[dict[str, str], list[str]]:
    """
    解析多行 ``KEY=VALUE`` 自定义环境变量文本。

    :param text: 用户配置的原始文本，允许空行与行内首尾空白
    :return: (变量字典, 警告列表)；缺少 ``=`` 或键为空的行跳过并记录警告，重复键以最后一次为准
    """
    variables: dict[str, str] = {}
    warnings: list[str] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        key, separator, value = stripped.partition("=")
        key = key.strip()
        if not separator or not key:
            warnings.append(f"忽略无效的环境变量行: {stripped!r}")
            continue
        variables[key] = value.strip()
    return variables, warnings


def _apply_wrapper(wrapper: str, command: str) -> str:
    """
    按包装命令语义拼装最终命令。

    含 ``{}`` 占位符时把占位符替换为完整命令（可多次出现）；否则作为前缀
    以空格拼接到命令前；包装命令为空时原样返回。
    """
    normalized = (wrapper or "").strip()
    if not normalized:
        return command
    if "{}" in normalized:
        return normalized.replace("{}", command)
    return f"{normalized} {command}"


def _render_window_title(template: str, *, instance: str, version: str, account: str) -> str:
    """
    渲染游戏窗口标题模板。

    支持 ``{instance}``/``{version}``/``{account}`` 占位符；未知占位符保留
    原文不展开，避免用户文本中的花括号触发意外替换；模板为空返回空字符串。
    """
    rendered = (template or "").strip()
    if not rendered:
        return ""
    return rendered.replace("{instance}", instance).replace("{version}", version).replace("{account}", account)


class LaunchCoordinator(_GameState):
    pre_launch_command_timeout_seconds = 60
    mesa_loader_windows_version = "26.0.4"
    _renderer_agents = {
        "default": None,
        "software": "llvmpipe",
        "directx12": "d3d12",
        "vulkan": "zink",
    }
    crash_log_markers = (
        "crash report saved to",
        "this crash report has been saved to",
        "could not save crash report",
        "/error]: unable to launch",
        "an exception was thrown, the game will display an error screen and halt",
        "exception_access_violation",
    )
    startup_complete_markers = (
        "sound engine started",
        "openal initialized",
        "created: ",
        "loaded 0 advancements",
        "connecting to ",
        "joining world",
    )

    def _emit_launch_progress(
        self,
        phase: str,
        message: str,
        percent: int | None = None,
        error_code: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {"phase": phase, "message": message}
        if percent is not None:
            payload["percent"] = percent
        if error_code:
            payload["errorCode"] = error_code
        self.events.emit("game:launch_progress", payload)

    def _resolve_java_path(self, value: Any, required_major: int | None = None) -> str:
        raw_path = str(value or "").strip()
        if raw_path:
            path = Path(raw_path).expanduser()
            if not path.is_file():
                raise GameServiceError("Java 可执行文件不存在", "JAVA_NOT_FOUND")
            return str(path.resolve())

        if not self._java_runtimes:
            self.scan_java()
        candidates = self._java_runtimes
        if required_major:
            candidates = [
                runtime for runtime in candidates if self._java_major_version(runtime.version) >= required_major
            ]
        if not candidates:
            if required_major:
                raise GameServiceError(f"未找到 Java {required_major} 或更高版本", "JAVA_VERSION_NOT_FOUND")
            raise GameServiceError("未找到 Java，请先在设置中选择 Java", "JAVA_NOT_FOUND")
        if required_major:
            runtime = min(
                candidates,
                key=lambda item: (self._java_major_version(item.version), str(item.path).casefold()),
            )
        else:
            runtime = max(candidates, key=lambda item: self._java_major_version(item.version))
        return str(runtime.path)

    def _prefer_java_executable(self, java_path: str, use_java_exe: bool) -> str:
        """
        在 Windows 上按需把 javaw.exe 替换为同目录的 java.exe。

        非 Windows 平台、未启用选项或同目录不存在 java.exe 时均保留原路径，
        使该偏好不会阻断正常游戏启动。
        """
        if not use_java_exe or sys.platform != "win32":
            return java_path
        selected = Path(java_path)
        if selected.name.casefold() != "javaw.exe":
            return java_path
        java_executable = selected.with_name("java.exe")
        if java_executable.is_file():
            self.logger.info("按设置使用 java.exe 代替 javaw.exe: %s", java_executable)
            return str(java_executable.resolve())
        self.logger.warning("未找到与 javaw.exe 同目录的 java.exe，继续使用原路径: %s", selected)
        return java_path

    def _set_high_performance_gpu_preference(self, java_path: str) -> None:
        """
        在 Windows 当前用户配置中登记 Java 的高性能 GPU 偏好。

        注册表写入仅影响当前用户；权限或系统策略拒绝写入时记录警告，但不影响
        后续 Java 启动流程。
        """
        if sys.platform != "win32" or winreg is None:
            return
        registry_path = r"Software\Microsoft\DirectX\UserGpuPreferences"
        executable = str(Path(java_path).resolve())
        try:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, registry_path) as key:
                try:
                    current, _ = winreg.QueryValueEx(key, executable)
                except FileNotFoundError:
                    current = ""
                if str(current) == "GpuPreference=2;":
                    return
                winreg.SetValueEx(key, executable, 0, winreg.REG_SZ, "GpuPreference=2;")
                self.logger.info("已设置 Java 高性能 GPU 偏好: %s", executable)
        except OSError as exc:
            self.logger.warning("设置 Java 高性能 GPU 偏好失败，游戏仍会继续启动: %s", exc)

    def _run_pre_launch_command(self, command: str, working_directory: Path) -> None:
        """
        在游戏工作目录执行用户配置的启动前命令，并将失败转换为稳定错误。

        命令受固定超时限制，输出只写入启动器日志；超时、无法创建进程与非零退出
        码都会转换为明确的游戏服务错误。
        """
        normalized = command.strip()
        if not normalized:
            return
        try:
            completed = subprocess.run(
                normalized,
                cwd=working_directory,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self.pre_launch_command_timeout_seconds,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GameServiceError("启动前命令执行超时", "PRE_LAUNCH_COMMAND_TIMEOUT") from exc
        except OSError as exc:
            raise GameServiceError(f"启动前命令无法执行: {exc}", "PRE_LAUNCH_COMMAND_FAILED") from exc
        output = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
        if output:
            self.logger.info("启动前命令输出:\n%s", output)
        if completed.returncode != 0:
            raise GameServiceError(f"启动前命令执行失败，退出码: {completed.returncode}", "PRE_LAUNCH_COMMAND_FAILED")

    @classmethod
    def _normalize_renderer(cls, value: Any) -> str:
        """
        规范化渲染器设置，拒绝无法映射到受控 Java Agent 的值。

        只允许白名单内的标识，以免配置内容被拼接为任意 Java Agent 参数。
        """
        renderer = str(value or "default").strip().casefold()
        if renderer not in cls._renderer_agents:
            raise GameServiceError("渲染器设置无效", "INVALID_GAME_OPTION")
        return renderer

    @staticmethod
    def _mesa_loader_architecture() -> str:
        """
        根据 Windows 系统环境选择 mesa-loader-windows 的 Maven 分类器。

        优先读取 WOW64 的实际系统架构变量，未识别的架构回退到 x64，以匹配
        发布仓库默认提供的分类器。
        """
        architecture = (
            os.environ.get("PROCESSOR_ARCHITEW6432") or os.environ.get("PROCESSOR_ARCHITECTURE") or ""
        ).casefold()
        if architecture in {"x86", "i386", "i486", "i586", "i686"}:
            return "x86"
        if architecture in {"arm64", "aarch64"}:
            return "arm64"
        return "x64"

    async def _ensure_renderer_agent(self, renderer: str) -> str | None:
        """
        确保 Windows 兼容渲染器对应的 Mesa Loader 已就绪并返回 Java Agent 参数。

        运行时资源只保存于启动器数据目录；下载失败时中止启动，避免把指向
        不存在文件的 ``-javaagent`` 交给 Java 后产生难以定位的错误。

        :param renderer: 已规范化的渲染器标识
        :return: 默认/非 Windows 时返回 ``None``，否则返回完整 Java Agent 参数
        :raises GameServiceError: 下载后资源仍不可用时抛出
        """
        agent_mode = self._renderer_agents[renderer]
        if agent_mode is None or sys.platform != "win32":
            return None
        architecture = self._mesa_loader_architecture()
        loader_path = self._data_path / "mesa-loader-windows" / self.mesa_loader_windows_version / "Loader.jar"
        if not loader_path.is_file() or loader_path.stat().st_size <= 0:
            artifact_name = f"mesa-loader-windows-{self.mesa_loader_windows_version}-{architecture}.jar"
            artifact_url = (
                "https://repo.maven.apache.org/maven2/org/glavo/mesa-loader-windows/"
                f"{self.mesa_loader_windows_version}/{artifact_name}"
            )
            self._emit_launch_progress("renderer_loader", "正在准备兼容渲染器组件", 74)
            downloader = self._downloader_factory(
                [(artifact_url, loader_path)],
                progress_callback=lambda done, total: self._emit_launch_progress(
                    "renderer_loader",
                    "正在下载兼容渲染器组件",
                    74 + int(done * 4 / total) if total else 74,
                ),
            )
            with self._lock:
                self._active_downloads["__launch__"] = downloader
            try:
                await downloader.run()
            finally:
                with self._lock:
                    self._active_downloads.pop("__launch__", None)
            if downloader.failed_entries or not loader_path.is_file() or loader_path.stat().st_size <= 0:
                raise GameServiceError("兼容渲染器组件下载失败", "RENDERER_LOADER_DOWNLOAD_FAILED")
        return f'-javaagent:"{loader_path}"={agent_mode}'

    @staticmethod
    def _fallback_required_java(game_version: Any) -> int | None:
        # 在版本 JSON 未声明运行时时，按 Minecraft 基础版本推断最低 Java 主版本。
        match = re.match(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?", str(game_version or "").strip())
        if match is None:
            return None
        version = tuple(int(part or 0) for part in match.groups())
        if version[0] >= 26:
            return 25
        if version[0] != 1:
            return None
        if version >= (1, 20, 5):
            return 21
        if version >= (1, 18, 0):
            return 17
        if version >= (1, 17, 0):
            return 16
        if version >= (1, 7, 10):
            return 8
        return None

    def _known_java_runtime(self, java_path: str) -> Any | None:
        target = str(Path(java_path).resolve(strict=False)).casefold()
        return next(
            (
                runtime
                for runtime in self._java_runtimes
                if str(Path(runtime.path).resolve(strict=False)).casefold() == target
            ),
            None,
        )

    @staticmethod
    def _probe_java_runtime(java_path: str) -> dict[str, str]:
        # 执行一次轻量 Java 属性查询，确认所选运行时可执行且版本可识别。
        try:
            completed = subprocess.run(
                [java_path, "-XshowSettings:properties", "-version"],
                capture_output=True,
                text=True,
                timeout=8,
                encoding="utf-8",
                errors="ignore",
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise GameServiceError(f"无法运行所选 Java：{exc}", "JAVA_RUNTIME_INVALID") from exc
        output = "\n".join((completed.stdout or "", completed.stderr or ""))
        properties: dict[str, str] = {}
        for line in output.splitlines():
            if "=" not in line:
                continue
            key, value = line.strip().split("=", 1)
            properties[key.strip()] = value.strip()
        version = properties.get("java.version", "")
        if not version:
            version_match = re.search(r'(?im)^(?:java|openjdk) version\s+"([^"]+)"', output)
            version = version_match.group(1) if version_match else ""
        if not version:
            raise GameServiceError("无法识别所选 Java 的版本信息", "JAVA_RUNTIME_INVALID")
        return {"version": version, "architecture": properties.get("os.arch", "unknown")}

    def _validate_launch_environment(
        self,
        java_path: str,
        required_java: int | None,
        memory: int,
    ) -> None:
        # 在创建 Minecraft 进程前检查可确定的 Java 版本和架构兼容性。
        runtime = self._known_java_runtime(java_path)
        if runtime is not None:
            version = str(runtime.version)
            architecture = str(runtime.architecture or "unknown")
        elif required_java is not None:
            probed = self._probe_java_runtime(java_path)
            version = probed["version"]
            architecture = probed["architecture"]
        else:
            return

        actual_java = self._java_major_version(version)
        if actual_java <= 0:
            raise GameServiceError("无法识别所选 Java 的主版本", "JAVA_RUNTIME_INVALID")
        if required_java is not None and actual_java < required_java:
            raise GameServiceError(
                f"该实例至少需要 Java {required_java}，当前选择的是 Java {actual_java}",
                "JAVA_VERSION_INCOMPATIBLE",
            )

        arch_key = architecture.casefold().replace("-", "_")
        if arch_key in {"x86", "i386", "i486", "i586", "i686"} and memory > 1536:
            raise GameServiceError(
                f"当前选择的是 32 位 Java，无法可靠分配 {memory} MiB 内存；请改用 64 位 Java 或降低内存",
                "JAVA_ARCH_MEMORY_LIMIT",
            )

    def _apply_fullscreen_option(self, game_directory: Path, enabled: bool) -> None:
        # Minecraft 无全屏命令行参数，启动前把 options.txt 的 fullscreen 键写为目标状态，
        # 由游戏启动时读取该值决定窗口模式；写入失败不阻断启动，沿用游戏内既有状态。
        options_path = game_directory / "options.txt"
        value = "true" if enabled else "false"
        try:
            lines = (
                options_path.read_text(encoding="utf-8", errors="replace").splitlines()
                if options_path.is_file()
                else []
            )
        except OSError:
            lines = []
        for index, line in enumerate(lines):
            if line.startswith("fullscreen:"):
                lines[index] = f"fullscreen:{value}"
                break
        else:
            lines.append(f"fullscreen:{value}")
        try:
            atomic_write_text(options_path, "\n".join(lines) + "\n")
        except OSError as exc:
            self.logger.warning("写入全屏设置失败，游戏将沿用既有窗口状态: %s", exc)

    async def launch_instance(  # noqa: C901 - launch transaction and cleanup boundary
        self,
        body: Mapping[str, object],
        *,
        game_path: Any,
        source: Any = "official",
        java_path: Any = None,
        memory: Any = 4096,
        width: Any = 854,
        height: Any = 480,
        fullscreen: Any = False,
        jvm_args: Any = None,
        game_args: Any = None,
        version_isolation: Any = None,
        lock_memory: Any = False,
        process_priority: Any = "normal",
        pre_launch_command: Any = None,
        renderer: Any = "default",
        prefer_high_performance_gpu: Any = False,
        use_java_exe: Any = False,
        disable_crash_analysis: Any = False,
    ) -> dict[str, str]:
        """
        检查游戏文件并启动实例。

        :param body: 经过边界校验的 IPC 请求数据
        :param game_path: Minecraft 游戏根目录
        :param source: 下载源名称，如 ``official`` 或 ``bmclapi``
        :param java_path: Java 可执行文件路径
        :param memory: 分配给游戏的内存大小，单位为 MiB
        :param width: 游戏窗口宽度
        :param height: 游戏窗口高度
        :param fullscreen: 启动时是否进入全屏模式
        :param jvm_args: 附加的 JVM 参数列表
        :param game_args: 附加的 Minecraft 参数列表
        :param version_isolation: 是否启用版本目录隔离
        :param lock_memory: 是否锁定 JVM 初始堆与最大堆一致（-Xms=-Xmx）
        :param process_priority: 游戏进程优先级: idle / below_normal / normal / above_normal / high
        :param pre_launch_command: 创建游戏进程前在实例工作目录执行的命令
        :param renderer: Windows 下使用的兼容渲染器类型
        :param prefer_high_performance_gpu: 是否在 Windows 中登记 Java 的高性能 GPU 偏好
        :param use_java_exe: 是否在 Windows 中将 javaw.exe 替换为 java.exe
        :param disable_crash_analysis: 是否禁止本次运行异常退出后的自动崩溃分析
        """
        version_name = self._normalize_version_name(body.get("version_id"))
        path = self._normalize_game_path(game_path)
        self.logger.debug(
            "准备启动实例: version=%s, path=%s, memory=%s, java=%s",
            version_name,
            path,
            memory,
            java_path,
        )
        version_json = path / "versions" / version_name / f"{version_name}.json"
        if not version_json.is_file():
            raise GameServiceError("游戏实例不存在或版本 JSON 缺失", "VERSION_NOT_FOUND")
        try:
            version_document = json.loads(version_json.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise GameServiceError("实例版本 JSON 无法读取或已经损坏", "VERSION_JSON_INVALID") from exc
        inherited_version = str(version_document.get("inheritsFrom") or "").strip()
        scanned_versions = self._search_factory(path).search_minecraft()
        version_info = scanned_versions.get(version_name, {}) if isinstance(scanned_versions, dict) else {}
        loader = str(version_info.get("LoaderType") or "Vanilla").strip() or "Vanilla"
        required_java_value = str(version_info.get("RequestJava") or "")
        required_java = int(required_java_value) if required_java_value.isdigit() else None
        if required_java is None:
            required_java = self._fallback_required_java(version_info.get("VanillaVersion"))
        ram = self._normalize_positive_int(memory, 4096, 256, 131072, "游戏内存")
        window_width = self._normalize_positive_int(width, 854, 320, 16384, "窗口宽度")
        window_height = self._normalize_positive_int(height, 480, 240, 16384, "窗口高度")
        fullscreen_enabled = bool(fullscreen)
        custom_jvm_args = self._normalize_string_list(jvm_args, "JVM 参数")
        custom_game_args = self._normalize_string_list(game_args, "游戏参数")
        lock_memory_enabled = bool(lock_memory)
        command_before_launch = str(pre_launch_command or "").strip()
        selected_renderer = self._normalize_renderer(renderer)
        high_performance_gpu = bool(prefer_high_performance_gpu)
        use_console_java = bool(use_java_exe)
        crash_analysis_disabled = bool(disable_crash_analysis)
        normalized_priority = self._normalize_process_priority(process_priority)
        normalized_source = self._normalize_source(source)
        context = self._context(path, normalized_source)
        isolated = self.resolve_version_isolation(path, version_name, version_isolation)
        # 插件启动钩子需要访问最终游戏目录，提前计算以避免在命令构建后再移动。
        game_directory = path / "versions"
        # 版本隔离=开 时该实例使用独立的 versions/<版本名>/ 数据目录，与设置项文案一致
        if isolated:
            game_directory /= version_name

        cancel_event = Event()
        with self._lock:
            if self._launch_cancel_event is not None:
                raise GameServiceError("已有游戏启动任务正在运行", "LAUNCH_ALREADY_RUNNING")
            self._launch_cancel_event = cancel_event

        try:
            self._emit_launch_progress("preparing", f"正在准备启动 {version_name}", 3)
            current_account_getter = getattr(self.accounts, "current_account", None)
            current_account = current_account_getter() if callable(current_account_getter) else None
            account_type = current_account.get("type") if isinstance(current_account, dict) else None
            if account_type == "microsoft":
                self._emit_launch_progress(
                    "microsoft_token",
                    "正在检查正版登录令牌，过期时将自动刷新",
                    7,
                )
            elif account_type == "authlib":
                self._emit_launch_progress(
                    "authlib_token",
                    "正在验证外置登录令牌，过期时将自动刷新",
                    7,
                )
            elif account_type == "offline":
                self._emit_launch_progress("offline_account", "正在读取离线账户信息", 7)
            else:
                self._emit_launch_progress("account", "正在验证游戏账户", 7)
            credentials = await self.accounts.get_launch_credentials()
            if credentials["user_type"] == "msa":
                self._emit_launch_progress("account_ready", "正版登录令牌已就绪", 17)
            elif credentials["user_type"] == "yggdrasil":
                self._emit_launch_progress("account_ready", "外置登录令牌已就绪", 17)
            else:
                self._emit_launch_progress("account_ready", "离线账户已就绪", 17)
            if cancel_event.is_set():
                raise GameServiceError("启动已取消", "LAUNCH_CANCELLED")

            authlib_path = None
            auth_server = None
            if credentials["user_type"] == "yggdrasil":
                if self.authlib_injector is None:
                    raise GameServiceError("未配置外置登录组件目录", "AUTHLIB_INJECTOR_UNAVAILABLE")
                auth_server = credentials.get("auth_server")
                if not auth_server:
                    raise GameServiceError("外置登录认证服务器地址缺失", "AUTHLIB_SERVER_MISSING")

            # 登录方式无关：始终确保 authlib-injector 组件就绪。正版/离线账号并不使用该
            # 组件，这里仅作静默预下载，保证将来切换外置登录时首次启动不再拉取。
            authlib_name = "下载 authlib-injector.jar"
            authlib_task_id = None
            if self.authlib_injector is not None:
                self._emit_launch_progress("authlib", "正在准备外置登录组件", 20)
                try:
                    if self.authlib_injector.needs_download():
                        authlib_task_id = f"authlib-{uuid4().hex}"
                        self.events.emit(
                            "game:install_progress",
                            {
                                "phase": "download",
                                "task_id": authlib_task_id,
                                "name": authlib_name,
                                "message": "正在下载 authlib-injector.jar",
                                "done": 0,
                                "total": 1,
                                "progress_type": "files",
                                "total_files": 1,
                                "downloaded_files": 0,
                                "speed": 0,
                                "subtask": "download_files",
                            },
                        )
                    authlib_path = await to_thread.run_sync(self.authlib_injector.ensure)
                    if authlib_task_id is not None:
                        self.events.emit(
                            "game:install_progress",
                            {
                                "phase": "done",
                                "task_id": authlib_task_id,
                                "name": authlib_name,
                                "message": "authlib-injector.jar 下载完成",
                                "done": 1,
                                "total": 1,
                                "total_files": 1,
                                "downloaded_files": 1,
                                "speed": 0,
                            },
                        )
                except (AuthlibError, OSError, KeyError, TypeError, ValueError, httpx.HTTPError) as exc:
                    if authlib_task_id is not None:
                        self.events.emit(
                            "game:install_progress",
                            {
                                "phase": "error",
                                "task_id": authlib_task_id,
                                "name": authlib_name,
                                "message": f"下载 authlib-injector.jar 失败: {exc}",
                                "done": 0,
                                "total": 1,
                            },
                        )
                    # 正版/离线下 authlib 仅预下载，失败不阻断启动；外置登录必须就绪否则中断。
                    if credentials["user_type"] == "yggdrasil":
                        raise GameServiceError(f"准备外置登录组件失败: {exc}", "AUTHLIB_INJECTOR_FAILED") from exc

            self._emit_launch_progress("environment_check", "正在校验 Java 与实例运行环境", 22)
            java = await to_thread.run_sync(self._resolve_java_path, java_path, required_java)
            java = self._prefer_java_executable(java, use_console_java)
            await to_thread.run_sync(
                self._validate_launch_environment,
                java,
                required_java,
                ram,
            )
            self._emit_launch_progress("environment_ready", "Java 与实例运行环境校验完成", 24)

            self._emit_launch_progress("checking", "正在检查游戏文件", 25)
            if inherited_version:
                inherited_candidates = (
                    path / "versions" / version_name / f"{inherited_version}.json",
                    path / "versions" / inherited_version / f"{inherited_version}.json",
                )
                if not any(candidate.is_file() for candidate in inherited_candidates):
                    self._emit_launch_progress(
                        "inherited_version",
                        f"正在补全基础版本 {inherited_version}",
                        28,
                    )
                    try:
                        await to_thread.run_sync(
                            context.games.build_minecraft_download_list,
                            inherited_version,
                            version_name,
                            False,
                        )
                    except Exception as exc:
                        raise GameServiceError(
                            f"实例依赖基础版本 {inherited_version}，但其版本元数据补全失败：{exc}",
                            "INHERITED_VERSION_DOWNLOAD_FAILED",
                        ) from exc
                    embedded_json = path / "versions" / version_name / f"{inherited_version}.json"
                    if not embedded_json.is_file():
                        raise GameServiceError(
                            f"实例缺少基础版本 {inherited_version} 的版本元数据",
                            "INHERITED_VERSION_MISSING",
                        )
            download_list = await to_thread.run_sync(context.files_checker.check_files, path, version_name)
            if cancel_event.is_set():
                raise GameServiceError("启动已取消", "LAUNCH_CANCELLED")
            self._emit_launch_progress(
                "files_checked",
                f"文件检查完成，共需补全 {len(download_list)} 个文件",
                55,
            )
            if download_list:
                downloader = self._downloader_factory(
                    download_list,
                    progress_callback=lambda done, total: self._emit_launch_progress(
                        "downloading",
                        "正在补全游戏文件",
                        55 + int(done * 15 / total) if total else 55,
                    ),
                )
                with self._lock:
                    self._active_downloads["__launch__"] = downloader
                try:
                    await downloader.run()
                    failed_entries = set(downloader.failed_entries)
                    if failed_entries and not cancel_event.is_set():
                        fallback_entries = await to_thread.run_sync(
                            self._fallback_download_entries,
                            path,
                            version_name,
                            normalized_source,
                            failed_entries,
                            getattr(downloader, "local_failed_paths", set()),
                        )
                        if fallback_entries and not cancel_event.is_set():
                            completed_count = len(downloader.completed_entries)
                            self._emit_launch_progress("downloading", "首选下载源失败，正在尝试备用源", 55)
                            fallback_downloader = self._downloader_factory(
                                fallback_entries,
                                progress_callback=lambda done, total: self._emit_launch_progress(
                                    "downloading",
                                    "正在从备用源补全游戏文件",
                                    55 + int((completed_count + done) * 15 / len(download_list)),
                                ),
                            )
                            with self._lock:
                                self._active_downloads["__launch__"] = fallback_downloader
                            await fallback_downloader.run()
                            fallback_failed_paths = {file_path for _, file_path in fallback_downloader.failed_entries}
                            recovered_paths = {file_path for _, file_path in fallback_entries} - fallback_failed_paths
                            failed_entries = {
                                (url, file_path)
                                for url, file_path in failed_entries
                                if file_path not in recovered_paths
                            }
                except Exception as exc:
                    if cancel_event.is_set():
                        raise GameServiceError("启动已取消", "LAUNCH_CANCELLED") from exc
                    raise
                finally:
                    with self._lock:
                        self._active_downloads.pop("__launch__", None)
                if cancel_event.is_set():
                    raise GameServiceError("启动已取消", "LAUNCH_CANCELLED")
                if failed_entries:
                    failed_url, failed_path = next(iter(failed_entries))
                    raise GameServiceError(
                        f"有 {len(failed_entries)} 个游戏文件补全失败，例如 {failed_path}（{failed_url}）",
                        "GAME_DOWNLOAD_FAILED",
                    )

            self._apply_fullscreen_option(game_directory, fullscreen_enabled)
            renderer_agent = await self._ensure_renderer_agent(selected_renderer)
            if cancel_event.is_set():
                raise GameServiceError("启动已取消", "LAUNCH_CANCELLED")
            self._emit_launch_progress("building_args", "正在生成启动参数", 72)
            launch_context = LaunchContext(
                version_id=version_name,
                loader=loader,
                game_path=path,
                game_directory=game_directory,
                version_isolation=isolated,
                jvm_args=list(custom_jvm_args),
                game_args=list(custom_game_args),
            )
            self.launch_hooks.prepare(launch_context)
            if renderer_agent is not None:
                launch_context.jvm_args.insert(0, renderer_agent)
            launch_config = LaunchConfig(
                java_path=java,
                game_path=path,
                version_name=version_name,
                use_ram=ram,
                lock_memory=lock_memory_enabled,
                player_name=credentials["player_name"],
                auth_uuid=credentials["uuid"],
                user_type=credentials["user_type"],
                access_token=credentials["access_token"],
                custom_jvm_params=launch_context.jvm_args or None,
                version_isolation=isolated,
                window_width=window_width,
                window_height=window_height,
                authlib_path=authlib_path,
                yggdrasil_api=auth_server,
            )
            command = await to_thread.run_sync(self._command_builder, launch_config)
            if launch_context.game_args:
                if sys.platform == "win32":
                    formatted_args = subprocess.list2cmdline(launch_context.game_args)
                else:
                    formatted_args = shlex.join(launch_context.game_args)
                command = f"{command} {formatted_args}"
            self._emit_launch_progress("args_built", "启动参数生成完成", 84)
            if cancel_event.is_set():
                raise GameServiceError("启动已取消", "LAUNCH_CANCELLED")

            self._emit_launch_progress("about_to_launch", "即将启动游戏", 94)
            if command_before_launch:
                self._emit_launch_progress("pre_launch_command", "正在执行启动前命令", 95)
                await to_thread.run_sync(self._run_pre_launch_command, command_before_launch, game_directory)
            if high_performance_gpu:
                await to_thread.run_sync(self._set_high_performance_gpu_preference, java)
            self._emit_launch_progress("launching", "正在创建游戏进程", 97)
            run_token = uuid4().hex
            run = _RunningGame(
                token=run_token,
                version_id=version_name,
                loader=loader,
                game_path=path,
                game_directory=game_directory,
                started_at=monotonic(),
                started_wall_time=time(),
                crash_analysis_disabled=crash_analysis_disabled,
            )
            with self._lock:
                self._running_games[run_token] = run
            self.launch_hooks.pre_launch(launch_context)
            try:

                def on_instance_exit(code: int, name: str) -> None:
                    self._handle_instance_exit(run_token, code, name)
                    self.launch_hooks.on_exit(launch_context)

                # 插件 env 是覆写/追加语义，必须合并进父进程环境后再传给子进程，
                # 否则游戏进程会丢失 PATH/SystemRoot 等系统变量导致启动失败。
                instance_env = {**os.environ, **launch_context.env} if launch_context.env else None
                instance_id, _process = self.instances.create_instance(
                    instance_name=version_name,
                    instance_type="Minecraft",
                    args=command,
                    cwd=launch_context.working_directory or (path / "versions" / version_name),
                    new_session=True,
                    env=instance_env,
                    priority=normalized_priority,
                    log_callback=lambda line, current_id: self._handle_instance_log(run_token, line, current_id),
                    exit_callback=on_instance_exit,
                )
            except Exception:
                with self._lock:
                    self._running_games.pop(run_token, None)
                raise
            self.launch_hooks.post_launch(launch_context)

            with self._lock:
                registered_run = self._running_games.get(run_token)
                if registered_run is not None:
                    registered_run.instance_id = instance_id
            self._version_stats.record_launch(path, version_name)
            with self._lock:
                registered_run = self._running_games.get(run_token)
                if registered_run is not None:
                    registered_run.pending = False
                    already_exited = registered_run.exited
                else:
                    already_exited = True
            if already_exited:
                self._finalize_instance_run(run_token, action="exited")
            else:
                self._emit_instance_change(run, "started")
            self._emit_launch_progress("launched", f"{version_name} 已启动", 100)
            return {
                "instanceId": instance_id,
                "versionId": version_name,
                "gamePath": str(path),
            }
        except GameServiceError as exc:
            if exc.error_code != "LAUNCH_CANCELLED":
                self._emit_launch_progress("error", str(exc), 0, exc.error_code)
            raise
        except Exception as exc:
            self.logger.exception("启动 Minecraft 失败")
            error = GameServiceError(f"启动游戏失败: {exc}", "GAME_LAUNCH_FAILED")
            self._emit_launch_progress("error", str(error), 0, error.error_code)
            raise error from exc
        finally:
            with self._lock:
                self._launch_cancel_event = None

    def cancel_launch(self) -> bool:
        """
        取消正在执行的启动或文件补全任务。
        """
        with self._lock:
            cancel_event = self._launch_cancel_event
            downloader = self._active_downloads.get("__launch__")
        if cancel_event is None:
            return False
        cancel_event.set()
        if downloader is not None:
            downloader.stop()
        return True

    def _emit_instance_change(self, run: _RunningGame, action: str) -> None:
        if not run.instance_id:
            return
        self.events.emit(
            "game:instances_changed",
            {
                "action": action,
                "instanceId": run.instance_id,
                "versionId": run.version_id,
                "gamePath": str(run.game_path),
            },
        )

    def _handle_instance_log(self, run_token: str, line: str, instance_id: str) -> None:
        # 缓冲单个游戏进程的近期输出，并记录不依赖退出码的生命周期信号。
        normalized = str(line or "").rstrip("\r\n")
        self.logger.debug("[%s] %s", instance_id, normalized)
        folded = normalized.casefold()
        with self._lock:
            run = self._running_games.get(run_token)
            if run is None:
                return
            run.output_lines.append(normalized)
            if any(marker in folded for marker in self.crash_log_markers):
                run.crash_marked = True
            if any(marker in folded for marker in self.startup_complete_markers):
                run.startup_complete = True

    def _handle_instance_exit(self, run_token: str, exit_code: int, instance_name: str) -> None:
        # 接收进程管理器线程的唯一退出通知，并在启动注册完成后结算统计。
        self.logger.info("Minecraft %s 已退出，退出码: %s", instance_name, exit_code)
        with self._lock:
            run = self._running_games.get(run_token)
            if run is None:
                return
            run.exited = True
            run.exit_code = exit_code
            if run.pending:
                return
            action = "stopped" if run.stopping else "exited"
        self._finalize_instance_run(run_token, action=action)

    def _finalize_instance_run(self, run_token: str, *, action: str) -> None:
        # 从内存运行表移除一次运行，并恰好一次地累计已观察时长。
        with self._lock:
            run = self._running_games.get(run_token)
            if run is None or run.pending:
                return
            self._running_games.pop(run_token, None)
        duration_seconds = max(0, int(monotonic() - run.started_at))
        self._version_stats.record_duration(run.game_path, run.version_id, duration_seconds)
        self.logger.debug(
            "游戏运行已结算: version=%s, action=%s",
            run.version_id,
            action,
        )
        if action != "launcher_closed":
            self._emit_instance_change(run, action)
        detected_by = self._crash_detection_signals(run, action)
        if detected_by and not run.crash_analysis_disabled:
            self._schedule_crash_analysis(run, detected_by)

    @staticmethod
    def _crash_detection_signals(run: _RunningGame, action: str) -> list[str]:
        if action != "exited" or run.stopping or run.exit_code is None:
            return []
        signals = []
        if run.exit_code != 0:
            signals.append("exit_code")
        if run.crash_marked:
            signals.append("crash_log")
        if not run.startup_complete:
            signals.append("startup_incomplete")
        return signals

    def _schedule_crash_analysis(self, run: _RunningGame, detected_by: list[str]) -> None:
        # 将文件收集和规则分析交给 GameService 拥有的后台执行器。
        with self._lock:
            if self._closing:
                return
        future = self._crash_executor.submit(
            self._crash_analyzer.analyze_runtime,
            version_id=run.version_id,
            game_path=run.game_path,
            game_directory=run.game_directory,
            started_wall_time=run.started_wall_time,
            output_lines=list(run.output_lines),
            exit_code=int(run.exit_code or 0),
            detected_by=detected_by,
        )
        with self._lock:
            self._crash_futures.add(future)

        def analysis_done(completed) -> None:
            with self._lock:
                self._crash_futures.discard(completed)
                closing = self._closing
            if closing or completed.cancelled():
                return
            try:
                result = completed.result()
            except Exception:
                error_id = uuid4().hex
                self.logger.exception(
                    "Minecraft 崩溃分析失败: version=%s, error_id=%s",
                    run.version_id,
                    error_id,
                )
                self.events.emit(
                    "launcher:error",
                    {
                        "error_id": error_id,
                        "title": "Minecraft 实例崩溃",
                        "message": f"实例“{run.version_id}”异常退出，但崩溃报告生成失败",
                    },
                )
                return
            report_id = str(result["reportId"])
            exit_code = result.get("exitCode")
            self.events.emit(
                "launcher:error",
                {
                    "error_id": report_id,
                    "title": "Minecraft 实例崩溃",
                    "message": f"实例“{run.version_id}”异常退出，退出码：{exit_code}",
                    "kind": "game_crash",
                    "crash": result,
                },
            )
            self.logger.warning(
                "Minecraft 崩溃分析完成: version=%s, exit_code=%s, report_id=%s",
                run.version_id,
                exit_code,
                report_id,
            )

        future.add_done_callback(analysis_done)

    def list_crash_candidates(self, game_path: Any, version_id: Any) -> list[dict[str, Any]]:
        """
        列出指定实例文件夹内可分析的候选日志文件。
        """
        path = self._normalize_game_path(game_path)
        version = self._normalize_version_name(version_id)
        game_directory = path / "versions" / version
        return self._crash_analyzer.candidate_files(path, version, game_directory)

    def analyze_crash_file(self, file_path: Any, game_path: Any, version_id: Any) -> dict[str, Any]:
        """
        在指定版本上下文中分析用户选择的日志或 ZIP 文件。

        :param file_path: 用户明确选择的本地文件
        :param game_path: Minecraft 游戏根目录
        :param version_id: 用于报告展示和 Mod 关联的版本名称
        :return: 结构化崩溃分析结果
        """
        path = self._normalize_game_path(game_path)
        version = self._normalize_version_name(version_id)
        source = Path(str(file_path)).expanduser().resolve(strict=False)
        return self._crash_analyzer.analyze_file(source, path, version)

    def get_crash_output(self, report_id: Any) -> dict[str, str]:
        """
        返回当前会话崩溃报告中的脱敏游戏输出。

        :param report_id: 当前会话报告编号
        :return: 输出文件名和文本内容
        """
        if not isinstance(report_id, str) or not report_id.strip():
            raise GameServiceError("崩溃报告编号不能为空", "INVALID_CRASH_REPORT_ID")
        return self._crash_analyzer.output(report_id.strip())

    def export_crash_report(self, report_id: Any, output_path: Any = None) -> dict[str, str]:
        """
        导出当前会话内的一份崩溃报告。

        :param report_id: 当前会话报告编号
        :param output_path: 可选 ZIP 输出路径
        :return: 导出文件的绝对路径
        """
        if not isinstance(report_id, str) or not report_id.strip():
            raise GameServiceError("崩溃报告编号不能为空", "INVALID_CRASH_REPORT_ID")
        target = None
        if output_path is not None:
            if not isinstance(output_path, (str, Path)) or not str(output_path).strip() or "\0" in str(output_path):
                raise GameServiceError("崩溃报告导出路径无效", "INVALID_PATH")
            target = Path(str(output_path)).expanduser().resolve(strict=False)
        return self._crash_analyzer.export(report_id.strip(), target)

    def get_version_stats(self, game_path: Any, version_id: Any) -> dict[str, int]:
        """
        返回指定版本目录中的运行统计。

        :param game_path: Minecraft 游戏根目录
        :param version_id: 版本目录名称
        :return: 启动次数、上次运行秒数和总运行秒数
        """
        path = self._normalize_game_path(game_path)
        name = self._normalize_version_name(version_id)
        if not (path / "versions" / name).is_dir():
            raise GameServiceError("游戏实例不存在", "VERSION_NOT_FOUND")
        return dict(self._version_stats.read(path, name))

    def list_instances(self) -> list[dict[str, Any]]:
        """
        返回由启动器管理的运行中 Minecraft 实例。
        """
        raw_instances = self.instances.get_instances_info()
        with self._lock:
            runs_by_instance = {
                run.instance_id: (token, run)
                for token, run in self._running_games.items()
                if run.instance_id is not None and not run.pending
            }

        result = []
        for item in raw_instances:
            if item.get("Type") != "Minecraft":
                continue
            instance_id = str(item.get("ID") or "")
            process = item.get("Instance")
            mapped = runs_by_instance.get(instance_id)
            exit_code = process.poll() if process is not None else None
            is_running = bool(process is not None and exit_code is None)
            if not is_running:
                if mapped is not None:
                    token, run = mapped
                    if isinstance(exit_code, int):
                        run.exit_code = exit_code
                    self._finalize_instance_run(token, action="stopped" if run.stopping else "exited")
                continue
            run = mapped[1] if mapped is not None else None
            version_id = run.version_id if run is not None else str(item.get("Name") or "")
            process_id = getattr(process, "pid", None)
            result.append(
                {
                    "id": instance_id,
                    "name": str(item.get("Name") or version_id or "Minecraft"),
                    "type": "Minecraft",
                    "isRunning": True,
                    "pid": int(process_id) if isinstance(process_id, int) else None,
                    "version": version_id,
                    "versionId": version_id,
                    "loader": run.loader if run is not None else "Vanilla",
                    "gamePath": str(run.game_path) if run is not None else "",
                }
            )
        return result

    def stop_instance(self, instance_id: Any) -> None:
        """
        通知指定的运行中 Minecraft 实例退出，超时后才强制结束。

        :param instance_id: 运行中游戏实例的标识
        """
        if not isinstance(instance_id, str) or not instance_id.strip():
            raise GameServiceError("实例 ID 不能为空", "INVALID_INSTANCE_ID")
        existing_ids = {
            str(item.get("ID")) for item in self.instances.get_instances_info() if item.get("Type") == "Minecraft"
        }
        if instance_id not in existing_ids:
            raise GameServiceError("游戏实例不存在", "INSTANCE_NOT_FOUND")
        with self._lock:
            matched = next(
                (
                    (token, run)
                    for token, run in self._running_games.items()
                    if run.instance_id == instance_id and not run.pending
                ),
                None,
            )
            if matched is not None:
                matched[1].stopping = True
        self.instances.stop_instance(instance_id, wait_timeout=3.0)
        if matched is not None:
            self._finalize_instance_run(matched[0], action="stopped")
