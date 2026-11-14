# 启动占位符替换策略重构（引号语义分类）实施文档

日期：2026-11-08（Asia/Shanghai）。状态：已实施并提交（Core 子仓库 `3233906`，主仓库集成 `292e359`）；真实启动器验收待用户执行。

制定时基线：主仓库 `5c86f98`，Core 子仓库包含 `09080a4`（含空格实例名修复）。实施前须重新核对基线与无关改动。

## 1. 问题与根因

**一句话结论：`PlaceholderReplacer` 给所有占位符统一加引号，`classpath_separator` 被替换成 `";"`；Forge 的 `-p` 模块路径形如 `${library_directory}/...jar${classpath_separator}${library_directory}/...`，替换后产生 `...jar";""D:\...` 的连续引号。Windows 命令行解析规则（`CommandLineToArgvW`/CreateProcess）把引号内的 `""` 视为转义的字面引号而非"关闭再打开"，引号状态从此错乱，其后所有空格不再分隔参数，主类不再独立成 token。**

证据链：

- 真实失败日志：`ECL_data/logs/EuoraCraft-Launcher.log.2026-11-07.gz` 第 1016 行起，`LAUNCH_COMMAND_INVALID`，"主类未作为唯一参数出现（出现 0 次）"。
- 现场复现：用实例真实 JSON（`.minecraft/versions/Zombie Invade 100 Days/`）逐参数解析，第 5 个参数（`-p` 模块路径，替换后含 30 个引号）解析后引号状态为奇数，后续参数全部合并。
- 最小复现：`MARKER "A"/x.jar";""B"/y.jar TAIL` 解析结果为 `['MARKER', 'A/x.jar;"B/y.jar TAIL']`，TAIL 被吞进引号。
- 统一加引号的实现：[ECLauncherCore.py](../../ECL/game/Core/ECLauncherCore.py#L369-L380) `replace()`。
- 既有端到端测试 [test_game_service.py](../../tests/test_game_service.py#L1482-L1534) 的 Forge JSON 未包含模块路径与 natives 参数形态，因此未拦住该回归；11-01 的修复（`09080a4`）解决的 ignoreList 切分本身是正确的。

11-01 引入的 `validate_launch_command` 校验器工作正常：把以前会静默传给 Java 的坏命令拦成了明确报错，本次不需要修改校验器。

## 2. 方案设计：占位符按语义分类

替换策略从"一律加引号"改为按占位符语义分类：

### 2.1 裸替换（结构值，契约上不含空格）

| 占位符 | 取值 | 裸替换依据 |
| --- | --- | --- |
| `classpath_separator` | `;`（Windows）/ `:`（其他） | 恒位于模块路径、类路径等更大 token 的内部，且紧跟下一个引号段；加引号必然触发 `""` 转义语义 |
| `assets_index_name` | Mojang 资源索引名（如 `1.20.1`、`26.3-pre-1`） | 纯标识符，无空格 |
| `resolution_width` / `resolution_height` | 数值 | 契约上为整数 |

### 2.2 引号替换（可含空格的路径、名称与凭据）

其余全部占位符维持引号：`library_directory`、`assets_root`、`natives_directory`、`game_directory`、`launcher_name`、`launcher_version`、`version_type`、`auth_player_name`、`user_type`、`auth_uuid`、`auth_access_token`、`user_properties`、`primary_jar_name`、`version_name`。

说明：

- `version_name` 在 `-DignoreList=...,${version_name}.jar` 中位于 token 内部，但引号段前后相邻字符是 `,` 与 `.jar`，不构成 `""` 相邻，引用是安全且必需的（值可含空格）。
- `primary_jar_name` 在 `-DversionFile=${primary_jar_name}` 中位于 `=` 之后，独立成段引用安全；实例 jar 名可能含空格（如 `Zombie Invade 100 Days.jar`），必须保留引用。
- `${classpath}` 展开保持不变：`"{classpath}" "{main_class}"`。

### 2.3 实现

`ECL/game/Core/ECLauncherCore.py` 的 `PlaceholderReplacer`：

- 新增类属性 `bare_keys: frozenset[str]`（按规范使用不可变容器，归入领域类而非模块级私有映射），内容为 2.1 节的四个键，附中文注释说明 `""` 转义语义与分类依据。
- `replace()` 循环内按键是否属于 `bare_keys` 选择裸替换或引号替换。

不改动 `JvmArgumentBuilder`、`ClasspathBuilder`、`LaunchCommand.py` 校验器、`launch.py` 错误映射。

## 3. 测试计划

### 3.1 Core 子仓库测试

修改 [test_placeholder_replacer.py](../../ECL/game/tests/test_placeholder_replacer.py)：

| 用例 | 覆盖点 |
| --- | --- |
| `test_classpath_separator_is_bare_between_quoted_segments` | 模块路径形态 raw 命令替换后，`parse_launch_command` 解析出独立 token：模块路径一个 token、`-cp`、classpath、主类各一个；主类恰好出现 1 次 |
| `test_numeric_and_index_placeholders_are_bare` | `--assetIndex ${assets_index_name} --width ${resolution_width} --height ${resolution_height}` 替换结果不含引号 |
| `test_paths_and_names_stay_quoted` | `game_directory`、`version_name`、`auth_player_name` 等仍带引号（守护既有行为） |

新增/修改 [test_launch_command.py](../../ECL/game/tests/test_launch_command.py)：

| 用例 | 覆盖点 |
| --- | --- |
| `test_validate_accepts_realistic_forge_module_path` | 含真实 Forge 模块路径、natives 参数、含空格实例名的完整命令通过校验，主类为唯一独立 token |
| `test_validate_rejects_quoted_classpath_separator_corruption` | 使用 `";"`（旧错误行为）构造的命令解析后主类合并进巨型 token，校验抛出 `LAUNCH_COMMAND_INVALID`（守护校验器对该形态的拦截能力） |

### 3.2 主仓库测试

修改 [test_game_service.py](../../tests/test_game_service.py)：

- `test_command_builder_quotes_version_name_for_forge_ignore_list` 的 Forge 实例 JSON 扩展为真实形态：加入 `-p` 模块路径、`-Djava.library.path=${natives_directory}`、独立 `-cp`/`${classpath}` 条目（与真实 `Zombie Invade 100 Days.json` / `1.20.1.json` 结构一致）。
- 断言扩展：argv 中主类 `cpw.mods.bootstraplauncher.BootstrapLauncher` 出现且仅出现 1 次。

既有 `test_placeholder_replacer.py`、`test_launch_command.py`、`test_game_service.py` 全部用例及 `tests/test_modpack_preparation.py` 相关用例必须继续通过。

## 4. 验证命令

在主仓库根目录：

```powershell
ruff check .
python -m pytest ECL/game/tests --tb=short -q
python -m pytest tests/test_game_service.py tests/test_modpack_preparation.py --tb=short -q
ruff format --check ECL tests
python -m pytest --tb=short -q
```

## 5. 验收标准

- 自动化：上述命令全部通过；新增用例覆盖"裸分隔符 + 相邻引号段"形态。
- 真实启动器：启动 `Zombie Invade 100 Days`，不再出现 `LAUNCH_COMMAND_INVALID`，Forge 进入正常加载阶段；再启动一个无空格实例（如 `neoforge-21.1.230-1.21.1`）确认无回归。
- 真实启动器验收结果与 pytest 结果分开记录。

## 6. 实施步骤

1. Core 子仓库：修改 `PlaceholderReplacer`（`bare_keys` 类属性 + `replace()` 分类逻辑），补充 3.1 节测试。
2. 主仓库：按 3.2 节扩展端到端测试。
3. 执行第 4 节全部命令。
4. 真实启动验收（第 5 节）。
5. 按 AGENTS 第 7 节提交：先 Core 子仓库本地 `main`，再主仓库集成指针；默认不推送。

## 7. 风险与回退

- **裸替换值含空格的风险**：四个键的取值来源（`cp_delimiter`、版本 JSON `assetIndex.id`、窗口分辨率配置）契约上均不含空格；`assets_index_name` 来自版本 JSON，若未来出现含空格的异常值，命令会拆分并被 `validate_launch_command` 的版本名/主类校验或文件缺失错误拦截，不会静默启动失败。
- **其他占位符的同类形态**：逐一核对了所有标准占位符在已知版本 JSON 中的出现位置，只有 `classpath_separator` 同时满足"位于 token 内部且紧跟引号段"；`version_name`、`primary_jar_name` 等内部出现位置的前后相邻字符不构成 `""` 相邻。
- **回退**：还原 `ECLauncherCore.py` 与两个测试文件的改动即可；不涉及用户数据、版本 JSON 或迁移。
- **长期项**：11-01 方案第 3 节记录的 argv 列表重构决策继续有效；本次事故属于其重新评估条件 1 的又一次印证，暂不实施，待下次启动命令相关变更时一并评估。
