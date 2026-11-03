# LittleSkin 登录接入

启动器支持 LittleSkin 授权码登录（OAuth 设备代码流）和浏览器登录（OAuth 授权代码流）。
在简体中文界面的“添加账户”中选择 LittleSkin，再选择登录方式。授权完成后，启动器会
列出允许使用的角色；只有一个角色时自动使用该角色。角色账户可以在其他界面语言下继续使用。

## 应用配置

应用维护者需要创建自己的 LittleSkin OAuth 应用。开发时在启动器根目录 `.env` 中配置：

```dotenv
LITTLESKIN_CLIENT_ID=你的应用ID
LITTLESKIN_CLIENT_SECRET=你的客户端密钥
LITTLESKIN_REDIRECT_URI=http://127.0.0.1:34127/oauth/littleskin/callback
```

`LITTLESKIN_CLIENT_ID` 用于两种方式。授权代码流另需 `LITTLESKIN_CLIENT_SECRET`；
只配置应用 ID 时，设备代码流仍可使用。设备代码流的请求和刷新不会发送客户端密钥。

本机回调地址有上述默认值，可用 `LITTLESKIN_REDIRECT_URI` 修改端口和路径。
地址必须使用包含端口的 `http://127.0.0.1` 回环地址，并与应用后台登记的 URL 完全一致。
监听器仅在授权代码登录期间运行，端口占用会明确失败，不自动更换端口。远程地址、
含凭据、查询参数或片段的地址均不可用。

同名非空系统环境变量优先于 `.env`，`.env` 优先于非空构建配置。全部未配置回调地址时，
使用默认值。`.env` 不提交到 Git，也无需向普通用户分发。

发行构建从 GitHub Secrets 的 `LITTLESKIN_CLIENT_ID`、`LITTLESKIN_CLIENT_SECRET` 和
可选的 `LITTLESKIN_REDIRECT_URI` 注入。普通用户无需自行填写这些配置。
授权代码流密钥会随发行包提供给桌面程序，可以被提取；GitHub Secrets 和编译不能保证
发行包内的密钥保密。应用密钥不会写入各账户凭据文件或交给前端。

## LittleSkin 应用准入

1. 在 LittleSkin 应用管理页面创建应用。
2. 按设备代码流要求登记官方回调 `https://open.littleskin.cn/oauth/callback`；需要授权代码流时，
   另登记实际使用的本机回调。多条回调的登记方式及审核以 LittleSkin 当前要求为准。
3. 申请设备代码流白名单，以及 `Yggdrasil.PlayerProfiles.Read`、
   `Yggdrasil.MinecraftToken.Create` 所需权限。设备流程请求还包含 `offline_access`，用于保持授权。
4. 本实现由启动器选择角色，不申请与角色读取互斥的 `Yggdrasil.PlayerProfiles.Select`。
5. 完成测试模式验收后，向 LittleSkin 维护者申请解除测试模式，供其他用户授权。

授权代码流要求服务端原样返回请求中的 `state`。启动器始终校验该值，丢失或不匹配的回调
不会完成授权；真实应用联调时必须核实。不要为了使登录继续而移除校验。

## 账户与故障处理

### 管理皮肤与披风

在账户列表中点击 LittleSkin 账户的材质管理按钮，或打开衣柜并选择该账户，
进入“LittleSkin 云端”。管理入口不受界面语言限制。

- 分别浏览云端皮肤、披风；点击材质名称预览，点击“穿戴”才修改角色。
- “重置皮肤”和“移除披风”只影响所选的一项，不覆盖另一项。
- “下载到本地衣柜”保存云端原始 PNG；“下载当前材质”保存当前角色穿戴。
  本地皮肤、披风条目均可另存 PNG。相同内容会沿用现有本地条目。
- 云端衣柜分页并可手动刷新；在网站更新收藏后，回到启动器刷新列表。
- 本阶段上传保留为 TODO，不提供启动器内上传或网站上传跳转入口。
  本地导入只保存到本地，不能直接为 LittleSkin 角色穿戴。

首次管理可能需要补充授权，新增请求 `Player.ReadWrite` 和 `Closet.ReadWrite`。
后者沿用当前已获批的应用权限，第一阶段仅用于读取云端衣柜，不提供云端收藏写操作。
应用获批不代表旧令牌已获用户授权；点击“授权账户管理”后，可以继续使用两种登录方式。
请授权拥有当前角色的账户；授权完成后账户 ID、收藏、置顶及当前选择保持不变。
拒绝、取消或授权其他账户不会替换原凭据。

若提示“修改结果尚未确认”，先刷新核对当前穿戴，不要连续点击穿戴重放请求。
若已修改成功但预览刷新失败，刷新预览即可。云端材质核对最多读取 20 页，每页 100 项，
并有 20 秒核对预算；单次网络请求仍受客户端超时约束，超过范围会明确失败。

### 登录与账户故障

- 授权令牌与游戏令牌分别保存和使用，启动游戏时使用绑定所选角色的游戏令牌。
- OAuth 令牌视为不透明字符串，不通过解码推断角色。刷新后立即保存新凭据，再兑换游戏令牌。
- 重复登录同一认证服务的同一角色沿用原账户 ID；不同角色可以分别添加。
- 拒绝、超时或取消不会保存待添加账户。保存失败可在同一次授权内重试。
- “应用尚未准入”时检查应用 ID、白名单、测试模式和权限；无角色时先在 LittleSkin 创建角色。
- `LITTLESKIN_INVALID_SCOPE` 表示服务端拒绝请求的权限。设备代码流申请
  `Yggdrasil.PlayerProfiles.Read Yggdrasil.MinecraftToken.Create offline_access`，其中游戏令牌创建权限
  需要应用获准使用。只有基础权限或角色读取可用，不代表该应用已具备游戏登录权限；
  请核对 LittleSkin 白名单审核邮件中是否包含 `Yggdrasil.MinecraftToken.Create`。
  授权代码流只申请前两个游戏权限，`offline_access` 只用于设备代码流。
- 浏览器登录回调失败时检查登记地址、本机配置、端口占用和 `state` 往返行为。
- 仅删除启动器中的账户会清除其本地凭据，不替代在 LittleSkin 网站撤销应用授权。

官方参考：[设备代码流](https://manual.littlesk.in/advanced/oauth2/device-authorization-grant)、
[授权代码流](https://manual.littlesk.in/advanced/oauth2/authorization-code-grant)、
[LittleSkin API](https://manual.littlesk.in/advanced/api)。
