# 文档索引

本目录保存启动器的长期文档：使用指南、协议规范、架构设计与决策记录。

## 目录结构

| 目录 | 内容 | 维护方式 |
| --- | --- | --- |
| `guides/` | 面向使用者的操作指南 | 随功能演进持续更新 |
| `protocols/` | 插件、主题等对外协议与格式规范 | 协议变更时同步更新 |
| `designs/` | 架构设计与实现设计 | 设计定稿后基本冻结 |
| `adr/` | 架构决策记录 | 只追加，不修改历史结论 |

`plans/` 存放单次功能变更的过程记录（需求、方案取舍、验收结果），属于阶段性材料，不纳入本索引；需要时直接查看该目录，已完成的记录通过 `git log --diff-filter=D -- docs/plans/` 追溯。

需要确认现行行为时以代码和 `AGENTS.md` 为准；追溯历史决策时看 `adr/`、`designs/` 与 Git 历史。

## 使用指南

| 文档 | 说明 |
| --- | --- |
| [发布流程](guides/release-process.md) | 四种发布类型、日常检查、构建触发与产物验收；`AGENTS.md` 第 9 节的完整版本 |
| [无头运行说明](guides/headless-runtime.md) | `--headless` 任务协议、输出格式与插件子进程边界 |
| [启动器命令行](guides/launcher-cli.md) | 启动参数、实例快捷启动与设置生效顺序 |
| [整合包安装与可选文件](guides/modpack-installation.md) | 安装预览、文件选择、后台任务和准备接口 |
| [可视化主题设计指南](guides/theme-designer-guide.zh-CN.md) | 主题设计器、预设库与 `.ecltheme` 文件 |

## 协议与格式

| 文档 | 说明 |
| --- | --- |
| [插件包格式](protocols/eclplugin-package-format.md) | `.eclplugin` 归档结构、权限声明与 Worker API v2 |
| [主题与插件外观协议](protocols/theme-plugin-protocol.md) | 主题数据格式、插件插槽与外观扩展点 |
| [联机扩展协议插件开发](protocols/plugin-connector-extensions.md) | 第三方联机协议如何接入启动器 |
| [插件实例兼容扩展点](protocols/plugin-instance-compatibility.md) | 只读实例元数据来源的注册方式 |

## 架构设计

| 文档 | 说明 |
| --- | --- |
| [Worker API v2 设计](designs/plugin-worker-api-v2-implementation-design.md) | 插件 Worker 进程的接口、权限与生命周期设计 |
| [无头进程集成设计](designs/plugin-headless-process-integration-design.md) | 插件子进程与启动器运行时的集成边界 |
| [原生 WebView 隔离设计](designs/plugin-native-webview-isolation-design.md) | 插件原生窗口的隔离与安全边界 |

## 决策记录

| 文档 | 说明 |
| --- | --- |
| [ADR-051 前端代码逻辑与结构优化](adr/ADR-051-前端代码逻辑与结构优化计划.md) | 前端分层与职责边界的决策依据 |

## 维护约定

- 新增功能方案放入 `plans/`，文件名沿用现有 `主题-plan.md` 风格；带日期的方案保留日期后缀。
- 方案完成后在文首状态行记录结论，随后从 `plans/` 移除，不做长期归档。
- 需要长期保留的结论写进 `designs/`、`protocols/` 或 `adr/`，不要只留在方案里。
- 协议、格式或对外行为变更时，同步更新 `protocols/` 与 `guides/` 中的对应文档，而不是只写在方案里。
- 移动或重命名文档时，必须同步修正引用方链接；`AGENTS.md` 与根目录 `README.md` 的引用同样适用。
