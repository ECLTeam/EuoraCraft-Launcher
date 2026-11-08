from __future__ import annotations

import argparse
import ast
from pathlib import Path

FORBIDDEN_DIRECTORIES = {"Adapters", "Api", "Common", "Events", "Game", "Infrastructure", "Plugin", "Services"}
FORBIDDEN_TOKENS = ("register_services", "EventBus().get(", "EventBus().register(")


def production_files(root: Path) -> list[Path]:
    ecl_root = root / "ECL"
    game_root = ecl_root / "game"
    return sorted(
        path for path in ecl_root.rglob("*.py") if "tests" not in path.parts and not path.is_relative_to(game_root)
    )


def audit_file(path: Path, root: Path, game_root: Path) -> list[str]:
    errors: list[str] = []
    source = path.read_text(encoding="utf-8")
    relative = path.relative_to(root)
    for token in FORBIDDEN_TOKENS:
        if token in source:
            errors.append(f"{relative}: 禁止使用 {token}")
    if "???" in source:
        errors.append(f"{relative}: 注释或字符串中仍存在未完成的问号占位符")

    tree = ast.parse(source, filename=str(relative))
    if not path.is_relative_to(game_root):
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("ECL.game."):
                errors.append(f"{relative}:{node.lineno}: 只能从 ECL.game 公共入口导入")

    for node in ast.walk(tree):
        if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) or not node.body:
            continue
        expression = node.body[0]
        is_docstring = (
            isinstance(expression, ast.Expr)
            and isinstance(expression.value, ast.Constant)
            and isinstance(expression.value.value, str)
        )
        if is_docstring and expression.lineno == expression.end_lineno:
            errors.append(f"{relative}:{node.lineno}: {node.name} 使用了单行 Docstring")
    return errors


def audit(root: Path) -> list[str]:
    errors: list[str] = []
    ecl_root = root / "ECL"
    actual_directories = {path.name for path in ecl_root.iterdir() if path.is_dir()}
    for directory in FORBIDDEN_DIRECTORIES & actual_directories:
        errors.append(f"后端目录必须使用小写命名: ECL/{directory}")

    game_root = ecl_root / "game"
    for path in production_files(root):
        errors.extend(audit_file(path, root, game_root))

    if [path for path in ecl_root.rglob("Libs.py") if not path.is_relative_to(game_root)]:
        errors.append("ECL 中仍存在重复或含义不明确的 Libs.py")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="检查 EuoraCraft 后端结构和注释规范")
    parser.add_argument("root", nargs="?", default=".", type=Path, help="仓库根目录")
    args = parser.parse_args()
    errors = audit(args.root.resolve())
    if errors:
        print("\n".join(f"ERROR {error}" for error in errors))
        print(f"\n共发现 {len(errors)} 个问题")
        return 1
    print("EuoraCraft 后端结构和 Docstring 检查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
