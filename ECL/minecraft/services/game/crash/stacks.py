# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：崩溃堆栈分析器：帧解析、主嫌帧选择与包名候选提取。
#
# 公开接口：
#   - class CrashStackFrame — 单个堆栈帧的不可变描述。
#   - class CrashStackSummary — 一次堆栈分析的汇总结果。
#   - class CrashStackAnalyzer — 从日志文本中解析堆栈帧并提取包名候选。
# ============================================================

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CrashStackFrame:
    """
    单个堆栈帧的不可变描述。

    :param class_name: 完整类名（lambda 归并到外层类后）
    :param method: 方法名，构造方法为 ``<init>`` / ``<clinit>``
    :param package: 类名前三段组成的包名候选
    """

    class_name: str
    method: str
    package: str


@dataclass(frozen=True, slots=True)
class CrashStackSummary:
    """
    一次堆栈分析的汇总结果。

    :param frames: 按出现顺序排列的非忽略堆栈帧
    :param packages: 去重后的包名候选，按首次出现顺序排列
    """

    frames: tuple[CrashStackFrame, ...]
    packages: tuple[str, ...]

    @property
    def primary(self) -> CrashStackFrame | None:
        """
        返回主嫌帧：整个堆栈中第一个不属于忽略前缀的帧。
        """
        return self.frames[0] if self.frames else None


class CrashStackAnalyzer:
    """
    从日志文本中解析堆栈帧并提取包名候选。

    解析覆盖 ``Caused by`` 异常链的全部帧；JDK、Minecraft 与主流加载器的
    内部帧按前缀忽略。包名候选供 Mod 索引反查肇事模组。
    """

    max_frames = 24
    max_packages = 12
    ignored_prefixes = (
        "java.",
        "javax.",
        "jdk.",
        "sun.",
        "com.mojang.",
        "net.minecraft.",
        "net.minecraftforge.",
        "net.fabricmc.",
        "org.spongepowered.",
        "org.lwjgl.",
        "com.google.",
        "org.apache.",
    )

    # 帧标识允许段间出现 "/"（JDK 模块限定帧如 java.base/java.lang.Thread），
    # 否则会截断出 "java.base" 这类无法命中忽略前缀的伪帧。
    _frame_pattern = re.compile(r"\bat\s+([A-Za-z_$][\w$]*(?:[./][\w$<>]+)+)")

    def analyze(self, text: str) -> CrashStackSummary:
        """
        解析文本中的堆栈帧。

        :param text: 已脱敏的合并日志文本
        :return: 帧列表与包名候选；无堆栈帧时两个字段为空元组
        """
        frames: list[CrashStackFrame] = []
        packages: list[str] = []
        for match in self._frame_pattern.finditer(text):
            class_name, method = self._split_frame(match.group(1))
            if class_name.startswith(self.ignored_prefixes):
                continue
            package = ".".join(class_name.split(".")[:3])
            if len(frames) < self.max_frames:
                frames.append(CrashStackFrame(class_name=class_name, method=method, package=package))
            if package not in packages and len(packages) < self.max_packages:
                packages.append(package)
        return CrashStackSummary(frames=tuple(frames), packages=tuple(packages))

    @staticmethod
    def _split_frame(raw: str) -> tuple[str, str]:
        # 拆分帧标识中的类名与方法名；构造方法段 ``<init>`` 不能按最后一
        # 段拆分，否则会把构造器所属段误认为类名。
        if raw.endswith(".<init>"):
            return raw[: -len(".<init>")], "<init>"
        if raw.endswith(".<clinit>"):
            return raw[: -len(".<clinit>")], "<clinit>"
        class_name, _, method = raw.rpartition(".")
        if not class_name:
            return raw, ""
        return class_name, method
