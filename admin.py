"""
AuroraPriceBot — پنل ادمین (فقط OWNER)
آمار کاربران/گروه‌ها، broadcast، health، تنظیمات.
ذخیره‌سازی: JSON فایل (سبک، بدون دیتابیس سنگین) با lock.
"""
import os
import json
import time
import datetime
import threading
import logging

log = logging.getLogger(__name__)

OWNER_ID = int(os.getenv("OWNER_ID", "1776112463"))
DATA_DIR = "hist"
USERS_FILE = os.path.join(DATA_DIR, "_stats_users.json")
GROUPS_FILE = os.path.join(DATA_DIR, "_stats_groups.json")
DAILY_FILE = os.path.join(DATA_DIR, "_stats_daily.json")

_lock = threading.RLock()  # RLock — track_user خودش lock رو گرفته و _save_debounced هم دوباره می‌گیره
_users: dict = {}
_groups: dict = {}
_daily: dict = {}

# debounce ذخیره: به‌جای یک بار نوشتن فایل به ازای هر پیام، حداکثر هر ۳۰ ثانیه یک بار
SAVE_DEBOUNCE_S = 30.0
_dirty = False
_last_save_ts = 0.0
_save_timer: threading.Timer | None = None

# ---------- load/save ----------
def _load():
    global _users, _groups, _daily
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        if os.path.exists(USERS_FILE):
            with open(USERS_FILE) as f:
                _users = json.load(f)
    except Exception:
        _users = {}
    try:
        if os.path.exists(GROUPS_FILE):
            with open(GROUPS_FILE) as f:
                _groups = json.load(f)
    except Exception:
        _groups = {}
    try:
        if os.path.exists(DAILY_FILE):
            with open(DAILY_FILE) as f:
                _daily = json.load(f)
    except Exception:
        _daily = {}

def _save():
    """ذخیره‌ی اتمیک: اول تو فایل tmp نوشته می‌شه، بعد با os.replace جاشگزین می‌شه —
    فایل روی دیسک همیشه معتبره (حتی اگه وسط نوشتن کرش/kill بشه) و هندل همیشه بسته می‌شه."""
    global _last_save_ts
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        for path, data in ((USERS_FILE, _users), (GROUPS_FILE, _groups), (DAILY_FILE, _daily)):
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(path + ".tmp", path)
        _last_save_ts = time.time()
    except Exception as e:
        log.warning("admin save: %s", e)

def _save_debounced():
    """اگه از آخرین ذخیره کمتر از ۳۰ ثانیه گذشته، فقط dirty می‌شه و یک Timer تک‌شلیک
    (daemon=True — مانع shutdown نمی‌شه) ذخیره رو زمان‌بندی می‌کنه؛ وگرنه همین حالا ذخیره."""
    global _dirty, _save_timer
    with _lock:
        now = time.time()
        if now - _last_save_ts >= SAVE_DEBOUNCE_S:
            _dirty = False
            _save()
            return
        _dirty = True
        if _save_timer is None:
            _save_timer = threading.Timer(SAVE_DEBOUNCE_S, _flush_if_dirty)
            _save_timer.daemon = True
            _save_timer.start()

def _flush_if_dirty():
    """Timer رسیده — اگه چیزی ذخیره‌نشده مونده، ذخیره کن."""
    global _dirty, _save_timer
    with _lock:
        _save_timer = None
        if _dirty:
            _dirty = False
            _save()

def _day_key(ts=None) -> str:
    ir = datetime.timezone(datetime.timedelta(hours=3, minutes=30))
    return datetime.datetime.fromtimestamp(ts or time.time(), ir).strftime("%Y-%m-%d")

_load()

# ---------- tracking (از همه‌ی پیام‌ها صدا زده می‌شه) ----------
def track_user(user) -> dict:
    """ثبت/آپدیت کاربر — با یک entry خلاصه: id, name, username, first/last seen, count."""
    if user is None or user.is_bot:
        return {}
    uid = str(user.id)
    now = time.time()
    day = _day_key(now)
    with _lock:
        u = _users.get(uid) or {
            "name": "", "username": "", "first": now, "count": 0
        }
        u["name"] = (user.first_name or "") + (" " + user.last_name if user.last_name else "")
        u["username"] = user.username or ""
        u["last"] = now
        u["count"] = u.get("count", 0) + 1
        _users[uid] = u
        # daily counters
        d = _daily.setdefault(day, {"msgs": 0, "users": 0})
        prev_seen_day = u.get("day")
        if prev_seen_day != day:
            d["users"] += 1
            u["day"] = day
        d["msgs"] += 1
        # نگه‌داشتن ۳۰ روز آخر
        if len(_daily) > 35:
            for k in sorted(_daily)[:-30]:
                _daily.pop(k, None)
        _save_debounced()
    return u

def track_group(chat) -> None:
    """ثبت گروه/سوپرگروه که ربات توش فعال شده."""
    if chat is None or chat.type not in ("group", "supergroup"):
        return
    cid = str(chat.id)
    now = time.time()
    with _lock:
        g = _groups.get(cid) or {"title": "", "first": now, "count": 0}
        g["title"] = chat.title or ""
        g["last"] = now
        g["count"] = g.get("count", 0) + 1
        _groups[cid] = g
        _save_debounced()

# ---------- آمار ----------
def overview() -> dict:
    with _lock:
        total_users = len(_users)
        total_groups = len(_groups)
        today = _day_key()
        d = _daily.get(today, {})
        # کاربران فعال ۲۴س/۷روز
        now = time.time()
        act24 = sum(1 for u in _users.values() if now - u.get("last", 0) < 86400)
        act7 = sum(1 for u in _users.values() if now - u.get("last", 0) < 7 * 86400)
        # مجموع پیام‌ها
        total_msgs = sum(u.get("count", 0) for u in _users.values())
        # برترین‌ها
        top = sorted(_users.items(), key=lambda kv: -kv[1].get("count", 0))[:5]
        # دیروز برای مقایسه
        ir = datetime.timezone(datetime.timedelta(hours=3, minutes=30))
        yday = datetime.datetime.fromtimestamp(time.time() - 86400, ir).strftime("%Y-%m-%d")
        d_prev = _daily.get(yday, {})
        return {
            "users": total_users,
            "groups": total_groups,
            "act24": act24,
            "act7": act7,
            "today_msgs": d.get("msgs", 0),
            "today_users": d.get("users", 0),
            "yday_msgs": d_prev.get("msgs", 0),
            "total_msgs": total_msgs,
            "top": [(uid, u.get("name", "?"), u.get("count", 0)) for uid, u in top],
            "daily": {k: v.get("msgs", 0) for k, v in sorted(_daily.items())[-7:]},
        }

def users_list(page: int = 0, per: int = 10):
    with _lock:
        items = sorted(_users.items(), key=lambda kv: -kv[1].get("last", 0))
    total = len(items)
    start = page * per
    chunk = items[start:start + per]
    return chunk, total, (start + per < total)

def groups_list():
    with _lock:
        return sorted(_groups.items(), key=lambda kv: -kv[1].get("count", 0))

# ---------- broadcast ----------
def all_user_ids() -> list:
    with _lock:
        return list(_users.keys())

def all_group_ids() -> list:
    with _lock:
        return list(_groups.keys())
