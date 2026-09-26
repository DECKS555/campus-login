# v2.1.1

> 仓库 `DECKS555/campus-login` 已填好。推 Tag `v2.1.1` 后再创建 Release。

## 标题（Release title）

```
校园网一键登录 v2.1.1 — 校园网自动认证 + 后台保活（Windows）
```

## 发布正文（复制下方 ```markdown 块里的内容到 Release 描述框）

```markdown
填一次校园网账号和密码，之后插上网线 / 连上校园 Wi-Fi 就能自动认证上网；掉线自动重连，关掉窗口也在后台继续守着，而且**不会在任务栏右下角留下任何图标**。

---

## 核心功能

**① 一键认证**
自动识别当前网络状态，支持锐捷 ePortal / CAS 统一认证。首次填好账号密码点「保存并立即登录」，之后的事情都由它自己完成。

**② 后台保活 —— 真正的"设完就忘"**
关掉窗口后保活进程继续运行：按 30 秒一轮探测网络，掉线立即重新认证；在线稳定后自动放宽到 60 秒一轮。
- 进程优先级自动降到「低于正常」并开启系统节流（EcoQoS），**实测常驻约 3 MB 内存、CPU 长期 0%**。
- 后台**完全不打扰**：任务栏右下角不会多出图标，也不会弹窗口。只有在真需要告诉你的时候（比如账号被手机顶掉、网络已恢复）才短暂弹一条系统通知，读完自动消失。

**③ 多设备互斥与自动让位（本项目的特色功能）**
校园网账号通常只允许一台设备在线。程序会**只读地**查询自助服务的在线设备 MAC：确认是手机等其它设备在用网，就主动进入礼让期（首次 15 分钟，反复被顶则翻倍，上限 120 分钟），避免电脑和手机互相把对方踢下线；对方一断线，电脑立刻恢复认证。整个过程不会去踢别人的设备。

**④ 连续失败自动退避**
登录连续失败时，重试间隔逐级拉长、上限 300 秒（可调）。校园网维护或断网期间不会疯狂刷请求。

**⑤ 换学校也能用**
认证网关既可以手动填写，也可以点「自动探测网关」自动发现——探测中按钮会转圈，完成后在页面内直接给出结果回执，不弹系统弹窗。换学校的同学不用改任何代码。

**⑥ 状态看得见**
界面实时显示连接状态、运营商、出口 IP；「在线设备」查询能列出当前谁在用这个账号（MAC / IP / 设备类型 / 是否本机），并给出判定；运行日志在首页一屏可见，也能翻页看完整记录。

**⑦ 参数可调，改完即生效**
检测间隔、在线间隔、内容校验间隔、心跳日志间隔、退避上限都可在设置页调整；改完点「保存参数」立即落盘——**后台保活运行中无需重启**，约一个检测间隔内自动生效。

**⑧ 开机自启**
可一键开启开机自动拉起后台保活（走 Windows 计划任务，开机时不弹窗口、不弹 UAC）。

**⑨ 内置更新检查**
直接对接本仓库的 GitHub Releases，在「设置 → 检查更新」即可查看新版本。也支持不改代码换成你自己的仓库：在 `config.json` 里加 `"update_repo": "你的用户名/你的仓库名"` 即可（优先级高于内置值）。

**⑩ 隐私与安全**
密码以 Windows DPAPI 密文存储（仅本机能解密），明文密码绝不写入日志；日志里的 sessionId、ticket 等临时凭据一律脱敏。程序不向任何第三方上传数据。

---

## 本次更新（v2.1.1）

- **后台保活不再在任务栏通知区域留下图标。** 旧版为了让提醒有落点，一启动就把图标挂进通知区域且从不移除，于是那里会长期蹲着一个点它也没反应的校园网图标。现在改成「按需挂载、弹完即卸」：平时一个图标都没有，只有真的触发「网络已恢复」「已让位给其他设备」这类提醒时才短暂出现约 8 秒，随后自动消失。
- **修复自行打包时图标丢失的问题。** `--icon` 只设置 exe 文件自身的图标、不会把 `app.ico` 放进运行时目录，导致用 `src/build.bat` 打出来的 exe 标题栏 / 任务栏图标退回默认样式。已补齐打包参数（同时补上惰性导入的 `notify_win` 模块）。

完整变更记录见仓库内的 `更新日志.txt`。

---

## 下载

| 文件 | 说明 |
|------|------|
| `CampusLogin.exe` | 直接运行版，无需安装、无需 Python，双击即用 |
| `USAGE.md` | 使用说明（即仓库里的 `使用说明.md`） |

源码见下方 `Source code (zip)`（GitHub 自动生成）。

## 快速开始

1. 把 `CampusLogin.exe` 放到任意普通文件夹（**不要放 `Program Files`**，避免权限问题），双击运行。
2. 第一次运行：在「连接」页填写校园网账号、密码，选择运营商，点 **保存并立即登录**。
3. 想让它一直帮你保活：点 **启动后台保活**。之后关掉窗口也不影响联网。
4. 想开机就自动联网：打开 **开机自启**。

## 首次运行提醒

`CampusLogin.exe` 未做代码签名，Windows SmartScreen 可能提示“Windows 已保护你的电脑”。这是未签名软件的正常现象：点 **更多信息** → **仍要运行** 即可。

## 适用环境

- Windows 10 / 11（64 位）
- 锐捷 ePortal / CAS 体系的校园网（各校网关细节略有差异，遇到适配问题欢迎带上 `login.log` 到 Issues 反馈）

## 说明

本项目仅供合法校园网用户个人自用，请遵守所在学校的网络管理规定。
```

## 需要上传的 Assets

1. `CampusLogin.exe`
2. `USAGE.md` —— **必须是这个英文名**，GitHub 会把非 ASCII 文件名的附件改名成 `default.md`
3. `Source code (zip)` / `Source code (tar.gz)` —— GitHub 自动生成，不要手动传

> `USAGE.md` 就是仓库里的 `使用说明.md`，上传时改名再传即可（内容完全一样）。

## 发布前检查清单

- [x] `UPDATE_REPO` 为 `DECKS555/campus-login`（`src/app.py`）
- [x] `src/app.py` 里 `APP_VERSION = "v2.1.1"`，与 Tag 一致
- [x] `CampusLogin.exe` 已换成 v2.1.1，且与 `src/` 源码一致
- [x] `CampusLogin.exe --gui-smoke` 自检通过（53 项断言全过）
- [x] 仓库内没有 `config.json` / `login.log` / `app.pid` / `daemon_state.json`
- [x] README / Release 文案里的用户名、仓库名已替换为真实值
- [ ] 仓库设为 **Public**（否则用户点「检查更新」会 404）

## 推送命令

> 本地仓库已提交并打好 `v2.1.1` tag，所以**只需执行下面两条 push**。

```powershell
cd D:\first-cc\campus-login

# 把 <TOKEN> 换成你的 GitHub Personal Access Token（生成步骤见 D:\first-cc\上传步骤.md）
git push https://DECKS555:<TOKEN>@github.com/DECKS555/campus-login.git main
git push https://DECKS555:<TOKEN>@github.com/DECKS555/campus-login.git v2.1.1
```

如果上面的方式提示认证失败，改用不带 token 的地址推送，等弹出登录框时：
用户名填 `DECKS555`，密码框粘贴 **Token**（不是 GitHub 登录密码）。

更安全的方式（不在命令行里留下 token）：

```bash
git -c credential.helper="!printf 'username=DECKS555\npassword=<TOKEN>\n'" \
    -c credential.helperOnce=true push -u origin main
git -c credential.helper="!printf 'username=DECKS555\npassword=<TOKEN>\n'" \
    -c credential.helperOnce=true push origin v2.1.1
```
