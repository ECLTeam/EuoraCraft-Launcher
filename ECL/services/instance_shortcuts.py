# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：生成 Windows 实例快捷方式与稳定图标缓存，不改变启动配置。
#
# 公开接口：
#   - class ShortcutIcon — 描述实例已解析的有效图标。
#   - class InstanceShortcutService — 将实例启动意图写入桌面或指定位置的 .lnk。
# ============================================================

from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

from ECL.utils.errors import GameServiceError
from ECL.utils.files import atomic_write_bytes


@dataclass(frozen=True, slots=True)
class ShortcutIcon:
    """
    保存实例元数据解析后选中的图标种类和值。
    """

    icon_type: str
    value: str


class InstanceShortcutService:
    """
    在 Windows 上创建可重复使用的实例快捷方式，POSIX 明确返回不支持。

    参数通过 JSON 标准输入传给固定 PowerShell 脚本，用户路径不参与脚本拼接。
    """

    create_script: str = """
    $ErrorActionPreference = 'Stop'
    $request = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($request.shortcut)
    $link.TargetPath = $request.executable
    $link.Arguments = $request.arguments
    $link.WorkingDirectory = $request.workingDirectory
    $link.IconLocation = $request.icon
    $link.Description = $request.description
    $link.Save()
    """
    desktop_script: str = "[Console]::Write([Environment]::GetFolderPath('Desktop'))"
    image_keys: frozenset[str] = frozenset(
        {"grass", "chest", "command", "coal", "iron", "quartz", "fabric", "forge", "neoforge", "quilt", "optifine"}
    )
    max_icon_bytes: int = 10 * 1024 * 1024

    def __init__(self, data_path: Path, resource_path: Path, *, is_frozen: bool, app_path: Path) -> None:
        """
        保存运行环境，用于区分安装版和源码版的真实启动入口。

        :param data_path: 快捷方式继续使用的数据目录
        :param resource_path: 内置图标所在资源目录
        :param is_frozen: 是否为打包后的可执行文件
        :param app_path: 源码或安装入口目录
        """
        self._data_path = data_path
        self._resource_path = resource_path
        self._is_frozen = is_frozen
        self._app_path = app_path

    def launch_command(self, instance_path: Path) -> tuple[Path, str]:
        """
        构造带绝对实例目录及数据目录的命令，保留空格与 Unicode。

        :param instance_path: 实例 versions 子目录
        :return: 可执行文件路径与 Windows 命令行参数字符串
        """
        executable = Path(sys.executable).resolve()
        if self._is_frozen:
            original_executable = Path(sys.argv[0]).resolve()
            if original_executable.suffix.casefold() == ".exe" and original_executable.is_file():
                executable = original_executable
        arguments: list[str] = []
        if not self._is_frozen:
            windowless_python = executable.with_name("pythonw.exe")
            if windowless_python.is_file():
                executable = windowless_python
            arguments.append(str(self._app_path / "main.py"))
        arguments.extend(("--data-dir", str(self._data_path), "--launch", str(instance_path)))
        return executable, subprocess.list2cmdline(arguments)

    def create(
        self,
        instance_path: Path,
        name: str,
        icon: ShortcutIcon,
        output_path: Path | None = None,
    ) -> dict[str, str]:
        """
        原子创建实例 .lnk；已有文件保持原样，不自动覆盖。

        :param instance_path: 经过实例边界验证的绝对目录
        :param name: 实例显示名称
        :param icon: 与实例列表相同的有效图标
        :param output_path: 指定位置，缺省时使用系统桌面已知目录
        :return: 快捷方式和图标的绝对路径
        :raises GameServiceError: 平台不支持、目标冲突或图标/快捷方式写入失败
        """
        if sys.platform != "win32":
            raise GameServiceError("当前仅支持 Windows 快捷方式", "SHORTCUT_PLATFORM_UNSUPPORTED")
        if output_path is None:
            desktop_path = Path(self._powershell(self.desktop_script).stdout.strip())
            if not desktop_path.is_absolute() or not desktop_path.is_dir():
                raise GameServiceError("无法定位 Windows 桌面", "SHORTCUT_DESKTOP_UNAVAILABLE")
            filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")[:100] or "Minecraft"
            output_path = desktop_path / f"{filename}.lnk"
        if not output_path.is_absolute() or output_path.suffix.lower() != ".lnk":
            raise GameServiceError("请选择绝对路径的 .lnk 文件", "SHORTCUT_PATH_INVALID")
        if output_path.exists():
            raise GameServiceError("该快捷方式已存在，请选择其他文件名", "SHORTCUT_EXISTS")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        executable, arguments = self.launch_command(instance_path)
        icon_file = self._cache_icon(icon)
        with TemporaryDirectory(prefix=".ecl-shortcut-", dir=output_path.parent) as staging_directory:
            staged_link = Path(staging_directory) / "instance.lnk"
            self._powershell(
                self.create_script,
                {
                    "shortcut": str(staged_link),
                    "executable": str(executable),
                    "arguments": arguments,
                    "workingDirectory": str(self._app_path),
                    "icon": str(icon_file),
                    "description": f"启动 {name}",
                },
            )
            if not staged_link.is_file():
                raise GameServiceError("Windows 未创建快捷方式", "SHORTCUT_CREATE_FAILED")
            staged_link.rename(output_path)
        return {"path": str(output_path), "iconPath": str(icon_file)}

    def _cache_icon(self, icon: ShortcutIcon) -> Path:
        """
        将当前有效图标转换为稳定 ICO，避免临时目录或前端 URL 失效。
        """
        if icon.icon_type == "data" and icon.value.startswith("data:image/"):
            image_bytes = base64.b64decode(icon.value.split(",", 1)[1], validate=True)
        elif icon.icon_type in {"local", "external"}:
            source_path = Path(icon.value)
            if source_path.stat().st_size > self.max_icon_bytes:
                raise GameServiceError("实例图标过大", "SHORTCUT_ICON_INVALID")
            image_bytes = source_path.read_bytes()
        else:
            key = icon.value.lower() if icon.value.lower() in self.image_keys else "grass"
            candidates = (
                self._resource_path / "frontend" / "dist" / "img" / "item" / f"{key}.png",
                self._app_path / "frontend" / "public" / "img" / "item" / f"{key}.png",
            )
            source_path = next((candidate for candidate in candidates if candidate.is_file()), None)
            if source_path is None:
                raise GameServiceError("无法读取实例内置图标", "SHORTCUT_ICON_INVALID")
            image_bytes = source_path.read_bytes()
        if len(image_bytes) > self.max_icon_bytes:
            raise GameServiceError("实例图标过大", "SHORTCUT_ICON_INVALID")
        cache_file = self._data_path / "shortcut-icons" / f"{hashlib.sha256(image_bytes).hexdigest()}.ico"
        if cache_file.is_file():
            return cache_file
        with Image.open(BytesIO(image_bytes)) as source:
            if source.width * source.height > 16_777_216:
                raise GameServiceError("实例图标尺寸过大", "SHORTCUT_ICON_INVALID")
            converted = source.convert("RGBA")
            converted.thumbnail((256, 256), Image.Resampling.LANCZOS)
            canvas = Image.new("RGBA", (256, 256))
            canvas.alpha_composite(converted, ((256 - converted.width) // 2, (256 - converted.height) // 2))
            buffer = BytesIO()
            canvas.save(buffer, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
        atomic_write_bytes(cache_file, buffer.getvalue())
        return cache_file

    @staticmethod
    def _powershell(script: str, payload: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        """
        隐藏执行固定脚本，用户输入只经标准输入传递且执行时间有上限。
        """
        return subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); " + script,
            ],
            input=json.dumps(payload, ensure_ascii=True) if payload else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
