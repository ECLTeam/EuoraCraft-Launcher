# 实例资料修改示例

先在启动器中选择游戏目录。查看游戏是本示例的必要权限，管理游戏为可选：仅查看时可以发现、扫描和读取资料；允许管理后才能修改资料，并覆盖所选目录现有及以后新增的实例。

显式调用discover/scan取得当前运行引用，用inspect取得profile_etag，再向update提供目标、确认的旧版本、保留的mutation_key及要修改的alias、description、favorite或tags_json。false和空标签数组有实际含义，未提供的字段保持原值。加载、卸载及停止不自动修改资料。

update仅返回任务接纳回执。用status查看state、effect和cleanup_pending；提交等待失败先用原键查询mutations.get，不换键重放，不自动读取新版本强行覆盖。只可取消尚未接受提交的自身任务。

宿主核对原资料版本后释放其独占句柄，再用原子替换发布新资料：要么旧资料完整保留，要么新资料完整可见，不会出现半写或名称缺失。替换成功后旧版本不可找回，宿主不保留备份、不登记恢复记录。原、新资料均至多64 KiB；系统与文件系统限制见[文件平台边界](../../../docs/protocols/eclplugin-package-format.md#文件平台边界)。


示例声明 `entry_point: main:Plugin`，不在加载阶段扫描或写入。使用主仓库现行打包接口生成归档，例如在仓库根目录执行：

```powershell
python -c "from pathlib import Path; from ECL.plugins.package_archive import build_plugin_package; build_plugin_package(Path('examples/plugins/worker_instance_profile'), Path('build/worker_instance_profile.eclplugin'))"
```

完整输入、错误与恢复契约见 [获准实例资料修改](../../../docs/protocols/eclplugin-package-format.md#获准实例资料修改)。
