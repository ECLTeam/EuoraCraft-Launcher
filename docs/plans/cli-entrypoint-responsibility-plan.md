# 命令行入口职责调整实施方案

## 目标

让 `main.py` 只负责启动进程并转交控制权。命令行参数的读取、校验和本次运行的启动调用统一放在 `ECL/cli.py`。现有参数、退出码和单实例转发行为保持一致。

## 当前实现

- `ECL/cli.py` 定义参数、校验规则和 `LaunchOptions`。
- `main.py` 的 `run_launcher` 调用参数解析器，再延迟导入 `EuoraCraftLauncher` 并运行。
- `ECL/host/single_instance.py` 直接复用 `parse_launch_options` 校验转发请求。
- `--help`、`--version` 和无效参数在导入 `ECL.launcher` 前退出。

## 选定方案

1. 在 `ECL/cli.py` 新增公开的 `run_launcher(argv: list[str] | None = None) -> int`：读取调用方提供的参数或 `sys.argv[1:]`，调用现有 `parse_launch_options`，解析成功后才导入 `EuoraCraftLauncher` 并返回其退出码。更新文件头公开接口和函数文档字符串。
2. 将 `main.py` 简化为导入并调用 `ECL.cli.run_launcher`，保留 `main.run_launcher` 这一现有可导入名称。修改文件头，使说明与实际职责一致。
3. 保持 `parse_launch_options`、`LaunchOptions`、`ECL/host/single_instance.py` 和所有命令行选项的行为不变。保留帮助、版本和错误参数在桌面后端加载前退出的顺序。
4. 在 `tests/test_cli_launch_options.py` 增加入口测试：核对正常参数会传给启动器、返回退出码；帮助和版本请求不会导入 `ECL.launcher`；错误参数保持原退出码。测试不创建真实桌面窗口。

## 验收与提交

1. 执行 `ruff check ECL tests`、`ruff format --check ECL tests` 和相关 `pytest` 用例。
2. 执行 `python main.py --help`、`python main.py --version`，核对输出、退出码和无日志文件副作用。
3. 因入口关系到启动器运行流程，按项目规范执行 `cd frontend && pnpm check`、`pnpm build`，并实际启动启动器验证窗口可用、退出正常。
4. 检查 `git diff` 和 `git status`。此前已存在的注释改写单独处理，不混入这次功能提交；本次入口调整通过验证后以中文 Conventional Commit 提交。

## 风险与处理

- 把启动器导入放到 `ECL/cli.py` 的函数内部，防止 `--help`、`--version` 提前加载桌面依赖。
- `main.py` 继续暴露 `run_launcher` 名称，避免已有调用方因导入路径变化失败。
- 本地 `main` 目前有一批尚未提交的注释改写；实施时只提交本方案相关的代码、测试和必要文档。
