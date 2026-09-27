# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：崩溃分析规则目录：声明式诊断规则、命名捕获组与置信度定义。
#
# 公开接口：
#   - Confidence — 崩溃原因置信度字面量类型。
#   - class CrashRule — 单条崩溃诊断规则的不可变描述。
#   - class CrashRuleCatalog — 规则目录，集中保存全部诊断规则。
# ============================================================

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

Confidence = Literal["certain", "likely", "possible"]

_EMPTY_GROUPS: Mapping[str, str] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class CrashRule:
    """
    单条崩溃诊断规则的不可变描述。

    :param code: 稳定原因码，前端按 ``code.replace(".", "_")`` 查找文案键
    :param confidence: 命中后的置信度档位
    :param priority: 数值越小越优先，用于多规则命中时的取舍
    :param patterns: 依次尝试的日志匹配模式，大小写不敏感
    :param parameter_groups: 正则命名捕获组到 ``parameters`` 键的映射；
        命中后提取组值写入原因参数，未声明的命名组被忽略
    :param mod_hint_keys: ``parameters`` 中需要送入 Mod 索引反查肇事模组的键
    """

    code: str
    confidence: Confidence
    priority: int
    patterns: tuple[re.Pattern[str], ...]
    parameter_groups: Mapping[str, str] = _EMPTY_GROUPS
    mod_hint_keys: tuple[str, ...] = ()


def _patterns(*values: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(value, re.IGNORECASE) for value in values)


class CrashRuleCatalog:
    """
    集中保存崩溃诊断规则。

    规则只匹配 Minecraft、JVM 与主流加载器的公开输出；原因代码和前端文案均为
    ECL 自有定义。规则表达参考 GPL-3.0 项目 HMCL 的 CrashReportAnalyzer
    （https://github.com/HMCL-dev/HMCL/blob/main/HMCLCore/src/main/java/org/jackhuang/hmcl/game/CrashReportAnalyzer.java），
    本项目在 Python 语义下独立实现并保留署名；HMCL 的多条同域规则在 ECL 中
    合并为携带多个模式的单条原因。新增规则时必须同步补充前端各语言的
    ``error.crash.reasons`` 文案、本目录完整性测试与代表样例日志单测。
    """

    confidence_levels = ("certain", "likely", "possible")

    @classmethod
    def confidence_rank(cls, confidence: Confidence) -> int:
        """
        返回置信度档位的排序权重，数值越小越可信。

        :param confidence: 置信度档位
        :return: 排序权重
        """
        return cls.confidence_levels.index(confidence)

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
                r"outofmemoryerror",
                r"out of physical ram",
                r"out of memory error",
                r"could not reserve enough space",
                r"insufficient memory for the java runtime environment",
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
                r"unsupported (?:class file )?major\.minor version (?P<expected>\d+)\.0",
                r"class file version (?P<expected>\d+)\.0",
            ),
            MappingProxyType({"expected": "expected"}),
        ),
        CrashRule(
            "java.module_access",
            "likely",
            0,
            _patterns(
                r"module java\.base does not (?:export|open)",
                r"inaccessibleobjectexception",
                r"java\.lang\.nosuchfieldexception: ucp",
                r"classcastexception: (?:java\.base/jdk|class jdk)",
            ),
        ),
        CrashRule(
            "java.legacy_forge",
            "likely",
            0,
            _patterns(r"manifestentryverifier", r"unable to make protected final java\.lang\.class"),
        ),
        CrashRule(
            "java.openj9",
            "certain",
            0,
            _patterns(r"openj9 is (?:not supported|incompatible)", r"j9vminternals"),
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
            "java.mac_jdk_legacy",
            "likely",
            0,
            _patterns(r"nswindow drag regions should only be invalidated"),
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
            _patterns(
                r"exception_access_violation",
                r"problematic frame:.*(?:nvoglv|atio|ig\w*)",
                r"glx: failed to create context: glxbadfbconfig",
            ),
        ),
        CrashRule(
            "graphics.macos_glfw",
            "likely",
            0,
            _patterns(r"failed to find service port for display"),
        ),
        CrashRule(
            "graphics.rtss_sodium",
            "certain",
            1,
            _patterns(r"not compatible with sodium"),
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
            _patterns(
                r"unsatisfiedlinkerror",
                r"failed to locate library: (?P<library>.+)",
                r"no .* in java\.library\.path",
            ),
            MappingProxyType({"library": "library"}),
        ),
        CrashRule(
            "files.integrity_failure",
            "likely",
            0,
            _patterns(
                r"signer information does not match",
                r"invalid or corrupt jarfile",
                r"zip error:.*invalid",
                r"sha1 digest error for (?P<file>.+)",
            ),
            MappingProxyType({"file": "file"}),
        ),
        CrashRule(
            "files.already_exists",
            "likely",
            1,
            _patterns(r"java\.nio\.file\.filealreadyexistsexception: (?P<file>.+)"),
            MappingProxyType({"file": "file"}),
        ),
        CrashRule(
            "loader.install_incomplete",
            "likely",
            0,
            _patterns(
                r"cannot find launch target fmlclient",
                r"invalid paths argument, contained no existing paths",
                r"classnotfoundexception:.*(?:modlauncher|fabricloader)",
                r"found multiple arguments for option",
            ),
        ),
        CrashRule(
            "loader.forge_error_screen",
            "likely",
            1,
            _patterns(
                r"an exception was thrown, the game will display an error screen and halt\.\s*[\r\n]+(?P<reason>[^\r\n]+)"
            ),
            MappingProxyType({"reason": "reason"}),
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
            _patterns(
                r"duplicatemodsfoundexception",
                r"found duplicate mods?",
                r"modresolutionexception:\s*duplicate",
                r"found a duplicate mod (?P<name>.+) at (?P<path>.+)",
            ),
            MappingProxyType({"name": "name", "path": "path"}),
            ("name",),
        ),
        CrashRule(
            "mod.optifine_duplicate",
            "certain",
            1,
            _patterns(r"module optifine reads another module named optifine"),
        ),
        CrashRule(
            "mod.missing_dependency",
            "certain",
            0,
            _patterns(
                r"missing or unsupported mandatory dependencies",
                r"missing or unsupported mandatory dependencies:\s*[\r\n]+(?P<reason>\t[^\r\n]+)",
                r"depends on .* which is missing",
                r"requires version .* of .* but only",
                r"could not find required mod: (?P<sourcemod>.+?) requires (?P<destmod>.+)",
                r"could not resolve valid mod collection \(at: (?P<sourcemod>.+?) requires (?P<destmod>.+)\)",
            ),
            MappingProxyType({"sourcemod": "source_mod", "destmod": "target_mod", "reason": "reason"}),
            ("source_mod",),
        ),
        CrashRule(
            "mod.incompatible",
            "certain",
            0,
            _patterns(
                r"incompatible mods found",
                r"some of your mods are incompatible",
                r"mod resolution encountered an incompatible mod set",
                r"warnings were found!",
                r"found conflicting mods: (?P<sourcemod>.+?) conflicts with (?P<destmod>.+)",
            ),
            MappingProxyType({"sourcemod": "source_mod", "destmod": "conflicting_mod"}),
            ("source_mod", "conflicting_mod"),
        ),
        CrashRule(
            "mod.mixin_failure",
            "likely",
            1,
            _patterns(
                r"mixin (?:prepare|apply|transform) failed",
                r"mixinbootstrap.*(?:not found|missing)",
                r"invalidmixinexception",
                r"mixin\.injection\.throwables",
                r"critical injection failure",
            ),
        ),
        CrashRule(
            "mod.mixin_bootstrap_missing",
            "likely",
            1,
            _patterns(
                r"classnotfoundexception: org\.spongepowered\.asm\.launch\.mixintweaker",
                r"noclassdeffounderror: org/spongepowered/asm/mixin/transformer/fabricmixintransformerproxy",
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
            _patterns(
                r"failed to create mod instance",
                r"caught exception from ",
                r"exception during mod loading",
                r"loaderexceptionmodcrash: caught exception from (?P<name>.+?) \((?P<id>.+?)\)",
                r"failed to create mod instance\. modid: (?P<id>.+?),",
                r"could not execute entrypoint stage '.+?' due to errors, provided by '(?P<id>.+?)'!",
            ),
            MappingProxyType({"name": "mod_name", "id": "mod_id"}),
            ("mod_id",),
        ),
        CrashRule(
            "mod.loader_reported",
            "likely",
            1,
            _patterns(
                r"failure message:",
                r"a potential solution has been determined",
                r"mod loading has failed",
                r"\tmod file:",
            ),
        ),
        CrashRule(
            "mod.optifine_conflict",
            "likely",
            1,
            _patterns(
                r"optifine.*(?:incompatible|not compatible)",
                r"shaders mod detected.*optifine",
                r"optifine.*nosuchmethoderror",
                r"defineanonymousclass",
                r"invalid services found optifine",
                r"outside of image bounds",
            ),
        ),
        CrashRule(
            "mod.id_limit_exceeded",
            "certain",
            1,
            _patterns(r"maximum id range exceeded"),
        ),
        CrashRule(
            "mod.invalid_module_name",
            "likely",
            1,
            _patterns(r"invalid module name: (?P<name>.+) is not a java identifier"),
            MappingProxyType({"name": "name"}),
        ),
        CrashRule(
            "mod.class_resolution_failure",
            "likely",
            1,
            _patterns(
                r"java\.lang\.noclassdeffounderror: (?P<class>.+)",
                r"java\.lang\.nosuchmethoderror: (?P<class>.+)",
                r"java\.lang\.illegalaccesserror: tried to access class (?P<accessed>[\w.$]+) from class (?P<accessor>[\w.$]+)",
            ),
            MappingProxyType({"class": "class", "accessed": "accessed_class", "accessor": "accessor_class"}),
            ("class", "accessed_class", "accessor_class"),
        ),
        CrashRule(
            "resource.render_failure",
            "likely",
            1,
            _patterns(
                r"1282:\s*invalid operation",
                r"lower resolution resourcepack",
                r"texture.*(?:too large|out of memory)",
                r"maybe try a lower ?resolution (?:resourcepack|texturepack)",
            ),
        ),
        CrashRule(
            "world.block_failure",
            "likely",
            1,
            _patterns(
                r"block location:\s*world:\s*(?P<location>.+)",
                r"ticking block",
                r"\bblock: (?P<block>.+)",
            ),
            MappingProxyType({"block": "block", "location": "location"}),
        ),
        CrashRule(
            "world.entity_failure",
            "likely",
            1,
            _patterns(
                r"entity.s exact location:\s*(?P<location>.+)",
                r"ticking entity",
                r"\bentity type: (?P<entity>.+)",
            ),
            MappingProxyType({"entity": "entity", "location": "location"}),
        ),
        CrashRule("game.manual_debug_crash", "certain", 1, _patterns(r"manually triggered debug crash")),
        CrashRule(
            "game.crash_report",
            "likely",
            2,
            _patterns(r"crash report saved to", r"this crash report has been saved to", r"could not save crash report"),
        ),
    )
