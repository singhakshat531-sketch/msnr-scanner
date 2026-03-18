#!/usr/bin/env python3
"""
MSNR + CRT Scanner v3 — GitHub Actions
Elite Observer — High RR Setup Finder

Logic:
- Scans every 15 minutes
- REQUIRED: Fresh 1D MSNR level + 1H CRT confirmed + 1D trend not ranging
- BONUS: 4H MSNR confluence, HSL, strong body ratio
- GRADES: A+ / A / B
- ANTI-SPAM: no duplicate alerts for same level within 6 hours
"""

import json
import urllib.request
import urllib.parse
import os
import time
import hashlib
from datetime import datetime, timezone

# ── CONFIG ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "1247283950")
MIN_RR           = 3.0
LOOKBACK_1D      = 2
LOOKBACK_4H      = 3
LOOKBACK_1H      = 3
CONFLUENCE_PCT   = 1.0   # 4H level within 1% of 1D level = stacked
SPAM_HOURS       = 6     # don't re-alert same setup within 6 hours
STATE_FILE       = "last_alerts.json"

# ── FETCH ────────────────────────────────────────────────────────────────────
def fetch_candles(symbol, interval, limit=120):
    endpoint  = "histoday" if interval == "1d" else "histohour"
    aggregate = {"1d":1, "4h":4, "1h":1}.get(interval, 1)
    url = (f"https://min-api.cryptocompare.com/data/v2/{endpoint}"
           f"?fsym={symbol}&tsym=USD&limit={limit}&aggregate={aggregate}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MSNR-Scanner/3.0"})
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
def find_msnr_levels(candles, lb=3, max_dist=10.0):
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
    return sorted(levels, key=lambda x: x["dist"])[:6]

# ── 4H CONFLUENCE CHECK ──────────────────────────────────────────────────────
def find_4h_confluence(level_1d, levels_4h, pct=CONFLUENCE_PCT):
    """Check if any 4H level is within pct% of the 1D level."""
    for lv4 in levels_4h:
        if lv4["type"] != level_1d["type"]: continue  # same direction
        dist = abs(lv4["price"] - level_1d["price"]) / level_1d["price"] * 100
        if dist <= pct:
            return lv4
    return None

# ── CRT DETECTION ────────────────────────────────────────────────────────────
def find_crt_setup(c1h, level_1d, level_4h, trend, pair):
    """
    Scan 1H candles for CRT pattern at 1D MSNR level.
    Returns setup dict or None.
    """
    if len(c1h) < 15 or trend == "RANGING": return None

    bull = level_1d["type"] == "V"
    if bull  and trend != "BULLISH": return None
    if not bull and trend != "BEARISH": return None

    scan_start = max(5, len(c1h) - 36)  # look back 36 1H candles = 1.5 days

    for i in range(scan_start, len(c1h) - 1):
        # Define external range
        rs = i - 1
        while rs > 1:
            a, b = c1h[rs], c1h[rs-1]
            if bull     and a["c"] >= b["c"]: break
            if not bull and a["c"] <= b["c"]: break
            rs -= 1

        seg = c1h[rs:i+1]
        rH  = max(x["h"] for x in seg)
        rL  = min(x["l"] for x in seg)
        cur = c1h[-1]["c"]

        if (rH - rL) / cur < 0.002: continue  # range too small

        sw = c1h[i]
        cf = c1h[i+1]

        # CRT-2: wick sweeps 1D MSNR level, close back inside range
        if bull:
            crt2 = sw["l"] <= level_1d["price"] and sw["c"] > level_1d["price"] and sw["c"] > rL
        else:
            crt2 = sw["h"] >= level_1d["price"] and sw["c"] < level_1d["price"] and sw["c"] < rH
        if not crt2: continue

        # CRT-3: decisive body closes beyond entire range
        body = abs(cf["c"] - cf["o"])
        cr   = cf["h"] - cf["l"]
        br   = body / cr if cr > 0 else 0

        if bull:
            crt3 = cf["c"] > rH and br > 0.5
        else:
            crt3 = cf["c"] < rL and br > 0.5
        if not crt3: continue

        # Valid CRT — calculate trade levels
        entry = level_1d["price"]
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
        if rr1 < MIN_RR: continue

        # Three limit order prices
        ord1 = cf["c"]            # range breakout level
        ord2 = sw["o"]            # shared CRT-2/3 open
        ord3 = level_1d["price"]  # 1D MSNR level (best entry)

        # Grading
        score = 0
        if level_1d["freshness"] == "FRESH": score += 2
        if level_1d["hsl"]:                  score += 1
        if level_4h is not None:             score += 2  # 4H confluence = big boost
        if br > 0.65:                        score += 1
        if i >= len(c1h) - 8:               score += 1  # recent confirmation

        grade = "A+" if score >= 6 else "A" if score >= 3 else "B"

        pf = lambda p: f"${p:,.0f}"  # BTC price format

        return {
            "pair":        pair,
            "direction":   "LONG" if bull else "SHORT",
            "grade":       grade,
            "score":       score,
            "entry":       pf(entry),
            "sl":          pf(sl),
            "tp1":         pf(tp1),
            "tp2":         pf(tp2),
            "tp3":         pf(tp3),
            "ord1":        pf(ord1),
            "ord2":        pf(ord2),
            "ord3":        pf(ord3),
            "rr1":         f"{rr1:.1f}",
            "rr2":         f"{rr2:.1f}",
            "level_1d":    pf(level_1d["price"]),
            "level_4h":    pf(level_4h["price"]) if level_4h else "None",
            "confluence":  level_4h is not None,
            "freshness":   level_1d["freshness"],
            "hsl":         level_1d["hsl"],
            "br":          f"{br:.2f}",
            "trend":       trend,
            "raw_entry":   entry,
            "raw_level":   level_1d["price"],
        }

    return None

# ── ANTI-SPAM ────────────────────────────────────────────────────────────────
def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                return json.load(f)
    except: pass
    return {}

def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print(f"State save error: {e}")

def already_alerted(state, pair, level_price):
    """Return True if same pair+level was alerted within SPAM_HOURS."""
    key = f"{pair}_{int(level_price)}"
    if key not in state: return False
    last = state[key]
    now  = datetime.now(timezone.utc).timestamp()
    return (now - last) < SPAM_HOURS * 3600

def mark_alerted(state, pair, level_price):
    key = f"{pair}_{int(level_price)}"
    state[key] = datetime.now(timezone.utc).timestamp()

# ── TELEGRAM ─────────────────────────────────────────────────────────────────
def send_telegram(msg):
    if not TELEGRAM_TOKEN:
        print("No token — message:")
        print(msg)
        return
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       msg,
        "parse_mode": "HTML",
    }).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=10) as r:
            res = json.loads(r.read())
        print("Telegram sent ✓" if res.get("ok") else f"Telegram error: {res}")
    except Exception as e:
        print(f"Telegram error: {e}")

def format_msg(s):
    em  = {"A+": "🟢", "A": "🔵", "B": "🟡"}.get(s["grade"], "⚪")
    de  = "📈" if s["direction"] == "LONG" else "📉"
    conf = "✓ YES" if s["confluence"] else "✗ NO"
    return "\n".join([
        f"⚡ <b>MSNR + CRT SETUP DETECTED</b>",
        "",
        f"{em} <b>{s['pair']} · {s['direction']} · {s['grade']}</b> {de}",
        "",
        f"<b>Entry zone:</b>  {s['entry']}",
        f"<b>Stop Loss:</b>   {s['sl']}",
        f"<b>TP1:</b>         {s['tp1']}  <i>(+{s['rr1']}R)</i>",
        f"<b>TP2:</b>         {s['tp2']}  <i>(+{s['rr2']}R)</i>",
        f"<b>TP3:</b>         {s['tp3']}  <i>(trail)</i>",
        "",
        "─── 3 LIMIT ORDERS ───",
        f"Order 1 (20%):  {s['ord1']}",
        f"Order 2 (35%):  {s['ord2']}",
        f"Order 3 (45%):  {s['ord3']} ← best",
        "",
        "─── CONFLUENCES ───",
        f"<b>1D MSNR:</b>      {s['level_1d']} · {s['freshness']}",
        f"<b>4H Confluence:</b> {conf} ({s['level_4h']})",
        f"<b>HSL:</b>          {'✓ YES' if s['hsl'] else '✗ NO'}",
        f"<b>CRT body ratio:</b> {s['br']}",
        f"<b>Trend:</b>        {s['trend']}",
        "",
        f"🕐 {datetime.utcnow().strftime('%d %b %Y · %H:%M UTC')}",
        "",
        "→ Verify on TradingView first",
        "→ Place limit orders · Walk away",
    ])

# ── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    print(f"=== MSNR Scanner v3 · {datetime.utcnow().isoformat()} ===")
    state     = load_state()
    all_found = []

    # ── BTC ──────────────────────────────────────────────────────────────────
    print("\nFetching BTC data...")
    d1 = fetch_candles("BTC", "1d", 60);  time.sleep(2)
    h4 = fetch_candles("BTC", "4h", 120); time.sleep(2)
    h1 = fetch_candles("BTC", "1h", 120)

    if not (d1 and h4 and h1):
        print("BTC: Insufficient data — skipping")
    else:
        tr = get_trend(d1)
        print(f"BTC trend: {tr}")

        if tr == "RANGING":
            print("BTC: Market ranging — no setups possible")
        else:
            lvl_1d = find_msnr_levels(d1, lb=LOOKBACK_1D)
            lvl_4h = find_msnr_levels(h4, lb=LOOKBACK_4H)
            print(f"BTC 1D levels: {len(lvl_1d)}")
            print(f"BTC 4H levels: {len(lvl_4h)}")

            for lv in lvl_1d:
                # Check anti-spam first
                if already_alerted(state, "BTCUSDT", lv["price"]):
                    print(f"  Skipping {lv['type']}-level ${lv['price']:,.0f} — alerted recently")
                    continue

                # Find 4H confluence
                lv4h = find_4h_confluence(lv, lvl_4h)

                # Scan 1H for CRT
                setup = find_crt_setup(h1, lv, lv4h, tr, "BTCUSDT")

                if setup:
                    conf_txt = f"+ 4H confluence" if lv4h else ""
                    print(f"  ✓ SETUP: {setup['direction']} {setup['grade']} "
                          f"Entry:{setup['entry']} TP2:{setup['tp2']} "
                          f"+{setup['rr2']}R {conf_txt}")
                    all_found.append(setup)
                    mark_alerted(state, "BTCUSDT", lv["price"])
                else:
                    print(f"  {lv['type']}-level ${lv['price']:,.0f} — no CRT setup")

    # ── GOLD — skipped ────────────────────────────────────────────────────────
    print("\nGold: Skipped (BTC-only mode)")

    # ── ALERTS ───────────────────────────────────────────────────────────────
    print()
    if all_found:
        # Sort by grade score, send best first
        all_found.sort(key=lambda x: -x["score"])
        for setup in all_found:
            msg = format_msg(setup)
            send_telegram(msg)
            time.sleep(1)
        save_state(state)
    else:
        send_telegram("✅ MSNR Scanner is live and working!")
        print("No setups — no alert sent")

    print("=== Scan complete ===")

if __name__ == "__main__":
    main()
