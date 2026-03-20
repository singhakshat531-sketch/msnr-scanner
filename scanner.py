#!/usr/bin/env python3
"""
MSNR Scanner v6 — GitHub Actions
Elite Observer

SIMPLIFIED RELIABLE APPROACH:
- Detects 4H MSNR levels (proven working)
- Alerts when price is NEAR a fresh level (within 0.5%)
- YOU check the chart for CRT manually
- No false negatives from over-strict CRT detection

Alert fires when:
1. Price within 0.5% of fresh 4H MSNR level
2. Bias aligns (bullish = V-levels, bearish = A-levels)
3. Level not alerted in last 4 hours
"""

import json
import urllib.request
import urllib.parse
import os
import time
import base64
from datetime import datetime, timezone

# ── CONFIG ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "1247283950")
GITHUB_PAT       = os.environ.get("PAT_TOKEN", "")
GITHUB_REPO      = "singhakshat531-sketch/msnr-scanner"
DATA_FILE        = "trades.json"
NEAR_PCT         = 0.5   # alert when within 0.5% of level
SPAM_HOURS       = 4     # don't re-alert same level within 4 hours
SIM_START        = 1000.0
RISK_PCT         = {"aplus":3.0, "a":2.0, "b":1.0}

# ── GITHUB ────────────────────────────────────────────────────────────────────
def github_read(filename):
    if not GITHUB_PAT:
        try:
            with open(filename) as f: return json.load(f), None
        except: return None, None
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"token {GITHUB_PAT}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "MSNR-Scanner/6.0"
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        return json.loads(base64.b64decode(data["content"]).decode()), data["sha"]
    except urllib.error.HTTPError as e:
        if e.code == 404: return None, None
        print(f"  GitHub read error: {e}"); return None, None
    except Exception as e:
        print(f"  GitHub read error: {e}"); return None, None

def github_write(filename, content, sha=None):
    if not GITHUB_PAT:
        with open(filename, "w") as f: json.dump(content, f, indent=2)
        return True
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
    body = {
        "message": f"scanner {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
        "content": base64.b64encode(json.dumps(content, indent=2).encode()).decode(),
        "branch": "main"
    }
    if sha: body["sha"] = sha
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={
            "Authorization": f"token {GITHUB_PAT}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json",
            "User-Agent": "MSNR-Scanner/6.0"
        }, method="PUT"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            json.loads(r.read())
        print(f"  GitHub write OK: {filename}"); return True
    except Exception as e:
        print(f"  GitHub write error: {e}"); return False

def load_state():
    data, sha = github_read(DATA_FILE)
    if data is None:
        print("  Creating fresh state")
        data = {
            "balance": SIM_START,
            "activeTrades": [],
            "history": [],
            "equityCurve": [{"t": datetime.utcnow().isoformat(), "v": SIM_START}],
            "alerts": {},
            "stats": {"totalTrades":0,"wins":0,"losses":0,"be":0,"netR":0.0,"bestR":0.0},
            "lastUpdated": datetime.utcnow().isoformat(),
            "levelAlerts": {}
        }
    if "levelAlerts" not in data:
        data["levelAlerts"] = {}
    return data, sha

def save_state(data, sha):
    data["lastUpdated"] = datetime.utcnow().isoformat()
    return github_write(DATA_FILE, data, sha)

# ── FETCH ─────────────────────────────────────────────────────────────────────
def fetch_candles(symbol, interval, limit=150):
    endpoint  = "histoday" if interval == "1d" else "histohour"
    aggregate = {"1d":1, "4h":4, "1h":1}.get(interval, 1)
    url = (f"https://min-api.cryptocompare.com/data/v2/{endpoint}"
           f"?fsym={symbol}&tsym=USD&limit={limit}&aggregate={aggregate}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MSNR-Scanner/6.0"})
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

# ── TREND + BIAS ───────────────────────────────────────────────────────────────
def get_trend(candles, lookback=20):
    if len(candles) < lookback: return "UNKNOWN"
    c = [x["c"] for x in candles[-lookback:]]
    half = len(c) // 2
    avg_s = sum(c[:half]) / half
    avg_e = sum(c[half:]) / half
    pct   = (avg_e - avg_s) / avg_s * 100
    bull  = sum(1 for i in range(2,len(c)) if c[i]>c[i-1]>c[i-2])
    bear  = sum(1 for i in range(2,len(c)) if c[i]<c[i-1]<c[i-2])
    if pct > 3  and bull > bear:        return "BULLISH"
    if pct < -3 and bear > bull:        return "BEARISH"
    if pct > 2  and bull > bear * 1.5:  return "BULLISH"
    if pct < -2 and bear > bull * 1.5:  return "BEARISH"
    return "RANGING"

def get_bias(tr1d, tr4h):
    if tr1d == "BULLISH": return "BULLISH"
    if tr1d == "BEARISH": return "BEARISH"
    if tr1d == "RANGING":
        if tr4h == "BULLISH": return "BULLISH"
        if tr4h == "BEARISH": return "BEARISH"
    return "RANGING"

# ── MSNR LEVELS ───────────────────────────────────────────────────────────────
def find_msnr_levels(candles, lb=2, max_dist=15.0):
    if len(candles) < lb*2 + 2: return []
    closes = [c["c"] for c in candles]
    cur    = closes[-1]
    levels = []
    for i in range(lb, len(closes) - lb):
        is_a = all(closes[i]>closes[i-j] and closes[i]>closes[i+j] for j in range(1,lb+1))
        is_v = all(closes[i]<closes[i-j] and closes[i]<closes[i+j] for j in range(1,lb+1))
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
        hsl = sum(1 for pc in closes[:i] if abs(pc-price)/price < 0.005) >= 2
        levels.append({
            "type": t, "price": price,
            "freshness": "FRESH" if wicks == 0 else "UNFRESH",
            "hsl": hsl, "dist": dist, "wicks": wicks
        })
    return sorted(levels, key=lambda x: x["dist"])[:8]

# ── NEAR LEVEL CHECK ──────────────────────────────────────────────────────────
def is_near_level(cur_price, level_price, pct=NEAR_PCT):
    return abs(cur_price - level_price) / level_price * 100 <= pct

def already_alerted(state, level_key):
    alerts = state.get("levelAlerts", {})
    if level_key not in alerts: return False
    try:
        last = datetime.fromisoformat(alerts[level_key])
        now  = datetime.utcnow()
        return (now - last).total_seconds() < SPAM_HOURS * 3600
    except: return False

def mark_alerted(state, level_key):
    state.setdefault("levelAlerts", {})[level_key] = datetime.utcnow().isoformat()

# ── GRADE LEVEL ───────────────────────────────────────────────────────────────
def grade_level(lv, lv_1d_near=None):
    score = 0
    if lv["freshness"] == "FRESH": score += 2
    if lv["hsl"]:                  score += 1
    if lv_1d_near:                 score += 2
    if lv["dist"] < 0.3:           score += 1  # very close
    grade = "aplus" if score >= 5 else "a" if score >= 3 else "b"
    return grade, score

# ── TELEGRAM ──────────────────────────────────────────────────────────────────
def send_telegram(msg):
    if not TELEGRAM_TOKEN:
        print("No token — message:"); print(msg); return
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg,
        "parse_mode": "HTML"
    }).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=10) as r:
            res = json.loads(r.read())
        print("Telegram sent ✓" if res.get("ok") else f"Telegram error: {res}")
    except Exception as e:
        print(f"Telegram error: {e}")

def format_retest_alert(lv, cur_price, bias, grade, tr1d, tr4h, lv_1d_target, bl):
    """Alert for when price retests a broken level — highest quality setup."""
    bull = lv["type"] == "V"
    grade_label = "A+" if grade == "aplus" else grade.upper()
    pf = lambda p: f"${p:,.0f}"
    entry = lv["price"]
    sl_est = entry * (0.997 if bull else 1.003)
    risk_est = abs(entry - sl_est)
    tp1_est = entry + risk_est*3 if bull else entry - risk_est*3
    tp2_est = lv_1d_target["price"] if lv_1d_target else (
        entry + risk_est*7 if bull else entry - risk_est*7
    )
    dist_pct = abs(cur_price - entry) / entry * 100

    return "\n".join([
        f"🔥 RETEST ALERT — {'LONG' if bull else 'SHORT'}",
        f"",
        f"Grade: {grade_label}  |  BTCUSDT",
        f"",
        f"Price:    {pf(cur_price)}",
        f"Level:    {pf(entry)}  ({dist_pct:.2f}% away)",
        f"Type:     {lv['type']}-Level RETEST",
        f"Broke at: {bl.get('brokeAt','')[:16].replace('T',' ')} UTC",
        f"",
        f"Est Entry: {pf(entry)}",
        f"Est SL:    {pf(sl_est)}",
        f"Est TP1:   {pf(tp1_est)}  (+3R)",
        f"Est TP2:   {pf(tp2_est)}",
        f"",
        f"Bias: {bias}  (1D:{tr1d} 4H:{tr4h})",
        f"",
        f"→ HIGH QUALITY SETUP — RANGE BREAK + RETEST",
        f"→ Check 1H for confirmation candle",
        f"→ Enter on confirmation only",
        f"",
        f"{datetime.utcnow().strftime('%d %b %Y  %H:%M UTC')}",
        f"singhakshat531-sketch.github.io/msnr-scanner",
    ])

def format_alert(lv, cur_price, bias, grade, tr1d, tr4h, lv_1d_target):
    bull = lv["type"] == "V"
    direction = "LONG" if bull else "SHORT"
    grade_label = "A+" if grade == "aplus" else grade.upper()
    pf = lambda p: f"${p:,.0f}"

    # Estimated levels (rough guide for manual trading)
    entry = lv["price"]
    sl_est = entry * (0.997 if bull else 1.003)
    risk_est = abs(entry - sl_est)
    tp1_est = entry + risk_est*3 if bull else entry - risk_est*3
    tp2_est = lv_1d_target["price"] if lv_1d_target else (
        entry + risk_est*7 if bull else entry - risk_est*7
    )

    dist_pct = abs(cur_price - entry) / entry * 100

    return "\n".join([
        f"⚡ MSNR LEVEL ALERT — {direction}",
        f"",
        f"Grade: {grade_label}  |  BTCUSDT",
        f"",
        f"Price now:  {pf(cur_price)}",
        f"Level:      {pf(entry)}  ({dist_pct:.2f}% away)",
        f"Type:       {lv['type']}-Level ({lv['freshness']})",
        f"HSL:        {'YES ✓' if lv['hsl'] else 'NO'}",
        f"",
        f"Est. Entry: {pf(entry)}",
        f"Est. SL:    {pf(sl_est)}",
        f"Est. TP1:   {pf(tp1_est)}  (+3R)",
        f"Est. TP2:   {pf(tp2_est)}",
        f"",
        f"Bias: {bias}  (1D:{tr1d} 4H:{tr4h})",
        f"",
        f"→ CHECK 1H CHART FOR CRT PATTERN",
        f"→ Verify sweep + confirmation candle",
        f"→ Trade only if CRT confirms",
        f"",
        f"{datetime.utcnow().strftime('%d %b %Y  %H:%M UTC')}",
        f"singhakshat531-sketch.github.io/msnr-scanner",
    ])

# ── SIMULATOR UPDATE ──────────────────────────────────────────────────────────
def update_simulator(state, cur_price):
    """Update any open paper trades with current price."""
    still_active = []
    for t in state["activeTrades"]:
        bull    = t.get("bull", t["direction"] == "LONG")
        entry   = t["rawEntry"]
        sl      = t["rawSL"]
        tp1     = t["rawTP1"]
        tp2     = t["rawTP2"]
        risk    = t.get("rawRisk", abs(entry - sl))
        if risk <= 0: still_active.append(t); continue
        riskPct = RISK_PCT.get(t["grade"], 1.0)
        riskAmt = t.get("entryBalance", SIM_START) * (riskPct / 100)
        posSize = riskAmt / risk
        phase   = t.get("phase", "entry")

        if phase == "entry":
            if (bull and cur_price <= sl) or (not bull and cur_price >= sl):
                state["balance"] += -riskAmt
                state["stats"]["losses"]      += 1
                state["stats"]["totalTrades"] += 1
                state["stats"]["netR"]         = round(state["stats"]["netR"] - 1, 2)
                state["history"].insert(0, {**t, "result":"loss",
                    "pnl":round(-riskAmt,2), "pnlR":-1.0,
                    "closePrice":round(cur_price,2),
                    "closedAt":datetime.utcnow().isoformat(), "note":"SL hit"})
                state["equityCurve"].append({"t":datetime.utcnow().isoformat(),
                    "v":round(state["balance"],2)})
                continue
            if (bull and cur_price >= tp1) or (not bull and cur_price <= tp1):
                profit = posSize * abs(tp1-entry) * 0.5
                state["balance"] += profit
                t["phase"]  = "tp1"
                t["rawSL"]  = entry
                state["stats"]["netR"] = round(state["stats"]["netR"] + 1.5, 2)
                state["history"].insert(0, {**t, "result":"tp1",
                    "pnl":round(profit,2), "pnlR":1.5,
                    "closePrice":round(cur_price,2),
                    "closedAt":datetime.utcnow().isoformat(),
                    "note":"TP1 — 50% closed, SL to BE"})
                state["equityCurve"].append({"t":datetime.utcnow().isoformat(),
                    "v":round(state["balance"],2)})
                still_active.append(t); continue

        elif phase == "tp1":
            if (bull and cur_price <= entry) or (not bull and cur_price >= entry):
                state["stats"]["be"]          += 1
                state["stats"]["totalTrades"] += 1
                state["history"].insert(0, {**t, "result":"be",
                    "pnl":0.0, "pnlR":0.0,
                    "closePrice":round(cur_price,2),
                    "closedAt":datetime.utcnow().isoformat(), "note":"BE stop"})
                state["equityCurve"].append({"t":datetime.utcnow().isoformat(),
                    "v":round(state["balance"],2)})
                continue
            if (bull and cur_price >= tp2) or (not bull and cur_price <= tp2):
                profit = posSize * abs(tp2-entry) * 0.5
                netR   = round(1.5 + abs(tp2-entry)/risk*0.5, 1)
                state["balance"] += profit
                state["stats"]["wins"]        += 1
                state["stats"]["totalTrades"] += 1
                state["stats"]["netR"]         = round(state["stats"]["netR"]+netR, 2)
                state["stats"]["bestR"]        = max(state["stats"]["bestR"], netR)
                state["history"].insert(0, {**t, "result":"win",
                    "pnl":round(profit,2), "pnlR":netR,
                    "closePrice":round(cur_price,2),
                    "closedAt":datetime.utcnow().isoformat(),
                    "note":f"TP2 hit +{netR}R"})
                state["equityCurve"].append({"t":datetime.utcnow().isoformat(),
                    "v":round(state["balance"],2)})
                continue

        still_active.append(t)

    state["activeTrades"] = still_active
    state["history"]      = state["history"][:100]
    state["equityCurve"]  = state["equityCurve"][-200:]

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    print(f"=== MSNR Scanner v6  {datetime.utcnow().isoformat()} ===")

    # Load state
    print("\nLoading state...")
    state, sha = load_state()
    print(f"  Balance: ${state['balance']:.2f} | "
          f"Active: {len(state['activeTrades'])} | "
          f"History: {len(state['history'])}")

    # Fetch data
    print("\nFetching BTC data...")
    d1 = fetch_candles("BTC", "1d", 60);   time.sleep(2)
    h4 = fetch_candles("BTC", "4h", 200);  time.sleep(2)
    h1 = fetch_candles("BTC", "1h", 100)

    if not (d1 and h4):
        print("Insufficient data"); return

    cur_price = h1[-1]["c"] if h1 else h4[-1]["c"]
    tr1d      = get_trend(d1, lookback=20)
    tr4h      = get_trend(h4, lookback=42)
    bias      = get_bias(tr1d, tr4h)

    print(f"\nBTC: ${cur_price:,.0f}")
    print(f"1D: {tr1d} | 4H: {tr4h} | Bias: {bias}")

    # Detect levels
    lvl_4h = find_msnr_levels(h4, lb=2, max_dist=15.0)
    lvl_1d = find_msnr_levels(d1, lb=2, max_dist=20.0)

    print(f"\n4H levels: {len(lvl_4h)}")
    for l in lvl_4h:
        near = "← NEAR" if is_near_level(cur_price, l["price"]) else ""
        print(f"  {l['type']}-level ${l['price']:,.0f} | "
              f"{l['freshness']} | HSL:{l['hsl']} | "
              f"dist:{l['dist']:.2f}% {near}")

    # ── RANGE BREAK + RETEST DETECTION ──────────────────────────────────────
    alerts_sent = 0
    broken_levels = state.get("brokenLevels", {})

    if bias != "RANGING":
        for lv in lvl_4h:
            bull = lv["type"] == "V"
            if bull  and bias != "BULLISH": continue
            if not bull and bias != "BEARISH": continue

            key = f"BTCUSDT_{lv['type']}_{int(lv['price'])}"

            # Find 1D target
            lv_1d_target = next((
                l for l in sorted(lvl_1d, key=lambda x: abs(x["price"]-lv["price"]))
                if l["type"] == lv["type"]
                and ((bull and l["price"] > lv["price"])
                     or (not bull and l["price"] < lv["price"]))
            ), None)

            grade, score = grade_level(lv, lv_1d_target)

            # ── SCENARIO 1: RANGE BREAK ──────────────────────────────────
            # Price has broken above/below the 4H level
            # Record it so we can watch for retest
            if bull:
                # V-level: watch for break above (price closes above level)
                broke_above = h4 and h4[-1]["c"] > lv["price"] * 1.005
            else:
                # A-level: watch for break below (price closes below level)
                broke_above = h4 and h4[-1]["c"] < lv["price"] * 0.995

            if broke_above and key not in broken_levels:
                broken_levels[key] = {
                    "level": lv["price"],
                    "type": lv["type"],
                    "direction": "LONG" if bull else "SHORT",
                    "brokeAt": datetime.utcnow().isoformat(),
                    "grade": grade,
                    "lv_1d_target": lv_1d_target
                }
                print(f"  Range break recorded: {lv['type']}-level ${lv['price']:,.0f}")

            # ── SCENARIO 2: RETEST OF BROKEN LEVEL ───────────────────────
            # Price has come back to test the broken level
            if key in broken_levels:
                bl = broken_levels[key]
                is_retest = is_near_level(cur_price, bl["level"], pct=0.5)

                if is_retest:
                    if not already_alerted(state, key + "_retest"):
                        msg = format_retest_alert(
                            lv, cur_price, bias, grade,
                            tr1d, tr4h, lv_1d_target, bl
                        )
                        send_telegram(msg)
                        mark_alerted(state, key + "_retest")
                        alerts_sent += 1
                        print(f"\n  ✓ RETEST ALERT: {lv['type']}-level "
                              f"${lv['price']:,.0f} {grade.upper()}")

            # ── SCENARIO 3: PROXIMITY ALERT (backup) ─────────────────────
            # Price approaching level for the first time
            elif is_near_level(cur_price, lv["price"]):
                if not already_alerted(state, key):
                    msg = format_alert(lv, cur_price, bias, grade, tr1d, tr4h, lv_1d_target)
                    send_telegram(msg)
                    mark_alerted(state, key)
                    alerts_sent += 1
                    print(f"\n  ✓ PROXIMITY ALERT: {lv['type']}-level "
                          f"${lv['price']:,.0f} {grade.upper()}")
                else:
                    print(f"  {lv['type']}-level ${lv['price']:,.0f} — alerted recently")
            else:
                print(f"  {lv['type']}-level ${lv['price']:,.0f} "
                      f"— watching (dist:{lv['dist']:.2f}%)")

    state["brokenLevels"] = broken_levels

    if alerts_sent == 0:
        print("\nNo alerts this run")

    # Update simulator
    print("\nUpdating simulator...")
    update_simulator(state, cur_price)
    print(f"  Balance: ${state['balance']:.2f}")

    # Save
    print("\nSaving to GitHub...")
    save_state(state, sha)

    print("=== Scan complete ===")

if __name__ == "__main__":
    main()
