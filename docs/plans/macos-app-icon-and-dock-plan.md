# macOS 应用图标与 Dock 显示实施方案（已实施）

## 目标与验收

- macOS 的 `EuoraCraft Launcher.app` 在 Finder、Dock、DMG 中一律显示应用图标，不再显示 PyInstaller/Python 图标。
- 换标流程可复现：改图标源文件后运行一条命令即可重新生成 macOS 图标，测试会拦住忘记重新生成的情况。
- 打包配置在缺少 macOS 图标时立即失败并给出修复命令，不再静默回退到 PyInstaller 默认图标。
- alpha/dev 构建（`console=True`）的 `.app` 同样进入 Dock 并带菜单栏，与 beta/release 行为一致。

## 当前项目情况

- macOS 产物由 PyInstaller 出 `.app`（`EuoraCraft-Launcher.spec`），Windows/Linux 由 Nuitka 出单文件；`Tauri.toml` 的 `bundle.icon` 不参与本次打包，运行时配置由 `ECL/adapters/tauri.py` 的 `_build_config()` 注入。
- 图标根因：`EuoraCraft-Launcher.spec` 的 `_resolve_icon()` 在 macOS 下查找 `resources/img/logo.icns`，但仓库只提供 `resources/img/logo.ico`，缺失时静默返回 `None`。PyInstaller 的 `BUNDLE.__init__` 收到空图标时回退到自带的 `bootloader/images/icon-windowed.icns`（PyInstaller/Python 图标），`assemble()` 再把它复制进 `Contents/Resources/` 并写入 `CFBundleIconFile`，因此 Finder、Dock、DMG 全部显示 Python 图标。
- Dock 相邻缺陷：PyInstaller 在 `console=True` 时自动向 `Info.plist` 写入 `LSBackgroundOnly=True`，系统会把应用当作后台应用（不进 Dock、无菜单栏）。`_resolve_console_mode()` 仅在 beta/rc/release 返回 `False`，因此 alpha/dev 构建命中该分支。
- 素材现状：`resources/img/logo.ico` 内含单个 256×256 PNG，与 `packaging/arch/euoracraft-launcher.png` 逐像素一致，仓库无更高分辨率图标源。

## 方案比较与选择

图标来源：

| 方案 | 改动 | 取舍 |
| --- | --- | --- |
| A. 提交 `resources/img/logo.icns` + 生成脚本 + 校验测试（已选择） | 新增约 221KB 二进制图标与 `packaging/build-macos-icon.py`，spec 缺文件即失败。 | 产物确定、可 review、可直接复用于 DMG 卷图标；仓库多一个二进制文件，换标需重跑生成命令（由测试兜底）。 |
| B. 构建期从 `logo.ico` 自动生成 | 在 spec 内用 Pillow 生成 `.icns` 写入 workpath。 | 仓库无二进制；但打包路径多一段二进制格式代码，图标无法在 git 中 review，Pillow 自带 ICNS 写出器不含 16/32 小尺寸。 |
| C. 只补一个 `.icns` | 仅新增文件、修正 `_resolve_icon()`。 | 改动最小，但换标无流程、无回归保障。 |

Dock 显示：

| 方案 | 改动 | 取舍 |
| --- | --- | --- |
| A. `info_plist` 显式 `LSBackgroundOnly=False`（已选择） | 覆盖 PyInstaller 的默认值，console 模式保持不变。 | alpha/dev 从终端启动仍可拿到标准输出，排障能力不变；两种构建的 Dock 行为统一。 |
| B. macOS 强制 `console=False` | 让 PyInstaller 走窗口化分支。 | alpha/dev 的标准输出会被吞掉，启动问题更难排查。 |

## 实施步骤

1. 新增 `packaging/build-macos-icon.py`：用 Pillow 读取 `resources/img/logo.ico` 中最大的图层作为母版，按 `icp4`=16、`icp5`=32、`ic11`=32、`ic12`=64、`ic07`=128、`ic13`=256、`ic08`=256 生成 PNG 条目，自写 `icns` 容器（`icns` 魔数 + 总长度 + TOC + 条目负载），原子写入 `resources/img/logo.icns`；母版尺寸对应的条目为无损拷贝，其余用 LANCZOS 缩小。脚本可重复执行，输出中文摘要。
2. 运行生成脚本，提交 `resources/img/logo.icns`。
3. 修改 `EuoraCraft-Launcher.spec`：`_resolve_icon()` 在 macOS 缺少 `logo.icns` 时抛出 `SystemExit` 并提示生成命令；`BUNDLE` 的 `info_plist` 显式写入 `"LSBackgroundOnly": False`，附中文注释说明覆盖原因。
4. 新增 `tests/test_macos_bundle_icon.py`：
   - 用桩 globals（`SPECPATH`/`DISTPATH`/`workpath`/`Analysis`/`PYZ`/`EXE`/`COLLECT`/`BUNDLE`）与桩 `PyInstaller.utils.hooks`，在临时目录内 `exec` 真实 spec（monkeypatch `sys.platform` 为 `darwin`），断言 `BUNDLE`/`EXE` 收到 `logo.icns`、`info_plist["LSBackgroundOnly"] is False`、缺图标时抛 `SystemExit` 且消息包含生成命令；同一套桩在 `win32` 下断言仍使用 `logo.ico`。
   - icns 语义校验：条目类型与像素尺寸映射齐全；`ic08`（256）与 ico 母版逐像素相等；其余条目与母版缩小结果在容差内一致，防止换标后遗漏重新生成。
5. 本文补充实施与验证记录，并按仓库规范提交一次 commit。

## 测试计划

- 后端门禁：`ruff check ECL tests`、`ruff format --check ECL tests`、`python -m pytest -q`（新增用例在修复前失败、修复后通过）。
- 本机（macOS）额外验证：
  1. 生成器连续执行两次产物字节一致，确认幂等。
  2. `sips -g all`、`iconutil -c iconset` 与 Pillow 三方解析生成的 `.icns`，确认条目类型与尺寸。
  3. 最小 PyInstaller 冒烟：用生成的 `logo.icns` 与 `LSBackgroundOnly=False` 打一个 hello-world `.app`，检查 `Contents/Info.plist` 的 `CFBundleIconFile`、`LSBackgroundOnly`，并用 `codesign --verify` 确认签名未被破坏。
  4. 打开该 `.app` 后用 `osascript` 读取 System Events 的后台进程列表，确认应用为前台应用（Dock 可见）。
- 不在本次自动执行完整启动器 macOS 打包：需要 `pytauri-wheel` 与 `frontend/dist` 产物，耗时且依赖网络，最终 `.app` 由本地实机或 CI macOS 产物确认。
- 前端无改动，不触发 `pnpm check`。

## 边界与风险

- 图标母版只有 256×256，超过 256 的条目（512/1024）放大后没有额外细节，故不生成，由系统缩放；Dock 与菜单栏使用的 32/64/128 条目为原生尺寸。
- `.icns` 容器由本仓库自行写出，格式为 Apple 公开结构；已用 `iconutil`、`sips`、Pillow 三方解析交叉验证，避免只依赖单一实现。
- `resources/` 会被 Nuitka 与 PyInstaller 整目录打包，新增图标使各平台载荷增加约 221KB，相对整体体积可忽略。
- 换标流程固定为：替换 `resources/img/logo.ico` → 运行 `python packaging/build-macos-icon.py` → 执行测试；README 仍引用 `logo.ico`，无需改动。
- `LSBackgroundOnly=False` 只影响 macOS 的 `Info.plist`，不改变 Windows/Linux 产物，也不改变 macOS 的 console 模式语义。
- macOS 会缓存应用图标，本地验证旧包时可能需要 `touch` `.app` 或重启 Dock 才能看到新图标，属系统行为，不纳入自动化。

## 参考

- [PyInstaller macOS BUNDLE 图标回退与 Info.plist 生成](https://github.com/pyinstaller/pyinstaller/blob/develop/PyInstaller/building/osx.py)
- [Apple LSBackgroundOnly 键说明](https://developer.apple.com/library/archive/documentation/General/Reference/InfoPlistKeyReference/Articles/LaunchServicesKeys.html)
- [Apple Icon Design 指南](https://developer.apple.com/design/human-interface-guidelines/resources)

## 实施与验证记录

- 改动落地：`packaging/build-macos-icon.py`（生成器）、`resources/img/logo.icns`（221054 字节，7 个条目）、`EuoraCraft-Launcher.spec`（缺图标快速失败、`LSBackgroundOnly=False`）、`tests/test_macos_bundle_icon.py`（4 个用例）。未改动 `Tauri.toml`、`ECL/` 与前端。
- 回归测试：新增用例在改 spec 前 2 项失败（图标为 `None`、缺图标不报错），改 spec 后 4 项全部通过。`ruff check ECL tests`、`ruff format --check ECL tests` 通过；完整后端测试 1145 项通过、4 项跳过（本地需 `git submodule update --init --recursive` 检出 `ECL/game`、`ECL/services/florolding`、`frontend`；本机 shell 预设的 `CURSEFORGE_API_KEY` 会影响 `tests/test_mcmod.py` 的一项断言，属环境变量而非代码问题）。
- 图标正确性：生成器在 Python 3.12 与 3.14 下重复执行产物字节一致（SHA256 `b458060c…`）。`sips -g all` 读出 256×256、`hasAlpha: yes`；`iconutil -c iconset` 成功还原 16/32/64/128/256 全部尺寸；Pillow 读出同一组尺寸；`ic08`（256）条目与 `logo.ico` 母版逐像素相同。
- PyInstaller 交互验证：用同一图标与 info_plist 打包 hello-world `.app` 做 A/B 对照，两个产物 `CFBundleIconFile` 均为 `logo.icns`、`Contents/Resources/logo.icns` 就位、`codesign --verify --deep --strict` 通过。带 `LSBackgroundOnly=False` 的产物 `lsappinfo` 报告 `type="Foreground"`（进 Dock、有菜单栏），不带的产物为 `type="BackgroundOnly"`（无 Dock 图标），确认覆盖项正是差异来源。
- 换标流程：替换 `resources/img/logo.ico` → `python packaging/build-macos-icon.py` → `python -m pytest tests/test_macos_bundle_icon.py`。本机验证旧包时需 `touch` `.app` 或重启 Dock 才能刷新图标缓存。