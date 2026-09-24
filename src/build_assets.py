#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
界面改版资产生成脚本（build_assets.py）
====================================================================
把设计包里的 8 个矢量图标，按颜色矩阵生成「换色变体」SVG，供 Tk 9.0 原生加载。

背景：Tk 9.0 的 PhotoImage 原生支持 SVG（矢量、任意缩放无损），但 SVG 内的
fill/stroke 是写死的 #014DB2。本脚本按当前运行状态需要的颜色，为每个图标
生成若干变体文件（字符串替换颜色），运行时按状态选对应文件加载即可。

输出：assets/icons/<icon>-<colorname>.svg
输入：assets/design/icon-*.svg

可重复运行，幂等。仅构建期使用，不进入 exe 运行时依赖。
"""

import os

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(HERE)  # CampusLoginApp/
SRC_DIR = os.path.join(APP_DIR, "assets", "design")
OUT_DIR = os.path.join(APP_DIR, "assets", "icons")

ICONS = (
    "icon-wifi", "icon-user", "icon-lock", "icon-eye",
    "icon-power", "icon-refresh", "icon-shield", "icon-clock",
)

# 颜色矩阵：名字 -> hex（与 theme.py / 设计规范一致）
COLORS = {
    "blue":   "#014DB2",
    "deep":   "#001645",
    "white":  "#FFFFFF",
    "gray":   "#9CA3AF",
    "dark":   "#374151",
    "green":  "#10B981",
    "red":    "#EF4444",
    "amber":  "#F59E0B",
}


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    count = 0
    for icon in ICONS:
        src = os.path.join(SRC_DIR, icon + ".svg")
        if not os.path.exists(src):
            print("[跳过] 找不到 %s" % src)
            continue
        with open(src, "rb") as f:
            raw = f.read()
        for name, color in COLORS.items():
            out = os.path.join(OUT_DIR, "%s-%s.svg" % (icon, name))
            with open(out, "wb") as f:
                f.write(raw.replace(b"#014DB2", color.encode("utf-8")))
            count += 1
    print("已生成 %d 个图标变体 -> %s" % (count, OUT_DIR))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
