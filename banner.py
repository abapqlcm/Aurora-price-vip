"""
بنر حرفه‌ای قیمت — پس‌زمینه‌ی تار پرچم/لوگو + کارت شیشه‌ای + نمودار Area
تم: شیشه‌ای (glassmorphism) مشکی‌طلایی
"""
import io
import logging
import math
import os
import random
import subprocess
import tempfile
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests
import imageio_ffmpeg
import arabic_reshaper
from bidi.algorithm import get_display
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageChops, features

import catalog
import datafeeds

log = logging.getLogger("render")

FONT_DIR = "fonts"
GOLD = (212, 175, 55)
GOLD_BRIGHT = (255, 215, 0)
WHITE = (245, 245, 250)
GRAY = (185, 185, 195)
GREEN = (46, 204, 113)
RED = (235, 87, 87)
CARD_BG = (255, 255, 255, 235)

# ۱. رنگ‌های متغیر بر اساس نوع ارز (Dark Theme + Color Coding)
COLORS_BY_TYPE = {
    "fiat": {"bg": (15, 25, 40), "accent": (70, 150, 255), "text": (100, 180, 255)},  # آبی = ارز
    "gold": {"bg": (40, 35, 15), "accent": (255, 215, 0), "text": (255, 200, 0)},   # طلایی = طلا
    "crypto": {"bg": (25, 15, 35), "accent": (180, 100, 255), "text": (200, 150, 255)},  # بنفش = کریپتو
    "stable": {"bg": (20, 35, 25), "accent": (100, 255, 150), "text": (150, 255, 180)},  # سبز = تتر/stable
}

W, H = 900, 950


def _fa(text: str) -> str:
    """متن فارسی خام."""
    return str(text)


_RAQM = None  # کش وضعیت libraqm

def _rtl(s: str) -> str:
    """متن فارسی برای PIL — idempotent و قطعی (همه‌جا یک جواب):
    - اگر raqm فعال است: متن خام بده (raqm خودش RTL می‌کند) — فقط یک بار
    - اگر raqm نیست: reshape + bidi — فقط یک بار
    محافظ anti-double-process: اگر متن قبلاً reshape شده (حروف presentation forms
    در بازهٔ FB50–FEFF)، دیگر دست نمی‌خورد — جلوی برعکس‌شدن متن گرفته می‌شه."""
    global _RAQM
    if _RAQM is None:
        try:
            _RAQM = features.check("raqm")
        except Exception:
            _RAQM = False
    s = str(s)
    if _RAQM:
        return s
    # anti-double-process: presentation forms یعنی قبلاً reshape شده
    if any("\uFB50" <= ch <= "\uFEFF" for ch in s):
        return s
    try:
        return get_display(arabic_reshaper.reshape(s))
    except Exception:
        return s


_FONTS: dict = {}

def _font(size: int, weight: str = "m") -> ImageFont.FreeTypeFont:
    """فونت با کش — دیگه هر بار truetype باز نمی‌شه (سرعت)."""
    key = (size, weight)
    if key not in _FONTS:
        path = {"b": f"{FONT_DIR}/Vazir-Bold.ttf",
                "m": f"{FONT_DIR}/Vazir-Medium.ttf",
                "r": f"{FONT_DIR}/Vazir-Regular.ttf"}[weight]
        _FONTS[key] = ImageFont.truetype(path, size)
    return _FONTS[key]


_BG_FAIL_CACHE: dict = {}  # url → timestamp آخرین خطای دانلود (کش منفی ۳۰۰ ثانیه‌ای)
_BG_OK_CACHE: dict = {}    # url → تصویر موفق (بخوانی دوم در همون رندر بدون درخواست/دیسک)
_BG_FAIL_TTL = 300


def _fetch_bg_image(code: str) -> Optional[Image.Image]:
    """پرچم یا لوگوی رمزارز — با کش در assets/ + کش منفی خطاها (TTL ۳۰۰s)."""
    url, kind, key = catalog.asset_urls(code)
    if not url:
        return None
    now = time.time()
    fail_ts = _BG_FAIL_CACHE.get(url)
    if fail_ts is not None:
        if now - fail_ts < _BG_FAIL_TTL:
            return None  # URL خراب — تا ۵ دقیقه دوباره تلاش نشه
        _BG_FAIL_CACHE.pop(url, None)
    hit = _BG_OK_CACHE.get(url)
    if hit is not None:
        return hit.copy()
    os.makedirs("assets", exist_ok=True)
    path = f"assets/{key}.png"
    if not os.path.exists(path):
        try:
            r = requests.get(url, timeout=6, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            open(path, "wb").write(r.content)
        except Exception as e:
            log.warning("bg fetch %s: %s", url, e)
            _BG_FAIL_CACHE[url] = now
            return None
    try:
        img = Image.open(path).convert("RGB")
    except Exception as e:
        log.warning("bg open %s: %s", path, e)
        _BG_FAIL_CACHE[url] = now
        return None
    _BG_OK_CACHE[url] = img
    return img.copy()


def _blurred_bg(img: Image.Image, color_type: str = "fiat") -> Image.Image:
    """پس‌زمینه: cover + blur شدید + تم تاریک رنگی."""
    # cover-crop به نسبت W×H
    sw, sh = img.size
    target = W / H
    src = sw / sh
    if src > target:
        nw = int(sh * target)
        x0 = (sw - nw) // 2
        img = img.crop((x0, 0, x0 + nw, sh))
    else:
        nh = int(sw / target)
        y0 = (sh - nh) // 2
        img = img.crop((0, y0, sw, y0 + nh))
    img = img.resize((W, H), Image.LANCZOS)
    img = img.filter(ImageFilter.GaussianBlur(34))
    
    # ۲. تم تاریک با رنگ‌های متغیر
    colors = COLORS_BY_TYPE.get(color_type, COLORS_BY_TYPE["fiat"])
    base_color = colors["bg"]
    # دارک‌تر: overlay نیمه‌تنیده
    overlay = Image.new("RGB", (W, H), base_color)
    return Image.blend(img, overlay, 0.55)  # بیشتر تاریک شدند (۰.۵۵ به‌جای ۰.۴۵)


def _smooth(points, steps=None):
    """Catmull-Rom spline — خط نرم و یکدست (بدون تیکه‌تیکه)."""
    if len(points) < 3:
        return points
    pts = [(float(x), float(y)) for x, y in points]
    out = []
    n = len(pts)
    steps = steps or max(8, int((pts[-1][0] - pts[0][0]) / 2))
    for i in range(n - 1):
        p0 = pts[max(0, i - 1)]
        p1 = pts[i]
        p2 = pts[min(n - 1, i + 1)]
        p3 = pts[min(n - 1, i + 2)]
        for t in [j / steps for j in range(steps)]:
            t2, t3 = t * t, t * t * t
            x = 0.5 * ((2 * p1[0]) + (-p0[0] + p2[0]) * t +
                       (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2 +
                       (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3)
            y = 0.5 * ((2 * p1[1]) + (-p0[1] + p2[1]) * t +
                       (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2 +
                       (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3)
            out.append((x, y))
    out.append(pts[-1])
    return out


def _area_chart(history, w: int, h: int, up: bool, ohlcv=None, candlestick: bool = False) -> Image.Image:
    """نمودار حرفه‌ای: خط smooth نوری + گرادیان + کندل‌های ظریف + محور قیمت داخل کادر.
    candlestick=True → کندل‌ها برجسته‌تر (بدنه‌ی پهن‌تر + گلو) وقتی ohlcv کافی هست."""
    PAD_T, PAD_B, PAD_X = 14, 10, 6
    if len(history) < 2:
        history = (history + [history[0]]) if history else [0, 0]
    mn, mx = min(history), max(history)
    if mx == mn:
        mx = mn * 1.001 + 1
    rng = mx - mn
    n = len(history)
    line_color = GREEN if up else RED

    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    plot_h = h - PAD_T - PAD_B

    def y_of(v):
        return PAD_T + plot_h - (v - mn) / rng * plot_h

    # شبکه‌ی افقی ظریف (۴ خط داخل کادر)
    for i in range(1, 4):
        gy = PAD_T + int(plot_h * i / 4)
        d.line([(0, gy), (w, gy)], fill=(255, 255, 255, 16), width=1)

    # نقاط اصلی
    pts = []
    for i, v in enumerate(history):
        x = PAD_X + i / (n - 1) * (w - 2 * PAD_X)
        pts.append((x, y_of(v)))

    # گرادیان زیر خط (با ماسک smooth)
    smooth = _smooth(pts)
    grad = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grad)
    for yy in range(h):
        alpha = max(0, int(100 * (1 - yy / h)))
        gd.line([(0, yy), (w, yy)], fill=(line_color[0], line_color[1], line_color[2], alpha))
    mask = Image.new("L", (w, h), 0)
    md = ImageDraw.Draw(mask)
    poly = [(x, y) for x, y in smooth]
    poly += [(w - PAD_X, h), (PAD_X, h)]
    md.polygon(poly, fill=70)
    img.paste(grad, (0, 0), mask)

    # کندل‌ها (اگه OHLC داریم) — نیمه‌شفاف و باریک (پیش‌فرض) یا برجسته (candlestick=True)
    if ohlcv and 8 <= len(ohlcv) <= 120:
        show = ohlcv if len(ohlcv) <= 60 else ohlcv[::2]
        sstep = (w - 2 * PAD_X) / len(show)
        cw = max(3, min(8, int(sstep * 0.55))) if candlestick else max(2, min(6, int(sstep * 0.55)))
        body_a = 180 if candlestick else 110
        wick_a = 150 if candlestick else 80
        for i, (o, hi, lo, c) in enumerate(show):
            x = int(PAD_X + i * sstep + sstep / 2)
            col = GREEN if c >= o else RED
            yh = y_of(hi); yl = y_of(lo)
            yh = max(PAD_T, min(h - PAD_B, yh))
            yl = max(PAD_T, min(h - PAD_B, yl))
            d.line([(x, yh), (x, yl)], fill=col + (wick_a,), width=1)
            yo, yc = y_of(o), y_of(c)
            t, b = max(PAD_T, min(h - PAD_B, min(yo, yc))), max(PAD_T, min(h - PAD_B, max(yo, yc)))
            if b - t < 1.2:
                b = t + 1.2
            d.rounded_rectangle((x - cw // 2, t, x + cw // 2 + 1, b), radius=1, fill=col + (body_a,))
            # گلو دور بدنه‌ی کندل (فقط در حالت برجسته) — لایه‌ی محو زیر بدنه
            if candlestick:
                gr = cw + 5
                d.rounded_rectangle((x - gr // 2, t - 2, x + gr // 2 + 1, b + 2), radius=2,
                                    fill=col + (28,))

    # خط اصلی نرم + هاله‌ی نور (glow) — چند پاس با آلفا کم
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    gd2 = ImageDraw.Draw(glow)
    gd2.line(smooth, fill=line_color + (70,), width=9, joint="curve")
    gd2.line(smooth, fill=line_color + (110,), width=5, joint="curve")
    glow = glow.filter(ImageFilter.GaussianBlur(3))
    img.alpha_composite(glow)
    d.line(smooth, fill=(0, 0, 0, 60), width=2, joint="curve")
    d.line(smooth, fill=line_color + (255,), width=3, joint="curve")

    # نقطه‌ی درخشان آخر (داخل کادر) + Pulse (انیموشن ضعیف)
    ex, ey = smooth[-1]
    ey = max(PAD_T, min(h - PAD_B, ey))
    # ۵. Pulse: چندین لایه نورانی برای اثر تپش
    for r, a in [(16, 40), (11, 80), (6, 150), (2, 255)]:
        d.ellipse((ex - r, ey - r, ex + r, ey + r), fill=line_color + (a,))

        # محور قیمت — حذف شد (خواست کاربر: اعداد سمت راست چارت نباشه)
    return img


def _fmt(v) -> str:
    """BUG-3 فیکس: قیمت‌های خیلی کوچک (<0.01 مثل SHIB) با 8 رقم دقت — 0.00000521 قبلاً 0.000005 می‌شد."""
    if isinstance(v, float) and v < 0.01 and v > 0:
        s = f"{v:,.10f}".rstrip("0").rstrip(".")
        # حداقل ۸ رقم معنادار بعد از صفرهای پیشرو
        if len(s.split(".")[-1]) < 8 and "." in s:
            s = f"{v:,.8f}".rstrip("0")
            if s.endswith("."):
                s = s[:-1]
    elif isinstance(v, float) and v < 10:
        s = f"{v:,.6f}".rstrip("0").rstrip(".")
    elif isinstance(v, float):
        s = f"{v:,.2f}".rstrip("0").rstrip(".")
    else:
        s = f"{v:,}"
    return s


def render_fa_num(v) -> str:
    """عدد انگلیسی → فارسی (برای چیپ خرید/فروش)."""
    fa = str(_fmt(v)).translate(str.maketrans("0123456789,", "۰۱۲۳۴۵۶۷۸۹،"))
    return fa


_VIDEO_LOCK = threading.Lock()
_TON_BG_CACHE: dict = {}
_VIDEO_BASE_CACHE: dict = {}  # بک‌گراند ویدیو (no_chart) — کلید: (code, price, pct)  # بک‌گراند تون static — یک بار ساخته شه
_MP4_CACHE: OrderedDict = OrderedDict()
# ۲۵. قیمت لحظه‌ی رندر هر ویدیو — برای مچ‌کردن کپشن با بنر (بدون مغایرت)
_LAST_VIDEO_PRICE: dict = {}


def get_last_video_price(code: str):
    """قیمتی که آخرین ویدیوی رندرشده‌ی این ارز روش ساخته شده (برای کپشن مچ)."""
    ck = catalog.resolve(code) or code
    return _LAST_VIDEO_PRICE.get(ck)


def render_banner_video(code: str, duration: float = 3.0, fps: int = 24) -> Optional[bytes]:
    """۵. نمودار متحرک MP4 — خط با انیمیشن کشیده می‌شه + نقطه‌ی pulse.
    خروجی bytes (mp4/h264). کش ۶۰ ثانیه. fallback: None → فراخوان از PNG استفاده کنه."""
    ck = catalog.resolve(code) or code
    now = time.time()
    hit = _MP4_CACHE.get(ck)
    if hit and now - hit[0] < 60:
        val = hit[1]
        if val is None:
            del _MP4_CACHE[ck]
        else:
            return val
    with _VIDEO_LOCK:
        hit = _MP4_CACHE.get(ck)
        now = time.time()
        if hit and now - hit[0] < 60:
            return hit[1]
        try:
            # Fetch data once, pass to renderer (avoids duplicate HTTP call)
            data = datafeeds.get_banner_data(code)
            out = _render_video_uncached(code, ck, now, duration, fps, prefetched_data=data)
            if out is None:
                log.warning("video render returned None %s (hist=%s)", code, len((data or {}).get("history") or []))
            if out and data:
                _LAST_VIDEO_PRICE[ck] = data.get("price")
                _cache_cap(_LAST_VIDEO_PRICE)
            return out
        except Exception as e:
            log.warning("video render %s: %s", code, e)
            return None


def _ember_base(code: str, ck: str):
    """تصویر پایه‌ی Ember (بدون قیمت/نمودار) با کش — در والد صدا زده می‌شود تا
    فرزندهای fork درگیر قفل رندر نشوند."""
    _bkey = (ck, _asset_type(code))
    _bhit = _VIDEO_BASE_CACHE.get(_bkey)
    if _bhit is not None:
        return _bhit.copy()
    base_png = render_banner_base_no_chart(code, omit_price=True)
    if not base_png:
        return None
    base_img = Image.open(io.BytesIO(base_png)).convert("RGB")
    if len(_VIDEO_BASE_CACHE) > 12:
        keys_to_remove = list(_VIDEO_BASE_CACHE.keys())[:len(_VIDEO_BASE_CACHE) - 12]
        for k in keys_to_remove:
            del _VIDEO_BASE_CACHE[k]
    _VIDEO_BASE_CACHE[_bkey] = base_img.copy()
    return base_img


def _ember_prep(code: str, ck: str, data: dict):
    """آماده‌سازی مشترک رندر ویدیو: تاریخچه، پایه، لایه‌ها و نقاط نمودار.
    خروجی: دیکشنری آماده‌ی فریم‌سازی یا None."""
    hist = data.get("history") or []
    # فالبک تاریخچه: TGJU گاهی 500 می‌ده → برای usdt/dollar اول کندل‌های والکس،
    # برای بقیه‌ی ارزها تاریخچه‌ی محلی خودشان (تاریخچه‌ی تتر برای دلار/تتر فقط)
    if len(hist) < 4:
        if ck in ("usdt", "dollar"):
            wh = datafeeds.wallex_history("USDTTMN", 24)
            if len(wh) >= 4:
                hist = wh
        if len(hist) < 2:
            lh = datafeeds.local_history(ck)
            if len(lh) >= 2:
                hist = [p for _, p in lh]
        if len(hist) < 2:
            hist = [data["price"] * 0.999, data["price"]]
    if len(hist) < 2:
        return None
    price = data["price"]
    pct = data.get("change_pct")
    base_img = _ember_base(code, ck)
    if base_img is None:
        return None
    Wv, Hv = base_img.size
    w, h = (Wv // 2) * 2, (Hv // 2) * 2
    if (w, h) != (Wv, Hv):
        base_img = base_img.crop((0, 0, w, h))
    grad = _ember_amber_grad()
    chip_layer = _ember_chip_layer(pct)
    glow = _ember_glow_layer()
    cx0, cy0, cx1, cy1 = _EMBER_CHART
    sm = _ember_smooth(hist, cx0, cy0, cx1, cy1)
    hi, lo, frac = _ember_range(data, hist)
    # هاله‌ی خورشید در ۸ سطحِ تنفس از پیش داخل پایه پخته می‌شود (کامپوزیت کمتر)
    bases = []
    for _k in range(8):
        _ga = glow.copy()
        _av = 0.10 + 0.05 * (-1 + 2 * _k / 7)
        _ga.putalpha(_ga.split()[3].point(lambda v: int(v * _av)))
        bases.append(Image.alpha_composite(base_img.convert("RGBA"), _ga))
    return {
        "price": price, "pf": _ember_price_font(price), "bases": bases,
        "grad": grad, "chip": chip_layer, "sm": sm, "frac": frac,
        "chart": (cx0, cy0, cx1, cy1), "w": w, "h": h,
    }


def _ember_frame(P: dict, f: int, n_frames: int, fps: int):
    """یک فریم کامل Ember (تصویر RGB) از روی prep."""
    price = P["price"]
    pf = P["pf"]
    grad = P["grad"]
    chip_layer = P["chip"]
    sm = P["sm"]
    frac = P["frac"]
    cx0, cy0, cx1, cy1 = P["chart"]
    t = f / max(1, n_frames - 1)
    tsec = f / fps
    _ph = math.sin(tsec * math.pi * 2 * 0.57)
    img = P["bases"][int((_ph + 1) / 2 * 7.99)].copy()
    # پیشرفت اصلی — یک محرک واحد برای قیمت + نمودار + نوار بازه (هم‌زمان، بدون عقب‌جلو)
    mp = _ember_ease_io(_ember_clamp01((t - 0.06) / 0.56))
    # قیمت — شمارش از صفر تا قیمت نهایی با گرادیان کهربایی
    pv = price if t >= 0.64 else price * mp
    pv_show = int(pv) if price >= 100 else pv
    # کامپوزیت فقط روی نوار قیمت (نه تمام صفحه) — سریع‌تر
    _band = (40, 230, 880, 430)
    tm = Image.new("L", (_band[2] - _band[0], _band[3] - _band[1]), 0)
    ImageDraw.Draw(tm).text((84 - _band[0], 266 - _band[1]), _fmt(pv_show), font=pf, fill=255)
    _comp = Image.composite(grad.crop(_band), Image.new("RGBA", tm.size, (0, 0, 0, 0)), tm)
    img.paste(_comp, (_band[0], _band[1]), _comp)
    d = ImageDraw.Draw(img, "RGBA")
    # نمودار — کشیده‌شدن هم‌زمان با قیمت
    if mp > 0:
        n = max(2, int(len(sm) * mp))
        pts = sm[:n]
        fa = int(46 * _ember_clamp01((t - 0.22) / 0.34))
        if fa > 0:
            # پرکردن نیمه‌شفاف فقط با لایه+کامپوزیت درست blend می‌شود (Pillow 10.4)
            fl = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            ImageDraw.Draw(fl).polygon(pts + [(pts[-1][0], cy1), (cx0, cy1)], fill=(255, 154, 60, fa))
            img = Image.alpha_composite(img, fl)
            d = ImageDraw.Draw(img, "RGBA")
        d.line(pts, fill=(255, 154, 60, 90), width=7, joint="curve")
        d.line(pts, fill=(255, 205, 140, 255), width=3, joint="curve")
        ex, ey = pts[-1]
        pulse = 0.5 + 0.5 * math.sin(tsec * math.pi * 4)
        d.ellipse((ex - 10, ey - 10, ex + 10, ey + 10), fill=(255, 170, 90, int(40 + 40 * pulse)))
        d.ellipse((ex - 5, ey - 5, ex + 5, ey + 5), fill=(255, 244, 224, 255))
    # نوار بازه — لغزش یک‌طرفه از LOW تا جای واقعی قیمت (با همان mp)
    if t >= 0.06:
        mxp = 88 + (812 - 88) * (frac * mp)
        d.rounded_rectangle((88, 746, mxp, 754), 4, fill=(255, 170, 60, 230))
        d.ellipse((mxp - 9, 741, mxp + 9, 759), fill=(255, 244, 224, 255), outline=(255, 154, 60, 255), width=2)
    # چیپ درصد — ورود نرم نزدیک پایان sweep
    cp = _ember_clamp01((t - 0.60) / 0.14)
    if cp > 0 and chip_layer is not None:
        ch = chip_layer.copy()
        ch.putalpha(ch.split()[3].point(lambda v: int(v * cp)))
        img.paste(ch, (0, int((1 - cp) * 10)), ch)
        d = ImageDraw.Draw(img, "RGBA")
    # بج LIVE — چشمک واقعی روشن/خاموش (دوره‌ی ۱٫۲ ثانیه)
    cyc = (tsec % 1.2) / 1.2
    bright = 0.10 + 0.90 * ((0.5 + 0.5 * math.sin(cyc * math.pi * 2 - math.pi / 2)) ** 1.3)
    def _lc(c0, c1, k):
        return tuple(int(c0[i] + (c1[i] - c0[i]) * k) for i in range(3)) + (255,)
    rr_ = 8 + 9 * cyc
    d.ellipse((715 - rr_, 110 - rr_, 715 + rr_, 110 + rr_),
              outline=_lc((96, 58, 26), (255, 170, 90), bright * (1 - cyc)), width=2)
    d.ellipse((715 - 9, 110 - 9, 715 + 9, 110 + 9), fill=_lc((70, 42, 20), (150, 92, 40), bright))
    d.ellipse((715 - 6, 110 - 6, 715 + 6, 110 + 6), fill=_lc((120, 70, 28), (255, 199, 116), bright))
    # ذرات جرقه‌ی شناور — لایه + کامپوزیت (blend درست آلفا)
    pov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    pd = ImageDraw.Draw(pov)
    for p in _EMBER_PARTS:
        py = (p["y"] - p["sp"] * tsec) % 950
        px = p["x"] + math.sin(tsec * p["fq"] + p["ph"]) * p["amp"]
        twk = 0.55 + 0.45 * math.sin(tsec * 2.2 + p["tw"])
        rad = p["r"]
        pd.ellipse((px - rad * 3, py - rad * 3, px + rad * 3, py + rad * 3), fill=p["col"] + (int(26 * twk),))
        pd.ellipse((px - rad, py - rad, px + rad, py + rad), fill=p["col"] + (int(215 * twk),))
    img = Image.alpha_composite(img, pov)
    d = ImageDraw.Draw(img, "RGBA")
    # حلقه‌ی سونار دور لوگو (چسبیده + موج ملایم)
    sp_ = (tsec % 1.5) / 1.5
    sr = 40 + 13 * sp_
    d.ellipse((836 - sr, 886 - sr, 836 + sr, 886 + sr), outline=(255, 170, 90, int(110 * (1 - sp_))), width=2)
    return img.convert("RGB")


def _ember_write_segment(P: dict, f0: int, f1: int, n_frames: int, fps: int, path: str) -> bool:
    """فریم‌های [f0, f1) را با ffmpeg جدا به یک سگمنت mp4 تبدیل می‌کند."""
    _FF = imageio_ffmpeg.get_ffmpeg_exe()
    w, h = P["w"], P["h"]
    proc = subprocess.Popen(
        [_FF, "-y", "-threads", "1", "-f", "rawvideo", "-vcodec", "rawvideo",
         "-s", f"{w}x{h}",
         "-pix_fmt", "rgb24", "-r", str(fps), "-i", "-",
         "-an", "-vcodec", "libx264", "-preset", "ultrafast", "-tune", "animation", "-threads", "1",
         "-pix_fmt", "yuv420p", "-crf", "20", path],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for f in range(f0, f1):
            proc.stdin.write(_ember_frame(P, f, n_frames, fps).tobytes())
        proc.stdin.close()
    except BrokenPipeError:
        pass
    try:
        proc.wait(timeout=60)
    except Exception:
        proc.kill()
        proc.wait()
        return False
    return os.path.exists(path) and os.path.getsize(path) > 1000


def _ember_segment_job(job):
    """کار هر Process: prep مستقل + رندر سگمنت خودش (خطا = فایل ناقص → والد می‌فهمد)."""
    code, ck, data, fps, f0, f1, n_frames, path = job
    try:
        P = _ember_prep(code, ck, data)
        if P is None:
            return
        _ember_write_segment(P, f0, f1, n_frames, fps, path)
    except Exception:
        pass


def _render_video_parallel(code: str, ck: str, data: dict, duration: float, fps: int, n_frames: int):
    """رندر موازی روی ۲ هسته: دو Process، هرکدام نصف فریم‌ها + concat بدون ریکد.
    هر خطایی → None تا مسیر سریال فالبک شود."""
    import multiprocessing as _mp
    # پایه را در والد آماده کن تا فرزندها درگیر قفل رندر نشوند (کش گرم ارث می‌رسد)
    if _ember_base(code, ck) is None:
        return None
    mid = n_frames // 2
    tmpd = tempfile.mkdtemp(prefix="ember_seg_")
    seg1 = os.path.join(tmpd, "s1.mp4")
    seg2 = os.path.join(tmpd, "s2.mp4")
    outp = os.path.join(tmpd, "out.mp4")
    lst = os.path.join(tmpd, "list.txt")
    procs = []
    try:
        ctx = _mp.get_context("fork")
        for (f0, f1, path) in ((0, mid, seg1), (mid, n_frames, seg2)):
            p = ctx.Process(target=_ember_segment_job,
                            args=((code, ck, data, fps, f0, f1, n_frames, path),))
            p.start()
            procs.append(p)
        for p in procs:
            p.join(timeout=45)
        for p in procs:
            if p.is_alive():
                p.terminate()
                p.join(timeout=5)
                return None
        if not (os.path.exists(seg1) and os.path.getsize(seg1) > 1000
                and os.path.exists(seg2) and os.path.getsize(seg2) > 1000):
            return None
        with open(lst, "w") as fh:
            fh.write(f"file '{seg1}'\nfile '{seg2}'\n")
        _FF = imageio_ffmpeg.get_ffmpeg_exe()
        r = subprocess.run([_FF, "-y", "-f", "concat", "-safe", "0", "-i", lst,
                            "-c", "copy", "-movflags", "+faststart", outp],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        if r.returncode != 0 or not os.path.exists(outp) or os.path.getsize(outp) < 1000:
            return None
        return open(outp, "rb").read()
    finally:
        for p in procs:
            try:
                if p.is_alive():
                    p.terminate()
            except Exception:
                pass
        try:
            import shutil as _sh
            _sh.rmtree(tmpd, ignore_errors=True)
        except Exception:
            pass


def _render_video_uncached(code: str, ck: str, now: float, duration: float, fps: int, prefetched_data: dict | None = None) -> Optional[bytes]:
    # FIX: use prefetched data if available to avoid duplicate HTTP call
    data = prefetched_data if prefetched_data is not None else datafeeds.get_banner_data(code)
    if not data or not data.get("price"):
        return None
    n_frames = int(duration * fps)
    # مسیر موازی ۲ هسته‌ای (فالبک خودکار به سریال)
    if n_frames >= 24:
        try:
            out = _render_video_parallel(code, ck, data, duration, fps, n_frames)
            if out:
                _MP4_CACHE[ck] = (now, out)
                _cache_cap(_MP4_CACHE)
                return out
        except Exception as e:
            log.warning("parallel video render %s: %s", code, e)
    P = _ember_prep(code, ck, data)
    if P is None:
        return None
    _FF = imageio_ffmpeg.get_ffmpeg_exe()
    w, h = P["w"], P["h"]
    tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    tmp.close()
    try:
        proc = subprocess.Popen(
            [_FF, "-y", "-threads", "1", "-f", "rawvideo", "-vcodec", "rawvideo",
             "-s", f"{w}x{h}",
             "-pix_fmt", "rgb24", "-r", str(fps), "-i", "-",
             "-an", "-vcodec", "libx264", "-preset", "ultrafast", "-tune", "animation", "-threads", "1",
             "-pix_fmt", "yuv420p",
             "-movflags", "+faststart", "-crf", "20", tmp.name],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for f in range(n_frames):
                proc.stdin.write(_ember_frame(P, f, n_frames, fps).tobytes())
            proc.stdin.close()
        except BrokenPipeError:
            pass
        try:
            proc.wait(timeout=30)
        except Exception as e_w:
            log.warning("video ffmpeg wait %s: %s", code, e_w)
            proc.kill()
            proc.wait()
            return None
        out = open(tmp.name, "rb").read()
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass
    if not out or len(out) < 1000:
        log.warning("video output empty %s: %d bytes", code, len(out) if out else 0)
        return None
    _MP4_CACHE[ck] = (now, out)
    _cache_cap(_MP4_CACHE)
    return out


def _code_ids(code: str):
    """(tgju_id، binance_symbol) برای کد."""
    std = catalog.resolve(code)
    if not std:
        return None, None
    if std in catalog.FIAT:
        return catalog.FIAT[std][2], None  # tgju_id
    if std in catalog.GOLD:
        return catalog.GOLD[std][1], None
    if std in catalog.STABLE:
        return catalog.STABLE[std][1], None
    if std in catalog.CRYPTO:
        return None, catalog.CRYPTO[std][1]
    return None, None


_PNG_CACHE: OrderedDict = OrderedDict()
_RENDER_LOCK = threading.Lock()  # ۱۳. رندر هم‌زمان دوتا thread برای یه ارز → lock

def _asset_type(code: str) -> str:
    """نوع ارز برای رنگ‌بندی: fiat / gold / crypto / stable."""
    std = catalog.resolve(code) or code
    if std in catalog.FIAT:
        return "fiat"
    if std in catalog.GOLD:
        return "gold"
    if std in catalog.STABLE:
        return "stable"
    if std in catalog.CRYPTO:
        return "crypto"
    return "fiat"


_GOLD_LOGO_CACHE: dict = {}  # size → تصویر شمش (کپی برگردونده می‌شه تا تماس‌ها کش رو خراب نکنن)


def _gold_ingot_logo(size: int = 84) -> Optional[Image.Image]:
    """لوگوی شمش طلای سه‌بعدی با گرادیان — برای کادر آیکون طلا/سکه."""
    hit = _GOLD_LOGO_CACHE.get(size)
    if hit is not None:
        return hit.copy()
    s4 = size * 4  # supersample برای لبه‌های نرم
    img = Image.new("RGBA", (s4, s4), (0, 0, 0, 0))

    def lerp(a, b, t):
        return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))

    top_c, mid_c, bot_c = (255, 236, 150), (250, 200, 60), (176, 128, 20)

    # بدنه‌ی شمش — ذوزنقه با گرادیان عمودی
    m = s4 * 0.14          # حاشیه
    th = s4 * 0.46         # ارتفاع بدنه
    top_in = s4 * 0.09     # فرو رفتن بالای ذوزنقه
    bx0, by0 = m, s4 * 0.30
    bx1, by1 = s4 - m, by0 + th
    body = Image.new("RGBA", (s4, s4), (0, 0, 0, 0))
    bd = ImageDraw.Draw(body)
    bd.polygon([(bx0 + top_in, by0), (bx1 - top_in, by0), (bx1, by1), (bx0, by1)],
               fill=(255, 255, 255, 255))
    grad = Image.new("RGBA", (s4, s4), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grad)
    for yy in range(int(by0), int(by1) + 1):
        t = (yy - by0) / max(1, th)
        c = lerp(top_c, bot_c, t) if t < 0.55 else lerp(mid_c, bot_c, (t - 0.55) / 0.45)
        gd.line([(0, yy), (s4, yy)], fill=c + (255,))
    img.paste(grad, (0, 0), body)

    # براقیت بالای شمش (هایلایت)
    hl = Image.new("RGBA", (s4, s4), (0, 0, 0, 0))
    hd = ImageDraw.Draw(hl)
    hd.polygon([(bx0 + top_in + s4 * 0.05, by0 + s4 * 0.02),
                (bx1 - top_in - s4 * 0.05, by0 + s4 * 0.02),
                (bx1 - s4 * 0.16, by0 + th * 0.42),
                (bx0 + s4 * 0.16, by0 + th * 0.42)],
               fill=(255, 255, 255, 90))
    img = Image.alpha_composite(img, hl)

    # لبه‌ی پایین سایه
    sd = ImageDraw.Draw(img)
    sd.polygon([(bx0, by1), (bx1, by1), (bx1 - s4 * 0.03, by1 + s4 * 0.035),
                (bx0 + s4 * 0.03, by1 + s4 * 0.035)],
               fill=(120, 85, 10, 200))

    # درخشش دور شمش
    glow = Image.new("RGBA", (s4, s4), (0, 0, 0, 0))
    gld = ImageDraw.Draw(glow)
    gld.polygon([(bx0 + top_in, by0), (bx1 - top_in, by0), (bx1, by1), (bx0, by1)],
                outline=(255, 215, 0, 160), width=s4 // 28)
    glow = glow.filter(ImageFilter.GaussianBlur(s4 / 22))
    img = Image.alpha_composite(glow, img)

    out = img.resize((size, size), Image.LANCZOS)
    _cache_cap(_GOLD_LOGO_CACHE)
    _GOLD_LOGO_CACHE[size] = out.copy()
    return out.copy()


def _gold_lux_bg() -> Image.Image:
    """پس‌زمینه‌ی لوکس طلا — گرادیان تیره گرم + اشعه + bokeh طلایی + وینیت."""
    img = Image.new("RGB", (W, H), (14, 11, 4))
    d = ImageDraw.Draw(img, "RGBA")
    # گرادیان شعاعی مرکز-پایین (طلایی گرم)
    cx, cy = W // 2, int(H * 0.62)
    maxr = int((W ** 2 + H ** 2) ** 0.5 / 2)
    for r in range(maxr, 0, -12):
        t = r / maxr
        a = int(38 * (1 - t))
        d.ellipse((cx - r, cy - int(r * 1.15), cx + r, cy + int(r * 1.15)),
                  fill=(60, 44, 8, a))
    # اشعه‌های مورب ملایم
    ray = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    rd = ImageDraw.Draw(ray)
    rnd = random.Random(7)
    for i in range(9):
        x = rnd.randint(-100, W + 100)
        wdt = rnd.randint(30, 90)
        rd.polygon([(x, 0), (x + wdt, 0), (x + wdt - 220, H), (x - 220, H)],
                   fill=(255, 200, 60, rnd.randint(5, 12)))
    ray = ray.filter(ImageFilter.GaussianBlur(22))
    img = Image.alpha_composite(img.convert("RGBA"), ray)
    # bokeh طلایی
    bk = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bk)
    for i in range(26):
        x, y = rnd.randint(0, W), rnd.randint(0, H)
        r = rnd.randint(6, 34)
        a = rnd.randint(14, 44)
        bd.ellipse((x - r, y - r, x + r, y + r), fill=(255, 210, 90, a))
    bk = bk.filter(ImageFilter.GaussianBlur(7))
    img = Image.alpha_composite(img, bk)
    # وینیت تیره دور کادر
    vin = Image.new("L", (W, H), 0)
    vd = ImageDraw.Draw(vin)
    vd.ellipse((-W * 0.25, -H * 0.25, W * 1.25, H * 1.25), fill=255)
    vin = vin.filter(ImageFilter.GaussianBlur(140))
    dark = Image.new("RGBA", (W, H), (6, 5, 2, 150))
    dark.putalpha(Image.eval(vin, lambda p: max(0, 150 - p // 2)))
    img = Image.alpha_composite(img, dark)
    return img.convert("RGB")


_GOLD_BG_CACHE: Optional[Image.Image] = None


def _get_gold_bg() -> Image.Image:
    global _GOLD_BG_CACHE
    if _GOLD_BG_CACHE is None:
        _GOLD_BG_CACHE = _gold_lux_bg()
    return _GOLD_BG_CACHE


def _apply_asset_theme(base: Image.Image, asset_type: str) -> Image.Image:
    """۸. تم تخصصی هر دسته — لایه‌های پس‌زمینه با هویت بصری مجزا.
    بدون تغییر ساختار، فقط افکت روی base (امن برای ویدیو/PNG)."""
    if asset_type == "crypto":
        # شبکه/سیرکوئیت: گرید نقطه‌ای + گره‌های متصل (بنفش ملایم)
        img = base.convert("RGBA")
        ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        od = ImageDraw.Draw(ov)
        step = 90
        for gx in range(0, W + step, step):
            for gy in range(0, H + step, step):
                od.ellipse((gx - 2, gy - 2, gx + 2, gy + 2), fill=(180, 100, 255, 26))
        # خطوط اتصال محدود (بین گره‌های مجاور افقی)
        for gx in range(0, W + step, step):
            for gy in range(0, H + step, step):
                if gx + step <= W + step:
                    od.line([(gx, gy), (gx + step, gy)], fill=(180, 100, 255, 12), width=1)
        ov = ov.filter(ImageFilter.GaussianBlur(1))
        img.alpha_composite(ov)
        return img.convert("RGB")
    if asset_type == "stable":
        # هاله‌ی سبز ملایم از پایین (تری/استیبل)
        img = base.convert("RGBA")
        ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        od = ImageDraw.Draw(ov)
        for yy in range(H):
            t = 1 - yy / H
            a = int(18 * t * t)
            if a > 0:
                od.line([(0, yy), (W, yy)], fill=(100, 255, 150, a))
        img.alpha_composite(ov)
        return img.convert("RGB")
    if asset_type == "fiat":
        # خطوط موج ظریف ارزی (آبی) — نوارهای افقی ملایم
        img = base.convert("RGBA")
        ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        od = ImageDraw.Draw(ov)
        for yy in range(120, H, 220):
            a = 10
            od.line([(0, yy), (W, yy)], fill=(70, 150, 255, a), width=2)
        img.alpha_composite(ov)
        return img.convert("RGB")
    return base



def _draw_24h_range_bar(cd, x0, x1, y, low, high, price, accent):
    """۱۹. نوار موقعیت ۲۴ ساعته — قیمت الان کجای بازه Low–High است."""
    span = (high - low) or 1
    frac = max(0.0, min(1.0, (price - low) / span))
    bar_w = x1 - x0
    # ریل
    cd.rounded_rectangle((x0, y, x1, y + 10), radius=5, fill=(255, 255, 255, 28))
    # پرشدگی تا موقعیت فعلی
    pos_x = int(x0 + bar_w * frac)
    cd.rounded_rectangle((x0, y, pos_x, y + 10), radius=5, fill=accent + (230,))
    # نقطه‌ی موقعیت فعلی (درخشان)
    for r, a in [(9, 60), (6, 140), (3, 255)]:
        cd.ellipse((pos_x - r, y + 5 - r, pos_x + r, y + 5 + r), fill=accent + (a,))
    # لیبل‌های Low/High
    f_min = _font(20, "r")
    cd.text((x1, y + 22), _rtl(_fa(_fmt(high))), font=f_min, fill=GRAY, anchor="ra")
    cd.text((x0, y + 22), _rtl(_fa(_fmt(low))), font=f_min, fill=GRAY, anchor="la")


def _draw_card_neon(d, x0, y0, x1, y1, t: float):
    """ایده ۴ (نسخه نهایی): نئون دور کارت — فقط رنگ پیوسته عوض می‌شه (آبی→بنفش→طلایی→سبز).
    بدون قوس چرخان (کاربر نخواست)."""
    palette = [(70, 150, 255), (180, 100, 255), (255, 215, 0), (100, 255, 150)]
    n = len(palette)
    ph = t * n
    i0 = int(ph) % n
    i1 = (i0 + 1) % n
    ffrac = ph - int(ph)
    col = tuple(int(palette[i0][k] + (palette[i1][k] - palette[i0][k]) * ffrac) for k in range(3))
    d.rounded_rectangle((x0, y0, x1, y1), radius=36, outline=col + (200,), width=4)


def _draw_shine_sweep(cd, x0, y0, x1, y1, t: float, accent):
    """ایده ۱: نوار نور (shine) که روی کارت حرکت می‌کنه. t∈[0,1) موقعیت."""
    cx = x0 + (x1 - x0) * t
    w = (x1 - x0) * 0.16
    for i in range(int(w), 0, -2):
        a = int(90 * (1 - i / w))
        if a <= 0:
            continue
        xx = cx - i
        cd.rectangle((xx, y0, xx + 2, y1), fill=(255, 255, 255, a))
    # حلقه‌ی نرم روی لبه
    halo = max(2, int(w * 0.4))
    if 0 <= cx <= x1:
        cd.rectangle((cx - halo, y0, cx + halo, y1),
                     fill=(255, 255, 255, int(40 * (0.5 + 0.5 * math.sin(t * math.pi)))), width=0)


def _draw_neon_border(card, cw, ch, t: float, accent):
    """ایده ۴: حاشیه نئون با گرادیان چرخشی. t∈[0,1) فاز. در PNG از t=0 استفاده می‌شه."""
    # رنگ چرخشی بین آبی/بنفش/طلایی/سبز
    palette = [(70, 150, 255), (180, 100, 255), (255, 215, 0), (100, 255, 150)]
    n = len(palette)
    ph = t * n
    i0 = int(ph) % n
    i1 = (i0 + 1) % n
    f = ph - int(ph)
    col = tuple(int(palette[i0][k] + (palette[i1][k] - palette[i0][k]) * f) for k in range(3))
    glow = Image.new("RGBA", card.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for gw, ga in [(8, 36), (5, 70), (3, 120)]:
        gd.rounded_rectangle((-gw // 2, -gw // 2, cw - 1 + gw // 2, ch - 1 + gw // 2),
                             radius=36 + gw // 2, outline=col + (ga,), width=gw)
    glow = glow.filter(ImageFilter.GaussianBlur(7))
    card.alpha_composite(glow)
    # حاشیه‌ی تیز روی خود کارت (بازگردوننده‌ی رنگ اصلی هم هست)
    cdraw = ImageDraw.Draw(card)
    cdraw.rounded_rectangle((1, 1, cw - 2, ch - 2), radius=36, outline=col + (220,), width=2)
    return col


def _draw_particles(cd, w, h, t: float, accent, seed=1):
    """ایده ۶: ذرات نورانی شناور (بالا می‌رن). t∈[0,1) فاز کلی.
    seed پیش‌فرض per-frame (فریم فایل ویدیو) پاس داده می‌شه → هر فریم چیدمان خودش رو
    داره و ذرات بین فریم‌ها واقعاً جابه‌جا دیده می‌شن (فقط فرمول حرکت عوض نشده)."""
    rnd = random.Random(seed)
    for _ in range(14):
        px = rnd.randint(20, w - 20)
        speed = 0.05 + rnd.random() * 0.08
        base_y = rnd.randint(40, h - 40)
        phase = rnd.random()
        yy = (base_y - ((t + phase) % 1.0) * h * speed) % h
        r = rnd.randint(2, 5)
        a = int(120 * (1 - ((t + phase) % 1.0)))
        if a <= 0:
            continue
        cd.ellipse((px - r, int(yy) - r, px + r, int(yy) + r),
                  fill=accent[:3] + (max(0, a),))


def _draw_gold_ingot_divider(cd, x0, x1, y):
    """ایده ۸: جداکننده ردیف شمش‌های طلایی کوچیک (فقط طلا)."""
    n = max(3, int((x1 - x0) / 70))
    gap = (x1 - x0) / (n + 1)
    for i in range(n):
        cx = x0 + gap * (i + 1)
        cd.rounded_rectangle((cx - 22, y - 8, cx + 22, y + 8), radius=4,
                             fill=(255, 200, 60, 200), outline=(255, 230, 120, 255), width=1)


def _draw_progress_ring(cd, cx, cy, r, frac, accent):
    """ایده ۹: حلقه پیشرفت دور لوگو — frac∈[0,1] نشون‌دهنده موقعیت قیمت در بازه ۲۴س."""
    # پس‌زمینه
    cd.arc((cx - r, cy - r, cx + r, cy + r), 0, 360, fill=(255, 255, 255, 36), width=5)
    # پرشدگی از بالا شروع می‌شه
    start = -90
    end = start + 360 * max(0.0, min(1.0, frac))
    cd.arc((cx - r, cy - r, cx + r, cy + r), start, end, fill=accent + (235,), width=5)
    # نقطه‌ی انتهایی درخشان
    ang = math.radians(end)
    ex = cx + r * math.cos(ang)
    ey = cy + r * math.sin(ang)
    for rr, aa in [(9, 60), (5, 140), (3, 255)]:
        cd.ellipse((ex - rr, ey - rr, ex + rr, ey + rr), fill=accent + (aa,))


def _draw_surge_badge(cd, x, y, accent):
    """ایده ۷: بج جهش (🔺/🔻) وقتی |٪|≥۲ — روی کادر."""
    cd.rounded_rectangle((x, y, x + 120, y + 40), radius=20,
                         fill=(255, 60, 60, 50), outline=(255, 90, 90, 220), width=2)
    cd.text((x + 60, y + 20), "⚡ جهش", font=_font(22, "b"), fill=(255, 160, 160), anchor="mm")


def _rollup_digits(img, cx, cy, target, t, unit=""):
    """ایده ۲: انیمیشن roll-up عدد قیمت (فقط ویدیو). t∈[0,1) پیشرفت.
    قرارداد دقت دقیقاً مثل _fmt (بر اساس target تا فرمت وسط انیمیشن نپره):
    <0.01 → ~۸ رقم اعشار، <10 → ۶ رقم، بقیه → ۲ رقم — فریم آخر == _fmt(price)."""
    eased = 1 - (1 - t) ** 3
    cur = target * eased
    if isinstance(target, float) and target < 0.01 and target > 0:
        txt = f"{cur:,.10f}".rstrip("0").rstrip(".")
        if len(txt.split(".")[-1]) < 8 and "." in txt:
            txt = f"{cur:,.8f}".rstrip("0")
            if txt.endswith("."):
                txt = txt[:-1]
    elif isinstance(target, float) and target < 10:
        txt = f"{cur:,.6f}".rstrip("0").rstrip(".")
    elif isinstance(target, float):
        txt = f"{cur:,.2f}".rstrip("0").rstrip(".")
    else:
        txt = f"{int(round(cur)):,}"
    if unit == "دلار":
        txt += " $"
    d = ImageDraw.Draw(img)
    d.text((cx, cy), _rtl(_fa(txt)), font=_font(104, "b"), fill=GOLD_BRIGHT, anchor="mm")
    return img


def _draw_live_badge(cd, x, y, pulse_t: float = 0.0):
    """۲۰. بج LIVE — دایره سبز درخشان + متن. pulse_t∈[0,1) → تپش نرم."""
    if pulse_t > 0:
        blink = 0.3 + 0.7 * (0.5 + 0.5 * math.sin(pulse_t * 2 * math.pi))
    else:
        blink = 1.0
    for r, a in [(9, int(60 * blink)), (6, int(130 * blink)), (4, int(255 * blink))]:
        cd.ellipse((x - r, y - r, x + r, y + r), fill=(46, 204, 113, min(255, a)))
    f_b = _font(22, "b")
    lv = blink if pulse_t > 0 else 1.0
    cd.text((x + 14, y), "LIVE", font=f_b,
            fill=(int(46 * lv), int(204 * lv), int(113 * lv)), anchor="lm")


def _cache_cap(d: dict, max_entries: int = 24):
    """سقف حافظه‌ی کش‌ها — قدیمی‌ترین ورودیِ درج‌شده حذف می‌شه (ترتیب درج dict)."""
    while len(d) > max_entries:
        d.pop(next(iter(d)))


def render_market_card() -> Optional[bytes]:
    """۲۱. کارت «بازار امروز» — گرید ۸ ارز + بیشترین رشد/افت."""
    now = time.time()
    hit = _PNG_CACHE.get("__market__")
    if hit and now - hit[0] < 20:
        return hit[1]
    with _RENDER_LOCK:
        hit = _PNG_CACHE.get("__market__")
        if hit and now - hit[0] < 20:
            return hit[1]
        try:
            m = datafeeds.market_overview()
        except Exception:
            m = None
        if not m or not m.get("rows"):
            return None

        W2, H2 = 900, 1100
        img = Image.new("RGB", (W2, H2), (12, 12, 16))
        d = ImageDraw.Draw(img)
        # گرادیان پس‌زمینه
        for yy in range(H2):
            t = yy / H2
            d.line([(0, yy), (W2, yy)], fill=(int(12 + 16 * t), int(12 + 12 * t), int(16 + 10 * t)))

        # هدر
        f_title = _font(46, "b")
        d.text((W2 // 2, 70), _rtl(_fa("📊 بازار امروز")), font=f_title, fill=GOLD_BRIGHT, anchor="mm")
        _draw_live_badge(d, W2 // 2 - 110, 70)
        ir = timezone(timedelta(hours=3, minutes=30))
        now_fa = datetime.now(ir).strftime("%H:%M:%S")
        d.text((W2 // 2 + 130, 70), _rtl(_fa(now_fa)), font=_font(24, "r"), fill=GRAY, anchor="mm")

        # گرید ۲×۴
        cols, cw2 = 2, (W2 - 80) // 2
        rows_data = m["rows"][:8]
        rh = 150
        y0 = 140
        accent_map = {"fiat": (70, 150, 255), "gold": (255, 215, 0),
                      "crypto": (180, 100, 255), "stable": (100, 255, 150)}
        for i, r in enumerate(rows_data):
            col, row = i % cols, i // cols
            x0 = 40 + col * (cw2 + 20)
            y1 = y0 + row * (rh + 16)
            atype = _asset_type(r["key"])
            accent = accent_map.get(atype, (70, 150, 255))
            d.rounded_rectangle((x0, y1, x0 + cw2, y1 + rh), radius=22,
                                fill=(22, 22, 30, 200), outline=accent + (160,), width=2)
            # نام + قیمت + ٪
            d.text((x0 + cw2 - 20, y1 + 26), _rtl(_fa(r["name"])), font=_font(28, "b"),
                   fill=WHITE, anchor="ra")
            price_txt = f"{_fmt(r['price'])}" + ("" if r["unit"] == "تومان" else " $")
            d.text((x0 + cw2 - 20, y1 + 78), _rtl(_fa(price_txt)), font=_font(36, "b"),
                   fill=GOLD_BRIGHT, anchor="ra")
            if r["unit"] == "تومان":
                d.text((x0 + cw2 - 20, y1 + 122), _rtl(_fa("تومان")), font=_font(20, "r"),
                       fill=GRAY, anchor="ra")
            if r["pct"] is not None:
                c = GREEN if r["pct"] >= 0 else RED
                d.text((x0 + 20, y1 + 78), f"{r['pct']:+.2f}%", font=_font(26, "b"),
                       fill=c, anchor="lm")
            # اسپارک‌لاین کوچیک
            try:
                dd = datafeeds.get_banner_data(r["key"])
                h = (dd.get("history") or [])[-30:] if dd else []
                if len(h) >= 3:
                    mn, mx = min(h), max(h)
                    rng = (mx - mn) or 1
                    sw, sh = cw2 // 3, 34
                    sx0, sy0 = x0 + 20, y1 + rh - 58
                    pts = [(sx0 + i / (len(h) - 1) * sw, sy0 + sh - (v - mn) / rng * sh)
                           for i, v in enumerate(h)]
                    d.line(pts, fill=accent + (200,), width=2, joint="curve")
            except Exception:
                pass

        # بیشترین رشد/افت
        yb = y0 + 4 * (rh + 16) + 16
        f_b = _font(26, "b")
        if m.get("top"):
            n, p = m["top"]
            d.rounded_rectangle((40, yb, W2 // 2 - 10, yb + 56), radius=16,
                                fill=(46, 204, 113, 40), outline=GREEN + (200,), width=2)
            d.text((W2 // 4, yb + 28), _rtl(_fa(f"🔺 بیشترین رشد: {n} ({p:+.2f}%)")),
                   font=f_b, fill=GREEN, anchor="mm")
        if m.get("bottom"):
            n, p = m["bottom"]
            d.rounded_rectangle((W2 // 2 + 10, yb, W2 - 40, yb + 56), radius=16,
                                fill=(235, 87, 87, 40), outline=RED + (200,), width=2)
            d.text((W2 * 3 // 4, yb + 28), _rtl(_fa(f"🔻 بیشترین افت: {n} ({p:+.2f}%)")),
                   font=f_b, fill=RED, anchor="mm")

        # واترمارک
        d.text((W2 // 2, H2 - 30), _rtl(_fa("⭐ AuroraPriceBot · @iprez")),
               font=_font(22, "b"), fill=(230, 230, 235, 200), anchor="mm")

        buf = io.BytesIO()
        img.save(buf, "PNG")
        _PNG_CACHE["__market__"] = (now, buf.getvalue())
        _cache_cap(_PNG_CACHE)
        return _PNG_CACHE["__market__"][1]


def render_banner(code: str) -> Optional[bytes]:
    ck = catalog.resolve(code) or code
    hit = _PNG_CACHE.get(ck)
    now = time.time()
    # کش ۲۰ ثانیه — تعادل تازگی/سرعت (رندر تکراری کمتر، صف کوتاه‌تر)
    if hit and now - hit[0] < 20:
        return hit[1]

    # ۱۳. فقط یه thread در آن واحد رندر کنه (double-check بعد از lock)
    with _RENDER_LOCK:
        hit = _PNG_CACHE.get(ck)
        now = time.time()
        if hit and now - hit[0] < 20:
            return hit[1]
        return _render_banner_uncached(code, ck, now)


def render_banner_base_no_chart(code: str, omit_price: bool = False) -> Optional[bytes]:
    """بنر کامل بدون نمودار (پس‌زمینه‌ی ویدیوی انیمیشنی) — بدون کش (بلافاصله مصرف می‌شه).
    omit_price=True → قیمت و چیپ «تومان» کشیده نمی‌شه (ویدیو هر فریم خودش می‌کشه)."""
    ck = catalog.resolve(code) or code
    with _RENDER_LOCK:
        now = time.time()
        return _render_banner_uncached(code, ck, now, no_chart=True, omit_price=omit_price)


# ================= Ember theme (جایگزین طراحی بنر — ۲۰۲۶-۱۱-۲۵) =================
# طراحی Ember Sunset: کارت شیشه‌ای گرم روی گرادیان آلویی-نارنجی، قیمت کهربایی،
# نمودار و نوار بازه‌ی هم‌زمان. entry pointها و کش‌ها دست نخورده‌اند.
_EMBER_CHART = (84, 548, 816, 668)
_EMBER_CREAM = (255, 244, 224, 255)
_EMBER_MUT = (232, 183, 148, 255)
_EMBER_BACKDROP = None
_EMBER_GRAD = None
_EMBER_GLOW = None


def _ember_clamp01(v):
    return max(0.0, min(1.0, v))


def _ember_ease_io(t):
    return 4 * t ** 3 if t < 0.5 else 1 - ((-2 * t + 2) ** 3) / 2


def _ember_vgrad(stops):
    g = Image.new("RGB", (1, H))
    for y in range(H):
        t = y / H
        for i in range(len(stops) - 1):
            p0, c0 = stops[i]
            p1, c1 = stops[i + 1]
            if p0 <= t <= p1:
                k = (t - p0) / max(1e-6, p1 - p0)
                g.putpixel((0, y), tuple(int(c0[j] + (c1[j] - c0[j]) * k) for j in range(3)))
                break
    return g.resize((W, H))


def _ember_backdrop():
    """پس‌زمینه + کارت Ember — مستقل از ارز، یک بار ساخته و کش می‌شود."""
    global _EMBER_BACKDROP
    if _EMBER_BACKDROP is not None:
        return _EMBER_BACKDROP.copy()
    img = _ember_vgrad([(0, (27, 11, 46)), (0.45, (59, 16, 57)), (0.75, (122, 46, 29)), (1, (24, 10, 20))]).convert("RGBA")
    sun = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(sun).ellipse((430, 300, 1010, 880), fill=(255, 154, 60, 130))
    img = Image.alpha_composite(img, sun.filter(ImageFilter.GaussianBlur(70)))
    sun2 = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(sun2).ellipse((600, 440, 880, 720), fill=(255, 190, 90, 120))
    img = Image.alpha_composite(img, sun2.filter(ImageFilter.GaussianBlur(40)))
    dr = ImageDraw.Draw(img)
    rr = random.Random(11)
    for _ in range(90):
        x, y = rr.randint(0, W), rr.randint(0, int(H * 0.45))
        dr.ellipse((x, y, x + 2, y + 2), fill=(255, 240, 220, rr.randint(20, 70)))
    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle((44, 52, 856, 848), 36, fill=(0, 0, 0, 130))
    img = Image.alpha_composite(img, sh.filter(ImageFilter.GaussianBlur(14)))
    card = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(card).rounded_rectangle((44, 40, 856, 836), 36, fill=(26, 12, 16, 178), outline=(255, 170, 90, 90), width=2)
    img = Image.alpha_composite(img, card)
    vig_mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(vig_mask).ellipse((-180, -160, 1080, 1110), fill=255)
    vig_mask = vig_mask.filter(ImageFilter.GaussianBlur(120))
    vig = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    vig.putalpha(ImageChops.invert(vig_mask).point(lambda v: int(v * 0.35)))
    img = Image.alpha_composite(img, vig)
    _EMBER_BACKDROP = img
    return img.copy()


def _ember_amber_grad():
    global _EMBER_GRAD
    if _EMBER_GRAD is None:
        _EMBER_GRAD = _ember_vgrad([(0, (255, 227, 179)), (0.55, (255, 170, 60)), (1, (214, 106, 28))]).convert("RGBA")
    return _EMBER_GRAD


def _ember_glow_layer():
    global _EMBER_GLOW
    if _EMBER_GLOW is None:
        g = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(g).ellipse((430, 300, 1010, 880), fill=(255, 154, 60, 255))
        _EMBER_GLOW = g.filter(ImageFilter.GaussianBlur(70))
    return _EMBER_GLOW


_prnd = random.Random(7)
_EMBER_PARTS = [dict(x=_prnd.uniform(70, 830), y=_prnd.uniform(0, 950), sp=_prnd.uniform(7, 18),
                     amp=_prnd.uniform(4, 11), fq=_prnd.uniform(0.5, 1.4), ph=_prnd.uniform(0, 6.28),
                     r=_prnd.uniform(1.1, 2.8), tw=_prnd.uniform(0, 6.28),
                     col=_prnd.choice([(255, 170, 90), (255, 205, 140), (255, 128, 66)]))
                for _ in range(26)]


def _ember_ticker(std: str) -> str:
    if std in catalog.CRYPTO:
        return std
    if std in catalog.GOLD:
        return "GOLD"
    return {"dollar": "USD", "euro": "EUR", "pound": "GBP"}.get(std, catalog.display_en(std))


def _ember_price_font(price) -> ImageFont.FreeTypeFont:
    """فونت قیمت — برای عددهای خیلی بلند (سکه) خودکار کوچک می‌شود تا از کارت بیرون نزند."""
    size = 100
    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    while size > 52 and probe.textlength(_fmt(price), font=_font(size, "b")) > 680:
        size -= 8
    return _font(size, "b")


def _ember_range(data, hist):
    """(hi, lo, frac) — اول high/low واقعی ۲۴ساعته‌ی دیتا، وگرنه از تاریخچه."""
    price = data["price"]
    hi = data.get("high_24")
    lo = data.get("low_24")
    if not hi or not lo or hi <= lo:
        vals = [float(x) for x in (hist or []) if x] or [price]
        lo, hi = min(vals), max(vals)
    lo = min(lo, price)
    hi = max(hi, price)
    frac = (price - lo) / (hi - lo) if hi > lo else 0.5
    return hi, lo, frac


def _ember_smooth(vals, x0, y0, x1, y1):
    """Catmull-Rom روی مقادیر تاریخچه — خروجی: نقاط (x,y) نرم برای نمودار."""
    vals = [float(v) for v in vals]
    if len(vals) < 2:
        vals = vals + vals
    mn, mx = min(vals), max(vals)
    pad = (mx - mn) * 0.12 or 1
    pts = [(x0 + (x1 - x0) * i / (len(vals) - 1),
            y1 - (y1 - y0) * (v - mn + pad) / (mx - mn + 2 * pad)) for i, v in enumerate(vals)]
    sm = []
    for i in range(len(pts) - 1):
        p0 = pts[max(0, i - 1)]
        p1 = pts[i]
        p2 = pts[i + 1]
        p3 = pts[min(len(pts) - 1, i + 2)]
        for j in range(10):
            t = j / 10
            sm.append((0.5 * ((2 * p1[0]) + (-p0[0] + p2[0]) * t + (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t ** 2 + (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t ** 3),
                       0.5 * ((2 * p1[1]) + (-p0[1] + p2[1]) * t + (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t ** 2 + (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t ** 3)))
    sm.append(pts[-1])
    return sm


def _ember_icon(code: str, std: str):
    """دایره‌ی آیکون ۸۸×۸۸ — پرچم/لوگوی ارز؛ طلا: شمش؛ فالبک: حروف نماد."""
    disc = Image.new("RGBA", (88, 88), (0, 0, 0, 0))
    dd = ImageDraw.Draw(disc)
    dd.ellipse((0, 0, 87, 87), fill=(20, 28, 48, 235), outline=(150, 180, 255, 90), width=2)
    img = None
    if std == "TON" and os.path.exists("assets/ton_icon.png"):
        try:
            img = Image.open("assets/ton_icon.png").convert("RGBA")
        except Exception:
            img = None
    if img is None:
        try:
            img = _fetch_bg_image(code)
        except Exception:
            img = None
    if img is not None:
        ic = img.convert("RGBA").resize((67, 67), Image.LANCZOS)
        m = Image.new("L", (67, 67), 0)
        ImageDraw.Draw(m).ellipse((2, 2, 65, 65), fill=255)
        disc.paste(ic, (10, 10), m)
        return disc
    if _asset_type(code) == "gold":
        try:
            ing = _gold_ingot_logo(76)
            if ing is not None:
                disc.alpha_composite(ing.convert("RGBA"), (6, 6))
                return disc
        except Exception:
            pass
    dd.text((44, 44), _ember_ticker(std)[:4], font=_font(22, "b"), fill=_EMBER_CREAM, anchor="mm")
    return disc


def _ember_chip_layer(pct):
    """لایه‌ی چیپ درصد تغییر + برچسب PAST 24 HOURS (None اگر pct نداریم)."""
    if pct is None:
        return None
    up = pct >= 0
    acc = (52, 211, 153) if up else (248, 113, 113)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    cd = ImageDraw.Draw(layer)
    txt = f"{abs(pct):.2f}%"
    cf = _font(27, "b")
    cw = cd.textlength(txt, font=cf) + 62
    cd.rounded_rectangle((84, 446, 84 + cw, 500), 27, fill=acc + (36,), outline=acc + (150,), width=1)
    if up:
        cd.polygon((112, 460, 104, 476, 120, 476), fill=acc + (255,))
    else:
        cd.polygon((112, 486, 104, 470, 120, 470), fill=acc + (255,))
    cd.text((130, 458), txt, font=cf, fill=acc + (255,), anchor="la")
    cd.text((84 + cw + 16, 460), "P A S T   2 4   H O U R S", font=_font(16, "m"), fill=_EMBER_MUT, anchor="la")
    return layer


def _ember_base_image(code: str, data: dict, hist, full: bool):
    """تصویر Ember — full=True: بنر کامل استاتیک؛ full=False: پایه‌ی ویدیو (بدون قیمت/نمودار/چیپ)."""
    std = catalog.resolve(code) or code
    price = data["price"]
    pct = data.get("change_pct")
    img = _ember_backdrop()
    dr = ImageDraw.Draw(img)
    img.alpha_composite(_ember_icon(code, std), (84, 84))
    dr = ImageDraw.Draw(img)
    title = "US DOLLAR" if std == "dollar" else catalog.display_en(std)
    dr.text((196, 94), title, font=_font(42, "b"), fill=_EMBER_CREAM, anchor="la")
    dr.text((198, 150), f"EMBER · {_ember_ticker(std)}", font=_font(16, "m"), fill=_EMBER_MUT, anchor="la")
    # بج LIVE (قرص) — نقطه در حالت استاتیک روشن ثابت؛ در ویدیو هر فریم چشمک می‌زند
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(ov).rounded_rectangle((692, 88, 816, 132), 22, fill=(255, 154, 60, 30), outline=(255, 170, 90, 130), width=1)
    img = Image.alpha_composite(img, ov)
    dr = ImageDraw.Draw(img)
    dr.text((732, 100), "LIVE", font=_font(20, "b"), fill=(255, 226, 188, 255), anchor="la")
    if full:
        dr.ellipse((706, 101, 724, 119), fill=(150, 92, 40, 255))
        dr.ellipse((709, 104, 721, 116), fill=(255, 199, 116, 255))
    dr.text((84, 238), "L I V E   P R I C E", font=_font(18, "m"), fill=_EMBER_MUT, anchor="la")
    # واحد کنار قیمت — موقعیتش از پهنای قیمت نهایی حساب می‌شود (در هر دو حالت)
    unit = "USD" if _asset_type(code) == "crypto" else "TOMAN"
    pf = _ember_price_font(price)
    tw = dr.textlength(_fmt(price), font=pf)
    dr.text((84 + tw + 16, 310), unit, font=_font(28, "b"), fill=(255, 205, 140, 255), anchor="la")
    hi, lo, frac = _ember_range(data, hist)
    # برچسب‌های بازه + ترک کم‌رنگ (در هر دو حالت)
    dr.text((88, 684), "LOW", font=_font(16, "m"), fill=_EMBER_MUT, anchor="la")
    dr.text((88, 712), _fmt(lo), font=_font(21, "b"), fill=_EMBER_CREAM, anchor="la")
    dr.text((812, 684), "HIGH", font=_font(16, "m"), fill=_EMBER_MUT, anchor="ra")
    dr.text((812, 712), _fmt(hi), font=_font(21, "b"), fill=_EMBER_CREAM, anchor="ra")
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(ov).rounded_rectangle((88, 746, 812, 754), 4, fill=(255, 220, 180, 46))
    img = Image.alpha_composite(img, ov)
    dr = ImageDraw.Draw(img)
    if full:
        # قیمت با گرادیان کهربایی
        tm = Image.new("L", (W, H), 0)
        ImageDraw.Draw(tm).text((84, 266), _fmt(price), font=pf, fill=255)
        img = Image.alpha_composite(img, Image.composite(_ember_amber_grad(), Image.new("RGBA", (W, H), (0, 0, 0, 0)), tm))
        dr = ImageDraw.Draw(img)
        # چیپ درصد
        chip = _ember_chip_layer(pct)
        if chip is not None:
            img = Image.alpha_composite(img, chip)
            dr = ImageDraw.Draw(img)
        # نمودار کامل
        cx0, cy0, cx1, cy1 = _EMBER_CHART
        sm = _ember_smooth(hist, cx0, cy0, cx1, cy1)
        fl = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(fl).polygon(sm + [(cx1, cy1), (cx0, cy1)], fill=(255, 154, 60, 46))
        img = Image.alpha_composite(img, fl)
        dr = ImageDraw.Draw(img)
        dr.line(sm, fill=(255, 154, 60, 90), width=7, joint="curve")
        dr.line(sm, fill=(255, 205, 140, 255), width=3, joint="curve")
        ex, ey = sm[-1]
        dr.ellipse((ex - 10, ey - 10, ex + 10, ey + 10), fill=(255, 170, 90, 110))
        dr.ellipse((ex - 5, ey - 5, ex + 5, ey + 5), fill=(255, 244, 224, 255))
        # نوار بازه — پرشده تا جای واقعی قیمت + مارکر
        mxp = 88 + (812 - 88) * frac
        dr.rounded_rectangle((88, 746, mxp, 754), 4, fill=(255, 170, 60, 230))
        dr.ellipse((mxp - 9, 741, mxp + 9, 759), fill=(255, 244, 224, 255), outline=(255, 154, 60, 255), width=2)
    # فوتر — لوگو + حلقه‌ی چسبیده + امضا
    try:
        lg = Image.open("assets/logo_circle.png").convert("RGBA")
        img.paste(lg, (800, 850), lg)
    except Exception:
        pass
    dr = ImageDraw.Draw(img)
    dr.ellipse((796, 846, 876, 926), outline=(255, 170, 90, 150), width=2)
    dr.text((785, 886), "AuroraPriceBot · @iprez", font=_font(24, "b"), fill=(255, 240, 224, 220), anchor="rm")
    return img


def _ember_render_banner(code: str, ck: str, now: float, data: dict, hist, no_chart: bool) -> Optional[bytes]:
    img = _ember_base_image(code, data, hist, full=not no_chart)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG")
    if no_chart:
        # بنر ویدیویی کش نمی‌شه (موقته) — فقط برگرد
        return buf.getvalue()
    _PNG_CACHE[ck] = (now, buf.getvalue())
    _cache_cap(_PNG_CACHE)
    return _PNG_CACHE[ck][1]


def _render_banner_uncached(code: str, ck: str, now: float, no_chart: bool = False,
                            omit_price: bool = False) -> Optional[bytes]:
    """رندر واقعی بنر — فقط از render_banner صدا زده شه (lock گرفته شده).
    no_chart=True → پایه‌ی Ember بدون نمودار/قیمت (پس‌زمینه‌ی ویدیوی انیمیشنی)."""
    data = datafeeds.get_banner_data(code)
    if not data or not data.get("price"):
        return None

    tg_id, b_sym = _code_ids(code)
    # ثبت snapshot زنده (برای تاریخچه‌ی دقیقه‌ای آینده)
    if tg_id:
        datafeeds.record_snapshot(catalog.resolve(code), tgju_id=tg_id)
    elif b_sym:
        datafeeds.record_snapshot(catalog.resolve(code), binance_sym=b_sym)
    else:
        datafeeds.record_snapshot(catalog.resolve(code))

    price = data["price"]
    if not data.get("updated"):
        ir = timezone(timedelta(hours=3, minutes=30))
        data["updated"] = datetime.now(ir).strftime("%H:%M:%S")
    hist = data.get("history") or []
    # ارزهای er-api تاریخچه ندارن → خط تخت زنده بساز (از snapshotهای ربات)
    if len(hist) < 2 and price:
        lh = datafeeds.local_history(ck)
        if len(lh) >= 2:
            hist = [p for _, p in lh]
        else:
            hist = [price * 0.999, price]

    return _ember_render_banner(code, ck, now, data, hist, no_chart=no_chart)
