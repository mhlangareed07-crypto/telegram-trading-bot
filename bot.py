# ============================================================
# LIQUIDITY PHANTOM BOT — ICT / TJR EDITION
# Strategy: Liquidity Sweep -> MSS -> Displacement -> FVG retrace
# Symbol: XAU/USD | Timeframe: 15min | All sessions, no filter
# ============================================================

import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta
from threading import Thread
from zoneinfo import ZoneInfo

import requests
import telebot
from flask import Flask
from telebot import types

# ============================================================
# CONFIG
# ============================================================
BOT_TOKEN = "8686284897:AAHntaD1FtwY8FOojnCFSEdApjXQSudHBPM"
TWELVE_DATA_API_KEY = "5288e6a3d6c64135bbab2c360bc40747"

SYMBOL = "XAU/USD"
DEFAULT_INTERVAL = "15min"
CANDLE_COUNT = 300          # ~3 days of 15m candles

# Displacement thresholds (TJR-style)
DISPLACEMENT_ATR_MULT = 1.5
DISPLACEMENT_BODY_RATIO = 0.6

# Risk model
DEFAULT_RR_TARGET = 2.0

# Caching
CANDLE_CACHE_SECONDS = 45

# Swing detection
SWING_LOOKBACK = 2

# ICT validity window (candles after MSS)
ICT_VALIDITY_CANDLES = 100
ICT_SCAN_LOOKBACK = 150
ICT_FVG_SEARCH_PAD = 10

# Timezone
SAST = ZoneInfo("Africa/Johannesburg")

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
log = logging.getLogger(__name__)

# Telegram bot
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# ============================================================
# DATABASE
# ============================================================
DB_PATH = "signals.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT,
            symbol TEXT,
            timeframe TEXT,
            session TEXT,
            direction TEXT,
            entry REAL,
            stop REAL,
            tp1 REAL,
            tp2 REAL,
            raid TEXT,
            mss TEXT,
            fvg TEXT,
            order_block TEXT,
            displacement TEXT,
            outcome TEXT
        )
    """)
    conn.commit()
    conn.close()


def log_signal(record):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO signals
        (ts, symbol, timeframe, session, direction, entry, stop, tp1, tp2,
         raid, mss, fvg, order_block, displacement, outcome)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)
    """, (
        record["ts"], record["symbol"], record["timeframe"], record["session"],
        record["direction"], record["entry"], record["stop"],
        record["tp1"], record["tp2"], record["raid"], record["mss"],
        record["fvg"], record["order_block"], record["displacement"],
    ))
    conn.commit()
    sid = cur.lastrowid
    conn.close()
    return sid


def tag_outcome(signal_id, outcome):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE signals SET outcome=? WHERE id=?", (outcome, signal_id))
    conn.commit()
    conn.close()


def get_stats():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT direction, outcome FROM signals WHERE outcome IS NOT NULL"
    ).fetchall()
    conn.close()
    total = len(rows)
    wins = sum(1 for _, o in rows if o == "WIN")
    losses = sum(1 for _, o in rows if o == "LOSS")
    be = sum(1 for _, o in rows if o == "BE")
    decided = wins + losses
    win_rate = (wins / decided * 100) if decided else None
    total_r = wins * DEFAULT_RR_TARGET - losses
    avg_r = (total_r / total) if total else None
    gains = wins * DEFAULT_RR_TARGET
    drawdown = losses
    pf = (gains / drawdown) if drawdown else (float("inf") if gains else None)
    return {
        "total": total, "wins": wins, "losses": losses, "be": be,
        "win_rate": win_rate, "avg_r": avg_r, "pf": pf,
    }


# ============================================================
# CANDLE FETCH — Twelve Data
# ============================================================
_candle_cache = {}


def fetch_candles(interval=DEFAULT_INTERVAL, outputsize=CANDLE_COUNT):
    key = (SYMBOL, interval, outputsize)
    now = time.time()
    cached = _candle_cache.get(key)
    if cached and (now - cached[0]) < CANDLE_CACHE_SECONDS:
        return cached[1], None

    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": SYMBOL,
        "interval": interval,
        "outputsize": outputsize,
        "timezone": "UTC",
        "apikey": TWELVE_DATA_API_KEY,
    }

    last_err = None
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=15)
            data = r.json()
            last_err = None
            break
        except requests.RequestException as e:
            last_err = str(e)
            time.sleep(1.5)

    if last_err is not None:
        return [], f"Network error: {last_err}"

    if isinstance(data, dict) and data.get("code") == 429:
        return [], "Twelve Data rate limit reached. Wait a moment."
    if "values" not in data:
        msg = data.get("message", "unknown error") if isinstance(data, dict) else "bad response"
        return [], f"Twelve Data error: {msg}"

    candles = []
    for c in data["values"]:
        try:
            candles.append({
                "dt": datetime.fromisoformat(c["datetime"]).replace(tzinfo=ZoneInfo("UTC")),
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
                "volume": float(c.get("volume") or 0),
            })
        except (KeyError, ValueError, TypeError):
            continue

    candles.sort(key=lambda x: x["dt"])
    if not candles:
        return [], "No valid candles returned."

    _candle_cache[key] = (now, candles)
    return candles, None


# ============================================================
# INDICATORS
# ============================================================
def average_true_range(candles, period=14):
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        trs.append(max(
            c["high"] - c["low"],
            abs(c["high"] - p["close"]),
            abs(c["low"] - p["close"]),
        ))
    return sum(trs[-period:]) / period


def find_swings(candles, lookback=SWING_LOOKBACK):
    """Fractal swing detection: a high is confirmed only if `lookback`
    candles on both sides have lower highs. Same logic inverted for lows."""
    swings = []
    if len(candles) < lookback * 2 + 1:
        return swings
    for i in range(lookback, len(candles) - lookback):
        c = candles[i]
        left = candles[i - lookback:i]
        right = candles[i + 1:i + lookback + 1]
        if all(w["high"] < c["high"] for w in left + right):
            swings.append({"idx": i, "type": "high", "price": c["high"], "dt": c["dt"]})
        if all(w["low"] > c["low"] for w in left + right):
            swings.append({"idx": i, "type": "low", "price": c["low"], "dt": c["dt"]})
    return swings


def label_structure(swings):
    last_high = last_low = None
    trend = None
    labeled = []
    for s in swings:
        if s["type"] == "high":
            if last_high is None:
                label = "H"
            else:
                label = "HH" if s["price"] > last_high else "LH"
            last_high = s["price"]
        else:
            if last_low is None:
                label = "L"
            else:
                label = "HL" if s["price"] > last_low else "LL"
            last_low = s["price"]
        labeled.append({**s, "label": label})
        if label in ("HH", "HL"):
            trend = "bullish"
        elif label in ("LH", "LL"):
            trend = "bearish"
    return labeled, trend, last_high, last_low


def find_equal_levels(swings, level_type, tolerance_pct=0.0008):
    prices = [s["price"] for s in swings if s["type"] == level_type]
    clusters = []
    used = [False] * len(prices)
    for i in range(len(prices)):
        if used[i]:
            continue
        cluster = [prices[i]]
        used[i] = True
        for j in range(i + 1, len(prices)):
            if used[j]:
                continue
            if abs(prices[j] - prices[i]) / prices[i] <= tolerance_pct:
                cluster.append(prices[j])
                used[j] = True
        if len(cluster) >= 2:
            clusters.append(sum(cluster) / len(cluster))
    return clusters


def previous_day_high_low(candles):
    by_day = {}
    for c in candles:
        day = c["dt"].astimezone(SAST).date()
        by_day.setdefault(day, []).append(c)
    days = sorted(by_day.keys())
    if len(days) < 2:
        return None, None
    prev = by_day[days[-2]]
    return max(c["high"] for c in prev), min(c["low"] for c in prev)


SESSION_DEFS = [
    ("Asian", "Asia/Tokyo", 9, 18),
    ("London", "Europe/London", 8, 17),
    ("New York", "America/New_York", 8, 17),
]


def previous_session_high_low(candles):
    if not candles:
        return None, None, None
    now_sast = candles[-1]["dt"].astimezone(SAST)
    windows = []
    for name, tzname, sh, eh in SESSION_DEFS:
        tz = ZoneInfo(tzname)
        for offset in (0, -1, -2):
            d = (now_sast + timedelta(days=offset)).astimezone(tz).date()
            start = datetime(d.year, d.month, d.day, sh, 0, tzinfo=tz)
            end = datetime(d.year, d.month, d.day, eh, 0, tzinfo=tz)
            windows.append((name, start.astimezone(SAST), end.astimezone(SAST)))
    completed = [w for w in windows if w[2] < now_sast]
    if not completed:
        return None, None, None
    completed.sort(key=lambda w: w[2], reverse=True)
    name, start, end = completed[0]
    in_sess = [c for c in candles if start <= c["dt"].astimezone(SAST) <= end]
    if not in_sess:
        return None, None, name
    return max(c["high"] for c in in_sess), min(c["low"] for c in in_sess), name


def active_sessions():
    now = datetime.now(SAST)
    active = []
    for name, tzname, sh, eh in SESSION_DEFS:
        tz = ZoneInfo(tzname)
        d = now.astimezone(tz).date()
        start = datetime(d.year, d.month, d.day, sh, 0, tzinfo=tz).astimezone(SAST)
        end = datetime(d.year, d.month, d.day, eh, 0, tzinfo=tz).astimezone(SAST)
        if start <= now <= end:
            active.append(name)
    return active


# ============================================================
# ICT ENGINE — Sweep -> MSS -> Displacement -> FVG -> Entry
# ============================================================
def build_liquidity_pools(candles, swings):
    pools = []
    for p in find_equal_levels(swings, "high"):
        pools.append({"type": "BSL", "price": p, "source": "equal highs"})
    for p in find_equal_levels(swings, "low"):
        pools.append({"type": "SSL", "price": p, "source": "equal lows"})
    pdh, pdl = previous_day_high_low(candles)
    if pdh is not None:
        pools.append({"type": "BSL", "price": pdh, "source": "PDH"})
    if pdl is not None:
        pools.append({"type": "SSL", "price": pdl, "source": "PDL"})
    psh, psl, sess = previous_session_high_low(candles)
    if psh is not None:
        pools.append({"type": "BSL", "price": psh, "source": f"prev {sess} high"})
    if psl is not None:
        pools.append({"type": "SSL", "price": psl, "source": f"prev {sess} low"})
    return pools


def find_raids(candles, pools, lookback=ICT_SCAN_LOOKBACK, confirm_within=3):
    """Scan the last `lookback` candles for a wick beyond any pool level,
    followed by a close back inside within `confirm_within` candles."""
    start = max(0, len(candles) - lookback)
    events = []
    seen = set()
    for pool in pools:
        level = pool["price"]
        direction = "high" if pool["type"] == "BSL" else "low"
        for i in range(start, len(candles)):
            c = candles[i]
            beyond = (c["high"] > level) if direction == "high" else (c["low"] < level)
            if not beyond:
                continue
            for j in range(i + 1, min(i + 1 + confirm_within, len(candles))):
                back = (candles[j]["close"] < level) if direction == "high" else (candles[j]["close"] > level)
                if back:
                    key = (pool["type"], round(level, 2), i)
                    if key not in seen:
                        seen.add(key)
                        events.append({
                            "raid_idx": i,
                            "confirm_idx": j,
                            "pool": pool,
                            "reversal_direction": "bullish" if pool["type"] == "SSL" else "bearish",
                        })
                    break
    events.sort(key=lambda e: e["confirm_idx"], reverse=True)
    return events


def find_mss(candles, raid_confirm_idx, direction, window=ICT_VALIDITY_CANDLES):
    end = min(raid_confirm_idx + window, len(candles) - 1)
    if raid_confirm_idx <= 0:
        return None
    pre = find_swings(candles[:raid_confirm_idx + 1])
    if direction == "bullish":
        highs = [s for s in pre if s["type"] == "high"]
        if not highs:
            return None
        level = highs[-1]["price"]
        for j in range(raid_confirm_idx + 1, end + 1):
            if candles[j]["close"] > level:
                return {"idx": j, "level": level}
    else:
        lows = [s for s in pre if s["type"] == "low"]
        if not lows:
            return None
        level = lows[-1]["price"]
        for j in range(raid_confirm_idx + 1, end + 1):
            if candles[j]["close"] < level:
                return {"idx": j, "level": level}
    return None


def find_displacement(candles, start_idx, end_idx, direction):
    if end_idx <= start_idx:
        return None
    atr = average_true_range(candles[max(0, start_idx - 14):end_idx + 1]) or 0
    if atr == 0:
        return None
    for j in range(start_idx, end_idx + 1):
        c = candles[j]
        body = abs(c["close"] - c["open"])
        rng = c["high"] - c["low"]
        ratio = (body / rng) if rng else 0
        cdir = "bullish" if c["close"] > c["open"] else "bearish"
        if cdir == direction and body > DISPLACEMENT_ATR_MULT * atr and ratio > DISPLACEMENT_BODY_RATIO:
            return {"idx": j, "body": body, "atr": atr, "ratio": round(ratio, 2)}
    return None


def detect_fvgs(candles):
    """3-candle Fair Value Gap: candle[i-1].high < candle[i+1].low (bullish) or
    candle[i-1].low > candle[i+1].high (bearish)."""
    fvgs = []
    for i in range(1, len(candles) - 1):
        c1, c3 = candles[i - 1], candles[i + 1]
        if c1["high"] < c3["low"]:
            fvgs.append({"type": "bullish", "top": c3["low"], "bottom": c1["high"], "i": i, "filled": False})
        elif c1["low"] > c3["high"]:
            fvgs.append({"type": "bearish", "top": c1["low"], "bottom": c3["high"], "i": i, "filled": False})
    for g in fvgs:
        for c in candles[g["i"] + 2:]:
            if g["bottom"] <= c["low"] <= g["top"] or g["bottom"] <= c["high"] <= g["top"]:
                g["filled"] = True
                break
    return fvgs


def find_fvg_near(candles, mss_idx, direction):
    start = max(0, mss_idx - ICT_FVG_SEARCH_PAD)
    end = min(mss_idx + ICT_FVG_SEARCH_PAD, len(candles) - 1)
    window = candles[start:end + 1]
    if len(window) < 3:
        return None
    fvgs = detect_fvgs(window)
    matching = [f for f in fvgs if f["type"] == direction]
    if not matching:
        return None
    best = min(matching, key=lambda f: abs((f["i"] + start) - mss_idx))
    best = dict(best)
    best["i"] += start
    return best


def find_order_block(candles, raid_idx, mss_idx, direction):
    for i in range(mss_idx - 1, max(raid_idx - 1, -1), -1):
        c = candles[i]
        if direction == "bullish" and c["close"] < c["open"]:
            return {"type": "bullish OB", "high": c["high"], "low": c["low"], "idx": i}
        if direction == "bearish" and c["close"] > c["open"]:
            return {"type": "bearish OB", "high": c["high"], "low": c["low"], "idx": i}
    return None


def check_invalidated(candles, raid, mss, direction):
    """Setup invalid if price closes back beyond the raid level after the MSS."""
    raid_level = raid["pool"]["price"]
    for j in range(mss["idx"] + 1, len(candles)):
        c = candles[j]
        if direction == "bullish" and c["close"] < raid_level:
            return True
        if direction == "bearish" and c["close"] > raid_level:
            return True
    return False


def price_in_zone(candles, low, high):
    last = candles[-1]
    return last["low"] <= high and last["high"] >= low


def build_ict_setup(candles):
    if not candles or len(candles) < 30:
        return {"state": "NONE", "reason": "Not enough candle data."}

    swings = find_swings(candles)
    pools = build_liquidity_pools(candles, swings)
    raids = find_raids(candles, pools)

    if not raids:
        return {"state": "NONE", "reason": "No liquidity raid in recent lookback."}

    for raid in raids:
        direction = raid["reversal_direction"]
        mss = find_mss(candles, raid["confirm_idx"], direction)
        if not mss:
            continue
        displacement = find_displacement(candles, raid["confirm_idx"] + 1, mss["idx"], direction)
        if not displacement:
            continue
        fvg = find_fvg_near(candles, mss["idx"], direction)
        if not fvg:
            continue
        if check_invalidated(candles, raid, mss, direction):
            continue

        ob = find_order_block(candles, raid["raid_idx"], mss["idx"], direction)
        zone_low, zone_high = fvg["bottom"], fvg["top"]
        in_zone = price_in_zone(candles, zone_low, zone_high)
        candles_since = (len(candles) - 1) - mss["idx"]
        valid_window = candles_since <= ICT_VALIDITY_CANDLES

        if in_zone and valid_window:
            state = "ENTRY"
        elif valid_window:
            state = "DEVELOPING"
        else:
            continue

        return {
            "state": state,
            "direction": direction,
            "pool": raid["pool"],
            "raid_idx": raid["raid_idx"],
            "mss": mss,
            "displacement": displacement,
            "fvg": fvg,
            "order_block": ob,
            "candles_since_mss": candles_since,
        }

    return {"state": "NONE", "reason": "No raid led to MSS + displacement + FVG in the valid window."}


# ============================================================
# RENDERERS
# ============================================================
def fmt_price(p):
    return f"{p:.2f}" if p is not None else "n/a"


def render_structure(candles, swings):
    labeled, trend, lh, ll = label_structure(swings)
    lines = ["📊 <b>Market Structure (15min)</b>"]
    lines.append(f"Trend: <b>{(trend or 'Undetermined').upper()}</b>")
    lines.append(f"Last swing high: {fmt_price(lh)}")
    lines.append(f"Last swing low: {fmt_price(ll)}")
    recent = " → ".join(s["label"] for s in labeled[-6:]) or "n/a"
    lines.append(f"Recent labels: {recent}")
    return "\n".join(lines)


def render_liquidity(candles, swings):
    pools = build_liquidity_pools(candles, swings)
    bsl = [p for p in pools if p["type"] == "BSL"]
    ssl = [p for p in pools if p["type"] == "SSL"]
    pdh, pdl = previous_day_high_low(candles)
    lines = ["💧 <b>Liquidity Pools</b>"]
    lines.append(f"BSL pools (buy-side): {len(bsl)}")
    lines.append(f"SSL pools (sell-side): {len(ssl)}")
    lines.append(f"Previous day high: {fmt_price(pdh)}")
    lines.append(f"Previous day low: {fmt_price(pdl)}")
    if bsl:
        lines.append(f"Nearest BSL: {fmt_price(min(p['price'] for p in bsl))}")
    if ssl:
        lines.append(f"Nearest SSL: {fmt_price(max(p['price'] for p in ssl))}")
    return "\n".join(lines)


def render_sessions():
    active = active_sessions()
    now = datetime.now(SAST).strftime("%H:%M")
    lines = [f"🕐 <b>Sessions</b> (now {now} SAST)"]
    for name, _, sh, eh in SESSION_DEFS:
        mark = "🟢" if name in active else "⚫"
        lines.append(f"{mark} {name}")
    return "\n".join(lines)


def render_setup(candles):
    setup = build_ict_setup(candles)
    if setup["state"] == "NONE":
        return (
            "🎯 <b>SIGNAL — XAUUSD 15min</b>\n"
            "⚪ <b>NO CLEAR SETUP</b>\n"
            f"{setup.get('reason', '')}\n\n"
            "<i>Waiting for: liquidity raid → MSS → displacement → FVG retrace.</i>"
        )

    d = setup["direction"]
    icon = "🟢" if d == "bullish" else "🔴"
    pool = setup["pool"]
    mss = setup["mss"]
    disp = setup["displacement"]
    fvg = setup["fvg"]
    ob = setup["order_block"]

    lines = [
        "🎯 <b>SIGNAL — XAUUSD 15min</b>",
        f"{icon} <b>{'BUY' if d == 'bullish' else 'SELL'} — {setup['state']}</b>",
        "",
        "<b>ICT Sequence:</b>",
        f"1. {pool['type']} raid @ {fmt_price(pool['price'])} ({pool['source']})",
        f"2. MSS {d} — broke {fmt_price(mss['level'])}",
        f"3. Displacement: body {disp['body']:.2f} vs ATR {disp['atr']:.2f} (ratio {disp['ratio']})",
        f"4. FVG {d}: {fmt_price(fvg['bottom'])} → {fmt_price(fvg['top'])}",
    ]
    if ob:
        lines.append(f"5. Order block: {ob['type']} @ {fmt_price(ob['low'])}–{fmt_price(ob['high'])}")
    else:
        lines.append("5. Order block: none identified")
    lines.append("")

    if setup["state"] == "ENTRY":
        entry = fvg["top"] if d == "bullish" else fvg["bottom"]
        if d == "bullish":
            stop = (min(fvg["bottom"], ob["low"]) if ob else fvg["bottom"]) - 0.5
        else:
            stop = (max(fvg["top"], ob["high"]) if ob else fvg["top"]) + 0.5
        risk = abs(entry - stop)
        tp1 = entry + risk if d == "bullish" else entry - risk
        tp2 = entry + risk * DEFAULT_RR_TARGET if d == "bullish" else entry - risk * DEFAULT_RR_TARGET
        lines += [
            "<b>Levels:</b>",
            f"Entry: {fmt_price(entry)}",
            f"Stop:  {fmt_price(stop)}",
            f"TP1:   {fmt_price(tp1)} (1R)",
            f"TP2:   {fmt_price(tp2)} ({DEFAULT_RR_TARGET:g}R)",
        ]
    else:
        lines.append(
            f"⏳ Price has not retraced into the FVG yet.\n"
            f"Watch zone: {fmt_price(fvg['bottom'])} → {fmt_price(fvg['top'])}"
        )

    lines.append(f"\nCandles since MSS: {setup['candles_since_mss']} / {ICT_VALIDITY_CANDLES}")
    lines.append("<i>Analysis only. You enter manually.</i>")
    return "\n".join(lines)


def render_stats():
    s = get_stats()
    lines = ["📈 <b>Signal Stats</b>"]
    if s["total"] == 0:
        lines.append("No tagged outcomes yet. Use the ✅ / ❌ / ➖ buttons under a signal.")
    else:
        lines.append(f"Tagged: {s['total']} (W:{s['wins']} L:{s['losses']} BE:{s['be']})")
        if s["win_rate"] is not None:
            lines.append(f"Win rate: {s['win_rate']:.1f}%")
        if s["avg_r"] is not None:
            lines.append(f"Avg R: {s['avg_r']:.2f}")
        if s["pf"] is not None:
            pf = "∞" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
            lines.append(f"Profit factor: {pf}")
    return "\n".join(lines)


def render_full(candles):
    swings = find_swings(candles)
    return "\n\n".join([
        render_structure(candles, swings),
        render_liquidity(candles, swings),
        render_sessions(),
        render_setup(candles),
        render_stats(),
    ])


# ============================================================
# BOT MENUS & HANDLERS
# ============================================================
def main_menu():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🎯 SIGNAL", callback_data="menu:signal"),
        types.InlineKeyboardButton("📊 STRUCTURE", callback_data="menu:structure"),
    )
    kb.add(
        types.InlineKeyboardButton("💧 LIQUIDITY", callback_data="menu:liquidity"),
        types.InlineKeyboardButton("🕐 SESSIONS", callback_data="menu:sessions"),
    )
    kb.add(
        types.InlineKeyboardButton("📈 STATS", callback_data="menu:stats"),
        types.InlineKeyboardButton("🔍 FULL", callback_data="menu:full"),
    )
    kb.add(types.InlineKeyboardButton("🔄 REFRESH", callback_data="menu:refresh"))
    return kb


def outcome_kb(sid):
    kb = types.InlineKeyboardMarkup(row_width=3)
    kb.add(
        types.InlineKeyboardButton("✅ WIN", callback_data=f"tag:{sid}:WIN"),
        types.InlineKeyboardButton("❌ LOSS", callback_data=f"tag:{sid}:LOSS"),
        types.InlineKeyboardButton("➖ BE", callback_data=f"tag:{sid}:BE"),
    )
    return kb


@bot.message_handler(commands=["start", "help"])
def cmd_start(message):
    bot.reply_to(
        message,
        "<b>Liquidity Phantom Bot — ICT Edition</b>\n"
        "XAU/USD • 15min • Sweep → MSS → FVG\n\n"
        "Commands:\n"
        "/analyze — Interactive menu\n"
        "/signal — Current ICT setup\n"
        "/debug — Raw engine diagnostics\n"
        "/stats — Historical performance\n"
        "/size &lt;account&gt; &lt;risk%&gt; &lt;entry&gt; &lt;stop&gt;\n"
        "/help — This message\n\n"
        "<i>Analysis only. No trades are placed.</i>",
    )


@bot.message_handler(commands=["analyze"])
def cmd_analyze(message):
    bot.send_message(message.chat.id, "Select a section:", reply_markup=main_menu())


@bot.message_handler(commands=["signal"])
def cmd_signal(message):
    candles, err = fetch_candles()
    if err:
        bot.reply_to(message, f"⚠️ {err}")
        return
    setup = build_ict_setup(candles)
    text = render_setup(candles)
    if setup["state"] == "ENTRY":
        d = setup["direction"]
        fvg = setup["fvg"]
        ob = setup["order_block"]
        entry = fvg["top"] if d == "bullish" else fvg["bottom"]
        if d == "bullish":
            stop = (min(fvg["bottom"], ob["low"]) if ob else fvg["bottom"]) - 0.5
        else:
            stop = (max(fvg["top"], ob["high"]) if ob else fvg["top"]) + 0.5
        risk = abs(entry - stop)
        tp1 = entry + risk if d == "bullish" else entry - risk
        tp2 = entry + risk * DEFAULT_RR_TARGET if d == "bullish" else entry - risk * DEFAULT_RR_TARGET
        rec = {
            "ts": datetime.now(SAST).isoformat(),
            "symbol": SYMBOL,
            "timeframe": DEFAULT_INTERVAL,
            "session": ",".join(active_sessions()) or "none",
            "direction": d,
            "entry": entry, "stop": stop, "tp1": tp1, "tp2": tp2,
            "raid": str(setup["pool"]),
            "mss": str(setup["mss"]),
            "fvg": str(setup["fvg"]),
            "order_block": str(setup["order_block"]),
            "displacement": str(setup["displacement"]),
        }
        sid = log_signal(rec)
        bot.send_message(message.chat.id, text, reply_markup=outcome_kb(sid))
        bot.send_message(message.chat.id, f"Signal #{sid} logged. Tag the outcome when it plays out.")
    else:
        bot.send_message(message.chat.id, text)


@bot.message_handler(commands=["debug"])
def cmd_debug(message):
    candles, err = fetch_candles()
    if err:
        bot.reply_to(message, f"⚠️ {err}")
        return
    swings = find_swings(candles)
    pools = build_liquidity_pools(candles, swings)
    raids = find_raids(candles, pools)
    setup = build_ict_setup(candles)
    lines = [
        "<b>DEBUG</b>",
        f"Candles: {len(candles)}",
        f"Swings: {len(swings)}",
        f"Pools: {len(pools)}",
        f"Raids: {len(raids)}",
        f"Last close: {candles[-1]['close']}",
        f"Last candle: {candles[-1]['dt'].astimezone(SAST).strftime('%Y-%m-%d %H:%M SAST')}",
        f"Setup state: {setup['state']}",
        f"Reason: {setup.get('reason', '-')}",
    ]
    bot.reply_to(message, "\n".join(lines))


@bot.message_handler(commands=["stats"])
def cmd_stats(message):
    bot.reply_to(message, render_stats())


@bot.message_handler(commands=["size"])
def cmd_size(message):
    parts = message.text.split()[1:]
    if len(parts) != 4:
        bot.reply_to(message, "Usage: /size &lt;account&gt; &lt;risk%&gt; &lt;entry&gt; &lt;stop&gt;")
        return
    try:
        account, risk_pct, entry, stop = (float(x) for x in parts)
    except ValueError:
        bot.reply_to(message, "All values must be numbers.")
        return
    risk_amount = account * (risk_pct / 100)
    distance = abs(entry - stop)
    if distance == 0:
        bot.reply_to(message, "Entry and stop cannot be equal.")
        return
    units = risk_amount / distance
    bot.reply_to(
        message,
        "<b>Position Size (informational)</b>\n"
        f"Risk amount: {risk_amount:.2f}\n"
        f"Stop distance: {distance:.2f}\n"
        f"Theoretical size: {units:.4f} price units\n\n"
        "<i>Check your broker's contract specs for actual lot size.</i>",
    )


@bot.message_handler(func=lambda m: True)
def fallback(message):
    bot.reply_to(message, "Unknown command. Use /help.")


@bot.callback_query_handler(func=lambda c: c.data.startswith("menu:"))
def handle_menu(call):
    key = call.data.split(":", 1)[1]
    bot.answer_callback_query(call.id)
    candles, err = fetch_candles()
    if err:
        bot.edit_message_text(
            f"⚠️ {err}",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=main_menu(),
        )
        return
    if key in ("full", "refresh"):
        text = render_full(candles)
    elif key == "signal":
        text = render_setup(candles)
    elif key == "structure":
        text = render_structure(candles, find_swings(candles))
    elif key == "liquidity":
        text = render_liquidity(candles, find_swings(candles))
    elif key == "sessions":
        text = render_sessions()
    elif key == "stats":
        text = render_stats()
    else:
        text = render_full(candles)
    bot.edit_message_text(
        text,
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=main_menu(),
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("tag:"))
def handle_tag(call):
    _, sid, outcome = call.data.split(":")
    tag_outcome(int(sid), outcome)
    bot.answer_callback_query(call.id, text=f"Signal #{sid} → {outcome}")
    bot.edit_message_text(
        f"Signal #{sid} tagged: <b>{outcome}</b>",
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
    )


# ============================================================
# RENDER KEEP-ALIVE
# ============================================================
app = Flask(__name__)


@app.route("/")
def home():
    return "Liquidity Phantom Bot is running."


def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)


def keep_alive():
    t = Thread(target=run_web, daemon=True)
    t.start()


# ============================================================
# MAIN
# ============================================================
def main():
    init_db()
    keep_alive()
    log.info("Liquidity Phantom Bot (ICT Edition) starting...")
    while True:
        try:
            bot.infinity_polling(skip_pending=True, timeout=20, long_polling_timeout=20)
        except Exception as e:
            log.error(f"Polling error: {e}. Retrying in 5s...")
            time.sleep(5)


if __name__ == "__main__":
    main()

