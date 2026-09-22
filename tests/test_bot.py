"""تست‌های رگرشن برای AuroraPriceBot — بدون نیاز به تلگرام یا API زنده.

اجرا: python3 -m tests.test_bot  (یا pytest tests/)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import render
import catalog


# ---------------- parse_input ----------------

CASES_PARSE = [
    # (ورودی, kind ожидаемо, data)
    ("دلار", "single", "dollar"),
    ("دولار", "single", "dollar"),
    ("usd", "single", "dollar"),
    ("یورو", "single", "euro"),
    ("طلا", "single", "gold_18"),
    ("طلای ۱۸", "single", "gold_18"),
    ("سکه", "single", "coin_emami"),
    ("تتر", "single", "usdt"),
    ("بیت کوین", "single", "BTC"),
    ("بیتکوین", "single", "BTC"),
    ("BTC", "single", "BTC"),
    ("تون", "single", "TON"),
    ("گرم", "single", "TON"),
    ("۱۲۵ دلار", "calc", ("dollar", 125.0)),
    ("2 گرم طلا", "calc", ("gold_18", 2.0)),
    ("۵۰ تتر", "calc", ("usdt", 50.0)),
    ("بازار", "market", None),
    ("بازار امروز", "market", None),
    ("market", "market", None),
    ("پینگ", None, None),       # کلمات کلیدی قبل از parse هندل می‌شن
    ("چطوری حالت؟", None, None),  # جمله طولانی → None
]

def test_parse_input():
    fails = 0
    for text, kind_exp, data_exp in CASES_PARSE:
        kind, data = render.parse_input(text)
        if kind != kind_exp:
            print(f"  ✗ {text!r}: kind={kind} (expected {kind_exp})")
            fails += 1
            continue
        if kind == "single" and data != data_exp:
            print(f"  ✗ {text!r}: key={data!r} (expected {data_exp!r})")
            fails += 1
        if kind == "calc" and data != tuple(data_exp):
            print(f"  ✗ {text!r}: calc={data!r} (expected {data_exp!r})")
            fails += 1
    print(f"  parse_input: {len(CASES_PARSE) - fails}/{len(CASES_PARSE)} passed")
    return fails == 0


def test_parse_fa_digits():
    """اعداد فارسی باید به انگلیسی تبدیل بشن."""
    ok = render.parse_input("۱۲۵ دلار") == ("calc", ("dollar", 125.0))
    ok &= render.parse_input("۱،۰۰۰ یورو") == ("calc", ("euro", 1000.0))
    print(f"  parse_fa_digits: {'OK' if ok else 'FAIL'}")
    return ok


def test_parse_long_ignored():
    """جمله‌های طولانی در گروه نباید ربات رو بیدار کنن."""
    ok = render.parse_input("سلام خوبی امروز بازار چطوره میشه") == (None, None)
    print(f"  parse_long_ignored: {'OK' if ok else 'FAIL'}")
    return ok


# ---------------- fmt_num ----------------

CASES_FMT = [
    (1234567, "1,234,567"),
    (0, "0"),
    (100000.0, "100,000"),
    (0.00001234, "0.00001234"),   # کریپتو کوچک
    (1.5, "1.5"),
    ("1234", "1,234"),            # ورودی رشته‌ای
    ("invalid", "invalid"),       # رشته‌ی غیرعددی دست‌نخورده
]

def test_fmt_num():
    fails = 0
    for v, exp in CASES_FMT:
        got = render.fmt_num(v)
        if got != exp:
            print(f"  ✗ fmt_num({v!r}) = {got!r} (expected {exp!r})")
            fails += 1
    print(f"  fmt_num: {len(CASES_FMT) - fails}/{len(CASES_FMT)} passed")
    return fails == 0


# ---------------- catalog ----------------

def test_catalog_resolve():
    ok = catalog.resolve("dollar") == "dollar"
    ok &= catalog.resolve("usd") == "dollar"
    ok &= catalog.resolve("eur") == "euro"
    ok &= catalog.resolve("BTC") == "BTC"
    ok &= catalog.resolve("xyz_unknown") is None
    # طلا/سکه
    ok &= catalog.resolve("gold_18") == "gold_18"
    ok &= catalog.resolve("coin_emami") == "coin_emami"
    print(f"  catalog_resolve: {'OK' if ok else 'FAIL'}")
    return ok


def test_catalog_coverage():
    """هر کد catalog باید در FA_TO_KEY هم باشه تا parse_input پیداش کنه."""
    missing = []
    for k in list(catalog.FIAT) + list(catalog.GOLD) + list(catalog.STABLE) + list(catalog.CRYPTO):
        # کدهای crypto به حروف کوچیک هم ثبت می‌شن
        if k not in render.FA_TO_KEY and k.lower() not in render.FA_TO_KEY:
            missing.append(k)
    if missing:
        print(f"  ✗ missing from FA_TO_KEY: {missing}")
    print(f"  catalog_coverage: {'OK' if not missing else 'FAIL'} ({len(render.FA_TO_KEY)} aliases)")
    return not missing


def test_catalog_asset_urls():
    """هر کد باید آدرس آیکون داشته باشه (یا None برای طلا)."""
    fails = 0
    for k in list(catalog.FIAT) + list(catalog.CRYPTO) + list(catalog.GOLD) + list(catalog.STABLE):
        try:
            url, kind, key = catalog.asset_urls(k)
            if k in catalog.FIAT and (not url or kind != "flag"):
                print(f"  ✗ fiat {k}: url={url} kind={kind}")
                fails += 1
            if k in catalog.CRYPTO and (not url or kind != "crypto"):
                print(f"  ✗ crypto {k}: url={url} kind={kind}")
                fails += 1
        except Exception as e:
            print(f"  ✗ asset_urls({k}): {e}")
            fails += 1
    print(f"  catalog_asset_urls: {fails == 0 and 'OK' or 'FAIL'}")
    return fails == 0


# ---------------- datafeeds (بدون شبکه) ----------------

def test_cache_ttl():
    """_cached باید TTL رو رعایت کنه — کش ۳ ثانیه."""
    import time
    import datafeeds
    calls = []
    def fn():
        calls.append(1)
        return "v"
    datafeeds._cache.clear()
    datafeeds._cached("test_key", fn)
    datafeeds._cached("test_key", fn)  # باید کش بشه
    n1 = len(calls)
    time.sleep(3.2)  # از TTL رد می‌شه
    datafeeds._cached("test_key", fn)
    n2 = len(calls)
    ok = (n1 == 1) and (n2 == 2)
    print(f"  cache_ttl: {'OK' if ok else 'FAIL'} (calls before={n1} after-ttl={n2})")
    datafeeds._cache.pop("test_key", None)
    return ok


# ---------------- admin ----------------

def test_admin_day_key():
    """_day_key باید با وقت ایران باشه (+۳:۳۰)."""
    import admin
    import time
    import datetime
    ir = datetime.timezone(datetime.timedelta(hours=3, minutes=30))
    exp = datetime.datetime.fromtimestamp(time.time(), ir).strftime("%Y-%m-%d")
    got = admin._day_key()
    ok = got == exp
    print(f"  admin_day_key: {'OK' if ok else 'FAIL'} ({got})")
    return ok


def test_admin_track_user():
    """ثبت کاربر باید شمارنده رو بالا ببره."""
    import admin
    admin._users.clear()
    admin._daily.clear()

    class FakeUser:
        id = 999999999
        is_bot = False
        first_name = "Test"
        last_name = None
        username = "tester"

    admin.track_user(FakeUser())
    admin.track_user(FakeUser())
    ok = admin._users.get("999999999", {}).get("count") == 2
    print(f"  admin_track_user: {'OK' if ok else 'FAIL'}")
    admin._users.clear()
    admin._daily.clear()
    return ok


# ---------------- runner ----------------

def main():
    tests = [
        ("parse_input", test_parse_input),
        ("parse_fa_digits", test_parse_fa_digits),
        ("parse_long_ignored", test_parse_long_ignored),
        ("fmt_num", test_fmt_num),
        ("catalog_resolve", test_catalog_resolve),
        ("catalog_coverage", test_catalog_coverage),
        ("catalog_asset_urls", test_catalog_asset_urls),
        ("cache_ttl", test_cache_ttl),
        ("admin_day_key", test_admin_day_key),
        ("admin_track_user", test_admin_track_user),
    ]
    passed = 0
    for name, fn in tests:
        print(f"[{name}]")
        try:
            if fn():
                passed += 1
        except Exception as e:
            import traceback
            print(f"  ✗ EXCEPTION: {e}")
            traceback.print_exc()
    print(f"\n{'='*40}\nResult: {passed}/{len(tests)} test suites passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
