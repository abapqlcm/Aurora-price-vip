"""
داده‌های بنر: قیمت لحظه‌ای + درصد تغییر ۲۴h + تاریخچه برای نمودار
منابع:
  - TGJU ajax (قیمت لحظه‌ای ایران) + TGJU indicator API (تاریخچه)
  - Binance klines (تاریخچه + تغییر 24h کریپتو)
"""
import re
import time
import logging
from typing import List, Optional, Tuple

import requests

import catalog

log = logging.getLogger("data")

UA = {"User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
_cache = {}
CACHE_TTL = 3  # کش خیلی کوتاه — قیمت واقعاً لایو (تغییرات سریع دیده بشه)


def _get(url, timeout=12):
    r = requests.get(url, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r


def _cached(key, fn):
    now = time.time()
    # تبدیل کلید به رشته برای اطمینان از کارکرد صحیح کش
    key_str = str(key) if not isinstance(key, str) else key
    if key_str in _cache and now - _cache[key_str][0] < CACHE_TTL:
        return _cache[key_str][1]
    v = fn()
    _cache[key_str] = (now, v)
    return v


# ---------------- TGJU ----------------

def _tgju_now() -> dict:
    def fetch():
        return _get("https://call1.tgju.org/ajax.json").json().get("current", {})
    return _cached("tgju", fetch)


def tgju_quote(tgju_id: str) -> Tuple[Optional[int], Optional[float], Optional[int]]:
    """(قیمت تومان، درصد تغییر، تغییر ریالی) — از فیلدهای p/dp/d."""
    c = _tgju_now()
    rec = c.get(tgju_id, {})
    p = rec.get("p")
    if p is None:
        return None, None, None
    p = int(float(str(p).replace(",", "")))
    dp = rec.get("dp")
    try:
        dp = float(dp)
    except (TypeError, ValueError):
        dp = None
    d = rec.get("d")
    try:
        d = int(float(str(d).replace(",", "")))
    except (TypeError, ValueError):
        d = None
    return p, dp, d


_hist_cache: dict = {}

def tgju_history(tgju_id: str, days: int = 14) -> List[float]:
    """بسته‌شدن‌های روزانه از indicator API — کش 1 ساعته (روزانه‌ست، زود عوض نمی‌شه).
    خطا هم ۵ دقیقه کش می‌شه تا قطعی API باعث کندی زنجیره‌ای نشه."""
    key = f"h14_{tgju_id}"
    hit = _hist_cache.get(key)
    if hit and time.time() - hit[0] < (3600 if hit[1] else 300):
        return hit[1]
    def fetch():
        try:
            r = _get(f"https://api.tgju.org/v1/market/indicator/summary-table-data/{tgju_id}", timeout=15)
            data = r.json().get("data", [])
            if not data:
                _hist_cache[key] = (time.time(), [])
                return []
            rows = data[:days]
            closes = []
            for row in rows:
                if len(row) < 4:
                    continue
                close = row[3]  # [open, low, high, close, ...]
                closes.append(float(str(close).replace(",", "")))
            v = list(reversed(closes))
            _hist_cache[key] = (time.time(), v)
            return v
        except Exception as e:
            log.warning("tgju history %s: %s", tgju_id, e)
            # کش منفی ۵ دقیقه‌ای — جلوی تلاش مجدد هر ثانیه رو می‌گیره
            _hist_cache[key] = (time.time(), [])
            return []
    return fetch()


# ---------------- Binance ----------------

def binance_klines(symbol: str, interval: str = "15m", limit: int = 96) -> List[float]:
    """کندل‌های اخیر — close prices، قدیمی→جدید."""
    ohlcv = binance_ohlcv(symbol, interval, limit)
    return [c[3] for c in ohlcv] if ohlcv else []


def binance_ohlcv(symbol: str, interval: str = "15m", limit: int = 96) -> List[list]:
    """کندل‌های اخیر — [open, high, low, close]، قدیمی→جدید."""
    def fetch():
        try:
            r = _get(f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}")
            return [[float(k[1]), float(k[2]), float(k[3]), float(k[4])] for k in r.json()]
        except Exception as e:
            log.warning("binance ohlcv %s: %s", symbol, e)
            return []
    return _cached(("ohlc", symbol), fetch)


def binance_24h_change(symbol: str) -> Optional[float]:
    """درصد تغییر 24 ساعته از ticker."""
    def fetch():
        try:
            r = _get(f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}")
            return float(r.json()["priceChangePercent"])
        except Exception as e:
            log.warning("binance 24h %s: %s", symbol, e)
            return None
    return _cached(("c", symbol), fetch)


# ---------------- Wallex (بازار آزاد ایران — لایو) ----------------

# ⚡ سرعت: یک کال واحد به coin-market-list → همه‌ی قیمت‌های والکس (خرید/شاخص/های/لو/٪)
# قبلاً ۳ تابع جدا هر کدوم ~۴۰۰-۵۵۰ms کال میزدن → الان فقط ۱ کال (۵۵۰ms) برای هر ۳ تابع
_WALLEX_URL = ("https://api.wallex.ir/v1/coin-market-list?baseAsset=USDT&fields="
               "baseAsset,quotes.TMN.price,quotes.TMN.wPrice,"
               "quotes.TMN.dailyHighPrice,quotes.TMN.dailyLowPrice,quotes.TMN.percentChange24h")

def wallex_usdt_quote() -> dict:
    """یک کال — کل کوتیشن تتر والکس:
    {buy, index, high, low, ch} | {} اگه خطا."""
    def fetch():
        try:
            r = _get(_WALLEX_URL, timeout=8)
            for m in r.json().get("result", {}).get("markets", []):
                if m.get("baseAsset") == "USDT":
                    t = m.get("quotes", {}).get("TMN", {})
                    return {
                        "buy": float(t.get("price") or 0) or None,       # قیمت خرید (۲۱۸k)
                        "index": float(t.get("wPrice") or 0) or None,    # شاخص OTC (۲۱۷k)
                        "high": float(t.get("dailyHighPrice") or 0) or None,
                        "low": float(t.get("dailyLowPrice") or 0) or None,
                        "ch": float(t.get("percentChange24h") or 0),
                    }
        except Exception as e:
            log.warning("wallex usdt: %s", e)
        return {}
    return _cached("wallex_usdt", fetch)


def wallex_buy_quote() -> Tuple[Optional[float], Optional[float]]:
    """(قیمت خرید والکس، تغییر ۲۴h٪) — از کوتیشن واحد (بدون کال تکراری)."""
    q = wallex_usdt_quote()
    return q.get("buy"), q.get("ch")


def wallex_index_high_low() -> Tuple[Optional[float], Optional[float]]:
    """سقف/کف ۲۴ ساعته‌ی شاخص OTC — خام، از کوتیشن واحد."""
    q = wallex_usdt_quote()
    return q.get("high"), q.get("low")


def wallex_history(symbol: str = "USDTTMN", hours: int = 24) -> List[float]:
    """تاریخچه‌ی واقعی ۲۴ ساعته از کندل‌های UDF والکس (ساعتی) — کش ۱۰ دقیقه.
    برای نمودار بنر دلار/تتر وقتی TGJU در دسترس نیست."""
    key = f"whist_{symbol}_{hours}"
    hit = _hist_cache.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]

    def fetch():
        try:
            now = int(time.time())
            url = (f"https://api.wallex.ir/v1/udf/history?symbol={symbol}"
                   f"&resolution=60&from={now - hours*3600}&to={now}")
            r = _get(url, timeout=12)
            j = r.json()
            closes = [float(c) for c in (j.get("c") or []) if c]
            # حذف کندل‌های صفر/ناقص
            closes = [c for c in closes if c > 0]
            _hist_cache[key] = (time.time(), closes)
            return closes
        except Exception as e:
            log.warning("wallex history %s: %s", symbol, e)
            _hist_cache[key] = (time.time(), [])
            return []
    return fetch()

def _wallex_markets() -> dict:
    """نمادهای والکس — کش ۵ ثانیهای (لایو)."""
    def fetch():
        r = _get("https://api.wallex.ir/v1/markets", timeout=12)
        return r.json().get("result", {}).get("symbols", {})
    return _cached("wallex", fetch)


def wallex_quote(symbol: str) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """(آخرین قیمت تومانی، تغییر ۲۴h٪، بالاترین ۲۴h) از والکس."""
    syms = _wallex_markets()
    rec = syms.get(symbol)
    if not rec:
        return None, None, None
    st = rec.get("stats", {})
    def _f(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None
    last = _f(st.get("lastPrice"))
    if last is None:
        last = _f(st.get("bidPrice")) or _f(st.get("askPrice"))
    ch = _f(st.get("24h_ch"))
    return last, ch, _f(st.get("24h_highPrice"))


def usdt_toman() -> float:
    """شاخص OTC والکس (wPrice — خام لایو). fallback: markets lastPrice."""
    p = wallex_usdt_quote().get("index")
    if p:
        return p
    last, _, _ = wallex_quote("USDTTMN")
    return last or 0.0


# ---------------- Binance FX (نرخ جهانی ارزها) ----------------

def binance_fx_rate(usd_pair: str) -> Optional[float]:
    """نرخ دلاری ارز (مثل EURUSDT) از بایننس."""
    def fetch():
        try:
            r = _get(f"https://api.binance.com/api/v3/ticker/24hr?symbol={usd_pair}")
            return float(r.json()["lastPrice"])
        except Exception as e:
            log.warning("binance fx %s: %s", usd_pair, e)
            return None
    return _cached(("fx", usd_pair), fetch)




# ---------------- ER-API (166 ارز جهانی — یک کال) ----------------

_fx_rates_cache = {"t": 0, "rates": {}}

def erapi_rates() -> dict:
    """نرخ جهانی همه‌ی ارزها نسبت به دلار — کش 10 دقیقه.
    خطا هم کش منفی ~90s می‌شه و در این فاصله کش قدیمی (حتی stale) سرو می‌شه."""
    now = time.time()
    have = bool(_fx_rates_cache["rates"])
    if have and now - _fx_rates_cache["t"] < 600:
        return _fx_rates_cache["rates"]
    # کش منفی: بعد از خطا ~90 ثانیه دوباره تلاش نکن
    if now - _fx_rates_cache["t"] < 90 and not have:
        return {}
    try:
        r = _get("https://open.er-api.com/v6/latest/USD", timeout=12)
        rates = r.json().get("rates", {})
        if rates:
            _fx_rates_cache["t"] = now
            _fx_rates_cache["rates"] = rates
            return rates
        # پاسخ بدون rates → مثل خطا رفتار کن (ت timestamp برای کش منفی)
        _fx_rates_cache["t"] = now
    except Exception as e:
        log.warning("erapi: %s", e)
        _fx_rates_cache["t"] = now
    # بازگشت کش قدیمی حتی اگر stale باشد (جلوگیری از کرش در fx_to_usd)
    return _fx_rates_cache["rates"]


def fx_to_usd(ccy: str) -> Optional[float]:
    """1 واحد از این ارز چند دلار؟ (مثل SEK → 0.104)"""
    rates = erapi_rates()
    v = rates.get(ccy.upper())
    if not v:
        return None
    return 1.0 / v


# ---------------- واحد یکپارچه ----------------

# کد داخلی → کد ISO واقعی برای er-api — فقط وقتی نام er-api با code.upper() فرق داره
# بقیه‌ی کدها مستقیم با code.upper() از rates خونده می‌شن (er-api 160+ کد داره)
_FIAT_ISO = {
    "dollar": "USD",  # کد داخلی اسم کامل داره، نه ISO
    "euro": "EUR",    # euro.upper() = EURO — کد er-api نیست
    "pound": "GBP",   # pound.upper() = POUND — کد er-api نیست
    "sdp": "SDD",     # پوند سودان — audit: er-api کد SDD. اما لایو er-api فقط SDG داره (166 کد)
}


def _fiat_iso(code: str) -> str:
    """کد ISO er-api برای کد داخلی — upper() پیش‌فرض، نگاشت صریح فقط برای استثناها."""
    return _FIAT_ISO.get(code) or code.upper()


def _fx_pct_from_binance(code: str, iso: str) -> Optional[float]:
    """درصد تغییر 24h ارز غیردلاری: نرخ جفت بایننس (مثل EURUSDT) الان vs ۲۴ ساعت قبل.
    اگر بایننس جفت نداشت یا خطا خورد → None (کپشن نباید +0.00% الکی چاپ کنه)."""
    try:
        pair = f"{iso}USDT"
        now_rate = binance_fx_rate(pair)
        ohlcv = binance_ohlcv(pair, "1h", 25) if now_rate else []
        if not now_rate or not ohlcv:
            return None
        p_24 = ohlcv[0][3]  # close قدیمی‌ترین کندل از ۲۵ کندل ساعتی (~24 ساعت)
        if not p_24 or p_24 == 0:
            return None
        return round((now_rate - p_24) / p_24 * 100, 2)
    except Exception as e:
        log.warning("fiat 24h pct %s: %s", code, e)
        return None


def get_banner_data(code: str) -> Optional[dict]:
    """
    برای هر کد داخل catalog، داده‌ی کامل بنر را می‌سازد:
    {name, price(تومان), change_pct, change_abs, history[], source}
    """
    code = catalog.resolve(code)
    if not code:
        return None
    if code in catalog.FIAT:
        name, _, tg_id, fx_sym = catalog.FIAT[code]
        # دلار = قیمت «خرید ارز» والکس (price لایو — همون که تو بخش خرید میبینی)
        # بقیه ارزها = قیمت خرید × نرخ جهانی + ۰.۵٪ سود بازار
        buy_p, _ = wallex_buy_quote()  # price لایو والکس (۲۱۸k)
        usdt_t = round(buy_p * 1.005) if buy_p else None  # فقط برای ارزهای غیردلاری
        price = None
        pct = None
        if code == "dollar":
            price = round(buy_p) if buy_p else None  # خام خرید — مثل تتر
            pct = wallex_usdt_quote().get("ch")  # از کوتیشن واحد — بدون کال دوم
        elif fx_sym:
            rate = binance_fx_rate(fx_sym)
            if usdt_t and rate:
                price = round(usdt_t * rate)
            else:
                # بایننس جفت نداشت → er-api
                v = fx_to_usd(_fiat_iso(code))
                if usdt_t and v:
                    price = round(usdt_t * v)
        else:
            v = fx_to_usd(_fiat_iso(code))
            if usdt_t and v:
                price = round(usdt_t * v)
            else:
                v = None
        # تغییر ۲۴h ارزهای غیردلاری — نرخ جهانی الان vs ۲۴ ساعت قبل (بایننس)
        # اگر در دسترس نبود None می‌مونه (فالبک TGJU dp خودش رو داره)
        if code != "dollar" and price is not None and pct is None:
            pct = _fx_pct_from_binance(code, _fiat_iso(code))
        if price is None:
            # فالبک نهایی: TGJU
            p, dp, d = tgju_quote(tg_id)
            hist_rial = tgju_history(tg_id)
            hist = [x / 10 for x in hist_rial]
            return {
                "name": name, "price": (p // 10 if p else None), "change_pct": dp,
                "change_abs": (d // 10 if d else None),
                "history": hist, "unit": "تومان", "source": "TGJU",
            }
        return {
            "name": name, "price": price, "change_pct": pct,
            "change_abs": None,
            "history": ([x / 10 for x in tgju_history(tg_id)] if tg_id else []),
            "unit": "تومان", "source": "Wallex+Binance (لایو +۰.۵٪)",
        }

    if code in catalog.GOLD:
        name, tg_id, _ = catalog.GOLD[code]
        p, dp, d = tgju_quote(tg_id)
        hist_rial = tgju_history(tg_id)
        hist = [x / 10 for x in hist_rial]
        return {
            "name": name, "price": (p // 10 if p else None), "change_pct": dp,
            "change_abs": (d // 10 if d else None),
            "history": hist, "unit": "تومان", "source": "TGJU",
        }

    if code in catalog.STABLE:
        name, tg_id, _ = catalog.STABLE[code]
        # تتر = قیمت «خرید ارز» والکس (price — لایو، همون عددی که کاربر تو بخش خرید میبینه)
        # ⚠️ های/لو ۲۴h همیشه از v1/markets (24h_highPrice/24h_lowPrice) میاد —
        # فیلدهای dailyHighPrice/dailyLowPrice در coin-market-list خرابن (هر دو == wPrice)
        buy_p, ch = wallex_buy_quote()      # قیمت خرید لایو (۲۱۸k) — بدون سود اضافه
        otc = usdt_toman()                  # شاخص wPrice خام (۲۱۶k)
        last = round(buy_p) if buy_p else None
        if last:
            high_24 = low_24 = None
            try:
                syms = _wallex_markets()
                stats = syms.get('USDTTMN', {}).get('stats', {})
                # های/لو ۲۴h خام والکس — بدون سود ۰.۵٪ (همون که سایت wallex.ir نشون میده)
                if stats.get('24h_highPrice'):
                    high_24 = round(float(stats['24h_highPrice']))
                if stats.get('24h_lowPrice'):
                    low_24 = round(float(stats['24h_lowPrice']))
            except Exception as e:
                log.warning("wallex USDTTMN stats: %s", e)
            if high_24 is None or low_24 is None:
                # فالبک: markets در دسترس نبود → کوتیشن واحد (رفتار قدیمی)
                hi_i, lo_i = wallex_index_high_low()
                high_24 = round(hi_i) if hi_i else last
                low_24 = round(lo_i) if lo_i else last
            # fake ohlcv برای chart — ساختار [open, high, low, close]؛ فقط وقتی hi>lo معناداره
            ohlcv = []
            if high_24 > low_24:
                ohlcv = [
                    [low_24, high_24, low_24, low_24],
                    [low_24, high_24, low_24, last],
                    [last, high_24, low_24, last]
                ]
            return {
                "name": name, "price": last, "change_pct": ch,
                "change_abs": None,
                "high_24": high_24, "low_24": low_24,  # برای نوار موقعیت ۲۴س
                "index_price": otc,  # شاخص wPrice — برای کپشن (تفاوت با خرید)
                "history": [x / 10 for x in tgju_history(tg_id)],
                "ohlcv": ohlcv,
                "unit": "تومان", "source": "Wallex خرید (لایو)",
            }
        # fallback
        p, dp, d = tgju_quote(tg_id)
        hist = [x / 10 for x in tgju_history(tg_id)]
        return {
            "name": name, "price": (p // 10 if p else None), "change_pct": dp,
            "change_abs": (d // 10 if d else None),
            "history": hist, "unit": "تومان", "source": "TGJU",
        }

    if code in catalog.CRYPTO:
        name, sym, _ = catalog.CRYPTO[code]
        ohlcv = binance_ohlcv(sym)
        if not ohlcv:
            return None
        klines = [c[3] for c in ohlcv]
        price_usd = klines[-1]
        # تغییر ۲۴h واقعی — از ticker/24hr بایننس (priceChangePercent؛ کندل ۲۵ در 15m فقط ۶ ساعت بود)
        pct = binance_24h_change(sym)
        if pct is None:
            # فالبک: کندل ۲۴h قبل از خود کندل‌ها (۹۶ کندل ۱۵m = ۲۴ ساعت → ایندکس -97)
            p_24 = klines[-97] if len(klines) >= 97 else klines[0]
            pct = round((price_usd - p_24) / p_24 * 100, 2) if p_24 and p_24 != 0 else None
        else:
            p_24 = price_usd / (1 + pct / 100) if pct != -100 else 0.0
        # تاریخچه‌ی ۲۴h به تومان تبدیل می‌کنیم (دلار × تتر داخلی) تا همه چیز تومانی باشه؟
        # نه — کریپتو را دلاری نشان می‌دهیم، استانداردتر است
        ch_abs = price_usd - p_24 if p_24 else 0.0
        # ۱۷. high/low واقعی ۲۴h از کندل‌های ۱۵ دقیقه‌ای اخیر (۹۶ کندل = ۲۴ ساعت)
        h24 = ohlcv[-96:] if len(ohlcv) >= 96 else ohlcv
        high_24 = max((x[1] for x in h24), default=price_usd) if h24 else price_usd
        low_24 = min((x[2] for x in h24), default=price_usd) if h24 else price_usd
        return {
            "name": name, "price": price_usd, "change_pct": pct,
            "change_abs": ch_abs, "history": klines, "ohlcv": ohlcv,
            "high_24": high_24, "low_24": low_24,  # برای نوار موقعیت ۲۴س
            "unit": "دلار", "source": "Binance",
        }

    return None


# ۱۸. کارت «بازار امروز» — گرید چند ارز + بیشترین رشد/افت
MARKET_KEYS = ["dollar", "euro", "pound", "usdt", "gold_18", "coin_emami", "BTC", "ETH"]


def market_overview() -> dict:
    """دیتای خلاصه برای کارت بازار: قیمت + ٪تغییر هر ارز محبوب.
    خروجی: {"rows": [(name, price, pct, unit)], "top": (name,pct)|None, "bottom": (name,pct)|None}
    """
    rows = []
    for k in MARKET_KEYS:
        try:
            d = get_banner_data(k)
        except Exception:
            d = None
        if not d or not d.get("price"):
            continue
        rows.append({
            "key": k, "name": d["name"], "price": d["price"],
            "pct": d.get("change_pct"), "unit": d.get("unit", "تومان"),
        })
    top = bottom = None
    with_pct = [r for r in rows if r["pct"] is not None]
    if with_pct:
        sorted_r = sorted(with_pct, key=lambda r: r["pct"], reverse=True)
        # فقط اگر بیشترین مقدار مثبت باشد top را تنظیم کن
        if sorted_r[0]["pct"] > 0:
            top = (sorted_r[0]["name"], sorted_r[0]["pct"])
        # فقط اگر کمترین مقدار منفی باشد bottom را تنظیم کن
        if sorted_r[-1]["pct"] < 0:
            bottom = (sorted_r[-1]["name"], sorted_r[-1]["pct"])
    return {"rows": rows, "top": top, "bottom": bottom}


# ---------------- Local minute history (self-recorded) ----------------
import json as _json
import os as _os

HIST_DIR = "hist"
TTL_MIN = 60  # نگه‌داشتن ۶۰ نقطه‌ی اخیر


def _hist_path(key: str) -> str:
    _os.makedirs(HIST_DIR, exist_ok=True)
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", key)
    return _os.path.join(HIST_DIR, f"{safe}.json")


_last_snap_ts: dict = {}  # code → آخرین ts ثبت‌شده — skip بدون خواندن فایل


def record_snapshot(code: str, tgju_id: str = None, binance_sym: str = None):
    """قیمت لحظه‌ای را در تاریخچه‌ی محلی ثبت می‌کند — هر بار که بنر خواسته شه."""
    # ۱۱. جلوگیری از ثبت تکراری در <55 ثانیه — با کش in-memory (بدون I/O)
    now = int(time.time())
    if now - _last_snap_ts.get(code, 0) < 55:
        return
    path = _hist_path(f"local_{code}")
    try:
        data = []
        if _os.path.exists(path):
            try:
                with open(path, "r") as f:
                    data = _json.load(f)
            except Exception:
                data = []
        # اگه آخرین snapshot فایل تازه‌ست (<55s) — مثلاً بعد از ری‌استارت پروسه
        if data and now - data[-1][0] < 55:
            _last_snap_ts[code] = data[-1][0]
            return
        price = None
        if not tgju_id and not binance_sym:
            d = get_banner_data(code)
            price = d.get("price") if d else None
        if tgju_id:
            p, _, _ = tgju_quote(tgju_id)
            price = p // 10 if p else None  # تومان
        elif binance_sym:
            try:
                price = float(_get(f"https://api.binance.com/api/v3/ticker/price?symbol={binance_sym}").json()["price"])
            except Exception:
                price = None
        if not price:
            return
        data.append([now, price])
        # حذف نقاط قدیمی‌تر از ۶۰ نقطه یا ۲۴ ساعت
        data = [x for x in data if now - x[0] < 86400][-60:]
        # نوشتن اتمیک — tmp + replace تا فایل نصفه‌نیمه باقی نمونه
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            _json.dump(data, f)
        _os.replace(tmp, path)
        _last_snap_ts[code] = now
    except Exception as e:
        log.warning("record_snapshot %s: %s", code, e)


def local_history(code: str) -> list:
    """تاریخچه‌ی محلی ثبت‌شده (may be short)."""
    path = _hist_path(f"local_{code}")
    try:
        return _json.load(open(path))
    except Exception:
        return []


def smart_history(code: str, tgju_id: str = None, binance_sym: str = None) -> Tuple[list, str]:
    """
    هوشمندانه‌ترین تاریخچه:
    - اگر تاریخچه‌ی محلی ≥ ۱۰ نقطه → همون (برچسب: 'X دقیقه اخیر')
    - وگرنه تاریخچه‌ی رسمی (TGJU روزانه / Binance ساعتی)
    خروجی: (مقادیر، برچسب)
    """
    local = local_history(code)
    if len(local) >= 10:
        span_min = round((local[-1][0] - local[0][0]) / 60)
        label = f"روند {span_min} دقیقه اخیر (زنده)"
        return [x[1] for x in local], label
    # fallback رسمی
    if tgju_id:
        hist = tgju_history(tgju_id)
        if hist:
            return [x / 10 for x in hist], "روند ۱۴ روز گذشته"
    if binance_sym:
        k = binance_klines(binance_sym)
        if k:
            return k, "روند ۲۴ ساعت گذشته"
    return [], ""
