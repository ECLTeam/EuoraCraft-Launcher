# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：崩溃分析规则目录：声明式诊断规则与置信度定义。
#
# 公开接口：
#   - Confidence — 崩溃原因置信度字面量类型。
#   - class CrashRule — 单条崩溃诊断规则的不可变描述。
#   - class CrashRuleCatalog — 规则目录，集中保存全部诊断规则。
# ============================================================

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Confidence = Literal["certain", "likely", "possible"]


@dataclass(frozen=True, slots=True)
class CrashRule:
    """
    单条崩溃诊断规则的不可变描述。

    :param code: 稳定原因码，前端按 ``code.replace(".", "_")`` 查找文案键
    :param confidence: 命中后的置信度档位
    :param priority: 数值越小越优先，用于多规则命中时的取舍
    :param patterns: 依次尝试的日志匹配模式，大小写不敏感
    """

    code: str
    confidence: Confidence
    priority: int
    patterns: tuple[re.Pattern[str], ...]


def _patterns(*values: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(value, re.IGNORECASE) for value in values)


class CrashRuleCatalog:
    """
    集中保存崩溃诊断规则。

    规则只匹配 Minecraft、JVM 与主流加载器的公开输出；原因代码和前端文案均为
    ECL 自有定义。规则表达参考 GPL-3.0 项目 HMCL 的 CrashReportAnalyzer
    （https://github.com/HMCL-dev/HMCL/blob/main/HMCLCore/src/main/java/org/jackhuang/hmcl/game/CrashReportAnalyzer.java），
    本项目在 Python 语义下独立实现并保留署名；扩大规则集时必须同步补充
    前端各语言的 ``error.crash.reasons`` 文案与规则单测。
    """

    rules = (
        CrashRule(
            "jvm.invalid_arguments",
            "certain",
            0,
            _patterns(r"unrecognized (?:vm )?option", r"could not create the java virtual machine"),
        ),
        CrashRule(
            "memory.out_of_memory",
            "certain",
            0,
            _patterns(
                r"outofmemoryerror", r"out of physical ram", r"out of memory error", r"could not reserve enough space"
            ),
        ),
        CrashRule(
            "java.incompatible_version",
            "certain",
            0,
            _patterns(
                r"unsupported class file (?:major|minor) version",
                r"compiled by a more recent version of the java runtime",
                r"level is not supported by the active jre",
            ),
        ),
        CrashRule(
            "java.module_access",
            "likely",
            0,
            _patterns(
                r"module java\.base does not (?:export|open)",
                r"inaccessibleobjectexception",
                r"java\.lang\.nosuchfieldexception: ucp",
            ),
        ),
        CrashRule(
            "java.legacy_forge",
            "likely",
            0,
            _patterns(r"manifestentryverifier", r"unable to make protected final java\.lang\.class"),
        ),
        CrashRule(
            "java.openj9", "certain", 0, _patterns(r"openj9 is (?:not supported|incompatible)", r"j9vminternals")
        ),
        CrashRule(
            "java.32bit_heap",
            "likely",
            0,
            _patterns(r"invalid maximum heap size", r"unable to allocate .* object heap"),
        ),
        CrashRule(
            "java.architecture_mismatch",
            "likely",
            0,
            _patterns(
                r"can.t load (?:amd )?64-bit .* on a 32-bit platform",
                r"wrong elf class",
                r"%1 is not a valid win32 application",
            ),
        ),
        CrashRule(
            "graphics.opengl_unsupported",
            "certain",
            0,
            _patterns(
                r"driver does not appear to support opengl",
                r"pixel format not accelerated",
                r"couldn.t set pixel format",
            ),
        ),
        CrashRule(
            "graphics.driver_crash",
            "likely",
            0,
            _patterns(r"exception_access_violation", r"problematic frame:.*(?:nvoglv|atio|ig\w*)"),
        ),
        CrashRule(
            "jvm.native_crash",
            "likely",
            0,
            _patterns(
                r"a fatal error has been detected by the java runtime environment",
                r"internal error \(.*?\), pid=\d+",
                r"sigsegv",
            ),
        ),
        CrashRule(
            "native.library_missing",
            "likely",
            0,
            _patterns(r"unsatisfiedlinkerror", r"failed to locate library", r"no .* in java\.library\.path"),
        ),
        CrashRule(
            "files.integrity_failure",
            "likely",
            0,
            _patterns(r"signer information does not match", r"invalid or corrupt jarfile", r"zip error:.*invalid"),
        ),
        CrashRule(
            "loader.install_incomplete",
            "likely",
            0,
            _patterns(
                r"cannot find launch target fmlclient",
                r"invalid paths argument.*fmlcore",
                r"classnotfoundexception:.*(?:modlauncher|fabricloader)",
            ),
        ),
        CrashRule(
            "mod.extracted_jar",
            "certain",
            0,
            _patterns(r"extracted mod jars? found", r"directories below appear to be extracted jar files"),
        ),
        CrashRule(
            "mod.duplicate",
            "certain",
            0,
            _patterns(r"duplicatemodsfoundexception", r"found duplicate mods?", r"modresolutionexception:\s*duplicate"),
        ),
        CrashRule(
            "mod.missing_dependency",
            "certain",
            0,
            _patterns(
                r"missing or unsupported mandatory dependencies",
                r"depends on .* which is missing",
                r"requires version .* of .* but only",
            ),
        ),
        CrashRule(
            "mod.incompatible",
            "certain",
            0,
            _patterns(
                r"incompatible mods found",
                r"some of your mods are incompatible",
                r"mod resolution encountered an incompatible mod set",
            ),
        ),
        CrashRule(
            "mod.mixin_failure",
            "likely",
            1,
            _patterns(
                r"mixin (?:prepare|apply|transform) failed",
                r"mixinbootstrap.*(?:not found|missing)",
                r"invalidmixinexception",
            ),
        ),
        CrashRule(
            "mod.config_failure",
            "likely",
            1,
            _patterns(r"failed loading config file", r"parsingexception", r"failed to load config .* for mod"),
        ),
        CrashRule(
            "mod.initialization_failure",
            "likely",
            1,
            _patterns(r"failed to create mod instance", r"caught exception from ", r"exception during mod loading"),
        ),
        CrashRule(
            "mod.loader_reported",
            "likely",
            1,
            _patterns(r"failure message:", r"a potential solution has been determined", r"mod loading has failed"),
        ),
        CrashRule(
            "mod.optifine_conflict",
            "likely",
            1,
            _patterns(
                r"optifine.*(?:incompatible|not compatible)",
                r"shaders mod detected.*optifine",
                r"optifine.*nosuchmethoderror",
            ),
        ),
        CrashRule(
            "resource.render_failure",
            "likely",
            1,
            _patterns(
                r"1282:\s*invalid operation", r"lower resolution resourcepack", r"texture.*(?:too large|out of memory)"
            ),
        ),
        CrashRule("world.block_failure", "likely", 1, _patterns(r"block location:\s*world:", r"ticking block")),
        CrashRule("world.entity_failure", "likely", 1, _patterns(r"entity.s exact location:", r"ticking entity")),
        CrashRule("game.manual_debug_crash", "certain", 1, _patterns(r"manually triggered debug crash")),
        CrashRule(
            "game.crash_report",
            "likely",
            2,
            _patterns(r"crash report saved to", r"this crash report has been saved to", r"could not save crash report"),
        ),
    )
