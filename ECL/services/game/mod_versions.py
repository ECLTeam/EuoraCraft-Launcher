# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：按加载器语法判断本地模组版本约束；不支持的表达式保留未知结果。
#
# 公开接口：
#   - class ModVersionPredicate — 检查 Fabric 扩展语义版本及 Maven 数字版本区间。
# ============================================================
from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import zip_longest


@dataclass(frozen=True, slots=True)
class _Version:
    components: tuple[int, ...]
    prerelease: tuple[str, ...] | None

    @classmethod
    def parse(cls, value: str) -> _Version | None:
        match = re.fullmatch(r"(\d+(?:\.\d+)*)(?:-([0-9A-Za-z.-]*))?(?:\+[0-9A-Za-z.-]+)?", value)
        if not match:
            return None
        return cls(
            tuple(int(part) for part in match[1].split(".")),
            tuple(match[2].split(".")) if match[2] is not None else None,
        )

    def compare(self, other: _Version) -> int:
        """
        对齐数字分量并比较预发布标记，空标记用于下一分支的排除上界。
        """
        for first, second in zip_longest(self.components, other.components, fillvalue=0):
            if first != second:
                return 1 if first > second else -1
        if self.prerelease is None or other.prerelease is None:
            return int(self.prerelease is None) - int(other.prerelease is None)
        for first, second in zip_longest(self.prerelease, other.prerelease, fillvalue=""):
            if first == second:
                continue
            if not first or not second:
                return 1 if first else -1
            if first.isdigit() and second.isdigit():
                if int(first) == int(second):
                    continue
                return 1 if int(first) > int(second) else -1
            if first.isdigit() != second.isdigit():
                return -1 if first.isdigit() else 1
            return 1 if first > second else -1
        return 0


class ModVersionPredicate:
    """
    检查明确支持的加载器约束，复杂或无法确认的语法返回未知。

    Fabric 的数组约束按或组合，单个表达式中的比较条件按且组合。
    Maven 仅解释数字版本区间，不把其他库的排序规则当成 Maven 规则。
    """

    @classmethod
    def matches(cls, version: str | None, constraints: tuple[str, ...], syntax: str) -> bool | None:
        """
        判断一个已知实际版本是否满足声明的约束。

        :param version: 实际提供者版本；缺失时不能据最低要求推断成功
        :param constraints: 保留原样的或条件列表
        :param syntax: 加载器使用的版本语法
        :return: 满足、不满足或无法判断
        """
        if not version or version in {"Unknown", "unknown"} or "${" in version:
            return None
        results = [cls._expression(version, expression, syntax) for expression in constraints or ("*",)]
        if True in results:
            return True
        return None if None in results else False

    @classmethod
    def _expression(cls, version: str, expression: str, syntax: str) -> bool | None:
        expression = expression.strip()
        if expression == "*":
            return True
        if syntax == "maven":
            return cls._maven(version, expression)
        if syntax != "fabric":
            return None
        results = [cls._fabric_term(version, term) for term in expression.split()]
        if not results:
            return None
        if False in results:
            return False
        return None if None in results else True

    @staticmethod
    def _fabric_term(version: str, term: str) -> bool | None:
        """
        按 Fabric 的比较与兼容范围规则处理一个条件，未知语法不推断成功。
        """
        match = re.fullmatch(r"(>=|<=|>|<|=|~|\^)?([^\s|]+)", term)
        if not match:
            return None
        operator, required = match[1] or "=", match[2]
        actual = _Version.parse(version)
        expected = _Version.parse(required)
        if actual is None or expected is None:
            return version == required if operator == "=" and not any(char in required for char in "*xX[]()") else None
        comparison = actual.compare(expected)
        if operator in {"~", "^"}:
            components = list(expected.components)
            position = 1 if operator == "~" and len(components) > 1 else 0
            upper = components[: position + 1]
            upper[position] += 1
            return comparison >= 0 and actual.compare(_Version(tuple(upper), ("",))) < 0
        return {
            "=": comparison == 0,
            ">": comparison > 0,
            "<": comparison < 0,
            ">=": comparison >= 0,
            "<=": comparison <= 0,
        }[operator]

    @staticmethod
    def _maven(version: str, expression: str) -> bool | None:
        """
        仅比较数字 Maven 区间，避免用语义版本排序误判 Maven 限定版本。
        """
        expression = expression.replace(" ", "")
        actual = _Version.parse(version)
        if actual is None or actual.prerelease is not None or "+" in version:
            return None
        intervals = re.findall(r"[\[(][^\[\]()]*[\])]", expression)
        if not intervals or ",".join(intervals) != expression.replace(" ", ""):
            return None
        for interval in intervals:
            parts = interval[1:-1].split(",")
            if len(parts) == 1:
                expected = _Version.parse(parts[0])
                if (
                    interval[0] != "["
                    or interval[-1] != "]"
                    or expected is None
                    or expected.prerelease is not None
                    or "+" in parts[0]
                ):
                    return None
                if actual.compare(expected) == 0:
                    return True
                continue
            if len(parts) != 2:
                return None
            lower, upper = (_Version.parse(part) if part else None for part in parts)
            if any(
                part and (parsed is None or parsed.prerelease is not None or "+" in part)
                for part, parsed in zip(parts, (lower, upper), strict=True)
            ):
                return None
            if lower is not None and (actual.compare(lower) < 0 or (actual.compare(lower) == 0 and interval[0] == "(")):
                continue
            if upper is not None and (
                actual.compare(upper) > 0 or (actual.compare(upper) == 0 and interval[-1] == ")")
            ):
                continue
            return True
        return False
