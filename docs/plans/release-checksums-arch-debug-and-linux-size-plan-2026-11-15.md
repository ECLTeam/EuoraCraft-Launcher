# 发布产物校验文件、禁用 Arch debug 包与 Linux 体积裁剪方案（2026-11-15）

## 背景与目标

- 构建器评估结论：暂不迁移 Nuitka。启动器瓶颈在 WebView/前端加载，Nuitka 的编译收益小；
  而 `pytaurix`（本地构建原生 wheel）依赖 `collect_all` 收集 data/binaries/hiddenimports，
  五个构建环境需重新配置 C 工具链并全量回归，成本远超收益。
- 目标 1：Arch 侧从源头不再产出 debug 符号包（此前仅靠 workflow 挑选主包时排除）。
- 目标 2：每个发布产物旁生成一个 sha256 校验文件，随产物上传 artifact 与 GitHub Release。
- 目标 3：裁剪 Linux（及全平台通用的）冻结产物中运行时不会加载的内容，减小体积。

## 改动点

### 1. packaging/arch/PKGBUILD

- 新增 `options=('!debug')`：产物是上游 Release 的预编译二进制，makepkg 不再产出
  `euoracraft-launcher-debug-*.pkg.tar.zst`。
- `build.yml` 中挑选主包时对 debug 包名的排除保留，作为防线。

### 2. EuoraCraft-Launcher.spec

- 新增 `_drop_unused_packaging_files`，对 `a.datas` 与 `a.binaries` 过滤：
  - 数据前缀 `share/locale/`、`share/doc/`、`share/man/`、`share/gtk-doc/`：
    GTK/WebKit 按依赖分析被整体收集，翻译与文档缺失时回退英文，不影响功能。
  - 后缀 `.a`、`.la`（静态库）与 `.debug`（分离调试符号）：均不参与动态加载。
- 对没有这些条目的平台（如 Windows）是安全无操作。

### 3. .github/workflows/build.yml

- `build-backend`：新增 "Generate artifact checksum" 步骤，产物旁生成
  `<产物名>.sha256`（`sha256sum` 兼容格式，macOS 用 `shasum -a 256` 同格式输出）；
  校验文件随产物上传 artifact，并加入 Release 上传清单。覆盖四个平台产物。
- `build-arch-pkg`：新增 "Generate Arch artifact checksums"，为 Arch 主包与裸二进制
  各生成校验文件（内容使用裸文件名，下载后可在任意目录 `sha256sum -c`）；校验文件
  单独上传 artifact。
- `publish-arch`：下载两个校验文件，四件套（包、包校验、二进制、二进制校验）逐一
  存在性校验后上传 Release。

## 测试计划与结果

- `tests/test_ci_workflows.py` 新增
  `test_arch_package_disables_debug_and_every_artifact_has_a_checksum`：
  PKGBUILD 含 `options=('!debug')`；两类校验生成步骤各出现一次；校验文件随产物
  上传 artifact 并进入 Release；publish-arch 下载并上传校验文件。
- 结果：`pytest tests/test_ci_workflows.py` 60 通过；`build.yml` YAML 解析通过；
  spec 语法编译通过；`ruff check tests/test_ci_workflows.py` 通过。

## 影响范围与后续注意

- Release 每个产物多一个同名 `.sha256` 文件；用户校验方式：
  `sha256sum -c EuoraCraft-Launcher-<平台>-<架构>-<版本>.sha256`。
- Linux onefile 体积大头（WebKitGTK/JSC 系统库）为运行必需，不可裁剪；本次裁剪
  仅移除 locale/文档/静态库/调试符号，预计节省量取决于 CI 环境实际收集情况，
  首次发布后可对比产物体积确认。
- 不涉及 frontend 子模块与运行时代码行为。
