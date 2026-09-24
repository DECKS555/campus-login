#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重新打包 CampusLogin.exe（直接调 PyInstaller，不走 build.bat）。

为什么不走 build.bat：在 Git Bash 里跑它会因为 TEMP 被设成 Unix 路径而报出一串
看起来毫不相关的错（详见项目记忆 campusloginapp-build-gotcha）。

关键点：
  * 用 subprocess 传 **args 列表**，不拼 shell 字符串——参数里有中文路径会被破坏；
  * --workpath/--specpath 指向 %TEMP%，不在项目里留 build/dist/*.spec；
  * --hidden-import PIL.ImageTk：抗锯齿渲染靠它，而它是**惰性导入**（放在函数里，
    避免 daemon 模式捎带加载 tkinter），显式声明免得 PyInstaller 漏收；
  * 打完后顺手组一份「校园网一键登录-对外发布」（只有 exe + 使用说明.md）。工作目录
    里有 config.json 和 login.log，直接压工作目录发出去就等于发账号密码。
"""
import os
import shutil
import subprocess
import sys

APP_DIR = r"D:\first-cc\校园网一键登录-发布包"
SRC = os.path.join(APP_DIR, "src")
ASSETS = os.path.join(APP_DIR, "assets")
TEMP = os.environ["TEMP"]
WORK = os.path.join(TEMP, "campuslogin_build_work")
PY = r"D:\python\python.exe" if os.path.exists(r"D:\python\python.exe") else sys.executable

# 对外发布目录放在工作目录**之外**，否则「把工作目录压包发出去」这个坏习惯照样会
# 把 config.json 一起带走。同名目录每次重建。
RELEASE_DIR = os.path.join(os.path.dirname(APP_DIR), "校园网一键登录-对外发布")
# 按 _内部工具与备份/说明.txt 的要求，对外只发这两份。
RELEASE_FILES = ("CampusLogin.exe", "使用说明.md")


def make_release_dir():
    """组一个只剩「exe + 使用说明.md」的干净目录，供压缩后直接转发。

    工作目录里同时躺着 config.json（真实账号 + DPAPI 密文）、login.log、src/ 与
    _内部工具与备份/——全都不能对外。手动挑文件这种事迟早会忘一次，而忘一次就是把
    账号密码发出去了，所以让打包流程顺手产出这份。
    """
    if os.path.isdir(RELEASE_DIR):
        shutil.rmtree(RELEASE_DIR, ignore_errors=True)
    os.makedirs(RELEASE_DIR, exist_ok=True)
    for name in RELEASE_FILES:
        src = os.path.join(APP_DIR, name)
        if not os.path.exists(src):
            print("[警告] 缺少 %s，干净发布目录不完整" % name)
            continue
        shutil.copy2(src, os.path.join(RELEASE_DIR, name))
    print("干净发布目录：%s" % RELEASE_DIR)
    for name in sorted(os.listdir(RELEASE_DIR)):
        p = os.path.join(RELEASE_DIR, name)
        print("  %s (%.1f MB)" % (name, os.path.getsize(p) / 1048576.0))
    print("[提醒] 对外只发这个目录。当前工作目录含 config.json / login.log，"
          "不要整体转发或压包。")
    return 0


def main():
    ico = os.path.join(ASSETS, "app.ico")
    if not os.path.exists(ico):
        print("[错误] 缺少 %s" % ico)
        return 1
    icons = os.path.join(ASSETS, "icons")
    if not os.path.isdir(icons):
        print("[错误] 缺少 %s（先用 build_assets.py 生成）" % icons)
        return 1

    # 旧的 exe 挪到一边：新包万一有问题，用户手上还有能跑的那个
    old = os.path.join(APP_DIR, "CampusLogin.exe")
    if os.path.exists(old):
        # 上一轮打包留下来的 .old 要先删掉，否则 shutil.move 会撞 FileExistsError
        if os.path.exists(old + ".old"):
            try:
                os.remove(old + ".old")
            except Exception as e:
                print("[警告] 删除旧备份失败：%s" % e)
        shutil.move(old, old + ".old")
        print("旧 exe 已改名为 CampusLogin.exe.old")

    if os.path.isdir(WORK):
        shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK, exist_ok=True)

    args = [
        PY, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--windowed",
        "--name", "CampusLogin",
        "--icon", ico,
        "--add-data", os.path.join(ASSETS, "app.png") + ";assets",
        "--add-data", os.path.join(ASSETS, "app.ico") + ";assets",
        "--add-data", icons + ";icons",
        "--hidden-import", "PIL.ImageTk",
        # notify_win 是在 login_core 的**函数里**惰性 import 的（保住 daemon 模式
        # 不加载多余模块），显式声明免得 PyInstaller 的静态分析漏收
        "--hidden-import", "notify_win",
        "--distpath", APP_DIR,
        "--workpath", WORK,
        "--specpath", WORK,
        os.path.join(SRC, "app.py"),
    ]
    print("开始打包...（约 1~3 分钟）")
    r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    tail = (r.stdout or "").strip().splitlines()[-12:]
    for ln in tail:
        print("  " + ln)
    if r.returncode != 0:
        print("[错误] 打包失败")
        print((r.stderr or "")[-2000:])
        return r.returncode

    exe = os.path.join(APP_DIR, "CampusLogin.exe")
    print("打包完成：%s (%.1f MB)" % (exe, os.path.getsize(exe) / 1048576.0))
    shutil.rmtree(WORK, ignore_errors=True)
    # PyInstaller 可能顺手在项目里留下 build/dist
    for junk in ("build", "dist"):
        p = os.path.join(APP_DIR, junk)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
    make_release_dir()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
