#!/usr/bin/env python3
"""
MSNR + CRT Scanner v4 — GitHub Actions
Elite Observer — High RR Setup Finder

Timeframe hierarchy:
- 1D MSNR + 4H MSNR stacked + 1H CRT = A+ (rarest, highest RR)
- 1D MSNR + 1H CRT = A
- 4H MSNR + 1H CRT = B
All three fire Telegram alerts.
"""

import json
import urllib.request
import urllib.parse
import os
import time
from datetime import datetime, timezone

# ── CONFIG ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "1247283950")
MIN_RR           = 3.0
SPAM_HOURS       = 6
STATE_FILE       = "last_alerts.json"

# ── FETCH ────────────────────────────────────────────────────────────────────
def fetch_candles(symbol, interval, limit=120):
    endpoint  = "histoday" if interval == "1d" else "histohour"
    aggregate = {"1d": 1, "4h": 4, "1h": 1}.get(interval, 1)
    url = (f"https://min-api.cryptocompare.com/data/v2/{endpoint}"
           f"?fsym={symbol}&tsym=USD&limit={limit}&aggregate={aggregate}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MSNR-Scanner/4.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
        if data.get("Response") != "Success":
            print(f"  Error {symbol} {interval}: {data.get('Message','?')}")
            return []
        result = [
            {"t": k["time"]*1000, "o": float(k["open"]), "h": float(k["high"]),
             "l": float(k["low"]),  "c": float(k["close"])}
            for k in data["Data"]["Data"]
            if not (k["open"] == 0 and k["close"] == 0)
        ]
        print(f"  {symbol} {interval}: {len(result)} candles OK")
        return result[-limit:]
    except Exception as e:
        print(f"  Fetch error {symbol} {interval}: {e}")
        return []

# ── TREND ────────────────────────────────────────────────────────────────────
def get_trend(candles):
    if len(candles) < 10: return "UNKNOWN"
    c = [x["c"] for x in candles[-20:]]
    bull = sum(1 for i in range(2, len(c)) if c[i] > c[i-1] > c[i-2])
    bear = sum(1 for i in range(2, len(c)) if c[i] < c[i-1] < c[i-2])
    if bull > bear + 2: return "BULLISH"
    if bear > bull + 2: return "BEARISH"
    return "RANGING"

# ── MSNR LEVELS ──────────────────────────────────────────────────────────────
def find_msnr_levels(candles, lb=3, max_dist=12.0):
    """Find A-levels and V-levels on line chart (closes only)."""
    if len(candles) < lb*2 + 2: return []
    closes = [c["c"] for c in candles]
    cur    = closes[-1]
    levels = []
    for i in range(lb, len(closes) - lb):
        is_a = all(closes[i] > closes[i-j] and closes[i] > closes[i+j] for j in range(1, lb+1))
        is_v = all(closes[i] < closes[i-j] and closes[i] < closes[i+j] for j in range(1, lb+1))
        if not is_a and not is_v: continue
        price = closes[i]
        dist  = abs(cur - price) / cur * 100
        if dist > max_dist: continue
        t = "A" if is_a else "V"
        wicks, dead = 0, False
        for fc in candles[i+1:]:
            if t == "A":
                if fc["c"] > price: dead = True; break
                if fc["h"] >= price: wicks += 1
            else:
                if fc["c"] < price: dead = True; break
                if fc["l"] <= price: wicks += 1
        if dead: continue
        hsl = sum(1 for pc in closes[:i] if abs(pc - price) / price < 0.005) >= 2
        levels.append({
            "type":      t,
            "price":     price,
            "freshness": "FRESH" if wicks == 0 else "UNFRESH",
            "hsl":       hsl,
            "dist":      dist,
            "wicks":     wicks,
        })
    return sorted(levels, key=lambda x: x["dist"])[:8]

# ── 4H CONFLUENCE ────────────────────────────────────────────────────────────
def find_confluence(level, levels_other, pct=1.0):
    """Find matching level in another timeframe within pct%."""
    for lv in levels_other:
        if lv["type"] != level["type"]: continue
        if abs(lv["price"] - level["price"]) / level["price"] * 100 <= pct:
            return lv
    return None

# ── CRT DETECTION ────────────────────────────────────────────────────────────
def find_crt(c1h, level, trend):
    """
    Scan 1H candles for CRT pattern at given level.
    Returns (sweep_candle, confirm_candle, body_ratio) or None.
    """
    if len(c1h) < 15 or trend == "RANGING": return None
    bull = level["type"] == "V"
    if bull  and trend != "BULLISH": return None
    if not bull and trend != "BEARISH": return None

    for i in range(max(5, len(c1h) - 48), len(c1h) - 1):
        # External range
        rs = i - 1
        while rs > 1:
            a, b = c1h[rs], c1h[rs-1]
            if bull     and a["c"] >= b["c"]: break
            if not bull and a["c"] <= b["c"]: break
            rs -= 1
        seg = c1h[rs:i+1]
        rH  = max(x["h"] for x in seg)
        rL  = min(x["l"] for x in seg)
        if (rH - rL) / c1h[-1]["c"] < 0.002: continue

        sw = c1h[i]
        cf = c1h[i+1]

        # CRT-2: wick sweeps level, close back inside range
        if bull:
            crt2 = sw["l"] <= level["price"] and sw["c"] > level["price"] and sw["c"] > rL
        else:
            crt2 = sw["h"] >= level["price"] and sw["c"] < level["price"] and sw["c"] < rH
        if not crt2: continue

        # CRT-3: decisive body past entire range
        body = abs(cf["c"] - cf["o"])
        cr   = cf["h"] - cf["l"]
        br   = body / cr if cr > 0 else 0
        if bull:
            crt3 = cf["c"] > rH and br > 0.5
        else:
            crt3 = cf["c"] < rL and br > 0.5
        if not crt3: continue

        return {"sw": sw, "cf": cf, "br": br, "rH": rH, "rL": rL}

    return None

# ── BUILD SETUP ──────────────────────────────────────────────────────────────
def build_setup(pair, level, crt, trend, tf, confluence_lv, confluence_tf):
    bull  = level["type"] == "V"
    entry = level["price"]
    sw    = crt["sw"]; cf = crt["cf"]; br = crt["br"]

    if bull:
        sl   = sw["l"] * 0.9992
        risk = abs(entry - sl)
        tp1  = entry + risk * 3
        tp2  = entry + risk * 7
        tp3  = entry + risk * 12
    else:
        sl   = sw["h"] * 1.0008
        risk = abs(sl - entry)
        tp1  = entry - risk * 3
        tp2  = entry - risk * 7
        tp3  = entry - risk * 12

    rr1 = abs(tp1 - entry) / risk
    rr2 = abs(tp2 - entry) / risk
    if rr1 < MIN_RR: return None

    # Order prices
    ord1 = cf["c"]
    ord2 = sw["o"]
    ord3 = level["price"]

    # Grading
    score = 0
    if level["freshness"] == "FRESH":   score += 2
    if level["hsl"]:                    score += 1
    if confluence_lv is not None:       score += 2
    if br > 0.65:                       score += 1
    if level["dist"] < 3:               score += 1  # very close to current price

    # Grade by timeframe + score
    if tf == "1D" and confluence_lv:
        grade = "A+"
    elif tf == "1D":
        grade = "A+" if score >= 6 else "A"
    elif tf == "4H" and confluence_lv:
        grade = "A"
    else:
        grade = "A" if score >= 4 else "B"

    pf = lambda p: f"${p:,.0f}"

    return {
        "pair":         pair,
        "direction":    "LONG" if bull else "SHORT",
        "grade":        grade,
        "score":        score,
        "tf":           tf,
        "entry":        pf(entry),
        "sl":           pf(sl),
        "tp1":          pf(tp1),
        "tp2":          pf(tp2),
        "tp3":          pf(tp3),
        "ord1":         pf(ord1),
        "ord2":         pf(ord2),
        "ord3":         pf(ord3),
        "rr1":          f"{rr1:.1f}",
        "rr2":          f"{rr2:.1f}",
        "level":        pf(level["price"]),
        "level_type":   level["type"],
        "freshness":    level["freshness"],
        "hsl":          level["hsl"],
        "confluence":   confluence_lv is not None,
        "conf_tf":      confluence_tf,
        "conf_level":   pf(confluence_lv["price"]) if confluence_lv else "None",
        "br":           f"{br:.2f}",
        "trend":        trend,
        "raw_level":    level["price"],
    }

# ── ANTI-SPAM ────────────────────────────────────────────────────────────────
def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f: return json.load(f)
    except: pass
    return {}

def save_state(state):
    try:
        with open(STATE_FILE, "w") as f: json.dump(state, f)
    except Exception as e:
        print(f"State error: {e}")

def already_alerted(state, pair, tf, level_price):
    key = f"{pair}_{tf}_{int(level_price)}"
    if key not in state: return False
    return (datetime.now(timezone.utc).timestamp() - state[key]) < SPAM_HOURS * 3600

def mark_alerted(state, pair, tf, level_price):
    key = f"{pair}_{tf}_{int(level_price)}"
    state[key] = datetime.now(timezone.utc).timestamp()

# ── TELEGRAM ─────────────────────────────────────────────────────────────────
def send_telegram(msg):
    if not TELEGRAM_TOKEN:
        print("No token:"); print(msg); return
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"
    }).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=10) as r:
            res = json.loads(r.read())
        print("Telegram sent" if res.get("ok") else f"Telegram error: {res}")
    except Exception as e:
        print(f"Telegram error: {e}")

def format_msg(s):
    em  = {"A+": "A+", "A": "A", "B": "B"}
    de  = "LONG" if s["direction"] == "LONG" else "SHORT"
    conf_line = f"YES - {s['conf_tf']} level @ {s['conf_level']}" if s["confluence"] else "NO"
    return "\n".join([
        f"MSNR + CRT SETUP — {s['pair']}",
        f"",
        f"Grade: {s['grade']}  |  {de}  |  {s['tf']} level",
        f"",
        f"Entry:  {s['entry']}",
        f"SL:     {s['sl']}",
        f"TP1:    {s['tp1']}  (+{s['rr1']}R)",
        f"TP2:    {s['tp2']}  (+{s['rr2']}R)",
        f"TP3:    {s['tp3']}  (trail)",
        f"",
        f"3 LIMIT ORDERS:",
        f"Order 1 (20%):  {s['ord1']}",
        f"Order 2 (35%):  {s['ord2']}",
        f"Order 3 (45%):  {s['ord3']}  best",
        f"",
        f"CONFLUENCES:",
        f"MSNR Level:    {s['level']}  {s['freshness']}",
        f"Confluence:    {conf_line}",
        f"HSL:           {'YES' if s['hsl'] else 'NO'}",
        f"Body ratio:    {s['br']}",
        f"Trend:         {s['trend']}",
        f"",
        f"{datetime.utcnow().strftime('%d %b %Y  %H:%M UTC')}",
        f"",
        f"Verify on TradingView then place orders.",
    ])

# ── SCAN PAIR ─────────────────────────────────────────────────────────────────
def scan_pair(symbol, pair, state):
    print(f"\nFetching {pair} data...")
    d1 = fetch_candles(symbol, "1d", 90);  time.sleep(2)
    h4 = fetch_candles(symbol, "4h", 150); time.sleep(2)
    h1 = fetch_candles(symbol, "1h", 150)

    if not (d1 and h4 and h1):
        print(f"{pair}: Insufficient data"); return []

    trend_1d = get_trend(d1)
    trend_4h = get_trend(h4)
    print(f"{pair} trend 1D: {trend_1d}  |  4H: {trend_4h}")

    if trend_1d == "RANGING" and trend_4h == "RANGING":
        print(f"{pair}: Both timeframes ranging — skipping"); return []

    # Use 1D trend as primary, fall back to 4H if 1D is ranging
    primary_trend = trend_1d if trend_1d != "RANGING" else trend_4h

    lvl_1d = find_msnr_levels(d1, lb=2, max_dist=12)
    lvl_4h = find_msnr_levels(h4, lb=3, max_dist=8)
    lvl_1h = find_msnr_levels(h1, lb=3, max_dist=5)

    print(f"{pair} levels — 1D: {len(lvl_1d)}  4H: {len(lvl_4h)}  1H: {len(lvl_1h)}")

    setups = []
    seen   = set()  # avoid duplicate setups at same price

    # ── PASS 1: 1D levels ────────────────────────────────────────────────────
    for lv in lvl_1d:
        key = f"1D_{int(lv['price'])}"
        if key in seen: continue
        if already_alerted(state, pair, "1D", lv["price"]):
            print(f"  1D {lv['type']}-level ${lv['price']:,.0f} — alerted recently"); continue

        conf4h = find_confluence(lv, lvl_4h, pct=1.0)
        crt    = find_crt(h1, lv, primary_trend)

        if crt:
            setup = build_setup(pair, lv, crt, primary_trend, "1D", conf4h, "4H")
            if setup:
                conf_txt = "+ 4H confluence" if conf4h else ""
                print(f"  SETUP 1D {lv['type']}-level ${lv['price']:,.0f} "
                      f"{setup['direction']} {setup['grade']} TP2:{setup['tp2']} {conf_txt}")
                setups.append(setup)
                mark_alerted(state, pair, "1D", lv["price"])
                seen.add(key)
        else:
            print(f"  1D {lv['type']}-level ${lv['price']:,.0f} — no CRT")

    # ── PASS 2: 4H levels (only if not already covered by 1D) ───────────────
    for lv in lvl_4h:
        # Skip if a 1D level already covers this price area
        already_covered = any(
            abs(lv["price"] - s["raw_level"]) / lv["price"] < 0.01
            for s in setups
        )
        if already_covered: continue

        key = f"4H_{int(lv['price'])}"
        if key in seen: continue
        if already_alerted(state, pair, "4H", lv["price"]):
            print(f"  4H {lv['type']}-level ${lv['price']:,.0f} — alerted recently"); continue

        crt = find_crt(h1, lv, primary_trend)
        if crt:
            setup = build_setup(pair, lv, crt, primary_trend, "4H", None, None)
            if setup:
                print(f"  SETUP 4H {lv['type']}-level ${lv['price']:,.0f} "
                      f"{setup['direction']} {setup['grade']} TP2:{setup['tp2']}")
                setups.append(setup)
                mark_alerted(state, pair, "4H", lv["price"])
                seen.add(key)
        else:
            print(f"  4H {lv['type']}-level ${lv['price']:,.0f} — no CRT")

    return setups

# ── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    print(f"=== MSNR Scanner v4  {datetime.utcnow().isoformat()} ===")
    state     = load_state()
    all_found = []

    # BTC
    btc_setups = scan_pair("BTC", "BTCUSDT", state)
    all_found.extend(btc_setups)

    # Gold — skipped (no free API for GitHub Actions)
    print("\nGold: Skipped (BTC-only mode)")

    # Send alerts
    print()
    if all_found:
        all_found.sort(key=lambda x: -x["score"])
        for s in all_found:
            send_telegram(format_msg(s))
            time.sleep(1)
        save_state(state)
        print(f"Sent {len(all_found)} alert(s)")
    else:
        print("No setups — no alert sent")

    print("=== Scan complete ===")

if __name__ == "__main__":
    main()
