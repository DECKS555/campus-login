#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Windows 托盘气泡通知（纯 ctypes，不引第三方依赖）。

为什么不用 PowerShell 弹 Win10/11 的 Toast：Toast 需要应用注册 AppUserModelID，
而这是一个 PyInstaller onefile 打包的绿色 exe，没有包标识，CreateToastNotifier
往往**静默不显示**——看起来"实现了"，实际用户什么都收不到。Shell_NotifyIconW 的
NIF_INFO 气泡不要求任何注册，是这类程序唯一可靠的选择。

为什么需要一个窗口：Shell_NotifyIconW 必须挂在某个 HWND 上（没有窗口就没有托盘
图标，也就没有气泡的落点）。所以这里在**独立线程**里建一个消息专用窗口
（HWND_MESSAGE，不可见、不进任务栏），挂一个托盘图标，然后跑自己的消息循环。

线程与失败隔离：全部跑在 daemon 线程里，keepalive 主循环只做一次"入队"（deque
append，微秒级）。任何一步失败都静默吞掉——**通知弹不出来绝不允许影响保活**。
"""
import collections
import ctypes
import logging
import os
import sys
import threading
import time

# ---------- Shell_NotifyIcon 常量 ----------
NIM_ADD, NIM_MODIFY, NIM_DELETE, NIM_SETVERSION = 0, 1, 2, 4
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_STATE, NIF_INFO, NIF_SHOWTIP = 0x1, 0x2, 0x4, 0x8, 0x10, 0x80
NIIF_INFO = 0x1
NOTIFYICON_VERSION_4 = 4

WM_APP = 0x8000
WM_TRAY = WM_APP + 1
WM_QUIT = 0x0012
PM_REMOVE = 0x1
HWND_MESSAGE = -3

IDI_APPLICATION = 32512
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x10
LR_DEFAULTSIZE = 0x40

_TIP = "校园网保活：正在运行"

# 气泡显示由系统设置决定时长（NOTIFYICON_VERSION_4 起 uTimeout 不再生效）
_QUEUE = collections.deque(maxlen=8)
_LOCK = threading.Lock()
# ready：窗口+图标都挂上了；disabled：起不来（非 Windows / 建窗口失败），不再重试；
# stopped：被显式 stop()，不自动重启。
_STATE = {"thread": None, "hwnd": None, "stop": False,
          "ready": False, "disabled": False, "stopped": False}

_user32 = None
_shell32 = None


def _libs():
    """惰性加载 DLL，非 Windows / 加载失败时返回 (None, None)。"""
    global _user32, _shell32
    if _user32 is None:
        try:
            _user32 = ctypes.WinDLL("user32", use_last_error=True)
            _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        except Exception:
            _user32 = _shell32 = False
    return (_user32 or None), (_shell32 or None)


# ---------- 结构体 ----------

class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    """字段顺序/宽度必须与 shellapi.h 完全一致，错一个 DWORD 就整条不生效。"""
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("hWnd", ctypes.c_void_p),
        ("uID", ctypes.c_uint32),
        ("uFlags", ctypes.c_uint32),
        ("uCallbackMessage", ctypes.c_uint32),
        ("hIcon", ctypes.c_void_p),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", ctypes.c_uint32),
        ("dwStateMask", ctypes.c_uint32),
        ("szInfo", ctypes.c_wchar * 256),
        ("uVersion", ctypes.c_uint32),      # 与 uTimeout 是 union
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", ctypes.c_uint32),
        ("guidItem", _GUID),
        ("hBalloonIcon", ctypes.c_void_p),
    ]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("style", ctypes.c_uint32),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", ctypes.c_void_p),
        ("hIcon", ctypes.c_void_p),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", ctypes.c_wchar_p),
        ("lpszClassName", ctypes.c_wchar_p),
        ("hIconSm", ctypes.c_void_p),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint32),
                ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_ssize_t),
                ("time", ctypes.c_uint32), ("pt", POINT)]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint,
                            ctypes.c_size_t, ctypes.c_ssize_t)

_CLASS_NAME = "CampusLoginNotifyWnd"
_wndproc_ref = None      # 必须长期持有：否则 Windows 回调到已释放的函数指针


def _icon_path():
    """找打包进来（或源码目录里）的 app.ico；找不到返回 None（回落到系统默认图标）。"""
    cands = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cands.append(os.path.join(meipass, "assets", "app.ico"))
    here = os.path.dirname(os.path.abspath(__file__))
    cands.append(os.path.join(here, os.pardir, "assets", "app.ico"))
    cands.append(os.path.join(os.path.dirname(sys.executable), "assets", "app.ico"))
    for p in cands:
        p = os.path.normpath(p)
        if os.path.exists(p):
            return p
    return None


_ICON_CACHE = {"h": None}      # 图标句柄只加载一次，见 _load_icon


def _load_icon(user32):
    """优先用程序自己的图标；取不到就退回系统默认图标。

    句柄**只建一次并缓存**：原来每次弹气泡都 LoadImageW 一个新图标且从不
    DestroyIcon，这是常驻进程里的 GDI 对象泄漏（每进程 GDI 句柄上限约 1 万个）。
    系统默认图标（LoadIconW）是共享资源，本来就不该销毁，缓存它对行为无影响。
    """
    cached = _ICON_CACHE["h"]
    if cached:
        return cached
    path = _icon_path()
    h = None
    if path:
        user32.LoadImageW.restype = ctypes.c_void_p
        h = user32.LoadImageW(None, ctypes.c_wchar_p(path), IMAGE_ICON, 0, 0,
                              LR_LOADFROMFILE | LR_DEFAULTSIZE)
    if not h:
        user32.LoadIconW.restype = ctypes.c_void_p
        user32.LoadIconW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        h = user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))
    _ICON_CACHE["h"] = h or None
    return _ICON_CACHE["h"]


def _make_nid(hwnd):
    nid = NOTIFYICONDATAW()
    nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
    nid.hWnd = hwnd
    nid.uID = 1
    return nid


def _wnd_proc(hwnd, msg, wparam, lparam):
    """消息专用窗口不需要处理任何消息，交给 DefWindowProc 即可。"""
    user32, _ = _libs()
    if user32 and msg == WM_TRAY:
        pass          # 托盘图标被点击：暂时不做交互
    return user32.DefWindowProcW(ctypes.c_void_p(hwnd), msg, wparam, lparam)


def _create_window(user32):
    """建一个消息专用窗口（不可见、不占任务栏），返回 HWND；失败返回 None。"""
    global _wndproc_ref
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetModuleHandleW.restype = ctypes.c_void_p
    hinst = kernel32.GetModuleHandleW(None)

    _wndproc_ref = WNDPROC(_wnd_proc)
    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    user32.DefWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                      ctypes.c_size_t, ctypes.c_ssize_t]

    wc = WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
    wc.lpfnWndProc = ctypes.cast(_wndproc_ref, ctypes.c_void_p)
    wc.hInstance = hinst
    wc.lpszClassName = _CLASS_NAME
    if not user32.RegisterClassExW(ctypes.byref(wc)):
        # 已经注册过（同一进程重复 start）也能继续用
        if ctypes.get_last_error() != 1410:      # ERROR_CLASS_ALREADY_EXISTS
            return None

    user32.CreateWindowExW.restype = ctypes.c_void_p
    user32.CreateWindowExW.argtypes = [
        ctypes.c_uint32, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    return user32.CreateWindowExW(0, _CLASS_NAME, "CampusLoginNotify", 0,
                                  0, 0, 0, 0,
                                  ctypes.c_void_p(HWND_MESSAGE), None, hinst, None)


def _add_icon(user32, shell32, hwnd):
    nid = _make_nid(hwnd)
    nid.uFlags = NIF_ICON | NIF_TIP | NIF_MESSAGE | NIF_SHOWTIP
    nid.uCallbackMessage = WM_TRAY
    nid.hIcon = _load_icon(user32)
    nid.szTip = _TIP
    if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
        return False
    # 不 SETVERSION 到 4 的话，NIF_INFO 气泡在新系统上通常不显示
    nid.uVersion = NOTIFYICON_VERSION_4
    shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(nid))
    return True


def _show_balloon(shell32, hwnd, title, message):
    nid = _make_nid(hwnd)
    nid.uFlags = NIF_INFO | NIF_ICON | NIF_TIP
    nid.hIcon = _load_icon(_libs()[0])
    nid.szTip = _TIP
    nid.szInfoTitle = str(title)[:63]
    nid.szInfo = str(message)[:255]
    nid.dwInfoFlags = NIIF_INFO
    shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))


def _drain(shell32, hwnd):
    """把排队的通知都弹出去。必须在**拥有窗口的这个线程**上调用。"""
    while True:
        with _LOCK:
            if not _QUEUE:
                return
            title, message = _QUEUE.popleft()
        try:
            _show_balloon(shell32, hwnd, title, message)
        except Exception:
            logging.debug("显示气泡通知失败", exc_info=True)


def _run():
    user32, shell32 = _libs()
    if not user32 or not shell32:
        _STATE["disabled"] = True
        return
    hwnd = None
    try:
        hwnd = _create_window(user32)
        if not hwnd:
            logging.debug("创建通知窗口失败，跳过系统通知")
            return
        _STATE["hwnd"] = hwnd
        if not _add_icon(user32, shell32, hwnd):
            logging.debug("添加托盘图标失败，跳过系统通知")
            return
        _STATE["ready"] = True
        user32.PeekMessageW.argtypes = [ctypes.POINTER(MSG), ctypes.c_void_p,
                                        ctypes.c_uint32, ctypes.c_uint32,
                                        ctypes.c_uint32]
        user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
        msg = MSG()
        while not _STATE["stop"]:
            # 先排空队列再阻塞：气泡只能在拥有窗口的线程上发
            _drain(shell32, hwnd)
            if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                if msg.message == WM_QUIT:
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            else:
                # 250ms 一轮：只为让"入队的通知"及时弹出来，代价可忽略。
                # 不用 SetTimer/WM_TIMER 是为了不给窗口回调增加状态。
                time.sleep(0.25)
    except Exception:
        logging.debug("通知线程异常退出", exc_info=True)
    finally:
        if hwnd and shell32:
            try:
                shell32.Shell_NotifyIconW(NIM_DELETE,
                                          ctypes.byref(_make_nid(hwnd)))
            except Exception:
                pass
        _STATE["hwnd"] = None
        if not _STATE["ready"]:
            # 起不来就别每来一条通知再试一次（线程会反复生灭）。通知是尽力而为的功能。
            _STATE["disabled"] = True


def _ensure_started():
    with _LOCK:
        if _STATE["stopped"] or _STATE["disabled"]:
            return
        t = _STATE["thread"]
        if t is not None and t.is_alive():
            return
        # 线程没起来或已经死了（例如 stop 之外的意外退出）都重新拉起
        _STATE["stop"] = False
        _STATE["ready"] = False
        t = threading.Thread(target=_run, name="campus-notify", daemon=True)
        _STATE["thread"] = t
    t.start()


def notify(title, message):
    """弹一条系统通知（非阻塞、幂等、失败静默）。

    第一次调用会自动起通知线程；线程尚未就绪时通知先入队，就绪后补弹。
    """
    try:
        with _LOCK:
            _QUEUE.append((title, message))
        _ensure_started()
    except Exception:
        logging.debug("入队系统通知失败", exc_info=True)


def stop():
    """结束通知线程（移除托盘图标）。进程退出时其实不需要调，留着给自检用。"""
    with _LOCK:
        if _STATE["thread"] is None:
            return
        _STATE["stop"] = True
        _STATE["stopped"] = True
        hwnd = _STATE["hwnd"]
    user32, _ = _libs()
    if hwnd and user32:
        try:
            user32.PostMessageW(ctypes.c_void_p(hwnd), WM_QUIT, 0, 0)
        except Exception:
            pass




def available():
    """当前环境是否支持托盘通知（自检用）。"""
    user32, shell32 = _libs()
    return bool(user32 and shell32)
