#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
校园网一键登录 —— 认证核心模块（login_core）
====================================================================
由客户原有脚本 D:\\first-cc\\campus-login\\campus_login.py（只读参考）移植而来的
锐捷 ePortal（成都信息工程大学）自动认证逻辑，自包含、仅依赖 Python 标准库。

完整保留的原有能力：
  * 纯标准库 AES-128-ECB + PKCS#7 实现（含 S 盒生成），并保留
    FIPS-197 / NIST SP 800-38A 公开标准向量自检 aes_self_test()；
  * 网络三态判定 classify_network（在线 / 未认证 / 本地断网）、
    并行公网 TCP 探测 public_tcp_ok、HTTP 内容校验 http_content_ok（防“假联网”）；
  * 完整登录流程 run_login_flow：访问 123.123.123.123 触发 NAS 跳转拿
    sessionId -> getCurrentNode -> queryTerminalInfo 补 userIp -> 打开 CAS 页面
    解析 login-croypto（AES 密钥）与 login-page-flowkey（execution）->
    AES 加密密码提交 -> serviceLogin 选择运营商上线；
  * 保活循环 run_forever：30 秒检测 + 连败指数退避（30->60->120->240->300 封顶）；
  * Windows 命名单实例锁 acquire_single_instance_lock（CreateMutexW +
    use_last_error 的坑原样保留）。

与原脚本的差异（按本任务要求，两者必须共存互不干扰）：
  * BASE_DIR 在 PyInstaller frozen 模式下取 exe 所在目录，config.json /
    login.log / app.pid 全部落在本程序自己的目录，绝不依赖原程序目录；
  * 单实例互斥量名改为 campus_login_app_singleton_v1，PID 文件改为 app.pid
    （原程序使用 campus_login_singleton / campus_login.pid）；
  * PID 归属校验改为识别 CampusLogin 进程（原程序识别的是 campus_login.py）；
  * 面向 GUI 增加带默认值合并的 load_config()/save_config()、
    结构化返回的 run_once()、daemon 状态查询/停止等辅助函数。

本模块不负责开机自启（计划任务由 app.py 注册，任务名 CampusLoginAppAutoStart）。

对外接口（app.py 使用）：
  load_config() / save_config(cfg) / check_network(cfg) / run_login_flow(cfg)
  run_once(cfg) / run_forever(cfg) / run_probe(cfg) / aes_self_test()
  acquire_single_instance_lock() / write_pid_file() / remove_pid_file()
  daemon_running() / stop_daemon() / read_daemon_pid() / tail_log()
"""

import base64
import ctypes
import http.client
import http.cookiejar
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from logging.handlers import RotatingFileHandler

# ============================ 路径与常量 ============================

# frozen（PyInstaller 打包）时 BASE_DIR = exe 所在目录；源码运行时 = 本文件所在目录。
# 这样 config.json / login.log / app.pid 始终跟随 exe，整个文件夹可直接拷给他人使用。
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
LOG_PATH = os.path.join(BASE_DIR, "login.log")
PID_PATH = os.path.join(BASE_DIR, "app.pid")
# 后台保活把「自动让位」状态写在这里，界面直接读来显示（跨进程的只读状态快照）。
# 不用 config.json 承载：那是用户的持久配置，两边同时写会互相覆盖。
DAEMON_STATE_PATH = os.path.join(BASE_DIR, "daemon_state.json")

# 单实例互斥量名：不得与原程序的 campus_login_singleton 相同，避免两个程序互杀
MUTEX_NAME = "campus_login_app_singleton_v1"
ERROR_ALREADY_EXISTS = 183
# Windows: 子进程不新建控制台窗口（windowed exe / pythonw 环境下必须，否则闪黑框）
CREATE_NO_WINDOW = 0x08000000
# Windows: 分离进程（GUI 关闭后 daemon 不受影响）
DETACHED_PROCESS = 0x00000008

# ============================ 子进程环境清洗（PyInstaller onefile 修复） ============================

# PyInstaller 单文件模式注入的环境变量键名/前缀：子进程若继承这些标记，会误认为
# 自己是父进程的子进程而复用父进程的解包目录 _MEIxxxxxx，导致父进程退出时无法
# 删除该目录（弹 "Failed to remove temporary directory"）。所有 subprocess 调用
# 都必须改用 clean_child_env() 提供的清洗副本。
_PYI_ENV_DROP_KEYS = ("_MEIPASS", "_MEIPASS2")
_PYI_ENV_DROP_PREFIXES = ("_PYI_", "PYINSTALLER_")


def clean_child_env(extra=None):
    """返回一份清洗过的环境变量副本，所有 subprocess 调用都必须用它。

    PyInstaller 单文件模式会把 _MEIPASS/_MEIPASS2/_PYI_*/PYINSTALLER_* 注入环境；
    子进程若继承这些标记，会误认为自己是父进程的子进程而**复用父进程的解包目录**，
    导致父进程退出时无法删除 _MEI 目录（弹 "Failed to remove temporary directory"）。
    同时要把 PATH 里的 _MEI 目录项剔除：PATH/SetDllDirectory 的继承会让子进程
    从父进程的解包目录加载 DLL，同样锁住文件。

    参数：
      extra: 可选的 dict，叠加到清洗后的环境上（同名键覆盖）。
    返回：
      dict —— os.environ 的清洗副本（绝不修改 os.environ 本身）。
    """
    env = {}
    for key, value in os.environ.items():
        if key in _PYI_ENV_DROP_KEYS:
            continue
        if any(key.startswith(p) for p in _PYI_ENV_DROP_PREFIXES):
            continue
        env[key] = value
    # PATH：按 ';' 切分，剔除任何包含 "_MEI" 的路径项（大小写不敏感），再拼回
    path_val = env.get("PATH")
    if path_val:
        parts = [p for p in path_val.split(os.pathsep)
                 if p and "_mei" not in p.lower()]
        env["PATH"] = os.pathsep.join(parts)
    if extra:
        for key, value in extra.items():
            env[key] = value
    return env


# ============================ 历史解包残留回收 ============================

def _looks_like_our_mei_dir(path):
    """判断一个 _MEI* 目录是否属于本程序（PyInstaller onefile 解包目录）。

    只认本程序特有的组合，识别不出归属的一律返回 False（宁可不删也不能误删）：
      * 目录内存在 assets/app.png（本程序 --add-data 打包的界面图标）；或
      * 同时存在 python3*.dll 与 tcl9tk90.dll（本程序随 tkinter 打包的特有组合）。
    """
    try:
        if os.path.exists(os.path.join(path, "assets", "app.png")):
            return True
        try:
            low = {n.lower() for n in os.listdir(path)}
        except OSError:
            return False
        has_python = any(n.startswith("python3") and n.endswith(".dll") for n in low)
        return has_python and ("tcl9tk90.dll" in low)
    except Exception:
        return False


def _dir_size(path):
    """统计目录占用字节数（仅用于日志汇报，失败按 0 计，不影响删除逻辑）。"""
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    continue
    except Exception:
        pass
    return total


# —— 存活标记：目录是否仍被活进程使用（P0 误删修复的核心）——
ALIVE_MARKER_NAME = ".cla_alive"
# 无标记目录的龄期兜底阈值（小时）：旧版本/异常情况没有存活标记，只能靠目录年龄判断
MEI_MIN_AGE_HOURS = 24.0


def write_alive_marker():
    """往当前解包目录写存活标记（内容为本进程 PID）。

    供 cleanup_stale_mei_dirs 判断某个 _MEI* 目录是否仍被活进程使用。frozen
    （PyInstaller 单文件）时 sys._MEIPASS 才存在；源码运行没有解包目录，直接跳过。
    写入失败静默降级：标记写不进只是少一层保护，绝不能因此影响启动。
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return
    try:
        with open(os.path.join(meipass, ALIVE_MARKER_NAME), "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass


def remove_alive_marker():
    """best-effort 删除存活标记（进程正常退出时清理）。失败静默忽略。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return
    try:
        os.remove(os.path.join(meipass, ALIVE_MARKER_NAME))
    except OSError:
        pass


def _read_alive_pid(path):
    """读取存活标记里的 PID；不存在/内容非法返回 None。"""
    try:
        with open(path, "r") as f:
            text = f.read().strip()
    except OSError:
        return None
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _process_alive(pid):
    """判断 Windows 进程 PID 是否存活（OpenProcess 拿非空句柄=存活）。

    安全取向：任何无法确定的情况一律按「存活」处理（宁可不删，不可误删）：
      * OpenProcess 拿到非空句柄 -> 存活；
      * 返回空且错误码是 ERROR_ACCESS_DENIED(5) -> 无法断定已死，按存活跳过；
      * 其余返回空 -> 进程不存在，视为孤儿残留可删；
      * 非 Windows / 调用异常 -> 按存活处理。
    说明：若 PID 已被无关进程复用，这里会误判为「存活」而跳过——这个方向是安全的。
    **但反过来用它来断言"我杀的那个进程还没死"是不成立的**，见
    _open_wait_handle / _target_still_running。
    """
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # PROCESS_QUERY_LIMITED_INFORMATION：最低权限查询，兼容受限进程
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        # ERROR_ACCESS_DENIED = 5：无权限查询不等于进程已死，按存活跳过
        return ctypes.get_last_error() == 5
    except Exception:
        return True


SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0


def _open_wait_handle(pid):
    """打开一个可等待的进程句柄（停止保活用）；打不开返回 None。

    句柄指向**进程对象**而不是 PID 号：即使该 PID 事后被系统回收给了别的进程，
    这个句柄仍然只代表原来那个进程。停止保活必须靠它判断"被我们杀的那个进程"
    是否真的退出了——只看 PID 存活会被 PID 复用骗到。本机实测 PID 回收极频繁
    （甚至能看到子进程的 PID 比它父进程还小），复现时日志里那次成功停止之后
    仍然报出「停止失败：进程 2248」，而 2248 当时已经不是我们的 daemon 了。
    """
    try:
        pid = int(pid)
        if pid <= 0:
            return None
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint]
        # SYNCHRONIZE：允许 WaitForSingleObject；QUERY_LIMITED：兼容受限进程
        return kernel32.OpenProcess(
            SYNCHRONIZE | 0x1000, 0, pid) or None
    except Exception:
        return None


def _target_still_running(pid, handle):
    """停止保活专用：这个目标是否"还没退出"。

    有句柄就等句柄（不受 PID 复用影响）；打不开句柄（已退出/无权限）才退回按
    PID 判断，此时保持"宁可不删"的保守取向。
    """
    if handle:
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.WaitForSingleObject.restype = ctypes.c_uint
            kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            # 已退出 -> WAIT_OBJECT_0；仍在跑 -> WAIT_TIMEOUT(258)
            return kernel32.WaitForSingleObject(handle, 0) != WAIT_OBJECT_0
        except Exception:
            return True
    return _process_alive(pid)


def _close_process_handles(handles):
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        for h in handles:
            if h:
                kernel32.CloseHandle(h)
    except Exception:
        pass



def _dir_age_hours(path):
    """返回目录 mtime 距今的小时数；读取失败按 0（视为很新，保守跳过）。"""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return 0.0
    return max(0.0, (time.time() - mtime) / 3600.0)


def cleanup_stale_mei_dirs():
    """回收 %TEMP% 下属于本程序的历史解包残留（_MEI* 目录）。

    PyInstaller onefile 正常退出时会自动删除自己的 _MEI 目录；但当 daemon 以子进程
    模式复用父进程 _MEI（本次修复前的老行为）或被 taskkill /F 强杀时，目录会残留且
    不再回收。本函数在启动时把这些历史残留收回来，返回 (删除目录数, 释放字节数)。

    安全规则（必须严格遵守，宁可不删也不能误删）：
      1. 只扫 %TEMP%（tempfile.gettempdir()）下的 _MEI* 目录；
      2. 只处理属于本程序的目录（见 _looks_like_our_mei_dir），识别不出归属一律跳过；
      3. 跳过当前进程自己的 sys._MEIPASS（normcase 后比较）；
      4. 存活判定（P0 修复）：目录内若存在 .cla_alive，读出 PID 并用 OpenProcess
         判定该进程是否存活——存活则一律跳过（哪怕它是别的实例）；PID 已死则视为
         孤儿残留可删。目录内若没有标记（旧版本/异常），用龄期兜底：mtime 距今
         不足 24 小时跳过，超过 24 小时才允许删除；
      5. 删除前先做占用探测：os.rename(d, d + ".meiclean")，失败说明目录正被占用
         -> 跳过；rename 成功才 rmtree 改名后的目录（绝不直接 rmtree 原目录，
         避免把正在运行实例的文件删一半导致对方崩溃）；
      6. 全程 try/except 兜底，任何异常都不影响主流程，失败静默忽略。
    """
    deleted = 0
    freed = 0
    try:
        temp_dir = tempfile.gettempdir()
        entries = os.listdir(temp_dir)
    except Exception:
        return 0, 0
    current_meipass = getattr(sys, "_MEIPASS", None)
    if current_meipass:
        try:
            current_meipass = os.path.normcase(os.path.abspath(current_meipass))
        except Exception:
            current_meipass = None
    for name in entries:
        try:
            if not name.startswith("_MEI"):
                continue
            # 上一轮清理失败留下的 .meiclean 残留：它同样以 _MEI 开头，会被再次
            # 命中并再改名成 .meiclean.meiclean；而目标已存在时 os.rename 在
            # Windows 上直接抛 FileExistsError，于是这个目录从此再也清不掉。
            if name.endswith(".meiclean"):
                continue
            path = os.path.join(temp_dir, name)
            if not os.path.isdir(path):
                continue
            if current_meipass and os.path.normcase(os.path.abspath(path)) == current_meipass:
                continue  # 跳过当前进程自己的解包目录
            if not _looks_like_our_mei_dir(path):
                continue  # 无法确认归属 -> 一律跳过（绝不碰别的程序的目录）

            # —— P0 修复：先做存活判定，再决定是否删除 ——
            marker_path = os.path.join(path, ALIVE_MARKER_NAME)
            if os.path.exists(marker_path):
                pid = _read_alive_pid(marker_path)
                # PID 读不出来（标记损坏/为空）：无法判定，按存活跳过（宁可不删）
                if pid is None or _process_alive(pid):
                    continue
                # 有标记但 PID 已死 -> 孤儿残留，继续走下面的删除流程
            elif _dir_age_hours(path) < MEI_MIN_AGE_HOURS:
                # 无标记（旧版本/异常情况）且目录很新：龄期兜底，24 小时内绝不碰
                continue

            size = _dir_size(path)
            renamed = path + ".meiclean"
            # 上一轮可能留下了同名残留，先清掉，否则 rename 撞 FileExistsError
            if os.path.exists(renamed):
                try:
                    shutil.rmtree(renamed)
                except Exception:
                    pass
            if os.path.exists(renamed):
                continue          # 清不掉就别动原目录
            try:
                os.rename(path, renamed)  # 占用探测：rename 失败即目录正被占用
            except OSError:
                continue
            try:
                # 不再用 ignore_errors：那会"删掉能删的、跳过被占用的"，留下一个
                # 残缺目录，而且无论是否真删掉都照样计数——日志显示已清理，实际
                # 是半删。删不干净就把目录换回原名，下次再试。
                shutil.rmtree(renamed)
            except Exception:
                try:
                    os.rename(renamed, path)
                except Exception:
                    pass
                continue
            deleted += 1
            freed += size
        except Exception:
            continue
    if deleted:
        logging.info("清理历史解包残留: 删除 %d 个目录，释放 %.1f MB",
                     deleted, freed / (1024.0 * 1024.0))
    return deleted, freed


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36 Edg/152.0.0.0")

# 自助服务门户（/self/index，Vue SPA + CAS OAuth）要用**更像浏览器**的 UA，
# 并且必须**手动逐跳跟随**重定向：用 make_opener 的自动跟随会撞进
# /login 自跳环被 urllib 报成 "infinite loop"。
UA_BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# 查询账号"实际绑定的运营商"的自助服务接口（登录自助服务后才能调）
SELF_OPERATORS_API = "/sam/api/userself/reset/operators/options"


# 网络三态：公网通 / 网关可达但没认证 / 本地根本没连上
ONLINE = "online"
UNAUTHENTICATED = "unauthenticated"
OFFLINE = "offline"

STATE_TEXT = {
    ONLINE: "在线",
    UNAUTHENTICATED: "未认证",
    OFFLINE: "本地断网",
}

# 公网 TCP 探测目标（直连 IP，绕开校园 DNS），任一可达即认为公网通
PUBLIC_PROBES = (("223.5.5.5", 80), ("114.114.114.114", 53), ("180.101.50.242", 80))

# 域名兜底探测：**只在上面三路 IP 全不可达时**才跑。存在的意义是「这几家的 IP 恰好
# 被运营商/防火墙屏蔽，但公网其实通」——纯 IP 探测在那种环境里会永久误判为离线，
# 每轮都退避重试，用户看到的是"程序坏了"。走域名要多过一次 DNS，所以放在慢路径上。
# 误判风险与 IP 探测同级：NAS 拦截页若也接受 TCP，仍由 http_content_ok 的内容校验兜底。
PUBLIC_HOST_PROBES = (("www.baidu.com", 80), ("www.qq.com", 80))

# 校园级默认参数（可被 config.json 覆盖）。
# 账号/密码默认空字符串：本程序任何默认值、文档、测试数据都不得包含真实凭据。
DEFAULT_CONFIG = {
    "username": "",
    "password": "",
    "service": "cmcc",  # 运营商：cmcc 移动 / ctcc 电信 / unicom 联通 / local 教育网
    "portal_host": "10.254.241.66",
    "customPageId": "a35fa25313ed4013a4eae78484c5a0a5",
    "nasIp": "10.254.1.198",
    "check_interval_seconds": 30,
    "max_backoff_seconds": 300,
    # —— 后台占用优化相关（本次新增，老配置文件缺失时自动回落默认）——
    # 在线稳定期（连续 2 轮 ONLINE）放宽后的轮询间隔；不得小于 check_interval_seconds
    "online_interval_seconds": 60,
    # HTTP 内容校验（最重的一步：GET + 读 8KB）的最小执行间隔（秒），其余轮次走缓存。
    # 注意：这是有界的条件性 trade-off——TCP 判定公网通且状态未变且上次校验通过时，
    # 最多容忍该秒数的陈旧窗口；调小更保守（如 60），调大更省请求（详见 check_network 说明）。
    "content_check_interval": 300,
    # 状态无变化时，每隔多少分钟写一行心跳日志（其余轮次不写盘）
    "log_heartbeat_minutes": 30,
    "test_url": "http://www.baidu.com/",
    # —— 多设备互斥：账号只允许一台在线时的「自动让位」策略 ——
    # 校园网账号通常只允许一台设备在线。手机在教室一认证，寝室的电脑就被踢下线，
    # 而电脑的保活会立刻重新认证、把手机踢回去，两边就这么互相踢。
    # 首选判据是「在线设备的 MAC」（确证），查不到接口时才退回窗口推断（猜测）：
    # 认证**成功**之后撑不过 yield_detect_window_seconds 就又变成未认证，即被顶掉。
    "yield_enabled": True,
    "yield_minutes": 15,                  # 首次礼让时长；反复被顶则每次翻倍
    "max_yield_minutes": 120,             # 礼让上限，避免无限让位让电脑长期离线
    "yield_detect_window_seconds": 180,   # 窗口推断：认证后活不过这么久 => 判定被顶
    # —— 用「在线设备的 MAC」精确判定是不是别的设备在上网 ——
    # 查询走自助服务的 CAS 会话，跟校园网认证是两套会话，不会把已在线的设备挤掉。
    "device_check_enabled": True,         # 关掉则退回纯窗口推断
    "device_check_cache_seconds": 60,     # 查询结果缓存（查一次要跑一遍 CAS 登录）
    "device_recheck_seconds": 120,        # 礼让期间多久复查一次（对方一走就提前恢复）
}

# 整数字段的下限校验表（非法/越界一律回落到默认值，避免保活循环里 int() 崩溃）
_INT_FIELDS = {
    "check_interval_seconds": (DEFAULT_CONFIG["check_interval_seconds"], 1),
    "online_interval_seconds": (DEFAULT_CONFIG["online_interval_seconds"], 1),
    "content_check_interval": (DEFAULT_CONFIG["content_check_interval"], 1),
    "max_backoff_seconds": (DEFAULT_CONFIG["max_backoff_seconds"], 1),
    "log_heartbeat_minutes": (DEFAULT_CONFIG["log_heartbeat_minutes"], 1),
    "yield_detect_window_seconds": (DEFAULT_CONFIG["yield_detect_window_seconds"], 30),
    "yield_minutes": (DEFAULT_CONFIG["yield_minutes"], 1),
    "max_yield_minutes": (DEFAULT_CONFIG["max_yield_minutes"], 1),
    "device_check_cache_seconds": (DEFAULT_CONFIG["device_check_cache_seconds"], 10),
    "device_recheck_seconds": (DEFAULT_CONFIG["device_recheck_seconds"], 30),
}

# Windows 进程优先级类别（1.4：daemon 降为「低于正常」，避免与前台应用抢资源）
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
PRIORITY_CLASS_NAMES = {
    0x00000040: "IDLE（空闲）",
    0x00004000: "BELOW_NORMAL（低于正常）",
    0x00000020: "NORMAL（正常）",
    0x00008000: "ABOVE_NORMAL（高于正常）",
    0x00000080: "HIGH（高）",
    0x00000100: "REALTIME（实时）",
}

_TCP_POOL = None

# 登录失败原因概要（run_once 结构化返回给 GUI 展示；不包含任何敏感信息）
_LAST_FAIL_REASON = ""

# 最近一次失败的分类码（no_redirect / gateway_unreachable / params_missing /
# login_rejected）。保活循环用它判断「再跑一次登录流程会不会有不同结果」——
# no_redirect 与 gateway_unreachable 是环境结论，login_rejected 是凭据结论，
# 重跑一万次都一样，不该按 30s 硬重试。
_LAST_FAIL_WHY = ""

# ============================ 极轻量运行统计（1.6） ============================
# 全部为整数自增，无 I/O、无锁、无线程，常驻开销可忽略；供 --probe 与 QA 核对。
STATS = {
    "rounds": 0,      # 保活循环轮次（每轮一次网络检测）
    "tcp_probes": 0,  # TCP 连接探测次数（公网 3 路 + 网关 1 路，离线时另有域名兜底 2 路）
    "http_checks": 0,  # HTTP 内容校验（真实执行，未走缓存）次数
    "log_lines": 0,   # 写出的日志行数（含文件与控制台）
}

# 统计计数器可能在并发路径上自增（tcp_reachable 跑在线程池里、日志 handler
# 可能被多线程触发），用一把锁保护自增，避免丢失更新。锁只包住一次整数自增，
# 常驻开销可忽略。
_STATS_LOCK = threading.Lock()


def _bump_stat(key, n=1):
    """线程安全地给统计计数器自增（极轻量）。"""
    with _STATS_LOCK:
        STATS[key] += n


def stats_snapshot():
    """返回统计计数器的快照副本（加锁读取，避免读到并发自增的中间态）。"""
    with _STATS_LOCK:
        return dict(STATS)


def stats_line():
    """把统计计数器格式化成一行文本（--probe / daemon 退出汇总用）。"""
    s = stats_snapshot()
    return ("统计: 轮次=%d TCP探测=%d HTTP校验=%d 日志行=%d"
            % (s["rounds"], s["tcp_probes"], s["http_checks"], s["log_lines"]))


class _CountingHandler(logging.Handler):
    """只做一件事：统计已写出的日志行数（极轻量，无 I/O、无线程）。"""

    def emit(self, record):
        _bump_stat("log_lines")


# ============================ 进程优先级（1.4，纯标准库 ctypes） ============================

def set_low_priority():
    """把当前进程降到「低于正常」优先级，避免长时间保活时与前台应用抢 CPU。

    用 kernel32.SetPriorityClass(GetCurrentProcess(), BELOW_NORMAL_PRIORITY_CLASS)。
    纯标准库实现，不引入 psutil 等第三方库。任何失败（拿不到句柄/调用失败/非
    Windows）都静默降级——绝不能因降优先级失败而崩溃或影响保活。返回是否成功。
    """
    try:
        kernel32 = ctypes.WinDLL("kernel32")
        # 64 位下必须把伪句柄声明成指针，否则 GetCurrentProcess() 的返回值会被截断
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        kernel32.SetPriorityClass.restype = ctypes.c_int
        handle = kernel32.GetCurrentProcess()
        return bool(kernel32.SetPriorityClass(handle, BELOW_NORMAL_PRIORITY_CLASS))
    except Exception:
        return False


def trim_working_set(pid=None):
    """把某个进程（默认自己）的物理内存（工作集）还给系统。

    pid 参数是为 daemon 准备的：打包成单文件后，daemon 的父进程是 PyInstaller 的
    bootloader 壳子（它因为要持有独立解包目录必须常驻），平时没什么事却一直挂着
    十几 MB 物理内存。子进程启动后顺手把父进程的也回收一次。

    常驻保活进程大部分时间是空转的，但 Python + 打包运行时加载过的一堆页会一直
    挂在物理内存里，任务管理器里看着"占用几十 MB"。这里调用
    SetProcessWorkingSetSize(-1, -1)（等价 EmptyWorkingSet）把这些页换出去——
    页面本身在磁盘上有后备（exe/映射文件），需要时会重新换入，**不会丢数据**，
    只是下次访问时多一次软缺页。

    只在两处调用：daemon 每轮检测结束后的空闲期、GUI 窗口最小化期间。频繁调用
    （比如每帧）会反复换出正在用的页、反而变慢，所以调用频率都以秒/分钟计。
    任何失败都静默降级。
    注意：这个函数导出自 **kernel32**，不是 psapi——旧文档写的是 psapi，实测
    Win10/11 上 psapi 没导出它，照旧文档写会静默失败（返回 0）。

    只在两处调用：daemon 每轮检测结束后的空闲期、GUI 窗口最小化期间。频繁调用
    （比如每帧）会反复换出正在用的页、反而变慢，所以调用频率都以秒/分钟计。
    任何失败都静默降级。
    """
    try:
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.SetProcessWorkingSetSize.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
                                                      ctypes.c_size_t]
        kernel32.SetProcessWorkingSetSize.restype = ctypes.c_int
        if pid is None:
            handle = kernel32.GetCurrentProcess()
        else:
            PROCESS_SET_QUOTA = 0x0100
            PROCESS_QUERY_INFORMATION = 0x0400
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_int,
                                             ctypes.c_uint]
            handle = kernel32.OpenProcess(
                PROCESS_SET_QUOTA | PROCESS_QUERY_INFORMATION, 0, int(pid))
            if not handle:
                return False
        try:
            # (-1, -1) = 尽可能地把工作集压到最小
            return bool(kernel32.SetProcessWorkingSetSize(
                handle, ctypes.c_size_t(-1), ctypes.c_size_t(-1)))
        finally:
            if pid is not None:
                try:
                    kernel32.CloseHandle(handle)
                except Exception:
                    pass
    except Exception:
        return False


def set_power_throttling(enable=True):
    """给当前进程开/关 Win11 节流模式（EcoQoS）。

    开启后系统会在 CPU 繁忙、未接电源等场景自动降频这个进程。保活进程本来就是
    "几分钟醒一次、其余时间睡觉"的性质，节流对它没有体感影响，却能实打实降低
    长期的后台能耗。

    用 SetProcessInformation(ProcessPowerThrottling) 实现（纯 ctypes）。
    老 Windows（Win10 及更早）不支持这个信息类，调用失败静默降级即可。
    """

    class _ThrottlingState(ctypes.Structure):
        _fields_ = [("Version", ctypes.c_uint32),
                    ("ControlMask", ctypes.c_uint32),
                    ("StateMask", ctypes.c_uint32)]

    try:
        PROCESS_POWER_THROTTLING = 4
        PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.SetProcessInformation.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                                   ctypes.c_void_p, ctypes.c_uint32]
        kernel32.SetProcessInformation.restype = ctypes.c_int
        st = _ThrottlingState()
        st.Version = 1
        st.ControlMask = PROCESS_POWER_THROTTLING_EXECUTION_SPEED
        st.StateMask = PROCESS_POWER_THROTTLING_EXECUTION_SPEED if enable else 0
        return bool(kernel32.SetProcessInformation(
            kernel32.GetCurrentProcess(), PROCESS_POWER_THROTTLING,
            ctypes.byref(st), ctypes.sizeof(st)))
    except Exception:
        return False


def get_priority_class():
    """返回当前进程优先级类别的可读名称（--probe 诊断用）。查询失败返回提示串。"""
    try:
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.GetPriorityClass.argtypes = [ctypes.c_void_p]
        kernel32.GetPriorityClass.restype = ctypes.c_uint
        val = kernel32.GetPriorityClass(kernel32.GetCurrentProcess())
        return PRIORITY_CLASS_NAMES.get(val, "0x%08X（未知）" % val)
    except Exception:
        return "未知（查询失败）"


class ConfigError(Exception):
    """配置文件读写/内容异常。调用方（GUI/CLI）捕获后友好提示，不允许闪退。"""


def _set_fail_reason(reason):
    """记录登录失败原因概要（内部用）。"""
    global _LAST_FAIL_REASON
    _LAST_FAIL_REASON = reason
    return False


def get_fail_reason():
    """最近一次登录失败的原因概要（无则为空串）。"""
    return _LAST_FAIL_REASON


# 认证被拒：serviceLogin 没返回 success（账号或密码错误、欠费停机、账号已在别处在线）。
# 与 no_redirect / gateway_unreachable 不同，这类失败**高频重试还有额外代价**：短期内
# 反复提交错误凭据正是运营商侧锁定账号的典型触发条件，比空转更严重。所以除了放宽间隔，
# 还单独记连败次数，超过阈值就长间隔静默等待——直到成功一次，或用户手动重登。
AUTH_REJECTED_WHY = "login_rejected"

# 连续被拒多少次后停手（8 × 30s ≈ 4 分钟，远早于常见的 15~30 分钟锁定窗口）。
_AUTH_STOP_AFTER = 8
# 停手后的重试间隔：保留自动恢复能力（欠费充值后能自己连上），但不再高频试探。
_AUTH_SETTLE_SECONDS = 1800

_AUTH_REJECT_STREAK = 0


def get_auth_reject_streak():
    """连续被认证拒绝的次数（登录成功时清零）。"""
    return _AUTH_REJECT_STREAK


def _fail_auth_rejected():
    """serviceLogin 未返回 success：记分类码 + 连败计数 + 节流日志。返回 False。"""
    global _LAST_FAIL_WHY, _AUTH_REJECT_STREAK
    _LAST_FAIL_WHY = AUTH_REJECTED_WHY
    _AUTH_REJECT_STREAK += 1
    n = _AUTH_REJECT_STREAK
    if n == 1:
        logging.error("serviceLogin 未返回 success（常见原因：账号或密码错误、欠费停机）。"
                      "连续 %d 次后会放宽到每 %d 分钟重试一次，以免触发运营商侧的账号锁定。",
                      _AUTH_STOP_AFTER, _AUTH_SETTLE_SECONDS // 60)
    elif n == _AUTH_STOP_AFTER:
        logging.error("登录已连续被拒 %d 次，改为每 %d 分钟重试一次。请核对账号/密码、"
                      "是否欠费停机，或该账号是否已在其它设备在线；修改后重新点击"
                      "「立即登录」即可。", n, _AUTH_SETTLE_SECONDS // 60)
    elif n % 10 == 0:
        logging.error("登录仍被拒（已连续 %d 次）", n)
    else:
        logging.debug("登录被拒（连续 %d 次，已节流）", n)
    return _set_fail_reason("serviceLogin 未返回 success（常见原因：账号或密码错误、欠费停机）")


# 保活循环会因这几类失败放宽重试间隔（原因见 run_forever 的注释）。
# 三者都是「重跑登录流程不会改变结果」的结论：前两个是环境如此，login_rejected 是
# 凭据或账号状态如此。
_NO_RETRY_WHY = ("no_redirect", "gateway_unreachable", AUTH_REJECTED_WHY)


def get_fail_why():
    """最近一次登录失败的分类码。

    no_redirect / gateway_unreachable / params_missing / login_rejected。
    """
    return _LAST_FAIL_WHY


# ============================ AES-128-ECB ============================

def _rotl8(x, n):
    return ((x << n) | (x >> (8 - n))) & 0xFF


def _gen_sbox():
    sbox = [0] * 256
    p = 1
    q = 1
    while True:
        p = p ^ (p << 1) ^ (0x1B if p & 0x80 else 0)
        p &= 0xFF
        q ^= (q << 1) & 0xFF
        q ^= (q << 2) & 0xFF
        q ^= (q << 4) & 0xFF
        if q & 0x80:
            q ^= 0x09
        q &= 0xFF
        xformed = q ^ _rotl8(q, 1) ^ _rotl8(q, 2) ^ _rotl8(q, 3) ^ _rotl8(q, 4)
        sbox[p] = (xformed & 0xFF) ^ 0x63
        if p == 1:
            break
    sbox[0] = 0x63
    return sbox


_SBOX = _gen_sbox()
_RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]


def _key_expansion(key):
    w = [list(key[i * 4:i * 4 + 4]) for i in range(4)]
    for i in range(4, 44):
        temp = list(w[i - 1])
        if i % 4 == 0:
            temp = temp[1:] + temp[:1]
            temp = [_SBOX[b] for b in temp]
            temp[0] ^= _RCON[i // 4 - 1]
        w.append([w[i - 4][j] ^ temp[j] for j in range(4)])
    return [sum(w[r * 4:r * 4 + 4], []) for r in range(11)]


def _add_round_key(state, rk):
    return [state[i] ^ rk[i] for i in range(16)]


def _sub_bytes(state):
    return [_SBOX[b] for b in state]


def _shift_rows(state):
    s = state
    return [s[0], s[5], s[10], s[15], s[4], s[9], s[14], s[3],
            s[8], s[13], s[2], s[7], s[12], s[1], s[6], s[11]]


def _xtime(a):
    return ((a << 1) ^ (0x1B if a & 0x80 else 0)) & 0xFF


def _mix_columns(state):
    out = [0] * 16
    for c in range(4):
        a0, a1, a2, a3 = state[4 * c:4 * c + 4]
        out[4 * c] = _xtime(a0) ^ (_xtime(a1) ^ a1) ^ a2 ^ a3
        out[4 * c + 1] = a0 ^ _xtime(a1) ^ (_xtime(a2) ^ a2) ^ a3
        out[4 * c + 2] = a0 ^ a1 ^ _xtime(a2) ^ (_xtime(a3) ^ a3)
        out[4 * c + 3] = (_xtime(a0) ^ a0) ^ a1 ^ a2 ^ _xtime(a3)
    return out


def _encrypt_block(block, rkeys):
    state = _add_round_key(list(block), rkeys[0])
    for r in range(1, 10):
        state = _sub_bytes(state)
        state = _shift_rows(state)
        state = _mix_columns(state)
        state = _add_round_key(state, rkeys[r])
    state = _sub_bytes(state)
    state = _shift_rows(state)
    state = _add_round_key(state, rkeys[10])
    return bytes(state)


def aes_ecb_pkcs7_encrypt(key, data):
    rkeys = _key_expansion(key)
    pad_len = 16 - (len(data) % 16)
    padded = data + bytes([pad_len]) * pad_len
    return b"".join(_encrypt_block(padded[i:i + 16], rkeys) for i in range(0, len(padded), 16))


# (密钥hex, 明文hex, 期望密文hex) —— 公开标准向量，不含任何真实凭据
_AES_VECTORS = (
    ("000102030405060708090a0b0c0d0e0f",
     "00112233445566778899aabbccddeeff",
     "69c4e0d86a7b0430d8cdb78070b4c55a954f64f2e4e86e9eee82d20216684899"),
    ("2b7e151628aed2a6abf7158809cf4f3c",
     "6bc1bee22e409f96e93d7e117393172aae2d8a571e03ac9c9eb76fac45af8e51",
     "3ad77bb40d7a3660a89ecaf32466ef97f5d3d58503b9699de785895a96fdbaaf"
     "a254be88e037ddd9d79fb6411c3f9df8"),
)


def aes_self_test():
    """用两组公开标准向量校验 AES-128-ECB + PKCS#7（覆盖密钥扩展与多分组填充）。
    向量 1 = FIPS-197 C.1，向量 2 = NIST SP 800-38A F.2.1，密文均经 openssl 复核。"""
    try:
        for key_hex, pt_hex, ct_hex in _AES_VECTORS:
            got = aes_ecb_pkcs7_encrypt(bytes.fromhex(key_hex), bytes.fromhex(pt_hex))
            if got.hex() != ct_hex:
                logging.error("AES 向量不匹配: %s", key_hex)
                return False
        return True
    except Exception as e:
        logging.error("AES 自检异常: %s", e)
        return False


# ============================ 日志与配置 ============================

def setup_logging(console=True):
    """初始化日志：RotatingFileHandler 写 BASE_DIR\\login.log（1MB x 3），
    可选同时输出到控制台。

    日志里不出现明文密码；sessionId / CAS ticket 等临时凭据写盘前经 mask_secret()
    打码（login.log 与 exe 同目录，会跟着发布包一起被拷走）。
    返回文件日志是否启用成功（exe 目录只读时降级为仅控制台，不崩溃）。"""
    # 打包成 exe 后控制台沿用系统 GBK 代码页，UTF-8 中文会显示成乱码（--probe 的诊断
    # 输出直接没法看）。这里把标准流转成 UTF-8；失败不影响任何功能。
    for _stream in (sys.stdout, sys.stderr):
        try:
            if _stream is not None and hasattr(_stream, "reconfigure"):
                _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    # 重复调用时先清掉旧 handler，避免双份输出
    for h in list(logger.handlers):
        logger.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    file_ok = False
    try:
        # 1MB x 3：实测一天约 100KB，512KB 会导致保活跑几天就把排障历史轮转掉
        fh = RotatingFileHandler(LOG_PATH, maxBytes=1024 * 1024,
                                 backupCount=3, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(fh)
        file_ok = True
    except OSError as e:
        # 日志文件都写不进去（如 exe 目录被写保护）：不崩溃，仅跳过文件日志
        if sys.stderr is not None:
            try:
                sys.stderr.write("警告: 无法写日志文件 %s: %s\n" % (LOG_PATH, e))
            except Exception:
                pass
    if console and sys.stdout is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(sh)
    # 统计"写出的日志行数"的极轻量 handler（1.6）；放在最后，计入所有真正落地的日志
    logger.addHandler(_CountingHandler())
    return file_ok


def _cfg_int(cfg, key, minimum=1):
    """从配置里安全取整数字段：缺失/非法/越界一律回落到 DEFAULT_CONFIG 里的默认值。
    返回值为 [minimum, +inf) 内的整数，供保活循环直接使用。"""
    default = DEFAULT_CONFIG.get(key, minimum)
    try:
        v = int(cfg.get(key, default))
    except (TypeError, ValueError):
        v = default
    if v < minimum:
        v = max(minimum, default)
    return v


def _normalize_numbers(cfg):
    """就地规范化所有整数字段，并保证 online_interval_seconds >= check_interval_seconds。
    load_config / save_config 共用，确保非法值永远不会传进保活循环。"""
    for key, (default, minimum) in _INT_FIELDS.items():
        try:
            v = int(cfg.get(key, default))
        except (TypeError, ValueError):
            v = default
        if v < minimum:
            v = max(minimum, default)
        cfg[key] = v
    # 在线间隔不得小于基础间隔：否则在线稳定期反而会更频繁探测，违背优化初衷
    if cfg["online_interval_seconds"] < cfg["check_interval_seconds"]:
        cfg["online_interval_seconds"] = cfg["check_interval_seconds"]
    return cfg


# ============================ 日志脱敏 ============================
# sessionId 与 CAS ticket 都是临时凭据：拿到 sessionId 就能顶着你的会话继续操作，
# ticket 是 CAS 登录票据。login.log 跟 exe 同目录、会跟着发布包一起被拷走，所以
# 写盘前一律打码。保留前几位方便排障时对得上，其余打星。
_SECRET_PATTERNS = (
    # URL query / 表单：sessionId=xxxx&...  ticket=ST-xxx
    re.compile(r'((?:sessionId|ticket)=)([^&\s"\'}]+)()'),
    # JSON 响应体："sessionId" : "xxxx"
    re.compile(r'("(?:sessionId|ticket)"\s*:\s*")([^"]*)(")'),
    # Python 字面量：str(dict) / str(list) 用的是单引号 —— 'sessionId': 'xxxx'。
    # 少了这一条，把 dict 直接丢给 mask_secret 会原样输出（实测 login.log 里
    # 躺着 4 条完整 sessionId，而同一次请求的相邻日志已正确打码）。
    re.compile(r"('(?:sessionId|ticket)'\s*:\s*')([^']*)(')"),
)


def _mask_match(m):
    """保留前 6 位便于排障对照，尾部引号原样带回。"""
    return m.group(1) + m.group(2)[:6] + "***" + m.group(3)


def mask_cred(value):
    """裸凭据值（没有 key= 前缀、正则匹配不到时）打码：保留前 4 位便于排障对照。"""
    s = str(value or "")
    if not s:
        return ""
    return s[:4] + "***" if len(s) > 4 else "***"


def mask_secret(text):
    """把文本里的 sessionId / ticket 打码后再交给 logging。dict/list 也可以直接传。"""
    try:
        s = str(text)
    except Exception:
        return "***"
    for pat in _SECRET_PATTERNS:
        s = pat.sub(_mask_match, s)
    return s


# ============================ 凭据保护（Windows DPAPI） ============================
# config.json 跟 exe 放在同一个文件夹，整个目录是会直接拷给别人用的。密码若明文
# 落盘，等于把校园网账号跟着发布包一起送出去。这里用 Windows DPAPI（CryptProtectData）
# 加密：密钥由系统按「当前 Windows 用户」派生，换用户/换机器/重装系统都解不开，
# 且不需要额外依赖（ctypes 调系统库，PyInstaller 打包无影响）。
# 内存里仍然是明文（登录要用），只有**磁盘上**是密文。

_CRED_PREFIX = "dpapi:"   # 密文标记，用于区分旧版本的明文配置


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_protect(raw):
    """用当前 Windows 用户凭据加密；非 Windows 或调用失败返回 None（调用方降级）。"""
    if not raw or sys.platform != "win32":
        return None
    try:
        buf = ctypes.create_string_buffer(raw, len(raw))
        in_blob = _DATA_BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        out_blob = _DATA_BLOB()
        ok = ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob))
        if not ok:
            return None
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(out_blob.pbData)
    except Exception:
        return None


def _dpapi_unprotect(blob):
    """_dpapi_protect 的逆操作；失败返回 None。"""
    if not blob or sys.platform != "win32":
        return None
    try:
        buf = ctypes.create_string_buffer(blob, len(blob))
        in_blob = _DATA_BLOB(len(blob), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        out_blob = _DATA_BLOB()
        ok = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob))
        if not ok:
            return None
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(out_blob.pbData)
    except Exception:
        return None


def protect_secret(plain):
    """明文 -> 可写入 config.json 的字符串。失败返回 ''（调用方据此降级为明文）。"""
    if not plain:
        return ""
    blob = _dpapi_protect(plain.encode("utf-8"))
    if blob is None:
        return ""
    return _CRED_PREFIX + base64.b64encode(blob).decode("ascii")


def unprotect_secret(stored):
    """config.json 里的字符串 -> 明文。已是明文（旧配置）则原样返回，失败返回 ''。"""
    if not stored:
        return ""
    s = str(stored)
    if not s.startswith(_CRED_PREFIX):
        return s          # 旧版本遗留的明文，直接沿用
    try:
        blob = base64.b64decode(s[len(_CRED_PREFIX):])
    except Exception:
        return ""
    raw = _dpapi_unprotect(blob)
    if raw is None:
        return ""
    return raw.decode("utf-8", "ignore")


def load_config(path=None):
    """读取 config.json 并与 DEFAULT_CONFIG 合并后返回。
    文件不存在 -> 返回纯默认值（账号密码为空）；
    文件存在但损坏 -> 抛 ConfigError（由调用方友好提示，不闪退）。

    密码字段：磁盘上是 DPAPI 密文（password_enc），这里解密后填进 cfg["password"]，
    上层（GUI/登录流程）拿到的始终是明文，无需感知加密。旧版明文配置照常可读。"""
    p = path or CONFIG_PATH
    if not os.path.exists(p):
        return dict(DEFAULT_CONFIG)
    try:
        with open(p, "r", encoding="utf-8") as f:
            user_cfg = json.load(f)
    except json.JSONDecodeError as e:
        raise ConfigError("config.json 已损坏（不是合法的 JSON）：%s\n"
                          "可删除该文件后重新填写保存。" % e)
    except OSError as e:
        raise ConfigError("无法读取 config.json：%s" % e)
    if not isinstance(user_cfg, dict):
        raise ConfigError("config.json 格式不对：根节点必须是对象 { }")
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(user_cfg)
    # 凭据还原：password_enc（密文）优先，解密结果放进 password 供上层使用
    enc = user_cfg.get("password_enc")
    if enc:
        plain = unprotect_secret(enc)
        if plain:
            cfg["password"] = plain
        else:
            # 密文解不开：换过 Windows 用户/机器/重装过系统。不能拿空密码去登录，
            # 否则会被网关判成密码错误甚至触发账号锁定——这里明确告警，让 GUI 提示重填。
            logging.warning("config.json 里的密码密文无法解密（可能换过 Windows 账户或电脑），请重新填写密码")
            cfg["password"] = ""
    # 数值字段兜底：非法值回落到默认，保证 online>=check 等约束（老文件缺字段也能读）
    _normalize_numbers(cfg)
    # 布尔字段兜底：容忍字符串 "true"/"1"/"yes" 等写法
    return cfg


def save_config(cfg, path=None):
    """把配置与默认值合并后写入 config.json（临时文件 + 原子替换）。
    写入失败（目录只读等）抛 ConfigError。密码只进文件，绝不写日志。"""
    p = path or CONFIG_PATH
    merged = dict(DEFAULT_CONFIG)
    for k, v in (cfg or {}).items():
        if v is not None:
            merged[k] = v
    _normalize_numbers(merged)
    # —— 凭据保护：明文密码一律不落盘 ——
    # config.json 与 exe 同目录，整个文件夹是会拷给别人用的；明文密码等于把校园网
    # 账号跟着发布包一起送出去。这里存 DPAPI 密文，只有本机能解。
    plain = str(merged.pop("password", "") or "")
    if plain:
        enc = protect_secret(plain)
        if enc:
            merged["password_enc"] = enc
            merged["password"] = ""      # 保留字段以免老代码 KeyError，但值已清空
        else:
            # DPAPI 不可用（非 Windows / 加密失败）：宁可明文落盘，也绝不能把用户的密码弄丢
            logging.warning("无法加密密码（DPAPI 不可用），已按明文保存——请勿把 config.json 外传")
            merged["password"] = plain
            merged.pop("password_enc", None)
    else:
        merged["password"] = ""
        # 没有新的明文要存 —— **已有的密文必须原样保留**。原来这里无条件 pop 掉
        # password_enc，结果调用方只是改了个不相干的字段（比如自动探测到的网关地址），
        # 或者 DPAPI 这一轮恰好解密失败，就把用户的密码连带删掉了，之后再也登不上。
        if not merged.get("password_enc"):
            merged.pop("password_enc", None)
    tmp = p + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except OSError as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise ConfigError(
            "无法写入 %s：%s\n"
            "（程序所在目录可能被写保护，请把整个文件夹复制到普通目录后再试）" % (p, e))
    return merged


def tail_log(lines=15, path=None):
    """读取日志尾部若干行（GUI 状态区展示用）。文件不存在/不可读返回空串。

    旧实现是 readlines() 整个文件再切片——日志轮转上限 1MB，GUI 每次刷新日志卡
    都要为「取最后 15 行」把 1MB 全读进内存（还建上万个 str 对象），在 resize
    防抖回调里更是连续触发。改成从文件末尾按块倒读，开销只跟需要的行数有关。
    """
    p = path or LOG_PATH
    try:
        n = max(1, int(lines))
    except (TypeError, ValueError):
        n = 15
    try:
        size = os.path.getsize(p)
    except OSError:
        return ""
    if size <= 0:
        return ""

    chunk = 8192
    data = b""
    pos = size
    try:
        with open(p, "rb") as f:
            # 倒着读，直到凑够 n+1 个换行（多要一行以确认起点在行首）
            while pos > 0 and data.count(b"\n") <= n:
                step = min(chunk, pos)
                pos -= step
                f.seek(pos)
                data = f.read(step) + data
    except OSError:
        return ""
    text = data.decode("utf-8", "replace")
    if pos > 0:
        # 起点落在某行中间：第一段是半行，丢掉它再取尾部
        cut = text.find("\n")
        text = text[cut + 1:] if cut >= 0 else ""
    return "\n".join(text.splitlines()[-n:]).rstrip("\n")


# ============================ HTTP 通用 ============================

class Recorder(urllib.request.HTTPRedirectHandler):
    """记录重定向链的 opener 组件（诊断用）。"""

    def __init__(self):
        self.hops = []
        super().__init__()

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.hops.append(newurl)
        if len(self.hops) > 20:
            raise urllib.error.HTTPError(req.full_url, code, "too many redirects", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def make_opener():
    cj = http.cookiejar.CookieJar()
    rec = Recorder()
    # 显式禁用系统/第三方代理：本程序判定的是「本机直连校园网」的真实状态，
    # 而 public_tcp_ok 用的是裸 socket（从不走代理），两者口径必须一致；
    # 否则会出现「TCP 直连判定未认证、HTTP 却经代理拿到网页」的自相矛盾。
    op = urllib.request.build_opener(rec, urllib.request.HTTPCookieProcessor(cj),
                                     urllib.request.ProxyHandler({}))
    op.addheaders = [("User-Agent", UA)]
    return op, cj, rec


def http_get(op, url, timeout=10, referer=None):
    h = {"User-Agent": UA}
    if referer:
        h["Referer"] = referer
    req = urllib.request.Request(url, headers=h)
    return op.open(req, timeout=timeout)


def http_post(op, url, data_bytes=None, json_body=None, extra_headers=None, timeout=10):
    headers = {"User-Agent": UA}
    if json_body is not None:
        data_bytes = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=data_bytes, headers=headers, method="POST")
    return op.open(req, timeout=timeout)


# ============================ 网络探测 ============================

def _tcp_pool():
    """复用同一个线程池：保活循环每轮都要探测，不必每 30 秒重建线程。"""
    global _TCP_POOL
    if _TCP_POOL is None:
        _TCP_POOL = ThreadPoolExecutor(max_workers=len(PUBLIC_PROBES),
                                       thread_name_prefix="netprobe")
    return _TCP_POOL


def tcp_reachable(target, timeout=2):
    _bump_stat("tcp_probes")  # 统计实际发生的 TCP 连接尝试（1.6），线程安全自增
    try:
        with socket.create_connection(target, timeout=timeout):
            return True
    except OSError:
        return False


def public_tcp_ok(timeout=2):
    """并行直连多个公网 IP，任一可达即 True，总耗时不超过 timeout。
    未认证时 NAS 会丢包/拒绝，所以这个探测本身不能区分「没认证」和「断网」。

    **这里只探 IP**。域名兜底（PUBLIC_HOST_PROBES）由调用方在"连认证网关也不通"
    时再补一次——见 check_network：未认证的常见情形是"IP 不通但网关通"，那种情况下
    不需要兜底，放在这里会让每一轮都平白多等一次 DNS+连接。"""
    futs = [_tcp_pool().submit(tcp_reachable, t, timeout) for t in PUBLIC_PROBES]
    try:
        for f in as_completed(futs, timeout=timeout):
            if f.result():
                return True
    except Exception:
        pass
    return False


def _public_host_ok(timeout=2):
    """域名兜底：解析 + 连接，并行发出，整体不超过 timeout。"""
    futs = [_tcp_pool().submit(tcp_reachable, t, timeout)
            for t in PUBLIC_HOST_PROBES]
    try:
        for f in as_completed(futs, timeout=timeout):
            if f.result():
                return True
    except Exception:
        pass
    return False


def classify_network(public_ok, portal_ok):
    """把两个可达性探测映射成网络状态（纯函数，便于单测）。
    公网通 -> 在线；公网不通但认证网关可达 -> 未认证，该跑登录流程；
    两者都不通 -> 本地网络根本没连上，此时跑登录流程纯属白等。"""
    if public_ok:
        return ONLINE
    if portal_ok:
        return UNAUTHENTICATED
    return OFFLINE


def http_content_ok(cfg):
    """确认 HTTP 拿到的是真实网站而不是认证拦截页。

    1.3：复用模块级 opener（原先每次调用都新建 CookieJar + opener，开销不小）。
    用锁保护：GUI 后台线程与保活线程都可能调用内容校验，共享同一 CookieJar 不是
    线程安全对象，串行化最稳妥（调用频率很低，锁竞争可忽略）。"""
    host = cfg["portal_host"]
    _bump_stat("http_checks")  # 统计真实执行的内容校验次数（走缓存的不计），线程安全
    got_response = False
    with _HTTP_LOCK:
        op = _content_opener()
        for url in (cfg.get("test_url", "http://www.baidu.com/"), "http://www.qq.com/"):
            try:
                r = http_get(op, url, timeout=3)
                final = r.geturl()
                st = r.status
                body = r.read(8192)
                r.close()
                got_response = True   # 至少有一次真的拿到了 HTTP 响应
                if host in final:  # 被重定向回认证门户
                    continue
                if st != 200:
                    continue
                # 拦截页通常 <2KB 且不含目标站点特征；真实首页较大
                if len(body) < 2000 and b"baidu" not in body.lower() and b"qq.com" not in body.lower():
                    continue
                return True
            except Exception:
                continue
    # False=确认拿到的是拦截页；None=请求本身没成功（超时/连接失败），**未知**。
    # 这个区分很关键：把"请求超时"当成"内容不对"，会让弱网抖动时把已在线误判成
    # 未认证，于是每 30 秒跑一次完整登录流程（CAS + 提交密码），既刷屏又可能撞上
    # 账号锁定。请求失败属于未知，不该推翻 TCP 探测给出的在线结论。
    return False if got_response else None


# 内容校验专用 opener 的缓存（1.3）；锁保护见 http_content_ok。
_CONTENT_OPENER = None
_HTTP_LOCK = threading.Lock()


def _content_opener():
    """内容校验专用 opener：模块级只构造一次后复用。"""
    global _CONTENT_OPENER
    if _CONTENT_OPENER is None:
        op, _, _ = make_opener()
        _CONTENT_OPENER = op
    return _CONTENT_OPENER


# 内容校验缓存（1.2）：记录上一轮 TCP 判定与上次校验结果，用于给最重的 HTTP 请求降频。
# **加锁**：GUI 后台线程（手动「保存并立即登录」）与保活线程都会走 check_network，
# 无锁共享可变 dict 会读到"半更新"的组合——例如 public_ok 已是本轮新值而 state 还是
# 上一轮的旧值，changed 判定就会错乱（该真实校验时走了缓存，反过来也一样）。
# 锁只护住四个字段的读写，**绝不覆盖 http_content_ok**（那是秒级 I/O，持锁会串行化两
# 个线程的网络请求）。
_CONTENT_CACHE = {
    "public_ok": None,    # 上一轮 TCP 探测是否判定公网通
    "state": None,        # 上一轮最终状态
    "content_ok": False,  # 上次内容校验是否通过
    "last_ok_ts": 0.0,    # 上次内容校验"通过"的时间戳
}
_CONTENT_CACHE_LOCK = threading.Lock()


def _content_cache_read(keys):
    with _CONTENT_CACHE_LOCK:
        return tuple(_CONTENT_CACHE[k] for k in keys)


def _content_cache_write(**kv):
    with _CONTENT_CACHE_LOCK:
        _CONTENT_CACHE.update(kv)


def check_network(cfg):
    """返回 ONLINE / UNAUTHENTICATED / OFFLINE。

    公网 TCP 不可达有两种截然不同的原因：连着校园网但没认证，或者本地网络
    根本没连上。旧实现把两者都当成「未认证」，于是半夜断网时会每 30 秒跑一次
    完整登录流程，每轮白等最多 12 秒 socket 超时。用认证网关自身的可达性区分。

    1.2 内容校验缓存：http_content_ok（GET + 读 8KB）是每轮最重的一步。仅当
    「本轮 TCP 判定公网通」且「上一轮也是同一状态（未变化）」且「上次内容校验
    通过」且「距上次通过未超过 content_check_interval」时才跳过真实校验。

    准确表述（这是一个有界的条件性 trade-off，不是"零代价"）：
      * 只要 TCP 结果变化 / 上次校验失败 / 超时 / 从未校验，一律真实校验，绝不受缓存影响；
      * 但确实存在一个有界窗口：当「TCP 一直可达」而「页面内容在窗口内变成 NAS 拦截页」
        时，最多 content_check_interval（默认 300 秒）内会沿用上一条 ONLINE 结论，
        即该窗口内可能把"未认证"短暂当成"在线"（有界、条件性误判窗口）。
      * 触发前提较强：目标环境里未认证时 NAS 通常对公网 IP 丢包/拒绝，TCP 探测会变为
        不可达 → 直接判未认证，因此该窗口在目标环境一般不会触发；而且最长 5 分钟后必
        会重新真实校验并纠正。想要更保守可把 content_check_interval 调小（如 60）。
    """
    public_ok = public_tcp_ok()
    if public_ok:
        # classify_network 只在 public_ok 为假时才看 portal_ok，而 Python 是急切求值：
        # 写成 classify_network(public_ok, tcp_reachable(...)) 会让「已经在线」的每一轮都
        # 白跑一次最长 3 秒的网关 TCP。这里显式短路，在线时不再碰网关。
        state = ONLINE
    else:
        portal_ok = tcp_reachable((cfg["portal_host"], 80))
        if not portal_ok:
            # 公网 IP 与认证网关**都不通**：这时「本地真断网」和「那三个 IP 恰好被
            # 运营商屏蔽、公网其实通」无法区分，值得多花一次域名探测——纯 IP 探测会
            # 让后一种环境永久误判为离线（每轮退避重试，用户以为程序坏了）。
            # 未认证的常见情形是"IP 不通但网关通"，走不到这里，所以常见路径不会变慢。
            public_ok = _public_host_ok(2)
        state = classify_network(public_ok, portal_ok)

    if state == ONLINE:
        interval = _cfg_int(cfg, "content_check_interval", minimum=1)
        now = time.time()
        prev_public_ok, prev_state, cache_ok, cache_ts = _content_cache_read(
            ("public_ok", "state", "content_ok", "last_ok_ts"))
        changed = (public_ok != prev_public_ok) or (state != prev_state)
        fresh = cache_ok and (now - cache_ts < interval)
        if changed or not fresh:
            ok = http_content_ok(cfg)      # 真实校验（锁外执行，别把 I/O 圈进锁里）
            if ok:
                _content_cache_write(content_ok=True, last_ok_ts=now)
            else:
                _content_cache_write(content_ok=False)
                if ok is False:
                    # 明确拿到了拦截页：公网 TCP 通但内容是门户 => 仍属未认证
                    state = UNAUTHENTICATED
                # ok is None：请求本身没成功（超时/连接失败），属于"未知"。公网
                # TCP 是通的，此时推翻成未认证会让每 30 秒完整登录一次——抖动时
                # 这个代价很大，所以沿用在线结论，下一轮继续真实校验。
                else:
                    logging.debug("内容校验请求失败（网络抖动），本轮沿用在线结论")
        # else：缓存命中，直接信任 ONLINE，跳过最重的 HTTP 请求
    else:
        # 非在线状态：作废缓存，下次一旦变 ONLINE 必须重新真实校验
        _content_cache_write(content_ok=False)

    _content_cache_write(public_ok=public_ok, state=state)
    return state


def encrypt_text(key_b64, text):
    key = base64.b64decode(key_b64)
    return base64.b64encode(aes_ecb_pkcs7_encrypt(key, text.encode("utf-8"))).decode("ascii")


# ============================ sessionId 获取 ============================

def _http_alive(op, url, timeout=3):
    """只探 HTTP 会话建不建得起来（不关心内容），用来给失败原因定性。"""
    try:
        r = http_get(op, url, timeout=timeout)
        r.read(2048)
        r.close()
        return True
    except urllib.error.HTTPError:
        # 能返回 4xx/5xx/302（哪怕是重定向环被 urllib 报成异常）都说明 HTTP 服务活着，
        # 以前一律当"无响应"，于是把"网关正常但已在线"误报成"网关不可达"。
        return True
    except Exception:
        return False


# ============================ 认证网关的自动发现 ============================
#
# `portal_host` 在 DEFAULT_CONFIG 里那个默认值**只对作者所在学校有效**——别的学校
# 门户地址完全不同，写死就等于这个程序只能在一所学校用。所以必须能自己找到网关：
# 连着校园网但未认证时，访问任意 HTTP 地址都会被 NAS 劫持，返回一个指向认证门户的
# 跳转，门户地址就写在跳转目标里。这是唯一能跨学校通用的发现方式。

# 「诱饵」地址：故意选没人用的 IP。未认证时 NAS 会劫持它们并返回门户跳转；已认证
# 或不在校园网时请求会真发到公网，自然没人回（超时是正常判定信号，不是故障）。
PORTAL_DECOYS = ("123.123.123.123", "1.1.1.1")
# 跳转目标要命中以下任一特征才认可是认证门户，避免把普通网站的跳转当成门户
_PORTAL_MARKS = ("/eportal/", "/portal/", "cas-sso", "sessionId=",
                 "wlanuserip", "index.jsp")


def portal_host_from(url):
    """从跳转 URL 里取出门户地址（IP 或域名）；不像门户则返回空串。"""
    if not url:
        return ""
    try:
        p = urllib.parse.urlparse(url)
        host = (p.netloc.split("@")[-1].split(":")[0] or "").strip()
        blob = (p.path or "") + "?" + (p.query or "")
    except Exception:
        return ""
    if not host or host in PORTAL_DECOYS:
        return ""
    if not any(k in blob for k in _PORTAL_MARKS):
        return ""
    return host


def discover_portal_host(timeout=3):
    """主动探测认证网关地址，探不到返回空串（界面「自动探测」按钮用）。

    刻意用 http.client 而不是 urllib：urllib 会自动跟随重定向，等它停下时原始
    Location 已经丢了，而这里要的正是那个还没被跟随的第一跳。
    """
    for decoy in PORTAL_DECOYS:
        try:
            conn = http.client.HTTPConnection(decoy, 80, timeout=timeout)
            try:
                conn.request("GET", "/", headers={"User-Agent": UA_BROWSER})
                resp = conn.getresponse()
                loc = resp.getheader("Location") or ""
                body = resp.read(8192).decode("utf-8", "ignore")
            finally:
                conn.close()
        except Exception as e:
            logging.debug("探测 %s 未获响应（%s）", decoy, e)
            continue
        base = "http://%s/" % decoy
        cand = urllib.parse.urljoin(base, loc) if loc else ""
        if not cand:
            m = re.search(r'location\.href\s*=\s*["\']([^"\']+)["\']', body)
            if m:
                cand = urllib.parse.urljoin(base, m.group(1))
        host = portal_host_from(cand)
        if host:
            logging.info("自动探测到认证网关：%s", host)
            return host
    return ""


def get_session_info(op, host):
    """
    触发 H3C NAS 的 captive portal 跳转，拿到 sessionId 等流程参数。
    流程：访问 123.123.123.123 -> NAS 返回 JS 跳转到 /eportal/index.jsp?wlanuserip=...&wlanacname=...
         -> 访问它 -> 302 到 /portal/portal-main?sessionId=...&userIp=...&nasIp=...&customPageId=...

    返回 (params_dict|None, reason)：
      reason = ok / gateway_unreachable / no_redirect / params_missing
    以前只返回 None，调用方只能报一句笼统的「请确认处于连着网但未认证状态」——
    实测断网时它会连续 50+ 次报同一句，既误导（真正原因往往是 NAS 的 HTTP 没响应）
    又把日志刷爆。现在把"网关本身就回不了 HTTP"和"网关通但没给跳转"分开。
    """
    js_redirect = None
    # 超时从 (4, 3) 收到 (3, 2)：未认证时 NAS 是**立刻**劫持并返回跳转的，正常路径
    # 根本等不到超时；这两个超时只在「其实已经认证 / 不在校园网」时才被吃满，而那是
    # 每轮都要白等的纯浪费。同一次 run_login_flow 最坏从 7 秒降到 5 秒。
    for trigger, tmo in (("http://123.123.123.123/", 3), ("http://1.1.1.1/", 2)):
        try:
            r = http_get(op, trigger, timeout=tmo)
            final_url = r.geturl()
            body = r.read().decode("utf-8", "ignore")
            r.close()
            # 方式一：旧环境 NAS 返回 <script>location.href="..."</script>
            m = re.search(r'location\.href="([^"]+)"', body)
            if m:
                js_redirect = m.group(1)
                logging.info("NAS 跳转(JS): %s", mask_secret(js_redirect))
                break
            # 方式二：新环境 NAS 直接 HTTP 302 到认证页（urllib 已自动跟随）
            # 若最终 URL 已经在认证门户域下且带 eportal/portal 路径，直接用它
            if any(k in final_url for k in ("/eportal/", "/portal/", "cas-sso", "sessionId=")):
                js_redirect = final_url
                logging.info("NAS 跳转(302): %s", mask_secret(js_redirect))
                break
        # 这两个地址是刻意挑的「诱饵」：未认证时 NAS 会劫持它们并返回 portal 跳转；
        # 已认证 / 不在校园网时请求会真发到公网，自然没人回 -> 超时。
        # 所以「超时」是正常判定信号，不是故障，别写成「异常」吓人。
        except urllib.error.HTTPError as e:
            # urllib 把「重定向环」也报成 HTTPError，此时 Location 往往就是门户地址
            loc = (e.headers.get("Location", "") if e.headers else "")
            cand = urllib.parse.urljoin(trigger, loc) if loc else ""
            # 这里**不能**要求 cand 包含配置里的 host：那样就成了"得先知道网关才能
            # 发现网关"，换一所学校就永远探不到。改成"只要它长得像门户就认"。
            if portal_host_from(cand):
                js_redirect = cand
                logging.info("NAS 跳转(302 终止): %s", mask_secret(js_redirect))
                break
            logging.info("探测 %s 未被 NAS 拦截（%s）——当前已认证或不在校园网认证范围内", trigger, e)
        except Exception as e:
            logging.info("探测 %s 未被 NAS 拦截（%s）——当前已认证或不在校园网认证范围内", trigger, e)

    if not js_redirect:
        # 两个触发地址都没给出跳转：再探一次门户网关自身，区分「网络层/NAS 有问题」
        # 和「网关正常，但你其实已经在别的地方认证过了」——这两种情况的处置完全不同。
        if not _http_alive(op, "http://%s/" % host):
            return None, "gateway_unreachable"
        return None, "no_redirect"

    try:
        r = http_get(op, js_redirect, timeout=10)
        final = r.geturl()
        r.close()
    except urllib.error.HTTPError as e:
        final = e.headers.get("Location", "")
    except Exception as e:
        logging.warning("访问 /eportal/index.jsp 异常: %s", e)
        return None, "params_missing"

    logging.info("最终重定向: %s", mask_secret(final))
    qs = urllib.parse.urlparse(final).query
    params = dict(urllib.parse.parse_qsl(qs))
    if params.get("sessionId"):
        # 带上真正的门户地址：配置里那个默认值可能属于另一所学校，后续每一步
        # （getCurrentNode / CAS 登录 / serviceLogin）都得用探测到的这个。
        params["portalHost"] = portal_host_from(js_redirect) or host
        return params, "ok"
    return None, "params_missing"


# 同一类失败原因的重复计数（见 _fail_no_session）
_FAIL_REPEAT = {}
_FAIL_REPEAT_LOCK = threading.Lock()


def _fail_no_session(why, host):
    """拿不到 sessionId 时的**分情况**诊断 + 日志节流。

    以前无论什么情况都报同一句「请确认处于连着网但未认证状态」，实测在 NAS 繁忙
    或校园网出口不通时会连报几十次，既看不出真正原因也刷爆日志。现在按原因分类，
    并且同一原因**只记首次 + 每 10 次一条汇总**。
    """
    if why == "gateway_unreachable":
        short = "连不上认证网关（校园网出口可能未通）"
        detail = ("认证网关 %s 的 HTTP 服务没有响应：TCP 能连上但取不到任何 HTTP 内容，"
                  "通常是 NAS 繁忙/维护，或当前网络不在校园网认证范围内" % host)
    elif why == "no_redirect":
        short = "认证网关没返回登录跳转（可能已经在线）"
        detail = ("认证网关 %s 可达，但没有返回 portal 跳转页——通常意味着这台设备"
                  "已经认证过了，或当前网络不在校园网认证范围内" % host)
    else:
        short = "未能获取 sessionId（需处于连着校园网但未认证的状态）"
        detail = short

    with _FAIL_REPEAT_LOCK:
        n = _FAIL_REPEAT.get(why, 0) + 1
        _FAIL_REPEAT[why] = n
    global _LAST_FAIL_WHY
    _LAST_FAIL_WHY = why
    if n == 1:
        logging.error("%s", detail)
    elif n % 10 == 0:
        logging.error("%s（已连续 %d 次）", detail, n)
    else:
        logging.debug("%s（连续 %d 次，已节流）", detail, n)
    return _set_fail_reason(short)


def _reset_fail_repeat():
    """登录成功后清零连败计数（下次若再失败，仍然从"首次"开始完整记录）。"""
    global _AUTH_REJECT_STREAK
    with _FAIL_REPEAT_LOCK:
        _FAIL_REPEAT.clear()
    _AUTH_REJECT_STREAK = 0


# ============================ 登录流程 ============================

def parse_login_page(page):
    m = re.search(r'id=["\']login-croypto["\'][^>]*>(.*?)<', page, re.S)
    e = re.search(r'id=["\']login-page-flowkey["\'][^>]*>(.*?)<', page, re.S)
    if m and e:
        return m.group(1).strip(), e.group(1).strip()
    m = re.search(r'login-croypto[^>]*>([^<]{8,})<', page)
    e = re.search(r'login-page-flowkey[^>]*>([^<]{8,})<', page)
    if m and e:
        return m.group(1).strip(), e.group(1).strip()
    return None, None


def run_login_flow(cfg):
    """完整登录流程。成功 True；失败 False（原因概要可用 get_fail_reason() 取）。"""
    global _LAST_FAIL_REASON, _LAST_FAIL_WHY
    _LAST_FAIL_REASON = ""
    _LAST_FAIL_WHY = ""
    host = cfg["portal_host"]
    custom_page_id = cfg["customPageId"]
    nas_ip = cfg["nasIp"]
    if not host:
        # 配置里没写网关（对外分发时可以预置为空）：先自己探一次，探不到再走常规
        # 失败流程。这样别人拿到程序什么都不用填，连上网点登录就能用。
        host = discover_portal_host()
        if host:
            logging.info("未配置认证网关，自动探测到 %s", host)
            cfg["portal_host"] = host
            try:
                save_config(cfg)
            except Exception as e:
                logging.warning("保存探测到的网关地址失败（不影响本次登录）：%s", e)

    op, cj, rec = make_opener()

    # 1) 触发 NAS 跳转，拿 sessionId 等参数
    info, why = get_session_info(op, host)
    if not info:
        logging.info("Cookies(仅名称): %s", [c.name for c in cj])
        if why in ("gateway_unreachable", "no_redirect"):
            # 最常见的原因就是**门户地址不对**（默认值只对某一所学校有效）。主动探
            # 一次，把探到的地址写进日志，用户照着填或点「自动探测」即可。
            found = discover_portal_host()
            if found and found != host:
                logging.info("探测到本机所在网络的认证网关是 %s（当前配置为 %s）——"
                             "请在设置页「高级参数」改成它，或点「自动探测」",
                             found, host)
        return _fail_no_session(why, host)
    # 门户地址一律以网关实际返回的为准：配置里那个默认值只对作者所在学校有效，换一
    # 所学校必须跟着网关走，否则后面 getCurrentNode / CAS 登录 / serviceLogin 每一步
    # 都会打到错误地址上。探到新地址就写回配置，下次启动直接用。
    found = info.get("portalHost") or ""
    if found and found != host:
        logging.info("认证网关 %s -> %s（自动更正并保存）", host, found)
        host = found
        cfg["portal_host"] = found
        try:
            save_config(cfg)
        except Exception as e:
            logging.warning("保存探测到的网关地址失败（不影响本次登录）：%s", e)
    session_id = info["sessionId"]
    custom_page_id = info.get("customPageId") or cfg["customPageId"]
    nas_ip = info.get("nasIp") or cfg["nasIp"]
    # 这两个参数**运行时以网关下发的为准**，但以前只改内存不写盘，于是设置页里
    # 一直显示旧值（换学校后更是显示上一所学校的），看起来就是"参数不对"。
    # 探到新值就写回配置，界面显示的和实际生效的才是同一个。
    updated = {}
    if custom_page_id and custom_page_id != cfg.get("customPageId"):
        updated["customPageId"] = custom_page_id
    if nas_ip and nas_ip != cfg.get("nasIp"):
        updated["nasIp"] = nas_ip
    if updated:
        logging.info("网关下发参数更新（自动保存）：%s",
                     "，".join("%s -> %s" % (k, v) for k, v in updated.items()))
        cfg.update(updated)
        try:
            save_config(cfg)
        except Exception as e:
            logging.warning("保存网关下发的参数失败（不影响本次登录）：%s", e)
    user_ip = info.get("userIp") or cfg.get("userIp", "")
    logging.info("sessionId = %s, userIp = %s, nasIp = %s, customPageId = %s",
                 mask_cred(session_id), user_ip, nas_ip, custom_page_id)

    # 2) getCurrentNode
    try:
        r = http_post(op, "http://%s/eportal/workFlow/getCurrentNode" % host,
                      json_body={"sessionId": session_id, "flowKey": "portal_auth"})
        b = r.read().decode("utf-8", "ignore")
        r.close()
        logging.info("getCurrentNode -> %s", mask_secret(b[:300]))
    except Exception as e:
        logging.warning("getCurrentNode 异常: %s", e)

    # 3) 若 userIp 仍为空，尝试 queryTerminalInfo 补充
    if not user_ip:
        try:
            r = http_get(op, "http://%s/eportal/adaptor/queryTerminalInfo?sessionId=%s&macAddr=&%d"
                         % (host, session_id, int(time.time() * 1000)), timeout=8)
            b = r.read().decode("utf-8", "ignore")
            r.close()
            ip = (json.loads(b).get("data") or {}).get("ipAddr")
            if ip:
                user_ip = ip
                logging.info("userIp(补充) = %s", user_ip)
        except Exception as e:
            logging.warning("queryTerminalInfo 异常: %s", e)

    # 4) 打开 CAS 登录页，拿密钥和 execution
    timer = int(time.time() * 1000)
    login_url = ("http://%s/cas-sso/login?flowSessionId=%s&customPageId=%s&preview=false"
                 "&appType=normal&language=zh-CN&timer=%d&nasIp=%s&userIp=%s"
                 % (host, session_id, custom_page_id, timer, nas_ip,
                    urllib.parse.quote(str(user_ip))))
    try:
        r = http_get(op, login_url, timeout=10)
        page = r.read().decode("utf-8", "ignore")
        r.close()
    except Exception as e:
        logging.warning("打开 CAS 登录页失败: %s", e)
        return _set_fail_reason("打开 CAS 登录页失败（认证网关 %s 无响应）" % host)

    key_b64, execution = parse_login_page(page)
    if not key_b64 or not execution:
        # 不落盘登录页正文：里面含密钥与令牌
        logging.error("未找到 login-croypto / login-page-flowkey（登录页 %d 字节）", len(page))
        return _set_fail_reason("登录页上未找到密钥/令牌（login-croypto / login-page-flowkey）")
    logging.info("已取得 AES 密钥(%d 字符)与 execution(%d 字符)", len(key_b64), len(execution))

    # 5) 加密并提交登录
    form = urllib.parse.urlencode({
        "username": cfg["username"],
        "type": "UsernamePassword",
        "_eventId": "submit",
        "geolocation": "",
        "execution": execution,
        "captcha_code": "",
        "croypto": key_b64,
        "password": encrypt_text(key_b64, cfg["password"]),
        "captcha_payload": encrypt_text(key_b64, "{}"),
    }).encode("utf-8")
    post_url = login_url + "&accept-language=zh-CN"
    login_ok = False
    try:
        req = urllib.request.Request(
            post_url, data=form,
            headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded",
                     "Origin": "http://" + host, "Referer": login_url},
            method="POST")
        resp = op.open(req, timeout=10)
        final = resp.geturl()
        resp.close()
        logging.info("登录提交最终跳转: %s", mask_secret(final))
        login_ok = ("ticket=ST-" in final) or ("auth-success" in final) or ("callbackAuthorize" in final)
    except urllib.error.HTTPError as e:
        loc = e.headers.get("Location", "")
        logging.info("登录提交返回 %s, Location: %s", e.code, mask_secret(loc))
        login_ok = ("ticket=ST-" in loc) or ("callbackAuthorize" in loc)
    except Exception as e:
        logging.warning("登录提交异常: %s", e)
    logging.info("CAS 登录 %s", "成功" if login_ok else "结果未知，继续 serviceLogin")

    # 6) 获取服务列表
    try:
        r = http_post(op, "http://%s/eportal/network/serviceSelection" % host,
                      json_body={"sessionId": session_id},
                      extra_headers={"isPortal": "true"})
        b = r.read().decode("utf-8", "ignore")
        r.close()
        # 同一函数里 serviceLogin 的响应走了 mask_secret，这里也必须脱敏：该接口
        # 会回显 sessionId / userIp，而 login.log 跟 exe 同目录、会随发布包拷走。
        logging.info("serviceSelection 响应: %s", mask_secret(b[:300]))
    except Exception as e:
        logging.warning("serviceSelection 异常: %s", e)

    # 7) serviceLogin 上线（运营商服务由 cfg["service"] 决定，默认移动 cmcc）
    try:
        r = http_post(op, "http://%s/eportal/network/serviceLogin" % host,
                      json_body={"sessionId": session_id, "service": cfg.get("service", "cmcc")},
                      extra_headers={"isPortal": "true"})
        b = r.read().decode("utf-8", "ignore")
        r.close()
        logging.info("serviceLogin 响应: %s", mask_secret(b[:300]))
        ok = ((json.loads(b).get("data") or {}).get("authResult") == "success")
        if ok:
            logging.info("serviceLogin 成功，设备已上线")
            _reset_fail_repeat()      # 成功了，清掉此前的连败计数
            return True
        return _fail_auth_rejected()
    except Exception as e:
        logging.warning("serviceLogin 异常: %s", e)
        return _set_fail_reason("serviceLogin 请求异常：%s" % e)


# ============================ 自助服务：查账号实际绑定的运营商 ============================


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不自动跟随：自助服务的 OAuth 链必须手动逐跳走，否则撞进 /login 自跳环。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _selfservice_opener():
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj),
                                     _NoRedirect)
    op.addheaders = [("User-Agent", UA_BROWSER),
                     ("Accept", "text/html,application/xhtml+xml,*/*;q=0.8")]
    return op, cj


def _fetch_follow(op, url, max_hops=12, timeout=10, data=None, headers=None,
                  method=None):
    """手动跟随重定向，返回 (final_url, status, body, hops)。"""
    cur = url
    hops = []
    seen = set()
    for _ in range(max_hops):
        if cur in seen:
            raise RuntimeError("重定向环: %s" % cur)
        seen.add(cur)
        req = urllib.request.Request(cur, data=data, method=method)
        req.add_header("User-Agent", UA_BROWSER)
        if headers:
            for k, v in headers.items():
                req.add_header(k, v)
        try:
            r = op.open(req, timeout=timeout)
            status = r.status
            loc = r.headers.get("Location", "")
            body = r.read().decode("utf-8", "ignore")
            r.close()
        except urllib.error.HTTPError as e:
            status = e.code
            loc = e.headers.get("Location", "")
            try:
                body = e.read().decode("utf-8", "ignore")
            except Exception:
                body = ""
        hops.append((cur, status, loc))
        if not loc or status == 200:
            return cur, status, body, hops
        cur = urllib.parse.urljoin(cur, loc)
        data = None
        method = "GET"
        headers = None
    raise RuntimeError("重定向超过 %d 跳" % max_hops)


# 绑定运营商的查询缓存：查一次要跑完整 CAS 登录（会**再提交一次密码**），而绑定关系
# 只在 SAM 后台改绑时才变。GUI 上连点几次「保存并立即登录」原本会连打几次 CAS ——
# login.log 实测 23 秒内 4 次。只缓存**查到了**的结果：返回 None 是"这次没查出来"
# （自助服务不通 / 接口变了），把它缓存住会让之后一小时的点击全都不再重试。
_BOUND_OP_TTL = 3600
_BOUND_OP_CACHE = {}
_BOUND_OP_LOCK = threading.Lock()


def fetch_bound_operator(cfg, timeout=12, use_cache=True):
    """查账号在 SAM 后台**实际绑定**的运营商名（如"移动"）。结果按
    (账号, 网关) 缓存 _BOUND_OP_TTL 秒；use_cache=False 强制真实查询。"""
    if not _has_credentials(cfg):
        return None
    key = (cfg["username"], cfg["portal_host"])
    now = time.time()
    if use_cache:
        with _BOUND_OP_LOCK:
            hit = _BOUND_OP_CACHE.get(key)
        if hit and (now - hit[0]) < _BOUND_OP_TTL:
            logging.info("自助服务：绑定运营商复用缓存（%s，%.0f 分钟前查过）",
                         hit[1], (now - hit[0]) / 60.0)
            return hit[1]
    bound = _fetch_bound_operator_uncached(cfg, timeout)
    if bound:
        with _BOUND_OP_LOCK:
            _BOUND_OP_CACHE[key] = (now, bound)
    return bound


def _selfservice_login(op, cfg, timeout=12):
    """在给定 opener 上完成自助服务的 CAS 登录，成功返回 True。

    自助服务（/self）跟校园网认证是**两套会话**：登录它不会占用校园网的在线名额，
    也不会把已在线的设备挤掉——这正是它能当"旁观者"去查谁在线的前提。
    """
    host = cfg["portal_host"]
    final_url, _st, page, _ = _fetch_follow(
        op, "http://%s/self/index" % host, timeout=timeout)
    key_b64, execution = parse_login_page(page)
    if not key_b64 or not execution:
        return False
    form = urllib.parse.urlencode({
        "username": cfg["username"],
        "type": "UsernamePassword",
        "_eventId": "submit",
        "geolocation": "",
        "execution": execution,
        "captcha_code": "",
        "croypto": key_b64,
        "password": encrypt_text(key_b64, cfg["password"]),
        "captcha_payload": encrypt_text(key_b64, "{}"),
    }).encode("utf-8")
    _fetch_follow(op, final_url + "&accept-language=zh-CN", data=form,
                  headers={"Content-Type": "application/x-www-form-urlencoded",
                           "Origin": "http://" + host, "Referer": final_url},
                  method="POST", timeout=timeout)
    return True


def _fetch_bound_operator_uncached(cfg, timeout=12):
    """真实查询（无缓存）——会完整跑一遍自助服务的 CAS 登录。

    为什么需要它：网关的 serviceLogin 未必校验运营商，账号在 SAM 后台却是绑死
    某一个运营商的。没有这个校验，用户选错运营商也会看到绿色的"成功"——正是本次
    要消除的假成功。

    查不到（无凭据 / 自助服务不通 / 接口变了）一律返回 None，调用方必须按
    "未知"处理，**不得**据此把正常登录判成失败。
    """
    if not _has_credentials(cfg):
        return None
    host = cfg["portal_host"]
    op, _cj = _selfservice_opener()
    try:
        # 1) 自助服务入口 -> CAS 登录页 -> 提交登录（与校园网登录同字段，密码 AES 加密）
        if not _selfservice_login(op, cfg, timeout=timeout):
            logging.info("自助服务：登录页未取到密钥/令牌")
            return None
        # 2) 查绑定的运营商
        req = urllib.request.Request(
            "http://%s%s" % (host, SELF_OPERATORS_API),
            headers={"User-Agent": UA_BROWSER, "Accept": "application/json,*/*"})
        r = op.open(req, timeout=timeout)
        b = r.read().decode("utf-8", "ignore")
        r.close()
        ops = ((json.loads(b).get("data") or {}).get("operators") or [])
        if ops:
            name = ops[0].get("operatorsName")
            if name:
                logging.info("自助服务：账号绑定运营商 = %s", name)
                return str(name)
    except Exception as e:
        logging.info("自助服务查绑定运营商失败（按未知处理）: %s", e)
    return None


# ============================ 在线设备判定：到底是谁在用这个账号 ============================

_LOCAL_MAC_CACHE = {"ts": 0.0, "macs": set()}
_LOCAL_MAC_TTL = 600                      # 网卡不常换，10 分钟刷一次足够

_DEV_MAC_RE = re.compile(r"^[0-9A-Fa-f]{12}$")


def norm_mac(value):
    """MAC 归一化成 12 位小写无分隔符；不合法返回空串。

    网关返回 "FC-5C-EE-BF-A8-DF"，PowerShell 给 "FC5CEEBFA8DF" 或带横杠——
    统一后再比，别靠大小写碰运气。
    """
    v = re.sub(r"[^0-9A-Fa-f]", "", str(value or ""))
    return v.lower() if _DEV_MAC_RE.match(v) else ""


def pretty_mac(value):
    """显示用格式：FC:5C:EE:BF:A8:DF（空值返回空串）。"""
    v = norm_mac(value)
    return ":".join(v[i:i + 2] for i in range(0, 12, 2)).upper() if v else ""


def local_mac_addresses(force=False):
    """本机**物理**网卡的 MAC 集合（12 位小写无分隔符形式）。

    两个实测踩过的坑：
      1. `getmac` 在多网卡机器上给的**不是正在上网的那块**（本次返回
         FC-B0-DE-57-5B-24，真实上网的是 FC-5C-EE-BF-A8-DF）——拿它当判据会
         把"我自己在线"误判成"别的设备在上网"，那还不如不用；
      2. 中文 Windows 下 getmac / ipconfig 输出是 GBK，按 utf-8 解码会抛
         UnicodeDecodeError，把整个 MAC 列表弄丢（必须显式 gbk + errors）。
    顺序：PowerShell(最准) -> ipconfig(GBK 解析) -> uuid.getnode(兜底)。
    """
    now = time.time()
    if (not force and _LOCAL_MAC_CACHE["macs"]
            and (now - _LOCAL_MAC_CACHE["ts"]) < _LOCAL_MAC_TTL):
        return _LOCAL_MAC_CACHE["macs"]
    macs = set()
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-NetAdapter -Physical | Where-Object {$_.Status -eq 'Up'} | "
             "Select-Object -ExpandProperty MacAddress | ConvertTo-Json -Compress"],
            capture_output=True, timeout=30, encoding="utf-8", errors="ignore",
            # 必须隐藏窗口：--windowed 打包下父进程没有控制台，PowerShell 会自己
            # 开一个（蓝框闪约 1 秒），而这次查询恰好在界面启动瞬间跑。
            creationflags=CREATE_NO_WINDOW)
        txt = (res.stdout or "").strip()
        if txt:
            data = json.loads(txt)
            for item in (data if isinstance(data, list) else [data]):
                m = norm_mac(item)
                if m:
                    macs.add(m)
    except Exception:
        pass
    if not macs:
        try:
            res = subprocess.run(["ipconfig", "/all"], capture_output=True,
                                 timeout=30, encoding="gbk", errors="ignore",
                                 creationflags=CREATE_NO_WINDOW)
            for line in (res.stdout or "").splitlines():
                m = re.search(r"物理地址[\.:\s]*([0-9A-Fa-f\-]{17})", line)
                if m:
                    mac = norm_mac(m.group(1))
                    if mac:
                        macs.add(mac)
        except Exception:
            pass
    if not macs:
        try:
            import uuid as _uuid
            mac = norm_mac("%012X" % _uuid.getnode())
            if mac:
                macs.add(mac)
        except Exception:
            pass
    _LOCAL_MAC_CACHE["ts"] = now
    _LOCAL_MAC_CACHE["macs"] = macs
    return macs


def local_gateway_ip(host):
    """本机访问网关时使用的 IPv4（UDP connect 取 sockname：不发包、零开销）。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((host, 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return ""


def primary_mac_address(host=""):
    """正在上网的那块网卡的 MAC（形如 FC:5C:EE:BF:A8:DF），取不到返回空串。

    多网卡机器（有线 + 无线 + 虚拟机网卡）上随便挑一个，很可能挑到没在用的那块
    ——界面要回答"我是哪台设备"，显示错了比不显示更糟。所以优先按**出口 IP** 反查
    对应的网卡，查不到才退回物理网卡列表里稳定的第一个（排序后取首个，保证每次一致）。
    """
    ip = local_gateway_ip(host) if host else ""
    if ip:
        try:
            res = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "$a = Get-NetIPAddress -IPAddress '%s' -AddressFamily IPv4 | "
                 "Select-Object -First 1; if ($a) { "
                 "(Get-NetAdapter -InterfaceIndex $a.InterfaceIndex | "
                 "Select-Object -ExpandProperty MacAddress) }" % ip],
                capture_output=True, timeout=30, encoding="utf-8", errors="ignore",
                creationflags=CREATE_NO_WINDOW)
            out = (res.stdout or "").strip()
            if out:
                m = norm_mac(out.splitlines()[0])
                if m:
                    return pretty_mac(m)
        except Exception:
            pass
    macs = sorted(local_mac_addresses())
    return pretty_mac(macs[0]) if macs else ""


# ============================ 在线设备查询（只读） ============================
#
# 自助服务有个接口直接列出「当前哪些设备在线」，每条带 MAC / IP / 设备类型。
# 登录自助服务走的是 CAS 会话，跟校园网认证是两套会话——登录自助服务**不会**
# 占用校园网名额、不会把已在线的设备挤掉，所以它可以放心地当"旁观者"。
#
# 同一套 API 里还有 devices/kick-offline/batch（踢下线）、devices/modify、
# nosense/unbind/batch 等改动类接口——本程序**一律不调用**，只做只读查询。

SELF_DEVICES_API = "/sam/api/userself/devices"

_ONLINE_DEV_CACHE = {"ts": 0.0, "devices": None}

# classify_online_devices 的四种结论
DEV_MINE = "mine"          # 在线的是本机
DEV_OTHER = "other"        # 在线的是别的设备（手机 / 另一台电脑）
DEV_NONE = "none"          # 没人在线
DEV_UNKNOWN = "unknown"    # 查不出来（自助服务不通 / 接口变了）


def _short_mac(mac):
    """日志/界面只显示 MAC 尾号：够区分设备，又不把整机 MAC 写进日志。"""
    m = norm_mac(mac)
    return ("尾号 %s" % m[-4:].upper()) if len(m) >= 4 else "未知"


def fetch_online_devices(cfg, timeout=12, use_cache=True):
    """查「当前哪些设备在线」，返回 list[dict]；**查不到返回 None（未知）**。

    每条：mac / ip / type(电脑|手机|平板) / current / duration / name / auth。
    """
    if not _has_credentials(cfg):
        return None
    now = time.time()
    if use_cache and _ONLINE_DEV_CACHE["devices"] is not None:
        ttl = _cfg_int(cfg, "device_check_cache_seconds", minimum=10)
        if (now - _ONLINE_DEV_CACHE["ts"]) < ttl:
            return _ONLINE_DEV_CACHE["devices"]
    op, _cj = _selfservice_opener()
    try:
        if not _selfservice_login(op, cfg, timeout=timeout):
            return None
        req = urllib.request.Request(
            "http://%s%s" % (cfg["portal_host"], SELF_DEVICES_API),
            headers={"User-Agent": UA_BROWSER, "Accept": "application/json,*/*"})
        r = op.open(req, timeout=timeout)
        b = r.read().decode("utf-8", "ignore")
        r.close()
        data = (json.loads(b).get("data") or {})
        devices = []
        for d in (data.get("onlineDevices") or []):
            devices.append({
                "mac": norm_mac(d.get("userMac")),
                "ip": d.get("userIpv4") or "",
                "type": d.get("deviceType") or "",
                "current": bool(d.get("currentDevice")),
                "duration": d.get("onlineDuration") or "",
                "name": d.get("deviceName") or "",
                "auth": d.get("authType") or "",
            })
        _ONLINE_DEV_CACHE["ts"] = now
        _ONLINE_DEV_CACHE["devices"] = devices
        return devices
    except Exception as e:
        logging.info("自助服务查在线设备失败（按未知处理）: %s", e)
        return None


def classify_online_devices(devices, mine_macs=None, my_ip=""):
    """判定在线设备是不是本机，返回 (结论, 说明文字)。

    devices 为 None（查不出来）时返回 DEV_UNKNOWN：调用方必须按"未知"处理，
    **绝不能**据此当成"没人在线"去抢线。
    """
    if devices is None:
        return DEV_UNKNOWN, ""
    if not devices:
        return DEV_NONE, "当前没有设备在线"
    mine = local_mac_addresses() if mine_macs is None else mine_macs
    for d in devices:
        if d.get("mac") and d["mac"] in mine:
            return DEV_MINE, "%s（%s，已在线 %s）" % (
                d.get("type") or "设备", d.get("ip") or "无 IP",
                d.get("duration") or "未知时长")
    # MAC 对不上时再用 IP 兜一次（虚拟网卡 / 网卡改名会让 MAC 取不全）
    if my_ip:
        for d in devices:
            if d.get("ip") and d["ip"] == my_ip:
                return DEV_MINE, "%s（%s，按 IP 匹配）" % (
                    d.get("type") or "设备", my_ip)
    d = devices[0]
    return DEV_OTHER, "%s（%s，IP %s，已在线 %s）" % (
        d.get("type") or "其他设备", _short_mac(d.get("mac")),
        d.get("ip") or "无 IP", d.get("duration") or "未知时长")


def check_online_owner(cfg):
    """「现在是谁在用这个账号」——查在线设备并判定归属，返回 (结论, 说明文字)。

    这是保活循环里判断该不该让位的**首选依据**：确证在线的是别的设备就让位，
    确证没人在线/是本机就正常登录，只有查不出来（DEV_UNKNOWN）才退回窗口推断。
    """
    devices = fetch_online_devices(cfg)
    return classify_online_devices(
        devices, my_ip=local_gateway_ip(cfg.get("portal_host", "")))


# ============================ 版本更新检查（GitHub Releases） ============================
#
# 程序**不自动下载替换**：exe 运行时被自己占用（保活 daemon 也持有它），活着的时候
# 覆盖自身必然失败。所以这里只做「查到并提示」，下载由用户手动完成——跟使用说明里
# 写的升级方式一致。
#
# 版本清单直接复用 GitHub 的 Release API，不需要自己维护 version.json：每次发版在
# GitHub 上打一个 Release 就等于发布了新版本。两个必须遵守的约束：
#   1) 仓库必须 Public——匿名请求读不到私有仓库的 Release（返回 404）；
#   2) api.github.com 匿名限流 60 次/小时/**出口 IP**。校园网是 NAT 出口，几十个人
#      共用一个 IP，所以只在用户主动点击时查询、失败一律静默，绝不每次启动都打。

GITHUB_API_LATEST = "https://api.github.com/repos/%s/releases/latest"
GITHUB_ATOM = "https://github.com/%s/releases.atom"
UPDATE_TIMEOUT = 6              # 未认证时公网会被 NAS 拦，超时必须短，别把界面卡住
UPDATE_MIN_GAP = 30             # 进程内冷却秒数，防连点把限流打满
_LAST_UPDATE_TS = 0.0           # 上次实际发起查询的时间戳（进程内冷却用）


def parse_version(text):
    """把 "v2.10.0" 拆成 (2, 10, 0)；解析不出数字返回 None。

    必须按数字段拆：字符串比较会把 "v2.10.0" 判成小于 "v2.9.0"（因为 '1' < '9'），
    版本号一旦过 10 就会「明明是新版却提示已是最新」。
    """
    m = re.search(r"(\d+(?:\.\d+)*)", str(text or ""))
    if not m:
        return None
    try:
        return tuple(int(x) for x in m.group(1).split("."))
    except ValueError:
        return None


def version_gt(a, b):
    """a 是否比 b 新。任一侧解析不出数字时返回 False——宁可漏报也不误报更新。"""
    va = a if isinstance(a, tuple) else parse_version(a)
    vb = b if isinstance(b, tuple) else parse_version(b)
    if not va or not vb:
        return False
    n = max(len(va), len(vb))
    # 位数不同补零：2.1 与 2.1.0 应当视为相等
    va = va + (0,) * (n - len(va))
    vb = vb + (0,) * (n - len(vb))
    return va > vb


def _parse_release(data):
    """从 GitHub Release JSON 里挑出需要的字段。"""
    if not isinstance(data, dict):
        return None
    tag = (data.get("tag_name") or data.get("name") or "").strip()
    if not tag:
        return None
    url, size = "", 0
    for a in (data.get("assets") or []):
        if (a.get("name") or "").lower().endswith(".exe"):
            url = a.get("browser_download_url") or ""
            size = int(a.get("size") or 0)
            break
    page = data.get("html_url") or ""
    return {"version": tag, "url": url or page, "page": page, "size": size,
            "notes": (data.get("body") or "").strip(),
            "published": (data.get("published_at") or "")[:10]}


def _fetch_latest_api(repo, timeout=UPDATE_TIMEOUT):
    """主查询：Release API，字段最全（能拿到 exe 直链、大小、更新说明）。"""
    req = urllib.request.Request(
        GITHUB_API_LATEST % repo,
        headers={"User-Agent": UA_BROWSER, "Accept": "application/vnd.github+json"})
    r = urllib.request.urlopen(req, timeout=timeout)
    try:
        return _parse_release(json.loads(r.read().decode("utf-8", "ignore")))
    finally:
        r.close()


def _fetch_latest_atom(repo, timeout=UPDATE_TIMEOUT):
    """兜底：releases.atom 是静态订阅源，匿名可读且不限流，API 挂了也能拿到版本号。

    只能拿到版本号和页面链接（没有 exe 直链与大小），所以仅在 API 失败时用。
    """
    req = urllib.request.Request(GITHUB_ATOM % repo,
                                 headers={"User-Agent": UA_BROWSER})
    r = urllib.request.urlopen(req, timeout=timeout)
    try:
        xml = r.read().decode("utf-8", "ignore")
    finally:
        r.close()
    titles = re.findall(r"<title>\s*([^<]+?)\s*</title>", xml)
    if len(titles) < 2:                      # 第 0 个是 feed 标题，第 1 个才是最新 tag
        return None
    page = ""
    for href in re.findall(r'<link[^>]+href="([^"]+)"', xml):
        if "/releases/tag/" in href:
            page = href
            break
    return {"version": titles[1].strip(), "url": page, "page": page,
            "size": 0, "notes": "", "published": ""}


def _quote_repo(repo):
    """把 owner/repo 拼成可安全放进 URL 的形式。

    仓库名里若含非 ASCII 字符（中文等），直接拼进 URL 时 urllib 会按 latin-1
    编码并抛 UnicodeEncodeError，"检查更新"就只能永远返回"没查到"。对 owner
    和仓库名分别 percent 编码即可安全请求。
    （注：GitHub 仓库名实际上只允许 ASCII 字母数字与 . - _，本项目已改用
    纯 ASCII 仓库名，但保留编码以防有人在 config.json 里填了含中文的地址。）
    """
    parts = [p for p in str(repo or "").split("/") if p]
    return "/".join(urllib.parse.quote(p, safe="") for p in parts)


def check_update(repo, current_version, timeout=UPDATE_TIMEOUT):
    """查有没有新版本，返回 (状态, 信息dict)。

    状态：available（有新版本）/ up_to_date（已是最新）/ unknown（查不到）。
    查不到**不算错误**：未认证时公网不通是常态，静默按"没查到"处理即可。
    """
    global _LAST_UPDATE_TS
    repo = (repo or "").strip().strip("/")
    if "/" not in repo:
        return "unknown", {}
    repo = _quote_repo(repo)
    now = time.time()
    if (now - _LAST_UPDATE_TS) < UPDATE_MIN_GAP:
        return "unknown", {}                 # 冷却期内不重复打 GitHub
    _LAST_UPDATE_TS = now
    info = None
    for fetch in (_fetch_latest_api, _fetch_latest_atom):
        try:
            info = fetch(repo, timeout=timeout)
        except Exception as e:
            logging.info("查询最新版本失败（%s）：%s", fetch.__name__, e)
            continue
        if info:
            break
    if not info:
        return "unknown", {}
    if version_gt(info["version"], current_version):
        return "available", info
    return "up_to_date", info


# ============================ 单次执行 / 诊断 / 保活 ============================

def _has_credentials(cfg):
    return bool(str(cfg.get("username", "")).strip() and str(cfg.get("password", "")))


def run_once(cfg):
    """单次「网络检测 + 按需登录」，返回结构化结果 dict：
      state  : ONLINE / UNAUTHENTICATED / OFFLINE（本次结束时的网络状态）
      ok     : 是否达成「可上网」
      message: 面向用户的结论文字
      detail : 失败原因概要（无则为空串）
    """
    state = check_network(cfg)
    if state == ONLINE:
        logging.info("当前已联网，无需登录")
        return {"state": ONLINE, "ok": True,
                "message": "已联网，无需登录", "detail": ""}
    if state == OFFLINE:
        logging.info("本地网络不可达，跳过本次登录")
        return {"state": OFFLINE, "ok": False,
                "message": "本地网络不可达：当前没有连上校园网（检查网线/Wi-Fi），稍后会自动重试",
                "detail": ""}
    # UNAUTHENTICATED：只有这一种状态才需要登录
    if not _has_credentials(cfg):
        logging.warning("当前未认证，但尚未配置账号密码，无法登录")
        return {"state": UNAUTHENTICATED, "ok": False,
                "message": "当前未认证，但尚未配置账号密码——请先填写账号和密码",
                "detail": ""}
    logging.info("当前未认证，开始登录...")
    if run_login_flow(cfg):
        time.sleep(2)
        final_state = check_network(cfg)
        if final_state == ONLINE:
            logging.info("结果: 已联网")
            return {"state": ONLINE, "ok": True,
                    "message": "登录成功，设备已上线", "detail": ""}
        logging.info("结果: 流程完成但尚未联网")
        return {"state": final_state, "ok": False,
                "message": "登录流程已完成，但网络仍未恢复（详见 login.log）", "detail": ""}
    reason = get_fail_reason()
    logging.warning("登录失败，详见日志")
    return {"state": UNAUTHENTICATED, "ok": False,
            "message": "登录失败：%s" % (reason or "未知原因"),
            "detail": reason}


def run_probe(cfg):
    """诊断输出：网络状态、网关可达性、AES 自检、NAS 跳转链、进程优先级、统计。"""
    logging.info("=== 诊断 ===")
    logging.info("程序目录(BASE_DIR): %s", BASE_DIR)
    logging.info("进程优先级类别: %s", get_priority_class())
    logging.info("配置: 基础间隔=%ds 在线间隔=%ds 内容校验间隔=%ds 心跳=%dmin 退避上限=%ds",
                 _cfg_int(cfg, "check_interval_seconds", 1),
                 _cfg_int(cfg, "online_interval_seconds", 1),
                 _cfg_int(cfg, "content_check_interval", 1),
                 _cfg_int(cfg, "log_heartbeat_minutes", 1),
                 _cfg_int(cfg, "max_backoff_seconds", 1))
    # 凭据状态：只报「有没有 / 几位 / 是否加密」，绝不打印密码本身
    pwd = str(cfg.get("password", "") or "")
    if not pwd:
        cred = "未配置（请在界面填写后保存）"
    else:
        cred = "已配置（%d 位，磁盘上是 DPAPI 密文）" % len(pwd)
    logging.info("凭据: 账号=%s，密码=%s", cfg.get("username") or "（空）", cred)
    logging.info("网络状态: %s", check_network(cfg))
    _ip_ok = public_tcp_ok()
    logging.info("公网 TCP 可达(IP): %s", _ip_ok)
    if not _ip_ok:
        # 只在 IP 探测失败时补测域名，免得 --probe 在正常情况下也多等 2 秒
        logging.info("公网域名兜底可达: %s", _public_host_ok(2))
    logging.info("认证网关 %s:80 可达: %s",
                 cfg["portal_host"], tcp_reachable((cfg["portal_host"], 80)))
    logging.info("AES 自检(FIPS-197 + NIST SP 800-38A): %s", "通过" if aes_self_test() else "失败!")
    op, cj, rec = make_opener()
    info, why = get_session_info(op, cfg["portal_host"])
    logging.info("获取到的参数: %s（原因: %s）", mask_secret(info), why)
    logging.info("Cookies(仅名称): %s", [c.name for c in cj])
    logging.info("重定向链: %s", mask_secret(rec.hops))
    logging.info("%s", stats_line())


# OFFLINE 轮次只做一次 TCP 探测（公网 3 路 + 网关 1 路），**不跑登录流程**，单轮成本
# 以毫秒计；而恢复延迟是用户直接感知的（合盖唤醒、插上网线之后要等多久才自动登录）。
# 所以这里不做指数退避到 max_backoff_seconds，压在这个上限内滚动重试。
# 顺带补上了「注册表 Run 兜底自启没有联网触发器」这件事：登录时网络还没起来也无所谓，
# 最多这么多秒之后就会自己连上，不需要 Windows 的网络事件来叫醒。
_OFFLINE_INTERVAL_SECONDS = 60


def next_interval(base, failures, cap):
    """退避间隔：连续失败 n 次后等 base*2^n 秒，封顶 cap。failures<=0 时返回 base。"""
    if failures <= 0:
        return base
    return min(base * (2 ** min(failures, 16)), cap)


# ============================ 多设备互斥：自动让位 ============================
#
# 校园网账号通常只允许一台设备在线。典型冲突：寝室电脑跑着保活，用户在教室用手机
# 认证 -> 电脑被踢 -> 电脑保活立刻重新认证 -> 手机被踢 -> 手机再认证……两台设备
# 就这么互相踢，谁都用不好。
#
# 判据优先级：
#   1) **确证**：自助服务能列出「当前在线设备的 MAC」，不是本机就让位（首选）；
#   2) **推断**：查不出在线设备时，认证成功后撑不过判定窗口就掉线 = 被顶掉。
#      为什么可靠：网络故障不会让网关返回 serviceLogin 成功，断网不会被误判成抢占。
#
# 注意：这里**没有**手动暂停入口——让位完全由上面两条判据自动触发。


class YieldPolicy:
    """判定「是不是被别的设备顶掉了」，并给出本次该让位多久。

    纯逻辑，不碰网络也不碰文件，便于单测。参数每轮从配置刷新（支持热改）。
    """

    def __init__(self, enabled=True, window=180, first_minutes=15, max_minutes=120):
        self.configure(enabled, window, first_minutes, max_minutes)
        self.yield_until = 0.0       # 礼让截止时间戳（0 = 未在礼让）
        self.yield_level = 0         # 连续被顶次数，决定礼让时长递增
        self.last_auth_ok_ts = None  # 上次认证成功的时间（None = 还没成功过）
        self.reason = ""             # 礼让原因（给界面显示）

    def configure(self, enabled, window, first_minutes, max_minutes):
        was_on = getattr(self, "enabled", None)
        self.enabled = bool(enabled)
        self.window = max(30, int(window))
        self.first_minutes = max(1, int(first_minutes))
        self.max_minutes = max(self.first_minutes, int(max_minutes))
        # 热关闭时必须立刻作废进行中的礼让：否则 remaining() 仍 > 0，用户明明把
        # yield_enabled 改成了 false，电脑却还要继续让到原定时间——开关形同虚设。
        if was_on is True and not self.enabled:
            self.yield_until = 0.0
            self.yield_level = 0
            self.last_auth_ok_ts = None
            self.reason = ""

    def note_auth_ok(self, now=None):
        """认证成功时调用。"""
        self.last_auth_ok_ts = time.time() if now is None else now

    def note_online(self, now=None):
        """检测为在线时调用：撑够判定窗口就说明冲突已解除，让位状态归零。"""
        now = time.time() if now is None else now
        # 用 None 而不是 0 作哨兵：时间戳本身就是数值，拿 0 当"没有"会在
        # 时间原点上误判（单测里 t=0 就撞上了）。
        if self.last_auth_ok_ts is not None \
                and (now - self.last_auth_ok_ts) >= self.window:
            self.yield_level = 0
            self.yield_until = 0.0
            self.reason = ""

    def on_offline(self):
        """本地断网：跟设备争抢无关，取消礼让（别因为断网白白让位）。"""
        self.yield_until = 0.0
        self.reason = ""

    def remaining(self, now=None):
        """礼让剩余秒数（0 = 未在礼让）。"""
        now = time.time() if now is None else now
        return max(0.0, self.yield_until - now)

    def check_preempted(self, now=None):
        """在未认证时调用：按窗口**推断**是否被顶，返回让位分钟数，0 = 立即登录。"""
        now = time.time() if now is None else now
        if not self.enabled or self.last_auth_ok_ts is None:
            return 0                 # 未启用 / 从没认证成功过，谈不上被顶
        if (now - self.last_auth_ok_ts) >= self.window:
            return 0                 # 上次认证活够了时长 = 正常掉线，立刻重登
        self.yield_level += 1
        minutes = min(self.first_minutes * (2 ** (self.yield_level - 1)),
                      self.max_minutes)
        self.yield_until = now + minutes * 60
        self.reason = ("检测到其他设备正在使用本账号（本次认证仅维持 %.0f 分钟"
                       "就被顶掉）" % max(1.0, (now - self.last_auth_ok_ts) / 60.0))
        return minutes

    def preempt_by_device(self, now, info=""):
        """**确证**别的设备在用（在线设备的 MAC 不是本机）时的礼让。

        与 check_preempted 的区别：那个是**猜**，这个是**看**（自助服务明确列出
        在线设备是手机还是别的电脑）。礼让时长同样递增，但 reason 会带上对方的
        真实设备类型与在线时长——一眼能看懂到底在让给谁。
        """
        now = time.time() if now is None else now
        self.yield_level += 1
        minutes = min(self.first_minutes * (2 ** (self.yield_level - 1)),
                      self.max_minutes)
        self.yield_until = now + minutes * 60
        self.reason = "检测到其他设备正在使用本账号（%s）" % (info or "非本机 MAC")
        return minutes

    def clear_yield_now(self):
        """提前结束礼让（对方已下线）：清掉礼让期和"上次认证成功"记录。

        只清 yield_until 不够——下一轮 check_preempted 看到"距上次认证很近"会
        **立刻再次判定被顶**，礼让刚结束就又续上，等于提前恢复根本没生效。
        """
        self.yield_until = 0.0
        self.yield_level = 0
        self.last_auth_ok_ts = None
        self.reason = ""


def write_daemon_state(**fields):
    """把保活的让位状态写成 JSON 快照（原子写：tmp + replace）。

    界面只读它来显示。写失败无所谓——那纯粹是展示用的，绝不能影响保活本身。
    """
    try:
        data = read_daemon_state()
        data.update(fields)
        data["updated"] = time.time()
        tmp = DAEMON_STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, DAEMON_STATE_PATH)
    except Exception:
        logging.debug("写 daemon 状态快照失败", exc_info=True)


def read_daemon_state():
    """读 daemon 状态快照；不存在/损坏一律返回空 dict（界面据此按无状态显示）。"""
    try:
        with open(DAEMON_STATE_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def notify_yield(minutes):
    """让位给其他设备时提醒一次（节流；界面开着时跳过）。"""
    global _last_notify_ts
    try:
        now = time.time()
        with _notify_lock:
            if now - _last_notify_ts < _NOTIFY_COOLDOWN_SECONDS:
                return
            _last_notify_ts = now
        if gui_window_open():
            return
        import notify_win
        notify_win.notify("校园网已让位给其他设备",
                          "检测到本账号在其他设备登录，%d 分钟内本机不再自动认证。"
                          % minutes)
    except Exception:
        logging.debug("发送让位通知失败", exc_info=True)


def run_forever(cfg):
    """常驻保活循环。

    间隔策略（1.1 自适应）：
      * 基础间隔 check_interval_seconds（默认 30s）；
      * 连续检测为 ONLINE 达到 2 轮后，放宽到 online_interval_seconds（默认 60s）——
        在线稳定期把探测轮次从 120 次/小时降到约 60 次/小时；
      * 一旦出现任何非 ONLINE（未认证/断网/探测异常），立即回到基础间隔，
        未认证时必须用基础间隔快速重试登录，绝不拖延。
    日志策略（1.5 降噪）：状态未变化时不写日志；只在①状态变化、②登录失败、
      ③每 log_heartbeat_minutes 写一行心跳时记录。故障排查所必需的信息（状态变化、
      失败概要、重试计划、退避间隔）全部保留。
    """
    base = _cfg_int(cfg, "check_interval_seconds", minimum=1)
    online_interval = max(base, _cfg_int(cfg, "online_interval_seconds", minimum=1))
    cap = _cfg_int(cfg, "max_backoff_seconds", minimum=1)
    hb_minutes = _cfg_int(cfg, "log_heartbeat_minutes", minimum=1)
    heartbeat_sec = hb_minutes * 60

    logging.info(
        "校园网保活已启动：基础间隔 %ds；在线稳定(连续 2 轮)后放宽至 %ds；"
        "连败退避上限 %ds；心跳日志每 %dmin 一行",
        base, online_interval, cap, hb_minutes)

    failures = 0
    online_streak = 0      # 连续 ONLINE 轮数（用于判定是否放宽间隔）
    last_state = None      # 上一轮状态（用于判定状态是否变化 -> 是否写日志）
    last_interval = None   # 上一轮实际使用的间隔（退避间隔变化时补记一条）
    last_hb = time.time()  # 上次写日志的时间（用于心跳）

    # 多设备互斥：账号只能一台在线时，确认/推断被别的设备顶掉就主动让位，别互相踢
    policy = YieldPolicy(
        cfg.get("yield_enabled", True),
        _cfg_int(cfg, "yield_detect_window_seconds", minimum=30),
        _cfg_int(cfg, "yield_minutes", minimum=1),
        _cfg_int(cfg, "max_yield_minutes", minimum=1),
    )
    last_hold_kind = None  # "yield" / None（当前是否处于礼让，用于状态显示）
    last_dev_check = 0.0   # 上次查「在线设备」的时间（礼让期间复查的节流）

    while True:
        # 每轮重载一次配置。原来全程只用入参这份启动瞬间的快照：用户在界面改了
        # 密码/运营商/间隔后，界面回执是"已保存并登录成功"，后台却仍拿着**旧密码**
        # 重试——改密场景下必然连续被拒，还会撞上运营商侧的账号锁定。
        # 读失败（文件正被 GUI 写、损坏、DPAPI 解不开）就沿用上一轮 cfg，
        # 绝不让"重载配置"这件事影响保活本身。
        try:
            fresh = load_config()
            # 解不开凭据的新配置不采纳：空密码会被提交给网关，那比用旧密码更糟。
            if fresh != cfg and _has_credentials(fresh):
                cfg = fresh
                base = _cfg_int(cfg, "check_interval_seconds", minimum=1)
                online_interval = max(
                    base, _cfg_int(cfg, "online_interval_seconds", minimum=1))
                cap = _cfg_int(cfg, "max_backoff_seconds", minimum=1)
                hb_minutes = _cfg_int(cfg, "log_heartbeat_minutes", minimum=1)
                heartbeat_sec = hb_minutes * 60
                # 让位参数同样支持热改（比如临时关掉让位、或把礼让时长调大）
                policy.configure(
                    cfg.get("yield_enabled", True),
                    _cfg_int(cfg, "yield_detect_window_seconds", minimum=30),
                    _cfg_int(cfg, "yield_minutes", minimum=1),
                    _cfg_int(cfg, "max_yield_minutes", minimum=1))
                logging.info("检测到配置已更新，已应用：基础间隔 %ds / 在线 %ds / 退避上限 %ds",
                             base, online_interval, cap)
        except Exception:
            logging.debug("重载配置失败，沿用当前配置", exc_info=True)
        _bump_stat("rounds")
        try:
            state = check_network(cfg)
        except Exception:
            # 顶层兜底：单个未捕获异常不该让常驻进程静默死掉
            logging.exception("网络检测异常")
            state = None
            failures += 1

        now = time.time()
        logged = False

        if state == ONLINE:
            if last_state != ONLINE:
                logging.info("网络在线")
                logged = True
                # 只有「从断网/未认证恢复到在线」才值得通知用户——界面可能根本没开着，
                # 而这是他们唯一真正需要知道的事。首次启动（last_state is None）不通知。
                if last_state in (UNAUTHENTICATED, OFFLINE):
                    notify_reconnected()
            elif failures:
                logging.info("网络已恢复正常")
                logged = True
            failures = 0
            online_streak += 1
            # 网络已在线：连败计数一并清零。这个计数原本只在 serviceLogin 成功时
            # 复位，于是历史上连败到 8 次（→1800s 长间隔）之后，即便网络早已恢复
            # 在线，残留值仍会被下一次**真实的**认证失败直接套用 1800s——本该
            # 30 秒恢复的场景要干等半小时。
            if _AUTH_REJECT_STREAK:
                _reset_fail_repeat()
            # 撑够判定窗口说明冲突已解除（手机已经下线），让位级别归零
            policy.note_online(now)
            # 礼让期内却检测到本机在线，只可能是用户手动登录成功了（保活在礼让期
            # 内不会自己发起认证）——冲突已解决，礼让立刻作废。不清的话界面会一直
            # 显示"让位中"，跟实际在线对不上（原实现只在礼让到期时才清快照）。
            if last_hold_kind == "yield":
                policy.clear_yield_now()
                last_hold_kind = None
                write_daemon_state(yield_until=0, reason="")
            interval = online_interval if online_streak >= 2 else base
        elif state == OFFLINE:
            # 本地根本没连上（网卡断开/不在校园网），跑登录流程纯属白等超时
            failures += 1
            online_streak = 0
            # 断网跟「被别的设备顶掉」无关（顶掉必须发生在校园网内），别白白让位
            policy.on_offline()
            # 固定短间隔而非指数退避，理由见 _OFFLINE_INTERVAL_SECONDS 处的注释：
            # 断网轮次几乎不花钱，而恢复慢是用户能直接感觉到的。
            interval = max(base, min(base * 2, _OFFLINE_INTERVAL_SECONDS))
            # 仅状态变化或退避间隔变化时记录（避免每轮刷一行）
            if last_state != OFFLINE or interval != last_interval:
                logging.info("本地网络不可达（连续 %d 次），%d 秒后重试",
                             failures, interval)
                logged = True
        elif state == UNAUTHENTICATED:
            # 连着校园网但没认证：只有这一种情况才值得跑完整登录流程
            online_streak = 0
            interval = base
            # 只在「刚变成未认证」时写这行；连续未认证时由下面的「登录未成功」汇总，
            # 否则未认证的每一轮都会多刷一行（实测 4 小时 124 行）。
            if last_state != UNAUTHENTICATED:
                logging.info("检测到未认证，开始重新登录")
                logged = True

            # —— 先决定这一轮该不该「让位」：账号通常只允许一台设备在线，此刻
            #    强行认证只会把对方踢掉、然后被对方踢回来，陷入互相踢 ——
            hold = False
            recheck = _cfg_int(cfg, "device_recheck_seconds", minimum=30)
            if policy.remaining(now) > 0:
                # 已在礼让期内：定期复查，对方一下线就提前恢复，别干等满 15 分钟
                hold = True
                if cfg.get("device_check_enabled", True) \
                        and (now - last_dev_check) >= recheck:
                    last_dev_check = now
                    verdict, info = check_online_owner(cfg)
                    if verdict in (DEV_NONE, DEV_MINE):
                        policy.clear_yield_now()
                        logging.info("对方设备已下线（%s），提前结束让位并立即登录",
                                     info)
                        logged = True
                        hold = False
            elif cfg.get("device_check_enabled", True):
                # 首选判据：直接看「在线设备的 MAC 是不是本机」（确证）
                verdict, info = check_online_owner(cfg)
                if verdict == DEV_OTHER:
                    minutes = policy.preempt_by_device(now, info)
                    last_dev_check = now
                    logging.warning(
                        "检测到其他设备正在使用本账号（%s），让位 %d 分钟后再试",
                        info, minutes)
                    logged = True
                    notify_yield(minutes)
                    hold = True
                elif verdict == DEV_UNKNOWN:
                    # 查不出来（自助服务不通 / 接口变了）：退回窗口推断。**绝不**
                    # 把"查不出来"当成"没人在线"去抢线。
                    minutes = policy.check_preempted(now)
                    if minutes:
                        logging.warning(
                            "查不到在线设备，按行为推断被其他设备顶掉，让位 %d 分钟后再试",
                            minutes)
                        logged = True
                        notify_yield(minutes)
                        hold = True
                else:
                    # DEV_MINE / DEV_NONE：账号空闲或就是本机，正常登录
                    last_dev_check = now
            else:
                # 关掉了 MAC 判定，退回老的窗口推断
                minutes = policy.check_preempted(now)
                if minutes:
                    logging.warning("检测到其他设备占用本账号，让位 %d 分钟后再试",
                                    minutes)
                    logged = True
                    notify_yield(minutes)
                    hold = True

            if hold:
                # 礼让期间不发起认证。interval 取「复查周期」与「剩余时长」的较小
                # 值，这样对方一走最多等一轮就能恢复，而不是干等到礼让期满。
                left = policy.remaining(now)
                interval = max(base, min(int(left) if left else base, recheck))
                last_hold_kind = "yield"
                write_daemon_state(yield_until=policy.yield_until,
                                   reason=policy.reason)
            else:
                if last_hold_kind == "yield":
                    last_hold_kind = None
                    write_daemon_state(yield_until=0, reason="")
                if not _has_credentials(cfg):
                    # 与 run_once 对齐的闸门：DPAPI 解密失败/配置被清空时，绝不能把空
                    # 密码提交给网关（会被当成密码错误反复重试）。
                    logging.error("配置中没有可用的账号密码，跳过本轮登录（请在界面重新填写并保存）")
                    ok = False
                else:
                    try:
                        ok = run_login_flow(cfg)
                    except Exception:
                        logging.exception("登录流程异常")
                        ok = False
                if ok:
                    failures = 0
                    policy.note_auth_ok(now)
                else:
                    failures += 1
                    # 未认证原则上要快速重试（1.1 的硬要求），但「网关可达却不给跳转」
                    # 和「连不上网关」是**环境结论**，重跑登录流程结果完全一样。实测
                    # 2026-09-23 晚 18:36~22:21 就这样每 30s 空转了 124 轮、刷了 197 行
                    # 日志，每轮还白等 7 秒 socket 超时。这里前 3 次仍用基础间隔（真未认证
                    # 时第 1 次就成功，不受影响），之后按退避放宽，避免长时间空转。
                    # login_rejected（凭据被拒）同样按退避放宽，且连败够多时直接长间隔停手，
                    # 理由见 AUTH_REJECTED_WHY 处的注释。
                    why = get_fail_why()
                    if why == AUTH_REJECTED_WHY and get_auth_reject_streak() >= _AUTH_STOP_AFTER:
                        # 凭据被连续拒绝：继续高频提交只会撞上运营商侧的账号锁定，
                        # 长间隔静默等待（充值/改密后自然会连上，或用户手动重登立即恢复）。
                        interval = _AUTH_SETTLE_SECONDS
                    elif why in _NO_RETRY_WHY and failures > 3:
                        interval = next_interval(base, failures - 3, cap)
                    else:
                        interval = base
                    if interval != last_interval or failures <= 3:
                        logging.info("登录未成功（连续 %d 次），%d 秒后重试", failures, interval)
                        logged = True
        else:
            # state is None：本轮检测异常，按退避重试
            online_streak = 0
            interval = next_interval(base, failures, cap)

        # 心跳：本轮没写状态日志、且距上次写日志超过心跳周期时，补一行
        if not logged and (now - last_hb) >= heartbeat_sec:
            logging.info("保活心跳：状态=%s，连续异常 %d 次",
                         STATE_TEXT.get(state, "检测异常"), failures)
            logged = True
        if logged:
            last_hb = now

        last_state = state
        last_interval = interval
        if not logged:
            # 空闲期把空转时用不到的物理内存还回去：保活进程常驻几小时到几天，
            # 任务管理器里长期挂着几十 MB 没必要。间隔以秒计，成本可忽略。
            trim_working_set()
        time.sleep(interval)


# ============================ 单实例与 daemon 管理 ============================

def read_daemon_pid():
    """读取 app.pid 里的后台保活进程 PID；无文件/内容非法返回 None。"""
    try:
        with open(PID_PATH, "r") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _is_our_daemon(pid):
    """确认 PID 确实是本程序的后台保活进程（--daemon 模式），避免误杀无关进程。

    **返回三态，这个区分是关键**：
      True  = 确认是本程序的 daemon
      False = 确认不是（进程已不存在，或进程在但命令行不符）
      None  = **查不出来**（PowerShell 超时/被拦/没输出）——未知，不等于"已死"

    原来只返回 True/False，把"查不出来"和"确认不是"混为一谈；调用方据此删掉
    app.pid，可 daemon 还活着、互斥量还在它手里，于是留下"互斥量被占、PID 文件却
    没了"的自相矛盾状态——此后每次启动保活都会刷两行红色 ERROR（用户报的
    「显示未知报错」）。

    frozen 进程：Win32_Process 的 ExecutablePath / CommandLine 含 CampusLogin；
    源码进程：CommandLine 含 app.py 且带 --daemon。
    必须带 --daemon 参数：这样界面临时进程（GUI）绝不会被误杀；
    原程序 campus_login.py 带下划线，也不会被 “CampusLogin” 误匹配。"""
    if not pid or pid <= 0:
        return False
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command",
             "$p = Get-CimInstance Win32_Process -Filter 'ProcessId=%d'; "
             "if ($p) { $p.ExecutablePath; $p.CommandLine }" % pid],
            stderr=subprocess.DEVNULL, timeout=8,
            creationflags=CREATE_NO_WINDOW,
            env=clean_child_env(),
        ).decode("utf-8", "ignore")
    except Exception:
        return None      # 查询失败 = 未知，不要当成"不是我们的进程"
    if not out.strip():
        return False     # 查询成功但没有输出 = 进程确实不存在
    low = out.lower()
    is_ours = ("campuslogin" in low) or ("app.py" in low)
    return is_ours and ("--daemon" in low)


def _kill_process(pid):
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
                       creationflags=CREATE_NO_WINDOW,
                       env=clean_child_env())
        return True
    except Exception:
        return False


def acquire_single_instance_lock():
    """用 Windows 命名互斥量保证只有一个 daemon 实例。
    发现已有实例时，先结束旧实例再接管；成功返回句柄，失败返回 None。"""
    try:
        # use_last_error=True 不可省：ctypes 直调 kernel32.GetLastError() 的结果
        # 可能已被 ctypes 内部的 Win32 调用覆盖，会误判「是否已有实例」。
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError):
        return True

    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

    def create():
        h = kernel32.CreateMutexW(None, 0, MUTEX_NAME)
        return h, ctypes.get_last_error() == ERROR_ALREADY_EXISTS

    handle, exists = create()
    if not handle:
        return True  # 取不到句柄的异常环境，不阻塞启动
    if not exists:
        return handle

    kernel32.CloseHandle(handle)
    old_pid = read_daemon_pid()
    if old_pid and old_pid != os.getpid() and _is_our_daemon(old_pid):
        logging.info("检测到旧实例 PID %d，先关闭它再接管...", old_pid)
        _kill_process(old_pid)
        # 等旧进程真正退出、Mutex 释放，最多 5 秒
        for _ in range(50):
            time.sleep(0.1)
            h, still_exists = create()
            if h and not still_exists:
                return h
            if h:
                kernel32.CloseHandle(h)
        logging.error("旧实例未能及时退出，本次启动退出")
        return None

    # PID 文件无效或已不是本程序：兜底重试一次
    h, still_exists = create()
    if h and not still_exists:
        return h
    if h:
        kernel32.CloseHandle(h)
    # 走到这里说明互斥量被占、但没有任何可接管的旧实例。这属于**正常情况**
    # （用户多开、计划任务与手动启动撞车），不是故障——原来这行是 logging.error，
    # 于是日志里冒出红字，看起来像程序坏了。
    logging.info("已有后台保活实例在运行，本次不再重复启动")
    return None


GUI_MUTEX_NAME = "campus_login_app_gui_v1"   # GUI 专用，与 daemon 的锁分开
GUI_WINDOW_TITLE = "校园网一键登录"

_SYNCHRONIZE = 0x00100000


def gui_window_open():
    """当前是否有界面在运行（靠 GUI 单实例互斥量判断，不枚举窗口）。"""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenMutexW.restype = ctypes.c_void_p
        kernel32.OpenMutexW.argtypes = [ctypes.c_uint32, ctypes.c_int,
                                        ctypes.c_wchar_p]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        h = kernel32.OpenMutexW(_SYNCHRONIZE, False, GUI_MUTEX_NAME)
        if h:
            kernel32.CloseHandle(ctypes.c_void_p(h))
            return True
    except Exception:
        pass
    return False


# 恢复在线的提醒节流：网络抖动时状态会反复横跳，不能让通知跟着刷屏。
_NOTIFY_COOLDOWN_SECONDS = 300
_notify_lock = threading.Lock()
_last_notify_ts = 0.0


def notify_reconnected():
    """后台保活重新上线时提醒用户一次（节流；界面开着时跳过）。

    界面开着的时候它自己就在显示状态，再弹系统通知是噪音；真正需要通知的是
    「窗口关着、用户在干别的」这种情况。弹窗动作交给 notify_win 的独立线程，
    这里只做判断——保活循环里任何阻塞都会推迟下一轮检测。
    """
    global _last_notify_ts
    try:
        now = time.time()
        with _notify_lock:
            if now - _last_notify_ts < _NOTIFY_COOLDOWN_SECONDS:
                return
            _last_notify_ts = now
        if gui_window_open():
            return
        import notify_win
        notify_win.notify("校园网已恢复连接", "后台保活已重新认证成功。")
    except Exception:
        # 通知是尽力而为：弹不出来绝不允许影响保活
        logging.debug("发送恢复通知失败", exc_info=True)


def acquire_gui_instance_lock():
    """GUI 单实例锁：已有界面在运行时返回 None，调用方应激活旧窗口后退出。

    原先只有 daemon 有单实例保护，**GUI 完全没有**：双击两次就有两个完整界面在跑，
    各自显示各自的状态、各自启停保活，还会互相抢 daemon 的锁（日志里表现为反复
    "检测到旧实例…接管"），也就是用户说的「多开导致进程重复、影响正常功能」。

    和 daemon 一样：返回的句柄被丢弃但**不会关闭**（ctypes 返回的是普通整数，
    没有析构），互斥量因此一直held到进程退出，正是我们要的语义。
    """
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError):
        return True          # 非 Windows / 取不到 kernel32：不阻塞启动
    try:
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                          ctypes.c_wchar_p]
        handle = kernel32.CreateMutexW(None, 0, GUI_MUTEX_NAME)
        if not handle:
            return True      # 异常环境：不阻塞启动
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            return None
        return handle
    except Exception:
        return True


def focus_existing_window(title=None):
    """把已经打开的界面窗口拉到前台（先还原最小化）。找不到返回 False。"""
    title = title or GUI_WINDOW_TITLE
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        hwnd = user32.FindWindowW(None, title)
        if not hwnd:
            return False
        SW_RESTORE = 9
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def find_daemon_pids():
    """列出所有正在运行的本程序 daemon 进程 PID（**不依赖 app.pid**）。

    返回 **None 表示查询失败**（PowerShell 超时/被拦），和"查到了 0 个"是两回事——
    调用方据此决定是"确实没在跑"还是"查不出来别乱下结论"。

    为什么需要它：app.pid 只是"快路径"，一旦它丢了或者内容失准（PID 被系统复用、
    或某次强杀没来得及清理），只看文件就会得出"没在运行"的结论——而此时互斥量
    还在真正的 daemon 手里，于是"启动"启不动、"停止"找不到进程，界面彻底卡死。
    进程枚举是权威来源，用它兜底并顺手把 app.pid 修正回来。

    注意：PyInstaller onefile 的 daemon 是**父子两个进程**（引导父进程 + 真正跑
    Python 的子进程），命令行一模一样，所以这里通常返回两个 PID；按"同一个逻辑
    实例"处理即可（停止时两个都要结束）。
    """
    try:
        # 必须按 **进程名** 过滤，不能只看 CommandLine：这条查询自己的命令行里就含
        # 字面量 "--daemon"（写在过滤条件里），只匹配命令行的话它会把自己也列进来，
        # 于是永远至少有一个"daemon"，daemon_running() 就永远返回 True —— 一个
        # 会让"没在运行"永远判错的假阳性。
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | "
             "Where-Object { $_.CommandLine -like '*--daemon*' -and "
             "($_.Name -like 'CampusLogin*' -or $_.Name -like 'python*') } | "
             "ForEach-Object { $_.ProcessId }"],
            stderr=subprocess.DEVNULL, timeout=8,
            creationflags=CREATE_NO_WINDOW,
            env=clean_child_env(),
        ).decode("utf-8", "ignore")
    except Exception:
        return None            # 查询失败 = 未知，不是"没有"
    pids = []
    for tok in out.split():
        tok = tok.strip()
        if tok.isdigit():
            p = int(tok)
            if p != os.getpid():
                pids.append(p)
    return pids


def write_pid_file():
    try:
        with open(PID_PATH, "w") as f:
            f.write(str(os.getpid()))
    except OSError as e:
        logging.warning("写入 app.pid 失败: %s", e)


def remove_pid_file(reason=""):
    """删除 app.pid。reason 会写进日志——这个文件被误删过一次，导致状态自相矛盾
    且很难查（谁删的、什么时候删的都没痕迹），所以现在每次清理都留一条记录。"""
    try:
        os.remove(PID_PATH)
        if reason:
            logging.info("清理 app.pid：%s", reason)
            logging.debug("清理调用栈：\n%s",
                          "".join(traceback.format_stack()[-5:]))
    except OSError:
        pass


def daemon_running():
    """后台保活进程是否在运行。

    app.pid 只是**快路径**；判定不了就枚举进程兜底，并把 app.pid 修正回来（自愈）。
    只看 app.pid 有过一次很糟的后果：文件被误删后，daemon 明明活着（互斥量还在它
    手里），界面却报"未运行"，用户点启动又因为锁被占而起不来，彻底卡死。
    """
    pid = read_daemon_pid()
    if pid is not None and pid == os.getpid():
        return True  # daemon 模式自查
    if pid is not None:
        if not _process_alive(pid):
            remove_pid_file("记录的进程 %d 已不存在" % pid)
        else:
            verdict = _is_our_daemon(pid)
            if verdict is True:
                return True
            if verdict is False:
                remove_pid_file("PID %d 存在但不是本程序的后台保活（PID 被复用？）"
                                % pid)
            # verdict is None（查不出来）-> 不删文件，落到下面枚举兜底

    # 兜底：直接枚举进程。找到就认为在运行，并顺手把 app.pid 修正回来
    pids = find_daemon_pids()
    if pids:
        if pid not in pids:
            logging.info("app.pid 未指向真实后台保活（%s），按枚举结果自愈为 PID %d",
                         pid, pids[0])
            try:
                with open(PID_PATH, "w") as f:
                    f.write(str(pids[0]))
            except OSError as e:
                logging.warning("自愈写入 app.pid 失败: %s", e)
        return True
    if pids is None:
        # 枚举也失败了：唯一的信息是"app.pid 里那个进程还活着、且不能排除是我们的"，
        # 那就保守当作在运行——报"没在跑"会诱导用户去启动第二个实例，而锁多半还
        # 被真正的 daemon 占着，启也启不动。
        return bool(pid is not None and _process_alive(pid))
    return False           # 枚举成功且一个都没有 -> 确实没在运行


def stop_daemon():
    """停止后台保活进程。返回 (是否成功, 提示消息)。

    **把所有枚举到的 daemon 进程都结束掉**，而不是只杀 app.pid 里那一个：
    onefile 的 daemon 是父子两个进程，只杀一个会留下孤儿；而且 app.pid 失准时
    只看它就根本停不掉。
    """
    targets = set()
    pid = read_daemon_pid()
    if pid is not None and pid != os.getpid():
        if _process_alive(pid):
            verdict = _is_our_daemon(pid)
            if verdict is not False:      # True 或 None（查不出来）都算候选
                targets.add(pid)
            else:
                remove_pid_file("PID %d 不是本程序的后台保活" % pid)
        else:
            remove_pid_file("记录的进程 %d 已不存在" % pid)

    for p in (find_daemon_pids() or []):
        targets.add(p)

    if not targets:
        return False, "未发现正在运行的后台保活"

    # 先给每个目标留一个句柄：句柄盯的是"进程对象"，PID 事后被系统回收给别的
    # 进程也不影响它——这是停止成功与否唯一可靠的判据（见 _open_wait_handle）。
    order = sorted(targets)
    handles = {p: _open_wait_handle(p) for p in order}
    try:
        for p in order:
            _kill_process(p)
        # 等进程真正消失再下结论。PyInstaller onefile 的**引导父进程**要等子进程
        # 退出、把 _MEI 解包目录（30+ MB）清理完才结束，强杀之后可能还要好几秒。
        still = list(order)
        deadline = time.time() + 8.0
        retry_at = time.time() + 2.0
        while time.time() < deadline:
            still = [p for p in order
                     if _target_still_running(p, handles[p])]
            if not still:
                break
            if time.time() >= retry_at:
                # taskkill 偶尔没打中（超时/被占用），补一次再等，别直接判失败
                retry_at = time.time() + 2.0
                for p in still:
                    _kill_process(p)
            time.sleep(0.3)
    finally:
        _close_process_handles(handles.values())
    if still:
        return False, ("停止失败：进程 %s 仍在运行；"
                       "可在任务管理器中手动结束 CampusLogin.exe"
                       % ", ".join(str(p) for p in still))

    remove_pid_file("已停止后台保活（%d 个进程）" % len(targets))
    return True, "已停止后台保活（%d 个进程）" % len(targets)
