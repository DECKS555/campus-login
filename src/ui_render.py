#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抗锯齿渲染原语（ui_render.py）
====================================================================
存在的唯一理由：**tkinter 的 Canvas 不做抗锯齿**。

`create_polygon` / `create_line` 画出来的圆角是阶梯状的，逐扫描线拼出来的
渐变在四角会留下缺口。无论怎么加密采样点都修不好——底层渲染器就不支持。
本模块改用 Pillow：在 3 倍超采样下绘制，再用 LANCZOS 缩回设备像素，得到
真正平滑的边缘；高斯模糊阴影也能直接做出来（原来的做法是用十几层同心圆角
矩形去逼近，既慢又假）。

结果按参数缓存为 `tk.PhotoImage`，同一尺寸的控件只在首次绘制时付一次代价。

**没有 Pillow 时本模块所有函数返回 None**，调用方（app.py 的 round_rect /
_draw_round_gradient）会退回原来的 Canvas 画法。因此引入 Pillow 是可选的
增强，而不是新的硬依赖。

注意：`PIL.ImageTk` 会连带 import tkinter，而 app.py 在 --daemon/--once/--probe
下**刻意不加载 tkinter**（见 app.py 顶部注释）。所以 ImageTk 只做惰性导入，
本模块顶层不碰 tkinter。
"""

import math
import sys
from collections import OrderedDict

try:
    from PIL import Image, ImageDraw, ImageFilter
    HAS_PIL = True
    _IMPORT_ERROR = None
except Exception as _e:          # pragma: no cover - 取决于环境
    Image = ImageDraw = ImageFilter = None
    HAS_PIL = False
    _IMPORT_ERROR = _e

_ImageTk = None
_ImageTk_tried = False


def _imagetk():
    """惰性取 PIL.ImageTk（首次调用时才 import，避免捎带加载 tkinter）。"""
    global _ImageTk, _ImageTk_tried
    if not _ImageTk_tried:
        _ImageTk_tried = True
        try:
            from PIL import ImageTk
            _ImageTk = ImageTk
        except Exception:
            _ImageTk = None
    return _ImageTk


SS = 2                  # 超采样倍率。
# 原来是 3x：每输出像素要画 9 个子像素，一次完整重绘实测 260ms；降到 2x 后是 4 个
# 子像素，圆角的抗锯齿效果肉眼看不出差别，但整套界面的重绘成本直接砍掉一半多。
_MAX_CACHE = 400        # 缓存条目上限，防极端情况下无限增长

# OrderedDict 按插入顺序记录，用于 LRU 淘汰。
# 原来是「满了就 _cache.clear() 全清」：一旦触顶，所有已缓存的卡片位图**同时**
# 失效，下一帧要一次性重新光栅化整套界面（实测一次完整重绘上百毫秒）——用户看到
# 的就是毫无征兆地卡一下。改成淘汰最久未用的那条，内存上限不变，但不会再雪崩。
_cache = OrderedDict()

# ---- 交互式缩放（拖窗口）期间的"草图模式" ----
# 拖拽时控件尺寸每变一个像素，位图缓存键就变一次 → 缓存全部失效 → 每帧都要重新
# 光栅化所有卡片（实测 260ms/次），界面就是卡成幻灯片。所以拖拽期间只画一个
# 廉价的多边形占位，鼠标停下来之后再一次性输出高质量位图。
_sketch = False


def set_sketch(on):
    """进入/退出草图模式（拖拽窗口期间为 True）。"""
    global _sketch
    _sketch = bool(on)


def is_sketch():
    return _sketch


def cache_size():
    return len(_cache)


def _rgba(color, alpha=255):
    """'#RRGGBB' -> (r, g, b, a)。"""
    c = (color or "#000000").lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    if len(c) != 6:
        return (0, 0, 0, alpha)
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), alpha)


def shadow_pad(shadow):
    """算出阴影需要向外占用的边距（设备像素）。

    调用方要按这个值把卡片向内缩，否则阴影会被 Canvas 边缘裁掉。取 1.0×blur
    而不是 1.5×：高斯尾巴最外圈在 7% alpha 下已经看不见，留太多只是白白吃掉
    布局空间（卡片四周各多几像素，叠起来就是几十像素的高度）。
    """
    if not shadow:
        return 0
    dx, dy, blur, _color, _alpha = shadow
    return int(blur * 1.0 + max(abs(dx), abs(dy)) + 2)


def _gradient_image(w, h, c_top, c_bottom, horizontal=False):
    """生成 w×h 的 RGB 渐变图。

    先在 1 像素宽/高的条上插值（只有几十~一百多次循环），再交给 Pillow 的
    C 实现放大——这样既没有 Python 逐像素的开销，放大又是平滑的。
    """
    n = max(2, w if horizontal else h)
    strip = Image.new("RGB", (n, 1) if horizontal else (1, n))
    px = strip.load()
    r1 = _rgba(c_top)
    r2 = _rgba(c_bottom)
    for i in range(n):
        t = i / float(n - 1)
        col = (int(r1[0] + (r2[0] - r1[0]) * t),
               int(r1[1] + (r2[1] - r1[1]) * t),
               int(r1[2] + (r2[2] - r1[2]) * t))
        if horizontal:
            px[i, 0] = col
        else:
            px[0, i] = col
    return strip.resize((w, h), Image.BILINEAR)


def render_surface(w, h, radius=0, fill=None, gradient=None, border=None,
                   border_w=1, shadow=None, horizontal=False):
    """渲染一块圆角面 -> (PIL.Image, pad)。失败返回 None。

    w/h 是**卡片本体**的尺寸（设备像素），不含阴影边距；返回的图尺寸是
    (w + 2*pad, h + 2*pad)，本体位于 (pad, pad)。
    """
    if not HAS_PIL:
        return None
    w = int(round(w))
    h = int(round(h))
    if w < 1 or h < 1:
        return None

    pad = shadow_pad(shadow)
    W = (w + 2 * pad) * SS
    H = (h + 2 * pad) * SS
    if W <= 0 or H <= 0 or W * H > 40_000_000:   # 安全阀：约 4000x4000 以上不画
        return None

    ox = oy = pad * SS
    bw, bh = w * SS, h * SS
    box = (ox, oy, ox + bw - 1, oy + bh - 1)
    r = max(0.0, min(float(radius) * SS, bw / 2.0, bh / 2.0))

    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    # —— 阴影：真实高斯模糊，而不是同心矩形逼近 ——
    if shadow:
        dx, dy, blur, scolor, salpha = shadow
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(layer).rounded_rectangle(
            (ox + dx * SS, oy + dy * SS,
             ox + dx * SS + bw - 1, oy + dy * SS + bh - 1),
            radius=r, fill=_rgba(scolor, salpha))
        layer = layer.filter(ImageFilter.GaussianBlur(blur * SS / 2.0))
        canvas = Image.alpha_composite(canvas, layer)

    # —— 本体：渐变或纯色 ——
    if gradient:
        # 渐变按**本体**尺寸生成（不是整张画布）：paste 的遮罩尺寸必须与源图一致
        grad = _gradient_image(bw, bh, gradient[0], gradient[1], horizontal)
        mask = Image.new("L", (bw, bh), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, bw - 1, bh - 1), radius=r, fill=255)
        canvas.paste(grad, (ox, oy), mask)
    elif fill:
        ImageDraw.Draw(canvas).rounded_rectangle(
            box, radius=r, fill=_rgba(fill))

    # —— 描边：画在本体之上，避免被渐变遮掉 ——
    if border:
        ImageDraw.Draw(canvas).rounded_rectangle(
            box, radius=r, outline=_rgba(border),
            width=max(1, int(round(border_w * SS))))

    return canvas.resize((w + 2 * pad, h + 2 * pad), Image.LANCZOS), pad


def _to_photo(img):
    """PIL.Image -> tk.PhotoImage。"""
    tk_mod = _imagetk()
    if tk_mod is None:
        return None
    try:
        return tk_mod.PhotoImage(img)
    except Exception:
        return None


def surface_photo(w, h, radius=0, fill=None, gradient=None, border=None,
                  border_w=1, shadow=None, horizontal=False):
    """带缓存的 render_surface + 转 PhotoImage -> (photo, pad)，失败返回 (None, 0)。

    草图模式下**直接返回 (None, 0)**，让调用方走廉价的多边形路径——这是拖拽窗口
    不卡的关键。

    调用方必须持有返回的 photo 引用（缓存里也留了一份，但控件自己持有才稳妥，
    否则缓存被清时正在显示的图会被 GC 掉，表现为控件突然变空白）。
    """
    if not HAS_PIL or _sketch:
        return None, 0
    key = (int(round(w)), int(round(h)), float(radius), fill, gradient,
           border, border_w, shadow, bool(horizontal))
    hit = _cache.get(key)
    if hit is not None:
        _cache.move_to_end(key)      # 命中即刷新为最近使用
        return hit
    out = render_surface(w, h, radius=radius, fill=fill, gradient=gradient,
                         border=border, border_w=border_w, shadow=shadow,
                         horizontal=horizontal)
    if out is None:
        return None, 0
    img, pad = out
    photo = _to_photo(img)
    if photo is None:
        return None, 0
    if len(_cache) >= _MAX_CACHE:
        # 淘汰最久未用的一个，而不是清空整个缓存
        try:
            _cache.pop(next(iter(_cache)))
        except (StopIteration, RuntimeError):
            _cache.clear()
    _cache[key] = (photo, pad)
    return photo, pad


def probe():
    """给 --gui-smoke / --probe 用的自述信息。"""
    return {
        "has_pil": HAS_PIL,
        "error": None if HAS_PIL else repr(_IMPORT_ERROR),
        "ss": SS,
        "cached": len(_cache),
        "frozen": bool(getattr(sys, "frozen", False)),
    }


# ---------- 位图缩放（图标用） ----------

def photo_to_image(photo):
    """tk.PhotoImage -> PIL.Image(RGBA)。失败返回 None。"""
    tk_mod = _imagetk()
    if tk_mod is None:
        return None
    try:
        return tk_mod.getimage(photo).convert("RGBA")
    except Exception:
        return None


def rescale(photo, target_w, target_h=None):
    """把 Tk 位图缩放到目标尺寸，LANCZOS 抗锯齿。失败返回 None。

    放大时先整数 `zoom()` 再降采样回目标尺寸——等价于超采样，比直接放大清楚。
    图标场景专用：Tk 9 读 SVG 时 -width/-height 是**裁剪**不是缩放，必须走这里。
    """
    if not HAS_PIL or target_w is None or target_w <= 0:
        return None
    if target_h is None:
        target_h = target_w
    try:
        sw, sh = photo.width(), photo.height()
        if sw <= 0 or sh <= 0:
            return None
        work = photo
        if target_w > sw:
            k = int(math.ceil(target_w / float(sw)))
            if k > 1:
                work = photo.zoom(k, k)
        src = photo_to_image(work)
        if src is None:
            return None
        return _to_photo(src.resize((int(target_w), int(target_h)),
                                   Image.LANCZOS))
    except Exception:
        return None
