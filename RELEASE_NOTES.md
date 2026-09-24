# v2.1.0

> 已按仓库 `DECKS555/campus-login` 填好。推 Tag `v2.1.0` 后再创建 Release。

## 标题（Release title）

```
校园网一键登录 v2.1.0 — Windows 锐捷 ePortal 自动认证
```

## 发布正文（复制下方内容到 Release 描述框）

```markdown
### 下载
- 直接运行版：`CampusLogin.exe`（无需安装、无需 Python，双击即用）
- 使用说明：`使用说明.md`

### 主要特性
- 一键认证锐捷 ePortal / CAS 校园网，掉线自动重连。
- 支持后台保活：关闭窗口后仍持续检测与自动登录，不占任务栏位置。
- 多设备互斥：只读查询自助服务在线设备 MAC，确认是其它设备在用网时主动礼让，避免电脑与手机互相踢下线。
- 换学校可用：认证网关可手动填写，也可点「自动探测网关」自动发现。
- 设置页「保存参数」按钮：改完参数立即落盘，保活运行中无需重启，约一个检测间隔后自动生效。
- 内置 GitHub Releases 版本检查（已指向本仓库）。
- 密码使用 Windows DPAPI 加密，日志脱敏。

### 本次修复与优化
- 修复设置页参数没有保存入口、改完不生效的问题。
- 修复 `customPageId` / `nasIp` 运行时以网关下发为准却不写回配置的显示不一致问题。
- 修复自动探测网关缺少 loading / 回执反馈、且用系统弹窗提示的问题。
- 修复多次点击登录后主按钮卡死在「正在认证…」的问题。
- 修复登录按钮点不动的问题（属性名笔误导致每次点击都抛异常）。
- `检查更新` 的 URL 现在会做 percent 编码，仓库名含中文等非 ASCII 字符也能正常请求。

### 首次运行提醒

`CampusLogin.exe` 未做代码签名，Windows SmartScreen 可能提示"Windows 已保护你的电脑"。这是正常现象：点击 **更多信息** → **仍要运行** 即可。

### 源码
本次 Release 同时提供完整源码。欢迎在 Issues 反馈各校适配问题或提交 PR。
```

## 需要上传的 Assets

1. `CampusLogin.exe`
2. `使用说明.md`
3. `Source code (zip)` / `Source code (tar.gz)` —— GitHub 自动生成

## 发布前检查清单

- [x] `UPDATE_REPO` 已填 `DECKS555/campus-login`（`src/app.py`）
- [x] README / Release 文案里的用户名与仓库名已替换为真实值
- [x] 仓库内没有 `config.json` / `login.log` / `app.pid` / `daemon_state.json`
- [x] `CampusLogin.exe` 是最新版，与 `src/` 源码一致
- [x] `--gui-smoke` 自检通过
- [x] Git Tag 为 `v2.1.0`（与 `src/app.py` 里的 `APP_VERSION = "v2.1.0"` 一致）
- [ ] 仓库设为 **Public**（否则用户点「检查更新」会 404）

## 推送命令

> 本地仓库已初始化、已提交并打好 `v2.1.0` tag，所以**只需执行下面两条 push**。

```powershell
cd D:\first-cc\campus-login

# 把 <TOKEN> 换成你生成的 GitHub Personal Access Token（生成步骤见 D:\first-cc\上传步骤.md）
git push https://DECKS555:<TOKEN>@github.com/DECKS555/campus-login.git main
git push https://DECKS555:<TOKEN>@github.com/DECKS555/campus-login.git v2.1.0
```

若上面的方式提示认证失败，也可以先不带 token 推送，等弹出登录框时：
用户名填 `DECKS555`，密码框粘贴 **Token**（不是你的 GitHub 登录密码）。
