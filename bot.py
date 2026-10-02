"""
Liquidity Phantom Bot
======================
A Telegram MARKET ANALYSIS bot for XAU/USD (gold).

IMPORTANT — READ THIS:
This bot is NOT an autonomous trading agent. It does not connect to any
broker, does not place, modify, or close trades, and does not manage
positions. It only reads market data (Twelve Data) and returns analysis
and signal *suggestions* in Telegram. The user always makes the final
decision and enters trades manually on their own broker platform.
"""

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

# --- Render Web Server for 24/7 Keep-Alive ---
app = Flask('')

@app.route('/')
def home():
    return "Liquidity Phantom Bot is running 24/7!"

def run():
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run)
    t.daemon = True
    t.start()

# --- Configuration & Environment Variables ---
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8686284897:AAHNtaD1FtwY8FOojnCFSEdApjXQSudHBPM")
TWELVE_DATA_API_KEY = "5288e6a3d6c64135bbab2c360bc40747"

SYMBOL = "XAU/USD"
DEFAULT_INTERVAL = "15min"
CANDLE_COUNT = 150

DISPLACEMENT_ATR_MULT = 1.5
DISPLACEMENT_BODY_RATIO = 0.6

CONFLUENCE_THRESHOLD = 0.5

DEFAULT_RR_TARGET = 2.0

CANDLE_CACHE_SECONDS = 45

SAST = ZoneInfo("Africa/Johannesburg")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

DB_PATH = "signals.db"


def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
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
            bos_choch TEXT,
            liquidity_sweep TEXT,
            fvg TEXT,
            order_block TEXT,
            displacement TEXT,
            premium_discount TEXT,
            macro_note TEXT,
            confluence_score TEXT,
            outcome TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def log_signal(record: dict) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        """
        INSERT INTO signals
        (ts, symbol, timeframe, session, direction, entry, stop, tp1, tp2,
         bos_choch, liquidity_sweep, fvg, order_block, displacement,
         premium_discount, macro_note, confluence_score, outcome)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)
        """,
        (
            record["ts"], record["symbol"], record["timeframe"], record["session"],
            record["direction"], record["entry"], record["stop"], record["tp1"], record["tp2"],
            record["bos_choch"], record["liquidity_sweep"], record["fvg"], record["order_block"],
            record["displacement"], record["premium_discount"], record["macro_note"],
            record["confluence_score"],
        ),
    )
    conn.commit()
    signal_id = cur.lastrowid
    conn.close()
    return signal_id


def tag_outcome(signal_id: int, outcome: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE signals SET outcome = ? WHERE id = ?", (outcome, signal_id))
    conn.commit()
    conn.close()


def get_stats() -> dict:
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

    total_r = wins * DEFAULT_RR_TARGET - losses * 1.0
    avg_r = (total_r / total) if total else None

    gains = wins * DEFAULT_RR_TARGET
    drawdown = losses * 1.0
    profit_factor = (gains / drawdown) if drawdown else (float("inf") if gains else None)

    conn = sqlite3.connect(DB_PATH)
    ordered = conn.execute(
        "SELECT outcome FROM signals WHERE outcome IS NOT NULL ORDER BY id ASC"
    ).fetchall()
    conn.close()
    max_win_streak = max_loss_streak = cur_win = cur_loss = 0
    for (o,) in ordered:
        if o == "WIN":
            cur_win += 1
            cur_loss = 0
        elif o == "LOSS":
            cur_loss += 1
            cur_win = 0
        else:
            cur_win = cur_loss = 0
        max_win_streak = max(max_win_streak, cur_win)
        max_loss_streak = max(max_loss_streak, cur_loss)

    return {
        "total_tagged": total,
        "wins": wins,
        "losses": losses,
        "be": be,
        "win_rate": win_rate,
        "avg_r": avg_r,
        "profit_factor": profit_factor,
        "max_win_streak": max_win_streak,
        "max_loss_streak": max_loss_streak,
    }


_candle_cache: dict = {}


def fetch_candles(interval: str = DEFAULT_INTERVAL, outputsize: int = CANDLE_COUNT):
    cache_key = (SYMBOL, interval, outputsize)
    now = time.time()
    cached = _candle_cache.get(cache_key)
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
    try:
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()
    except requests.RequestException as exc:
        return [], f"Network error contacting Twelve Data: {exc}"

    if isinstance(data, dict) and data.get("code") == 429:
        return [], "Twelve Data rate limit reached. Try again shortly."
    if "values" not in data:
        return [], f"Twelve Data error: {data.get('message', 'unknown error')}"

    raw = data["values"]
    candles = []
    for c in raw:
        try:
            dt = datetime.strptime(c["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("UTC"))
        except ValueError:
            dt = datetime.strptime(c["datetime"], "%Y-%m-%d").replace(tzinfo=ZoneInfo("UTC"))
        candles.append(
            {
                "dt": dt,
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
                "volume": float(c["volume"]) if c.get("volume") not in (None, "") else None,
            }
        )
    candles.sort(key=lambda c: c["dt"])

    if len(candles) < 10:
        return [], "Not enough candle data returned to run analysis."

    _candle_cache[cache_key] = (now, candles)
    return candles, None


def find_swings(candles, left=2, right=2):
    swings = []
    n = len(candles)
    for i in range(left, n - right):
        window_highs = [candles[j]["high"] for j in range(i - left, i + right + 1)]
        window_lows = [candles[j]["low"] for j in range(i - left, i + right + 1)]
        if candles[i]["high"] == max(window_highs) and window_highs.count(candles[i]["high"]) == 1:
            swings.append({"i": i, "price": candles[i]["high"], "type": "high"})
        if candles[i]["low"] == min(window_lows) and window_lows.count(candles[i]["low"]) == 1:
            swings.append({"i": i, "price": candles[i]["low"], "type": "low"})
    swings.sort(key=lambda s: s["i"])
    return swings


def label_structure(swings):
    last_high = None
    last_low = None
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

        if label == "HH" or label == "HL":
            trend = "bullish"
        elif label == "LH" or label == "LL":
            trend = "bearish"

    return labeled, trend, last_high, last_low


def detect_bos_choch(candles, labeled_swings, trend, last_high, last_low):
    if not candles or trend is None or last_high is None or last_low is None:
        return {"event": None, "direction": None, "level": None}

    last_close = candles[-1]["close"]

    if trend == "bullish":
        if last_close > last_high:
            return {"event": "BOS", "direction": "bullish", "level": last_high}
        if last_close < last_low:
            return {"event": "CHoCH", "direction": "bearish", "level": last_low}
    elif trend == "bearish":
        if last_close < last_low:
            return {"event": "BOS", "direction": "bearish", "level": last_low}
        if last_close > last_high:
            return {"event": "CHoCH", "direction": "bullish", "level": last_high}

    return {"event": None, "direction": None, "level": None}


def market_structure_report(candles):
    swings = find_swings(candles)
    labeled, trend, last_high, last_low = label_structure(swings)
    bos_choch = detect_bos_choch(candles, labeled, trend, last_high, last_low)
    return {
        "swings": labeled,
        "trend": trend,
        "last_swing_high": last_high,
        "last_swing_low": last_low,
        "bos_choch": bos_choch,
        "confidence": "HIGH",
    }


def find_equal_levels(swings, level_type, tolerance_pct=0.0008):
    prices = [s["price"] for s in swings if s["type"] == level_type]
    clusters = []
    used = [False] * len(prices)
    for i, p in enumerate(prices):
        if used[i]:
            continue
        cluster = [p]
        used[i] = True
        for j in range(i + 1, len(prices)):
            if used[j]:
                continue
            if abs(prices[j] - p) / p <= tolerance_pct:
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
    prev_day_candles = by_day[days[-2]]
    return max(c["high"] for c in prev_day_candles), min(c["low"] for c in prev_day_candles)


def detect_sweep(candles, level, direction, lookback=5, confirm_within=3):
    if level is None:
        return False
    recent = candles[-lookback:]
    for idx, c in enumerate(recent):
        beyond = (c["high"] > level) if direction == "high" else (c["low"] < level)
        if not beyond:
            continue
        for confirm_c in recent[idx + 1: idx + 1 + confirm_within]:
            back_inside = (confirm_c["close"] < level) if direction == "high" else (confirm_c["close"] > level)
            if back_inside:
                return True
    return False


def liquidity_report(candles, swings):
    equal_highs = find_equal_levels(swings, "high")
    equal_lows = find_equal_levels(swings, "low")
    pdh, pdl = previous_day_high_low(candles)

    sweep_high = detect_sweep(candles, max(equal_highs) if equal_highs else pdh, "high")
    sweep_low = detect_sweep(candles, min(equal_lows) if equal_lows else pdl, "low")

    return {
        "equal_highs": equal_highs,
        "equal_lows": equal_lows,
        "prev_day_high": pdh,
        "prev_day_low": pdl,
        "sweep_high_detected": sweep_high,
        "sweep_low_detected": sweep_low,
        "confidence": "HIGH",
    }


def detect_fvgs(candles):
    fvgs = []
    for i in range(1, len(candles) - 1):
        c1, c3 = candles[i - 1], candles[i + 1]
        if c1["high"] < c3["low"]:
            fvgs.append({"type": "bullish", "top": c3["low"], "bottom": c1["high"], "i": i})
        elif c1["low"] > c3["high"]:
            fvgs.append({"type": "bearish", "top": c1["low"], "bottom": c3["high"], "i": i})

    for gap in fvgs:
        filled = False
        for c in candles[gap["i"] + 1:]:
            if gap["bottom"] <= c["low"] <= gap["top"] or gap["bottom"] <= c["high"] <= gap["top"]:
                filled = True
                break
        gap["filled"] = filled
    return fvgs


def find_order_blocks(candles, bos_choch):
    if not bos_choch or bos_choch["event"] is None:
        return None

    direction = bos_choch["direction"]
    lookback = candles[-15:]
    for i in range(len(lookback) - 2, 0, -1):
        c = lookback[i]
        is_bearish_candle = c["close"] < c["open"]
        is_bullish_candle = c["close"] > c["open"]
        if direction == "bullish" and is_bearish_candle:
            return {"type": "bullish_OB", "high": c["high"], "low": c["low"]}
        if direction == "bearish" and is_bullish_candle:
            return {"type": "bearish_OB", "high": c["high"], "low": c["low"]}
    return None


def fvg_ob_report(candles, bos_choch):
    fvgs = detect_fvgs(candles)
    unfilled = [f for f in fvgs if not f["filled"]]
    ob = find_order_blocks(candles, bos_choch)
    return {
        "fvgs_total": len(fvgs),
        "fvgs_unfilled": unfilled[-3:],
        "order_block": ob,
        "confidence": "MEDIUM",
    }


def average_true_range(candles, period=14):
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        tr = max(c["high"] - c["low"], abs(c["high"] - p["close"]), abs(c["low"] - p["close"]))
        trs.append(tr)
    return sum(trs[-period:]) / period


def displacement_report(candles):
    atr = average_true_range(candles)
    last = candles[-1]
    body = abs(last["close"] - last["open"])
    rng = last["high"] - last["low"]
    body_ratio = (body / rng) if rng else 0

    if atr is None:
        return {"detected": False, "reason": "insufficient data for ATR", "confidence": "LOW"}

    detected = (body > DISPLACEMENT_ATR_MULT * atr) and (body_ratio > DISPLACEMENT_BODY_RATIO)
    direction = "bullish" if last["close"] > last["open"] else "bearish"
    return {
        "detected": detected,
        "direction": direction if detected else None,
        "body": body,
        "atr": atr,
        "body_ratio": round(body_ratio, 2),
        "confidence": "HIGH",
    }


def premium_discount_report(candles, last_high, last_low):
    if last_high is None or last_low is None:
        return {"available": False, "confidence": "LOW"}
    eq = (last_high + last_low) / 2
    price = candles[-1]["close"]
    if price > eq:
        zone = "PREMIUM"
    elif price < eq:
        zone = "DISCOUNT"
    else:
        zone = "EQUILIBRIUM"
    return {
        "available": True,
        "range_high": last_high,
        "range_low": last_low,
        "equilibrium": eq,
        "price": price,
        "zone": zone,
        "confidence": "HIGH",
    }


SESSION_DEFS = [
    ("Asian", "Asia/Tokyo", 9, 18),
    ("London", "Europe/London", 8, 17),
    ("New York", "America/New_York", 8, 17),
]


def session_windows_today():
    now_sast = datetime.now(SAST)
    windows = []
    for name, tzname, start_h, end_h in SESSION_DEFS:
        tz = ZoneInfo(tzname)
        local_date = now_sast.astimezone(tz).date()
        start_local = datetime(local_date.year, local_date.month, local_date.day, start_h, 0, tzinfo=tz)
        end_local = datetime(local_date.year, local_date.month, local_date.day, end_h, 0, tzinfo=tz)
        windows.append((name, start_local.astimezone(SAST), end_local.astimezone(SAST)))
    return windows


def active_sessions(now_sast=None):
    now_sast = now_sast or datetime.now(SAST)
    windows = session_windows_today()
    active = [name for name, start, end in windows if start <= now_sast <= end]
    return active, windows


def session_high_low(candles, session_start_sast, session_end_sast):
    in_session = [
        c for c in candles
        if session_start_sast <= c["dt"].astimezone(SAST) <= session_end_sast
    ]
    if not in_session:
        return None, None
    return max(c["high"] for c in in_session), min(c["low"] for c in in_session)


def sessions_report(candles):
    active, windows = active_sessions()
    details = []
    for name, start, end in windows:
        sh, sl = session_high_low(candles, start, end)
        details.append({
            "name": name,
            "start_sast": start.strftime("%H:%M"),
            "end_sast": end.strftime("%H:%M"),
            "active": name in active,
            "session_high": sh,
            "session_low": sl,
        })
    return {"active_sessions": active, "details": details, "confidence": "HIGH"}


def order_flow_report():
    return {
        "available": False,
        "message": "Order Flow: UNAVAILABLE WITH CURRENT DATA SOURCE",
        "reason": "Twelve Data's time_series endpoint provides OHLC candles only "
                   "— no bid/ask, delta, order-book depth, or footprint data.",
        "required_source": "A Level 2 / tick-level data provider or broker feed with order-book access.",
    }


def volume_report(candles):
    has_volume = any(c["volume"] not in (None, 0) for c in candles[-30:])
    if not has_volume:
        return {"available": False, "confidence": "UNAVAILABLE",
                "note": "No usable volume field returned for this symbol/data source."}
    vols = [c["volume"] for c in candles[-30:] if c["volume"] is not None]
    avg = sum(vols) / len(vols)
    latest = candles[-1]["volume"] or 0
    relative = (latest / avg) if avg else None
    return {
        "available": True,
        "confidence": "LIMITED",
        "note": "This is tick volume from the data feed, not true centralized exchange volume.",
        "latest": latest,
        "average_30": round(avg, 2),
        "relative": round(relative, 2) if relative else None,
    }


def macro_report():
    return {"available": False, "message": "Macro calendar unavailable."}


def smt_report():
    return {"available": False, "message": "SMT divergence: UNAVAILABLE — requires a correlated "
                                            "instrument feed (e.g. DXY) which is not currently integrated."}


def build_confluence(structure, liquidity, fvgob, displacement, premdisc, direction):
    factors = []

    bos_choch = structure["bos_choch"]
    factors.append((
        f"{bos_choch['event'] or 'No'} {direction or ''} structure break".strip(),
        True,
        bos_choch["event"] is not None and bos_choch["direction"] == direction,
    ))

    swept = liquidity["sweep_low_detected"] if direction == "bullish" else liquidity["sweep_high_detected"]
    factors.append((
        f"{'Sell' if direction == 'bullish' else 'Buy'}-side liquidity swept",
        True,
        bool(swept),
    ))

    factors.append((
        "Displacement in setup direction",
        True,
        displacement["detected"] and displacement.get("direction") == direction,
    ))

    if premdisc["available"]:
        wants_discount = direction == "bullish"
        in_zone = (premdisc["zone"] == "DISCOUNT") if wants_discount else (premdisc["zone"] == "PREMIUM")
        factors.append((f"Price in {'discount' if wants_discount else 'premium'}", True, in_zone))
    else:
        factors.append(("Premium/discount", False, False))

    unfilled_matching = [
        f for f in fvgob["fvgs_unfilled"]
        if (f["type"] == "bullish" and direction == "bullish") or (f["type"] == "bearish" and direction == "bearish")
    ]
    factors.append((f"Unfilled {direction} FVG present", True, bool(unfilled_matching)))

    factors.append(("SMT divergence", False, False))
    factors.append(("Order flow", False, False))

    applicable = [f for f in factors if f[1]]
    met = sum(1 for f in applicable if f[2])
    return {"factors": factors, "score": met, "of": len(applicable)}


def _buffer(candles):
    atr = average_true_range(candles) or 0
    return max(atr * 0.15, 0.3)


def build_signal(candles, structure, liquidity, fvgob, displacement, premdisc):
    bos_choch = structure["bos_choch"]
    direction = bos_choch["direction"]

    if direction is None:
        return {"state": "NO CLEAR SETUP", "reason": "No confirmed BOS/CHoCH on this timeframe right now."}

    confluence = build_confluence(structure, liquidity, fvgob, displacement, premdisc, direction)
    score_pct = (confluence["score"] / confluence["of"]) if confluence["of"] else 0

    if score_pct < CONFLUENCE_THRESHOLD:
        return {
            "state": "NO CLEAR SETUP",
            "reason": f"Structure break present but confluence too low ({confluence['score']}/{confluence['of']}).",
            "confluence": confluence,
        }

    price = candles[-1]["close"]
    ob = fvgob["order_block"]

    if direction == "bullish":
        stop = (ob["low"] if ob else structure["last_swing_low"]) - _buffer(candles)
        entry = price
        risk = entry - stop
        tp1 = entry + risk
        tp2 = entry + risk * DEFAULT_RR_TARGET
        state = "BULLISH SETUP"
        invalidation = f"If price closes back below {stop:.2f}, this setup is invalidated."
    else:
        stop = (ob["high"] if ob else structure["last_swing_high"]) + _buffer(candles)
        entry = price
        risk = stop - entry
        tp1 = entry - risk
        tp2 = entry - risk * DEFAULT_RR_TARGET
        state = "BEARISH SETUP"
        invalidation = f"If price closes back above {stop:.2f}, this setup is invalidated."

    return {
        "state": state,
        "direction": direction,
        "entry": entry,
        "stop": stop,
        "tp1": tp1,
        "tp2": tp2,
        "risk": risk,
        "confluence": confluence,
        "invalidation": invalidation,
        "bos_choch": bos_choch,
    }


def fmt_price(p):
    return f"{p:.2f}" if p is not None else "n/a"


def full_analysis(interval=DEFAULT_INTERVAL):
    candles, err = fetch_candles(interval)
    if err:
        return None, err

    structure = market_structure_report(candles)
    liquidity = liquidity_report(candles, structure["swings"])
    fvgob = fvg_ob_report(candles, structure["bos_choch"])
    displacement = displacement_report(candles)
    premdisc = premium_discount_report(candles, structure["last_swing_high"], structure["last_swing_low"])
    sessions = sessions_report(candles)
    volume = volume_report(candles)
    order_flow = order_flow_report()
    macro = macro_report()
    smt = smt_report()
    signal = build_signal(candles, structure, liquidity, fvgob, displacement, premdisc)

    return {
        "candles": candles,
        "structure": structure,
        "liquidity": liquidity,
        "fvgob": fvgob,
        "displacement": displacement,
        "premdisc": premdisc,
        "sessions": sessions,
        "volume": volume,
        "order_flow": order_flow,
        "macro": macro,
        "smt": smt,
        "signal": signal,
    }, None


def render_structure(a):
    s = a["structure"]
    bc = s["bos_choch"]
    lines = [
        "📊 <b>Market Structure</b>",
        f"Trend: <b>{s['trend'] or 'Undetermined'}</b>",
        f"Last swing high: {fmt_price(s['last_swing_high'])}",
        f"Last swing low: {fmt_price(s['last_swing_low'])}",
    ]
    if bc["event"]:
        lines.append(f"⚡ {bc['event']} — {bc['direction']} (broke {fmt_price(bc['level'])})")
    else:
        lines.append("No confirmed BOS/CHoCH right now.")
    lines.append(f"Confidence: {s['confidence']}")
    return "\n".join(lines)


def render_liquidity(a):
    liq = a["liquidity"]
    lines = ["💧 <b>Liquidity</b>"]
    lines.append(f"Equal highs pools: {len(liq['equal_highs'])}")
    lines.append(f"Equal lows pools: {len(liq['equal_lows'])}")
    if liq["prev_day_high"]:
        lines.append(f"Previous day H/L: {fmt_price(liq['prev_day_high'])} / {fmt_price(liq['prev_day_low'])}")
    lines.append(f"Buy-side sweep detected: {'✅' if liq['sweep_high_detected'] else '❌'}")
    lines.append(f"Sell-side sweep detected: {'✅' if liq['sweep_low_detected'] else '❌'}")
    lines.append(f"Confidence: {liq['confidence']}")
    return "\n".join(lines)


def render_fvgob(a):
    f = a["fvgob"]
    lines = ["🧱 <b>FVG / Order Block</b>", f"Total FVGs found: {f['fvgs_total']}"]
    if f["fvgs_unfilled"]:
        for g in f["fvgs_unfilled"]:
            lines.append(f"  • Unfilled {g['type']} FVG: {fmt_price(g['bottom'])}–{fmt_price(g['top'])}")
    else:
        lines.append("  No unfilled FVGs in range.")
    ob = f["order_block"]
    lines.append(f"Order block: {ob['type'] + ' ' + fmt_price(ob['low']) + '-' + fmt_price(ob['high']) if ob else 'None detected'}")
    lines.append(f"Confidence: {f['confidence']}")
    return "\n".join(lines)


def render_displacement(a):
    d = a["displacement"]
    lines = ["⚡ <b>Displacement</b>"]
    if not d.get("detected") and d.get("reason"):
        lines.append(d["reason"])
    else:
        lines.append(f"Detected: {'✅ ' + d['direction'] if d['detected'] else '❌ None'}")
        lines.append(f"Body: {d['body']:.2f} vs ATR: {d['atr']:.2f} (x{DISPLACEMENT_ATR_MULT} threshold)")
        lines.append(f"Body/range ratio: {d['body_ratio']}")
    lines.append(f"Confidence: {d['confidence']}")
    return "\n".join(lines)


def render_premdisc(a):
    p = a["premdisc"]
    if not p["available"]:
        return "📈 <b>Premium / Discount</b>\nUnavailable — no dealing range established yet."
    return (
        "📈 <b>Premium / Discount</b>\n"
        f"Range: {fmt_price(p['range_low'])} – {fmt_price(p['range_high'])}\n"
        f"Equilibrium: {fmt_price(p['equilibrium'])}\n"
        f"Current price: {fmt_price(p['price'])}\n"
        f"Zone: <b>{p['zone']}</b>"
    )


def render_sessions(a):
    s = a["sessions"]
    lines = ["🕒 <b>Sessions (SAST)</b>"]
    for d in s["details"]:
        mark = "🟢" if d["active"] else "⚪"
        sh = fmt_price(d["session_high"]) if d["session_high"] else "n/a"
        sl = fmt_price(d["session_low"]) if d["session_low"] else "n/a"
        lines.append(f"{mark} {d['name']}: {d['start_sast']}-{d['end_sast']} SAST | H:{sh} L:{sl}")
    return "\n".join(lines)


def render_macro(a):
    m = a["macro"]
    smt = a["smt"]
    lines = ["🌎 <b>Macro / SMT</b>", m["message"], smt["message"]]
    return "\n".join(lines)


def render_data_stats(a):
    v = a["volume"]
    of = a["order_flow"]
    lines = ["📊 <b>Data / Statistics — Live Confidence</b>"]
    if v["available"]:
        lines.append(f"Volume: {v['confidence']} — {v['note']}")
        lines.append(f"  Latest: {v['latest']}, 30-bar avg: {v['average_30']}, relative: {v['relative']}")
    else:
        lines.append(f"Volume: UNAVAILABLE — {v['note']}")
    lines.append(of["message"])
    stats = get_stats()
    lines.append("")
    lines.append("📈 <b>Historical Signal Stats</b> (manually tagged outcomes)")
    if stats["total_tagged"] == 0:
        lines.append("No tagged signals yet. Use the ✅/❌/➖ buttons under a signal to tag outcomes.")
    else:
        lines.append(f"Tagged signals: {stats['total_tagged']} (W:{stats['wins']} L:{stats['losses']} BE:{stats['be']})")
        lines.append(f"Win rate: {stats['win_rate']:.1f}%")
        lines.append(f"Avg R: {stats['avg_r']:.2f}")
        pf = stats["profit_factor"]
        lines.append(f"Profit factor: {'∞' if pf == float('inf') else f'{pf:.2f}' if pf is not None else 'n/a'}")
        lines.append(f"Max win streak: {stats['max_win_streak']} | Max loss streak: {stats['max_loss_streak']}")
    lines.append("\n<i>Historical performance is only historical performance — it is not a guarantee of future results.</i>")
    return "\n".join(lines)


def render_risk(a):
    sig = a["signal"]
    if sig["state"] == "NO CLEAR SETUP":
        return "⚖️ <b>Risk / Reward</b>\nNo active setup to size right now."
    return (
        "⚖️ <b>Risk / Reward</b>\n"
        f"Entry: {fmt_price(sig['entry'])}\n"
        f"Stop: {fmt_price(sig['stop'])}\n"
        f"Risk distance: {fmt_price(sig['risk'])}\n"
        f"TP1 (1R): {fmt_price(sig['tp1'])}\n"
        f"TP2 ({DEFAULT_RR_TARGET:g}R): {fmt_price(sig['tp2'])}\n\n"
        "Use /size &lt;account&gt; &lt;risk%&gt; &lt;entry&gt; &lt;stop&gt; for a position-size estimate.\n"
        "<i>Actual lot size depends on your broker's contract specs, tick value and leverage.</i>"
    )


def render_signal(a):
    sig = a["signal"]
    if sig["state"] == "NO CLEAR SETUP":
        return f"🎯 <b>SIGNAL</b>\n⚪ <b>NO CLEAR SETUP</b>\n{sig['reason']}"

    icon = "🟢" if sig["direction"] == "bullish" else "🔴"
    c = sig["confluence"]
    lines = [
        "🎯 <b>SIGNAL</b>",
        f"{icon} <b>{sig['state']}</b>",
        "",
        f"Direction: {sig['direction']}",
        f"Entry: {fmt_price(sig['entry'])}",
        f"Stop: {fmt_price(sig['stop'])}",
        f"TP1: {fmt_price(sig['tp1'])}",
        f"TP2: {fmt_price(sig['tp2'])}",
        f"R:R (TP2): 1:{DEFAULT_RR_TARGET:g}",
        "",
        f"Confluence: {c['score']}/{c['of']}",
    ]
    for label, applicable, met in c["factors"]:
        if not applicable:
            lines.append(f"⚠️ {label} (unavailable)")
        else:
            lines.append(f"{'✅' if met else '❌'} {label}")
    lines.append("")
    lines.append(f"INVALIDATION: {sig['invalidation']}")
    lines.append("\n<i>This is analysis, not financial advice. You decide whether to act on it.</i>")
    return "\n".join(lines)


def render_full(a):
    return "\n\n".join([
        render_structure(a), render_liquidity(a), render_fvgob(a),
        render_displacement(a), render_premdisc(a), render_sessions(a),
        render_macro(a), render_risk(a), render_signal(a),
    ])


SECTION_RENDERERS = {
    "full": render_full,
    "structure": render_structure,
    "liquidity": render_liquidity,
    "fvgob": render_fvgob,
    "displacement": render_displacement,
    "premdisc": render_premdisc,
    "sessions": render_sessions,
    "macro": render_macro,
    "stats": render_data_stats,
    "risk": render_risk,
    "signal": render_signal,
}


def main_menu_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🔎 FULL ANALYSIS", callback_data="menu:full"),
        types.InlineKeyboardButton("📊 MARKET STRUCTURE", callback_data="menu:structure"),
    )
    kb.add(
        types.InlineKeyboardButton("💧 LIQUIDITY", callback_data="menu:liquidity"),
        types.InlineKeyboardButton("🧱 FVG / ORDER BLOCK", callback_data="menu:fvgob"),
    )
    kb.add(
        types.InlineKeyboardButton("⚡ DISPLACEMENT", callback_data="menu:displacement"),
        types.InlineKeyboardButton("📈 PREMIUM / DISCOUNT", callback_data="menu:premdisc"),
    )
    kb.add(
        types.InlineKeyboardButton("🕒 SESSIONS", callback_data="menu:sessions"),
        types.InlineKeyboardButton("🌎 MACRO", callback_data="menu:macro"),
    )
    kb.add(
        types.InlineKeyboardButton("📊 DATA / STATISTICS", callback_data="menu:stats"),
        types.InlineKeyboardButton("⚖️ RISK / REWARD", callback_data="menu:risk"),
    )
    kb.add(types.InlineKeyboardButton("🎯 SIGNAL", callback_data="menu:signal"))
    kb.add(
        types.InlineKeyboardButton("🔄 REFRESH", callback_data="menu:refresh"),
        types.InlineKeyboardButton("⬅️ BACK", callback_data="menu:back"),
    )
    return kb


def signal_outcome_keyboard(signal_id: int):
    kb = types.InlineKeyboardMarkup(row_width=3)
    kb.add(
        types.InlineKeyboardButton("✅ WIN", callback_data=f"tag:{signal_id}:WIN"),
        types.InlineKeyboardButton("❌ LOSS", callback_data=f"tag:{signal_id}:LOSS"),
        types.InlineKeyboardButton("➖ BE", callback_data=f"tag:{signal_id}:BE"),
    )
    return kb


@bot.message_handler(commands=["analyze"])
def cmd_analyze(message: types.Message) -> None:
    bot.send_message(
        message.chat.id,
        f"🥇 <b>{SYMBOL} ANALYSIS</b> ({DEFAULT_INTERVAL})\nSelect a section:",
        reply_markup=main_menu_keyboard(),
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("menu:"))
def handle_menu(call: types.CallbackQuery) -> None:
    key = call.data.split(":", 1)[1]
    bot.answer_callback_query(call.id)

    if key == "back":
        bot.edit_message_text(
            f"🥇 <b>{SYMBOL} ANALYSIS</b> ({DEFAULT_INTERVAL})\nSelect a section:",
            chat_id=call.message.chat.id, message_id=call.message.message_id,
            reply_markup=main_menu_keyboard(), parse_mode="HTML",
        )
        return

    section = "full" if key == "refresh" else key
    a, err = full_analysis()
    if err:
        bot.edit_message_text(f"⚠️ {err}", chat_id=call.message.chat.id,
                               message_id=call.message.message_id, reply_markup=main_menu_keyboard())
        return

    text = SECTION_RENDERERS[section](a)
    bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                           reply_markup=main_menu_keyboard(), parse_mode="HTML")

    if section == "signal" and a["signal"]["state"] != "NO CLEAR SETUP":
        sig = a["signal"]
        active_names, _ = active_sessions()
        record = {
            "ts": datetime.now(SAST).isoformat(),
            "symbol": SYMBOL,
            "timeframe": DEFAULT_INTERVAL,
            "session": ",".join(active_names) or "none",
            "direction": sig["direction"],
            "entry": sig["entry"], "stop": sig["stop"], "tp1": sig["tp1"], "tp2": sig["tp2"],
            "bos_choch": str(sig["bos_choch"]),
            "liquidity_sweep": str({
                "buy_side": a["liquidity"]["sweep_high_detected"],
                "sell_side": a["liquidity"]["sweep_low_detected"],
            }),
            "fvg": str(a["fvgob"]["fvgs_unfilled"]),
            "order_block": str(a["fvgob"]["order_block"]),
            "displacement": str(a["displacement"]),
            "premium_discount": a["premdisc"].get("zone", "n/a"),
            "macro_note": a["macro"]["message"],
            "confluence_score": f"{sig['confluence']['score']}/{sig['confluence']['of']}",
        }
        signal_id = log_signal(record)
        bot.send_message(
            call.message.chat.id,
            f"Signal #{signal_id} logged. Tag the outcome once it plays out:",
            reply_markup=signal_outcome_keyboard(signal_id),
        )


@bot.callback_query_handler(func=lambda call: call.data.startswith("tag:"))
def handle_tag(call: types.CallbackQuery) -> None:
    _, sid, outcome = call.data.split(":")
    tag_outcome(int(sid), outcome)
    bot.answer_callback_query(call.id, text=f"Signal #{sid} tagged {outcome}")
    bot.edit_message_text(
        f"Signal #{sid} tagged: <b>{outcome}</b>",
        chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode="HTML",
    )


@bot.message_handler(commands=["size"])
def cmd_size(message: types.Message) -> None:
    parts = message.text.split()[1:]
    if len(parts) != 4:
        bot.reply_to(
            message,
            "Usage: <code>/size account_size risk_percent entry stop</code>\n"
            "Example: <code>/size 1000 1 4300 4285</code>",
        )
        return
    try:
        account, risk_pct, entry, stop = (float(x) for x in parts)
    except ValueError:
        bot.reply_to(message, "All four values must be numbers.")
        return

    risk_amount = account * (risk_pct / 100)
    distance = abs(entry - stop)
    if distance == 0:
        bot.reply_to(message, "Entry and stop can't be the same price.")
        return
    theoretical_units = risk_amount / distance

    bot.reply_to(
        message,
        "⚖️ <b>Position Size (informational only)</b>\n"
        f"Risk amount: {risk_amount:.2f}\n"
        f"Stop distance: {distance:.2f}\n"
        f"Theoretical size: {theoretical_units:.4f} units of price distance\n\n"
        "<i>This is a plain calculation only. Actual lot size depends on your broker's "
        "contract size, tick value, and leverage — check your broker's specs before sizing "
        "a real position. This bot does not place any trade.</i>",
    )


@bot.message_handler(commands=["start", "help"])
def cmd_start(message: types.Message) -> None:
    bot.reply_to(
        message,
        "⚡ <b>Liquidity Phantom Bot Online</b>\n\n"
        "This is a market ANALYSIS bot — it never places trades.\n\n"
        "Commands:\n"
        "/analyze — Open the interactive XAU/USD analysis menu\n"
        "/size — Position-size calculator (informational)\n"
        "/help — Show this message",
    )


@bot.message_handler(func=lambda m: True)
def fallback(message: types.Message) -> None:
    bot.reply_to(message, "Unknown command. Use /help.")


def main() -> None:
    init_db()
    keep_alive()
    logging.info("Liquidity Phantom Bot (analysis engine) starting...")
    
    while True:
        try:
            bot.infinity_polling(
                skip_pending=True,
                timeout=20,
                long_polling_timeout=20,
                non_stop=True
            )
        except Exception as e:
            logging.error(f"Polling error encountered: {e}. Retrying in 5 seconds...")
            time.sleep(5)


# ============================================================================
# ICT EXTENSION — sequence-aware setup recognition (SSL/BSL raid → displacement
# → MSS → FVG/PD array → retracement → entry). Namespaced ICT_.
# ============================================================================

ICT_VALIDITY_CANDLES = 40
ICT_SCAN_LOOKBACK = 150
ICT_FVG_SEARCH_PAD = 5


def ICT_previous_session_high_low(candles):
    now_sast = candles[-1]["dt"].astimezone(SAST)
    windows = []
    for name, tzname, start_h, end_h in SESSION_DEFS:
        tz = ZoneInfo(tzname)
        for day_offset in (0, -1):
            d = (now_sast + timedelta(days=day_offset)).astimezone(tz).date()
            start_local = datetime(d.year, d.month, d.day, start_h, 0, tzinfo=tz)
            end_local = datetime(d.year, d.month, d.day, end_h, 0, tzinfo=tz)
            windows.append((name, start_local.astimezone(SAST), end_local.astimezone(SAST)))
    completed = [w for w in windows if w[2] < now_sast]
    if not completed:
        return None, None, None
    completed.sort(key=lambda w: w[2], reverse=True)
    name, start, end = completed[0]
    in_session = [c for c in candles if start <= c["dt"].astimezone(SAST) <= end]
    if not in_session:
        return None, None, name
    return max(c["high"] for c in in_session), min(c["low"] for c in in_session), name


def ICT_liquidity_pools(candles, swings):
    pools = []
    for p in find_equal_levels(swings, "high"):
        pools.append({"type": "BSL", "price": p, "source": "equal highs"})
    for p in find_equal_levels(swings, "low"):
        pools.append({"type": "SSL", "price": p, "source": "equal lows"})
    pdh, pdl = previous_day_high_low(candles)
    if pdh:
        pools.append({"type": "BSL", "price": pdh, "source": "PDH"})
        pools.append({"type": "SSL", "price": pdl, "source": "PDL"})
    psh, psl, sess_name = ICT_previous_session_high_low(candles)
    if psh:
        pools.append({"type": "BSL", "price": psh, "source": f"previous {sess_name} session high"})
        pools.append({"type": "SSL", "price": psl, "source": f"previous {sess_name} session low"})
    return pools


def ICT_find_raids(candles, pools, lookback=ICT_SCAN_LOOKBACK, confirm_within=3):
    start = max(0, len(candles) - lookback)
    events = []
    for pool in pools:
        level = pool["price"]
        direction = "high" if pool["type"] == "BSL" else "low"
        for i in range(start, len(candles)):
            c = candles[i]
            beyond = (c["high"] > level) if direction == "high" else (c["low"] < level)
            if not beyond:
                continue
            for j in range(i + 1, min(i + 1 + confirm_within, len(candles))):
                back_inside = (candles[j]["close"] < level) if direction == "high" else (candles[j]["close"] > level)
                if back_inside:
                    events.append({"idx": j, "raid_idx": i, "pool": pool,
                                   "reversal_direction": "bullish" if pool["type"] == "SSL" else "bearish"})
                    break
    events.sort(key=lambda e: e["idx"], reverse=True)
    return events


def ICT_find_mss(candles, raid_idx, direction, window):
    end = min(raid_idx + window, len(candles) - 1)
    pre_swings = find_swings(candles[: raid_idx + 1])
    if direction == "bullish":
        prior_highs = [s["price"] for s in pre_swings if s["type"] == "high"]
        if not prior_highs:
            return None
        mss_level = prior_highs[-1]
        for j in range(raid_idx + 1, end + 1):
            if candles[j]["close"] > mss_level:
                return {"idx": j, "level": mss_level}
    else:
        prior_lows = [s["price"] for s in pre_swings if s["type"] == "low"]
        if not prior_lows:
            return None
        mss_level = prior_lows[-1]
        for j in range(raid_idx + 1, end + 1):
            if candles[j]["close"] < mss_level:
                return {"idx": j, "level": mss_level}
    return None


def ICT_find_displacement(candles, start_idx, end_idx, direction):
    atr = average_true_range(candles[: end_idx + 1]) or 0
    for j in range(start_idx, end_idx + 1):
        c = candles[j]
        body = abs(c["close"] - c["open"])
        rng = c["high"] - c["low"]
        body_ratio = (body / rng) if rng else 0
        cand_dir = "bullish" if c["close"] > c["open"] else "bearish"
        if cand_dir == direction and body > DISPLACEMENT_ATR_MULT * atr and body_ratio > DISPLACEMENT_BODY_RATIO:
            return {"idx": j, "body": body, "atr": atr}
    return None


def ICT_find_fvg_near(candles, start_idx, end_idx, direction):
    end = min(end_idx + ICT_FVG_SEARCH_PAD, len(candles) - 1)
    window = candles[max(0, start_idx - 1): end + 1]
    offset = max(0, start_idx - 1)
    fvgs = detect_fvgs(window)
    matching = [f for f in fvgs if f["type"] == direction]
    if not matching:
        return None
    best = min(matching, key=lambda f: abs((f["i"] + offset) - start_idx))
    best = dict(best)
    best["i"] += offset
    return best


def ICT_fvg_filled_before_last(candles, fvg):
    for c in candles[fvg["i"] + 1: -1]:
        if fvg["bottom"] <= c["low"] <= fvg["top"] or fvg["bottom"] <= c["high"] <= fvg["top"]:
            return True
    return False


def ICT_find_order_block(candles, raid_idx, mss_idx, direction):
    for i in range(mss_idx - 1, raid_idx - 1, -1):
        c = candles[i]
        if direction == "bullish" and c["close"] < c["open"]:
            return {"type": "bullish OB", "high": c["high"], "low": c["low"], "idx": i}
        if direction == "bearish" and c["close"] > c["open"]:
            return {"type": "bearish OB", "high": c["high"], "low": c["low"], "idx": i}
    return None


def ICT_check_invalidated(candles, raid, mss, direction):
    raid_extreme = raid["pool"]["price"]
    for j in range(mss["idx"] + 1, len(candles)):
        c = candles[j]
        if direction == "bullish" and c["close"] < raid_extreme:
            return True
        if direction == "bearish" and c["close"] > raid_extreme:
            return True
    return False


def ICT_price_in_zone(candles, zone_low, zone_high):
    last = candles[-1]
    return last["low"] <= zone_high and last["high"] >= zone_low


def ICT_build_setup(candles):
    structure = market_structure_report(candles)
    swings = structure["swings"]
    pools = ICT_liquidity_pools(candles, swings)
    raids = ICT_find_raids(candles, pools)

    for raid in raids:
        direction = raid["reversal_direction"]
        mss = ICT_find_mss(candles, raid["idx"], direction, ICT_VALIDITY_CANDLES)
        if not mss:
            continue
        displacement = ICT_find_displacement(candles, raid["idx"] + 1, mss["idx"], direction)
        if not displacement:
            continue
        fvg = ICT_find_fvg_near(candles, mss["idx"], mss["idx"], direction)
        if not fvg:
            continue
        if ICT_check_invalidated(candles, raid, mss, direction):
            continue

        ob = ICT_find_order_block(candles, raid["idx"], mss["idx"], direction)
        zone_low, zone_high = fvg["bottom"], fvg["top"]
        in_zone_now = ICT_price_in_zone(candles, zone_low, zone_high)
        candles_since_mss = (len(candles) - 1) - mss["idx"]
        still_valid_window = candles_since_mss <= ICT_VALIDITY_CANDLES

        pd = premium_discount_report(candles, max(raid["pool"]["price"], mss["level"]),
                                      min(raid["pool"]["price"], mss["level"]))

        setup = {
            "direction": direction,
            "pool": raid["pool"],
            "raid_idx": raid["idx"],
            "mss": mss,
            "displacement": displacement,
            "fvg": fvg,
            "fvg_is_inversion": ICT_fvg_filled_before_last(candles, fvg),
            "order_block": ob,
            "premium_discount": pd,
            "session_at_raid": LPX_active_session_label(candles[raid["idx"]]["dt"].astimezone(SAST)),
            "session_now": LPX_active_session_label(candles[-1]["dt"].astimezone(SAST)),
            "candles_since_mss": candles_since_mss,
        }

        if in_zone_now and still_valid_window:
            setup["state"] = "ENTRY"
            return setup
        if still_valid_window:
            setup["state"] = "DEVELOPING"
            return setup
        continue

    return {"state": "NONE"}


def ICT_format_setup(setup: dict) -> str:
    if setup["state"] == "NONE":
        return (
            "🎯 <b>ICT SETUP — XAUUSD</b>\n\n"
            "⚪ No developing ICT setup found right now.\n\n"
            "No liquidity raid in the recent lookback led to a confirmed MSS "
            "with displacement and an FVG that is still inside its validity window."
        )

    direction = setup["direction"]
    icon = "🟢" if direction == "bullish" else "🔴"
    pool = setup["pool"]
    fvg = setup["fvg"]
    ob = setup["order_block"]
    pd = setup["premium_discount"]

    raid_label = f"{pool['type']} raid at {fmt_price(pool['price'])} ({pool['source']})"
    mss_label = f"MSS confirmed {direction} (broke {fmt_price(setup['mss']['level'])})"
    disp_label = f"Displacement confirmed {direction}"
    fvg_kind = "Inversion FVG" if setup["fvg_is_inversion"] else "FVG"
    fvg_label = f"{fvg_kind} ({direction}) — {fmt_price(fvg['bottom'])} to {fmt_price(fvg['top'])}"
    ob_label = f"{ob['type']} — {fmt_price(ob['low'])} to {fmt_price(ob['high'])}" if ob else "None identified"

    pd_zone = pd.get("zone", "n/a") if pd.get("available") else "n/a"

    if setup["state"] == "ENTRY":
        if direction == "bullish":
            entry = fvg["top"]
            stop = min(fvg["bottom"], ob["low"] if ob else fvg["bottom"]) - 0.5
        else:
            entry = fvg["bottom"]
            stop = max(fvg["top"], ob["high"] if ob else fvg["top"]) + 0.5
        risk = abs(entry - stop)
        tp1 = entry + risk if direction == "bullish" else entry - risk
        tp2 = entry + risk * DEFAULT_RR_TARGET if direction == "bullish" else entry - risk * DEFAULT_RR_TARGET

        header = f"{icon} <b>ENTRY ZONE — {('BUY' if direction=='bullish' else 'SELL')}</b>"
        levels = (
            f"Entry: {fmt_price(entry)}\n"
            f"Stop: {fmt_price(stop)}\n"
            f"TP1: {fmt_price(tp1)}\n"
            f"TP2: {fmt_price(tp2)}\n"
            f"Risk/Reward: 1:{DEFAULT_RR_TARGET:g}\n\n"
        )
    else:
        header = f"{icon} <b>DEVELOPING — watching for retracement</b>"
        levels = f"Price has not yet retraced into the {fvg_kind} ({direction}).\n\n"

    return (
        "🎯 <b>ICT SETUP — XAUUSD</b>\n\n"
        f"{header}\n\n"
        f"Sequence:\n"
        f"1. {raid_label}\n"
        f"2. {disp_label}\n"
        f"3. {mss_label}\n"
        f"4. {fvg_label}\n"
        f"5. Order Block: {ob_label}\n\n"
        f"{levels}"
        f"Premium/Discount: {pd_zone}\n"
        f"Session at raid: {setup['session_at_raid']} | Session now: {setup['session_now']}\n"
        f"Candles since MSS: {setup['candles_since_mss']} (valid window: {ICT_VALIDITY_CANDLES})\n\n"
        "SMT divergence: UNAVAILABLE — no correlated instrument feed (e.g. DXY) is configured.\n\n"
        "⚠️ Analysis only. No broker orders are placed."
    )


@bot.message_handler(commands=["ict"])
def ICT_cmd_ict(message: types.Message) -> None:
    candles, err = fetch_candles(DEFAULT_INTERVAL, outputsize=max(ICT_SCAN_LOOKBACK + 30, CANDLE_COUNT))
    if err:
        bot.reply_to(message, f"⚠️ {err}")
        return
    setup = ICT_build_setup(candles)
    bot.reply_to(message, ICT_format_setup(setup))


_ICT_NEW_HANDLER_COUNT = 1
bot.message_handlers = (
    bot.message_handlers[-_ICT_NEW_HANDLER_COUNT:] + bot.message_handlers[:-_ICT_NEW_HANDLER_COUNT]
)


if __name__ == "__main__":
    main()
