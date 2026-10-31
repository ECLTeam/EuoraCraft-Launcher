# 取消启动立即退出方案

日期：2026-11-01
范围：

- 前端 `frontend/src/composables/useInstanceManager.ts`、`frontend/src/views/Game.vue`、`frontend/src/config/game.ts`、六份语言文件
- 后端 `ECL/services/game/launch.py`

## 1. 现象

点击「启动游戏」后立刻点取消：进度卡消失了，但启动按钮仍显示「启动中」并保持禁用，要等后端把当前阶段跑完才恢复。

## 2. 根因

- 前端 `launchGame()` 只有在 `game_launch` IPC 返回后才在 `finally` 里复位 `launching`；取消路径原先靠一个 5 秒兜底定时器才解锁按钮。
- 后端取消信号只在 `cancel_event.is_set()` 检查点生效。启动流程中存在若干无法中断的阻塞步骤：正版令牌刷新（网络）、authlib-injector 组件下载（同步 HTTP，最长 15 秒超时）、`check_files` 文件校验（大整合包数秒）、natives 解压与启动前命令。检查点不够密时，取消要等当前步骤跑完才生效。
- 后端在启动期间持有 `_launch_cancel_event` 预约，清理未结束时再次启动会返回 `LAUNCH_ALREADY_RUNNING`（「已有游戏启动任务正在运行」）。单纯前端立即解锁会让用户马上再点时撞上这个错误。

## 3. 方案（用户选定：立即解锁 + 后端补检查点）

### 3.1 前端立即解锁

- `useInstanceManager` 新增 `cancelLaunch()`：立刻作废当前启动会话、把 `launching` 置为 `false`、隐藏进度卡，取消信号改为后台发送（失败只记日志）。`Game.vue` 的取消回调与 5 秒兜底定时器一并删除。
- 新增启动会话号 `launchSession`：每次启动分配一个，取消时自增使其失效。进度事件、IPC 结果、`finally` 的解锁都按会话号判断，避免迟到的结果覆盖用户取消后重新发起的新启动。
- `game_launch` 返回 `LAUNCH_ALREADY_RUNNING` 时自动重试（700ms 间隔，最多 3 次），仍失败则用普通提示「上一次启动正在退出，请稍候再试」，不再弹红色启动失败弹窗。

### 3.2 后端补检查点

- 新增 `LaunchCoordinator._ensure_launch_not_cancelled(cancel_event)`，统一替换原有 6 处重复的 `if cancel_event.is_set(): raise ...` 写法。
- 新增检查点：authlib-injector 组件下载前（避免取消后仍发起最长 15 秒的同步请求）、Java 环境校验后、`check_files` 文件校验前（取消后不再启动耗时扫描）。

## 3.3 连点导致误启动（用户复测反馈）

首版上线后用户反馈「多点几下启动/关闭，有概率真的启动游戏」。两处原因叠加：

1. 取消只在检查点生效。取消信号到达时若已经进入创建进程阶段，后端仍会创建进程并返回成功，而前端已经把界面当作「已取消」，于是游戏在后台启动。
2. 进度卡消失后，同一屏幕位置正好是启动按钮；连点「关闭」的第二次点击会落到启动按钮上。上一版给 `LAUNCH_ALREADY_RUNNING` 加了自动重试，正好把这次误触变成一次成功启动。

修复：

- 后端在 `create_process()` 之后、`post_launch` 钩子之前再确认一次取消：命中则把运行记录标记为非 pending、`stopping`，调用 `_stop_launched_process()` 结束刚创建的游戏进程并抛 `LAUNCH_CANCELLED`。取消从此在进程创建阶段仍然赢下竞争。插件启动有自己的取消契约与进程归属，这条回收只作用于普通启动。
- 前端取消后 600ms 内忽略启动点击（`LAUNCH_CANCEL_GUARD_DELAY`），吞掉连点误触。
- `gameHomeApi.cancelLaunch()` 改为返回后端是否接受取消；为 `false` 表示启动已经完成，此时提示「游戏已经启动，取消未生效」（`game.status.launchCancelMissed`），不再假装取消成功。

## 4. 改动点

| 文件 | 改动 |
| --- | --- |
| `frontend/src/composables/useInstanceManager.ts` | 新增 `cancelLaunch()` 与会话号；`requestLaunch()` 负责 `LAUNCH_ALREADY_RUNNING` 重试；进度与结果按会话号过滤；取消后 600ms 忽略启动点击；取消未生效时提示 |
| `frontend/src/views/Game.vue` | 取消回调改为直接调用 `version.cancelLaunch()`，删除 5 秒兜底定时器及其清理 |
| `frontend/src/config/game.ts` | 新增 `LAUNCH_BUSY_RETRY_DELAY` / `LAUNCH_BUSY_RETRY_LIMIT` / `LAUNCH_CANCEL_GUARD_DELAY` |
| `frontend/src/i18n/locales/*.json` | 新增 `game.status.launchBusy`、`game.status.launchCancelMissed`（六种语言） |
| `frontend/src/features/game-home/api/gameHomeApi.ts`、`stores/gameHomeStore.ts` | `cancelLaunch()` 返回后端是否接受取消，`NO_ACTIVE_LAUNCH` 不再当异常 |
| `frontend/src/composables/useInstanceManager.test.ts` | 5 条用例：取消立即解锁并丢弃迟到结果、取消后重启不被旧会话覆盖、忙重试提示、连点保护、取消未生效提示 |
| `ECL/services/game/launch.py` | 新增 `_ensure_launch_not_cancelled()` 与 `_stop_launched_process()`；补齐 4 处检查点，含进程创建后的取消回收 |
| `tests/test_game_service.py` | 新增 `test_cancel_launch_stops_before_file_check`、`test_cancel_launch_during_process_creation_stops_the_game` |

## 5. 验证

- 前端：`eslint`、`prettier --check`、`vue-tsc` 通过；`vitest run` 全量 190 文件 / 1160 用例通过。
- 后端：`ruff check`、`ruff format --check` 通过；`pytest tests/test_game_service.py tests/test_instance_profiles.py tests/test_plugin_game_launch.py tests/test_architecture.py` 186 通过 / 2 跳过。
- 未做真实启动器联调：本机没有可交互的运行态，取消时序以单元测试覆盖（含模拟取消后迟到的 `game_launch` 结果、模拟进程创建期间取消）。

## 6. 仍存在的边界

- 已开始的同步网络请求（正版令牌刷新、authlib 组件下载）无法被取消信号打断，仍要等该请求返回；检查点保证其后不再继续下一步。
- 后端清理窗口内再次启动会重试 3 次（约 2.1 秒）；若清理更久，用户会看到「上一次启动正在退出」的普通提示。
- 取消与启动请求在同一通道上并发时，仍可能因到达顺序让取消落到新一次启动上（表现为「点了启动却被取消」），不会造成误启动。
