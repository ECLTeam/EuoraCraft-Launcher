# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：提供 Windows 原生进程和命名管道边界，导入时不读取平台专属 API。
#
# 公开接口：
#   - class WindowsRuntimeError — 原生操作失败及稳定错误码。
#   - class WindowsApi — 延迟绑定 Windows API、显式资源句柄和身份查询。
# ============================================================

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from pathlib import Path


class WindowsRuntimeError(RuntimeError):
    """
    保存原生错误类别，不输出命令、目录或用户 SID。
    """

    def __init__(self, message: str, error_code: str, native_error: int = 0) -> None:
        """
        保存用于诊断的系统错误与对外稳定错误码。

        :param message: 不包含敏感输入的规则说明
        :param error_code: 插件运行协议的稳定错误码
        :param native_error: Windows 错误值，不作为插件身份
        """
        super().__init__(message)
        self.error_code: str = error_code
        self.native_error: int = native_error


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", wintypes.LPVOID),
        ("bInheritHandle", wintypes.BOOL),
    ]


class _StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _ProcessInfo(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _JobAccounting(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_int64)
        for name in ("TotalUserTime", "TotalKernelTime", "ThisPeriodTotalUserTime", "ThisPeriodTotalKernelTime")
    ] + [
        (name, wintypes.DWORD)
        for name in ("TotalPageFaultCount", "TotalProcesses", "ActiveProcesses", "TotalTerminatedProcesses")
    ]


class _TokenUser(ctypes.Structure):
    _fields_ = [("Sid", wintypes.LPVOID), ("Attributes", wintypes.DWORD)]


class WindowsApi:
    """
    在 Windows 上按明确签名调用系统接口，句柄由调用方关闭。

    非 Windows 环境可以导入模块，但创建实例立即拒绝运行，不降级无监管。
    """

    def __init__(self) -> None:
        """
        延迟加载内核及安全接口，不在模块导入时访问 Windows 属性。

        :raises WindowsRuntimeError: 当前平台不受支持时抛出
        """
        if sys.platform != "win32":
            raise WindowsRuntimeError("当前平台尚不支持插件 Worker", "PLUGIN_RUNTIME_UNSUPPORTED")
        self.kernel: ctypes.CDLL = ctypes.WinDLL("kernel32", use_last_error=True)
        self.security: ctypes.CDLL = ctypes.WinDLL("advapi32", use_last_error=True)
        self._bind_kernel()
        self._bind_security()

    def _bind_kernel(self) -> None:
        pointer = wintypes.LPVOID
        dword_pointer = ctypes.POINTER(wintypes.DWORD)
        signatures = (
            ("CreateJobObjectW", wintypes.HANDLE, (pointer, wintypes.LPCWSTR)),
            ("SetInformationJobObject", wintypes.BOOL, (wintypes.HANDLE, ctypes.c_int, pointer, wintypes.DWORD)),
            ("AssignProcessToJobObject", wintypes.BOOL, (wintypes.HANDLE, wintypes.HANDLE)),
            ("IsProcessInJob", wintypes.BOOL, (wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL))),
            ("TerminateJobObject", wintypes.BOOL, (wintypes.HANDLE, wintypes.UINT)),
            (
                "QueryInformationJobObject",
                wintypes.BOOL,
                (wintypes.HANDLE, ctypes.c_int, pointer, wintypes.DWORD, pointer),
            ),
            (
                "CreateProcessW",
                wintypes.BOOL,
                (
                    wintypes.LPCWSTR,
                    wintypes.LPWSTR,
                    pointer,
                    pointer,
                    wintypes.BOOL,
                    wintypes.DWORD,
                    pointer,
                    wintypes.LPCWSTR,
                    pointer,
                    pointer,
                ),
            ),
            ("ResumeThread", wintypes.DWORD, (wintypes.HANDLE,)),
            ("CloseHandle", wintypes.BOOL, (wintypes.HANDLE,)),
            ("TerminateProcess", wintypes.BOOL, (wintypes.HANDLE, wintypes.UINT)),
            ("WaitForSingleObject", wintypes.DWORD, (wintypes.HANDLE, wintypes.DWORD)),
            ("GetExitCodeProcess", wintypes.BOOL, (wintypes.HANDLE, dword_pointer)),
            ("OpenProcess", wintypes.HANDLE, (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)),
            ("GetProcessTimes", wintypes.BOOL, (wintypes.HANDLE, pointer, pointer, pointer, pointer)),
            (
                "QueryFullProcessImageNameW",
                wintypes.BOOL,
                (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, dword_pointer),
            ),
            ("GetNamedPipeClientProcessId", wintypes.BOOL, (wintypes.HANDLE, dword_pointer)),
            ("GetNamedPipeServerProcessId", wintypes.BOOL, (wintypes.HANDLE, dword_pointer)),
            ("GetCurrentProcess", wintypes.HANDLE, ()),
            ("LocalFree", pointer, (pointer,)),
            ("CancelIoEx", wintypes.BOOL, (wintypes.HANDLE, pointer)),
        )
        for name, result, arguments in signatures:
            function = getattr(self.kernel, name)
            function.restype = result
            function.argtypes = arguments

    def _bind_security(self) -> None:
        pointer = wintypes.LPVOID
        signatures = (
            ("OpenProcessToken", wintypes.BOOL, (wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))),
            (
                "GetTokenInformation",
                wintypes.BOOL,
                (wintypes.HANDLE, ctypes.c_int, pointer, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)),
            ),
            ("ConvertSidToStringSidW", wintypes.BOOL, (pointer, ctypes.POINTER(wintypes.LPWSTR))),
            (
                "ConvertStringSecurityDescriptorToSecurityDescriptorW",
                wintypes.BOOL,
                (wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(pointer), pointer),
            ),
        )
        for name, result, arguments in signatures:
            function = getattr(self.security, name)
            function.restype = result
            function.argtypes = arguments

    @staticmethod
    def check(result: object, error_code: str) -> None:
        """
        将原生失败转换为不包含输入的稳定异常。

        :param result: Windows API 的成功标识
        :param error_code: 当前操作的领域错误码
        :raises WindowsRuntimeError: 调用失败时抛出
        """
        if not result:
            raise WindowsRuntimeError("Windows 插件运行操作失败", error_code, ctypes.get_last_error())

    def close(self, handle: int) -> None:
        """
        释放调用方持有的一个有效内核句柄。

        :param handle: 不再使用的句柄
        """
        self.check(self.kernel.CloseHandle(handle), "PLUGIN_HANDLE_CLOSE_FAILED")

    def create_job(self) -> int:
        """
        建立不继承的匿名 Job，最后句柄关闭时回收普通派生链。

        :return: 调用方必须关闭的 Job 句柄
        :raises WindowsRuntimeError: 创建或设置失败时抛出
        """
        handle = self.kernel.CreateJobObjectW(None, None)
        self.check(handle, "PLUGIN_JOB_CREATE_FAILED")
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000
        try:
            self.check(
                self.kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)),
                "PLUGIN_JOB_CREATE_FAILED",
            )
        except WindowsRuntimeError:
            self.close(handle)
            raise
        return int(handle)

    def assign(self, job: int, process: int) -> None:
        """
        把仍挂起的进程分配到指定 Job 并立即复核。

        :param job: 当前 Worker 的 Job
        :param process: 尚未恢复的进程句柄
        :raises WindowsRuntimeError: 分配或复核失败时抛出
        """
        self.check(self.kernel.AssignProcessToJobObject(job, process), "PLUGIN_JOB_ASSIGN_FAILED")
        if not self.is_in_job(job, process):
            raise WindowsRuntimeError("插件进程不属于预期 Job", "PLUGIN_JOB_ASSIGN_FAILED")

    def is_in_job(self, job: int | None, process: int) -> bool:
        """
        根据系统句柄核实成员资格，不信任客户端自报。

        :param job: 当前 Worker 的 Job
        :param process: 待核实的存活进程句柄
        :return: 是否属于指定 Job
        """
        result = wintypes.BOOL()
        self.check(self.kernel.IsProcessInJob(process, job, ctypes.byref(result)), "PLUGIN_JOB_QUERY_FAILED")
        return bool(result.value)

    def active_processes(self, job: int) -> int:
        """
        查询 Job 的实际活跃成员，不能用根 PID 退出替代。

        :param job: 待确认回收的 Job
        :return: 尚未结束的进程数量
        """
        information = _JobAccounting()
        self.check(
            self.kernel.QueryInformationJobObject(job, 1, ctypes.byref(information), ctypes.sizeof(information), None),
            "PLUGIN_JOB_QUERY_FAILED",
        )
        return int(information.ActiveProcesses)

    def create_suspended(
        self, arguments: tuple[str, ...], cwd_path: Path, environment: dict[str, str]
    ) -> tuple[int, int, int]:
        """
        不经 shell 创建挂起进程，不继承任何宿主句柄。

        :param arguments: 绝对执行文件及独立参数
        :param cwd_path: 受信工作目录
        :param environment: 已净化的完整环境
        :return: 进程句柄、主线程句柄和根 PID
        :raises WindowsRuntimeError: 参数或原生创建失败时抛出
        """
        import subprocess

        if not arguments or not Path(arguments[0]).is_absolute():
            raise WindowsRuntimeError("Worker 执行文件必须是绝对路径", "WORKER_EXECUTABLE_UNRESOLVED")
        if any("\0" in item for item in arguments) or any(
            not key or "=" in key or "\0" in key + value for key, value in environment.items()
        ):
            raise WindowsRuntimeError("Worker 启动参数无效", "PLUGIN_INVALID_REQUEST")
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline(arguments))
        environment_block = ctypes.create_unicode_buffer(
            "\0".join(
                f"{key}={value}" for key, value in sorted(environment.items(), key=lambda item: item[0].casefold())
            )
            + "\0\0"
        )
        startup = _StartupInfo()
        startup.cb = ctypes.sizeof(startup)
        startup.dwFlags = 1
        information = _ProcessInfo()
        self.check(
            self.kernel.CreateProcessW(
                arguments[0],
                command,
                None,
                None,
                False,
                0x00000004 | 0x00000400 | 0x08000000,
                environment_block,
                str(cwd_path),
                ctypes.byref(startup),
                ctypes.byref(information),
            ),
            "PLUGIN_PROCESS_CREATE_FAILED",
        )
        return int(information.hProcess), int(information.hThread), int(information.dwProcessId)

    def process_identity(self, process: int) -> tuple[int, Path]:
        """
        保存进程创建时间和真实映像，防止 PID 重用混淆会话。

        :param process: 已打开且需由调用方保留的句柄
        :return: FILETIME 创建值和映像路径
        """
        times = [ctypes.c_uint64() for _ in range(4)]
        self.check(
            self.kernel.GetProcessTimes(process, *(ctypes.byref(item) for item in times)), "PLUGIN_PROCESS_QUERY_FAILED"
        )
        image = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(image))
        self.check(
            self.kernel.QueryFullProcessImageNameW(process, 0, image, ctypes.byref(length)),
            "PLUGIN_PROCESS_QUERY_FAILED",
        )
        return times[0].value, Path(image.value).resolve()

    def open_process(self, pid: int) -> int:
        """
        打开连接方进程用于身份和存活检查，不授予写入进程能力。

        :param pid: 由原生管道取得的 PID
        :return: 需关闭的进程句柄
        """
        handle = self.kernel.OpenProcess(0x00100000 | 0x1000, False, pid)
        self.check(handle, "PLUGIN_PROCESS_QUERY_FAILED")
        return int(handle)

    def pipe_pid(self, handle: int, *, is_server: bool) -> int:
        """
        从系统管道句柄取得另一端的真实进程身份。

        :param handle: 已连接的管道句柄
        :param is_server: 当前是否为服务器一端
        :return: 另一端 PID
        """
        value = wintypes.DWORD()
        function = self.kernel.GetNamedPipeClientProcessId if is_server else self.kernel.GetNamedPipeServerProcessId
        self.check(function(handle, ctypes.byref(value)), "PLUGIN_PIPE_AUTH_FAILED")
        return value.value

    def pipe_security(self) -> tuple[_SecurityAttributes, ctypes.c_void_p]:
        """
        创建仅当前用户读写及 SYSTEM 完全访问的管道 DACL。

        用户读写不含创建另一管道实例的权限。调用方须 LocalFree 返回的描述符。

        :return: 安全属性和需释放的安全描述符
        """
        descriptor = wintypes.LPVOID()
        sid = self._user_sid()
        self.check(
            self.security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                f"D:P(A;;GA;;;SY)(A;;0x12019b;;;{sid})",
                1,
                ctypes.byref(descriptor),
                None,
            ),
            "PLUGIN_PIPE_CREATE_FAILED",
        )
        attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
        return attributes, descriptor

    def _user_sid(self) -> str:
        """
        从当前进程令牌取得用户 SID，只用于构造 DACL，不记录日志。
        """
        token = wintypes.HANDLE()
        self.check(
            self.security.OpenProcessToken(self.kernel.GetCurrentProcess(), 8, ctypes.byref(token)),
            "PLUGIN_PIPE_CREATE_FAILED",
        )
        string_sid = wintypes.LPWSTR()
        try:
            needed = wintypes.DWORD()
            self.security.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
            if not needed.value:
                raise WindowsRuntimeError("无法取得插件管道身份", "PLUGIN_PIPE_CREATE_FAILED")
            user_buffer = ctypes.create_string_buffer(needed.value)
            self.check(
                self.security.GetTokenInformation(token, 1, user_buffer, needed, ctypes.byref(needed)),
                "PLUGIN_PIPE_CREATE_FAILED",
            )
            user = ctypes.cast(user_buffer, ctypes.POINTER(_TokenUser)).contents
            self.check(
                self.security.ConvertSidToStringSidW(user.Sid, ctypes.byref(string_sid)), "PLUGIN_PIPE_CREATE_FAILED"
            )
            return string_sid.value
        finally:
            if string_sid:
                self.kernel.LocalFree(ctypes.cast(string_sid, wintypes.LPVOID))
            self.close(int(token.value))
