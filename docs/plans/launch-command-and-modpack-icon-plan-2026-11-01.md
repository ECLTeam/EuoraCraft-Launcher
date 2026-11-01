# 启动命令修复与整合包图标缓存实施文档

日期：2026-11-01（Asia/Shanghai）。状态：方案已按用户确认的方向整理，等待实施批准；本文档只描述做法，不代表生产代码已修改。

制定时基线：主仓库 `8aa1485a214efa9f23ac3b8da5ea5856ab1bc1d0`，Core 子仓库 `a20ff8c60971087abb3fddd7d2bdbeffc290f39e`，前端 `14907db64011134faae42cc328a03c8703b59a60`，工作区无未提交改动。实施前须重新核对基线与无关改动，行号可能随其他提交漂移。

本方案包含两个可独立实施、独立验收的章节：

1. **启动命令修复**：最小修复 `${version_name}` 的引用方式，并在启动前校验最终 argv。
2. **整合包图标缓存**：安装时把整合包图标写入实例 `.ecl/icon.<ext>`，并写入实例资料。

两章共享同一条边界：**长期“启动命令改为 argv 列表”重构本次不做**，原因与重新评估条件见第 3 节。

---

# 第一章：启动命令修复

## 1.1 问题与根因

**一句话结论：实例 JSON 的 `mainClass` 是正确的 `cpw.mods.bootstraplauncher.BootstrapLauncher`；真正的问题是启动器拼接命令时只给最后一个 `${version_name}` 加引号，Forge 的 `-DignoreList=...,${version_name}.jar` 在实例名含空格时被空格切开，`Invade` 成为 JVM 的第一个非选项参数（主类），于是 Java 报 `ClassNotFoundException: Invade`。**

关键证据：

- 实例描述：[Zombie Invade 100 Days.json](../../.minecraft/versions/Zombie%20Invade%20100%20Days/Zombie%20Invade%20100%20Days.json#L11) 第 11 行。
- 基础 Forge 描述：[forge-47.4.10-1.20.1.json](../../.minecraft/versions/forge-47.4.10-1.20.1/forge-47.4.10-1.20.1.json#L11) 第 11 行。
- Forge 的忽略列表：[Zombie Invade 100 Days.json](../../.minecraft/versions/Zombie%20Invade%20100%20Days/Zombie%20Invade%20100%20Days.json#L29) 第 29 行。
- 只给最后一次加引号的实现：[ECLauncherCore.py](../../ECL/game/Core/ECLauncherCore.py#L366-L393)。
- 命令以单个字符串交给进程：[InstancesManager.py](../../ECL/game/Core/InstancesManager.py#L122-L134)。
- 启动路径没有最终命令校验：[launch.py](../../ECL/services/game/launch.py#L913-L921)、[launch.py](../../ECL/services/game/launch.py#L1235)、[launch.py](../../ECL/services/game/launch.py#L1400-L1421)。

根因链条：

1. 在线安装默认把实例名设为整合包标题：[modpackImportStore.ts](../../frontend/src/features/instances/stores/modpackImportStore.ts#L151-L158)；本地导入也允许文件名保留空格：[modpackImportStore.ts](../../frontend/src/features/instances/stores/modpackImportStore.ts#L247-L252)。
2. Forge/NeoForge 的版本 JSON 在 `-DignoreList` 中引用 `${version_name}.jar`。
3. [ECLauncherCore.py](../../ECL/game/Core/ECLauncherCore.py#L385-L390) 用 `_replace_last` 只给最后一次出现加引号，`-DignoreList` 中的版本名保持裸值。
4. Windows 命令行按空格切分后，`...,forge-,Zombie`、`Invade`、`100`、`Days.jar` 变成多个 token；JVM 跳过 `-DignoreList=...` 选项后，把 `Invade` 当作主类。
5. 后续 `-cp`、`-p` 等变成程序参数，classpath 未生效，Java 报主类不存在。

正确主类与依赖完整性已核实：

- 正确主类：`cpw.mods.bootstraplauncher.BootstrapLauncher`。
- 提供主类的 jar：`.minecraft/libraries/cpw/mods/bootstraplauncher/1.1.2/bootstraplauncher-1.1.2.jar`，内含 `cpw/mods/bootstraplauncher/BootstrapLauncher.class`。
- 实例 JSON 声明的 29 个 library 与父版本 `1.20.1.json` 声明的 88 个 library 均无缺失。
- 实例自身 `<实例名>.jar` 缺失不是本次失败原因；[ECLauncherCore.py](../../ECL/game/Core/ECLauncherCore.py#L442-L447) 会回退到实例目录中的 `1.20.1.jar`。

## 1.2 实施范围

### 1.2.1 修改 `ECL/game/Core/ECLauncherCore.py`

文件：[ECLauncherCore.py](../../ECL/game/Core/ECLauncherCore.py)

| 位置 | 现状 | 改成什么 |
| --- | --- | --- |
| `PlaceholderReplacer._build_standard_replacements` 第 335-364 行 | 返回 `library_directory`、`game_directory`、`auth_player_name` 等键，不含 `version_name` | 在返回字典中增加 `"version_name": self.config.version_name`，让标准替换循环统一加引号 |
| `PlaceholderReplacer._replace_last` 第 366-372 行 | 只替换最后一次出现 | 删除该私有方法；本文件内没有其他调用方 |
| `PlaceholderReplacer.replace` 第 385-390 行 | 先 `_replace_last` 给最后一个 `version_name` 加引号，再裸替换其余；最后替换 `${classpath}` | 删除 `version_name` 的特殊分支；保留 `${classpath}` 替换：`result.replace("${classpath}", f'"{self.classpath}" "{self.main_class}"')` |
| `build_minecraft_cmd` 第 396-480 行 | 返回 `cmd.replace(...)` 生成的字符串 | 先生成 `command`，再用新校验函数校验，最后返回；校验位于第 480 行 `return` 之前 |

修改后的替换结果对已知版本 JSON 应为：

```text
-DignoreList=...,forge-,"Zombie Invade 100 Days".jar
```

Windows 命令行解析会去掉 token 内部的引号，得到单个 token：

```text
-DignoreList=...,forge-,Zombie Invade 100 Days.jar
```

同理，`--version ${version_name}` 仍会得到完整的 `"Zombie Invade 100 Days"`。

### 1.2.2 新增 `ECL/game/Core/LaunchCommand.py`

新文件，按 Core 子仓库约定添加文件头。公开接口：

- `parse_launch_command(command: str) -> list[str]`：按当前平台规则把最终命令解析为 argv。
- `validate_launch_command(command: str, *, version_name: str, main_class: str) -> None`：校验版本名与主类没有被参数切分；失败时抛出 `CoreError`，错误码 `LAUNCH_COMMAND_INVALID`。

解析规则：

- Windows：运行时分支使用 `ctypes.windll.shell32.CommandLineToArgvW`，解析后调用 `LocalFree` 释放；不得在模块导入阶段读取平台 API。
- 其他平台：使用 `shlex.split(command, posix=True)`。
- 平台判断使用 `platform.system()`，与 `ECLauncherCore.py` 现有写法一致。

校验规则：

1. `main_class` 必须非空，且不含空白、双引号或单引号；否则抛出 `LAUNCH_COMMAND_INVALID`。
2. `argv` 中必须存在且只存在一个等于 `main_class` 的独立 token；主类被切开或重复时抛出。
3. `command.count(version_name)` 必须等于 `sum(token.count(version_name) for token in argv)`；版本名被空格或引号切开时，解析后的完整出现次数会减少，抛出 `LAUNCH_COMMAND_INVALID`。

不在校验器里单独要求 `-DignoreList` 必须包含版本名：部分版本 JSON 的忽略列表本来就不引用版本名，硬编码该规则会产生误报。Forge/NeoForge 的具体形态由第 1.2.4 节的端到端用例直接断言 `-DignoreList` token。

错误类型复用 [Errors.py](../../ECL/game/Core/Errors.py) 的 `CoreError`，构造时传入 `LAUNCH_COMMAND_INVALID`。

### 1.2.3 修改 `ECL/services/game/launch.py`

文件：[launch.py](../../ECL/services/game/launch.py)

- 第 46 行导入改为同时导入 `CoreError`：
  `from ECL.game import CoreError, LaunchConfig`
- 在第 1417 行 `except Exception` 之前增加分支：

```python
except CoreError as exc:
    error = GameServiceError(str(exc), getattr(exc, "error_code", "GAME_LAUNCH_FAILED"))
    self._emit_launch_progress("error", str(error), 0, error.error_code)
    raise error from exc
```

该分支只承接 `build_minecraft_cmd` 新增的校验错误；当前 Core 命令构建只抛出 `FileNotFoundError`，不改变既有错误码。`GameServiceError` 的入口日志会记录中文说明，前端收到 `LAUNCH_COMMAND_INVALID` 后可给出“实例名称或版本描述异常”的明确提示。

### 1.2.4 新增与修改测试

| 文件 | 用例 | 覆盖点 |
| --- | --- | --- |
| 新建 [test_launch_command.py](../../ECL/game/tests/test_launch_command.py) | `test_parse_launch_command_preserves_quoted_token` | 中文/含空格值在引号内保持一个 token |
| 同上 | `test_validate_launch_command_accepts_space_version_name` | Forge 形态命令通过校验；`-DignoreList` token 含完整版本名；主类 token 正确 |
| 同上 | `test_validate_launch_command_rejects_split_version_name` | 裸版本名导致 token 切分时抛出 `LAUNCH_COMMAND_INVALID` |
| 同上 | `test_validate_launch_command_rejects_split_or_missing_main_class` | 主类被切开、缺失或重复时抛出 |
| 同上 | `test_validate_launch_command_rejects_invalid_main_class` | 主类含空白/引号时抛出 |
| 修改 [test_placeholder_replacer.py](../../ECL/game/tests/test_placeholder_replacer.py) | `test_version_name_is_quoted_in_every_placeholder_position` | 两处 `${version_name}` 都输出为带引号的值 |
| 修改 [test_game_service.py](../../tests/test_game_service.py) | `test_command_builder_quotes_version_name_for_forge_ignore_list` | 端到端构造 Forge 命令，解析 argv 后 `-DignoreList` 是单个 token，主类是 `BootstrapLauncher` |
| 同上 | `test_command_builder_rejects_invalid_main_class_with_actionable_error` | 非法主类在启动前失败，而不是交给 Java |

既有用例 [test_command_builder_places_neoforge_game_arguments_after_main_class](../../tests/test_game_service.py#L1391-L1433) 与 [test_imported_instance_builds_core_launch_command_with_complete_metadata](../../tests/test_modpack_preparation.py#L304-L351) 必须继续通过，用于证明无空格的普通实例没有回归。

## 1.3 不改什么

- 不把命令改为 argv 列表；不改 `InstancesManager.create_instance` 的参数签名。
- 不修改 Forge/NeoForge 下载得到的版本 JSON，也不在安装时重写 `-DignoreList`。
- 不删除或改变 `JvmArgumentBuilder.add_jvm_args` 现有的 `.replace(" ", "")` 行为；它只处理原始 JSON 参数，不负责占位符展开后的空格。
- 不改 classpath 构建、natives 解压、继承版本查找、文件补全逻辑。
- 不限制实例名不能含空格，也不迁移或改名已有实例。
- 不改变自定义 `_command_builder` 的注入语义；校验位于默认构建器 `build_minecraft_cmd` 内，测试用自定义构建器不受影响。
- 不为 `ECLauncherCore.py` 的历史 Ruff 豁免做全文件收敛；本次只在 `ECL/game/ruff.toml` 既有豁免下修改该文件，新增 `LaunchCommand.py` 则按新文件标准通过检查。

## 1.4 实施步骤

1. 新增 `ECL/game/Core/LaunchCommand.py`，实现平台解析与校验函数，添加 Core 文件头。
2. 修改 `ECLauncherCore.py`：`version_name` 纳入标准替换，删除 `_replace_last`，在 `build_minecraft_cmd` 返回前调用校验。
3. 修改 `launch.py`：增加 `CoreError` 导入与错误映射分支。
4. 新增/修改第 1.2.4 节测试，先运行 Core 定向测试，再运行主仓库集成测试。
5. 执行第 1.5 节全部检查；在真实启动器中启动 `Zombie Invade 100 Days`，确认不再出现 `ClassNotFoundException: Invade`，并回归启动一个无空格实例。
6. 验收通过后按 AGENTS 第 7 节提交；本次不自动推送。

## 1.5 测试计划与命令

### 判定

启动命令构建属于 Core 公共实现，且直接影响进程创建，不属于“单一后端领域局部修复”。按 AGENTS 6.1，应执行 **Core 子仓库全量验证 + 主仓库后端全量验证 + 真实启动器验收**。

### 命令

在 Core 子仓库（`ECL/game`）：

```powershell
ruff check .
```

在主仓库根目录：

```powershell
python -m pytest ECL/game/tests --tb=short -q
python -m pytest tests/test_game_service.py tests/test_modpack_preparation.py tests/test_plugin_game_launch.py tests/test_plugin_java_game_launch.py --tb=short -q
ruff check ECL tests
ruff format --check ECL tests
python -m pytest --tb=short -q
```

说明：Core 的测试依赖主仓库的 `ECL.game.*` 目录结构，不能在 `ECL/game` 内独立 `pytest`；按 [ruff.toml](../../ECL/game/ruff.toml#L1-L7) 的既有约定，Core 测试门禁由主仓库的 `pytest ECL/game/tests` 承担。`ECLauncherCore.py` 在 [ruff.toml](../../ECL/game/ruff.toml#L40-L56) 的既存豁免列表中，本次不扩大为全文件格式整理；新增 `LaunchCommand.py` 必须通过 `ruff check .`。

### 验收标准

- 新增 `test_command_builder_quotes_version_name_for_forge_ignore_list` 断言：
  - 解析后的 argv 中存在且仅存在一个以 `-DignoreList=` 开头的 token；
  - 该 token 完整包含 `Zombie Invade 100 Days.jar`；
  - argv 中不存在等于 `Invade`、`100`、`Days.jar` 的独立 token；
  - argv 中存在且仅存在一个等于 `cpw.mods.bootstraplauncher.BootstrapLauncher` 的 token。
- 无空格实例（如 `neoforge`、`1.21`）的既有命令构建测试全部通过。
- 非法主类测试得到 `LAUNCH_COMMAND_INVALID`，不再创建进程。
- 真实启动 `Zombie Invade 100 Days` 后：
  - 日志不再出现 `找不到或无法加载主类 Invade`；
  - 进程不会在创建后立即以退出码 1 结束；
  - Forge 正常进入加载阶段；
  - 无空格实例启动无回归。

## 1.6 风险与回退

- **重复加引号风险**：若未来某个版本 JSON 已经把 `${version_name}` 放在引号内，统一加引号可能产生双引号。已知当前所有版本 JSON 均为独立占位符或 `...,${version_name}.jar` 形态；新增校验会把它变成明确的 `LAUNCH_COMMAND_INVALID`，而不是静默启动失败。若真实出现，再单独实现“感知已有引号”的替换，不在本次扩大。
- **次数校验的边界**：校验使用原始命令中的版本名出现次数与解析后 token 内的次数比较。版本名含引号、反斜杠等 Windows 文件名不允许的字符时可能误报；这类实例名本身不应被支持，误报会以明确错误阻止启动，不会错误放行。
- **错误码映射**：新增 `CoreError` 分支只承接 Core 命令构建异常；现有 `FileNotFoundError` 仍走通用分支。测试覆盖两种路径。
- **回退**：还原 `ECLauncherCore.py`、`LaunchCommand.py`、`launch.py` 的改动即可；本方案不写用户数据、不改版本 JSON，无迁移或清理需求。

---

# 第二章：整合包图标缓存

## 2.1 问题与根因

**一句话结论：在线资源详情和前端已经拿到整合包图标，但安装计划、准备请求和安装结果都没有图标字段；本地导入只复制 overrides，安装完成后也不写 `.ecl/icon.*`，所以实例只能回退到 Forge/NeoForge 加载器图标。**

关键证据：

- 在线图标已经可取：
  - Modrinth：[resources.py](../../ECL/services/game/resources.py#L1180-L1186)。
  - CurseForge：[resources.py](../../ECL/services/game/resources.py#L1139-L1162)。
  - FTB：[resources.py](../../ECL/services/game/resources.py#L1211-L1235)。
- 前端已经展示图标：[OnlineModSearch.vue](../../frontend/src/components/mods/OnlineModSearch.vue#L1090-L1091)、[OnlineModSearch.vue](../../frontend/src/components/mods/OnlineModSearch.vue#L133)。
- 在线安装意图没有携带图标：[OnlineModSearch.vue](../../frontend/src/components/mods/OnlineModSearch.vue#L1220-L1231)、[modpackImportStore.ts](../../frontend/src/features/instances/stores/modpackImportStore.ts#L34-L39)。
- 在线准备请求模型没有图标：[models.py](../../ECL/api/models.py#L621-L624)；前端预览结构也没有图标：[modpackApi.ts](../../frontend/src/features/instances/api/modpackApi.ts#L25-L43)。
- 安装计划没有图标：[modpack.py](../../ECL/services/game/modpack.py#L154-L172)。
- 本地导入只复制 overrides：[modpack.py](../../ECL/services/game/modpack.py#L1339-L1365)；最终只写版本 json/jar：[modpack.py](../../ECL/services/game/modpack.py#L1367-L1396)。
- 安装完成后实例无 `.ecl` 和 `icon.*`（实地检查 `Zombie Invade 100 Days` 目录）。
- 自动图标规则：[instance_profiles.py](../../ECL/services/game/instance_profiles.py#L87-L101)；优先级：[instance_profiles.py](../../ECL/services/game/instance_profiles.py#L639-L660)。
- 现有本地图标存储入口：[instance_profiles.py](../../ECL/services/game/instance_profiles.py#L420-L471)；写资料并触发刷新：[profiles.py](../../ECL/services/game/profiles.py#L141-L165)。
- 扫描缓存已监听 `.ecl/icon.*`：[scan.py](../../ECL/services/game/scan.py#L193-L220)。

## 2.2 实施范围

### 2.2.1 修改 `ECL/services/game/modpack.py`

文件：[modpack.py](../../ECL/services/game/modpack.py)

#### 新增 `ModpackIconPolicy`

放在 `ModpackFormatPolicy` 之后（约第 133 行），只承载本次图标规则：

- `candidate_names`：按优先级排列的文件名（casefold 后比较）。
- `content_type_extensions`：`image/png`、`image/jpeg`、`image/webp`、`image/gif`、`image/bmp` 到扩展名的映射。
- `max_bytes`：`10 * 1024 * 1024`，与 `InstanceProfileStore` 的图标上限保持一致；注释说明该值需与实例资料图标校验同步。

候选文件名优先级：

1. `icon.png`
2. `icon.jpg` / `icon.jpeg`
3. `icon.webp`
4. `icon.gif`
5. `icon.bmp`
6. `pack.png`
7. `logo.png` / `Logo.png`（按 casefold 匹配）

只检查两个根目录：

- `layout_root`：`_detect_pack_layout` 返回的清单所在目录，覆盖 MultiMC 根目录 `icon.png` 和 HMCL 单层包装。
- `extracted`：解压内容根目录，覆盖根目录直接放图标的整合包。

不递归扫描 `overrides/**`，避免把某个模组或资源包的图标误当整合包图标。

#### 扩展 `ModpackPlan`

在 [modpack.py](../../ECL/services/game/modpack.py#L154-L172) 增加两个可选字段：

```python
icon_url: str | None = None
icon_source_path: Path | None = None
```

`ModpackPlan` 仍是 frozen dataclass；新增字段带默认值，已有构造调用不受影响。准备阶段用 `dataclasses.replace` 写入。

#### 扩展 `build_ftb_plan`

在 [modpack.py](../../ECL/services/game/modpack.py#L686-L719) 中从已获取的 `info` 读取图标：

- 取 `info["art"]` 中第一个 `type == "square"` 且 `url` 为非空字符串的条目。
- 逻辑与 [resources.py](../../ECL/services/game/resources.py#L1211-L1216) 的 FTB 详情一致。
- 写入 `ModpackPlan.icon_url`；无匹配时为 `None`。
- 不新增网络请求；FTB 的 `info` 已由 `_fetch_ftb_plan` 获取。

#### 扩展 `prepare_modpack`

在 [modpack.py](../../ECL/services/game/modpack.py#L839-L926) 的准备 worker 中：

- 本地来源（`source == "local"`）：
  - 在 `_detect_pack_layout` 之后调用 `_pack_icon_candidate(layout_root, extracted)`。
  - 命中时 `replace(plan, icon_source_path=candidate)`。
  - 未命中时保持 `None`。
- Modrinth / CurseForge：
  - 调用已有的 `self.fetch_project_info(source, project_id, "modpack")`。该方法在 [resources.py](../../ECL/services/game/resources.py#L1083-L1109)，内部已处理模组源切换与图标地址重写。
  - 从返回值取 `iconUrl`，只接受 `http://` 或 `https://` 字符串；否则视为无图标。
  - 命中时 `replace(plan, icon_url=icon_url)`。
  - 该方法失败（网络、API Key、结构无效）时记录警告并继续准备，不阻断预览。
- FTB：
  - 直接使用 `build_ftb_plan` 写入的 `icon_url`，不再请求项目详情。
- 图标失败只影响图标字段；`plan.warnings` 是否加入提示由实现时统一选择，但不得把图标失败升级为安装失败。

`ModpackCoordinator` 与 `ResourceCoordinator`、`ProfileCoordinator` 通过 [GameService](../../ECL/services/game/__init__.py#L32-L45) 组合；`self.fetch_project_info` 与 `self.set_instance_icon` 在完整服务中可用。若测试直接实例化 `ModpackCoordinator`，需要注入或替换这两个方法，不把组合关系复制成新的服务层。

#### 扩展 `_install_plan_into_instance`

在 [modpack.py](../../ECL/services/game/modpack.py#L1097-L1143) 中：

1. 在 `context.finalize` 之前准备图标本地文件：
   - `plan.icon_source_path` 存在：直接使用该路径，不复制。
   - `plan.icon_url` 存在：调用新增 `_download_pack_icon(url, destination, context)`，下载到 `staging.parent` 下的临时文件；文件名带实例名与随机标识，避免并发冲突。
   - 下载成功返回 `(path, is_temporary=True)`；本地来源返回 `(path, False)`。
2. 执行现有 `context.finalize(lambda: _commit_staged_instance(...))`。
3. 提交成功后，调用现有 `self.set_instance_icon(target.minecraft_root_path, target.instance_directory_name, "local", source_path=icon_path)`：
   - 复用 [profiles.py](../../ECL/services/game/profiles.py#L141-L165) 的校验、原子写入和 `_notify_profile_changed`。
   - 捕获 `GameServiceError`、`OSError`、`ValueError`，把原因追加到返回结果的 `warnings`，并记录日志；不抛出。
4. 在 `finally` 中删除 `is_temporary=True` 的临时图标文件；本地来源文件不删除。
5. 返回结果的 `warnings` 已包含图标失败说明；其他字段保持不变。

**顺序约束**：图标下载属于网络操作，必须在 `context.finalize` 之前完成；`finalize` 内只允许短时原子提交，不得加入网络请求。

本地 `icon_source_path` 指向准备快照目录中的解压文件，安装 worker 执行期间该目录由准备记录保持；安装结束后随准备记录清理。该路径只在内存计划中使用，不写入实例资料，也不跨任务复用。

#### 新增下载辅助

`_download_pack_icon`：

- 使用 [resources.py](../../ECL/services/game/resources.py#L122-L125) 的 `_proxied_stream`，`timeout=15`、`follow_redirects=True`。
- 先检查 `content-length`；超过 `ModpackIconPolicy.max_bytes` 直接放弃。
- 按块读取并累计字节；超过上限立即中止并清理临时文件。
- 扩展名优先取 `Content-Type` 映射；缺失或未知时取 URL 路径后缀；仍无法确定则放弃。
- 返回临时路径；任何异常转换为“无图标 + 警告”，由调用方处理。

### 2.2.2 复用而非修改的现有能力

- [instance_profiles.py](../../ECL/services/game/instance_profiles.py#L420-L471) 的 `set_icon`：负责 10 MiB、4096px、格式校验，写入 `.ecl/icon.<ext>`，并写 `icon: {"type": "local", "value": "icon.<ext>"}`。
- [profiles.py](../../ECL/services/game/profiles.py#L141-L165) 的 `set_instance_icon`：负责错误转换与实例列表刷新通知。
- [scan.py](../../ECL/services/game/scan.py#L193-L220)：已把 `.ecl/icon.*` 纳入缓存快照；安装后无需额外全量扫描即可刷新。
- [InstanceIcon.vue](../../frontend/src/components/instances/InstanceIcon.vue#L35-L43)：已支持 `type: "local"`；不改图标优先级。

### 2.2.3 可选的最小前端加固

为覆盖“图标文件存在但已损坏”的情况，建议在 [InstanceIcon.vue](../../frontend/src/components/instances/InstanceIcon.vue#L47-L56) 增加：

- `@error` 处理：本地/外部图标加载失败时清空 `localUrl`，回退到现有 `UiIcon` 加载器图标。
- 不新增 IPC 字段，不改变图标优先级，不改其他页面。

如果用户选择严格只做后端缓存，可删除本小节；此时“图标被删除”仍会回退（`file.toUrl` 为空），“图标被替换成损坏文件”会显示破图，需接受该边界。

### 2.2.4 IPC 与前端是否需要改动

**推荐方案（后端按 `source + project_id` 取图标）：不需要 IPC 契约改动，也不需要前端请求/响应改动。**

- `ModpackOnlinePrepareRequest` 已有 `source`、`project_id`、`file_id`，足够后端查询项目详情。
- 安装结果结构不变；图标通过实例资料生效。
- 前端只在实例列表重新渲染时读取新图标，不需要改 `modpackApi.ts`。
- 若纳入 2.2.3 的 `@error`，只改 `InstanceIcon.vue` 与对应测试，不涉及 IPC。

**备选方案（前端把已展示的 iconUrl 传给后端）：需要以下契约变更**

- `ECL/api/models.py` 的 `ModpackOnlinePrepareRequest` 增加可选 `icon_url`，并在后端校验协议、主机白名单和长度。
- `frontend/src/features/instances/stores/modpackImportStore.ts` 的 `OnlinePackTarget` 增加 `iconUrl`。
- `frontend/src/components/mods/OnlineModSearch.vue` 的 `openModpackOnlineInstall` 传入 `mod.iconUrl`。
- `frontend/src/features/instances/api/modpackApi.ts` 的 `prepare` 请求体增加 `icon_url`。
- 该方案省一次项目详情请求，但引入客户端 URL 信任问题，不推荐作为默认实现。

**预览页展示图标**不在本次范围；若需要，另行给 `ModpackPreview` 增加 `iconUrl` 字段并同步前端。

## 2.3 不改什么

- 不修改实例资料 schema；`icon` 仍只允许 `type`、`value`，不持久化来源 URL、项目 ID 或图标字节。
- 不改变图标优先级、自动图标规则、`InstanceIcon` 的现有回退顺序（除 2.2.3 的 `@error`）。
- 不在每次扫描时重新下载或重新读取压缩包；图标只在安装时获取一次。
- 不修改 ECL 旧格式导入流程；该流程继续整体复制目录，根目录 `icon.*` 的现有 HMCL 兼容行为保持不变。
- 不修改资源搜索/详情页的图标展示。
- 不新增后台图标刷新、图标更新或图标清理任务。
- 不把图标失败升级为安装失败，也不在安装结果中新增必填字段。

## 2.4 实施步骤

1. 在 `modpack.py` 增加 `ModpackIconPolicy`、`ModpackPlan` 图标字段。
2. 扩展 `build_ftb_plan` 读取 FTB `art` 图标。
3. 在 `prepare_modpack` 中接入本地候选检测与 Modrinth/CurseForge 项目详情图标。
4. 在 `_install_plan_into_instance` 中接入安装时下载、提交后 `set_instance_icon`、临时文件清理与警告回退。
5. 如批准 2.2.3，修改 `InstanceIcon.vue` 的 `@error` 回退。
6. 新增/修改第 2.5 节测试，执行后端检查与真实界面验收。
7. 验收通过后按 AGENTS 第 7 节提交；本次不自动推送。

## 2.5 测试计划与命令

### 判定

图标缓存涉及安装流程、实例资料持久化和共享状态，按 AGENTS 6.1 不属于局部样式改动，应执行 **主仓库后端全量验证**。若纳入 2.2.3 的 `InstanceIcon.vue` 修改，该组件被实例列表、详情页和运行实例复用，按“公共组件实现”执行 **前端全量 `pnpm check` + `pnpm build` + 真实启动器验收**。

### 命令

主仓库根目录：

```powershell
ruff check ECL tests
ruff format --check ECL tests
python -m pytest tests/test_modpack.py tests/test_modpack_preparation.py tests/test_instance_profiles.py --tb=short -q
python -m pytest --tb=short -q
```

如修改前端 `InstanceIcon.vue`，在 `frontend`：

```powershell
pnpm exec prettier --check src/components/instances/InstanceIcon.vue src/components/instances/InstanceIcon.test.ts
pnpm exec eslint src/components/instances/InstanceIcon.vue src/components/instances/InstanceIcon.test.ts
pnpm typecheck
pnpm exec vitest run src/components/instances/InstanceIcon.test.ts
pnpm check
pnpm build
```

### 新增/修改测试

| 文件 | 用例 | 覆盖点 |
| --- | --- | --- |
| [test_modpack.py](../../tests/test_modpack.py) | `test_local_pack_icon_candidates_follow_priority` | `icon.png` > `pack.png` > `logo.png`；`Logo.png` 大小写不敏感；只读根目录 |
| 同上 | `test_local_pack_icon_candidate_returns_none_when_absent` | 无候选时 `icon_source_path is None` |
| 同上 | `test_build_ftb_plan_extracts_square_icon` | FTB `art` 中 `type == "square"` 的 URL 写入 `icon_url`；无匹配时为 `None` |
| [test_modpack_preparation.py](../../tests/test_modpack_preparation.py) | `test_install_caches_online_pack_icon_into_ecl` | 假项目详情返回 iconUrl，假下载写入合法 PNG；安装后 `.ecl/icon.png` 存在，`.ecl/instance.json` 的 `icon` 为 local |
| 同上 | `test_installed_pack_icon_is_visible_in_scan` | 安装后 `scan`/`enrich_version` 返回 `type == "local"`，路径指向 `.ecl/icon.png` |
| 同上 | `test_install_keeps_instance_when_pack_icon_download_fails` | 下载抛错时安装仍成功；无 `.ecl/icon.png`；结果 warnings 含图标失败；版本元数据与启动描述完整 |
| 同上 | `test_install_caches_local_pack_icon_into_ecl` | 本地 zip 根目录含 `icon.png`；安装后缓存到 `.ecl/icon.png` |
| 新建 [InstanceIcon.test.ts](../../frontend/src/components/instances/InstanceIcon.test.ts) | `falls back to loader icon when local image fails` | 触发 `img` 的 `error` 后显示 `UiIcon`，不再显示破图 |

`test_modpack.py` 文件头的测试清单需同步加入新增用例名，保持现有文件约定。

### 验收标准

- 安装带图标的在线整合包后：
  - 实例目录出现 `.ecl/icon.png`（或对应扩展名）；
  - `.ecl/instance.json` 含 `"icon": {"type": "local", "value": "icon.png"}`；
  - 实例列表/详情显示该图标，而不是 Forge 加载器图标；
  - 重启启动器并断网后仍显示该图标（不依赖运行时网络）。
- 安装未提供图标或图标下载失败的整合包后：
  - 安装成功，实例可启动；
  - 实例列表回退到加载器图标；
  - 日志或结果 warnings 说明图标未缓存，但不出现安装失败。
- 手动删除 `.ecl/icon.png` 并刷新列表后，回退到加载器图标。
- 若纳入 2.2.3，手动把 `.ecl/icon.png` 替换为损坏文件后，列表回退到加载器图标而不是破图。

### 真实界面验收

- 默认窗口、最小窗口 960×600、放大窗口下检查实例列表与详情页图标尺寸、裁切和间距。
- Classic / Folia、亮暗主题下检查图标显示。
- 不做全站巡检，只验收实例列表、实例详情和运行实例三处消费 `InstanceIcon` 的入口。

## 2.6 风险与回退

- **额外请求**：Modrinth / CurseForge 各增加一次项目详情请求。准备阶段已有网络请求，图标请求失败不影响安装；若认为预览延迟不可接受，可改为安装时按 `source + project_id` 再查一次，但需要给 `ModpackPlan` 增加来源标识。推荐维持准备阶段取 URL、安装阶段下载字节。
- **图标 URL 失效**：准备阶段取到 URL、安装阶段下载失败时跳过图标；不重试、不阻断安装。
- **格式不支持/超限**：由扩展名、Content-Type、10 MiB 上限和 `set_icon` 的 4096px 校验共同拦截；失败仅记录警告。
- **本地误识别**：只读根目录且按固定优先级，不递归 overrides；若根目录存在与整合包无关的 `icon.png`，会作为包图标使用，这是可接受的约定。
- **提交后写图标失败**：实例已落位，图标写入失败只影响展示；不会回滚实例，也不会留下半安装状态。
- **回退**：移除 `ModpackPlan` 图标字段、准备/安装图标逻辑与 2.2.3 的前端 `@error` 即可；已写入的 `.ecl/icon.*` 与 `instance.json` 仍然有效，用户可在实例详情中改回“自动”，不需要迁移或删除用户文件。

---

# 3. 长期 argv 列表重构的决策

本次不做“启动命令改为 argv 列表”，原因是：

- 它需要同时改 Core 的 `JvmArgumentBuilder` / `PlaceholderReplacer` / `build_minecraft_cmd`、主仓库 `launch.py`、`InstancesManager.create_instance`、插件启动路径，以及 wrapper/pre/post 命令的字符串语义。
- 会触及 Windows 与 POSIX 两种进程创建方式，回归面远大于当前 bug。
- 当前问题可以用“所有 `${version_name}` 统一引用 + 启动前 argv 校验”局部修复，并能用真实 Forge 实例验收；先交付最小修复更符合当前风险控制。

出现以下任一条件时重新评估：

1. 又有用户可控值（实例别名、Java 路径、游戏参数等）以未受控方式进入命令字符串并造成切分。
2. wrapper/pre/post 命令或插件启动需要可靠的参数边界，字符串拼接无法再保证。
3. 启动器开始正式支持 Windows 以外的桌面平台，需要统一的 argv 语义。
4. 现有“占位符 + 引号”规则出现更多已知 JSON 形态，校验开始频繁误报。
5. 下一次启动命令相关的大范围重构或插件启动协议升级。

重新评估时应单独制定方案，不把 argv 重构夹带进本方案的两个章节。

---

# 4. 共同验收与交付边界

- 两个章节可分别实施、分别验收；第一章优先，因为它直接阻断启动。
- 两章都完成后，按 AGENTS 第 7 节分别提交，提交信息使用中文 Conventional Commits；先集成本地 `main`，默认不推送、不打标签、不发布。
- 第一章涉及 Core 子仓库：先验证并提交 `ECL/game` 的本地 `main`，再在主仓库集成对应指针，并运行主仓库集成测试。
- 第二章只涉及主仓库与前端（若纳入 2.2.3）；前端改动先集成本地 `frontend` 的 `main`，再集成主仓库指针。
- 真实启动器验收结果与自动化测试结果分开记录，不把 pytest 或 jsdom 通过泛化为桌面界面通过。

# 5. 需要用户确认的点

1. 第二章是否纳入 2.2.3 的 `InstanceIcon.vue` `@error` 回退。纳入会多一次前端全量检查与真实界面验收；不纳入则“图标被替换为损坏文件”会显示破图。
2. 是否接受 Modrinth / CurseForge 在准备阶段各增加一次项目详情请求。推荐接受；备选是前端传 `iconUrl`，但需要 IPC 契约变更。
3. ECL 旧格式导入的图标缓存是否保持现状。本方案默认不改，避免扩大导入流程范围。
</content>
