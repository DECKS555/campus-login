#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
校园网一键登录 —— GUI / CLI 入口（app.py）
====================================================================
用法：
  CampusLogin.exe               打开图形界面（双击即用）
  CampusLogin.exe --daemon      无界面后台保活（开机自启任务用的就是它）
  CampusLogin.exe --once        检测并登录一次后退出（排错用）
  CampusLogin.exe --probe       诊断输出（网络状态 + AES 自检 + 网关跳转链）
  CampusLogin.exe --devices     查「当前谁在用这个账号」（在线设备 MAC/类型 + 判定）
  CampusLogin.exe --gui-smoke   界面自检（QA 用：逐页切换 + 控件度量 + JSON 报告）
  CampusLogin.exe --version     打印版本号、内置更新仓库、配置文件路径

源码运行：python app.py [--daemon|--once|--probe|--devices]
依赖：Python 标准库（tkinter + 同目录 login_core + ui_render）为主；若环境里有
Pillow，则界面自动升级为抗锯齿渲染（见下），没有也能正常跑。

界面（按设计稿重构，后又做了一轮「去冗余 + 抗锯齿」改造）：
  * **三页导航**：连接 / 设置 / 日志。原先六页（连接状态 / 账号设置 / 保活设置 /
    开机自启 / 运行日志 / 关于）里，后五页的内容都在连接状态页上又以卡片重复了
    一遍，才是界面拥挤、要滚动、看着杂乱的根源；
  * 左侧 200px 导航栏：顶部品牌区（图标 + 名称 + 版本）、三页导航、底部实时状态
    块（状态 + 运营商 + 出口 IP）。**客户区不再画 44px 品牌条**——系统标题栏已经
    显示应用名，且已被 DWM 染成品牌蓝，再叠一条就是上下两条重复标题；
  * 连接页：渐变状态主卡（IP / 运营商两列指标）+「账号信息」「登录与保活」两列 +
    通栏运行日志卡，**要求一屏放下、不滚动**（--gui-smoke 有几何断言守着）；
  * 抗锯齿（可选增强）：圆角 / 渐变 / 投影统一走 ui_render.py，用 Pillow 在 3 倍
    超采样下绘制再 LANCZOS 缩小，投影是真高斯模糊。tkinter 的 Canvas 不做抗锯齿，
    原来的圆弧采样多边形 + 同心矩形「假投影」边缘有阶梯、圆角有缺口。没有 Pillow
    时自动退回老画法，功能不受影响，只是边缘带锯齿；
  * 图标经 ui_render.rescale 缩放：Tk 9 的 PhotoImage 读 SVG 时 -width/-height 是
    **裁剪**不是缩放，直接用会把图标裁成残片。图标一律用 assets/icons 下的 SVG
    变体，禁用 emoji；
  * 滚动条去掉 clam 主题的上下箭头，只留扁平滑块；输入框也去掉了 clam 的方框
    边框（不重设 layout 的话，圆角画布里会套一个直角方块）；
  * 标题栏用 DWM API 染成品牌蓝（Win10 不支持时静默降级为系统默认）；
  * 「在线时长」「今日流量」按需求/无数据源移除，随之去掉的还有每秒重绘的定时器。

保留的原有骨架（不得改动语义）：
  * 所有网络 / 计划任务 / 进程操作都在后台线程执行，通过 root.after 回调刷新
    界面（_run_bg + on_done），子线程绝不触碰任何 Tk 控件；
  * 计划任务名 CampusLoginAppAutoStart；单实例互斥量 campus_login_app_singleton_v1；
  * CLI 五模式（--daemon/--once/--probe/--start-keepalive/--stop-keepalive）；
  * main() 里 write_alive_marker() 与 finally 里的 remove_alive_marker()。
"""

import logging
import json
import math
import os
import queue
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser

# 源码模式下保证能 import 同目录的 login_core；
# frozen（PyInstaller）模式下两者已一同打进 exe，此行无副作用。
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import login_core as core

# 纯命令行模式：这些开关下进程不需要图形界面，**刻意不加载 tkinter**。
# 原因：PyInstaller onefile 下，一旦导入 tkinter 就会把 _tkinter.pyd / tcl90.dll /
# tcl9tk90.dll 载入内存并锁住，这些 DLL 被锁也是 _MEI 残留目录删不掉的原因之一；
# daemon 常驻还会白白多占内存。GUI（双击启动，不带这些开关）行为保持不变。
# 注意：--gui-smoke 需要 GUI，因此不在本列表里。
_CLI_ONLY_FLAGS = ("--daemon", "--once", "--probe", "--devices",
                   "--start-keepalive", "--stop-keepalive", "--version")
_CLI_MODE = any(flag in sys.argv[1:] for flag in _CLI_ONLY_FLAGS)

# tkinter 缺失时 CLI 三件套（--daemon/--once/--probe）仍可用；
# 纯 CLI 模式下刻意跳过导入（惰性/条件导入），GUI 模式才导入。
if _CLI_MODE:
    HAS_TK = False
else:
    try:
        import tkinter as tk
        import tkinter.font as tkfont
        from tkinter import messagebox, ttk
        HAS_TK = True
    except ImportError:
        HAS_TK = False

# 抗锯齿渲染原语（Pillow 3x 超采样 -> LANCZOS）。仅在 GUI 模式下导入：
# daemon/CLI 模式刻意不加载 tkinter，而 PIL.ImageTk 会捎带把它拉进来。
# 导入失败不影响功能——round_rect/_draw_round_gradient 会自动退回 Canvas 画法。
if HAS_TK:
    try:
        import ui_render
    except Exception:
        ui_render = None
else:
    ui_render = None

# 每个 Canvas 上最多保引用多少张位图（防重绘时无限累积）
_IMG_KEEP = 48


def _hold_image(canvas, iid, photo):
    """把 PhotoImage 的引用挂在 canvas 上。

    不这么做的话：PhotoImage 的 Python 对象被 GC 时，底层 Tk image 会被一并
    销毁，canvas 上刚创建的 item 立刻变空白——tkinter 最经典的坑。重绘会不断
    产生新 item id，所以超过 _IMG_KEEP 条时只保留最新的那批（正在显示的一定
    是最新创建的，不会被误删）。
    """
    store = getattr(canvas, "_img_refs", None)
    if store is None:
        store = {}
        canvas._img_refs = store
    store[iid] = photo
    if len(store) > _IMG_KEEP:
        for k in list(store.keys())[:-_IMG_KEEP]:
            del store[k]

# ============================ 常量 ============================

TASK_NAME = "CampusLoginAppAutoStart"   # 不得占用原程序的 CampusNetworkLogin
# 日志行高的**粗略估值**，只用来"先按最乐观的行数把日志读出来"，最终能放几行由
# 实测行高决定（见 refresh_log_tail）。真实行距是 23px（12px 字 21px + 上下各 1px
# 间距），原来写死 21 会多算一行，末行被卡片下边缘裁掉——即用户报的"上下拉窗口
# 时运行日志显示异常"。
LOG_ROW_H = 23
LOG_TAIL_MAX = 40                       # 日志卡最多显示几行（窗口拉很高时的上限）
# 日志卡里连**一行日志都放不下**（可用高度不足）时整栏收起：上下拉窗口把高度压到
# 最小时，卡片只剩标题、硬挤一行出来只会被下边缘裁半截——用户报的"上下拉动窗口时
# 运行日志显示异常"。放得下就显示，一行也放不下就收起来。
LOG_MIN_ROOM = LOG_ROW_H
LOG_PAGE_LINES = 200                    # 运行日志页完整视图行数
APP_VERSION = "v2.1.1"

# 更新服务器的「仓库名」，格式 "用户名/仓库名"（GitHub Releases）。
# 留空 = 没有配更新服务器，「检查更新」退化为只显示本机版本信息。
# 也可以用 config.json 里的 update_repo 覆盖（不用重新打包就能改）。
# 注意：仓库必须是 Public，匿名请求才读得到 Release（私有仓库返回 404）。
UPDATE_REPO = "DECKS555/campus-login"

# 开机自启任务的「网络已连接」触发器订阅：监听 NetworkProfile 的 EventID 10000
# （网络连接成功）。Register-ScheduledTask 没有直接建事件触发器的参数，必须用
# CIM 拼一个 MSFT_TaskEventTrigger，这个 XML 就是它的 Subscription。
#
# 这个常量曾经**只被引用、没有被定义**（从原 install_autostart.bat 移植时漏了），
# 于是 _build_register_ps 每次都抛 NameError，开机自启功能从来没成功过——界面表现
# 是点开关后立刻回弹、没有 UAC 也没有任何可用的提示。
NET_EVENT_SUBSCRIPTION = (
    "<QueryList><Query Id=\"0\" "
    "Path=\"Microsoft-Windows-NetworkProfile/Operational\">"
    "<Select Path=\"Microsoft-Windows-NetworkProfile/Operational\">"
    "*[System[EventID=10000]]</Select></Query></QueryList>"
)

# 运营商：界面文字 <-> config 里的 service 值
SERVICE_LABELS = {"cmcc": "移动", "ctcc": "电信", "unicom": "联通", "local": "教育网"}
SERVICE_ORDER = ["移动", "电信", "联通", "教育网"]
LABEL_TO_SERVICE = {v: k for k, v in SERVICE_LABELS.items()}

# 导航页定义：(page_id, 显示名, 图标名)
# 刻意只留三页：这个程序只做一件事（认证 + 保活）。原来「连接状态 / 账号设置 /
# 保活设置 / 开机自启 / 运行日志 / 关于」六页里，后五页的内容都在连接状态页上
# 又以卡片形式重复了一遍——既让界面显得杂乱，也是内容溢出屏幕的主因。
# 「设置」放最后：它只剩保活参数/高级参数这类低频项，日志比它更常看。
PAGES = (
    ("status", "连接", "wifi"),
    ("log", "日志", "clock"),
    ("settings", "设置", "refresh"),
)


# ============================ UI 设计令牌（按设计规范 README-给开发AI.md） ============================

# —— 色板 ——
C_PRIMARY = "#014DB2"          # 主色 / 品牌蓝
C_GRAD_TOP = "#0059C7"         # 渐变主按钮顶
C_GRAD_BOTTOM = "#003B99"      # 渐变主按钮底
C_SUCCESS = "#10B981"          # 成功绿
C_SUCCESS_TEXT = "#059669"     # 成功文字（浅底上更清晰的一档）
C_WARN = "#F59E0B"             # 警示黄
C_WARN_TEXT = "#B45309"        # 警示文字
C_DANGER = "#EF4444"           # 错误红
C_DANGER_TEXT = "#B91C1C"      # 错误文字
C_BLUE_LIGHT = "#E8F0FE"       # 浅蓝底（次级按钮 / 选中背景）
C_BLUE_LIGHT_BORDER = "#C6DAF8"
C_CARD_BLUE = "#F0F6FF"        # 浅蓝卡片底（侧边状态小块底）
C_RED_LIGHT = "#FEF2F2"        # 停止保活按钮底
C_RED_LIGHT_BORDER = "#FECACA"
C_AMBER_LIGHT = "#FEF3C7"      # 顶部黄色提示条底
C_AMBER_TEXT = "#92400E"
C_NEUTRAL = "#F3F4F6"          # 中性底（快捷操作 / 维护按钮）
C_NEUTRAL_HOVER = "#E9EBEE"
C_BG = "#F9F9F9"               # 画布底
C_CARD = "#FFFFFF"             # 卡片白
C_TEXT = "#0A1628"             # 主文字
C_TEXT_2 = "#6B7280"           # 次文字
C_TEXT_3 = "#9CA3AF"           # 弱文字 / 占位
C_BORDER = "#EAEAEA"           # 卡片描边
C_INPUT_BORDER = "#D6D9DE"     # 输入框描边
C_NAV_TEXT = "#374151"         # 导航默认文字
C_SIDEBAR_LINE = "#E5E7EB"     # 侧边栏分隔线
C_HERO_SUB = "#C7DBF7"         # 主卡副标题浅蓝
C_HERO_METRIC = "#B9CDEF"      # 主卡指标标签浅蓝
C_PLACEHOLDER = "#9CA3AF"      # 输入框占位提示灰
C_LOG_BG = "#FAFBFC"           # 日志文本框底色
C_SHADOW = "#101A28"           # 卡片投影基色（规范 rgba(16,26,40,.05)）
C_SCROLL_THUMB = "#D8DBDF"     # 滚动条滑块
C_SCROLL_THUMB_HOVER = "#BFC5CD"

# —— 字体 ——
FONT_UI = "Microsoft YaHei UI"     # 中文回退（本机无 Noto Sans SC）
FONT_NUM = "Segoe UI"              # 数字 / 英文
FONT_MONO = "Consolas"             # 日志等宽

# —— 尺寸（逻辑像素，绘制时统一过 _px()）——
# 高度从规范的 780 提到 840：三页化之后连接页要放「状态主卡 + 账号信息/登录保活
# 两列 + 运行日志卡」，按规范的字号/间距实测需要约 810px 才不溢出。780 会逼着
# 把行距压得比规范更紧，反而不好看；840 在 1080p 屏上仍只占约八成高。
WIN_W, WIN_H = 1060, 840
WIN_MIN_W, WIN_MIN_H = 960, 660
SIDEBAR_W = 200
# 页面四周留白：24 时蓝色主卡离窗口边框太远（用户要求「距离适当调小」）。
# 16 是能保持三页左右边缘仍然对齐、又不让主卡显小的一档。
CONTENT_PAD = 16
CARD_GAP = 16
CARD_RADIUS = 12
CARD_PAD = 18


# ============================ 通用小工具 ============================

def _to_int(value, default, lo, hi):
    """把输入框文本安全转成 [lo, hi] 内的整数，非法回落 default。"""
    try:
        v = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def ensure_console_output():
    """windowed exe（--noconsole 打包）在终端里跑 --once/--probe 时，
    把 stdout/stderr 接到调用方的控制台；双击启动或 GUI 模式下无效果。"""
    if sys.stdout is not None:
        return
    try:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32")
        ATTACH_PARENT_PROCESS = 0xFFFFFFFF  # -1
        if kernel32.AttachConsole(ATTACH_PARENT_PROCESS):
            conout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
            sys.stdout = conout
            sys.stderr = conout
    except Exception:
        pass


# ============================ 高 DPI 感知（必须在创建 Tk root 之前调用） ============================

# 全局 DPI 缩放比例（1.0 = 100%）。供界面按比例调整像素度量使用。
DPI_SCALE = 1.0
_DPI_DONE = False  # 防重复设置


def enable_dpi_awareness():
    """在创建 Tk root 之前调用：声明进程 DPI 感知，让 125%/150% 缩放屏下文字清晰不糊。

    优先级：先试 shcore.SetProcessDpiAwareness(1)（PROCESS_SYSTEM_DPI_AWARE，
    Windows 8.1+），失败再退 user32.SetProcessDPIAware()（老系统）。
    顺带用 LOGPIXELSX 估算缩放比例返回，便于按比例调整字体/控件度量。
    全程异常吞掉并静默降级（返回 1.0），绝不因此崩溃；重复调用只生效一次。
    """
    global DPI_SCALE, _DPI_DONE
    if _DPI_DONE:
        return DPI_SCALE
    _DPI_DONE = True
    scale = 1.0
    try:
        import ctypes
        # 1) 估算系统 DPI 缩放（用于按比例放大像素度量）
        try:
            user32 = ctypes.windll.user32
            gdi32 = ctypes.windll.gdi32
            hdc = user32.GetDC(0)
            dpi = gdi32.GetDeviceCaps(hdc, 88)  # LOGPIXELSX：96=100%, 120=125%, 144=150%
            user32.ReleaseDC(0, hdc)
            if dpi and dpi > 0:
                scale = dpi / 96.0
        except Exception:
            scale = 1.0
        # 2) 声明 DPI 感知
        ok = False
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
            ok = True
        except Exception:
            ok = False
        if not ok:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
    except Exception:
        scale = 1.0
    DPI_SCALE = scale
    return scale


def _px(n):
    """把 100% 下的像素度量按当前 DPI 缩放（用于行高/圆点等像素级尺寸）。"""
    try:
        return int(round(n * DPI_SCALE))
    except Exception:
        return int(n)


_FONT_CACHE = {}


def ui_font(size=12, weight="normal", family=None):
    """按 (family, size, weight) 缓存 tkfont.Font。

    tkfont.Font(...) 每 new 一个就要在 Tcl 里 font create（对象销毁时再 font
    delete），是两趟 Tcl 往返。绘制路径里 _content_width / _draw / Pill.set 等
    每帧都会建一两个，拖拽窗口时十几个控件叠起来就是上百趟往返。HeroCard 早就
    改成在 __init__ 里建好并复用，这里把同样的做法做成公共函数给其余控件用。
    """
    key = (family or FONT_UI, size, weight)
    f = _FONT_CACHE.get(key)
    if f is None:
        f = tkfont.Font(family=key[0], size=size, weight=weight)
        _FONT_CACHE[key] = f
    return f


def fit_text_to_width(font, text, budget, ellipsis="…"):
    """按像素宽度截断文本，放不下时补省略号。

    旧写法是每砍一个字符就重新 measure 一次（O(n²) 次字体度量），而日志行是每次
    刷新都要跑几十行、HeroCard 副标题在窗口拖动时每帧都跑——都落在热路径上。
    被测量的前缀是单调变长的，所以直接对「保留多少个字符」二分：O(log n) 次测量。
    """
    if budget <= 0:
        return ""
    if font.measure(text) <= budget:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if font.measure(text[:mid] + ellipsis) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return (text[:lo] + ellipsis) if lo else ellipsis


# ============================ 资源（图标 / 插画） ============================

def asset_path(name):
    """定位打包资源：
    frozen（PyInstaller）时从解包目录 sys._MEIPASS 取；源码运行时从 assets\\ 取。
    支持子目录相对路径（如 icons/icon-wifi-white.svg、design/空态插画-未连接-1024.png）。
    找不到返回 None（调用方自行兜底，不影响功能）。

    frozen 下**两个位置都要找**：--add-data 指定的目标目录可能就是解包根目录
    （icons 这么打，于是走 _MEIPASS/icons/...），也可能是 assets 子目录
    （app.png / app.ico 这么打，于是走 _MEIPASS/assets/...）。只查一处的结果就是
    exe 里图标永远找不到、任务栏显示 Tk 默认的羽毛图标（用户实测踩过）。
    """
    if getattr(sys, "frozen", False):
        root = getattr(sys, "_MEIPASS", core.BASE_DIR)
        for base in (root, os.path.join(root, "assets")):
            p = os.path.normpath(os.path.join(base, name))
            if os.path.exists(p):
                return p
        return None
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "assets")
    p = os.path.normpath(os.path.join(base, name))
    return p if os.path.exists(p) else None


def get_local_ip(portal_host=""):
    """取本机出口 IP（UDP getsockname 方式，只读路由表、不发包、无副作用）。

    优先连认证网关（校园网环境），失败再连公网 DNS；都失败返回 None（显示「—」）。
    """
    targets = [portal_host] if portal_host else []
    targets += ["223.5.5.5", "114.114.114.114"]
    for host in targets:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.settimeout(1.0)
                s.connect((host, 80))
                ip = s.getsockname()[0]
                if ip:
                    return ip
            finally:
                s.close()
        except Exception:
            continue
    return None


# ============================ 计划任务（开机自启） ============================

def _ps_quote(s):
    """包成 PowerShell 单引号字符串字面量（内部单引号翻倍）。"""
    return "'" + str(s).replace("'", "''") + "'"


def _write_ps1(content):
    """把 PowerShell 脚本写到 %TEMP% 临时文件。
    用 utf-8-sig（带 BOM）：PowerShell 5.1 才能正确识别含中文的路径。"""
    fd, path = tempfile.mkstemp(suffix=".ps1", prefix="CampusLoginApp_")
    with os.fdopen(fd, "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write(content)
    return path


def _remove_temp(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _task_action_paths():
    """计划任务的动作：frozen 直接跑 exe --daemon；源码模式跑 python app.py --daemon。"""
    if getattr(sys, "frozen", False):
        return sys.executable, "--daemon", core.BASE_DIR
    return (sys.executable,
            '"%s" --daemon' % os.path.abspath(__file__),
            core.BASE_DIR)


def _build_register_ps(exe_path, argument, work_dir, task_name):
    """生成注册计划任务的 PowerShell 脚本（参数照搬原 install_autostart.bat）：
    触发器 = 用户登录 + 网络连接成功（NetworkProfile/Operational EventID 10000）；
    设置 = Hidden / 电池供电也运行 / 无运行时长限制 / IgnoreNew；
    Principal = 当前用户 Interactive + RunLevel Limited。"""
    lines = [
        "$ErrorActionPreference = 'Stop'",
        "$action = New-ScheduledTaskAction -Execute %s -Argument %s -WorkingDirectory %s"
        % (_ps_quote(exe_path), _ps_quote(argument), _ps_quote(work_dir)),
        "$logonTrigger = New-ScheduledTaskTrigger -AtLogOn",
        "$cls = Get-CimClass -ClassName MSFT_TaskEventTrigger -Namespace Root/Microsoft/Windows/TaskScheduler",
        "$netTrigger = New-CimInstance -CimClass $cls -ClientOnly",
        "$netTrigger.Enabled = $true",
        "$netTrigger.Subscription = %s" % _ps_quote(NET_EVENT_SUBSCRIPTION),
        "$settings = New-ScheduledTaskSettingsSet -Hidden -AllowStartIfOnBatteries"
        " -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)"
        " -MultipleInstances IgnoreNew",
        "$me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name",
        "$principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited",
        "Register-ScheduledTask -TaskName %s -Action $action -Trigger @($logonTrigger, $netTrigger)"
        " -Settings $settings -Principal $principal -Force | Out-Null" % _ps_quote(task_name),
        "Write-Output 'REGISTER_OK'",
    ]
    return "\n".join(lines) + "\n"


def _build_unregister_ps(task_name):
    lines = [
        "$ErrorActionPreference = 'Stop'",
        "Unregister-ScheduledTask -TaskName %s -Confirm:$false" % _ps_quote(task_name),
        "Write-Output 'UNREGISTER_OK'",
    ]
    return "\n".join(lines) + "\n"


def _run_ps(ps_path, timeout=90):
    """非提权运行 PowerShell 脚本，返回 (returncode, 合并输出文本)。"""
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps_path],
            capture_output=True, timeout=timeout, creationflags=core.CREATE_NO_WINDOW,
            env=core.clean_child_env())
        return p.returncode, (p.stdout + p.stderr).decode("utf-8", "ignore")
    except subprocess.TimeoutExpired:
        return 124, "执行超时"
    except OSError as e:
        return 1, str(e)


def _query_task():
    """开机自启计划任务当前是否存在（schtasks /Query 校验）。异常按不存在处理。"""
    try:
        p = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME],
                           capture_output=True, timeout=15,
                           creationflags=core.CREATE_NO_WINDOW,
                           env=core.clean_child_env())
        return p.returncode == 0
    except Exception:
        return False


# ---------- 无 UAC 的开机自启：原生命令优先，注册表 Run 兜底 ----------
#
# 以前只走 PowerShell Register-ScheduledTask，很多机器上**提权才会成功**，于是每
# 次拨动开关都弹 UAC（用户明确要求不要弹），而且 PowerShell 冷启动要 1~2 秒。
# 现在改成三级降级，**每一级都不弹窗**：
#   1) schtasks.exe 原生命令（最快，约 0.2s）
#   2) PowerShell 脚本（保留"登录 + 联网"双触发器，非提权）
#   3) 注册表 Run 项（当前用户权限一定写得了，静默兜底；只保留登录时触发）
# 注意：给 winreg 用的子项路径**不能带 HKCU\ 前缀**（根键由 HKEY_CURRENT_USER 指定）
REG_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
REG_RUN_VALUE = "CampusLoginApp"


def _schtasks_create(exe_path, argument, work_dir):
    """用 schtasks.exe 原生命令注册（比 PowerShell 快一个数量级，无窗口）。"""
    tr = '"%s" %s' % (exe_path, argument)
    cmd = ["schtasks", "/Create", "/TN", TASK_NAME, "/TR", tr,
           "/SC", "ONLOGON", "/RL", "LIMITED", "/F"]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=20,
                           creationflags=core.CREATE_NO_WINDOW,
                           env=core.clean_child_env())
        if p.returncode == 0:
            return True, "ok"
        out = (p.stderr + p.stdout).decode("utf-8", "ignore").strip()
        return False, (out.splitlines()[-1] if out else "schtasks 返回 %d"
                       % p.returncode)
    except Exception as e:
        return False, str(e)


def _schtasks_delete():
    try:
        p = subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
                           capture_output=True, timeout=20,
                           creationflags=core.CREATE_NO_WINDOW,
                           env=core.clean_child_env())
        return p.returncode == 0
    except Exception:
        return False


def _reg_run_command():
    """注册表 Run 项的命令行（Run 值本身就是一条完整命令）。"""
    exe_path, argument, _wd = _task_action_paths()
    return '"%s" %s' % (exe_path, argument)


def _reg_run_key(write=False):
    """打开当前用户的 Run 注册表项。

    用 winreg 而不是 reg.exe：后者要另起一个进程（慢、且可能被安全策略拦），
    winreg 是标准库直调 API，零额外进程、无窗口、也不需要管理员权限。
    """
    import winreg
    access = winreg.KEY_WRITE if write else winreg.KEY_READ
    return winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_RUN_KEY, 0, access)


def _reg_run_set(value=None):
    """把开机自启写进当前用户的注册表 Run 项：无需管理员、无 UAC、无窗口。

    value 允许传一个临时名字给自检用（避免测试时动真实配置）。
    """
    import winreg
    try:
        with _reg_run_key(write=True) as k:
            winreg.SetValueEx(k, value or REG_RUN_VALUE, 0, winreg.REG_SZ,
                              _reg_run_command())
        return _reg_run_exists(value)
    except Exception:
        return False


def _reg_run_delete(value=None):
    import winreg
    try:
        with _reg_run_key(write=True) as k:
            winreg.DeleteValue(k, value or REG_RUN_VALUE)
    except Exception:
        pass
    return not _reg_run_exists(value)


def _reg_run_exists(value=None):
    """注册表 Run 项里是否有我们的开机自启（读不到值按不存在处理）。"""
    import winreg
    try:
        with _reg_run_key() as k:
            winreg.QueryValueEx(k, value or REG_RUN_VALUE)
        return True
    except Exception:
        return False


def autostart_enabled():
    """开机自启当前是否生效：计划任务或注册表 Run 项任一存在即算开启。

    不能只查计划任务——降级到注册表兜底的情况下任务计划里是没有任务的，只看任务
    会导致界面把"明明开了"显示成"未开启"。
    """
    return _query_task() or _reg_run_exists()


def register_autostart():
    """注册开机自启（任务名 CampusLoginAppAutoStart）。

    **全程不弹 UAC**（用户要求）。三级降级，每一级都不弹窗：
      1. schtasks.exe 原生命令 —— 最快，约 0.2 秒
      2. PowerShell Register-ScheduledTask（非提权）—— 保留"登录 + 联网"双触发器
      3. 注册表 Run 项 —— 当前用户权限必成，代价是只有"登录时"这一个触发器
    每级做完都用 autostart_enabled() 复核真实状态，成功即刻返回。
    返回 (是否成功, 提示消息)。
    """
    exe_path, argument, work_dir = _task_action_paths()

    # 1) 最快路径：schtasks 原生命令
    ok, why = _schtasks_create(exe_path, argument, work_dir)
    if ok and autostart_enabled():
        return True, "开机自启已设置（下次登录 Windows 时自动启动）"

    # 2) PowerShell：双触发器（登录 + 联网成功），仍然不提权
    ps_path = None
    try:
        ps_path = _write_ps1(_build_register_ps(exe_path, argument, work_dir,
                                                TASK_NAME))
        _rc, _out = _run_ps(ps_path)
        if autostart_enabled():
            return True, "开机自启已设置（登录或联网时自动启动）"
    except Exception:
        pass
    finally:
        if ps_path:
            _remove_temp(ps_path)

    # 3) 静默兜底：注册表 Run 项（无 UAC，必然成功）
    if _reg_run_set() and _reg_run_exists():
        # 这一级只有"登录时"一个触发器（Run 项没法挂网络事件），所以如实说明，
        # 并点出兜底机制：保活循环自己会重试，网络晚一点就绪也能连上。
        return True, ("开机自启已设置（下次登录 Windows 时自动启动；"
                      "若那时网络还没就绪，保活会自行重试连上）")

    return False, "设置开机自启失败：%s" % (why or "未知原因")


def unregister_autostart():
    """取消开机自启：同时清掉计划任务和注册表 Run 项（都可能被降级路径用过）。
    同样**全程不弹 UAC**。返回 (是否成功, 提示消息)。"""
    if not autostart_enabled():
        return True, "本来就没有设置开机自启，无需取消"

    _schtasks_delete()
    ps_path = None
    try:
        if _query_task():
            ps_path = _write_ps1(_build_unregister_ps(TASK_NAME))
            _run_ps(ps_path)
            if _query_task():
                _run_ps(ps_path)      # 非提权删不掉时再试一次（仍不弹窗）
    except Exception:
        pass
    finally:
        if ps_path:
            _remove_temp(ps_path)
    _reg_run_delete()

    if not autostart_enabled():
        return True, "已取消开机自启"
    return False, "取消开机自启失败，请稍后重试（也可在「任务计划程序」中手动删除 CampusLoginAppAutoStart）"


# ============================ 后台保活进程 ============================

def spawn_daemon():
    """以分离子进程启动本程序的 --daemon 模式：关掉 GUI 不受影响、无任何窗口。"""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--daemon"]
    else:
        cmd = [sys.executable, os.path.abspath(__file__), "--daemon"]
    return subprocess.Popen(
        cmd,
        cwd=core.BASE_DIR,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=core.CREATE_NO_WINDOW | core.DETACHED_PROCESS,
        close_fds=True,
        # 关键修复：传清洗过的环境（去掉 _MEIPASS/_PYI_* 并剔除 PATH 里的 _MEI 项），
        # 并叠加 PYINSTALLER_RESET_ENVIRONMENT=1（PyInstaller>=6.10 官方开关），让
        # daemon 子进程重置 PyInstaller 环境、**自建独立解包目录**，不复用父进程的 _MEI，
        # 从而父进程退出时能正常删除自己的 _MEI 目录（不再弹删除失败框）。
        env=core.clean_child_env({"PYINSTALLER_RESET_ENVIRONMENT": "1"}))


# ============================ GUI ============================

if HAS_TK:

    # ---------- 颜色与绘制基元 ----------

    def _mix(c1, c2, t):
        """线性插值两个 #RRGGBB 颜色：t=0 返回 c1，t=1 返回 c2。"""
        def h2rgb(c):
            c = (c or "#000000").lstrip("#")
            if len(c) != 6:
                return 0, 0, 0
            return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
        r1, g1, b1 = h2rgb(c1)
        r2, g2, b2 = h2rgb(c2)
        r = int(round(r1 + (r2 - r1) * t))
        g = int(round(g1 + (g2 - g1) * t))
        b = int(round(b1 + (b2 - b1) * t))
        return "#%02X%02X%02X" % (r, g, b)

    def left_button_down():
        """鼠标左键是否还按着（用来判断用户是不是正在拖窗口边框）。

        为什么问系统而不是绑 Tk 事件：拖窗口边框拖的是**非客户区**，那些鼠标事件
        归 Windows 自己处理，Tk 根本收不到 <ButtonPress>/<ButtonRelease>。
        """
        try:
            import ctypes
            return bool(ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000)
        except Exception:
            return False

    def round_rect_points(x0, y0, x1, y1, r, steps=8):
        """圆角矩形的顶点表（四角按圆弧采样成多边形）。

        不用 Tk 的 smooth=True：抛物线样条会经过直角顶点，在四角产生外凸毛刺。
        圆弧采样在 8~12px 半径下视觉足够平滑，而且边界精确。
        """
        r = max(0.0, min(r, (x1 - x0) / 2.0, (y1 - y0) / 2.0))
        pts = []

        def arc(cx, cy, a0, a1):
            for i in range(steps + 1):
                a = math.radians(a0 + (a1 - a0) * i / steps)
                pts.append(cx + r * math.cos(a))
                pts.append(cy + r * math.sin(a))

        arc(x1 - r, y0 + r, 270.0, 360.0)   # 右上角
        arc(x1 - r, y1 - r, 0.0, 90.0)      # 右下角
        arc(x0 + r, y1 - r, 90.0, 180.0)    # 左下角
        arc(x0 + r, y0 + r, 180.0, 270.0)   # 左上角
        return pts

    def sketch_rect(canvas, x0, y0, x1, y1, r, color, tags=None):
        """拖拽窗口期间的廉价占位：一个多边形，不投影、不描边、不渐变。

        整套界面用它重绘约 3ms（对比走 Pillow 位图的 260ms），所以拖拽是跟手的；
        鼠标停下后再由 app 统一重绘成高质量位图。
        """
        return canvas.create_polygon(round_rect_points(x0, y0, x1, y1, r),
                                     fill=color or "", outline="", tags=tags)

    def round_rect(canvas, x0, y0, x1, y1, r, steps=8, **kwargs):
        """在 Canvas 上画圆角矩形，返回 item id。

        优先走 ui_render（Pillow 3 倍超采样 + LANCZOS 缩放）渲染成抗锯齿位图，
        边缘才真正平滑；没有 Pillow 时退回下面的圆弧采样多边形——所以这条路
        永远画得出来，只是边缘带锯齿。

        支持 kwargs：fill / outline / width / tags，外加扩展的
        gradient=(c_top, c_bottom)、shadow=(dx, dy, blur, color, alpha)、
        horizontal=True。注意传了 shadow 时位图会向四周多出 shadow_pad 像素，
        调用方需保证 Canvas 上留得下（见 Card 的 _margin）。
        """
        fill = kwargs.pop("fill", None)
        outline = kwargs.pop("outline", None)
        width = kwargs.pop("width", 1)
        tags = kwargs.pop("tags", None)
        gradient = kwargs.pop("gradient", None)
        shadow = kwargs.pop("shadow", None)
        horizontal = kwargs.pop("horizontal", False)

        w = x1 - x0
        h = y1 - y0
        if ui_render is not None and w >= 1 and h >= 1:
            pad = ui_render.shadow_pad(shadow)
            photo, _pad = ui_render.surface_photo(
                w, h, radius=r, fill=fill, gradient=gradient,
                border=(outline if width else None), border_w=width,
                shadow=shadow, horizontal=horizontal)
            if photo is not None:
                iid = canvas.create_image(x0 - pad, y0 - pad, image=photo,
                                          anchor="nw", tags=tags)
                _hold_image(canvas, iid, photo)
                return iid

        # —— 拖拽窗口期间：只画一块廉价多边形占位 ——
        # 拖拽时控件宽度每变一像素，位图缓存键就变一次，全部失效；走 Pillow 的话
        # 一次完整重绘要 260ms，界面就卡成幻灯片（用户报的"缩放明显卡顿"）。
        if ui_render is not None and ui_render.is_sketch():
            col = fill
            if col is None and gradient is not None:
                col = _mix(gradient[0], gradient[1], 0.5)   # 用中间色当占位
            return sketch_rect(canvas, x0, y0, x1, y1, r, col, tags)

        # —— 回退：没有 Pillow（或位图渲染失败）——
        r = max(0.0, min(r, (x1 - x0) / 2.0, (y1 - y0) / 2.0))
        if gradient is not None:
            # 渐变必须走逐扫描线：多边形填不出渐变，落到下面会拿到 fill=None，
            # 主卡在无 Pillow 的环境里会变成一块**完全不可见**的图形，标题文字
            # 像浮在背景上。
            _draw_round_gradient(canvas, x0, y0, x1, y1, r, gradient[0],
                                 gradient[1], tags, horizontal=horizontal)
            if outline and width:
                return canvas.create_polygon(
                    round_rect_points(x0, y0, x1, y1, r, steps), fill="",
                    outline=outline, width=width, tags=tags)
            return None
        return canvas.create_polygon(
            round_rect_points(x0, y0, x1, y1, r, steps), fill=fill,
            outline=(outline if outline is not None else ""),
            width=width, tags=tags)

    def _draw_round_gradient(canvas, x0, y0, x1, y1, radius, c_top, c_bottom,
                             tag, horizontal=False):
        """画一个带圆角的渐变矩形。

        优先交给 ui_render 用 Pillow 渲染（渐变在低分辨率条上插值后再放大，
        圆角由 SS 分辨率的遮罩抠出，边缘平滑且四角不留缺口）；没有 Pillow 时
        退回原来的逐扫描线画法——那种画法在圆角处会有肉眼可见的阶梯和缺口。
        """
        w = x1 - x0
        h = y1 - y0
        if ui_render is not None and w >= 1 and h >= 1:
            photo, pad = ui_render.surface_photo(
                w, h, radius=radius, gradient=(c_top, c_bottom),
                horizontal=horizontal)
            if photo is not None:
                iid = canvas.create_image(x0 - pad, y0 - pad, image=photo,
                                          anchor="nw", tags=tag)
                _hold_image(canvas, iid, photo)
                return iid

        # —— 拖拽窗口期间：单一多边形占位（逐扫描线要 150 条，一样太贵）——
        if ui_render is not None and ui_render.is_sketch():
            return sketch_rect(canvas, x0, y0, x1, y1, radius,
                               _mix(c_top, c_bottom, 0.5), tag)

        # —— 回退：逐扫描线（原实现）——
        if w <= 1 or h <= 1:
            return None
        r = min(radius, w / 2.0, h / 2.0)
        if horizontal:
            for x in range(int(w)):
                t = x / max(1.0, w - 1)
                col = _mix(c_top, c_bottom, t)
                canvas.create_line(x0 + x, y0, x0 + x, y1, fill=col,
                                   width=1, tags=tag)
            return None
        for i in range(int(h)):
            y = y0 + i + 0.5
            t = (y - y0) / max(1.0, h)
            color = _mix(c_top, c_bottom, t)
            inset = 0.0
            dy_top = y - (y0 + r)
            dy_bot = (y1 - r) - y
            if dy_top < 0:
                v = r + dy_top
                inset = r - math.sqrt(max(0.0, r * r - v * v))
            elif dy_bot < 0:
                v = r + dy_bot
                inset = r - math.sqrt(max(0.0, r * r - v * v))
            canvas.create_line(x0 + inset, y, x1 - inset, y,
                               fill=color, width=1, tags=tag)
        return None

    def card_shadow():
        """卡片投影：规范 4.1 的 `0 2px 8px rgba(16,26,40,.05)`。

        alpha 取 18/255（≈7%）而不是字面的 5%：5% 落在 #F9F9F9 画布底上几乎
        分辨不出来，投影等于白做。7% 仍然克制，但能把卡片从背景里托起来。
        """
        return (0, _px(2), _px(8), C_SHADOW, 18)

    def card_margin():
        """卡片四周为投影预留的留白（设备像素）。

        没有 Pillow 时不画投影，也就不该留白——否则每张卡周围会空出一圈没有
        投影的缝，看起来像布局错了。
        """
        if ui_render is not None:
            return ui_render.shadow_pad(card_shadow())
        return 0

    def card_gap(pady=True):
        """卡片之间的视觉间距。

        卡片自带投影留白（Card._margin，四周各占一点），所以直接在 pack 里再留
        一个 CARD_GAP 会让实际间隙变成 CARD_GAP + 2*margin，明显偏大。这里把留白
        扣掉，保证视觉间隙就是规范里的 CARD_GAP。
        """
        gap = _px(CARD_GAP)
        if ui_render is not None:
            gap = max(0, gap - 2 * ui_render.shadow_pad(card_shadow()))
        return (0, gap) if pady else gap

    def bind_hover(btn, normal_style, hover_style):
        """给 ttk 按钮加显式 hover 反馈（保留的旧工具函数，供 ttk 控件使用）。"""
        def _enter(_e):
            try:
                if not btn.instate(["disabled"]):
                    btn.configure(style=hover_style)
            except Exception:
                pass

        def _leave(_e):
            try:
                if not btn.instate(["disabled"]):
                    btn.configure(style=normal_style)
            except Exception:
                pass

        btn.bind("<Enter>", _enter, add="+")
        btn.bind("<Leave>", _leave, add="+")

    # ---------- 输入框 ----------

    class PlaceholderEntry(ttk.Entry):
        """带灰色占位提示的输入框。

        关键点：占位文字只存在于「显示层」，绝不进入配置——占位生效时 value()
        返回空串，输入真实内容后 value() 才返回用户输入。这样可确保占位提示
        （如「请输入校园网账号」）永远不会被写进 config.json。
        """

        def __init__(self, master, placeholder="", show=None, **kw):
            self._placeholder = placeholder
            self._show = show
            self._ph_active = False
            super().__init__(master, **kw)
            if show is not None:
                self.configure(show=show)
            self._base_fg = C_TEXT
            self._sync()

        # ---------- 占位逻辑 ----------

        def _is_focused(self):
            try:
                return self.focus_get() is self
            except Exception:
                return False

        def _sync(self):
            """根据当前内容/焦点状态渲染：空且未聚焦 -> 显示灰色占位；否则显示真实内容。"""
            has_real = bool(super().get()) and not self._ph_active
            if self._placeholder and not has_real and not self._is_focused():
                self._show_placeholder()
            else:
                self._ph_active = False
                self.configure(foreground=self._base_fg)
                if self._show is not None:
                    self.configure(show=self._show)

        def _show_placeholder(self):
            self._ph_active = True
            if self._show is not None:
                self.configure(show="")  # 占位用明文显示，避免显示成一排星号
            super().delete(0, "end")
            super().insert(0, self._placeholder)
            self.configure(foreground=C_PLACEHOLDER)

        def _clear_placeholder(self):
            if self._ph_active:
                self._ph_active = False
                super().delete(0, "end")
                self.configure(foreground=self._base_fg)
                if self._show is not None:
                    self.configure(show=self._show)

        def _on_focus_in(self, _e=None):
            self._clear_placeholder()

        def _on_focus_out(self, _e=None):
            self._sync()

        def bind_placeholder_events(self):
            """绑定焦点事件（在控件布局完成后调用）。"""
            self.bind("<FocusIn>", self._on_focus_in, add="+")
            self.bind("<FocusOut>", self._on_focus_out, add="+")

        # ---------- 对外接口 ----------

        def value(self):
            """返回真实输入内容（占位生效时为空串，绝不返回占位文字）。"""
            if self._ph_active:
                return ""
            return super().get()

        def set_value(self, text):
            """设置真实内容（不会把内容与占位混淆）。"""
            text = "" if text is None else str(text)
            self._clear_placeholder()
            super().delete(0, "end")
            if text:
                super().insert(0, text)
            self._sync()

        def set_show(self, show):
            """切换密码掩码字符（占位生效时保持明文显示占位）。"""
            self._show = show
            if not self._ph_active:
                self.configure(show=show)

        def refresh_placeholder(self):
            """外部内容变化后手动刷新占位显示。"""
            self._sync()

    class RoundedEntry(tk.Canvas):
        """圆角输入框（高 40、圆角 8）：Canvas 画边框 + 内嵌 PlaceholderEntry。

        支持：前置图标（gray 16）、后置操作（密码显隐的眼睛按钮）、聚焦描边变主色。
        对外代理 PlaceholderEntry 的 value/set_value/set_show 等接口。
        """

        def __init__(self, master, placeholder="", show=None, icon=None,
                     textvariable=None, parent_bg=C_CARD, trailing_command=None,
                     trailing_text=""):
            super().__init__(master, height=_px(38), bg=parent_bg,
                             highlightthickness=0, bd=0)
            self._parent_bg = parent_bg
            self._icon = icon
            self._focused = False
            self._trailing_command = trailing_command
            self._trailing_text = trailing_text
            self._trailing_icon = None
            self._win = None
            self.entry = PlaceholderEntry(
                self, placeholder=placeholder, show=show, style="Flat.TEntry",
                font=(FONT_UI, 14), textvariable=textvariable)
            self.bind("<Configure>", lambda e: self._relayout())
            self.entry.bind("<FocusIn>", lambda e: self._set_focus(True), add="+")
            self.entry.bind("<FocusOut>", lambda e: self._set_focus(False), add="+")

        # ---- 外观 ----

        def _set_focus(self, focused):
            self._focused = focused
            self._relayout()

        def set_trailing(self, text, icon=None):
            """更新后置按钮（眼睛）的文字/图标并重绘。"""
            self._trailing_text = text
            if icon is not None:
                self._trailing_icon = icon
            self._relayout()

        def _relayout(self):
            # delete("all") 会连嵌入 Entry 的 window item 一起删掉。若只把 _win
            # 当作「是否首次布局」的标记，旧 id 依旧非 None，下面就走 else 分支
            # 对着一个已删除的 id 调 coords/itemconfigure（静默无效），Entry 再也
            # 不会被映射 —— 表现为输入框整个消失。每轮重绘必须重新创建 window item。
            self.delete("all")
            self._win = None
            w = self.winfo_width()
            h = _px(40)
            self.configure(height=h)
            if w < _px(20):
                return
            border = C_PRIMARY if self._focused else C_INPUT_BORDER
            round_rect(self, 0, 0, w - 1, h - 1, _px(8),
                       fill=C_CARD, outline=border, width=1)
            left = _px(12)
            if self._icon is not None:
                self.create_image(_px(12), h / 2.0, image=self._icon, anchor="w")
                left = _px(12) + _px(16) + _px(10)
            right = _px(12)
            if self._trailing_command is not None:
                # 后置区（图标 + 文字）按**实测文字宽度**排布。
                # 原来图标位置写死成 tx-34，可「显示」两个中文字实际宽就有 34px，
                # 图标于是整个压在文字里（重叠 16px，肉眼就是糊成一团）。
                pad_r = _px(12)
                gap = _px(8)
                icon_w = _px(16)
                tw = ui_font(12).measure(
                    self._trailing_text)
                text_x = w - pad_r                    # 文字右对齐到这里
                icon_x = text_x - tw - gap - icon_w   # 图标左边缘
                if self._trailing_icon is not None and icon_x >= left:
                    self.create_image(icon_x, h / 2.0, image=self._trailing_icon,
                                      anchor="w", tags="trailing")
                    right = pad_r + tw + gap + icon_w + _px(8)
                else:
                    right = pad_r + tw + _px(8)
                self.create_text(text_x, h / 2.0, text=self._trailing_text,
                                 anchor="e", fill=C_TEXT_2, font=(FONT_UI, 12),
                                 tags="trailing")
                # 每次重绘后重绑一次点击（先解绑避免叠加）
                self.tag_unbind("trailing", "<Button-1>")
                self.tag_bind("trailing", "<Button-1>", self._on_trailing)
            if self._win is None:
                self._win = self.create_window(left, h / 2.0, window=self.entry,
                                               anchor="w")
            else:
                self.coords(self._win, left, h / 2.0)
            # 拖拽期间不改内嵌 Entry 的宽度（重排很贵，见 Card._redraw 的说明）
            if ui_render is not None and ui_render.is_sketch():
                return
            self.itemconfigure(self._win, width=max(_px(40), w - left - right))

        def _on_trailing(self, _e=None):
            if self._trailing_command is not None:
                self._trailing_command()
            return "break"

        # ---- 代理 PlaceholderEntry 接口 ----

        def value(self):
            return self.entry.value()

        def set_value(self, text):
            self.entry.set_value(text)

        def set_show(self, show):
            self.entry.set_show(show)

        def refresh_placeholder(self):
            self.entry.refresh_placeholder()

        def bind_placeholder_events(self):
            self.entry.bind_placeholder_events()

        def bind_return(self, func):
            self.entry.bind("<Return>", func)

    # ---------- 通用圆角按钮 ----------

    class RoundButton(tk.Canvas):
        """通用圆角实色按钮（Canvas 绘制）：图标 + 文字，支持 hover / 禁用 / 描边。

        高度固定；宽度自适应内容（被 grid sticky="ew" 拉伸时由布局决定实际宽度）。
        圆角外的四角透出 parent_bg，视觉上即「父容器底色上的圆角按钮」。
        """

        def __init__(self, master, text="", icon=None, command=None, height=38,
                     fill="#FFFFFF", hover_fill=None, text_color=C_TEXT,
                     border=None, radius=8, font_size=13, parent_bg=None,
                     pad_x=18, font_weight="normal", **kw):
            bg = parent_bg if parent_bg is not None else fill
            super().__init__(master, height=_px(height), bg=bg,
                             highlightthickness=0, bd=0, cursor="hand2", **kw)
            self._text = text
            self._icon = icon
            self._command = command
            self._h = height
            self._fill = fill
            self._hover_fill = hover_fill or _mix(fill, "#000000", 0.06)
            self._text_color = text_color
            self._border = border
            self._radius = radius
            self._font_size = font_size
            self._font_weight = font_weight
            self._pad_x = _px(pad_x)
            self._enabled = True
            self._hover = False
            self._pressed = False     # 按下态：给点击一个即时视觉反馈
            # loading 态：左弧线转圈 + 文字（原 PrimaryButton 专属，探测/检查更新
            # 这类"点了要等几秒"的次要按钮同样需要，所以提到基类）
            self._loading = False
            self._default_text = text
            self._loading_text = None   # loading 态文案（子类可覆盖，如"正在认证…"）
            self._angle = 0
            self._spinner_job = None
            self.bind("<Configure>", lambda e: self._draw(), add="+")
            self.bind("<Enter>", self._on_enter, add="+")
            self.bind("<Leave>", self._on_leave, add="+")
            self.bind("<Button-1>", self._on_click, add="+")
            self.bind("<ButtonPress-1>", self._on_press, add="+")
            self.bind("<ButtonRelease-1>", self._on_release, add="+")
            # 窗口关闭时如果动画链还挂着，after 回调会在控件已销毁后继续跑
            self.bind("<Destroy>", self._on_destroy, add="+")
            self._draw()

        # ---- 状态 ----

        def set_enabled(self, enabled):
            self._enabled = bool(enabled)
            try:
                self.configure(cursor="hand2" if self._enabled else "arrow")
            except Exception:
                pass
            self._draw()

        def set_text(self, text):
            self._text = text
            self._draw()

        def set_icon(self, icon):
            self._icon = icon
            self._draw()

        def set_style(self, fill=None, text_color=None, border=None, hover_fill=None):
            """分段按钮 / 导航等需要切换外观时使用。"""
            if fill is not None:
                self._fill = fill
                self._hover_fill = hover_fill or _mix(fill, "#000000", 0.06)
            if hover_fill is not None:
                self._hover_fill = hover_fill
            if text_color is not None:
                self._text_color = text_color
            if border is not None:
                self._border = border
            self._draw()

        # ---- loading（转圈）----

        def set_loading(self, loading, text=None):
            """进入/退出 loading 态（转圈 + 可选临时文案）。

            与「禁用」的区别：禁用会淡化成灰色，而 loading 保持原色——点了按钮却
            看到一片灰，用户会以为没点上、于是再点一次。
            """
            self._loading = bool(loading)
            self._enabled = not self._loading
            if loading:
                # loading 态才回落到 _loading_text（如主按钮的"正在认证…"）
                self._text = text or self._loading_text or self._default_text
            else:
                # 退出 loading 必须回到默认文案，否则按钮会永远显示"正在认证…"
                self._text = text or self._default_text
            if loading:
                # 只有非 fill="x"/expand 的按钮才需要按新文案重新申请宽度；
                # fill="x" 的按钮由父容器撑开，改请求宽度只会触发不必要的重布局。
                try:
                    if not self.grid_info():
                        _pack = self.pack_info() if self.winfo_manager() == "pack" else {}
                        if _pack.get("fill") not in ("x", "both") and not _pack.get("expand"):
                            self.configure(width=self._content_width())
                except Exception:
                    pass
                # 只在没有链在跑时启动：连点两次会起两条链，而 _spinner_job 只记得
                # 最后一个 id，set_loading(False) 只能取消一条——剩下那条会以 30fps
                # 永久重绘按钮。
                if self._spinner_job is None:
                    self._animate()
            else:
                self._stop_spinner()
                try:
                    if not self.grid_info():
                        _pack = self.pack_info() if self.winfo_manager() == "pack" else {}
                        if _pack.get("fill") not in ("x", "both") and not _pack.get("expand"):
                            self.configure(width=self._content_width())
                except Exception:
                    pass
            try:
                self.configure(cursor="watch" if self._loading
                               else ("hand2" if self._enabled else "arrow"))
            except Exception:
                pass
            self._draw()

        def _stop_spinner(self):
            if self._spinner_job is not None:
                try:
                    self.after_cancel(self._spinner_job)
                except Exception:
                    pass
                self._spinner_job = None

        def _on_destroy(self, _e=None):
            self._stop_spinner()

        def _animate(self):
            """loading 动画：30fps。

            只改弧线的起始角，不整帧重绘——_draw() 会 delete("all") 后重画渐变底
            （_draw_round_gradient 逐行生成几十个 canvas item），每帧重建的开销远
            大于动画本身，是"点登录后界面发顿"的主要原因。渐变和文字只在进入/退出
            loading 时重画一次即可。
            """
            if not self._loading:
                # 链已失效：必须清掉 id，否则残留值会让下次 set_loading(True)
                # 误判为"已有链在跑"而不再启动，动画从此不动。
                self._spinner_job = None
                return
            self._angle = (self._angle + 10) % 360
            try:
                if self.find_withtag("spinner"):
                    self.itemconfigure("spinner", start=self._angle)
                else:
                    self._draw()      # 弧线还没建出来（宽度未定等），退回整帧重绘
            except Exception:
                self._draw()
            self._spinner_job = self.after(33, self._animate)

        def _draw_spinner_text(self, w, h, color):
            """loading 态内容：左弧线（tag=spinner，动画只改 start）+ 文字。"""
            d = _px(16)
            f = ui_font(self._font_size, self._font_weight)
            text_w = f.measure(self._text)
            x0 = max(_px(10), (w - (text_w + d + _px(10))) / 2.0)
            self.create_arc(x0, h / 2.0 - d / 2.0, x0 + d, h / 2.0 + d / 2.0,
                            start=self._angle, extent=280, style="arc",
                            outline=color, width=_px(2), tags=("spinner",))
            self.create_text(x0 + d + _px(10), h / 2.0, text=self._text,
                             anchor="w", fill=color, font=self._font())

        # ---- 事件 ----

        def _on_enter(self, _e=None):
            self._hover = True
            if self._enabled:
                self._draw()

        def _on_leave(self, _e=None):
            self._hover = False
            # 按下后把鼠标移出按钮再松开，<ButtonRelease> 落不到这里，
            # 不复位就会永久停在按下态
            self._pressed = False
            if self._enabled:
                self._draw()

        def _on_press(self, _e=None):
            if self._enabled and not getattr(self, "_loading", False):
                self._pressed = True
                self._draw()

        def _on_release(self, _e=None):
            if self._pressed:
                self._pressed = False
                self._draw()

        def _on_click(self, _e=None):
            if self._enabled and not self._loading and self._command is not None:
                self._command()
            return "break"

        # ---- 绘制 ----

        def _font(self):
            return (FONT_UI, self._font_size, self._font_weight)

        def _content_width(self):
            f = ui_font(self._font_size, self._font_weight)
            w = 2 * self._pad_x + f.measure(self._text)
            if self._icon is not None:
                w += self._icon.width() + _px(8)
            if self._loading:
                w += _px(26)      # 左侧转圈（16）+ 与文字的间隙（10）
            return max(w, _px(60))

        def _draw(self):
            self.delete("all")
            h = _px(self._h)
            self.configure(height=h)
            w = self.winfo_width()
            if w < _px(8):
                # 尚未布局或自由尺寸：按内容请求宽度
                want = self._content_width()
                if abs(self.winfo_width() - want) > 1:
                    self.configure(width=want)
                return
            if self._loading:
                # loading 态保持原色：淡化成灰会被当成"点了没反应"
                fill = self._fill
                tc = self._text_color
                border = self._border
            elif self._enabled:
                if self._pressed:
                    fill = _mix(self._hover_fill, "#000000", 0.12)   # 按下：再压深一档
                else:
                    fill = self._hover_fill if self._hover else self._fill
                tc = self._text_color
                border = self._border
            else:
                fill = _mix(self._fill, C_BG, 0.45)
                tc = _mix(self._text_color, C_BG, 0.55)
                border = _mix(self._border, C_BG, 0.5) if self._border else None
            kw = {"fill": fill, "outline": border or "", "width": 1}
            round_rect(self, 0, 0, w - 1, h - 1, _px(self._radius), **kw)
            if self._loading:
                self._draw_spinner_text(w, h, tc)
                return
            f = ui_font(self._font_size, self._font_weight)
            total = f.measure(self._text)
            if self._icon is not None:
                total += self._icon.width() + _px(8)
            x = max(_px(6), (w - total) / 2.0)
            if self._icon is not None:
                self.create_image(x, h / 2.0, image=self._icon, anchor="w")
                x += self._icon.width() + _px(8)
            self.create_text(x, h / 2.0, text=self._text, anchor="w",
                             fill=tc, font=self._font())

    class PrimaryButton(RoundButton):
        """主按钮：高 46、圆角 8、垂直渐变 #0059C7→#003B99、白字 15。

        额外支持 loading 态（认证中）：禁用 + 旋转弧线动画，替代前置图标。
        """

        def __init__(self, master, text="", icon=None, command=None, **kw):
            kw.setdefault("height", 46)
            kw.setdefault("radius", 8)
            kw.setdefault("font_size", 15)
            kw.setdefault("text_color", "#FFFFFF")
            kw.setdefault("font_weight", "normal")
            super().__init__(master, text=text, icon=icon, command=command, **kw)
            # 主按钮的 loading 有专属文案（认证中）；次要按钮不设，沿用自身文字
            self._loading_text = "正在认证…"

        def _draw(self):
            self.delete("all")
            h = _px(self._h)
            self.configure(height=h)
            w = self.winfo_width()
            if w < _px(8):
                want = self._content_width()
                if abs(self.winfo_width() - want) > 1:
                    self.configure(width=want)
                return
            if not self._enabled and not self._loading:
                # 禁用（非 loading）：灰色实底
                round_rect(self, 0, 0, w - 1, h - 1, _px(self._radius),
                           fill=_mix(C_PRIMARY, C_BG, 0.5), outline="")
            else:
                top = _mix(C_GRAD_TOP, "#001A45", 0.15) if self._hover else C_GRAD_TOP
                bot = _mix(C_GRAD_BOTTOM, "#001A45", 0.15) if self._hover else C_GRAD_BOTTOM
                _draw_round_gradient(self, 0, 0, w - 1, h - 1, _px(self._radius),
                                     top, bot, "grad")
            f = ui_font(self._font_size, self._font_weight)
            if self._loading:
                # loading：左侧旋转弧线 + 文字（与基类同一套画法，白字）
                self._draw_spinner_text(w, h, "#FFFFFF")
                return
            text_w = f.measure(self._text)
            total = text_w
            if self._icon is not None:
                total += self._icon.width() + _px(10)
            x = max(_px(10), (w - total) / 2.0)
            if self._icon is not None:
                self.create_image(x, h / 2.0, image=self._icon, anchor="w")
                x += self._icon.width() + _px(10)
            self.create_text(x, h / 2.0, text=self._text, anchor="w",
                             fill="#FFFFFF", font=self._font())

    # ---------- 开关 / 复选框 / 状态胶囊 ----------

    class ToggleSwitch(tk.Canvas):
        """40×22 圆角开关：开 #014DB2、关 #D6D9DE，白色滑块 16、内边距 3。"""

        def __init__(self, master, command=None, initial=False, bg=C_CARD):
            super().__init__(master, width=_px(40), height=_px(22), bg=bg,
                             highlightthickness=0, bd=0, cursor="hand2")
            self._on = bool(initial)
            self._command = command
            self._enabled = True
            self._busy = False      # 操作进行中：不可再点，但**不淡化**（见 _draw）
            self.bind("<Button-1>", self._click, add="+")
            self._draw()

        def set_state(self, on):
            self._on = bool(on)
            self._draw()

        def get(self):
            return self._on

        def set_enabled(self, enabled):
            self._enabled = bool(enabled)
            try:
                self.configure(cursor="hand2" if (self._enabled and not self._busy)
                               else "arrow")
            except Exception:
                pass
            self._draw()

        def set_busy(self, busy):
            """操作进行中：忽略点击，但保留目标态配色。

            以前只用 set_enabled(False) 表示"正在处理"，而它顺带把开关画成半透明
            灰色——用户点了开关，看到的还是原来那个灰滑块，像没反应。改用 busy 态：
            颜色照常（能立刻看到目标状态），只是暂时不吃点击。
            """
            self._busy = bool(busy)
            try:
                self.configure(cursor="arrow" if self._busy else "hand2")
            except Exception:
                pass
            self._draw()

        def _click(self, _e=None):
            if self._enabled and not self._busy and self._command is not None:
                self._command()
            return "break"

        def _draw(self):
            self.delete("all")
            w = _px(40)
            h = _px(22)
            r = h / 2.0
            pad = _px(3)
            d = h - 2 * pad
            track = C_PRIMARY if self._on else C_INPUT_BORDER
            if not self._enabled:      # 只有真正禁用才淡化；busy 态保持原色
                track = _mix(track, C_BG, 0.45)
            round_rect(self, 0, 0, w, h, r, fill=track, outline="")
            x = (w - pad - d) if self._on else pad
            self.create_oval(x, pad, x + d, pad + d, fill="#FFFFFF", outline="")

    class CheckBox(tk.Frame):
        """16×16 复选框 + 文字：勾选后 #014DB2 底 + Canvas 画的白色对勾
        （对勾用两段线绘制，不用 ✓ 等字符充当图标）。"""

        def __init__(self, master, text, initial=False, command=None, bg=C_CARD):
            super().__init__(master, bg=bg)
            self._checked = bool(initial)
            self._command = command
            self.box = tk.Canvas(self, width=_px(16), height=_px(16), bg=bg,
                                 highlightthickness=0, bd=0, cursor="hand2")
            self.box.pack(side="left")
            self.lbl = tk.Label(self, text=text, bg=bg, fg=C_TEXT,
                                font=(FONT_UI, 13), cursor="hand2")
            self.lbl.pack(side="left", padx=(_px(8), 0))
            self.box.bind("<Button-1>", self._toggle, add="+")
            self.lbl.bind("<Button-1>", self._toggle, add="+")
            self._draw()

        def get(self):
            return self._checked

        def set(self, value):
            self._checked = bool(value)
            self._draw()

        def set_enabled(self, enabled):
            state = "normal" if enabled else "disabled"
            try:
                self.lbl.configure(state=state)
            except Exception:
                pass

        def _toggle(self, _e=None):
            self._checked = not self._checked
            self._draw()
            if self._command is not None:
                self._command()
            return "break"

        def _draw(self):
            s = _px(16)
            c = self.box
            c.delete("all")
            if self._checked:
                round_rect(c, 0, 0, s - 1, s - 1, _px(4), fill=C_PRIMARY, outline="")
                c.create_line(s * 0.25, s * 0.54, s * 0.42, s * 0.72,
                              fill="#FFFFFF", width=max(2, _px(2)),
                              capstyle="round", joinstyle="round")
                c.create_line(s * 0.42, s * 0.72, s * 0.76, s * 0.30,
                              fill="#FFFFFF", width=max(2, _px(2)),
                              capstyle="round", joinstyle="round")
            else:
                round_rect(c, 0, 0, s - 1, s - 1, _px(4), fill="#FFFFFF",
                           outline=C_INPUT_BORDER, width=1)

    class Pill(tk.Canvas):
        """全圆角状态胶囊（高 28）：小圆点 + 文字，宽度自适应。

        kind: "online"（成功绿底白字）/ "neutral"（浅蓝底蓝字）/ "muted"（灰底灰字）
        """

        STYLES = {
            "online": (C_SUCCESS, "#FFFFFF", "#FFFFFF"),
            "neutral": (C_CARD_BLUE, C_PRIMARY, C_PRIMARY),
            "muted": (C_NEUTRAL, C_TEXT_2, C_TEXT_2),
        }

        def __init__(self, master, kind="neutral", text="", dot=True,
                     parent_bg=C_CARD, font_size=11):
            super().__init__(master, bg=parent_bg, highlightthickness=0, bd=0,
                             height=_px(28))
            self._parent_bg = parent_bg
            self._dot = dot
            self._font_size = font_size
            self.set(kind, text)

        def set(self, kind, text):
            fill, dotc, fg = self.STYLES.get(kind, self.STYLES["muted"])
            self.delete("all")
            h = _px(28)
            self.configure(height=h)
            f = ui_font(self._font_size)
            tw = f.measure(text)
            w = _px(24) + tw + (_px(12) if self._dot else 0)
            self.configure(width=w)
            round_rect(self, 0, 0, w - 1, h - 1, h / 2.0, fill=fill, outline="")
            x = _px(12)
            if self._dot:
                d = _px(6)
                self.create_oval(x, h / 2.0 - d / 2.0, x + d, h / 2.0 + d / 2.0,
                                 fill=dotc, outline="")
                x += d + _px(6)
            self.create_text(x, h / 2.0, text=text, anchor="w", fill=fg,
                             font=(FONT_UI, self._font_size))

    # ---------- 卡片 ----------

    class Card(tk.Canvas):
        """圆角白底卡片：Canvas 画圆角矩形（描边 1px #EAEAEA，圆角 12），
        内容放在内嵌 Frame 里；高度随内容自适应，宽度随布局填充。

        可选 title（左侧 4px 主色竖条 + 16 SemiBold 标题）与 link_text /
        link_command（标题行右侧的小链接，如「查看全部」）。

        stretch=True 时**高度改由父容器决定**（内容仍顶端对齐，多出来的空间留在
        底部）——用于"和旁边那张卡底边齐平"以及"由它吸收窗口多出来的高度"的场景。
        """

        def __init__(self, master, title=None, link_text=None,
                     link_command=None, pad=CARD_PAD,
                     parent_bg=C_BG, fill=C_CARD, radius=CARD_RADIUS,
                     title_fg=C_TEXT, stretch=False):
            super().__init__(master, bg=parent_bg, highlightthickness=0, bd=0)
            self._pad = _px(pad)
            self._radius = _px(radius)
            self._fill = fill
            self._border = C_BORDER
            self._stretch = bool(stretch)
            self.body = tk.Frame(self, bg=fill)
            self._win = None
            if title:
                head = tk.Frame(self.body, bg=fill)
                head.pack(fill="x", pady=(0, _px(10)))
                # 先 pack 右侧链接：packer 按调用顺序分配空间
                if link_text:
                    lbl = tk.Label(head, text=link_text, bg=fill, fg=C_PRIMARY,
                                   font=(FONT_UI, 12), cursor="hand2")
                    lbl.pack(side="right")
                    if link_command is not None:
                        lbl.bind("<Button-1>", lambda e: link_command())
                tk.Frame(head, bg=C_PRIMARY, width=_px(4), height=_px(16)).pack(
                    side="left", padx=(0, _px(8)))
                tk.Label(head, text=title, bg=fill, fg=title_fg,
                         font=(FONT_UI, 16, "bold")).pack(side="left")
            self.body.bind("<Configure>", self._on_body_resize, add="+")
            self.bind("<Configure>", self._on_canvas_resize, add="+")

        def _on_body_resize(self, _e=None):
            # 内容变化 -> 更新画布的**请求**高度（+ 上下内边距 + 四周投影留白）。
            #
            # 比的是 `winfo_reqheight()`（请求值）而不是 `winfo_height()`（实际值）：
            # 卡片被 grid/pack 拉高（为了和旁边那张底边齐平）时实际高度会大于请求值，
            # 拿实际值比就会每次都判定"不一致"再 configure 一遍 -> 和几何管理器来回
            # 打架（Configure 自触发循环）。
            #
            # stretch=True 的卡片（只有运行日志）：高度完全交给父容器，**不设请求值**
            # ——它的行数是按可用高度算的，一旦自己设请求值就会形成"高度→行数→高度"
            # 的循环。
            if not self._stretch:
                h = (self.body.winfo_reqheight() + 2 * self._pad
                     + 2 * self._margin())
                if abs(self.winfo_reqheight() - h) > 1:
                    self.configure(height=h)
            self._redraw()

        def body_avail_height(self):
            """卡片里可放内容的高度（已扣掉投影留白与内边距，未扣标题行）。"""
            return max(0, self.winfo_height() - 2 * self._margin() - 2 * self._pad)

        def _on_canvas_resize(self, _e=None):
            self._redraw()

        def _margin(self):
            """卡片四周为投影预留的留白（设备像素）。"""
            return card_margin()

        def _redraw(self):
            self.delete("card")
            w = self.winfo_width()
            h = self.winfo_height()
            if w < _px(20) or h < _px(20):
                return
            m = self._margin()
            if w - 2 * m < _px(40) or h - 2 * m < _px(20):
                return
            round_rect(self, m, m, w - 1 - m, h - 1 - m, self._radius,
                       fill=self._fill, outline=self._border, width=1,
                       shadow=card_shadow(), tags="card")
            ox = m + self._pad
            if self._win is None:
                self._win = self.create_window(ox, ox, window=self.body,
                                               anchor="nw")
            else:
                self.coords(self._win, ox, ox)
            # 拖拽期间**不要重排内嵌内容**：body 里每个子控件重新测量/换行，才是
            # 卡顿的大头（一次拖拽步里几百次 Tcl 调用都花在这）。让背景先跟上，
            # 内容暂时停在原宽度，松手后 _repaint_all 会按最终尺寸重排一遍。
            if ui_render is not None and ui_render.is_sketch():
                self.tag_raise(self._win)
                return
            # 内容帧的高度取「自然高度」与「卡片可用高度」的较大者：
            # 卡片被父容器拉高（为了和旁边那张底边齐平）时，多出来的空间就交给内容帧，
            # 由帧内部那些 expand=True 的可伸缩空档去平摊——否则卡片里会剩一大片空白。
            # 正常情况下两者相等，没有任何副作用。
            self.itemconfigure(
                self._win, width=max(_px(40), w - 2 * ox),
                height=max(self.body.winfo_reqheight(),
                           self.body_avail_height()))
            # 必须把内容窗口提到背景之上。Tk 的 canvas item 按创建顺序堆叠，而
            # _redraw 每次都用 delete("card") 重建背景（新 id 更大）——不 raise 的话，
            # 第二次重绘之后背景就压在内嵌内容帧上面，整张卡的控件集体「消失」
            # （不报错、不进日志，只是画面上没了）。RoundedEntry 没这个坑是因为
            # 它每轮都重建自己的 window item。
            self.tag_raise(self._win)

    # ---------- 导航项 ----------

    class NavItem(tk.Canvas):
        """侧边导航项：高 38、圆角 8、内边距 12、图标 16、图标文字间距 10。
        选中：#014DB2 底白字白图标；默认：透明底 #374151；hover：中性浅底。"""

        def __init__(self, master, page_id, text, icon_normal, icon_selected,
                 on_click, parent_bg=C_CARD):
            super().__init__(master, height=_px(38), bg=parent_bg,
                             highlightthickness=0, bd=0, cursor="hand2")
            self.page_id = page_id
            self._text = text
            self._ic_n = icon_normal
            self._ic_s = icon_selected
            self._on_click = on_click
            self._sel = False
            self._hover = False
            self.bind("<Button-1>", self._click, add="+")
            self.bind("<Enter>", lambda e: self._set_hover(True), add="+")
            self.bind("<Leave>", lambda e: self._set_hover(False), add="+")
            self.bind("<Configure>", lambda e: self._draw(), add="+")
            self._draw()

        def set_selected(self, selected):
            self._sel = bool(selected)
            self._draw()

        def _set_hover(self, hover):
            self._hover = hover
            self._draw()

        def _click(self, _e=None):
            self._on_click(self.page_id)
            return "break"

        def _draw(self):
            self.delete("all")
            h = _px(38)
            self.configure(height=h)
            w = self.winfo_width()
            if w < _px(20):
                return
            if self._sel:
                round_rect(self, 0, 0, w - 1, h - 1, _px(8), fill=C_PRIMARY, outline="")
            elif self._hover:
                round_rect(self, 0, 0, w - 1, h - 1, _px(8), fill=C_NEUTRAL, outline="")
            fg = "#FFFFFF" if self._sel else C_NAV_TEXT
            icon = self._ic_s if self._sel else self._ic_n
            x = _px(12)
            if icon is not None:
                self.create_image(x, h / 2.0, image=icon, anchor="w")
                x += _px(16) + _px(10)
            self.create_text(x, h / 2.0, text=self._text, anchor="w", fill=fg,
                             font=(FONT_UI, 13))

    # ---------- 分段按钮 ----------

    class SegmentedControl(tk.Frame):
        """运营商分段按钮：高 34、圆角 8、间距 8、等宽；单选。
        选中 #014DB2 底白字，默认白底灰边灰字。"""

        def __init__(self, master, options, command=None, parent_bg=C_CARD):
            super().__init__(master, bg=parent_bg)
            self._seg_opts = list(options)
            self.value = self._seg_opts[0]
            self._command = command
            self.buttons = []
            for i, opt in enumerate(self._seg_opts):
                b = RoundButton(self, text=opt, height=34, radius=8, font_size=13,
                                parent_bg=parent_bg,
                                command=lambda o=opt: self.select(o))
                b.grid(row=0, column=i, sticky="ew",
                       padx=(0, _px(8)) if i < len(self._seg_opts) - 1 else (0, 0))
                self.columnconfigure(i, weight=1, uniform="seg")
                self.buttons.append(b)
            self._apply()

        def select(self, opt):
            """用户点击某段：选中并触发回调。"""
            self.value = opt
            self._apply()
            if self._command is not None:
                self._command(opt)

        def set(self, opt):
            """程序化设置选中项（不触发回调）。"""
            if opt in self._seg_opts:
                self.value = opt
            self._apply()

        def get(self):
            return self.value

        def _apply(self):
            for b, opt in zip(self.buttons, self._seg_opts):
                if opt == self.value:
                    b.set_style(fill=C_PRIMARY, text_color="#FFFFFF",
                                border=None, hover_fill=_mix(C_PRIMARY, "#000000", 0.12))
                else:
                    b.set_style(fill=C_CARD, text_color=C_TEXT_2,
                                border=C_INPUT_BORDER, hover_fill=C_NEUTRAL)

    # ---------- 主窗口 ----------

    class CampusLoginApp:
        """主窗口：左侧导航（6 页）+ 内容区。凭据 -> 立即登录 -> 后台保活 ->
        开机自启 -> 状态区 -> 高级设置，全部功能保留，按设计稿重新组织布局。"""

        def __init__(self, root, smoke=False):
            self.root = root
            self.smoke = smoke
            # 用 core 里的常量：GUI 单实例保护靠 FindWindowW 按**标题**找回已有窗口，
            # 标题字符串必须和这里完全一致，写成两份迟早会漂移。
            root.title(core.GUI_WINDOW_TITLE)
            root.configure(bg=C_BG)
            # 修复"启动先小窗再变大"：Tk 窗口创建后按默认小尺寸（约 200x200）
            # 映射，而最终 geometry() 在布局建完才设——用户会先看到一个小窗口
            # 再"跳"成大窗口。这里先把窗口隐藏，设好最终尺寸、建完布局再显示，
            # 窗口首次出现就是最终大小。smoke 模式不隐藏（自检要截图/读几何）。
            if not smoke:
                root.withdraw()

            # 先加载已保存的配置；损坏则用默认值并稍后弹窗提醒（不闪退）
            self.config_error = None
            try:
                self.cfg = core.load_config()
            except core.ConfigError as e:
                self.cfg = dict(core.DEFAULT_CONFIG)
                self.config_error = str(e)

            # ---- 运行状态（全部只在主线程读写）----
            self.net_state = None        # 当前网络三态（None=未知/检测中）
            self.hero_mode = "off"       # "online" / "auth" / "off"
            self.hero_subtitle = ""
            self.authenticating = False
            self.local_ip = None         # 本机出口 IP（后台线程查询后回填）
            self.daemon_ok = False       # 后台保活是否运行
            self.autostart_on = False    # 计划任务是否已设置
            self._prev_state = None      # 上一次应用的网络状态（掉线提示用）
            self._page_id = "status"     # 当前导航页
            self._banner_job = None
            # 后台线程 -> 主线程 的结果队列：子线程只 put，主线程轮询后回调，
            # 绝不从子线程直接调用 root.after（Python 3.14 会抛 RuntimeError）。
            self._ui_queue = queue.Queue()

            # 窗口尺寸与居中在**构建布局之前**设定（默认 1060×780，最小 960×660）：
            # 配合上面的 withdraw()，窗口首次映射就是最终大小，不会先小后大。
            root.minsize(_px(WIN_MIN_W), _px(WIN_MIN_H))
            root.update_idletasks()
            w = min(_px(WIN_W), max(_px(WIN_MIN_W), root.winfo_screenwidth() - 40))
            h = min(_px(WIN_H), max(_px(WIN_MIN_H), root.winfo_screenheight() - 80))
            x = max(0, (root.winfo_screenwidth() - w) // 2)
            y = max(0, (root.winfo_screenheight() - h) // 2)
            root.geometry("%dx%d+%d+%d" % (w, h, x, y))

            self._setup_style()
            self._load_assets()
            self._build_layout()
            self._load_into_ui()
            self._apply_net_state(None)   # 初始：未知 -> 未连接空态
            self._switch_page("status")
            # 立刻用磁盘上已有的日志填满日志卡。否则要等后台启动检查（网络探测 +
            # PowerShell 查计划任务，可能十几秒）回调到 _done_startup 才刷新，用户
            # 打开窗口先看到的就是一张空白的「运行日志」卡。
            self.refresh_log_tail()

            # 启动主线程 UI 队列轮询（后台操作结果由此回到主线程）
            self._busy = False        # 是否有后台操作在跑（轮询器据此调速）
            self._busy_main = False   # 主流程忙态（登录/保活），见 _set_busy
            self._busy_ops = set()    # 可并存的独立操作名，各撤销自己那一份
            self._idle_ticks = 0      # 空闲轮询计数（用于周期性检查最小化并回收内存）
            self._pending_jobs = 0    # 已提交但结果还没回调到主线程的后台任务数
            self._pending_lock = threading.Lock()
            root.after(30, self._drain_ui_queue)
            # 本机 MAC：PowerShell 查询要 1 秒上下，走后台任务，取到再刷主卡
            self.local_mac = ""
            self._run_bg(self._fetch_local_mac, self._done_local_mac)
            # 让位状态自刷新：只读展示后台保活的让位剩余时间（无手动入口）。
            # 延后启动：此刻页面还没建好，lbl_yield 尚不存在，直接刷会抛异常。
            self._yield_job = None
            root.after(200, self._tick_yield_status)
            # 自动探测网关的「等待中」动画状态（按钮转圈之外，句尾还有跳动的点）
            self._probe_anim_job = None
            self._probe_text = ""
            self._probe_dot = 0

            # 鼠标滚轮：路由到指针下的可滚动画布
            root.bind_all("<MouseWheel>", self._on_mousewheel, add="+")
            # Ctrl+H 切换密码显示/隐藏（键盘可达）
            root.bind("<Control-h>", self._toggle_password, add="+")
            # 拖拽窗口时走草图模式，停下来再出高质量位图（见 _on_root_configure）
            self._resize_job = None
            self._last_size = None
            self._size_snapshot = None
            self._booting = True    # 启动期 <Configure> 不进草图（300ms 后解除）
            root.after(300, lambda: setattr(self, "_booting", False))
            root.bind("<Configure>", self._on_root_configure, add="+")

            # 标题栏染色（Win11 DWM；失败静默降级）
            root.after(50, self._dye_titlebar)

            if self.config_error:
                root.after(400, self._warn_config_broken)
            if not smoke:
                # 启动时后台做一轮完整检查（网络 / 保活进程 / 计划任务 / 出口 IP）
                root.after(100, self.startup_refresh)
                # 布局、尺寸、图标全部就绪后才显示窗口（配合开头的 withdraw()）：
                # 用户看到的第一帧就是 1060×780 的完整界面，没有"小窗变大"过程
                root.deiconify()

        # ---------- ttk 样式 ----------

        def _setup_style(self):
            """ttk 只承担三件事：滚动条、无边框输入框、默认背景。其余视觉全部
            由 Canvas 绘制的自绘控件承担。"""
            style = ttk.Style(self.root)
            try:
                style.theme_use("clam")
            except tk.TclError:
                pass
            self.style = style
            style.configure(".", background=C_BG, foreground=C_TEXT,
                            font=(FONT_UI, 14))
            style.configure("TFrame", background=C_BG)
            style.configure("TLabel", background=C_BG, foreground=C_TEXT,
                            font=(FONT_UI, 14))
            # 无边框输入框（内嵌在 RoundedEntry 的圆角画布里）
            style.configure("Flat.TEntry", fieldbackground=C_CARD,
                            foreground=C_TEXT, borderwidth=0, relief="flat",
                            insertcolor=C_TEXT, padding=0)
            style.map("Flat.TEntry",
                      fieldbackground=[("readonly", C_CARD)],
                      foreground=[("disabled", C_TEXT_3)])
            # 光靠 borderwidth=0 去不掉输入框的边框：clam 主题里那圈边框是
            # layout 中的外层元素画的，属性管不到它。不重设 layout 的话，圆角画布
            # 内会套着一个**直角方块边框**，输入框看起来像"双层框"。
            # 保留 Entry.field（它负责按 fieldbackground 铺底色），只去掉外边线。
            try:
                style.layout("Flat.TEntry", [
                    ("Entry.field", {
                        "sticky": "nswe",
                        "children": [
                            ("Entry.padding", {
                                "sticky": "nswe",
                                "children": [
                                    ("Entry.textarea",
                                     {"sticky": "nswe"})]})]})])
            except tk.TclError:
                pass  # 极老 Tk：退回主题默认 layout
            # background 是 Entry.field 之外那圈（padding 元素）的底色。默认取
            # 全局的 C_BG（浅灰），在白色圆角框里会显出一条灰带，必须改成卡片白。
            # 兜底：万一某些 Tk 版本仍画边框，把边框色也设成底色，视觉上等同没有。
            try:
                style.configure("Flat.TEntry", background=C_CARD,
                                bordercolor=C_CARD, lightcolor=C_CARD,
                                darkcolor=C_CARD)
            except tk.TclError:
                pass
            # 细滚动条：去掉上下箭头，只留扁平滑块（现代桌面应用观感）
            # 箭头来自 clam 主题的默认 layout，必须重设 layout 才能去掉。
            for orient, sticky in (("Vertical", "ns"), ("Horizontal", "ew")):
                try:
                    style.layout("%s.TScrollbar" % orient, [
                        ("%s.Scrollbar.trough" % orient, {
                            "sticky": sticky,
                            "children": [
                                ("%s.Scrollbar.thumb" % orient,
                                 {"expand": "1", "sticky": "nswe"})]})])
                except tk.TclError:
                    pass  # 极老 Tk：保留主题默认布局
            # lightcolor/darkcolor 与滑块同色 -> 消掉 clam 的立体描边，变扁平
            style.configure("TScrollbar", background=C_SCROLL_THUMB,
                            troughcolor=C_BG, bordercolor=C_BG,
                            lightcolor=C_SCROLL_THUMB,
                            darkcolor=C_SCROLL_THUMB,
                            relief="flat", borderwidth=0)
            style.configure("Vertical.TScrollbar", background=C_SCROLL_THUMB,
                            troughcolor=C_BG, bordercolor=C_BG,
                            lightcolor=C_SCROLL_THUMB,
                            darkcolor=C_SCROLL_THUMB, borderwidth=0)
            style.map("TScrollbar",
                      background=[("active", C_SCROLL_THUMB_HOVER)])

        # ---------- 资源加载 ----------

        def _load_assets(self):
            """加载 SVG 图标缓存与窗口/任务栏图标。

            空态插画（design/空态插画-未连接-1024.png）原先给「未连接空态卡」用，
            那张卡随三页化一起删了。插画本身是**白底不透明**的 RGB 图，放到灰蓝
            渐变的主卡上会露出一块白方块，所以没有改挂到主卡上——素材留在
            assets/design 里备用，运行时不加载。
            """
            self._svg_cache = {}
            self._svg_missing = []
            self._apply_window_icon()

        def _svg(self, name, color, size=16):
            """按 (名称, 颜色变体, 尺寸) 加载 assets/icons 下的 SVG 图标并缓存。

            尺寸必须自己缩放：Tk 9 的 PhotoImage 读 SVG 时，-width/-height 是
            **裁剪**而不是缩放——给 24x24 的图传 width=16，拿到的是左上角 16x16
            的一块，图标在界面上就成了"缺一角"的残片。所以先按原始尺寸光栅化，
            再交给 ui_render 用 LANCZOS 缩到目标尺寸（放大走整数 zoom 再降采样）。

            加载失败返回 None（控件自行跳过图标，绝不崩溃）。
            """
            target = _px(size)
            key = (name, color, target)
            if key in self._svg_cache:
                return self._svg_cache[key]
            img = None
            path = asset_path("icons/icon-%s-%s.svg" % (name, color))
            if path:
                try:
                    raw = tk.PhotoImage(file=path)      # 按 SVG 原始尺寸光栅化
                    img = None
                    if ui_render is not None:
                        img = ui_render.rescale(raw, target)
                    if img is None:
                        # 没有 Pillow：只能整数倍缩放，至少别裁掉内容
                        if target != raw.width() and target > 0:
                            k = max(1, raw.width() // target)
                            img = raw.subsample(k, k) if k > 1 else raw
                        else:
                            img = raw
                except Exception:
                    img = None
            if img is None:
                self._svg_missing.append((name, color, size))
            self._svg_cache[key] = img
            return img

        def _apply_window_icon(self):
            """设置窗口/任务栏图标。

            Windows 上 **iconbitmap(.ico) 优先**：它走窗口类图标，任务栏/Alt+Tab
            一定同步；iconphoto(PNG) 在部分 Windows/Tk 组合下标题栏变了、任务栏
            仍旧是 Tk 默认的羽毛图标（用户实测踩过）。PNG 只作 ico 缺失时的后备。
            """
            ico = asset_path("app.ico")
            png = asset_path("app.png")
            if ico:
                try:
                    self.root.iconbitmap(ico)
                    return
                except Exception:
                    pass
            if png:
                try:
                    img = tk.PhotoImage(file=png)
                    self._icon_img = img  # 持有原图引用，防止被回收
                    self.root.iconphoto(True, img)
                except Exception:
                    pass

        def _dye_titlebar(self):
            """把系统标题栏染成品牌蓝（客户决策 #1）。

            Win11：DwmSetWindowAttribute(DWMWA_CAPTION_COLOR=35)；
            COLORREF 是 BGR 顺序：#014DB2（R=0x01,G=0x4D,B=0xB2）-> 0x00B24D01。
            顺带把标题文字染白（DWMWA_TEXT_COLOR=36）。
            Win10 不支持该属性 -> 返回非 0，静默降级为系统默认标题栏。
            """
            try:
                import ctypes
                GA_ROOT = 2
                hwnd = ctypes.windll.user32.GetAncestor(self.root.winfo_id(), GA_ROOT)
                if not hwnd:
                    return
                dwm = ctypes.windll.dwmapi
                caption = ctypes.c_int(0x00B24D01)   # #014DB2 -> BGR
                text = ctypes.c_int(0x00FFFFFF)      # 白色文字
                dwm.DwmSetWindowAttribute(
                    hwnd, 35, ctypes.byref(caption), ctypes.sizeof(caption))
                dwm.DwmSetWindowAttribute(
                    hwnd, 36, ctypes.byref(text), ctypes.sizeof(text))
            except Exception:
                pass  # 非 Windows / 老 DWM：静默降级

        # ---------- 骨架布局 ----------

        def _build_layout(self):
            """骨架：左侧导航栏 + 1px 分隔线 + 内容区。

            客户区顶部**刻意不再画那条 44px 品牌条**：Windows 自身有一条标题栏，
            而且已经被 DWM 染成品牌蓝（_dye_titlebar），客户区再叠一条就是上下两条
            重复的标题——那正是原界面显得杂乱的主因之一。省下的 44px 正好让连接页
            能一屏放下，不用滚动。
            """
            self.main = tk.Frame(self.root, bg=C_BG)
            self.main.pack(fill="both", expand=True)
            self._build_sidebar(self.main)
            # 侧边栏右侧 1px 分隔线
            tk.Frame(self.main, bg=C_SIDEBAR_LINE, width=_px(1)).pack(
                side="left", fill="y")
            # content 放进 host（host 被 pack 管理，content 自身被 place 管理）：
            # 拖拽冻结/解冻只需要 place_configure 改尺寸，**全程不 unmap**。
            # 原来的冻结用 pack_forget+place、解冻用 place_forget+pack，一来一回
            # 内容区整体消失再重建（松手后白屏一帧 + 全部控件重排），就是用户报的
            # 「拉动窗口后闪动」。
            self.content_host = tk.Frame(self.main, bg=C_BG)
            self.content_host.pack(side="left", fill="both", expand=True)
            self.content = tk.Frame(self.content_host, bg=C_BG)
            self.content.place(x=0, y=0, relwidth=1, relheight=1)
            self._build_banner()
            self._build_pages()

        # 原 _build_topbar / _render_topbar（客户区顶部的 44px 渐变条 + 当前页名）
        # 已删除：它和系统标题栏构成上下两条重复标题，正是界面杂乱的主因。品牌
        # 移到侧边栏顶部（_build_brand），实时状态移到侧边栏底部（_render_side_status）。

        def _build_sidebar(self, parent):
            """左侧导航栏：品牌区 + 三页导航 + 底部实时状态块。

            品牌放在这里（而不是客户区顶部的渐变条）是刻意的：系统标题栏已经写着
            「校园网一键登录」，客户区再放一条品牌条就是上下两条重复标题。
            """
            sb = tk.Frame(parent, bg=C_CARD, width=_px(SIDEBAR_W))
            sb.pack(side="left", fill="y")
            sb.pack_propagate(False)
            inner = tk.Frame(sb, bg=C_CARD)
            inner.pack(fill="both", expand=True, padx=_px(14), pady=_px(18))
            self._build_brand(inner)
            # —— 导航（3 项）——
            self._navs = []
            tk.Frame(inner, bg=C_CARD, height=_px(12)).pack(fill="x")
            for page_id, text, icon in PAGES:
                nav = NavItem(inner, page_id, text,
                              self._svg(icon, "dark", 16),
                              self._svg(icon, "white", 16),
                              self._switch_page, parent_bg=C_CARD)
                nav.pack(fill="x", pady=_px(2))
                self._navs.append(nav)
            # —— 弹性空隙 ——
            tk.Frame(inner, bg=C_CARD).pack(fill="both", expand=True)
            # —— 底部实时状态块 ——
            self._side = tk.Canvas(inner, bg=C_CARD, highlightthickness=0, bd=0)
            self._side.pack(fill="x")
            self._side.bind("<Configure>",
                            lambda e: self._render_side_status(), add="+")

        def _build_brand(self, parent):
            """侧边栏顶部品牌区：应用图标 + 名称 + 版本。"""
            row = tk.Frame(parent, bg=C_CARD)
            row.pack(fill="x")
            icon = None
            png = asset_path("app.png")
            if png:
                try:
                    src = tk.PhotoImage(file=png)
                    if ui_render is not None:
                        icon = ui_render.rescale(src, _px(34))
                    if icon is None:                 # 没有 Pillow：整数降采样
                        f = max(1, src.width() // max(1, _px(34)))
                        icon = src.subsample(f, f)
                        self._brand_src = src        # 持引用，防回收
                    else:
                        self._brand_icon = icon
                except Exception:
                    icon = None
            if icon is not None:
                tk.Label(row, image=icon, bg=C_CARD, bd=0).pack(
                    side="left", padx=(0, _px(10)))
            texts = tk.Frame(row, bg=C_CARD)
            texts.pack(side="left", fill="x", expand=True)
            tk.Label(texts, text="校园网一键登录", bg=C_CARD, fg=C_TEXT,
                     font=(FONT_UI, 13, "bold"), anchor="w").pack(anchor="w")
            tk.Label(texts, text="%s · 锐捷 ePortal" % APP_VERSION, bg=C_CARD,
                     fg=C_TEXT_3, font=(FONT_NUM, 10),
                     anchor="w").pack(anchor="w")

        def _render_side_status(self):
            """侧边栏底部实时状态块：状态圆点 + 状态文案 + 出口 IP。

            原来这里显示「在线时长」，并且为了它挂了一个每秒重绘的定时器。在线
            时长对「现在能不能上网」这个核心问题毫无帮助，去掉之后连每秒重绘也
            一并省了。改显示 IP——排错时要看的就是它。
            """
            c = self._side
            c.delete("all")
            w = c.winfo_width()
            h = _px(58)
            c.configure(height=h)
            if w < _px(40):
                return
            round_rect(c, 0, 0, w - 1, h - 1, _px(10),
                       fill=C_CARD_BLUE, outline="")
            if self.net_state == core.ONLINE:
                dot, line1 = C_SUCCESS, "已连接 · %s" % self._service_label()
                line2 = self.local_ip or "正在获取 IP…"
            elif self.authenticating:
                dot, line1, line2 = C_WARN, "认证中…", "正在提交认证"
            elif self.net_state is None:
                # 启动检测还没回来：显示"检测中"而不是"未连接"（同主卡的理由）
                dot, line1, line2 = C_TEXT_3, "检测中…", "正在检测网络状态"
            else:
                dot, line1, line2 = C_TEXT_3, "未连接", "尚未连接校园网"
            c.create_oval(_px(12), _px(16), _px(12) + _px(7), _px(16) + _px(7),
                          fill=dot, outline="")
            c.create_text(_px(26), _px(19), text=line1, anchor="w",
                          fill=C_TEXT, font=(FONT_UI, 12, "bold"))
            c.create_text(_px(12), _px(38), text=line2, anchor="w",
                          fill=C_TEXT_2, font=(FONT_NUM, 11))

        def _build_banner(self):
            """顶部黄色提示条（掉线重连提醒，3 秒自动消失），默认隐藏。"""
            self.banner = tk.Frame(self.content, bg=C_AMBER_LIGHT)
            row = tk.Frame(self.banner, bg=C_AMBER_LIGHT)
            row.pack(anchor="w", padx=_px(24), pady=_px(8))
            dot = tk.Canvas(row, width=_px(8), height=_px(8), bg=C_AMBER_LIGHT,
                            highlightthickness=0, bd=0)
            dot.pack(side="left")
            dot.create_oval(_px(1), _px(1), _px(7), _px(7), fill=C_WARN, outline="")
            tk.Label(row, text="检测到网络波动，正在自动重连…", bg=C_AMBER_LIGHT,
                     fg=C_AMBER_TEXT, font=(FONT_UI, 13)).pack(
                side="left", padx=(_px(8), 0))

        def _build_pages(self):
            self.page_host = tk.Frame(self.content, bg=C_BG)
            self.page_host.pack(fill="both", expand=True)
            self.pages = {}
            self._build_page_status()
            self._build_page_log()
            self._build_page_settings()

        def _make_scroll_page(self):
            """建一个可纵向滚动的页面容器：Canvas + 内嵌 Frame + 自动显隐滚动条。
            返回 (wrap, canvas, inner)。"""
            wrap = tk.Frame(self.page_host, bg=C_BG)
            canvas = tk.Canvas(wrap, bg=C_BG, highlightthickness=0, bd=0)
            vbar = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
            canvas.configure(yscrollcommand=vbar.set)
            inner = tk.Frame(canvas, bg=C_BG)
            win = canvas.create_window((0, 0), window=inner, anchor="nw")
            inner.bind("<Configure>",
                       lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
                       add="+")

            def _on_canvas_cfg(_e):
                canvas.itemconfigure(win, width=canvas.winfo_width())
                self._update_scrollbar(canvas, vbar, inner)

            canvas.bind("<Configure>", _on_canvas_cfg, add="+")
            canvas.pack(side="left", fill="both", expand=True)
            canvas._scroll_self = canvas  # 滚轮路由标记
            return wrap, canvas, inner

        @staticmethod
        def _update_scrollbar(canvas, vbar, inner):
            """内容高于可视区时显示滚动条，否则隐藏。"""
            try:
                need = inner.winfo_reqheight() > canvas.winfo_height() + 1
                shown = bool(vbar.winfo_manager())
                if need and not shown:
                    vbar.pack(side="right", fill="y", before=canvas)
                elif not need and shown:
                    vbar.pack_forget()
            except Exception:
                pass

        # ---------- 缩放流畅性（拖拽窗口时走草图模式） ----------

        def _on_root_configure(self, event):
            """窗口尺寸变化：切草图模式（廉价重绘），并推迟高质量重绘。

            实测数据（SetWindowPos 模拟真实拖边框，1060→970 宽）：
              不处理            ~200 ms/步（最慢 396ms，约 5fps，就是用户报的卡顿）
              只切草图模式       ~59 ms/步（现在的做法，内容实时跟手）
              再冻结内容布局     ~48 ms/步 —— 但代价是拖拽期间内容停在旧尺寸：
                                 缩窄时右侧卡片被窗口边缘直接切掉、拉宽时露一条
                                 空白，松手才"跳"回正确位置（用户报的拖拽闪动/
                                 显示错误就是这个）。为省 ~10ms/步换显示错误
                                 不划算，已删除冻结，内容实时跟手。
            """
            if event.widget is not self.root:
                return
            size = (event.width, event.height)
            if size == self._last_size:
                return
            self._last_size = size
            # 启动阶段的首次 <Configure>（geometry 生效/窗口映射）**不是拖拽**：
            # 这时进草图模式，首屏会先画一遍粗糙占位、130ms 后又整树精修一遍，
            # 窗口刚打开就是"白屏一帧 + 内容闪动"。启动期只记尺寸，直接精修。
            if getattr(self, "_booting", False):
                return
            if ui_render is not None:
                ui_render.set_sketch(True)
                # 拖拽开始时给全树拍尺寸快照：松手后只精修"尺寸真的变了"的控件，
                # 别的控件的高质量位图本来就没失效，不必重画。
                if not getattr(self, "_size_snapshot", None):
                    self._size_snapshot = self._snapshot_sizes()
            # 高度拖拽时日志卡的显隐也要实时跟手（一行都放不下就当场收起），
            # 不然拖矮的过程中会一直挂着一张挤扁的日志卡。内部只是 pack 切换
            # 和几次 winfo 读取，很便宜；行数重排本身有防抖，松手才做。
            self._sync_log_card_visibility()
            if self._resize_job is not None:
                try:
                    self.root.after_cancel(self._resize_job)
                except Exception:
                    pass
            self._resize_job = self.root.after(90, self._end_resize)

        def _end_resize(self):
            """拖拽结束：退出草图模式，并按最终尺寸重绘一遍。

            关键一：**鼠标键还按着就不算"结束"**。光靠 90ms 防抖挡不住慢速拖动——
            实测每步间隔 160ms 时，14 步里 _end_resize 被触发 15 次、精修重绘累计
            花了 2.8 秒，界面就在"粗糙占位"和"精修位图"之间来回翻，肉眼就是明显
            的闪动。改成按住鼠标期间一直维持草图模式，松手那一刻才精修一次。

            关键二：松手后的精修**分批做、只做必要的**。原来是整棵控件树一口气
            event_generate 一遍 <Configure>，所有自绘控件同步重出位图，一次阻塞
            两三百毫秒——窗口卡死一瞬再"全亮"，加上解冻本身，就是用户报的闪动。
            现在先解冻重排（仍是廉价草图，几何立刻正确），再分批把尺寸真变了的
            控件换成高质量位图，期间界面始终响应。

            关键三（曾经有、后来删掉的"冻结内容"）：早期版本拖拽时把内容区钉在
            旧尺寸上以求快，代价是窗口缩窄时右侧卡片（保活/自启卡在最边上，最先
            中招）被窗口边缘直接切掉、拉宽时露一条背景空白，松手才"跳"回正确
            位置——用户报的"拖动时闪动和显示错误"就是它。实测草图模式下内容实时
            跟手只比冻结慢 ~10ms/步（24fps，依然流畅），显示却始终正确，故删除。
            """
            self._resize_job = None
            if left_button_down():
                self._resize_job = self.root.after(120, self._end_resize)
                return
            if ui_render is None:
                return
            try:
                self.root.update_idletasks()
            except Exception:
                pass
            # 运行日志卡显隐在松手这一刻就定下来（不等 120ms 的 refit 防抖），
            # 否则高度压矮后松手，会先看到一小会儿"只剩标题的挤扁卡片"。
            self._sync_log_card_visibility()
            ui_render.set_sketch(False)
            self._repaint_changed()

        def _snapshot_sizes(self):
            """全树控件尺寸快照（拖拽开始前调用）。"""
            snap = {}
            stack = [self.root]
            while stack:
                w = stack.pop()
                try:
                    snap[w] = (w.winfo_width(), w.winfo_height())
                    stack.extend(w.winfo_children())
                except Exception:
                    pass
            return snap

        def _repaint_changed(self):
            """只对「尺寸相对拖拽前真的变了」的控件补发 <Configure>（退出草图后
            按新尺寸重出高质量位图），分批执行避免一次性阻塞。

            没变尺寸的控件位图本来就有效，重画它们纯属浪费——原来全树无差别
            重绘，一次要两三百毫秒；现在通常只剩少数几个，分批后每批几毫秒。
            """
            before = getattr(self, "_size_snapshot", None)
            widgets = []
            stack = [self.root]
            while stack:
                w = stack.pop()
                widgets.append(w)
                try:
                    stack.extend(w.winfo_children())
                except Exception:
                    pass

            def mapped_size(w):
                try:
                    if not w.winfo_ismapped():
                        return None        # 没显示的控件不需要位图
                    return (w.winfo_width(), w.winfo_height())
                except Exception:
                    return None

            if before:
                targets = []
                for w in widgets:
                    now = mapped_size(w)
                    if now is None:
                        continue
                    old = before.get(w)
                    if old is None or old != now:
                        targets.append(w)
            else:
                targets = [w for w in widgets if mapped_size(w) is not None]

            chunk = 10

            def step(i=0):
                for w in targets[i:i + chunk]:
                    try:
                        w.event_generate("<Configure>")
                    except Exception:
                        pass
                if i + chunk < len(targets):
                    # 必须包 lambda：写 after(8, step(i+chunk)) 会把 step 立即求值
                    # 执行，递归在同一帧里同步跑完所有批次（分批重绘彻底失效）；
                    # 更糟的是 after(ms, None) 在 Tcl 里是**同步 sleep**（实测
                    # after(50,None) 阻塞 60ms），等于每批之间再同步卡 8ms。
                    self.root.after(8, lambda i=i + chunk: step(i))
                else:
                    try:
                        self.root.update_idletasks()
                    except Exception:
                        pass

            if targets:
                step()
            self._size_snapshot = None

        def _on_mousewheel(self, event):
            """滚轮路由：指针下有日志文本框就交给它，否则滚最近的可滚动画布。

            **指针在日志文本框上时必须直接返回**：Tk 自带的 Text 类绑定已经会滚它
            （只读 Text 也一样滚），我们再滚一次就变成"一格滚两下"，而且原来这里
            会继续往上找到日志页的外层画布**再滚一次页面**——两处同时动，就是用户
            报的"日志一栏上下滑动异常"。所以这里只做一件事：**别让外层画布也跟着
            滚**，日志框本身由 Tk 负责。
            """
            try:
                w = self.root.winfo_containing(event.x_root, event.y_root)
            except Exception:
                return
            while w is not None:
                if isinstance(w, tk.Text):
                    return                # 交给 Tk 的 Text 类绑定，且不再滚外层页面
                target = getattr(w, "_scroll_self", None)
                if target is not None:
                    delta = int(-event.delta / 120) or (
                        -1 if event.delta > 0 else 1)
                    try:
                        target.yview_scroll(delta, "units")
                    except Exception:
                        pass
                    return
                w = getattr(w, "master", None)

        def _page_header(self, parent, title, subtitle):
            """非连接状态页的页首标题。"""
            head = tk.Frame(parent, bg=C_BG)
            head.pack(fill="x", pady=(0, _px(16)))
            tk.Label(head, text=title, bg=C_BG, fg=C_TEXT,
                     font=(FONT_UI, 20, "bold"), anchor="w").pack(anchor="w")
            tk.Label(head, text=subtitle, bg=C_BG, fg=C_TEXT_2,
                     font=(FONT_UI, 13), anchor="w").pack(anchor="w", pady=(_px(4), 0))

        # ---------- 页面 ①：连接状态 ----------

        def _build_page_status(self):
            """页① 连接 —— 程序的主界面，日常只用这一页。

            布局（用户指定的排版）：
              * 上排两张卡：左「账号信息」/ 右「登录与保活」（登录 + 启停保活 +
                分隔线 + 开机自启，合并成一张），两张卡**底边齐平**；
              * 下排通栏「运行日志」，**只有它随窗口高度伸缩**——上下拉窗口时上排
                高度不变，日志区变高就多显示几行。

            所以这一页**不再是滚动页**：滚动页会把高度让给内容、日志就长不起来了。
            窗口拉到最小时日志仍保留一个最小高度，内容不会被裁。
            """
            page = tk.Frame(self.page_host, bg=C_BG)
            self.pages["status"] = page
            body = tk.Frame(page, bg=C_BG)
            body.pack(fill="both", expand=True,
                      padx=_px(CONTENT_PAD), pady=_px(CONTENT_PAD))
            # —— 顶部状态主卡（固定高度）——
            self.hero = HeroCard(body, self)
            self.hero.pack(fill="x")
            # —— 上排两列：左「账号信息」/ 右「登录与保活（含开机自启）」——
            # 两卡都 stretch：高度交给 grid 决定，于是底边天然齐平（高的那张定行高，
            # 矮的那张底部留白）。
            self.status_grid = tk.Frame(body, bg=C_BG)
            self.status_grid.pack(fill="x")
            self.status_grid.columnconfigure(0, weight=1, uniform="cols")
            self.status_grid.columnconfigure(1, weight=1, uniform="cols")
            self.col_l = tk.Frame(self.status_grid, bg=C_BG)
            self.col_l.grid(row=0, column=0, sticky="nsew")
            self.col_r = tk.Frame(self.status_grid, bg=C_BG)
            self.col_r.grid(row=0, column=1, sticky="nsew")
            self.account_outer = None
            self._build_account_card(self.col_l)
            self._build_login_card(self.col_r)
            # —— 下排通栏运行日志（唯一随窗口伸缩的部分）——
            self._build_log_card(body)

        # 原 _build_empty_state（未连接空态卡：插画 + 引导 + 重新连接按钮）已删除。
        # 它和「账号信息」「登录与保活」两张卡说的是同一件事：未连接时用户要做的
        # 就是填账号点登录，而登录按钮就在旁边。空态插画改为放进主卡的图标位
        # （HeroCard.render 未连接分支），既不丢这张设计素材，也不多占一屏。

        def _autosize_wrap(self, lbl, min_w=60):
            """让 Label 的 wraplength 跟随自己的实际宽度。

            写死 wraplength 的后果：窗口拉窄时文字还按旧宽度折行，超出卡片被裁
            （用户截图里开机自启小节的样子）。绑自身 <Configure> 动态改——宽度由
            父容器分配（fill="x"），wraplength 只影响高度，改完宽度不变，收敛。
            """
            def _on_cfg(e):
                w = max(min_w, e.width - 2)
                try:
                    if int(lbl.cget("wraplength")) != w:
                        lbl.configure(wraplength=w)
                except Exception:
                    pass
            lbl.bind("<Configure>", _on_cfg, add="+")

        def _build_account_card(self, parent):
            """账号信息卡：账号 / 密码 / 运营商。只此一份，挂在连接页左列。

            填满整列（fill+expand）：行高由更高的右卡决定，它被拉着一起变高，于是
            两卡底边齐平（多出来的空间留在卡片底部）。**不能**加 stretch——那会丢掉
            请求高度，画布退回 Tk 默认的 265px，行高不够、内容被裁。

            原实现在「连接状态」页和「账号设置」页各建一份实例、切页时销毁重建
            （还要靠 _sync_account_from_ui 兜住未保存的输入）。账号设置页删掉后
            这类共享/重建逻辑一并消失，输入天然不会丢。
            """
            card = Card(parent, title="账号信息")
            self.account_outer = card
            body = card.body

            def _flex():
                """可伸缩空档：卡片被拉高（为了和右卡底边齐平）时，多出来的高度会
                平摊到这几个空档里，而不是全堆在卡片底部形成一大片空白。没有多余
                高度时它们的自然高度是 0，不影响正常布局。"""
                tk.Frame(body, bg=C_CARD).pack(fill="both", expand=True)

            # 账号（下面几个标签的 pady 都比常规紧一档：省下的高度让给按钮上方
            # 的空档，让蓝按钮在卡片下部居中——用户指定的排版）
            tk.Label(body, text="账号", bg=C_CARD, fg=C_TEXT_2,
                     font=(FONT_UI, 12), anchor="w").pack(anchor="w", pady=(0, _px(3)))
            self.ent_username = RoundedEntry(
                body, placeholder="请输入校园网账号",
                icon=self._svg("user", "gray", 16), parent_bg=C_CARD)
            self.ent_username.pack(fill="x")
            _flex()
            # 密码（眼睛按钮显隐）
            tk.Label(body, text="密码", bg=C_CARD, fg=C_TEXT_2,
                     font=(FONT_UI, 12), anchor="w").pack(
                anchor="w", pady=(_px(4), _px(3)))
            self._password_visible = False
            self.ent_password = RoundedEntry(
                body, placeholder="请输入密码", show="*",
                icon=self._svg("lock", "gray", 16), parent_bg=C_CARD,
                trailing_command=self._toggle_password, trailing_text="显示")
            self.ent_password.set_trailing("显示", self._svg("eye", "gray", 16))
            self.ent_password.pack(fill="x")
            _flex()
            # 运营商分段按钮（配置字段名 cmcc/ctcc/unicom/local 只写进使用说明，
            # 界面上不再单独占一行——紧凑化，给下方的登录按钮腾高度）
            tk.Label(body, text="运营商", bg=C_CARD, fg=C_TEXT_2,
                     font=(FONT_UI, 12), anchor="w").pack(
                anchor="w", pady=(_px(4), _px(3)))
            self.seg_service = SegmentedControl(
                body, SERVICE_ORDER, parent_bg=C_CARD,
                command=self._on_service_changed)
            self.seg_service.pack(fill="x")
            _flex()
            # —— 主按钮 + 结果行（从右卡移过来，用户要求把登录功能整合进账号卡，
            #    右卡只剩保活/自启，上排变矮、日志区变高）——
            # 按钮上方留 ~20px：用户要求蓝按钮"往下一点、在空白里居中"，不是贴着
            # 运营商一行。多出来的高度从标题行/各组标签的 pady 里省回来（见下），
            # 卡片总高不变，日志区不缩水。
            self.btn_login = PrimaryButton(
                body, text="保存并立即登录", icon=self._svg("shield", "white", 16),
                command=self.on_save_login, parent_bg=C_CARD)
            self.btn_login.pack(fill="x", pady=(_px(20), 0))
            # 最近一次操作结果行（成功绿 / 失败红）。高度按需：空文本 1 行（不占
            # 一大块空白），有回执时 _set_result 会设成 2 行——长消息也不会把卡片
            # 撑高（见 _set_result）。
            self.lbl_result = tk.Label(body, text="", bg=C_CARD, fg=C_TEXT_2,
                                       font=(FONT_UI, 12), anchor="nw",
                                       justify="left", height=1,
                                       wraplength=_px(320))
            self.lbl_result.pack(fill="x", anchor="w", pady=(_px(2), 0))
            self._autosize_wrap(self.lbl_result)
            # 回车 = 保存并立即登录；占位焦点事件在布局完成后绑定
            self.ent_username.bind_return(lambda _e: self.on_save_login())
            self.ent_password.bind_return(lambda _e: self.on_save_login())
            # 从配置回填（每次重建都从 self.cfg 读取）
            self.ent_username.set_value(str(self.cfg.get("username", "")))
            self.ent_password.set_value(str(self.cfg.get("password", "")))
            self.ent_username.bind_placeholder_events()
            self.ent_password.bind_placeholder_events()
            self.seg_service.set(
                SERVICE_LABELS.get(self.cfg.get("service", "cmcc"), "移动"))
            # 填满左列（行高由右卡决定，见 _build_account_card 的说明）
            card.pack(fill="both", expand=True)

        def _build_login_card(self, parent):
            """保活与开机自启卡：启停保活 + 状态行 + 开机自启小节。

            主按钮「保存并立即登录」和结果行已移进左边账号卡（用户要求把登录功能
            整合到左边，让上排变矮、给日志腾高度），这张卡只管后台运行的两件事。

            开机自启并进这张卡（用户指定的排版）：上段是「启停保活」，下段用
            一条分隔线隔出「开机自启」，两段控制的东西互相独立。

            填满整列（fill+expand）：与左卡底边齐平（多出的高度摊进自启节上方的
            弹性空档）。**不能**加 stretch（同账号卡的说明）。
            """
            card = Card(parent, title="保活与开机自启")
            self.login_outer = card
            body = card.body
            # 启动 / 停止后台保活
            row = tk.Frame(body, bg=C_CARD)
            row.pack(fill="x")
            row.columnconfigure(0, weight=1, uniform="pair")
            row.columnconfigure(1, weight=1, uniform="pair")
            self.btn_keep_start = RoundButton(
                row, text="启动后台保活", height=38, radius=8, font_size=13,
                fill=C_BLUE_LIGHT, hover_fill="#DCE9FC", border=C_BLUE_LIGHT_BORDER,
                text_color=C_PRIMARY, parent_bg=C_CARD,
                command=self.on_start_keepalive)
            self.btn_keep_start.grid(row=0, column=0, sticky="ew", padx=(0, _px(8)))
            self.btn_keep_stop = RoundButton(
                row, text="停止后台保活", height=38, radius=8, font_size=13,
                fill=C_RED_LIGHT, hover_fill="#FBE3E3", border=C_RED_LIGHT_BORDER,
                text_color=C_DANGER, parent_bg=C_CARD,
                command=self.on_stop_keepalive)
            self.btn_keep_stop.grid(row=0, column=1, sticky="ew")
            # 保活状态行（圆点 + 文字）
            keep_row = tk.Frame(body, bg=C_CARD)
            keep_row.pack(fill="x", pady=(_px(8), 0))
            self.keep_dot = tk.Canvas(keep_row, width=_px(8), height=_px(8),
                                      bg=C_CARD, highlightthickness=0, bd=0)
            self.keep_dot.pack(side="left")
            # 保活状态行：**预留两行**（height=2）。
            # 文案在"未运行"时是一行、"运行中（PID xxxx）· 关闭窗口后仍在后台运行"
            # 时是两行。不预留的话，daemon 一启动这行就变高，而卡片高度只在特定
            # 时机重算，来不及跟上就把下面的控件裁掉（实际发生过：底部按钮被卡片
            # 下边缘切掉半截）。预留固定行数后，卡片高度从第一次布局起就是对的。
            self.lbl_keep = tk.Label(keep_row, text="后台保活：检查中…", bg=C_CARD,
                                     fg=C_TEXT_2, font=(FONT_UI, 12), anchor="nw",
                                     justify="left", height=2,
                                     wraplength=_px(320))
            self.lbl_keep.pack(side="left", fill="x", expand=True,
                               padx=(_px(8), 0))
            self._autosize_wrap(self.lbl_keep)
            # —— 自动让位状态（只读展示，无按钮）——
            # 后台保活确证「在线的是别的设备」时会主动让位，这里只把状态显示出来。
            # **不提供手动操作入口**：让位由判据自动触发、到期自动恢复，手动开关
            # 反而容易被忘掉，让电脑长期离线。
            self.lbl_yield = tk.Label(body, text="", bg=C_CARD, fg=C_TEXT_2,
                                      font=(FONT_UI, 11), anchor="nw",
                                      justify="left", height=2,
                                      wraplength=_px(320))
            self._autosize_wrap(self.lbl_yield)
            # 这里原来还有一个「断开校园网连接」按钮，已删除：它做不到字面意思上的
            # 断开（不向认证服务器注销，见客户决策 #3），做的事其实就是
            # stop_daemon + 重新检测一次网络状态 —— 跟上面那个「停止后台保活」
            # 多出来的高度优先摊在这里，让自启小节贴近卡片底部而不是中间留空
            self.keep_flex = tk.Frame(body, bg=C_CARD)
            self.keep_flex.pack(fill="both", expand=True)
            # —— 下段：开机自启（独立的一节，与上面的保活互不联动）——
            self._build_autostart_section(body)
            # 填满右列（行高由它决定，左卡跟着拉高 -> 两卡底边齐平）
            card.pack(fill="both", expand=True)

        def _build_autostart_section(self, parent):
            """「开机自启」小节 —— 放在「登录与保活」卡里，用一条分隔线与上面的
            登录/启停保活隔开。

            它**只做开机自启这一件事**，与上面的保活互相独立：
              * 开机自启 = 登录 Windows / 联网时把这个程序的后台服务拉起来（无窗口）
              * 后台保活 = 现在立刻让后台服务跑起来 / 停下来
            勾上自启**不会**改变保活现在的运行状态，点保活也**不会**动自启的计划任务。
            中间那条分隔线就是为了让"这是两件事"一眼看得出来；自检里也有断言守着
            （`autostart_toggle_does_not_touch_keepalive`，以及"这一节里不许出现
            保活字样"）。注意 pack 顺序：必须先 pack 右侧的 pill，再 pack 左侧
            expand=True 的文案区，否则 pill 会被挤到容器外被裁掉。
            """
            tk.Frame(parent, bg=C_BORDER, height=1).pack(fill="x", pady=_px(8))
            sec = tk.Frame(parent, bg=C_CARD)
            sec.pack(fill="x")
            self.autostart_outer = sec      # 自检查"这一节"文案用
            # 第一行：开关 + 短标题 + 右侧状态 pill。
            # 标题刻意短（就四个字）：toggle 和 pill 已经占掉了小半行，标题写长了
            # 会被挤到裁掉半句（实测"登录 Windows 时自动启…"）。详细说明放到下面
            # 单独一行，那样能用满可用宽度。
            row = tk.Frame(sec, bg=C_CARD)
            row.pack(fill="x")
            # 初始态是"检测中"（计划任务查询要等后台回来才有结果），别先喊"未开启"
            self.pill_auto = Pill(row, parent_bg=C_CARD,
                                  kind="muted", text="检测中")
            self.pill_auto.pack(side="right", padx=(_px(8), 0))
            self.toggle_auto = ToggleSwitch(row, command=self.on_toggle_autostart,
                                            initial=self.autostart_on, bg=C_CARD)
            self.toggle_auto.pack(side="left", pady=_px(4))
            tk.Label(row, text="开机自启", bg=C_CARD, fg=C_TEXT,
                     font=(FONT_UI, 14, "bold"), anchor="w").pack(
                side="left", padx=(_px(12), 0))
            _lbl_desc = tk.Label(
                sec, text="下次登录 Windows 时，自动在后台联网并保持在线",
                bg=C_CARD, fg=C_TEXT_2, font=(FONT_UI, 12),
                anchor="w", wraplength=_px(300), justify="left")
            _lbl_desc.pack(fill="x", anchor="w", pady=(_px(4), 0))
            self._autosize_wrap(_lbl_desc)
            # 功能注释（用户要求加、又要求缩短）：一句话说清"这是预约、不是立刻
            # 运行"。不含"保活"字样（自检断言：这一节文案与保活脱钩）。
            _lbl_hint = tk.Label(
                sec, text="提示：打开开关后不会立刻生效，下次开机登录时才自动启动",
                bg=C_CARD, fg=C_TEXT_3, font=(FONT_UI, 11),
                anchor="w", wraplength=_px(300), justify="left")
            _lbl_hint.pack(fill="x", anchor="w", pady=(_px(2), 0))
            self._autosize_wrap(_lbl_hint)
            # 自启的成败回执（预留 1 行，长文案不会把卡片撑高，见 Card/坑六）
            self.lbl_result_boot = tk.Label(
                sec, text="", bg=C_CARD, fg=C_TEXT_2, font=(FONT_UI, 12),
                anchor="nw", justify="left", height=1, wraplength=_px(300))
            self.lbl_result_boot.pack(fill="x", anchor="w", pady=(_px(4), 0))
            self._autosize_wrap(self.lbl_result_boot)

        def _build_log_card(self, parent):
            """运行日志卡（连接页右列）：彩色圆点日志行 + 标题右侧「查看全部」。

            行内容按可用宽度截断（列比原来的通栏窄得多），完整日志在「日志」页。

            stretch=True + 填满剩余空间：**它是整页唯一随窗口高度伸缩的部分**——
            上下拉窗口时上排高度不变，只有它变高变矮；变高就多显示几行日志
            （行数按可用高度算，见 refresh_log_tail）。
            """
            card = Card(parent, title="运行日志", link_text="查看全部",
                        link_command=lambda: self._switch_page("log"),
                        stretch=True)
            self.log_outer = card
            self.log_rows = tk.Frame(card.body, bg=C_CARD)
            self.log_rows.pack(fill="x")
            # 高度变了就按新高度重算能显示几行（防抖，避免拖拽时每步都重读日志）
            card.bind("<Configure>", lambda e: self._schedule_log_refit(), add="+")
            card.pack(fill="both", expand=True)

        def _schedule_log_refit(self):
            """日志卡尺寸变化 -> 稍后按新高度重算行数（防抖 120ms）。"""
            if getattr(self, "_log_fit_job", None) is not None:
                try:
                    self.root.after_cancel(self._log_fit_job)
                except Exception:
                    pass
            self._log_fit_job = self.root.after(120, self._refit_log_rows)

        def _refit_log_rows(self):
            self._log_fit_job = None
            if self.pages.get("status") is not None and self._page_id == "status":
                self.refresh_log_tail()

        def _build_maint_card(self, parent):
            """维护卡：版本信息 + 打开日志目录。"""
            card = Card(parent, title="维护")
            self.maint_outer = card
            row = tk.Frame(card.body, bg=C_CARD)
            row.pack(fill="x")
            row.columnconfigure(0, weight=1, uniform="m")
            row.columnconfigure(1, weight=1, uniform="m")
            self.btn_check_update = RoundButton(
                row, text="检查更新", height=38, radius=8, font_size=13,
                fill=C_NEUTRAL, hover_fill=C_NEUTRAL_HOVER,
                border=C_INPUT_BORDER, text_color=C_TEXT,
                parent_bg=C_CARD, command=self.on_check_update)
            self.btn_check_update.grid(row=0, column=0, sticky="ew", padx=(0, _px(8)))
            self.btn_open_log = RoundButton(
                row, text="打开日志目录", height=38, radius=8, font_size=13,
                fill=C_NEUTRAL, hover_fill=C_NEUTRAL_HOVER,
                border=C_INPUT_BORDER, text_color=C_TEXT,
                parent_bg=C_CARD, command=self.on_open_logdir)
            self.btn_open_log.grid(row=0, column=1, sticky="ew")
            card.pack(fill="x", pady=card_gap())

        # ---------- 页面 ②：设置 ----------

        def _build_page_settings(self):
            """页② 设置：启动行为 / 保活参数 / 高级参数 / 维护 / 关于。

            集中放「改一次就不用再管」的低频设置，所以这一页允许滚动——只有连接页
            要求一屏放下。
            """
            wrap, _canvas, inner = self._make_scroll_page()
            self.pages["settings"] = wrap
            body = tk.Frame(inner, bg=C_BG)
            body.pack(fill="both", expand=True,
                      padx=_px(CONTENT_PAD), pady=_px(CONTENT_PAD))
            self._page_header(body, "设置", "保活参数、维护与版本信息")
            # —— 保活参数 ——
            # 注释里写明合法范围：超出范围会被静默夹到边界（或回落默认值），不写
            # 出来的话用户填了不生效还以为程序坏了。
            card = Card(body, title="保活参数")
            self._build_param_rows(card.body, (
                ("检测间隔（秒）", "var_interval",
                 str(core.DEFAULT_CONFIG["check_interval_seconds"]),
                 "掉线或未认证时的检测与重试基础间隔（5~86400）",
                 "check_interval_seconds"),
                ("在线检测间隔（秒）", "var_online_interval",
                 str(core.DEFAULT_CONFIG["online_interval_seconds"]),
                 "网络正常时后台巡检的间隔，不得小于检测间隔（5~86400）",
                 "online_interval_seconds"),
                ("内容校验间隔（秒）", "var_content_interval",
                 str(core.DEFAULT_CONFIG["content_check_interval"]),
                 "防「假联网」的真实联网校验间隔（1~86400）",
                 "content_check_interval"),
                ("心跳日志间隔（分钟）", "var_heartbeat",
                 str(core.DEFAULT_CONFIG["log_heartbeat_minutes"]),
                 "无异常时写一条心跳日志的间隔（1~1440）",
                 "log_heartbeat_minutes"),
                ("退避上限（秒）", "var_backoff",
                 str(core.DEFAULT_CONFIG["max_backoff_seconds"]),
                 "连续失败时重试间隔的最大值（10~86400）",
                 "max_backoff_seconds"),
            ))
            # 参数没有"改完即生效"的魔法：不点保存就关窗口，改的东西只在界面上，
            # 下次打开还是旧值——这是"设置页参数不属实"的根因，所以必须有显式入口。
            _hint = tk.Label(
                card.body,
                text="改完后点「保存参数」才会写入配置文件。后台保活运行中无需重启，"
                     "它每轮都会重读配置，约一个检测间隔内自动生效。",
                bg=C_CARD, fg=C_TEXT_3, font=(FONT_UI, 11), anchor="w",
                justify="left", wraplength=_px(600))
            _hint.pack(fill="x", anchor="w", pady=(_px(8), _px(6)))
            self._autosize_wrap(_hint)
            self.btn_save_params = RoundButton(
                card.body, text="保存参数", height=36, radius=8, font_size=13,
                fill=C_NEUTRAL, hover_fill=C_NEUTRAL_HOVER,
                border=C_INPUT_BORDER, text_color=C_NAV_TEXT,
                parent_bg=C_CARD, command=self.on_save_params)
            self.btn_save_params.pack(anchor="w")
            # 回执写在按钮下方：保存后会把输入框刷新成**真正生效的值**，越界被夹的
            # 情况也在这里说清楚，界面上看到的永远等于实际在用的。
            self.lbl_params_status = tk.Label(
                card.body, text="", bg=C_CARD, fg=C_TEXT_3, font=(FONT_UI, 11),
                anchor="w", justify="left", wraplength=_px(600))
            self.lbl_params_status.pack(fill="x", anchor="w", pady=(_px(6), 0))
            self._autosize_wrap(self.lbl_params_status)
            card.pack(fill="x", pady=card_gap())
            # —— 高级参数卡（认证网关，保留原有能力）——
            adv = Card(body, title="高级参数")
            self._build_param_rows(adv.body, (
                ("认证网关 portal_host", "var_portal",
                 core.DEFAULT_CONFIG["portal_host"],
                 "校园网认证服务器地址（换学校必改，或点「自动探测」）",
                 "portal_host"),
                ("页面 ID customPageId", "var_page_id",
                 core.DEFAULT_CONFIG["customPageId"],
                 "由网关下发，每次登录自动更新（手动改仅作兜底）",
                 "customPageId"),
                ("NAS IP nasIp", "var_nas_ip", core.DEFAULT_CONFIG["nasIp"],
                 "由网关下发，每次登录自动更新（手动改仅作兜底）",
                 "nasIp"),
            ))
            # 每所学校的认证网关地址都不一样，配置里那个默认值只对作者所在学校有效。
            # 与其让人去问网管要 IP，不如点一下让程序自己找。
            # 说明文字必须 autosize：写死 wraplength=600 时窗口拉窄，文字仍按 600px
            # 折行，超出卡片的部分直接被裁掉（见 _autosize_wrap）。
            _adv_hint = tk.Label(
                adv.body,
                text="换了一所学校后网关地址就不同：点「自动探测」让程序自己找"
                     "（需处于连着校园网但还没认证的状态）。",
                bg=C_CARD, fg=C_TEXT_3, font=(FONT_UI, 11), anchor="w",
                justify="left", wraplength=_px(600))
            _adv_hint.pack(fill="x", anchor="w", pady=(_px(8), _px(6)))
            self._autosize_wrap(_adv_hint)
            self.btn_probe_portal = RoundButton(
                adv.body, text="自动探测网关", height=34, radius=8, font_size=12,
                fill=C_NEUTRAL, hover_fill=C_NEUTRAL_HOVER,
                border=C_INPUT_BORDER, text_color=C_TEXT,
                parent_bg=C_CARD, command=self.on_probe_portal)
            self.btn_probe_portal.pack(anchor="w")
            # 探测回执写在按钮下方，不再弹系统提示框：那个框里的长文案换行很难看，
            # 而且 messagebox 是嵌套事件循环——保活正忙时会叠出第二个框，页面上也
            # 看不出"到底开始探测没有"。原地显示 + 按钮转圈，两件事一起解决。
            self.lbl_probe_status = tk.Label(
                adv.body, text="", bg=C_CARD, fg=C_TEXT_3, font=(FONT_UI, 11),
                anchor="w", justify="left", wraplength=_px(600))
            self.lbl_probe_status.pack(fill="x", anchor="w", pady=(_px(6), 0))
            self._autosize_wrap(self.lbl_probe_status)
            adv.pack(fill="x", pady=card_gap())
            # —— 维护 ——
            self._build_maint_card(body)
            # —— 关于 ——
            self._build_about_card(body)

        def _build_about_card(self, parent):
            """关于卡（设置页底部）：版本 + 一句话说明 + 转发前的安全提醒。

            原来「关于」是一个独立导航页，内容只有这几行——为它占一个导航项不值，
            移到这里既保留了信息，也让导航只剩三页。
            """
            card = Card(parent, title="关于")
            tk.Label(card.body, text="校园网一键登录 %s" % APP_VERSION, bg=C_CARD,
                     fg=C_TEXT, font=(FONT_UI, 13, "bold"),
                     anchor="w").pack(anchor="w")
            tk.Label(card.body,
                     text="锐捷 ePortal 自动认证；掉线自动重连；后台保活进程独立运行，"
                          "关掉窗口不影响联网。",
                     bg=C_CARD, fg=C_TEXT_2, font=(FONT_UI, 12), anchor="w",
                     justify="left", wraplength=_px(640)).pack(
                anchor="w", pady=(_px(6), 0))
            tk.Label(card.body,
                     text="提示：转发本文件夹给他人前，请先删除 config.json"
                          "（其中保存了你的密码）。",
                     bg=C_CARD, fg=C_TEXT_3, font=(FONT_UI, 11), anchor="w",
                     justify="left", wraplength=_px(640)).pack(
                anchor="w", pady=(_px(10), 0))
            card.pack(fill="x", pady=card_gap())

        def _build_param_rows(self, parent, rows):
            """参数行：左侧「标签 + 灰字注释」，右侧定宽圆角输入框。

            原实现是标签一行、整宽输入框一行、注释再一行，每行约 76px；改成左右
            分栏后每行约 52px——设置页的八个参数因此能在一屏里看全，也更像常见的
            设置列表。

            rows 每项 = (标签, StringVar 名, 默认值, 注释, config.json 的键名)。
            键名会被记进 self._param_meta，保存参数后用它把输入框回写成**真正生效
            的值**（越界被夹的情况也能当场看见）。
            """
            grid = tk.Frame(parent, bg=C_CARD)
            grid.pack(fill="x")
            grid.columnconfigure(0, weight=1)
            meta = getattr(self, "_param_meta", None)
            if meta is None:
                meta = self._param_meta = []
            for i, (label, attr, default, comment, key) in enumerate(rows):
                left = tk.Frame(grid, bg=C_CARD)
                left.grid(row=i, column=0, sticky="w", pady=_px(7))
                tk.Label(left, text=label, bg=C_CARD, fg=C_TEXT,
                         font=(FONT_UI, 13), anchor="w").pack(anchor="w")
                tk.Label(left, text=comment, bg=C_CARD, fg=C_TEXT_3,
                         font=(FONT_UI, 11), anchor="w").pack(anchor="w")
                var = tk.StringVar(value=default)
                setattr(self, attr, var)
                meta.append((label, attr, key))
                entry = RoundedEntry(grid, textvariable=var, parent_bg=C_CARD)
                entry.configure(width=_px(180))
                entry.grid(row=i, column=1, sticky="e", pady=_px(7),
                           padx=(_px(16), 0))

        # ---------- 页面 ③：运行日志 ----------

        def _build_page_log(self):
            wrap, _canvas, inner = self._make_scroll_page()
            self.pages["log"] = wrap
            body = tk.Frame(inner, bg=C_BG)
            body.pack(fill="both", expand=True, padx=_px(CONTENT_PAD), pady=_px(CONTENT_PAD))
            self._page_header(body, "日志", "login.log 最近 %d 行（自动滚动到最新）"
                              % LOG_PAGE_LINES)
            card = Card(body, title="日志内容")
            self.log_page_card = card
            btn_row = tk.Frame(card.body, bg=C_CARD)
            btn_row.pack(fill="x", pady=(0, _px(10)))
            btn_row.columnconfigure(0, weight=1, uniform="lg")
            btn_row.columnconfigure(1, weight=1, uniform="lg")
            self.btn_log_refresh = RoundButton(
                btn_row, text="刷新", icon=self._svg("refresh", "dark", 14),
                height=36, radius=8, font_size=13, fill=C_NEUTRAL,
                hover_fill=C_NEUTRAL_HOVER, border=C_INPUT_BORDER,
                text_color=C_NAV_TEXT,
                parent_bg=C_CARD, command=self.refresh_log_tail)
            self.btn_log_refresh.grid(row=0, column=0, sticky="ew", padx=(0, _px(8)))
            self.btn_log_open = RoundButton(
                btn_row, text="打开日志目录", icon=self._svg("clock", "dark", 14),
                height=36, radius=8, font_size=13, fill=C_NEUTRAL,
                hover_fill=C_NEUTRAL_HOVER, border=C_INPUT_BORDER,
                text_color=C_NAV_TEXT,
                parent_bg=C_CARD, command=self.on_open_logdir)
            self.btn_log_open.grid(row=0, column=1, sticky="ew")
            # 完整日志文本框（等宽字体，按级别着色）+ 竖直滚动条。
            # 滚动条是必须的：文本框是只读的（disabled Text 收不到 Tk 自带的滚轮
            # 绑定），没有滚动条就只能靠 _on_mousewheel 接管的那条路，能见度太差。
            text_wrap = tk.Frame(card.body, bg=C_CARD)
            text_wrap.pack(fill="both", expand=True)
            self.log_text = tk.Text(text_wrap, height=24, wrap="word",
                                    font=(FONT_MONO, 10), background=C_LOG_BG,
                                    foreground=C_TEXT, relief="flat", bd=0,
                                    spacing1=_px(2), spacing2=_px(5),
                                    spacing3=_px(3),
                                    highlightthickness=1,
                                    highlightbackground=C_BORDER,
                                    highlightcolor=C_PRIMARY, state="disabled")
            # spacing1/2/3 必须给：Consolas 10pt 的行距只有 15px，而中文回退字体
            # （雅黑）的字形高约 18~19px——不加间距相邻两行的文字互相叠印，滚动时
            # 看起来就是"向下滑动显示异常"（重影/叠行）。spacing1+3 与 spacing2
            # 都补成 20px 档，逻辑行换行处和普通行间距保持一致。
            # 先 pack 固定宽的滚动条，再 pack 会 expand 的文本框（pack 是按调用
            # 顺序分配空间的，反了会把滚动条挤到容器外裁掉）。
            # **必须挂在 self 上**：tkinter 控件在 Python 侧没有引用时会被 GC，
            # 底层 Tk 控件连同它注册的 Tcl 命令一起销毁，文本框的 yscrollcommand
            # 就指向一个死命令（实测表现为读到一串内存地址乱码）。
            self.log_vbar = ttk.Scrollbar(text_wrap, orient="vertical",
                                          command=self.log_text.yview)
            self.log_text.configure(yscrollcommand=self.log_vbar.set)
            self.log_vbar.pack(side="right", fill="y")
            self.log_text.pack(side="left", fill="both", expand=True)
            self.log_text.tag_configure("time", foreground=C_TEXT_3)
            self.log_text.tag_configure("info", foreground=C_TEXT)
            self.log_text.tag_configure("warn", foreground=C_WARN_TEXT)
            self.log_text.tag_configure("error", foreground=C_DANGER_TEXT)
            card.pack(fill="x", pady=card_gap())

        # 原 _build_page_about（独立「关于」页）已删除：内容只有版本号和两行说明，
        # 为它占一个导航项不值。信息移进设置页底部的 _build_about_card。

        # ---------- 页面切换 ----------

        def _switch_page(self, page_id):
            """切换导航页：更新导航选中态、显隐页面。"""
            self._page_id = page_id
            for nav in self._navs:
                nav.set_selected(nav.page_id == page_id)
            for pid, wrap in self.pages.items():
                if pid != page_id:
                    wrap.pack_forget()
            self.pages[page_id].pack(fill="both", expand=True)
            try:
                self.root.update_idletasks()
            except Exception:
                pass
            # 切回连接页时立刻定一次日志卡显隐：在别的页上拉矮过窗口的话，
            # refit 被页码判断挡住没跑，卡片显隐还停在旧状态。
            if page_id == "status":
                self._sync_log_card_visibility()
            elif page_id == "log":
                # 日志页整屏 Text 只在看着它的时候才重写（见 refresh_log_tail），
                # 所以切进来这一下必须补一次，否则看到的是上一次的旧内容。
                self._refresh_log_page()

        def _sync_account_from_ui(self):
            """把账号卡当前输入同步进 self.cfg（保存/重启后台任务前调用）。"""
            card = getattr(self, "account_outer", None)
            if card is not None and card.winfo_exists():
                self.cfg["username"] = self.ent_username.value().strip()
                self.cfg["password"] = self.ent_password.value()
                self.cfg["service"] = LABEL_TO_SERVICE.get(self.seg_service.get(),
                                                           "cmcc")

        # 原 _mount_account_card / _mount_autostart_card（切页时销毁重建共享卡）
        # 已删除：账号卡只挂连接页、启动卡只挂设置页，各自只有一份实例，不再需要
        # 「跨页搬运 + 重建前同步输入」这套机制，输入丢失的风险也随之消失。

        # ---------- 配置读写 ----------

        def _load_into_ui(self):
            """把 self.cfg 回填到各输入框（账号卡由 _build_account_card 自回填）。"""
            cfg = self.cfg
            self.var_portal.set(str(cfg.get("portal_host", "")) or core.DEFAULT_CONFIG["portal_host"])
            self.var_page_id.set(str(cfg.get("customPageId", "")) or core.DEFAULT_CONFIG["customPageId"])
            self.var_nas_ip.set(str(cfg.get("nasIp", "")) or core.DEFAULT_CONFIG["nasIp"])
            self.var_interval.set(str(cfg.get("check_interval_seconds", 30)))
            self.var_online_interval.set(str(cfg.get("online_interval_seconds", 60)))
            self.var_content_interval.set(str(cfg.get("content_check_interval", 300)))
            self.var_heartbeat.set(str(cfg.get("log_heartbeat_minutes", 30)))
            self.var_backoff.set(str(cfg.get("max_backoff_seconds", 300)))

        def _collect_config(self):
            """从界面收集配置（非法输入安全回落默认值；占位文字绝不进配置）。"""
            cfg = dict(self.cfg or core.DEFAULT_CONFIG)
            cfg["username"] = self.ent_username.value().strip()
            cfg["password"] = self.ent_password.value()  # 密码原样保留，不去空格
            cfg["service"] = LABEL_TO_SERVICE.get(self.seg_service.get(), "cmcc")
            cfg["portal_host"] = self.var_portal.get().strip() or core.DEFAULT_CONFIG["portal_host"]
            cfg["customPageId"] = self.var_page_id.get().strip() or core.DEFAULT_CONFIG["customPageId"]
            cfg["nasIp"] = self.var_nas_ip.get().strip() or core.DEFAULT_CONFIG["nasIp"]
            cfg["check_interval_seconds"] = _to_int(self.var_interval.get(), 30, 5, 86400)
            cfg["online_interval_seconds"] = _to_int(
                self.var_online_interval.get(), 60, 5, 86400)
            cfg["content_check_interval"] = _to_int(
                self.var_content_interval.get(), 300, 1, 86400)
            cfg["log_heartbeat_minutes"] = _to_int(self.var_heartbeat.get(), 30, 1, 1440)
            cfg["max_backoff_seconds"] = _to_int(self.var_backoff.get(), 300, 10, 86400)
            # 在线间隔不得小于基础间隔（与 login_core._normalize_numbers 一致）
            if cfg["online_interval_seconds"] < cfg["check_interval_seconds"]:
                cfg["online_interval_seconds"] = cfg["check_interval_seconds"]
            return cfg

        # ---------- 设置页：参数落盘 ----------

        def on_save_params(self):
            """把设置页的参数写入 config.json（**不触发登录**）。

            这一页原先没有保存入口：改完参数只能回连接页点「保存并立即登录」或
            「启动保活」才顺带落盘，否则关掉窗口就丢了——用户界面上明明改了、
            实际没生效，正是"设置页参数不属实"的来源。
            """
            if self._busy:
                return
            cfg = self._collect_config()
            self._set_busy(True, op="save_params")
            self.btn_save_params.set_loading(True, "正在保存…")
            self._set_params_status("正在保存参数…", C_TEXT_3)
            self._run_bg(lambda: (cfg, self._job_save_params(cfg)),
                         self._done_save_params)

        @staticmethod
        def _job_save_params(cfg):
            core.save_config(cfg)   # 失败抛 ConfigError
            return True

        def _done_save_params(self, res, err):
            self._set_busy(False, op="save_params")
            try:
                self.btn_save_params.set_loading(False)
            except Exception:
                pass
            if err is not None:
                msg = "保存失败：%s" % err
                self._set_params_status(msg, C_DANGER_TEXT)
                if isinstance(err, core.ConfigError):
                    messagebox.showerror("保存参数失败", str(err), parent=self.root)
                return
            cfg, _ok = res
            self.cfg = cfg
            # 把输入框刷成真正生效的值：越界被夹的（比如退避上限填 3 变成 10）
            # 当场显示出来，界面上看到的就一定等于配置里在用的。
            adjusted = []
            for label, attr, key in getattr(self, "_param_meta", ()):
                var = getattr(self, attr, None)
                if var is None:
                    continue
                raw = var.get().strip()
                val = cfg.get(key)
                if raw and str(raw) != str(val):
                    adjusted.append("%s：%s → %s" % (label, raw, val))
                var.set("" if val is None else str(val))
            tail = ("保活运行中无需重启，下一轮自动生效。"
                    if self.daemon_ok else "下次启动/登录时生效。")
            if adjusted:
                self._set_params_status(
                    "已保存（超出范围的值已自动修正：%s）。%s"
                    % ("；".join(adjusted), tail), C_SUCCESS_TEXT)
            else:
                self._set_params_status("已保存。%s" % tail, C_SUCCESS_TEXT)

        def _set_params_status(self, text, color=C_TEXT_3):
            """设置页参数保存回执（页内显示，不弹系统框）。"""
            lbl = getattr(self, "lbl_params_status", None)
            if lbl is None:
                return
            try:
                lbl.configure(text=text, fg=color)
            except Exception:
                pass
            self._autosize_wrap(lbl)

        # ---------- 后台线程辅助 ----------

        def _run_bg(self, job, on_done):
            """在后台线程执行 job()，结束后把结果投递到 _ui_queue，由主线程轮询器
            回调 on_done(result, error)。job 内绝不触碰任何 Tk 控件/API（子线程
            也绝不直接调用 root.after——Python 3.14 会抛 RuntimeError 且被吞）。"""

            def wrapper():
                try:
                    res, err = job(), None
                except Exception as e:  # 后台线程异常不能拖垮进程
                    res, err = None, e
                try:
                    if on_done is not None:
                        self._ui_queue.put((on_done, res, err))
                except Exception:
                    pass  # 窗口已销毁
                finally:
                    with self._pending_lock:
                        self._pending_jobs -= 1

            with self._pending_lock:
                self._pending_jobs += 1
            threading.Thread(target=wrapper, daemon=True).start()

        def _drain_ui_queue(self):
            """主线程轮询器：把后台线程投递的 (on_done, res, err) 依次回调到主线程。
            由 root.after 定时（主线程）自调度，绝不跨线程触碰 Tk。

            轮询间隔分三档：有后台操作在跑/结果待上屏时用 25ms 保证及时；完全空闲
            降到 150ms（原来固定 60ms，绝大多数轮次都是空转唤醒，白白打断主线程）；
            窗口最小化时降到 1s 心跳，顺手把物理内存工作集还回去（任务管理器里看
            起来就不占地方了）。
            """
            try:
                while True:
                    on_done, res, err = self._ui_queue.get_nowait()
                    try:
                        on_done(res, err)
                    except Exception:
                        # 不能静默吞掉：所有 _done_* 都以「解禁按钮 + 刷新日志 +
                        # 回写状态」收尾，一旦中途抛异常（比如刷新日志时控件已销毁），
                        # 界面就永远停在忙态、结果行也不更新，而日志里没有任何线索。
                        # 记下来至少能定位；队列本身仍然继续处理。
                        logging.exception("界面回调 %s 执行失败",
                                          getattr(on_done, "__name__", on_done))
            except queue.Empty:
                pass
            with self._pending_lock:
                pending = self._pending_jobs
            if self._busy or pending:
                delay = 25
                self._idle_ticks = 0
            else:
                try:
                    iconic = (self.root.state() == "iconic")
                except Exception:
                    iconic = False
                if iconic:
                    delay = 1000
                    self._idle_ticks += 1
                    if self._idle_ticks >= 30:      # 约 30 秒回收一次
                        self._idle_ticks = 0
                        core.trim_working_set()
                else:
                    delay = 150
                    self._idle_ticks = 0
            try:
                self.root.after(delay, self._drain_ui_queue)
            except Exception:
                pass  # 窗口已关闭

        def _set_busy(self, busy, op=None):
            """操作进行中：禁用所有会改状态/发请求的按钮。

            op 为 None 时是全局开关（登录、保活等主流程）；传 op 名称则表示某个
            可并存的独立操作。原因：自启开关这类小任务**从未置过 busy**，收尾却
            无条件 _set_busy(False)，会把正在进行的登录/保活所禁用的按钮一起解禁
            ——用户于是在登录还在跑时又能点一遍，产生并发请求与状态错乱。改成记名
            集合后，各操作只撤销自己那一份，互不干扰。
            """
            if op is None:
                self._busy_main = bool(busy)
            elif busy:
                self._busy_ops.add(op)
            else:
                self._busy_ops.discard(op)
            state = self._busy_main or bool(self._busy_ops)
            self._busy = state        # 队列轮询器据此调整频率
            if state:
                self._idle_ticks = 0
            for btn in (self.btn_login, self.btn_keep_start, self.btn_keep_stop,
                        self.btn_check_update, self.btn_open_log,
                        self.btn_log_refresh, self.btn_log_open,
                        self.btn_probe_portal):
                try:
                    btn.set_enabled(not state)
                except Exception:
                    pass
            try:
                self.toggle_auto.set_enabled(not state)
            except Exception:
                pass

        # ---------- 状态渲染 ----------

        def _service_label(self):
            try:
                return self.seg_service.get()
            except Exception:
                return "移动"

        def _on_service_changed(self, value):
            """运营商切换时同步刷新顶部状态卡与侧边状态块。"""
            try:
                self.hero.render()
            except Exception:
                pass
            try:
                self._render_side_status()
            except Exception:
                pass

        def _apply_net_state(self, state):
            """把网络三态落到界面：状态主卡 + 侧边状态块。
            返回是否发生「由在线变离线」（用于顶部黄色提示条）。"""
            dropped = (self._prev_state == core.ONLINE
                       and state is not None and state != core.ONLINE)
            self.net_state = state
            if state == core.ONLINE:
                self.hero_mode = "online"
                self.hero_subtitle = ("锐捷 ePortal 认证成功 · "
                                      + ("后台保活运行中" if self.daemon_ok
                                         else "后台保活未运行"))
            else:
                if self.authenticating:
                    self.hero_mode = "auth"
                    self.hero_subtitle = "正在检测网络并提交认证，请稍候…"
                elif state is None:
                    # 启动后后台检测还没回来：显示"检测中"，**不要**先显示成
                    # "未连接"——明明在线却先喊一秒"未连接"，看起来像出了错。
                    self.hero_mode = "off"
                    self.hero_subtitle = "正在检测网络状态，请稍候…"
                else:
                    self.hero_mode = "off"
                    self.hero_subtitle = "填写账号密码，一键认证上网"
            if state is not None:
                self._prev_state = state
            self.hero.render()
            self._render_side_status()
            return dropped

        def _set_authenticating(self, busy):
            """认证中：主按钮 Loading 态 + 主卡「正在认证…」（已在线时保持在线展示）。"""
            self.authenticating = bool(busy)
            self.btn_login.set_loading(busy)
            if busy:
                if self.net_state != core.ONLINE:
                    self.hero_mode = "auth"
                    self.hero_subtitle = "正在检测网络并提交认证，请稍候…"
            else:
                if self.net_state == core.ONLINE:
                    self.hero_mode = "online"
                    self.hero_subtitle = ("锐捷 ePortal 认证成功 · "
                                          + ("后台保活运行中" if self.daemon_ok
                                             else "后台保活未运行"))
                else:
                    self.hero_mode = "off"
                    self.hero_subtitle = "填写账号密码，一键认证上网"
            self.hero.render()
            self._render_side_status()

        # 原 _update_empty_state（未连接时显隐空态卡）与 _render_mini（侧边小卡、
        # 含在线时长）已删除：前者随空态卡一起去掉，后者重写成 _render_side_status。

        def _refresh_daemon_ui(self, running):
            """后台保活状态行 + 按钮可用态 + 主卡副标题。"""
            self.daemon_ok = bool(running)
            pid = core.read_daemon_pid() if running else None
            dot = self.keep_dot
            dot.delete("all")
            if running:
                dot.create_oval(_px(1), _px(1), _px(7), _px(7),
                                fill=C_SUCCESS, outline="")
                self.lbl_keep.configure(
                    text="后台保活运行中（PID %s）· 关闭窗口后仍在后台运行" % pid,
                    fg=C_SUCCESS_TEXT)
                self.btn_keep_start.set_enabled(False)
                self.btn_keep_stop.set_enabled(True)
            else:
                dot.create_oval(_px(1), _px(1), _px(7), _px(7),
                                fill=C_TEXT_3, outline="")
                self.lbl_keep.configure(text="后台保活未运行", fg=C_TEXT_2)
                self.btn_keep_start.set_enabled(True)
                self.btn_keep_stop.set_enabled(False)
            # 主卡副标题同步
            if self.net_state == core.ONLINE:
                self.hero_subtitle = ("锐捷 ePortal 认证成功 · "
                                      + ("后台保活运行中" if running else "后台保活未运行"))
                self.hero.render()

        def _set_autostart_ui(self, exists, message=None, ok=True):
            """开机自启 Toggle + pill + 提示。"""
            self.autostart_on = bool(exists)
            self.toggle_auto.set_state(self.autostart_on)
            self.toggle_auto.set_enabled(True)
            self.toggle_auto.set_busy(False)
            if self.autostart_on:
                self.pill_auto.set("neutral", "已开启")
            else:
                self.pill_auto.set("muted", "未开启")
            if message:
                self._set_result(message, C_SUCCESS_TEXT if ok else C_DANGER_TEXT,
                                 target="boot")

        # 结果行写哪张卡：'main' = 登录与保活卡（登录/保活/断开），
        # 'boot' = 启动卡（开机自启 / 启动时自动登录）
        # 必须是**类属性**：曾误放在 _set_autostart_ui 的方法体内（缩进 8 空格），
        # 于是它只是那个函数的局部变量，_set_result 里的 self._RESULT_LABELS
        # 每次都抛 AttributeError —— 开机自启的回执从此显示不出来。
        _RESULT_LABELS = {"main": "lbl_result", "boot": "lbl_result_boot"}

        def _set_result(self, text, color=C_TEXT_2, target="main"):
            """显示"最近一次操作"的结果。

            按 target 落到对应那张卡的结果行上——两个功能的结果混在一张卡里，
            用户看不出是哪件事的回执。高度**按需伸缩**：空文本时只占一行（不留
            一大块空白，用户抱怨过账号卡底部空），有回执时占两行——预留两行，
            长消息也不会把卡片撑高、把下面的按钮挤出可视区（原来出过这个问题：
            一条长的开机自启回执把「断开连接」顶出了卡片）。
            """
            name = self._RESULT_LABELS.get(target, "lbl_result")
            lbl = getattr(self, name, None)
            if lbl is None:
                return
            try:
                lbl.configure(text=text, fg=color,
                              height=2 if text else 1)
            except Exception:
                pass

        def _show_banner(self):
            """顶部黄色提示条：3 秒后自动消失。"""
            if self._banner_job is not None:
                try:
                    self.root.after_cancel(self._banner_job)
                except Exception:
                    pass
                self._banner_job = None
            self.banner.pack(fill="x", before=self.page_host)
            self._banner_job = self.root.after(3000, self._hide_banner)

        def _hide_banner(self):
            self._banner_job = None
            try:
                self.banner.pack_forget()
            except Exception:
                pass

        # 原 _tick（每秒刷新在线时长 + 顶栏状态 pill）已删除：在线时长去掉了，
        # 顶栏也没了。状态变化本来就由 _apply_net_state / _refresh_daemon_ui 等
        # 事件驱动刷新，不需要再挂一个每秒空转的定时器。

        # ---------- 密码显示/隐藏 ----------

        def _toggle_password(self, _event=None):
            """在掩码(show='*')与明文(show='')之间切换显示方式。
            只改显示方式，绝不改变密码变量的值。返回 'break' 以吞掉 Ctrl+H 默认行为。"""
            self._password_visible = not self._password_visible
            self.ent_password.set_show("" if self._password_visible else "*")
            self.ent_password.set_trailing(
                "隐藏" if self._password_visible else "显示",
                self._svg("eye", "gray" if not self._password_visible else "blue", 16))
            return "break"

        # ---------- 日志展示 ----------

        @staticmethod
        def _parse_log_lines(n):
            """解析 login.log 尾部 n 行 -> [(HH:MM:SS, LEVEL, 文本)]。"""
            text = core.tail_log(n)
            out = []
            for ln in (text or "").splitlines():
                ln = ln.rstrip()
                if not ln.strip():
                    continue
                # 时间戳带毫秒（logging 的默认格式 HH:MM:SS,mmm），必须容忍
                # 逗号后半段；否则整行都不匹配，只能退化成「整行原文 + 恒 INFO」，
                # 彩色圆点就永远只有绿色（设计规范 4.5 #6 要求区分成功/异常）。
                m = re.match(
                    r"^\d{4}-\d{2}-\d{2} (\d{2}:\d{2}:\d{2})(?:,\d{3})? "
                    r"\[(\w+)\] (.*)$", ln)
                if m:
                    out.append((m.group(1), m.group(2).upper(), m.group(3)))
                else:
                    out.append(("", "INFO", ln))
            return out

        @staticmethod
        def _level_color(level):
            if level.startswith("ERROR"):
                return C_DANGER
            if level.startswith("WARN"):
                return C_WARN
            if level.startswith("INFO"):
                return C_SUCCESS
            return C_TEXT_3

        def _make_log_row(self):
            """新建一行日志（彩色圆点 + 文本），并把子控件挂在 row 上供复用。

            bd=0/pady=0：Label 默认的 1px 边框 + 1px 内边距会让每行多出约
            8px，日志卡一列四行就是 30px——白占高度，视觉上也没有用。
            """
            row = tk.Frame(self.log_rows, bg=C_CARD)
            row.pack(fill="x", pady=_px(1))
            d = tk.Canvas(row, width=_px(6), height=_px(6), bg=C_CARD,
                          highlightthickness=0, bd=0)
            d.pack(side="left", padx=(0, _px(8)))
            d.create_oval(0, 0, _px(6), _px(6), fill=C_TEXT_3, outline="")
            lab = tk.Label(row, text="", bg=C_CARD, fg=C_TEXT_2,
                           font=(FONT_UI, 12), anchor="w", bd=0, pady=0)
            lab.pack(side="left", fill="x", expand=True)
            row._dot = d
            row._label = lab
            return row

        def refresh_log_tail(self):
            """刷新主界面日志卡 + 运行日志页（读小文件，主线程直读即可）。"""
            # 主界面：彩色圆点行。
            # **能显示几行由可用高度决定**：日志卡是连接页唯一随窗口伸缩的部分，
            # 窗口拉高就多显示几行、拉矮就少显示几行（用户要的"只更改日志显示高度"）。
            self._sync_log_card_visibility()
            if getattr(self, "_log_card_hidden", False):
                # 一行日志都放不下：整栏已收起，只剩「日志」页要刷新。
                self._refresh_log_page()
                return
            self.log_outer.update_idletasks()
            self._calibrate_log_inset()
            room = self._log_room()
            if room is None:
                room = 0
            avail = self.log_rows.winfo_width() - _px(16)
            if avail < _px(80):
                avail = _px(280)          # 还没布局出来时的兜底宽度
            # 行控件**复用**：只改文本/圆点颜色，不再每次 destroy+create 几十个
            # widget。刷新触发得很密（每个后台操作结束、每次 resize 防抖、日志页
            # 手动刷新），全量重建的 cost 在拖拽结束那一刻会叠成一帧明显卡顿。
            f_row = ui_font(12)
            # 先按估值多读两行，最后再按**实测总高**把放不下的尾部行删掉。
            # 不能在建行过程中拿"第一行的 winfo_reqheight()"当行高：刚 create 出来
            # 那一刻标签还没算出字体高度，量到的会比真实值小（实测 21 vs 23），于是
            # 多留一行、末行被卡片下边缘裁掉——正是"上下拉窗口时日志显示异常"。
            est = max(1, min(LOG_TAIL_MAX, int(room // LOG_ROW_H) + 2))
            lines = self._parse_log_lines(est)
            rows = self.log_rows.winfo_children()
            for extra in rows[len(lines):]:
                extra.destroy()
            for idx, (ts, level, msg) in enumerate(lines):
                row = rows[idx] if idx < len(rows) else self._make_log_row()
                text = (("%s  " % ts) if ts else "") + msg
                try:
                    row._dot.itemconfigure(1, fill=self._level_color(level))
                    row._label.configure(text=fit_text_to_width(f_row, text, avail))
                except Exception:
                    pass
            self.log_rows.update_idletasks()
            # 先用已知行高估算可放几行，一次性砍掉多余的行；只在末尾留一次
            # 精确微调。旧写法是「每 destroy 一行就 update_idletasks 一次」，
            # 最多几十次同步几何重算，是这一段最贵的地方。
            row_h = getattr(self, "_log_row_h", 0) or LOG_ROW_H
            keep = max(1, min(len(lines), int(room // row_h))) if room > 0 else 1
            kids = self.log_rows.winfo_children()
            for extra in kids[keep:]:
                extra.destroy()
            if len(kids) > keep:
                self.log_rows.update_idletasks()
            while (self.log_rows.winfo_children()
                   and self.log_rows.winfo_reqheight() > room):
                self.log_rows.winfo_children()[-1].destroy()
                self.log_rows.update_idletasks()
            rows = self.log_rows.winfo_children()
            if rows:
                # 实测行高（此时标签已算好字体高度），供"一行都放不下就收起"用
                self._log_row_h = max(1, rows[0].winfo_reqheight())
            # 日志页那一整屏 Text 只在**用户正看着日志页**时才重写：200 行
            # delete+insert 是纯 Tk 开销，在连接页上完全看不见。
            if getattr(self, "_page_id", None) == "log":
                self._refresh_log_page()

        def _log_inset_default(self):
            """运行日志卡的固定开销兜底值：投影留白×2 + 内边距×2 + 标题行 + 标题
            与日志行的间距。只在实测标定（_calibrate_log_inset）还没成功时使用，
            宁可略偏大——偏大只会让"收起"来得稍早，不会出现放不下的行。"""
            return (2 * card_margin() + 2 * _px(CARD_PAD) + _px(24) + _px(10))

        def _calibrate_log_inset(self):
            """健康布局时实测一次"卡片固定开销"（inset = 卡高 - 可放行的净高）。

            标定**只认健康布局**（direct > 0 且卡片已正常映射）：窗口被压到最扁时
            卡片被挤没，拿那时的几何当基准会把开销算成整个卡高，于是推出来的净高
            永远为 0——表现就是"收起来后再也展不开"。开销本身与窗口尺寸无关，
            健康时标定一次就够用。
            """
            try:
                card_h = self.log_outer.winfo_height()
                if card_h <= 1 or not self.log_outer.winfo_ismapped():
                    return
                direct = (self.log_outer.body_avail_height()
                          - self.log_rows.winfo_y())
                if direct > 0:
                    self._log_inset = card_h - direct
            except Exception:
                pass

        def _log_room(self):
            """日志卡里"能放日志行"的净高（设备 px）；量不出来返回 None。

            统一用「连接页 body 实际高度 - 上方两张卡的实占高度 - 卡片固定开销」
            反推，**完全不依赖日志卡自身的几何**：它被收起（pack_forget）后不再是
            布局参与者，winfo_height 会停在收起前的值甚至归 1——旧实现"可见时量
            自己、收起时反推"两套公式各量各的，挤压态下（卡片被裁到只剩标题）量出
            None 就跳过收起判定，挤扁的卡片就留在界面上了（用户截图里的样子）。
            上方两张卡是 pack(fill="x") 的固定高度控件，任何时候量到的都是真实值。
            """
            try:
                body_h = self.log_outer.master.winfo_height()
                top = (self.hero.winfo_height()
                       + self.status_grid.winfo_height())
            except Exception:
                return None
            if body_h <= 1 or top <= 1:
                return None
            inset = getattr(self, "_log_inset", None) or self._log_inset_default()
            return max(0, body_h - top - inset)

        def _sync_log_card_visibility(self):
            """按当前净高**同步**决定运行日志卡显隐：一行都放不下就整栏收起，
            放得下则恢复。在拖拽结束（_end_resize）和网络事件刷新
            （refresh_log_tail）两条路上都会被调用，两处判定永远一致。
            """
            if self._page_id != "status" or not self.pages["status"].winfo_ismapped():
                return
            room = self._log_room()
            if room is None:
                return
            need = getattr(self, "_log_row_h", 0) or LOG_MIN_ROOM
            if room < need:
                if self._hide_log_card():
                    self._schedule_log_refit()
            elif self._show_log_card():
                self._schedule_log_refit()

        def _show_log_card(self):
            """恢复日志栏（高度不够被 _hide_log_card 收起来过）。返回是否真的变了。"""
            if getattr(self, "_log_card_hidden", False):
                self.log_outer.pack(fill="both", expand=True)
                self._log_card_hidden = False
                return True
            return False

        def _hide_log_card(self):
            """收起日志栏：可用高度连一行日志都放不下时，整栏收起比挤半行干净。"""
            if not getattr(self, "_log_card_hidden", False):
                for child in self.log_rows.winfo_children():
                    child.destroy()
                self.log_outer.pack_forget()
                self._log_card_hidden = True
                return True
            return False

        def _refresh_log_page(self):
            """「日志」页的完整日志文本（等宽字体，按级别着色）。"""
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            for ts, level, msg in self._parse_log_lines(LOG_PAGE_LINES):
                if ts:
                    self.log_text.insert("end", "%s " % ts, "time")
                tag = ("error" if level.startswith("ERROR")
                       else "warn" if level.startswith("WARN") else "info")
                self.log_text.insert("end", "[%s] %s\n" % (level, msg), tag)
            if not float(self.log_text.index("end-1c").split(".")[0]):
                self.log_text.insert("end", "（暂无日志）", "info")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")

        def _warn_config_broken(self):
            messagebox.showwarning(
                "配置文件损坏",
                "读取 config.json 失败：\n\n%s\n\n已改用默认参数；"
                "重新填写账号密码并保存即可覆盖损坏的文件。" % self.config_error,
                parent=self.root)

        # ---------- 启动时的整体检查 ----------

        def startup_refresh(self):
            # 主线程先采集 UI 配置，再交给后台线程；_job_* 绝不触碰 Tk
            self._set_busy(True)
            cfg = self._collect_config()
            self._run_bg(lambda: self._job_startup(cfg), self._done_startup)

        @staticmethod
        def _job_startup(cfg):
            state = core.check_network(cfg)
            daemon = core.daemon_running()
            task = autostart_enabled()
            ip = get_local_ip(str(cfg.get("portal_host", "")))
            return state, daemon, task, ip

        def _done_startup(self, result, err):
            self._set_busy(False)
            self.refresh_log_tail()
            if err is not None:
                self._set_result("状态检测失败：%s" % err, C_DANGER_TEXT)
                self._refresh_daemon_ui(False)
                self._set_autostart_ui(False, "计划任务：检测失败", ok=False)
                return
            state, daemon, task, ip = result
            self.local_ip = ip
            dropped = self._apply_net_state(state)
            if dropped:
                self._show_banner()
            self._refresh_daemon_ui(daemon)
            self._set_autostart_ui(task)

        # ---------- 本机 MAC ----------

        def _fetch_local_mac(self):
            """后台线程：取正在上网的那块网卡的 MAC。

            必须走后台——PowerShell 查询要 1 秒上下，卡在主线程上就是启动白屏。
            """
            return core.primary_mac_address(str(self.cfg.get("portal_host", "")))

        def _done_local_mac(self, result, err):
            if err is not None:
                logging.debug("取本机 MAC 失败: %s", err)
                return
            mac = (result or "").strip()
            if mac and mac != getattr(self, "local_mac", ""):
                self.local_mac = mac
                self.hero.render()
                self._render_side_status()

        # ---------- 自动让位状态（只读展示） ----------

        def _tick_yield_status(self):
            """每 5 秒刷一次让位状态（读 daemon_state.json，纯展示）。"""
            self._yield_job = None
            try:
                self._refresh_yield_status()
            except Exception:
                logging.debug("刷新让位状态失败", exc_info=True)
            try:
                self._yield_job = self.root.after(5000, self._tick_yield_status)
            except Exception:
                pass

        def _refresh_yield_status(self):
            lbl = getattr(self, "lbl_yield", None)
            if lbl is None:      # 页面还没建好（首次刷新早于布局）
                return
            d = core.read_daemon_state()
            left = int(float(d.get("yield_until") or 0) - time.time())
            lbl = self.lbl_yield
            if left <= 0:
                # 没在让位就整行收起，不留空白（pack_forget 后重新 pack 要指定
                # before，否则会掉到卡片末尾、跑到自启小节下面）
                if lbl.winfo_ismapped():
                    lbl.configure(text="")
                    lbl.pack_forget()
                return
            reason = (d.get("reason") or "其他设备正在使用本账号").strip()
            mins = max(1, (left + 59) // 60)
            txt = "已自动让位：%s，约 %d 分钟后重试（对方下线会提前恢复）" % (reason, mins)
            if not lbl.winfo_ismapped():
                lbl.pack(fill="x", pady=(_px(6), 0), before=self.keep_flex)
            if lbl.cget("text") != txt:
                lbl.configure(text=txt)

        # ---------- 保存并立即登录 ----------

        def on_save_login(self):
            # 忙态闸门：_set_busy 只禁用按钮，输入框仍可回车，连按两次回车就会
            # 并发跑两个 run_once（两次 CAS + 密码提交），先返回的那个还会把
            # busy/authenticating 复位，导致界面状态错乱。
            # 这里在 collect_config 之前就置 authenticating，把"检查-置位"的时间窗
            # 关到最小；即使如此，后面仍用 try/finally 保证状态一定复位。
            # 注意属性名是 self.authenticating（无下划线）——_set_authenticating 里
            # 设的也是它。曾写成 self._authenticating，点登录按钮直接 AttributeError，
            # 表现为"点了没反应、日志刷屏界面回调异常"。这里用 getattr 兜底，
            # 免得以后再改名又把入口打挂。
            if self._busy or getattr(self, "authenticating", False):
                return
            self._set_authenticating(True)
            cfg = self._collect_config()
            if not cfg["username"] or not cfg["password"]:
                self._set_authenticating(False)
                messagebox.showwarning(
                    "请填写凭据", "请先填写校园网账号和密码，再点击「保存并立即登录」。",
                    parent=self.root)
                return
            self._set_busy(True)
            self._set_result("正在登录，请稍候…（检测 + 登录最多约 30 秒）")
            # 配置随作业结果一起回传（见 _done_save_login），不用共享槽位
            self._run_bg(lambda: (cfg, self._job_save_login(cfg)),
                         self._done_save_login)

        @staticmethod
        def _job_save_login(cfg):
            core.save_config(cfg)      # 保存配置（失败抛 ConfigError）
            result = core.run_once(cfg)  # 单次检测 + 按需登录，结构化结果

            # —— 真实校验：拿账号在 SAM 后台**实际绑定**的运营商来比对 ——
            # 网关 serviceLogin 未必校验运营商，光看它返回 success 会出现"随便选
            # 什么都绿"的假成功。查不到绑定运营商（无凭据/自助服务不通）时按
            # "未知"处理，绝不因此把正常结果改判成失败。
            bound = None
            try:
                bound = core.fetch_bound_operator(cfg)
            except Exception:
                bound = None
            if bound:
                selected = SERVICE_LABELS.get(cfg.get("service", "cmcc"), "移动")
                if selected != bound:
                    result = dict(result)
                    result["ok"] = False          # 选错运营商：**不能算成功**
                    result["detail"] = "bound_mismatch"
                    if result.get("state") == core.ONLINE:
                        result["message"] = (
                            "在线未重认证；所选%s与绑定%s不符，断网重连才生效"
                            % (selected, bound))
                    else:
                        result["message"] = (
                            "所选%s与账号绑定%s不符，不能算认证成功"
                            % (selected, bound))
                else:
                    result = dict(result)
                    result["message"] = result["message"] + "（绑定：%s）" % bound
            return result

        def _done_save_login(self, res, err):
            """登录/保存结果回调：无论如何都要把 busy/authenticating 复位，避免
            按钮永远停在"正在认证…"。"""
            try:
                if err is not None:
                    if isinstance(err, core.ConfigError):
                        messagebox.showerror("保存配置失败", str(err), parent=self.root)
                        self._set_result("配置保存失败（详见弹窗）", C_DANGER_TEXT)
                    else:
                        self._set_result("登录过程出错：%s" % err, C_DANGER_TEXT)
                    return
                # job 里不能碰 self.cfg（线程纪律），所以配置**随结果回传**、在这里回填。
                # 原来是共享一个 self._pending_cfg 槽位：两个后台作业并发时（一边启动保活、
                # 一边点「保存并立即登录」）会互相把对方的配置覆盖掉，回填的就是错的；
                # 而且 on_save_login 自己**从来没设置过**那个槽位，所以这里读到的可能是别的
                # 作业留下的陈旧配置。
                cfg, result = res
                self.cfg = cfg
                if result["state"] == core.OFFLINE:
                    color, msg = C_TEXT_2, result["message"]
                elif result["ok"]:
                    color = C_SUCCESS_TEXT
                    msg = result["message"]
                    if result["state"] == core.ONLINE:
                        # 已在线时 core.run_once 直接返回「已联网，无需登录」——**不会**把
                        # 新运营商提交给网关（真正发 service 的是 serviceLogin，只在未认证
                        # 时才走）。不加这句，用户就会以为"选电信也认证成功"，实际是压根
                        # 没重新认证。绑定运营商已核对过时（结果里带「绑定：」）不再重复提示，
                        # 免得回执被撑到两行之外被裁掉。
                        if "（绑定：" not in msg:
                            msg += "（已在线，未重新认证；改运营商要下次真正认证才生效）"
                else:
                    color, msg = C_DANGER_TEXT, result["message"]
                self._set_result(msg, color)
                dropped = self._apply_net_state(result["state"])
                if dropped:
                    self._show_banner()
            finally:
                self._set_busy(False)
                self._set_authenticating(False)
                self.refresh_log_tail()

        # ---------- 后台保活 ----------

        def on_start_keepalive(self):
            cfg = self._collect_config()
            if not cfg["username"] or not cfg["password"]:
                messagebox.showwarning(
                    "请填写凭据",
                    "请先填写校园网账号和密码（建议先「保存并立即登录」验证一次），"
                    "再启动后台保活。", parent=self.root)
                return
            self._set_busy(True)
            self._set_result("正在检查现有的后台保活…")
            self._run_bg(core.daemon_running, self._done_check_before_start)

        def _done_check_before_start(self, running, err):
            if err is None and running:
                ok = messagebox.askyesno(
                    "后台保活已在运行",
                    "检测到后台保活正在运行。\n\n是否重启它以应用最新配置？\n"
                    "「是」= 停止旧进程，并按当前界面配置重启\n"
                    "「否」= 保持现状（不应用新配置）",
                    parent=self.root)
                if not ok:
                    self._set_busy(False)
                    self._set_result("保持原有后台保活运行（未应用新配置）")
                    return
            self._spawn_keepalive()

        def _spawn_keepalive(self):
            # 主线程采集配置后传给后台线程，_job_spawn_daemon 不触碰 Tk
            cfg = self._collect_config()
            self._set_result("正在启动后台保活…")
            self._run_bg(lambda: (cfg, self._job_spawn_daemon(cfg)),
                         self._done_spawn_daemon)

        @staticmethod
        def _job_spawn_daemon(cfg):
            core.save_config(cfg)  # 先保存最新配置，daemon 启动后读到的就是新配置
            spawn_daemon()
            time.sleep(1.5)        # 等 daemon 写 app.pid（接管旧实例时最多要 5 秒）
            if not core.daemon_running():
                time.sleep(4.0)
            return core.daemon_running()

        def _done_spawn_daemon(self, res, err):
            self._set_busy(False)
            self.refresh_log_tail()
            if err is not None:
                if isinstance(err, core.ConfigError):
                    messagebox.showerror("保存配置失败", str(err), parent=self.root)
                    self._set_result("启动失败（配置保存失败）", C_DANGER_TEXT)
                else:
                    self._set_result("启动失败：%s" % err, C_DANGER_TEXT)
                return
            cfg, running = res
            self.cfg = cfg
            if running:
                pid = core.read_daemon_pid()
                self._set_result("后台保活已启动（PID %s）" % pid, C_SUCCESS_TEXT)
            else:
                self._set_result("已拉起保活进程，但未能确认它在运行", C_WARN_TEXT)
            self._refresh_daemon_ui(running)

        def on_stop_keepalive(self):
            self._set_busy(True)
            self._set_result("正在停止后台保活…")
            self._run_bg(core.stop_daemon, self._done_stop_keepalive)

        def _done_stop_keepalive(self, result, err):
            self._set_busy(False)
            self.refresh_log_tail()
            if err is not None:
                self._set_result("停止操作出错：%s" % err, C_DANGER_TEXT)
                return
            ok, msg = result
            self._set_result(msg, C_SUCCESS_TEXT if ok else C_DANGER_TEXT)
            self._refresh_daemon_ui(False)

        # ---------- 断开连接（快捷操作） ----------

        # ---------- 开机自启（Toggle） ----------

        def on_toggle_autostart(self):
            """开关被点击：目标状态 = 当前状态的相反值。后台线程注册/删除计划任务
            （可能弹 UAC），完成后按计划任务真实状态回填开关与 pill。"""
            want = not self.autostart_on
            # 乐观 UI：先把开关/pill 画成目标状态。以前要点完以后台任务（1~3 秒）
            # 才回填，用户感觉"点了没反应"；现在立刻响应，失败再回弹并说明原因。
            self.autostart_on = want
            self.toggle_auto.set_enabled(True)
            self.toggle_auto.set_state(want)
            self.toggle_auto.set_busy(True)
            self.pill_auto.set("neutral" if want else "muted",
                               "已开启" if want else "未开启")
            cfg = self._collect_config()
            self._set_result("正在%s开机自启…" % ("设置" if want else "取消"),
                             target="boot")
            self._set_busy(True, op="autostart")
            self._run_bg(
                lambda: (cfg, self._job_toggle_autostart(cfg, want)),
                lambda res, err: self._done_toggle_autostart(want, res, err))

        @staticmethod
        def _job_toggle_autostart(cfg, want):
            core.save_config(cfg)  # 配置已在主线程采集，这里只落盘
            try:
                if want:
                    ok, msg = register_autostart()
                else:
                    ok, msg = unregister_autostart()
            except Exception as e:
                ok, msg = False, str(e)
            # 真实状态在**后台线程**查：autostart_enabled() 内部是
            # subprocess.run(schtasks, timeout=15)，放主线程最坏会冻结界面 15 秒。
            # 无论如何都以计划任务的实际存在与否为准回填，避免「动作返回成功但
            # 任务被系统回滚」导致开关与真实状态脱节。
            try:
                real = autostart_enabled()
            except Exception:
                real = want if ok else (not want)
            return ok, msg, real

        def _done_toggle_autostart(self, want, res, err):
            self._set_busy(False, op="autostart")
            self.refresh_log_tail()
            if err is not None:
                if isinstance(err, core.ConfigError):
                    messagebox.showerror("保存配置失败", str(err), parent=self.root)
                # 出异常：回弹到点击前的状态（不再主线程查 schtasks——那会冻结界面
                # 最多 15 秒）。配置不回填——save_config 就是在这里失败的，
                # self.cfg 保持上一次真正落盘的内容才对。
                self._set_autostart_ui(not want, "操作失败：%s" % err, ok=False)
                return
            cfg, result = res
            self.cfg = cfg
            ok, msg, real = result
            self._set_autostart_ui(real, msg, ok=ok)

        # ---------- 维护 ----------

        def on_probe_portal(self):
            """自动探测认证网关地址（换学校后点这个，不用手动问网管要 IP）。

            探测要挨个试诱饵地址，最坏 6 秒才有结果——期间按钮转圈 + 下方文案循环
            加点，让人一眼看出「已经在跑了」。
            """
            if "probe_portal" in self._busy_ops:
                return          # 已在探测：再点只会再开一个后台任务叠一个提示
            self._set_busy(True, op="probe_portal")
            self.btn_probe_portal.set_loading(True, "正在探测…")
            self._set_probe_status("正在探测认证网关（最多约 6 秒）", C_TEXT_2,
                                   animate=True)
            self._run_bg(core.discover_portal_host, self._done_probe_portal)

        def _done_probe_portal(self, host, error):
            """探测结果上屏。探不到要说清楚为什么，别让人以为是程序坏了。"""
            self._set_busy(False, op="probe_portal")
            self.btn_probe_portal.set_loading(False)
            cur = str(self.var_portal.get() or self.cfg.get("portal_host", "") or "")
            if error is not None or not host:
                why = "（%s）" % error if error is not None else ""
                self._set_probe_status(
                    "没探测到认证网关%s。自动探测依赖 NAS 的强制跳转，需要处于"
                    "「连着校园网、但还没认证」的状态；现在若已能正常上网就探不到。"
                    "当前网关：%s，也可以直接在上方手动填写。"
                    % (why, cur or "未设置"), C_WARN_TEXT)
                return
            self.var_portal.set(host)
            self._set_probe_status(
                "已探测到认证网关：%s，已填入上方输入框。回到「连接」页点"
                "「保存并立即登录」即可生效。" % host, C_SUCCESS_TEXT)

        def _set_probe_status(self, text, color=C_TEXT_2, animate=False):
            """探测回执上屏；animate=True 时句尾的「·」循环增加，表示还在跑。"""
            lbl = getattr(self, "lbl_probe_status", None)
            if lbl is None:
                return
            self._probe_text = text
            self._stop_probe_anim()
            try:
                lbl.configure(text=text, fg=color)
            except Exception:
                return
            if animate:
                self._probe_dot = 0
                self._tick_probe_anim()

        def _tick_probe_anim(self):
            """句尾 1~3 个「·」循环，400ms 一步——比转圈更醒目的「我在等」信号。"""
            lbl = getattr(self, "lbl_probe_status", None)
            if lbl is None:
                self._probe_anim_job = None
                return
            try:
                lbl.configure(text=self._probe_text + "·" * (1 + self._probe_dot % 3))
            except Exception:
                self._probe_anim_job = None      # 窗口已销毁，链到此为止
                return
            self._probe_dot += 1
            try:
                self._probe_anim_job = self.root.after(400, self._tick_probe_anim)
            except Exception:
                self._probe_anim_job = None

        def _stop_probe_anim(self):
            job = getattr(self, "_probe_anim_job", None)
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except Exception:
                    pass
            self._probe_anim_job = None

        def on_check_update(self):
            """检查更新：查 GitHub 上有没有比当前更新的 Release。

            以前这里只是把 APP_VERSION 打印出来，按钮却叫「检查更新」——用户会以为
            真的联网查过了。现在真的去查；没配仓库或查不到时如实退回本机版本信息，
            **绝不**在连不上服务器时谎称"已是最新版本"。
            """
            repo = str(self.cfg.get("update_repo") or UPDATE_REPO or "").strip()
            if not repo:
                self._show_version_info()
                return
            # 独立小操作，用记名 op，别把登录/保活按钮一起锁住
            self._set_busy(True, op="update")
            self.btn_check_update.set_loading(True, "检查中…")
            self._run_bg(lambda: core.check_update(repo, APP_VERSION),
                         self._done_check_update)

        def _done_check_update(self, result, error):
            """更新查询结果上屏（主线程回调）。失败一律按"没查到"处理，不弹错误框。"""
            self._set_busy(False, op="update")
            self.btn_check_update.set_loading(False)
            status, info = result if isinstance(result, tuple) else ("unknown", {})
            if error is not None:
                status, info = "unknown", {}

            if status == "available":
                notes = (info.get("notes") or "").strip()
                if len(notes) > 400:
                    notes = notes[:400] + "…"
                lines = ["发现新版本：%s（当前 %s）"
                         % (info.get("version") or "?", APP_VERSION)]
                if info.get("published"):
                    lines.append("发布日期：%s" % info["published"])
                if notes:
                    lines += ["", "更新说明：", notes]
                lines.append("")
                lines.append("是否打开下载页面？")
                try:
                    if messagebox.askyesno("检查更新", "\n".join(lines),
                                           parent=self.root):
                        url = info.get("page") or info.get("url") or ""
                        if url:
                            webbrowser.open(url)
                except Exception:
                    logging.debug("打开下载页面失败", exc_info=True)
                return

            if status == "up_to_date":
                messagebox.showinfo(
                    "检查更新",
                    "当前已是最新版本：%s\n\n服务器上最新为 %s。"
                    % (APP_VERSION, info.get("version") or APP_VERSION),
                    parent=self.root)
                return

            # unknown：校园网未认证时公网不通是常态，如实说明即可
            self._show_version_info(offline=True)

        def _show_version_info(self, offline=False):
            """本机版本信息（没配更新服务器 / 连不上时的兜底展示）。"""
            lines = ["当前版本：%s" % APP_VERSION]
            try:
                exe = os.path.join(core.BASE_DIR, "CampusLogin.exe")
                if os.path.exists(exe):
                    lines.append("打包时间：%s" % time.strftime(
                        "%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(exe))))
            except OSError:
                pass
            if offline:
                lines += ["", "未能连接到更新服务器",
                          "（校园网未认证时连不上外网属正常现象）。"]
            lines += [
                "",
                "升级方式：用新版 CampusLogin.exe 覆盖旧文件即可，",
                "config.json 里的账号密码配置不受影响。",
            ]
            messagebox.showinfo("关于版本", "\n".join(lines), parent=self.root)

        def on_open_logdir(self):
            """打开程序所在目录（log 与 config 都在这里）。"""
            try:
                os.startfile(core.BASE_DIR)  # noqa: AttributeError 仅非 Windows
            except Exception as e:
                messagebox.showerror("打开失败", "无法打开目录：%s" % e,
                                     parent=self.root)

    # ---------- 顶部状态主卡 ----------

    class HeroCard(tk.Canvas):
        """顶部状态主卡（连接页）：图标圆 + 状态文案 + 副标题 + 状态 pill +
        右侧两列指标（IP 地址 / 运营商）。

        在线：渐变 #0059C7→#003B99；未连接/认证中：灰蓝底。
        「在线时长」原先也在这里作为第三个指标、并且驱动一个每秒重绘的定时器，
        已按需求移除——它对「现在能不能上网」没有帮助，去掉后连每秒重绘也省了。
        「今日流量」无后端数据源，按客户决策 #2 隐藏。
        本类只负责绘制，数据全部从 app（CampusLoginApp）读取。
        """

        def __init__(self, master, app):
            super().__init__(master, bg=C_BG, highlightthickness=0, bd=0,
                             height=_px(140) + 2 * card_margin())
            self.app = app
            # 字体只建一次：渲染里要反复 measure 来做自适应排版，每帧新建 tkfont.Font
            # 会不断产生具名字体对象，白白堆积。
            self._f_title = tkfont.Font(family=FONT_UI, size=22, weight="bold")
            self._f_sub = tkfont.Font(family=FONT_UI, size=13)
            self._f_lbl = tkfont.Font(family=FONT_UI, size=12)
            self._f_val = tkfont.Font(family=FONT_NUM, size=15, weight="bold")
            self._f_pill = tkfont.Font(family=FONT_UI, size=11)
            self.bind("<Configure>", lambda e: self.render(), add="+")

        def render(self):
            self.delete("all")
            w = self.winfo_width()
            h = self.winfo_height()
            m = card_margin()          # 四周留给投影，否则会被 Canvas 边缘裁掉
            if w < _px(60) + 2 * m or h < _px(40) + 2 * m:
                return
            app = self.app
            online = app.hero_mode == "online"
            grad = ((C_GRAD_TOP, C_GRAD_BOTTOM) if online
                    else ("#5E6D86", "#3E4A5E"))       # 灰蓝底：未连接 / 认证中
            round_rect(self, m, m, w - 1 - m, h - 1 - m, _px(CARD_RADIUS),
                       gradient=grad, shadow=card_shadow(), tags="hero")
            # —— 左侧图标圆 ——
            cr = _px(32)
            cx = m + _px(34) + cr
            cy = h / 2.0
            self.create_oval(cx - cr, cy - cr, cx + cr, cy + cr,
                             fill="#4C86DE" if online else "#77869C", outline="")
            icon = app._svg("wifi", "white", 28)
            if icon is not None:
                self.create_image(cx, cy, image=icon)
            # —— 文案区 ——
            tx = cx + cr + _px(22)
            if online:
                title = "网络已连接"
            elif app.hero_mode == "auth":
                title = "正在认证…"
            elif getattr(app, "net_state", None) is None:
                title = "正在检测网络…"   # 启动检测还没回来，别先喊"未连接"
            else:
                title = "未连接"
            # 先量出右侧指标区**真正**占多宽，左侧文案才有据可依。
            # 原来这里三方互不知情：文字从前往后排在左、指标从后往前排在右、分隔线
            # 位置写死成 w-362，而它判断"会不会碰到文字"的阈值只留了 160px——可副
            # 标题实测宽 302px。窗口一窄分隔线就直接压在文字上（960 宽度下重叠约
            # 100px，正是用户截图里的样子）。
            cols = (
                ("IP 地址", app.local_ip or "—"),
                ("运营商", app._service_label()),
            )
            m_right = w - m - _px(26)
            col_w = [max(self._f_lbl.measure(lb), self._f_val.measure(vl))
                     for lb, vl in cols]
            col_gap = _px(36)
            m_left = m_right - (sum(col_w) + col_gap * (len(cols) - 1))
            min_gap = _px(28)

            def _fit(text, font, budget):
                """放不下就截断加省略号——宁可少显示几个字，也不让文字压到指标上。"""
                return fit_text_to_width(font, text, budget)

            budget = m_left - tx - min_gap
            title = _fit(title, self._f_title, budget)
            subtitle = _fit(app.hero_subtitle, self._f_sub, budget)
            self.create_text(tx, m + _px(31), text=title, anchor="w",
                             fill="#FFFFFF", font=(FONT_UI, 22, "bold"))
            self.create_text(tx, m + _px(63), text=subtitle, anchor="w",
                             fill=C_HERO_SUB, font=(FONT_UI, 13))
            # 状态 pill（直接画在主卡上）
            pill_h = _px(28)
            py = m + _px(85)
            if online:
                pf, pd, pt = C_SUCCESS, "#FFFFFF", "#FFFFFF"
                ptext = "在线"
            else:
                pf, pd, pt = C_CARD_BLUE, C_PRIMARY, C_PRIMARY
                if app.hero_mode == "auth":
                    ptext = "认证中"
                elif getattr(app, "net_state", None) is None:
                    ptext = "检测中"
                else:
                    ptext = "未连接"
            pw = _px(30) + self._f_pill.measure(ptext)
            round_rect(self, tx, py, tx + pw, py + pill_h, pill_h / 2.0,
                       fill=pf, outline="")
            d = _px(6)
            self.create_oval(tx + _px(12), py + pill_h / 2.0 - d / 2.0,
                             tx + _px(12) + d, py + pill_h / 2.0 + d / 2.0,
                             fill=pd, outline="")
            self.create_text(tx + _px(24), py + pill_h / 2.0, text=ptext,
                             anchor="w", fill=pt, font=(FONT_UI, 11))
            # —— 本机 MAC：紧跟状态 pill ——
            # 校园网后台是按设备（MAC）认人的，排障/报修时对方第一个问的就是它，
            # 放在状态旁边最顺手。空间不够就截断，绝不让它压到右边的指标列。
            mac = getattr(app, "local_mac", "") or ""
            if mac:
                mx = tx + pw + _px(12)
                avail = m_left - mx - _px(10)
                if avail >= _px(70):
                    self.create_text(
                        mx, py + pill_h / 2.0,
                        text=_fit("MAC %s" % mac, self._f_pill, avail),
                        anchor="w", fill=(C_HERO_SUB if online else C_TEXT_3),
                        font=(FONT_UI, 11))
            # —— 分隔线：画在文案与指标之间**真正的空隙**中央；空隙不够就干脆不画 ——
            text_right = tx + max(self._f_title.measure(title),
                                  self._f_sub.measure(subtitle))
            gap = m_left - text_right
            if gap >= min_gap * 2:
                div_x = text_right + gap / 2.0
                self.create_line(div_x, m + _px(30), div_x, h - m - _px(30),
                                 fill=("#4A82D8" if online else "#6E7C91"))
            # —— 右侧两列指标（右对齐、从右往左排；读序仍是 IP → 运营商）——
            x_right = m_right
            for (label, value), cw in zip(reversed(cols), reversed(col_w)):
                self.create_text(x_right, m + _px(50), text=label, anchor="e",
                                 fill=C_HERO_METRIC, font=(FONT_UI, 12))
                self.create_text(x_right, m + _px(80), text=value, anchor="e",
                                 fill="#FFFFFF", font=(FONT_NUM, 15, "bold"))
                x_right -= cw + col_gap


# ============================ GUI 冒烟自检（--gui-smoke，QA 用） ============================

SMOKE_PAGE_ORDER = ["status", "log", "settings"]


def run_gui_smoke(report_path=None, screenshot_dir=None):
    """界面自检模式（内部 QA 用，不对用户暴露）：
    构建完整 GUI -> 逐页切换 -> 采集控件度量（导航选中态 / 卡片圆角 / 渐变 /
    分段按钮 / 开关 / SVG 加载 / 空态插画）-> 写 JSON 报告 -> 自动关闭。
    退出码：0 = 全部通过；2 = 有断言失败。不发起任何网络登录请求。
    """
    if not HAS_TK:
        print("[错误] 缺少 tkinter，无法执行 GUI 自检")
        return 1
    enable_dpi_awareness()
    core.setup_logging(console=False)
    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", 1.3333 * DPI_SCALE)
    except Exception:
        pass
    app = CampusLoginApp(root, smoke=True)
    report = {
        "dpi_scale": DPI_SCALE,
        "tk_version": root.tk.call("info", "patchlevel"),
        # 标记这份报告来自源码还是打包后的 exe。两者的 login_core.BASE_DIR 不同
        # （src/ 与 exe 所在目录），读到的 config/login.log 也就不一样——不标出来
        # 很容易把"源码模式读到空配置"误判成功能坏了（见项目记忆的盲区那条）。
        "frozen": bool(getattr(sys, "frozen", False)),
        "base_dir": core.BASE_DIR,
        "svg_loaded": 0,
        "svg_missing": [],
        "pages": [],
        "checks": {},
        "errors": [],
    }
    grabber = None
    if screenshot_dir:
        try:
            os.makedirs(screenshot_dir, exist_ok=True)
        except Exception:
            pass
        try:
            from PIL import ImageGrab  # 仅源码冒烟可用（构建 venv 里装了 pillow）
            grabber = ImageGrab
        except Exception:
            grabber = None

    def snap(name):
        if grabber is None:
            return
        try:
            root.update_idletasks()
            box = (root.winfo_rootx(), root.winfo_rooty(),
                   root.winfo_rootx() + root.winfo_width(),
                   root.winfo_rooty() + root.winfo_height())
            grabber.grab(bbox=box).save(os.path.join(screenshot_dir,
                                                     "page_%s.png" % name))
        except Exception as e:
            report["errors"].append("screenshot %s: %s" % (name, e))

    def check(name, ok, detail=""):
        report["checks"][name] = bool(ok)
        if not ok:
            report["errors"].append("%s %s" % (name, detail))

    def collect_texts(w, out=None):
        """递归收集所有 Label 的文字，用来断言"某个文案确实不在界面上了"。"""
        if out is None:
            out = []
        try:
            if isinstance(w, tk.Label):
                out.append(str(w.cget("text")))
        except Exception:
            pass
        for c in w.winfo_children():
            collect_texts(c, out)
        return out

    def step(i=0):
        try:
            if i < len(SMOKE_PAGE_ORDER):
                pid = SMOKE_PAGE_ORDER[i]
                app._switch_page(pid)

                def do_measure():
                    root.update()  # 强制处理重绘事件，避免截到未画完的控件
                    nav_sel = [n.page_id for n in app._navs if n._sel]
                    info = {
                        "page": pid,
                        "nav_selected": nav_sel[0] if nav_sel else None,
                        "visible": app.pages[pid].winfo_ismapped(),
                    }
                    if pid == "status":
                        info["hero_items"] = len(app.hero.find_withtag("hero"))
                        info["login_btn_items"] = len(
                            app.btn_login.find_withtag("grad"))
                        info["hero_h"] = app.hero.winfo_height()
                        info["top_row_h"] = app.status_grid.winfo_height()
                        info["log_h"] = app.log_outer.winfo_height()
                        info["log_rows"] = len(app.log_rows.winfo_children())
                    if pid == "settings":
                        info["param_entries"] = sum(
                            1 for c in app.pages["settings"].winfo_children())
                    report["pages"].append(info)
                    snap(pid)
                    root.after(250, lambda: step(i + 1))

                root.after(400, do_measure)
            else:
                # ---- 最终断言 ----
                root.update()   # 确保最后一次布局/重绘已落盘再量控件
                report["svg_loaded"] = len([v for v in app._svg_cache.values()
                                            if v is not None])
                report["svg_missing"] = [list(k) for k in app._svg_missing]
                check("svg_loaded", report["svg_loaded"] >= 8,
                      "(loaded=%d)" % report["svg_loaded"])
                check("nav_three_items", len(app._navs) == len(PAGES),
                      len(app._navs))
                # 输入框必须真的被嵌回画布：曾经 delete("all") 删掉 window item
                # 后不再重建，账号/密码框会整个不显示（实际使用中必现）
                check("entry_embedded",
                      any(app.ent_username.type(i) == "window"
                          for i in app.ent_username.find_all()),
                      list(app.ent_username.find_all()))
                # 卡片内容必须**堆在背景之上**。Tk 的 canvas item 按创建顺序堆叠，
                # 而 Card._redraw 每轮都用 delete("card") 重建背景（新 id 更大）：
                # 少了 tag_raise，第二次重绘之后背景就压在内嵌内容帧上，整张卡的
                # 控件集体消失（不报错、不进日志，只是画面上没了）。
                card = app.account_outer
                if card._win is not None:
                    order = list(card.find_all())
                    check("card_content_on_top",
                          order.index(card._win) > max(
                              [order.index(i) for i in card.find_withtag("card")]),
                          "win=%s order=%s" % (card._win, order))
                else:
                    check("card_content_on_top", False, "no window item")
                # 日志解析：真实 logging 格式带毫秒（HH:MM:SS,mmm），必须解析出
                # 时间与级别，否则退化成整行原文 + 恒 INFO（彩色圆点失效）
                _saved_log = core.LOG_PATH
                probe_log = None
                try:
                    fd, probe_log = tempfile.mkstemp(suffix=".log")
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write("2026-09-22 18:48:46,529 [WARN] 网络波动\n")
                    core.LOG_PATH = probe_log
                    got = app._parse_log_lines(1)
                finally:
                    core.LOG_PATH = _saved_log
                    if probe_log:
                        try:
                            os.remove(probe_log)
                        except OSError:
                            pass
                check("log_parse_ms",
                      got == [("18:48:46", "WARN", "网络波动")], repr(got))

                # ---- 自动探测网关：转圈 loading + 页内回执（本次改动回归）----
                # 探测最坏要等 6 秒：以前点了按钮毫无变化、结果又走系统提示框（长
                # 文案换行难看，保活正忙时还会叠出第二个框）。现在按钮转圈、回执
                # 写在按钮下面，这两条断言钉住"点了看得出在跑"和"结果有地方显示"。
                try:
                    app._switch_page("settings")
                    root.update()
                    _btn = app.btn_probe_portal
                    _btn.set_loading(True, "正在探测…")
                    root.update()
                    _load_ok = (_btn._loading and _btn._spinner_job is not None
                                and (len(_btn.find_withtag("spinner")) == 1
                                     or _btn.winfo_width() < _px(8)))
                    _btn.set_loading(False)
                    root.update()
                    _load_ok = (_load_ok and not _btn._loading
                                and _btn._spinner_job is None
                                and not _btn.find_withtag("spinner"))
                    check("probe_button_spinner", _load_ok,
                          "loading=%s job=%s spin=%s w=%s" % (
                              _btn._loading, _btn._spinner_job,
                              len(_btn.find_withtag("spinner")),
                              _btn.winfo_width()))
                    app._done_probe_portal("10.0.0.9", None)
                    _ok_text = str(app.lbl_probe_status.cget("text"))
                    app._done_probe_portal("", None)
                    _fail_text = str(app.lbl_probe_status.cget("text"))
                    check("probe_status_inline",
                          "10.0.0.9" in _ok_text and "没探测到" in _fail_text
                          and app.var_portal.get() == "10.0.0.9",
                          "ok=%r fail=%r var=%r" % (
                              _ok_text, _fail_text, app.var_portal.get()))
                    # 等待动画必须能停：探测结束后还在跑就会一直去改已销毁的标签
                    app._stop_probe_anim()
                    check("probe_anim_stops", app._probe_anim_job is None)
                except Exception as _e:
                    for _n in ("probe_button_spinner", "probe_status_inline",
                               "probe_anim_stops"):
                        check(_n, False, repr(_e))

                # ---- 主按钮 loading 必须能正常退出（本次改动回归）----
                # 上个版本把 loading 提到 RoundButton 后，set_loading(False) 仍沿用了
                # _loading_text 做回退文案，导致退出 loading 后按钮文字永远停在
                # "正在认证…"——用户截图里就是主按钮一直显示正在认证。
                try:
                    _btn = app.btn_login
                    _btn.set_loading(True)
                    _txt_loading = _btn._text
                    root.update()
                    _login_load_ok = (_btn._loading and _txt_loading == "正在认证…"
                                      and len(_btn.find_withtag("spinner")) == 1)
                    _btn.set_loading(False)
                    _txt_done = _btn._text
                    root.update()
                    _login_load_ok = (_login_load_ok and not _btn._loading
                                      and _txt_done == "保存并立即登录"
                                      and not _btn.find_withtag("spinner"))
                    check("login_button_resets", _login_load_ok,
                          "loading=%s text=%r->%r spin=%s" % (
                              _btn._loading, _txt_loading, _txt_done,
                              len(_btn.find_withtag("spinner"))))
                except Exception as _e:
                    check("login_button_resets", False, repr(_e))

                # ---- 真实点击「保存并立即登录」必须不抛异常 ----
                # 上一版在 on_save_login 里写了 self._authenticating（真实属性是
                # self.authenticating），每次点击都 AttributeError，界面就是"点了
                # 没反应"。之前的断言只直接调 set_loading，绕开了点击路径，所以
                # 没抓到。这里把后台作业换成假的，走完整点击→回调链路。
                try:
                    _o_save, _o_run, _o_bound = (core.save_config,
                                                 core.run_once,
                                                 core.fetch_bound_operator)
                    core.save_config = lambda cfg: None
                    core.run_once = lambda cfg: {
                        "ok": True, "state": core.ONLINE, "message": "已联网"}
                    core.fetch_bound_operator = lambda cfg: None
                    app._collect_config = lambda: dict(
                        app.cfg, username="smoke", password="smoke")
                    app.on_save_login()          # 这里抛错就是入口又写坏了
                    _deadline = time.time() + 5
                    while time.time() < _deadline and (
                            app.authenticating or app._busy):
                        root.update()
                        time.sleep(0.02)
                    root.update()
                    _click_ok = (not app.authenticating and not app._busy
                                 and not app.btn_login._loading
                                 and app.btn_login._text == "保存并立即登录"
                                 and not app.btn_login.find_withtag("spinner"))
                    check("login_click_no_error", _click_ok,
                          "auth=%s busy=%s loading=%s text=%r" % (
                              app.authenticating, app._busy,
                              app.btn_login._loading, app.btn_login._text))
                except Exception as _e:
                    check("login_click_no_error", False, repr(_e))
                finally:
                    core.save_config, core.run_once = _o_save, _o_run
                    core.fetch_bound_operator = _o_bound
                    # 必须删掉实例级的 _collect_config 覆盖：它是绑在实例字典上的，
                    # 不还原的话后面所有用例拿到的都是这份假配置（本轮保存参数用例
                    # 就因此误报"45 被改成 30"）。删掉即恢复类方法。
                    try:
                        del app._collect_config
                    except AttributeError:
                        pass

                # ---- 结果行必须能落到"启动卡" ----
                # _RESULT_LABELS 曾被误放进方法体（局部变量），self._RESULT_LABELS
                # 每次 AttributeError -> 开机自启的回执永远显示不出来。
                try:
                    app._set_result("冒烟：开机自启回执", C_TEXT_2, target="boot")
                    _boot_lbl = getattr(app, "lbl_result_boot", None)
                    _boot_ok = (isinstance(app._RESULT_LABELS, dict)
                                and _boot_lbl is not None
                                and "冒烟" in str(_boot_lbl.cget("text")))
                    app._set_result("", target="boot")
                    check("boot_result_label", _boot_ok,
                          "labels=%r lbl=%r" % (getattr(app, "_RESULT_LABELS", None),
                                                _boot_lbl))
                except Exception as _e:
                    check("boot_result_label", False, repr(_e))

                # ---- 设置页参数：界面显示 == 磁盘生效值，且有保存入口 ----
                # 这一页原先没有保存按钮，改完不落盘，界面上看到的和实际在用的是
                # 两套；customPageId/nasIp 以前也只改内存不写盘。这里一并钉住。
                try:
                    _disk = core.load_config()
                    # 先回填一次：冒烟前面的探测用例把 var_portal 改成了假地址，
                    # 直接比会误报；顺带也验证 _load_into_ui 能把磁盘值灌回界面。
                    app._load_into_ui()
                    root.update()
                    _mism = []
                    for _label, _attr, _key in getattr(app, "_param_meta", ()):
                        _v = getattr(app, _attr, None)
                        if _v is None:
                            _mism.append((_key, "<no var>"))
                        elif str(_v.get()) != str(_disk.get(_key)):
                            _mism.append((_key, _v.get(), _disk.get(_key)))
                    check("settings_params_match_disk", not _mism, str(_mism))
                    # 保存入口必须存在；越界输入必须被夹回合法范围（而不是静默按旧值跑）
                    _has_entry = (hasattr(app, "on_save_params")
                                  and getattr(app, "btn_save_params", None) is not None)
                    _old_backoff = app.var_backoff.get()
                    app.var_backoff.set("3")
                    _clamped = app._collect_config()["max_backoff_seconds"] >= 10
                    app.var_backoff.set(_old_backoff)
                    check("settings_save_entry", _has_entry and _clamped,
                          "entry=%s clamp=%s" % (_has_entry, _clamped))
                except Exception as _e:
                    for _n in ("settings_params_match_disk", "settings_save_entry"):
                        check(_n, False, repr(_e))

                # ---- 「保存参数」必须真的写进 config.json（端到端）----
                # 把 CONFIG_PATH 临时指到临时文件：验证写入链路，又不碰用户的配置。
                try:
                    _o_cfgp = core.CONFIG_PATH
                    _tmp_cfg = os.path.join(tempfile.gettempdir(),
                                            "campus_smoke_cfg.json")
                    core.CONFIG_PATH = _tmp_cfg
                    try:
                        _old_iv = app.var_interval.get()
                        app.var_interval.set("45")
                        app.on_save_params()
                        _t0 = time.time()
                        while time.time() - _t0 < 5 and "save_params" in app._busy_ops:
                            root.update()
                            time.sleep(0.02)
                        root.update()
                        with open(_tmp_cfg, encoding="utf-8") as _f:
                            _saved = json.load(_f)
                        _txt = str(app.lbl_params_status.cget("text"))
                        _ok = (_saved.get("check_interval_seconds") == 45
                               and "已保存" in _txt
                               and not app.btn_save_params._loading)
                        app.var_interval.set(_old_iv)
                        check("settings_save_writes_disk", _ok,
                              "saved=%r status=%r loading=%s" % (
                                  _saved.get("check_interval_seconds"), _txt,
                                  app.btn_save_params._loading))
                    finally:
                        core.CONFIG_PATH = _o_cfgp
                        try:
                            os.remove(_tmp_cfg)
                        except OSError:
                            pass
                except Exception as _e:
                    check("settings_save_writes_disk", False, repr(_e))

                # ---- 五个已修 bug 的回归断言 ----
                # 开机自启：_build_register_ps 曾经引用了未定义的
                # NET_EVENT_SUBSCRIPTION，每次都抛 NameError，功能从来没成功过
                # （界面表现是点开关后立刻回弹、连 UAC 都不弹）。
                try:
                    _ps = _build_register_ps("C:\\x\\a.exe", "--daemon",
                                             "C:\\x", TASK_NAME)
                    _ps_ok = ("REGISTER_OK" in _ps
                              and "EventID=10000" in _ps
                              and "Register-ScheduledTask" in _ps)
                    _ps_err = ""
                except Exception as e:
                    _ps_ok, _ps_err = False, repr(e)
                check("autostart_script_builds", _ps_ok, _ps_err)
                # 免 UAC 兜底路径：注册表 Run 的 写->读->删 完整往返必须可用。
                # 这是开机自启"永不弹 UAC"的最后一道保险，一旦它在 frozen 环境里
                # 失效（比如忘打 winreg），开关就会退回需要提权的旧路径而无人察觉。
                _probe = REG_RUN_VALUE + "__smoketest"
                try:
                    _rr = [_reg_run_set(_probe), _reg_run_exists(_probe),
                           _reg_run_delete(_probe), not _reg_run_exists(_probe)]
                    check("registry_autostart_fallback_works", all(_rr), str(_rr))
                except Exception as _e:
                    check("registry_autostart_fallback_works", False, repr(_e))
                # 任务栏图标：ico 必须打进包（iconbitmap 走窗口类图标，任务栏才
                # 一定同步）。曾漏打包 app.ico -> frozen 下找不到 -> 任务栏羽毛。
                _base = (getattr(sys, "_MEIPASS", None)
                         or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "..", "assets"))
                try:
                    _listing = str(sorted(os.listdir(os.path.join(_base, "assets"))))
                except Exception as e:
                    _listing = "listdir failed: %r" % (e,)
                check("icon_resources_packaged",
                      asset_path("app.ico") is not None
                      and asset_path("app.png") is not None,
                      "base=%s assets=%s" % (_base, _listing))
                # 托盘通知（notify_win）：它是在 login_core 里**惰性 import** 的，
                # frozen 下一旦漏打包就是 ModuleNotFoundError —— 而那条路是
                # "尽力而为、失败静默"，线上表现只是"通知永远不弹"，不会报错，
                # 所以必须在这里钉住。结构体尺寸也要验证：NOTIFYICONDATAW 少一个
                # 字段，Windows 照样返回成功但气泡不显示。
                try:
                    import notify_win as _nw
                    _nw_sizes = (_nw.ctypes.sizeof(_nw.NOTIFYICONDATAW),
                                 _nw.ctypes.sizeof(_nw.MSG),
                                 _nw.ctypes.sizeof(_nw.WNDCLASSEXW))
                    _nw_avail, _nw_err = _nw.available(), ""
                except Exception as _e:
                    _nw_sizes, _nw_avail, _nw_err = (), False, repr(_e)
                # 只在 64 位断言具体数值（shellapi.h / winuser.h 的既定布局）
                _nw_want = ((976, 48, 80)
                            if _nw_sizes and _nw_sizes[0] > 500 else None)
                check("notify_win_ready",
                      bool(_nw_avail) and (_nw_want is None
                                           or _nw_sizes == _nw_want),
                      "sizes=%s want=%s err=%s" % (_nw_sizes, _nw_want, _nw_err))
                # 开机自启必须与后台保活**彻底独立**：切换自启不能去动保活的运行
                # 状态。文案上，这一节只允许在"提示：…「启动后台保活」按钮"那条
                # 功能注释里点名按钮（用户要求加的注释），其余文案仍不许出现"保活"。
                _boot_texts = collect_texts(app.autostart_outer)

                def _boot_text_bad(t):
                    return ("保活" in t
                            and not (t.startswith("提示：")
                                     and "「启动后台保活」按钮" in t))
                _boot_bad = [t for t in _boot_texts if _boot_text_bad(t)]
                check("autostart_card_says_nothing_about_keepalive",
                      not _boot_bad, str(_boot_bad))
                # 全界面文案不许含 U+FFFD 替换符（源码文件曾被损坏出"保���在线"
                # 式乱码，界面上显示为菱形问号；此断言守住不回归）
                _mojibake = [t for t in collect_texts(app.root) if "\ufffd" in t]
                check("no_replacement_char_in_ui_texts",
                      not _mojibake, str(_mojibake[:5]))
                _daemon_before = app.daemon_ok
                _keep_result_before = app.lbl_result.cget("text")
                app.autostart_on = True             # 模拟切一下自启状态
                app._set_autostart_ui(True)
                check("autostart_toggle_does_not_touch_keepalive",
                      app.daemon_ok == _daemon_before
                      and app.lbl_result.cget("text") == _keep_result_before,
                      "daemon_ok=%s 保活结果行=%r"
                      % (app.daemon_ok, app.lbl_result.cget("text")))
                app.autostart_on = False
                app._set_autostart_ui(False)
                # 密码框后置区：图标不能压在「显示」文字上
                _icon_bb = None
                _text_bb = None
                for _iid in app.ent_password.find_withtag("trailing"):
                    _bb = app.ent_password.bbox(_iid)
                    if _bb is None:
                        continue
                    if (app.ent_password.type(_iid) == "image"
                            and (_bb[2] - _bb[0]) <= 32):
                        _icon_bb = _bb
                    elif app.ent_password.type(_iid) == "text":
                        _text_bb = _bb
                check("pwd_trailing_no_overlap",
                      (_icon_bb is None or _text_bb is None
                       or _icon_bb[2] <= _text_bb[0]),
                      "icon=%s text=%s" % (_icon_bb, _text_bb))
                # PID 文件判定的三态：查不出来时不能当"已死"。原来的实现据此删掉
                # app.pid，可 daemon 还活着、互斥量还在它手里，于是留下"锁被占着、
                # PID 文件却没了"的矛盾状态，之后每次启动保活都刷两行红 ERROR。
                # 这里直接测决策逻辑：把"进程还活着"和"身份查不出来"都模拟出来,
                # 看它会不会保住 app.pid。
                _orig_is_ours = core._is_our_daemon
                _orig_exists = core._process_alive
                _pid_path = core.PID_PATH
                _pid_backup = None
                if os.path.exists(_pid_path):
                    try:
                        with open(_pid_path, "r") as _f:
                            _pid_backup = _f.read()
                    except OSError:
                        pass
                _kept = False
                try:
                    with open(_pid_path, "w") as _f:
                        _f.write(str(os.getpid() + 100000))   # 只要不是本进程
                    core._process_alive = lambda pid: True        # 假装还活着
                    core._is_our_daemon = lambda pid: None         # 但身份查不出来
                    core.daemon_running()
                    _kept = os.path.exists(_pid_path)
                except Exception:
                    _kept = False
                finally:
                    core._is_our_daemon = _orig_is_ours
                    core._process_alive = _orig_exists
                    try:
                        if _pid_backup is not None:
                            with open(_pid_path, "w") as _f:
                                _f.write(_pid_backup)
                        elif os.path.exists(_pid_path):
                            os.remove(_pid_path)
                    except OSError:
                        pass
                check("pid_survives_unverifiable_check", _kept,
                      "身份查不出来时把 app.pid 删了（原 bug）")
                # GUI 单实例：锁被占时要能识别出来
                check("gui_single_instance_api",
                      hasattr(core, "acquire_gui_instance_lock")
                      and hasattr(core, "focus_existing_window"))
                # 主卡最窄宽度下：文案不得压到分隔线/指标上
                _saved_geom = root.geometry()
                root.geometry("%dx%d" % (_px(WIN_MIN_W), _px(WIN_H)))
                root.update_idletasks()
                root.update()
                _lines = [app.hero.bbox(i) for i in app.hero.find_all()
                          if app.hero.type(i) == "line"]
                _subs = [app.hero.bbox(i) for i in app.hero.find_all()
                         if app.hero.type(i) == "text"
                         and "ePortal" in app.hero.itemcget(i, "text")]
                _ipl = [app.hero.bbox(i) for i in app.hero.find_all()
                        if app.hero.type(i) == "text"
                        and app.hero.itemcget(i, "text") == "IP 地址"]
                _hero_ok = True
                _detail = ""
                if _subs:
                    _sr = _subs[0][2]
                    if _lines:
                        _hero_ok = _hero_ok and _sr <= _lines[0][0]
                        _detail += "sub_right=%d div=%d " % (_sr, _lines[0][0])
                    if _ipl:
                        _hero_ok = _hero_ok and _sr <= _ipl[0][0]
                        _detail += "ip_left=%d" % _ipl[0][0]
                check("hero_no_overlap_min_width", _hero_ok, _detail)
                try:
                    root.geometry(_saved_geom)
                    root.update_idletasks()
                    root.update()
                except Exception:
                    pass

                # 长回执不能把卡片撑高：结果行预留了固定行数，文本再长也只能在预留
                # 区域里换行/被裁。原来出过事故——一条很长的开机自启回执把卡片顶高，
                # 把底部的「断开连接」按钮挤出了可视区。
                app._switch_page("status")
                root.update_idletasks()
                root.update()
                # 主按钮/结果行已移进左账号卡（用户要求登录功能整合到左边），
                # "长回执不撑卡"断言跟着改测账号卡
                _before_h = app.account_outer.winfo_reqheight()
                app._set_result(
                    "开机自启已设置（经提权确认）：登录 Windows 或网络连上时，"
                    "自动在后台保活联网（任务名 CampusLoginAppAutoStart）"
                    "——这是一条故意很长的回执，用来验证不会把卡片撑高")
                root.update_idletasks()
                root.update()
                _after_h = app.account_outer.winfo_reqheight()
                check("long_result_does_not_grow_card", _after_h == _before_h,
                      "%d -> %d" % (_before_h, _after_h))
                app._set_result("")

                # 导航顺序：连接 / 日志 / 设置（设置只剩低频项，放最后）
                _nav_ids = [n.page_id for n in app._navs]
                check("nav_order", _nav_ids == [p[0] for p in PAGES],
                      str(_nav_ids))
                # 开机自启现在并进「登录与保活」卡里作为一小节（用户指定的排版），
                # 但它仍然是独立的一节：父容器必须是登录卡的 body。
                check("autostart_section_inside_login_card",
                      app.autostart_outer.master is app.login_outer.body,
                      "parent=%s" % app.autostart_outer.master)
                check("page_switch", all(p["nav_selected"] == p["page"]
                                         for p in report["pages"]),
                      str([p for p in report["pages"]
                           if p["nav_selected"] != p["page"]]))
                # 主卡/主按钮现在是抗锯齿位图（Pillow 路径），无 Pillow 时退回
                # 多条 line/polygon——两种形态都算通过，只要画出来了东西。
                check("hero_painted", len(app.hero.find_withtag("hero")) >= 1,
                      len(app.hero.find_withtag("hero")))
                check("login_btn_painted",
                      len(app.btn_login.find_withtag("grad")) >= 1,
                      len(app.btn_login.find_withtag("grad")))
                check("card_painted",
                      len(app.account_outer.find_withtag("card")) >= 1)
                check("segmented", app.seg_service.get() in SERVICE_ORDER)
                _selected_btn = next(
                    (b for b, opt in zip(app.seg_service.buttons, SERVICE_ORDER)
                     if opt == app.seg_service.get()), None)
                check("segmented_selected_fill",
                      _selected_btn is not None and _selected_btn._fill == C_PRIMARY,
                      "selected=%s fill=%s" % (app.seg_service.get(),
                                              _selected_btn._fill if _selected_btn else None))
                check("toggle_exists", bool(app.toggle_auto.winfo_exists()))
                check("entry_borderless",
                      app.style.layout("Flat.TEntry") != "",
                      "Flat.TEntry 没有 layout")
                check("password_toggle",
                      (app.ent_password.set_show(""), True)[1])
                app._toggle_password()
                check("password_visible_switch",
                      app.ent_password.entry.cget("show") == "")
                app._toggle_password()

                # ---- 可见性 / 几何断言（原来只断言"控件存在"，漏掉了整个输入框
                #      消失、文字被裁这类"看起来坏了"的问题，这里补上）----
                app._switch_page("status")
                root.update_idletasks()
                root.update()
                report["status_hero_h"] = app.hero.winfo_height()
                report["status_top_row_h"] = app.status_grid.winfo_height()
                report["status_log_h"] = app.log_outer.winfo_height()
                # 版式：上排两张卡底边必须齐平（用户指定），日志是唯一伸缩的部分
                _acc_bottom = (app.account_outer.winfo_y()
                               + app.account_outer.winfo_height())
                _log_bottom = (app.login_outer.winfo_y()
                               + app.login_outer.winfo_height())
                check("top_cards_bottom_aligned",
                      abs(_acc_bottom - _log_bottom) <= 2,
                      "账号卡底=%d 登录卡底=%d" % (_acc_bottom, _log_bottom))
                # 上排两张卡必须**装得下自己的内容**：这两张卡是靠被拉高来对齐底边
                # 的，一旦请求高度丢了（画布退回 Tk 默认 265px），行高就会小于内容
                # 高度，两张卡一起被压扁、下面的控件全被裁掉。这条断言就是那次截图
                # 事故的守门员。
                for _nm, _c in (("账号卡", app.account_outer),
                                ("登录卡", app.login_outer)):
                    _need = (_c.body.winfo_reqheight() + 2 * _c._pad
                             + 2 * _c._margin())
                    check("top_card_%s_fits_content" % _nm,
                          _c.winfo_height() + 1 >= _need,
                          "%s 实际 %d < 内容需要 %d"
                          % (_nm, _c.winfo_height(), _need))
                # 上排两卡底部不能剩一大片空白：两卡被拉到同高，矮的那张多出来的
                # 空间靠卡内「可伸缩空档」平摊到各段之间。空档若失效（或内容帧没被
                # 拉到可用高度），空白就会全堆在底部——用户明确抱怨过「空白处太大」。
                for _nm, _c in (("账号卡", app.account_outer),
                                ("登录卡", app.login_outer)):
                    _kids = _c.body.winfo_children()
                    if not _kids:
                        continue
                    _last = max(_kids, key=lambda x: x.winfo_y())
                    # 量"卡片内容区底部"与"最后一个子控件底部"的差——空白是出现在
                    # **画布**里（body 之外的灰色区域），不是 body 内部。第一版拿
                    # body 的高度去减，body 自己会缩到内容高、差值恒为 0，断言是空的
                    # （拿坏版本跑一遍才发现）。
                    _off = _c._margin() + _c._pad          # body 在卡片里的偏移
                    _inner_bottom = _c.winfo_height() - _off
                    _content_bottom = _off + _last.winfo_y() + _last.winfo_height()
                    _blank = _inner_bottom - _content_bottom
                    check("top_card_%s_no_big_blank" % _nm, _blank <= _px(20),
                          "%s 底部空 %d px" % (_nm, _blank))
                # 日志卡必须留得下几行——它是唯一被压的部分，不能压成 0
                check("log_card_has_room",
                      app.log_outer.winfo_height() >= _px(110),
                      "日志卡只有 %d px" % app.log_outer.winfo_height())
                # 日志行数要跟着高度走：把它撑高，行数必须变多
                _rows_before = len(app.log_rows.winfo_children())
                _h_before = app.log_outer.winfo_height()
                _win_before = root.winfo_height()
                root.geometry("%dx%d" % (_px(WIN_W), _px(WIN_H) + 160))
                # 改窗口尺寸会触发"拖拽保护"：先冻结内容布局（place），130ms 后才解冻
                # 并按最终尺寸重排。这里必须等它真的解冻，否则量到的是冻结时的旧高度
                # （第一版就是这么误判成"日志没长高"的）。
                for _ in range(24):
                    root.update()
                    time.sleep(0.03)
                _rows_after = len(app.log_rows.winfo_children())
                check("log_rows_follow_height",
                      _rows_after >= _rows_before,
                      "加高前 %d 行 -> 加高后 %d 行" % (_rows_before, _rows_after))
                check("hero_keeps_height_when_taller",
                      app.hero.winfo_height() == report["status_hero_h"],
                      "主卡高度从 %d 变成 %d（上下拉伸应只影响日志）"
                      % (report["status_hero_h"], app.hero.winfo_height()))
                check("top_row_keeps_height_when_taller",
                      app.status_grid.winfo_height() == report["status_top_row_h"],
                      "上排高度从 %d 变成 %d"
                      % (report["status_top_row_h"],
                         app.status_grid.winfo_height()))
                check("log_grew_when_taller",
                      app.log_outer.winfo_height() > _h_before,
                      "日志卡 %d -> %d（窗口 %d -> %d, DPI %s）"
                      % (_h_before, app.log_outer.winfo_height(),
                         _win_before, root.winfo_height(), DPI_SCALE))
                root.geometry("%dx%d" % (_px(WIN_W), _px(WIN_H)))
                for _ in range(24):
                    root.update()
                    time.sleep(0.03)
                # 在线时长已按需求移除：主卡、侧边状态块、任何 Label 都不该再出现
                texts = collect_texts(app.main)
                check("no_online_duration",
                      not any("在线时长" in t for t in texts),
                      str([t for t in texts if "在线时长" in t]))
                # 运营商提示那行曾经被卡片右边裁掉（文字被截成半句）
                hint = app.account_outer.body.winfo_children()[-1]
                check("op_hint_not_clipped",
                      hint.winfo_reqwidth() <= app.account_outer.body.winfo_width() + 1,
                      "hint=%d body=%d" % (hint.winfo_reqwidth(),
                                           app.account_outer.body.winfo_width()))
                check("side_status_painted",
                      len(app._side.find_all()) >= 1,
                      len(app._side.find_all()))
                # 通检：卡片里**任何一行没设 wraplength 的文字**都不该被压窄。
                # 判据是"实际宽度 < 请求宽度"——被压窄说明 packer 没分给它足够空间，
                # 文字会被裁成半句（如"登录 Windows 时自动启…"）。
                # **不能**拿"父容器宽度"当判据：文字常常是被**兄弟控件**（开关、pill）
                # 挤掉的，父容器本身够宽，那样写会漏掉——我第一版就是这么写的，拿故意
                # 改坏的版本跑一遍是绿灯（断言是空的），换成本判据才抓得住。
                _clipped = []

                def _scan(w):
                    for _c in w.winfo_children():
                        if _c.winfo_class() == "Label":
                            try:
                                wl = int(float(_c.cget("wraplength")))
                            except Exception:
                                wl = 0
                            if (wl <= 0 and _c.winfo_width() > 1
                                    and _c.winfo_width() + 1 < _c.winfo_reqwidth()):
                                _clipped.append("%r(实际%d<需要%d)"
                                                % (_c.cget("text")[:14],
                                                   _c.winfo_width(),
                                                   _c.winfo_reqwidth()))
                        _scan(_c)

                for _card in (app.account_outer, app.login_outer,
                              app.log_outer, app.autostart_outer):
                    _scan(getattr(_card, "body", _card))
                check("no_clipped_card_text", not _clipped, str(_clipped[:4]))

                # 运营商分段交互
                app.seg_service.select("电信")
                check("segmented_switch", app.seg_service.get() == "电信")
                app.seg_service.set("移动")
                # 写报告
                report["pass"] = not report["errors"]
                if report_path:
                    os.makedirs(os.path.dirname(report_path), exist_ok=True)
                    with open(report_path, "w", encoding="utf-8") as f:
                        json.dump(report, f, ensure_ascii=False, indent=2)
                print("SMOKE_PASS" if report["pass"] else "SMOKE_FAIL")
                print("report: %s" % report_path)
                root.destroy()
        except Exception as e:
            report["errors"].append("step %d: %r" % (i, e))
            report["pass"] = False
            try:
                if report_path:
                    os.makedirs(os.path.dirname(report_path), exist_ok=True)
                    with open(report_path, "w", encoding="utf-8") as f:
                        json.dump(report, f, ensure_ascii=False, indent=2)
            except Exception:
                pass
            print("SMOKE_FAIL")
            try:
                root.destroy()
            except Exception:
                pass

    root.after(500, lambda: step())
    root.mainloop()
    return 0 if report.get("pass") else 2


def run_gui():
    """启动图形界面。tkinter 不可用时给出友好提示。
    注意：DPI 感知必须在创建 Tk root 之前声明，否则 125%/150% 缩放下界面会发虚。"""
    if not HAS_TK:
        print("[错误] 当前 Python 缺少 tkinter，无法启动图形界面。")
        print("命令行模式仍可用：--daemon / --once / --probe")
        return 1
    # 单实例：已经有界面在跑，就把那个窗口拉到前台，本次直接退出。
    # 没有这道闸的话，双击两次会起两个完整界面——各自显示各自的状态、各自启停
    # 保活，还会互相抢 daemon 的锁。锁句柄**故意不释放**，held 到进程退出为止。
    if core.acquire_gui_instance_lock() is None:
        core.focus_existing_window()
        return 0
    enable_dpi_awareness()
    core.setup_logging(console=False)
    # 启动后在后台线程回收历史解包残留（_MEI 目录），绝不阻塞界面
    threading.Thread(target=core.cleanup_stale_mei_dirs, daemon=True).start()
    root = tk.Tk()
    # 按 DPI 缩放比例调整 Tk 的字体度量（points->pixels），配合 DPI 感知实现清晰渲染
    try:
        root.tk.call("tk", "scaling", 1.3333 * DPI_SCALE)
    except Exception:
        pass

    # 兜底异常出口：--windowed 打包后没有控制台，Tk 回调里抛出的异常原本只写进一个
    # 看不见的 stderr，界面表现就是「点了没反应」，排查时无从下手。这里落进 login.log，
    # 并弹一次提示（只弹一次，否则同一个 bug 会每帧刷窗把界面顶死）。
    def _on_callback_error(exc, val, tb):
        logging.error("界面回调异常", exc_info=(exc, val, tb))
        if getattr(root, "_cb_error_shown", False):
            return
        root._cb_error_shown = True
        try:
            messagebox.showerror(
                "界面出现异常",
                "界面某处出错，部分功能可能不可用。\n\n%s: %s\n\n"
                "详细信息已写入 login.log；重启程序通常即可恢复。"
                % (getattr(exc, "__name__", "Exception"), val))
        except Exception:
            pass

    root.report_callback_exception = _on_callback_error
    app = CampusLoginApp(root)  # noqa: F841（持有引用，防止被垃圾回收）
    root.mainloop()
    return 0


# ============================ CLI 入口 ============================

def run_daemon_cli():
    """无界面后台保活：单实例锁 + PID 文件 + run_forever 循环。"""
    ensure_console_output()
    core.setup_logging()
    # daemon 启动时先回收历史解包残留（%TEMP% 下的 _MEI 目录）
    core.cleanup_stale_mei_dirs()
    try:
        cfg = core.load_config()
    except core.ConfigError as e:
        logging.error("%s", e)
        return 1
    if not (str(cfg.get("username", "")).strip() and str(cfg.get("password", ""))):
        logging.error("尚未配置账号密码：请先运行图形界面填写并「保存并立即登录」")
        return 1
    logging.info("校园网一键登录 daemon 启动（工作目录 %s）", core.BASE_DIR)
    # 1.4：把自身降到「低于正常」优先级，避免长时间保活与前台应用抢 CPU。
    # 失败静默降级，不影响保活。
    if core.set_low_priority():
        logging.info("已将进程优先级设为「低于正常」（当前：%s）", core.get_priority_class())
    else:
        logging.info("降低进程优先级失败，按默认优先级运行（不影响保活）")
    # Win11 节流模式（EcoQoS）：保活进程只是定期醒来做一次探测，降频无体感，
    # 却能明显降低长期后台能耗。不支持的旧系统上调用失败，静默降级。
    if core.set_power_throttling(True):
        logging.info("已开启后台节流模式（EcoQoS），降低保活期间的能耗")
    try:
        # 单文件打包后父进程是 PyInstaller 的 bootloader 壳（负责持有独立解包
        # 目录，必须常驻），启动完就没用了——顺手把它的物理内存也回收一次。
        ppid = os.getppid()
        if ppid and ppid > 1:
            core.trim_working_set(ppid)
    except Exception:
        pass  # 拿不到/不支持就算了，纯粹是锦上添花
    if core.acquire_single_instance_lock() is None:
        # 已有实例在运行 = 正常情况（多开 / 计划任务与手动启动撞车），不是错误。
        # 原来这里 return 1 并打 ERROR，界面上就多两行红字，用户以为出了故障。
        logging.info("已有后台保活实例在运行，本次不再重复启动（正常，非错误）")
        return 0
    core.write_pid_file()
    exit_code = 0
    try:
        core.run_forever(cfg)
    except KeyboardInterrupt:
        logging.info("保活循环被中断，准备退出")
    except Exception:
        logging.exception("保活循环异常退出")
        exit_code = 1
    finally:
        # 正常/异常退出都汇总打印一次统计（1.6），供 QA 核对优化效果
        logging.info("%s", core.stats_line())
        core.remove_pid_file()
    return exit_code


def run_once_cli():
    """单次「检测 + 按需登录」后退出（测试/排错用）。"""
    ensure_console_output()
    core.setup_logging()
    try:
        cfg = core.load_config()
    except core.ConfigError as e:
        print("[错误] %s" % e)
        return 1
    result = core.run_once(cfg)
    print("网络状态：%s" % core.STATE_TEXT.get(result["state"], result["state"]))
    print("结论：%s" % result["message"])
    return 0 if result["ok"] else 1


def run_probe_cli():
    """诊断输出：网络状态、网关可达性、AES 自检、NAS 跳转链。"""
    ensure_console_output()
    core.setup_logging()
    # 顺带触发一次历史解包残留回收，便于验证 / 日常清理
    n, freed = core.cleanup_stale_mei_dirs()
    print("历史解包残留清理：删除 %d 个目录，释放 %.1f MB" % (n, freed / (1024.0 * 1024.0)))
    try:
        cfg = core.load_config()
    except core.ConfigError as e:
        print("[警告] %s（本次改用默认参数）" % e)
        cfg = dict(core.DEFAULT_CONFIG)
    core.run_probe(cfg)
    return 0


def run_devices_cli():
    """诊断：当前哪些设备在用这个账号（在线设备 MAC / 类型 / IP）+ 本机比对结果。

    只读查询：只登录自助服务看在线设备清单，绝不调用踢下线/解绑这类改动接口。
    """
    ensure_console_output()
    core.setup_logging()
    try:
        cfg = core.load_config()
    except core.ConfigError as e:
        print("[错误] %s" % e)
        return 1
    try:
        host = str(cfg.get("portal_host", ""))
        macs = sorted(core.local_mac_addresses())
        print("本机 MAC：%s" % ("、".join(core.pretty_mac(m) for m in macs) or "未知"))
        ip = core.local_gateway_ip(host)
        print("访问网关所用 IP：%s" % (ip or "未知"))
        devices = core.fetch_online_devices(cfg, use_cache=False)
        if devices is None:
            print()
            print("在线设备：查不到（自助服务不通 / 接口变了）→ 保活会退回窗口推断")
            return 0
        verdict, info = core.classify_online_devices(devices, my_ip=ip)
        print()
        print("在线设备 %d 台：" % len(devices))
        for d in devices:
            print("  MAC=%s  IP=%-15s 类型=%-4s 当前设备=%-5s 名称=%-8s 已在线=%s"
                  % (core.pretty_mac(d["mac"]) or "-", d["ip"] or "-",
                     d["type"] or "-", "是" if d["current"] else "否",
                     d["name"] or "-", d["duration"] or "-"))
        print()
        print("判定：%s —— %s" % (verdict, info or "无"))
        if verdict == core.DEV_OTHER:
            print("→ 保活会主动让位，不去抢线")
        elif verdict in (core.DEV_MINE, core.DEV_NONE):
            print("→ 保活会正常认证")
        return 0
    except Exception as e:
        print("[错误] 查询失败：%s" % e)
        return 1


def run_start_keepalive_cli():
    """等价于 GUI「启动后台保活」按钮：拉起 --daemon 后本进程立即退出（不驻留）。

    便于用快捷方式 / 脚本启动后台保活。退出码 0=已拉起，1=配置缺失或失败。
    """
    ensure_console_output()
    core.setup_logging()
    try:
        cfg = core.load_config()
    except core.ConfigError as e:
        print("[错误] %s" % e)
        return 1
    if not (str(cfg.get("username", "")).strip() and str(cfg.get("password", ""))):
        print("[错误] 尚未配置账号密码：请先运行图形界面填写并「保存并立即登录」")
        return 1
    spawn_daemon()
    print("已拉起后台保活进程（--daemon），本进程随即退出。")
    return 0


def run_stop_keepalive_cli():
    """等价于 GUI「停止后台保活」按钮（复用 core.stop_daemon()）。

    退出码：0 = 已停止或本来就没有在运行（幂等成功）；1 = 停止失败。
    """
    ensure_console_output()
    core.setup_logging()
    ok, msg = core.stop_daemon()
    print(msg)
    if ok:
        return 0
    # stop_daemon 在「本来就没有运行」时也返回 False；这属于幂等成功
    return 0 if not core.daemon_running() else 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # 存活标记：所有模式启动即写入（仅 frozen 时生效），供清理器识别「目录仍被
    # 活进程使用」；正常退出在 finally 里 best-effort 删除（P0 误删修复）。
    core.write_alive_marker()
    try:
        if "--version" in argv:
            # 排查用：不加载 tkinter，直接打印版本与内置更新地址
            repo = UPDATE_REPO or "(未设置)"
            print("CampusLogin %s" % APP_VERSION)
            print("更新仓库: %s" % repo)
            print("配置文件: %s" % core.CONFIG_PATH)
            return 0
        if "--daemon" in argv:
            return run_daemon_cli()
        if "--once" in argv:
            return run_once_cli()
        if "--probe" in argv:
            return run_probe_cli()
        if "--devices" in argv:
            return run_devices_cli()
        if "--start-keepalive" in argv:
            return run_start_keepalive_cli()
        if "--stop-keepalive" in argv:
            return run_stop_keepalive_cli()
        if "--gui-smoke" in argv:
            # 界面自检（QA 用）：报告写到 %TEMP%，不弹消息框、不发登录请求
            report = os.path.join(tempfile.gettempdir(), "campuslogin_smoke",
                                  "smoke_report.json")
            shots = None if getattr(sys, "frozen", False) else \
                os.path.dirname(report)
            return run_gui_smoke(report_path=report, screenshot_dir=shots)
        return run_gui()
    finally:
        core.remove_alive_marker()


if __name__ == "__main__":
    sys.exit(main())
