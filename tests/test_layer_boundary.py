# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：校验后端三域依赖方向，内核不得依赖 MC。
#
# 公开接口：
#   - test_kernel_does_not_import_mc() -> None
#   - test_waiver_entries_are_alive() -> None
# ============================================================

from __future__ import annotations

import ast
from pathlib import Path

import ECL

ecl_root = Path(ECL.__file__).parent

mc_module_prefixes = ("ECL.minecraft", "ECL.java")

kernel_directories = (
    "plugins",
    "utils",
    "desktop",
    "headless",
)

kernel_root_modules = (
    "application.py",
    "bootstrap.py",
    "cli.py",
    "launcher.py",
    "services/operations.py",
    "services/processes.py",
    "services/updates.py",
)

kernel_exemptions: dict[str, str] = {
    "application.py": "构造根装配游戏服务，需要构建期注入的认证与资源站密钥；待装配逻辑迁出后消除",
    "plugins/worker_service.py": "装配缝：宿主侧构造 RuntimeJava/PublicQueries/Resources 三个注入式适配器；待装配逻辑统一后消除",
    "plugins/manager/base.py": "装配缝：实例化 MC 适配器工厂并注入 worker_service，同时引用 InstanceQueries 协议描述注入能力；长期保留",
    "plugins/java_resources.py": "插件 Java 资源声明以 ECL.java 的模型与查询为参数类型；待扩展点模型下沉后消除",
}

max_exemptions = 4


def _kernel_files() -> list[Path]:
    """
    收集当前归属内核的 Python 文件。

    `plugins/mc` 属于 MC 域，不纳入内核扫描；子模块 `game` 与
    `services/game`、`services/account` 本就不在内核范围内。

    :return: 内核文件路径列表
    """
    files: list[Path] = []
    for directory in kernel_directories:
        base = ecl_root / directory
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            if directory == "plugins" and path.is_relative_to(base / "mc"):
                continue
            files.append(path)
    files.extend(ecl_root / module for module in kernel_root_modules)
    return files


def _is_type_checking(test: ast.expr) -> bool:
    """
    判断条件表达式是否为 `TYPE_CHECKING` 或 `typing.TYPE_CHECKING`。
    """
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def _type_checking_nodes(tree: ast.AST) -> set[int]:
    """
    收集位于 `if TYPE_CHECKING:` 块内的节点。

    这些导入只服务于类型注解，不产生运行时依赖，因此不属于分层违规。
    第 2b 步把 broker 的类型导入移入该块依赖此规则。

    :return: 节点对象 id 集合
    """
    nodes: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _is_type_checking(node.test):
            for statement in node.body:
                for inner in ast.walk(statement):
                    nodes.add(id(inner))
    return nodes


def _violations(path: Path) -> list[str]:
    """
    返回单个文件导入 MC 模块的记录。

    :param path: 内核文件路径
    :return: `行号:模块名` 形式的违规记录
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    guarded = _type_checking_nodes(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if id(node) in guarded:
            continue
        modules: list[str] = []
        if isinstance(node, ast.ImportFrom) and node.module:
            modules = [node.module]
        elif isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        for module in modules:
            if module.startswith(mc_module_prefixes):
                found.append(f"{node.lineno}:{module}")
    return found


def _relative(path: Path) -> str:
    return path.relative_to(ecl_root).as_posix()


def test_kernel_does_not_import_mc() -> None:
    unexpected: list[str] = []
    for path in _kernel_files():
        found = _violations(path)
        if not found:
            continue
        relative = _relative(path)
        if relative in kernel_exemptions:
            continue
        unexpected.extend(f"{relative}:{item}" for item in found)

    assert unexpected == [], "内核禁止依赖 MC 域；如需临时例外，须在 kernel_exemptions 注明原因与消除条件"


def test_waiver_entries_are_alive() -> None:
    """
    豁免必须指向真实存在且确实违规的文件。

    文件迁走或违规消除后应删除对应豁免，否则列表会随时间腐化并掩盖新的依赖。
    """
    stale: list[str] = []
    for relative in kernel_exemptions:
        path = ecl_root / relative
        if not path.is_file():
            stale.append(f"{relative}: 文件不存在")
        elif not _violations(path):
            stale.append(f"{relative}: 已无违规，应移除豁免")

    assert stale == []
    assert len(kernel_exemptions) <= max_exemptions
