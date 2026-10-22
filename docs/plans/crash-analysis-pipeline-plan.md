# 崩溃分析结构化管道升级实施方案（crash/ 子包 + 规则目录 + 三源 Mod 索引 + crash_capture 拆分）

> 状态：待用户确认（AGENTS.md 工作流第 5 步）
> 日期：2026-09-27
> 来源：[docs/feature-comparison-and-optimization-plan.md](feature-comparison-and-optimization-plan.md) P1-9 / 4.4-13；连带纳入 4.2-7 中 crash_capture 拆分
> 已确认决策：规则**精选扩充**（30 → 约 55-60 条，含 HMCL 移植）；代码组织为 **`crash/` 子包**；结果结构**增量兼容**（不 breaking）；**一并拆分** launch.py 崩溃检测/调度。

## 1. 现状与差距

现状：`ECL/services/game/crash_analysis.py`（913 行单文件）承担全部职责——30 条 `_Rule`、日志收集、规则匹配、堆栈兜底、mods 目录包名映射、会话报告管理、导出。启动链路（`launch.py:225-1212`）内联崩溃标记、检测信号与分析调度。

| 缺口 | 现状 | 目标（对标） |
| --- | --- | --- |
| 规则目录未独立 | 规则与引擎同文件，堆规则会继续膨胀 | 声明式 `CrashRule` 目录独立成模块（PCL-CE `CrashRuleCatalog` 设计思路） |
| 参数提取依赖临场正则 | `_parameters()` 仅对 world/jar 特判 | HMCL 命名捕获组 → `parameters`（mod/file/id/class/expected 等） |
| 堆栈分析浅 | 仅在规则全部未命中时兜底，取前 3 段包名 | 完整堆栈解析（`Caused by` 链、主嫌帧、lambda 归并），并作为伴随证据常驻管道 |
| Mod 索引单源 | 只扫 mods 目录 JAR | 三源索引：崩溃报告 Mod 列表 ∪ 加载器调试日志 ∪ mods 目录（PCL-CE `CrashModIndex` 思路） |
| 无置信度聚合 | 命中后只保留最小 priority 组，堆栈结果二选一 | 按置信度/优先级排序输出主因 + 伴随因素（最多 5 条），堆栈归因作为 `possible` 级伴随 |
| 检测/调度内联在 launch.py | `crash_log_markers`、`_crash_detection_signals`、`_schedule_crash_analysis` 共约 100 行 | 拆为 `crash/capture.py`，launch.py 保留调用点 |

参考与许可：HMCL `CrashReportAnalyzer.java`（GPL-3.0，与本仓同许可，可移植正则与命名组，须保留署名注释——现有文件头已有先例）；PCL-CE `CrashAnalysis/`（自定义许可，仅借鉴管道分层设计，不复制代码）。

## 2. 目标管道

```
源收集（runtime / manual / ZIP，现有逻辑不变）
  → 文本规整与脱敏（现有逻辑不变）
  → 规则匹配（rules.py：命名捕获组 → parameters）
  → 堆栈分析（stacks.py：主嫌帧与包名候选，常驻执行）
  → Mod 索引归因（mod_index.py：三源索引，class/id/包名 → 肇事 Mod）
  → 置信度聚合（排序、去重、最多 5 条）
  → 结果组装与落盘（analysis.json / analysis.txt，增量字段）
  → 插件富化（ECL/plugins/crash_extensions.py，保持管道末端与现有契约）
```

## 3. 代码组织

```
ECL/services/game/crash/
├── __init__.py      # 包标记
├── rules.py         # CrashRule dataclass + CrashRuleCatalog（30 条迁移 + HMCL 精选移植 + 命名组定义）
├── stacks.py        # CrashStackAnalyzer：堆栈帧解析、主嫌帧、包名候选、lambda/合成帧归并
├── mod_index.py     # CrashModIndex：三源 Mod 索引 + 会话内指纹缓存
├── capture.py       # CrashCapture：崩溃标记、检测信号、分析调度、launcher:error 事件发射（自 launch.py 迁出）
└── analyzer.py      # CrashAnalyzer：管道编排 + 会话报告管理（自 crash_analysis.py 迁移主体）
ECL/services/game/crash_analysis.py   # 薄门面：re-export CrashAnalyzer，防止外部导入路径断裂
```

- 站内导入全部迁移：`base.py:51/176`、`tests/test_crash_analysis.py` 等改为 `from .crash.analyzer import CrashAnalyzer`；门面仅作过渡兼容。
- `crash_analysis.py` 文件头更新为门面说明；`rules.py` 文件头保留并扩充 HMCL 署名与 GPL-3.0 说明。
- 命名遵循规范：无新全大写常量；规则目录为领域类属性（沿用 `CrashAnalysisPolicy` → `CrashRuleCatalog` 模式）；堆栈快照用 `@dataclass(frozen=True, slots=True)`。

## 4. 规则目录升级（rules.py）

### 4.1 结构

```python
@dataclass(frozen=True, slots=True)
class CrashRule:
    code: str                       # 稳定原因码，前端 i18n 键为 code.replace(".", "_")
    confidence: Confidence          # certain / likely / possible
    priority: int                   # 数值越小越优先
    patterns: tuple[re.Pattern[str], ...]
    parameter_groups: Mapping[str, str] = MappingProxyType({})  # 命名捕获组 → parameters 键
    mod_hint_groups: tuple[str, ...] = ()   # 其捕获值送 Mod 索引反查肇事 Mod（class/id）
```

匹配语义不变（逐条扫描、证据行归一化截断 320 字符、单条证据上限 3 行）；新增：命名捕获组命中后写入 `parameters`，`mod_hint_groups` 的捕获值交给 Mod 索引反查。

### 4.2 HMCL 精选移植映射

| 处置 | HMCL 来源 | ECL 目标 |
| --- | --- | --- |
| 增补 patterns | MEMORY_EXCEEDED、RESOLUTION_TOO_HIGH、NIGHT_CONFIG_FIXES、SHADERS_MOD、FORGE_REPEAT_INSTALLATION、INCOMPLETE_FORGE_INSTALLATION、FABRIC_WARNINGS、MOD_RESOLUTION0、MODLAUNCHER_8 细化 | `memory.out_of_memory`、`resource.render_failure`、`mod.config_failure`、`mod.optifine_conflict`、`loader.install_incomplete`、`mod.incompatible`、`mod.loader_reported` 等现有码 |
| 增补 patterns + 命名组 | TOO_OLD_JAVA（expected）、FILE_CHANGED（file）、DUPLICATED_MOD（name/path）、FORGE_FOUND_DUPLICATE_MODS、MOD_RESOLUTION_MISSING/-_MINECRAFT/-_COLLECTION（sourcemod/destmod/version）、MOD_RESOLUTION_CONFLICT、FORGEMOD_RESOLUTION、CONFIG（id/file）、LOADING_CRASHED_FORGE/FABRIC、BOOTSTRAP_FAILED（id）、BLOCK/ENTITY（type/location）、UNSATISFIED_LINK_ERROR（name）、OPTIFINE 冲突系列（World 加载失败、Forest 系列 5 条合并） | 对应现有码 + `parameters` 参数化；OptiFine×模组冲突五条合并进 `mod.optifine_conflict` |
| 新增原因码 | NO_SUCH_METHOD_ERROR / NO_CLASS_DEF_FOUND_ERROR / ILLEGAL_ACCESS_ERROR（class） | `mod.class_resolution_failure`（class 值送 Mod 索引归因，价值最高） |
| 新增原因码 | TOO_MANY_MODS…ID_LIMIT、MOD_NAME、INSTALL_MIXINBOOTSTRAP + FABRIC_VERSION_0_12、OPTIFINE_REPEAT_INSTALLATION、FILE_ALREADY_EXISTS、MACOS_FAILED_TO_FIND_SERVICE_PORT、MAC_JDK_8U261、RTSS_FOREST_SODIUM、FORGE_ERROR | `mod.id_limit_exceeded`、`mod.invalid_module_name`、`mod.mixin_bootstrap_missing`、`mod.optifine_duplicate`、`files.already_exists`、`graphics.macos_glfw`、`java.mac_jdk_legacy`、`graphics.rtss_sodium`、`loader.forge_error_screen` |
| 不移植 | OPENJ9/JDK_9/JVM_32BIT/OPENGL_NOT_SUPPORTED/GRAPHICS_DRIVER/OUT_OF_MEMORY/DEBUG_CRASH/GL_OPERATION_FAILURE/MOD_FILES_ARE_DECOMPRESSED 等 | ECL 现有规则已覆盖且表达更完整 |

最终规则目录为 38 条规则、113 个匹配模式（28 条现有规则 + 10 条新码；HMCL 的多条同域正则在 ECL 合并为单条原因的多个模式，故条目数低于早期估算），新码共 10 个。每个新码需在前端 6 语言补 `error.crash.reasons.<code_下划线化>`（title + 2 条 suggestions）。所有移植正则在 Python `re` 语义下逐条校验（命名组须写作 `(?P<name>…)`，Java 风格 `(?<name>…)` 不被 `re` 支持），并以代表样例日志做单测。

## 5. 堆栈分析器（stacks.py）

- 迁移并升级现有 `_stack_candidates` / `ignored_stack_prefixes`：
  - 按 `Caused by` / 异常行分段，逐段收集 `at class.method(Source)` 帧；
  - 主嫌帧 = 该段首个非忽略前缀帧（现有前缀表迁入）；`class$lambda$…`、匿名/合成帧归并到外层类；
  - 输出 `frozen dataclass CrashStackFrame(class_name, method, package)` 有序列表（保留 12 包候选上限）。
- 行为变化：堆栈分析从"规则未命中才执行"改为**常驻执行**——规则命中时其结果作为伴随 `possible` 级原因追加（仅在反查到 Mod 或包名时），未命中规则时仍作为唯一兜底（保持现有行为）。

## 6. 三源 Mod 索引（mod_index.py）

| 来源 | 提取内容 |
| --- | --- |
| mods 目录 JAR（现有 `_mod_package_map` / `_mod_display_name` 迁入） | 包名前缀 → 显示名（fabric.mod.json / neoforge.mods.toml / jar 文件名） |
| 崩溃报告内嵌 Mod 列表 | FML 报告表（`Mod File:` / `Failure message:` 块）、旧版 Forge 模组列表段、Fabric `-- MODS --` 段 → mod_id → 显示名/文件 |
| 加载器调试日志（debug.log） | Forge `Found mod file` / `Loading mod file`、Fabric `Loading N mods` 列表行 → mod_id → 文件 |

- 归因接口：`resolve(packages: Iterable[str], class_names: Iterable[str]) -> CrashModAttribution`，返回 `mods`（去重显示名/文件名，上限 8）与 `packages`（未能映射的候选包名）。
- 会话内缓存：按 mods 目录（根目录与版本隔离目录）的路径 + mtime + 条目数指纹缓存 JAR 扫描结果；分析会话关闭即失效，文件替换（mtime 变化）自动重扫。
- 现有 `stack.suspected_mod` / `stack.suspected_component` 两码语义保留。

## 7. 结果契约（增量兼容）

```ts
interface CrashReason {
  code: string
  confidence: CrashConfidence
  evidence: string[]
  parameters: Record<string, unknown>
  mods?: string[]      // 新增：归因到的 Mod（显示名或文件名）
  packages?: string[]  // 新增：堆栈候选包名（stack 类原因携带）
}
```

- 顶层 `CrashAnalysisResult` 结构不变；`analysis.json`、`launcher:error` 事件 payload、`game_crash_analyze` 返回值自动携带新字段。
- 聚合行为变化（需回归验证）：reasons 列表从"仅最小 priority 组"改为按 `(priority, confidence)` 排序、去重、上限 5 条；前端按数组渲染，天然兼容；插件富化契约不变（仍末端追加/覆盖）。
- `analysis.txt` 可读报告同步追加 Mod 归因行。

## 8. crash_capture 拆分（launch.py → crash/capture.py）

- `CrashCapture` 持有：`crash_log_markers`、`_crash_futures`、executor 与 analyzer 引用、closing 标志；方法：
  - `handle_line(run_token, line)` — 崩溃标记状态（`crash_marked` 改由 capture 自持，`_RunningGame` 去掉该字段）；
  - `finalize(snapshot)` — 信号判定（`exit_code` / `crash_log` / `startup_incomplete`，语义与 `disable_crash_analysis` 跳过逻辑不变）+ 调度分析；
  - `close()` — 等待/放弃在途 future（现有 `_crash_futures` 语义不变）。
- 调用边界：launch.py 在 `_handle_instance_log` / `_finalize_instance_run` 处构造 `frozen dataclass` 快照（version_id、game_path、game_directory、started_wall_time、output_lines、exit_code、stopping、crash_analysis_disabled、instance_id）传入，保持 launch → crash 单向依赖，`_RunningGame` 不外泄。
- `list_crash_candidates` 等 4 个 IPC 门面方法留在 launch.py（一行委托），本次不动 IPC 面。
- 行为不变约束：`test_game_service.py` 现有 4 个崩溃链路测试（含 `disable_crash_analysis` 跳过、干净退出不触发、事件 payload 断言）必须原样通过。
- `startup_complete_markers` 属启动监听职责，留在 launch.py。

## 9. 前端改动

| 位置 | 改动 |
| --- | --- |
| `src/i18n/locales/*.json`（6 语言） | 新增 10 个原因码（title + 2 suggestions），以 zh-CN 为源翻译 |
| `src/types/instances.ts` | `CrashReason` 增加可选 `mods?` / `packages?` |
| `src/components/modals/ErrorModal.vue` | 原因卡片在 `mods` 存在时渲染 Mod 徽标行（复用现有 evidence 展示样式） |

`CrashLogPickerModal`、版本详情分析入口、`errorPresentation.ts` 无需改动。

## 10. 测试计划

| 范围 | 用例 |
| --- | --- |
| `tests/test_crash_analysis.py`（保留改造） | 现有 7 用例全绿（门面导入路径迁移） |
| `tests/test_crash_rules.py`（新增） | 规则目录完整性：code 唯一、patterns 可编译、parameter_groups 引用的组名存在于 patterns；每个新码的代表性样例日志命中并产出预期 parameters；**一致性测试：新码在 6 语言 locale JSON 中均有对应键** |
| `tests/test_crash_stacks.py`（新增） | `Caused by` 分段、主嫌帧选择、ignored 前缀、lambda 归并 |
| `tests/test_crash_mod_index.py`（新增） | 三源各自解析（Forge 报告表 / Fabric MODS 段 / debug.log 发现行 fixture）、包名归并、指纹缓存命中与失效 |
| `tests/test_crash_capture.py`（新增） | 信号判定矩阵（exit_code / crash_log / startup_incomplete / disabled / stopping）、快照边界、close 语义 |
| `tests/test_plugin_crash_extensions.py` | 原样通过（富化仍为管道末端） |
| 平台 | 无新平台 API；ZIP/路径逻辑沿用既有跨平台测试模式，POSIX 分支在 CI（Ubuntu）覆盖 |

CI 最低标准：`ruff check ECL tests`、`ruff format --check ECL tests`、相关 pytest；前端 `cd frontend && pnpm check` + `pnpm build`。实机验证：`pnpm build` 后启动启动器，对手工样例崩溃日志执行"分析"验证弹窗、新码文案与 Mod 徽标；再以异常退出实例验证自动分析链路与事件。

## 11. 实施步骤（每步独立提交，测试通过后提交）

| 步骤 | 提交 | 内容 |
| --- | --- | --- |
| 1 | `refactor: 崩溃分析拆分为 crash 子包并保持行为不变` | crash/ 骨架 + 主体迁移 + 门面 + 站内导入迁移；现有测试全绿 |
| 2 | `feat: 崩溃规则目录支持命名捕获组并移植 HMCL 精选规则` | rules.py 升级 + 10 新码 + parameters 参数化 + 后端单测 |
| 3 | `feat: 崩溃分析新增三源 Mod 索引与堆栈分析器升级` | stacks.py / mod_index.py + 归因字段 + 聚合排序 + 单测 |
| 4 | `refactor: 启动崩溃检测与调度拆分为 crash capture 模块` | capture.py + launch.py 瘦身 + 崩溃链路回归 |
| 5 | `feat: 前端展示崩溃 Mod 归因并补全新原因码文案` | 6 语言 i18n + 类型 + ErrorModal + 规则文案一致性测试 + `pnpm check`/`pnpm build`；GUI 交互走查由用户抽查 |

## 12. 风险与前置条件

- **前置依赖**：启动高级选项任务（`docs/launch-advanced-options-plan.md`）已进入实施且 `launch.py` 有在制改动，本方案步骤 4 与其同文件——**须待该任务全部提交后再开工**，避免冲突。
- Java/Python 正则语义差异：逐条移植并配样例测试，禁止直接照抄未验证正则。
- Forge/Fabric 报告格式随版本差异大：Mod 列表解析采用宽松逐行策略 + 多版本 fixture，解析失败静默降级为仅 mods 目录来源。
- reasons 列表含伴随因素属行为扩展：确认插件仅追加/覆盖（现状如此），文档中标注发布说明。
- HMCL 移植代码保留 GPL-3.0 署名注释；PCL-CE 仅设计参考，不复制实现。
