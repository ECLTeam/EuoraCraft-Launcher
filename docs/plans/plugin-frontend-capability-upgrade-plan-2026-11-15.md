# 插件前端能力升级与边界开放实施方案

- 日期：2026-11-15。
- 状态：待用户确认，未实施。
- 驱动：平台能力整体升级；用户已明确决策——**移除限制插件能力的既有边界**（2026-11-15 00:41 确认四项）。
- 本方案取代同日早版草案的边界声明；[主窗口注入决策](../designs/plugin-main-window-injection-decision.md)第 4 节控制表与[界面不新增防护](plugin-ui-no-guardrails-2026-11-13.md)结论中被本方案移除的条目按此更新。

## 0. 用户已确认的四项边界决策

1. **UI 能力门禁全部移除**：`ui.inject` 权限审批、`plugins.allow_ui_injection` 用户总开关、`_trusted_plugin_event` 系统插件专属推送通道——三者删除。安装即获得全部 UI 能力，事件推送对全体插件一致。禁用/卸载/运行结束的插槽清理保留（这是正确性，不是限制）。
2. **数据开放到可见内容**：上下文与 SDK 状态按主窗口已渲染内容全量开放（含账户摘要、完整任务与实例数据、实例路径）；凭据令牌、明文密码等主窗口自身不显示的数据不暴露。
3. **后端业务能力授权（`plugin.base`/`instances.*`/`network.read` 等）本次不动**：那是数据权限体系，涉及全部业务方法，另行出方案。
4. **结构与 SDK 范围限制全部放开**：面板数量/深度/列数上限移除，节点白名单退为渲染分支表；SDK DOM 开放全窗口、路由前缀放开（含带参导航）；独立原生窗口维持技术不可用（pytaurix 缺隔离原语，非政策限制，原语就绪后按包格式契约重新开放）。

Worker 进程隔离是架构而非开关，不在讨论范围。

## 1. 现状（实测）

| 面      | 现状                                                                                                                                                                                  | 位置                                                                        |
| ------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------- |
| 面板节点   | 6 种：text/group/button/setting/list/task；上限 48 节点/深度 3/16 面板/group children ≤16/list ≤8 列/PanelData lists ≤8、tasks ≤16                                                               | 主仓库 `ECL/plugins/runtime/models.py` L198-350；前端 `PluginPanelNodeView.vue` |
| 面板数据   | 按请求拉取（打开/切换/按钮后），无推送无轮询                                                                                                                                                             | `PluginPanelModal.vue`、broker `_panel_data`                               |
| UI 门禁  | broker 按 `ui.inject` 授权校验（`worker_service.py` L866 起重载逻辑）；`utils/config.py` L98 `allow_ui_injection` 总开关；`bridge.py` L524 `_trusted_plugin_event` 只放行系统插件的可执行内容事件；第三方插槽走启动拉取 + 状态重拉 | 主仓库                                                                       |
| 插槽     | 30 个已知插槽位，HTML/Vue 双形态，仅任务队列条目插槽有 contextKey（DOM 属性传递）                                                                                                                              | frontend `slotRegistry.ts`、`PluginSlotHost.vue`                           |
| SDK 范围 | `dom.ts` DOM 操作限 `#app` 内（L14-25）；`router.ts` 导航限插件自身 `/plugin/{name}` 前缀；状态切片仅 theme/launcher/account/sidebar                                                                      | frontend `plugin-sdk/`                                                    |

## 2. 方向 A：声明式面板——节点扩充 + 上限移除

### A1 新节点（4 种，纯展示）

| 节点        | 字段                                                                                       | 渲染     |
| --------- | ---------------------------------------------------------------------------------------- | ------ |
| `heading` | `level: 1-3`、`text`（≤120）                                                                | 对应级标题  |
| `divider` | （无字段）                                                                                    | 分隔线    |
| `metric`  | `label`（≤60）、`value`（≤60）、`tone`（default/primary/warning/danger/success）、`hint`（≤120，可选） | 指标卡    |
| `badge`   | `text`（≤40）、`tone`（同上）                                                                   | 行内状态徽章 |

第一期全部为静态字段，动态值走既有 `list`/`task` 数据通道。

### A2 `list` 行内操作

`PanelListNode` 增加 `row_actions: list[PanelRowAction]`（0-3 项）：`{label, command, tone, confirm}`；命令调用自动附带 `row_key` 参数（取行数据 `key` 列值，无 key 列却声明 row_actions 则校验失败）。前端渲染行尾操作按钮（confirm 走既有 NPopconfirm 模式），执行后刷新。

### A3 上限移除（边界决策 4）

- 删除 `bounded_tree` 校验器（≤48/深度 ≤3）、group children ≤16、list columns ≤8、PanelData lists ≤8/tasks ≤16 的上限约束；保留字段类型/长度合法性校验与节点数下限（≥1）。
- 前端 `SUPPORTED_NODES` 退为渲染分支表：未知节点仍走 NAlert 兜底（后端 Pydantic 已先拒绝未知 type，前端兜底不可达但保留防御渲染）。
- `PanelData` 每列表行数与 lists/tasks 容量按实际负载放行，不做人为上限。

### A4 面板定时刷新

`PanelDefinition` 增加 `refresh_seconds: int = 0`（0 关闭；1-3600，**不设 2s 下限**——上限移除原则同样适用于此）。前端对 >0 的面板起定时重拉，插件禁用/面板关闭即清理。

### 不加的节点

- 输入类：`setting` 节点已覆盖，不造第二条写入通道。
- `image` / 图表：image 牵涉网络来源策略、图表需引入图表库，均另议（不属于门禁类限制）。

## 3. 方向 B：主窗口插槽——新插槽、结构化上下文、门禁移除

### B1 门禁移除（边界决策 1，主仓库）

| 移除项                            | 位置                                                                                                       | 处理                               |
| ------------------------------ | -------------------------------------------------------------------------------------------------------- | -------------------------------- |
| `ui.inject` 授权校验               | broker `_ui_service`、`worker_service.py` L866 `reload_ui_injection_plugins` 及调用点、`registry.py` L306 注释语义 | 删除校验分支与重载函数；`ui.inject` 从能力清单移除  |
| `allow_ui_injection` 总开关       | `utils/config.py` L98；前端设置页对应开关（grep `allow_ui_injection` 同步）                                            | 配置项删除；按 AGENTS 第 2 节，废弃入口不保留别名   |
| `_trusted_plugin_event` 系统插件门禁 | `bridge.py` L524-542                                                                                     | 删除 is_system 判断，六类可执行内容事件对全体插件放行 |
| 授权界面 `ui.inject` 文案            | 权限清单/审阅 UI（grep 同步）                                                                                      | 随能力删除                            |
| 清理链路                           | 禁用/卸载/运行结束撤销插槽                                                                                           | **保留不动**                         |

### B2 新插槽位（6 个，可增删）

`plugin-slot-plugin-page-top`（插件页顶部）、`plugin-slot-plugin-page-bottom`（插件页底部）、`plugin-slot-instance-detail-actions`（实例详情操作区旁）、`plugin-slot-instance-summary`（实例详情概要区）、`plugin-slot-game-status-bottom`（游戏状态条下方）、`plugin-slot-modal-body-extra`（通用弹窗体扩展）。登记方式与既有插槽一致。

### B3 结构化插槽上下文（边界决策 2）

- `PluginSlotHost` 新 prop `context-data`（结构化对象），序列化为 `data-slot-context-data`（JSON）；既有 `contextKey` 兼容。
- SDK `getSlotContext(element?)` 读取所在插槽结构化上下文（page、slot id、contextKey、context-data）。
- 首批上下文：任务队列条目（task id + TaskView 完整摘要）、实例详情（id、名称、版本、路径——主窗口详情页已展示）、当前页面/路由信息。
- SDK `onPageChanged(handler)`：宿主 router afterEach 转发的轻量只读通知。

## 4. 方向 C：前端 SDK——能力增强 + 范围放开

### C1 状态切片扩充（边界决策 2）

`state.ts` 新增只读切片：`tasks`（完整任务队列摘要）、`instances`（实例列表：id、名称、版本、路径——详情页可见口径）、`account` 现有摘要保持、`plugins`（name、version、enabled、has_ui）。凭据令牌、密码不在任何切片中。

### C2 导航放开（边界决策 4）

`router.ts`：移除 `/plugin/{name}` 前缀限制，提供 `openRoute(route)` 任意宿主路由跳转（含带参深链）；既有"读路由信息"不变。

### C3 DOM 范围放开（边界决策 4）

`dom.ts`：移除 `#app` 作用域限制，DOM 查询/操作开放全窗口文档；`data-plugin`/`data-plugin-dom` 归属标记与 cleanup 链保留（正确性）。

### C4 组件工厂补齐

`widgets.ts` 新增 `createBadge(tone, text)`、`createMetric(label, value, hint?)`、`createTable(columns, rows)`。

### C5 SDK 公开面契约快照

`frontend/src/plugin-sdk/sdk-surface.test.ts`：快照 SDK 模块名与方法名全集，扩面必须显式更新快照。

## 5. 实施批次

| 批       | 内容                                                                       | 仓库       |
| ------- | ------------------------------------------------------------------------ | -------- |
| B1-门禁移除 | broker/worker_service/bridge/config/权限 UI 清理 + manifest `ui.inject` 处理删除 | 主仓库      |
| A-后     | models.py 新节点 + row_actions + refresh_seconds + 上限校验删除 + 节点契约快照          | 主仓库      |
| A-前     | 渲染分支、行操作、定时刷新、i18n                                                       | frontend |
| B-前     | 6 新插槽 + context-data + onPageChanged + 设置页开关移除                           | frontend |
| C       | state 切片、openRoute、dom 放开、widgets ×3、SDK 面快照                             | frontend |

每批独立提交、独立回滚。前端验收 `pnpm check`；后端全量 pytest + ruff。

## 6. 测试计划

| 层      | 内容                                                                                                                        |
| ------ | ------------------------------------------------------------------------------------------------------------------------- |
| 门禁移除   | 删除既有断言 ui.inject 拒绝/总开关/系统插件门禁的用例（grep `ui.inject`、`allow_ui_injection`、`is_system` 于 tests/），新增"任意已启用插件注册插槽即成功且事件推送到达"用例 |
| 面板契约   | 新节点校验、row_actions/key 列联动、refresh_seconds 1-3600、上限移除后大面板合法（tests/test_plugin_panels.py 扩展）                               |
| 节点快照   | 面板节点类型集合快照（防无意识扩节点）                                                                                                       |
| 前端渲染   | 新节点渲染分支、行操作带 row_key、定时刷新起停清理                                                                                             |
| 插槽/SDK | 6 插槽挂载、context-data 序列化与读取、onPageChanged、state 切片口径（不含令牌）、openRoute 任意路由、dom 全窗口、SDK 面快照                                  |
| 回归     | 后端全量 pytest；frontend `pnpm check`；两官方插件与一个第三方测试插件装载验证                                                                     |

## 7. 风险与应对

| 风险                     | 应对                                                                                 |
| ---------------------- | ---------------------------------------------------------------------------------- |
| 上限移除后超大面板拖垮渲染          | 不设人为上限；渲染性能问题作为普通缺陷随真实负载修复                                                         |
| 事件推送放开后插件可实时改界面        | 接受的能力本体（用户决策）；清理链路保证禁用/卸载即失效                                                       |
| 数据开放口径误伤凭据             | 切片与上下文字段清单入契约测试，断言不含 token/password 字段                                             |
| `ui.inject` 删除导致清单校验残留 | 全局 grep `ui.inject` 清点：broker、worker_service、registry、manifest 模型、权限 UI、文档、测试，一处不漏 |
| 文档与实现漂移                | frontend.md、注入决策文档、包格式文档同步修订                                                       |

## 8. 明确不在本方案范围

- *后端业务能力授权体系（边界决策 3，另行方案）。*
- *独立原生窗口（技术不可用，非政策）。*
- `image`/图表节点、面板数据推送事件。
- Worker 进程隔离变更（架构）。

## 10. 实施与验证记录（2026-11-15 凌晨）

五批全部完成。主仓库提交：`e161436`（B1 门禁移除）、`821647b`（A-后 面板契约）；frontend 子仓库提交：`cef95ad`（B1 前端 + A-前 渲染）、`4527f39`（B-前 插槽与上下文 + C SDK）、`ca8eba8`（收尾）；主仓库 `4601918` 更新子模块指针与文档。

### 实施勘误（与方案条目对照）

| 方案假设 | 实测 | 处理 |
| --- | --- | --- |
| ui.inject 授权在 broker 校验 | 清单 permissions 早已被 WorkerManifest extra="ignore" 丢弃；唯一功能门禁是 `_trusted_plugin_event`（is_system）；`reload_ui_injection_plugins`/`clear_all_slots` 是无调用方死代码 | 删除范围收窄为：bridge 门禁、死代码、config 项、DevToolsPanel 开关、测试夹具中性化 |
| _panel_service 有 16 面板上限 | broker.py L1004 确认存在 | 一并删除 |
| widgets 需新增 createBadge | `badge` 工厂已存在 | 只新增 metric/table |

### 验证

- 后端全量 pytest：2745 passed / 42 skipped（唯一 1 例失败来自用户工作区中未提交的 `image_url_resolve` 编辑中状态，复跑通过，与本方案无关，未纳入提交）。
- 前端 `pnpm check`（format/lint/design/page-root/typecheck/test）全绿：213 文件 1310 项通过。
- 面板契约测试 10 项、渲染测试 10 项（新增 6）、SDK 快照 4 项。
- SDK 切片数据口径：tasks/instances/plugins 均为管理页可见字段，无令牌/密码字段。
