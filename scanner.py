#!/usr/bin/env python3
"""
MSNR + CRT Scanner v5.1 — GitHub Actions
Elite Observer — High RR Setup Finder

CORRECT STRATEGY HIERARCHY:
- 1W/1D = bias direction + major targets ONLY
- 4H = primary entry levels (where we trade)
- 1H = CRT confirmation trigger
- Alert when ALL THREE align

FIXES FROM v5:
- 4H levels now properly detected (wider lookback)
- 1D used for bias only, not as skip filter
- If 1D ranging → use 4H trend as bias
- 4H levels are entry, 1D levels are targets
- More 4H candles fetched for better detection
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
GITHUB_REPO      = os.environ.get("GITHUB_REPO", "singhakshat531-sketch/msnr-scanner")
DATA_FILE        = "trades.json"
MIN_RR           = 2.5
SPAM_HOURS       = 6
SIM_START        = 1000.0
RISK_PCT         = {"aplus": 3.0, "a": 2.0, "b": 1.0}

# ── GITHUB FILE READ/WRITE ────────────────────────────────────────────────────
def github_read(filename):
    if not GITHUB_PAT:
        try:
            with open(filename) as f:
                return json.load(f), None
        except:
            return None, None
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"token {GITHUB_PAT}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "MSNR-Scanner/5.1"
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        content = json.loads(base64.b64decode(data["content"]).decode())
        return content, data["sha"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, None
        print(f"  GitHub read error: {e}")
        return None, None
    except Exception as e:
        print(f"  GitHub read error: {e}")
        return None, None

def github_write(filename, content, sha=None):
    if not GITHUB_PAT:
        with open(filename, "w") as f:
            json.dump(content, f, indent=2)
        return True
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
    body = {
        "message": f"scanner update {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
        "content": base64.b64encode(json.dumps(content, indent=2).encode()).decode(),
        "branch": "main"
    }
    if sha:
        body["sha"] = sha
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={
            "Authorization": f"token {GITHUB_PAT}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json",
            "User-Agent": "MSNR-Scanner/5.1"
        }, method="PUT"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            json.loads(r.read())
        print(f"  GitHub write OK: {filename}")
        return True
    except Exception as e:
        print(f"  GitHub write error: {e}")
        return False

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
            "lastUpdated": datetime.utcnow().isoformat()
        }
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
        req = urllib.request.Request(url, headers={"User-Agent": "MSNR-Scanner/5.1"})
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

# ── TREND ─────────────────────────────────────────────────────────────────────
def get_trend(candles, lookback=30):
    """
    Determine trend using price structure.
    1D: compare 30-day window
    4H: compare recent 60-candle window (10 days)
    Uses both directional move AND momentum.
    """
    if len(candles) < 10: return "UNKNOWN"
    c = [x["c"] for x in candles[-lookback:]]
    
    # Compare first half vs second half
    half = len(c) // 2
    avg_start = sum(c[:half]) / half
    avg_end   = sum(c[half:]) / half
    
    pct_change = (avg_end - avg_start) / avg_start * 100
    
    # Momentum
    bull = sum(1 for i in range(2, len(c)) if c[i] > c[i-1] > c[i-2])
    bear = sum(1 for i in range(2, len(c)) if c[i] < c[i-1] < c[i-2])
    
    # Need BOTH % change AND momentum to agree
    if pct_change > 3 and bull > bear:
        return "BULLISH"
    if pct_change < -3 and bear > bull:
        return "BEARISH"
    if pct_change > 2 and bull > bear * 1.5:
        return "BULLISH"
    if pct_change < -2 and bear > bull * 1.5:
        return "BEARISH"
    return "RANGING"

def get_bias(trend_1d, trend_4h):
    """
    CORRECT STRATEGY LOGIC:
    1D = primary bias (longer view)
    4H = confirmation or backup
    
    Rules:
    - 1D BULL + 4H anything = BULLISH (1D dominates)
    - 1D BEAR + 4H anything = BEARISH (1D dominates)
    - 1D RANGING + 4H BULL = BULLISH (4H takes over)
    - 1D RANGING + 4H BEAR = BEARISH (4H takes over)
    - 1D RANGING + 4H RANGING = RANGING (no trade)
    """
    if trend_1d == "BULLISH":
        return "BULLISH"
    elif trend_1d == "BEARISH":
        return "BEARISH"
    elif trend_1d == "RANGING":
        if trend_4h == "BULLISH":
            return "BULLISH"
        elif trend_4h == "BEARISH":
            return "BEARISH"
        else:
            return "RANGING"
    return "RANGING"

# ── MSNR LEVELS ───────────────────────────────────────────────────────────────
def find_msnr_levels(candles, lb=2, max_dist=15.0):
    """
    Find A-levels (peaks) and V-levels (valleys).
    lb=2 for 4H (more sensitive, finds more levels)
    lb=3 for 1H
    max_dist=15% for 4H (wider search)
    """
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

# ── CRT DETECTION ─────────────────────────────────────────────────────────────
def find_crt(c1h, level, bias):
    """
    Scan 1H candles for CRT at given 4H level.
    bias = overall market direction from 1D/4H
    """
    if len(c1h) < 15: return None
    bull = level["type"] == "V"

    # Trade WITH bias
    if bull  and bias != "BULLISH": return None
    if not bull and bias != "BEARISH": return None

    # Scan last 48 1H candles (2 days)
    for i in range(max(5, len(c1h) - 48), len(c1h) - 1):
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

        # Range must be meaningful
        if (rH - rL) / c1h[-1]["c"] < 0.002: continue

        sw = c1h[i]
        cf = c1h[i+1]

        # CRT-2: wick sweeps 4H level, CLOSE back INSIDE range
        # CRITICAL: close must be inside range (above rL AND below rH)
        # if close is outside range = breakout not sweep = SKIP
        if bull:
            crt2 = (sw["l"] <= level["price"] and   # wick swept level
                    sw["c"] > level["price"] and     # close above level
                    sw["c"] > rL and                 # close inside range (above low)
                    sw["c"] < rH)                    # close inside range (below high)
        else:
            crt2 = (sw["h"] >= level["price"] and   # wick swept level
                    sw["c"] < level["price"] and     # close below level
                    sw["c"] < rH and                 # close inside range (below high)
                    sw["c"] > rL)                    # close inside range (above low)
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

# ── BUILD SETUP ───────────────────────────────────────────────────────────────
def build_setup(lv_4h, crt, bias, pair, lv_1d_target=None):
    """
    Build setup using 4H level as entry.
    1D level = TP2 target (if available and in direction).
    """
    bull  = lv_4h["type"] == "V"
    entry = lv_4h["price"]
    sl    = crt["sw"]["l"] * 0.9992 if bull else crt["sw"]["h"] * 1.0008
    risk  = abs(entry - sl)
    if risk <= 0: return None

    tp1 = entry + risk * 3  if bull else entry - risk * 3
    tp2 = entry + risk * 7  if bull else entry - risk * 7
    tp3 = entry + risk * 12 if bull else entry - risk * 12

    # Use 1D level as TP2 if available and closer
    if lv_1d_target:
        dist_1d = abs(lv_1d_target["price"] - entry)
        dist_tp2 = abs(tp2 - entry)
        if dist_1d < dist_tp2 and dist_1d > abs(tp1 - entry):
            tp2 = lv_1d_target["price"]

    rr1 = abs(tp1 - entry) / risk
    rr2 = abs(tp2 - entry) / risk
    if rr1 < MIN_RR: return None

    # Grading
    score = 0
    if lv_4h["freshness"] == "FRESH":  score += 2
    if lv_4h["hsl"]:                   score += 1
    if lv_1d_target:                   score += 2  # 1D target confluence
    if crt["br"] > 0.65:               score += 1
    if lv_4h["dist"] < 2:              score += 1

    grade = "aplus" if score >= 6 else "a" if score >= 3 else "b"

    pf = lambda p: round(p, 2)
    return {
        "id":          f"{pair}_{lv_4h['type']}_{int(lv_4h['price'])}",
        "pair":        pair,
        "direction":   "LONG" if bull else "SHORT",
        "grade":       grade,
        "gradeLabel":  "A+" if grade == "aplus" else grade.upper(),
        "score":       score,
        "bull":        bull,
        "rawEntry":    pf(entry),
        "rawSL":       pf(sl),
        "rawTP1":      pf(tp1),
        "rawTP2":      pf(tp2),
        "rawTP3":      pf(tp3),
        "rawRisk":     pf(risk),
        "rr1":         round(rr1, 1),
        "rr2":         round(rr2, 1),
        "freshness":   lv_4h["freshness"],
        "hsl":         lv_4h["hsl"],
        "has1DTarget": bool(lv_1d_target),
        "target1D":    pf(lv_1d_target["price"]) if lv_1d_target else None,
        "br":          round(crt["br"], 2),
        "bias":        bias,
        "detectedAt":  datetime.utcnow().isoformat(),
    }

# ── SIMULATOR UPDATE ──────────────────────────────────────────────────────────
def update_simulator(state, cur_price, new_setups):
    changed = False
    still_active = []

    for t in state["activeTrades"]:
        bull    = t["bull"]
        entry   = t["rawEntry"]
        sl      = t["rawSL"]
        tp1     = t["rawTP1"]
        tp2     = t["rawTP2"]
        risk    = t["rawRisk"]
        riskPct = RISK_PCT.get(t["grade"], 1.0)
        riskAmt = t["entryBalance"] * (riskPct / 100)
        posSize = riskAmt / risk if risk > 0 else 0
        phase   = t.get("phase", "entry")

        if phase == "entry":
            if (bull and cur_price <= sl) or (not bull and cur_price >= sl):
                loss = -riskAmt
                state["balance"] += loss
                state["stats"]["losses"]    += 1
                state["stats"]["totalTrades"] += 1
                state["stats"]["netR"]       = round(state["stats"]["netR"] - 1, 2)
                state["history"].insert(0, {**t, "result":"loss", "pnl":round(loss,2),
                    "pnlR":-1.0, "closePrice":round(cur_price,2),
                    "closedAt":datetime.utcnow().isoformat(), "note":"SL hit"})
                state["equityCurve"].append({"t":datetime.utcnow().isoformat(),"v":round(state["balance"],2)})
                changed = True; continue

            if (bull and cur_price >= tp1) or (not bull and cur_price <= tp1):
                profit = posSize * abs(tp1 - entry) * 0.5
                state["balance"] += profit
                t["phase"]     = "tp1"
                t["rawSL"]     = entry
                t["remainPct"] = 50
                state["stats"]["netR"] = round(state["stats"]["netR"] + 1.5, 2)
                state["history"].insert(0, {**t, "result":"tp1", "pnl":round(profit,2),
                    "pnlR":1.5, "closePrice":round(cur_price,2),
                    "closedAt":datetime.utcnow().isoformat(),
                    "note":"TP1 hit — 50% closed, SL to BE"})
                state["equityCurve"].append({"t":datetime.utcnow().isoformat(),"v":round(state["balance"],2)})
                still_active.append(t); changed = True; continue

        elif phase == "tp1":
            if (bull and cur_price <= entry) or (not bull and cur_price >= entry):
                state["stats"]["be"]        += 1
                state["stats"]["totalTrades"] += 1
                state["history"].insert(0, {**t, "result":"be", "pnl":0.0,
                    "pnlR":0.0, "closePrice":round(cur_price,2),
                    "closedAt":datetime.utcnow().isoformat(), "note":"BE stop"})
                state["equityCurve"].append({"t":datetime.utcnow().isoformat(),"v":round(state["balance"],2)})
                changed = True; continue

            if (bull and cur_price >= tp2) or (not bull and cur_price <= tp2):
                profit = posSize * abs(tp2 - entry) * 0.5
                state["balance"] += profit
                netR = round(1.5 + abs(tp2 - entry) / risk * 0.5, 1)
                state["stats"]["wins"]       += 1
                state["stats"]["totalTrades"] += 1
                state["stats"]["netR"]        = round(state["stats"]["netR"] + netR, 2)
                state["stats"]["bestR"]       = max(state["stats"]["bestR"], netR)
                state["history"].insert(0, {**t, "result":"win", "pnl":round(profit,2),
                    "pnlR":netR, "closePrice":round(cur_price,2),
                    "closedAt":datetime.utcnow().isoformat(), "note":f"TP2 hit +{netR}R"})
                state["equityCurve"].append({"t":datetime.utcnow().isoformat(),"v":round(state["balance"],2)})
                changed = True; continue

        still_active.append(t)

    state["activeTrades"] = still_active

    for s in new_setups:
        if any(t["id"] == s["id"] for t in state["activeTrades"]): continue
        if any(t["id"] == s["id"] for t in state["history"]): continue
        riskPct = RISK_PCT.get(s["grade"], 1.0)
        riskAmt = state["balance"] * (riskPct / 100)
        state["activeTrades"].append({
            **s, "phase":"entry", "remainPct":100,
            "entryBalance":round(state["balance"],2),
            "riskAmt":round(riskAmt,2), "riskPct":riskPct,
            "enteredAt":datetime.utcnow().isoformat(),
        })
        state["equityCurve"].append({"t":datetime.utcnow().isoformat(),"v":round(state["balance"],2)})
        changed = True
        print(f"  Paper trade entered: {s['pair']} {s['direction']} "
              f"{s['gradeLabel']} Entry:${s['rawEntry']:,.0f}")

    state["history"]     = state["history"][:100]
    state["equityCurve"] = state["equityCurve"][-200:]
    return changed

# ── ANTI-SPAM ─────────────────────────────────────────────────────────────────
def already_alerted(state, pair, level_price):
    key = f"{pair}_{int(level_price)}"
    if key not in state.get("alerts", {}): return False
    last = datetime.fromisoformat(state["alerts"][key])
    now  = datetime.utcnow()
    return (now - last).total_seconds() < SPAM_HOURS * 3600

def mark_alerted(state, pair, level_price):
    state.setdefault("alerts", {})[f"{pair}_{int(level_price)}"] = datetime.utcnow().isoformat()

# ── TELEGRAM ──────────────────────────────────────────────────────────────────
def send_telegram(msg):
    if not TELEGRAM_TOKEN:
        print("No token — skipping"); return
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"
    }).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=10) as r:
            res = json.loads(r.read())
        print("Telegram sent ✓" if res.get("ok") else f"Telegram error: {res}")
    except Exception as e:
        print(f"Telegram error: {e}")

def format_msg(s, trend_1d, trend_4h):
    pf = lambda p: f"${p:,.0f}"
    bias_line = f"1D: {trend_1d} → 4H: {trend_4h} → Bias: {s['bias']}"
    return "\n".join([
        f"MSNR + CRT SETUP — {s['pair']}",
        f"",
        f"Grade: {s['gradeLabel']}  |  {s['direction']}  |  4H level",
        f"",
        f"Entry:  {pf(s['rawEntry'])}",
        f"SL:     {pf(s['rawSL'])}",
        f"TP1:    {pf(s['rawTP1'])}  (+{s['rr1']}R)",
        f"TP2:    {pf(s['rawTP2'])}  (+{s['rr2']}R)",
        f"TP3:    {pf(s['rawTP3'])}  (trail)",
        f"",
        f"4H Level:   {pf(s['rawEntry'])} · {s['freshness']}",
        f"1D Target:  {pf(s['target1D']) if s['target1D'] else 'None'}",
        f"HSL:        {'YES' if s['hsl'] else 'NO'}",
        f"Body ratio: {s['br']}",
        f"Bias:       {bias_line}",
        f"",
        f"{datetime.utcnow().strftime('%d %b %Y  %H:%M UTC')}",
        f"",
        f"Verify on TradingView then place orders.",
        f"singhakshat531-sketch.github.io/msnr-scanner",
    ])

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    print(f"=== MSNR Scanner v5.1  {datetime.utcnow().isoformat()} ===")

    print("\nLoading state...")
    state, sha = load_state()
    print(f"  Balance: ${state['balance']:.2f} | "
          f"Active: {len(state['activeTrades'])} | "
          f"History: {len(state['history'])}")

    print("\nFetching BTC data...")
    d1 = fetch_candles("BTC", "1d", 60);   time.sleep(2)
    h4 = fetch_candles("BTC", "4h", 200);  time.sleep(2)  # more candles for 4H
    h1 = fetch_candles("BTC", "1h", 150)

    if not (d1 and h4 and h1):
        print("Insufficient data"); return

    cur_price  = h1[-1]["c"]
    trend_1d   = get_trend(d1, lookback=20)   # 20 daily candles = ~1 month
    trend_4h   = get_trend(h4, lookback=42)   # 42 x 4H candles = ~1 week
    bias       = get_bias(trend_1d, trend_4h)

    print(f"\nBTC: ${cur_price:,.0f}")
    print(f"1D trend: {trend_1d} | 4H trend: {trend_4h} | Bias: {bias}")

    if bias == "RANGING":
        print("Both timeframes ranging — no trade conditions")
        # Still update simulator with current price
        update_simulator(state, cur_price, [])
        save_state(state, sha)
        print("=== Scan complete ===")
        return

    # ── DETECT LEVELS ──────────────────────────────────────────────────────
    # 4H levels = PRIMARY ENTRY LEVELS (lb=2, wide dist)
    lvl_4h = find_msnr_levels(h4, lb=2, max_dist=15.0)

    # 1D levels = TARGETS ONLY
    lvl_1d = find_msnr_levels(d1, lb=2, max_dist=20.0)

    print(f"\n4H levels (entry): {len(lvl_4h)}")
    for l in lvl_4h:
        print(f"  {l['type']}-level ${l['price']:,.0f} "
              f"| {l['freshness']} | HSL:{l['hsl']} | dist:{l['dist']:.1f}%")

    print(f"1D levels (targets): {len(lvl_1d)}")
    for l in lvl_1d:
        print(f"  {l['type']}-level ${l['price']:,.0f} "
              f"| {l['freshness']} | dist:{l['dist']:.1f}%")

    # ── SCAN FOR SETUPS ────────────────────────────────────────────────────
    new_setups   = []
    alert_setups = []
    seen         = set()

    for lv in lvl_4h:
        bull = lv["type"] == "V"

        # Must align with bias
        if bull  and bias != "BULLISH": continue
        if not bull and bias != "BEARISH": continue

        key = f"{lv['type']}_{int(lv['price'])}"
        if key in seen: continue

        # Find nearest 1D level in trade direction as target
        direction_levels = [
            l for l in lvl_1d
            if l["type"] == lv["type"]
            and ((bull and l["price"] > lv["price"])
                 or (not bull and l["price"] < lv["price"]))
        ]
        lv_1d_target = min(direction_levels,
                           key=lambda x: abs(x["price"] - lv["price"]),
                           default=None)

        # Scan 1H for CRT
        crt = find_crt(h1, lv, bias)

        if crt:
            setup = build_setup(lv, crt, bias, "BTCUSDT", lv_1d_target)
            if setup:
                new_setups.append(setup)
                seen.add(key)
                if not already_alerted(state, "BTCUSDT", lv["price"]):
                    alert_setups.append(setup)
                    mark_alerted(state, "BTCUSDT", lv["price"])
                print(f"\n  ✓ SETUP: {setup['direction']} {setup['gradeLabel']} "
                      f"Entry:${setup['rawEntry']:,.0f} "
                      f"TP1:${setup['rawTP1']:,.0f} (+{setup['rr1']}R) "
                      f"TP2:${setup['rawTP2']:,.0f} (+{setup['rr2']}R)")
        else:
            print(f"  {lv['type']}-level ${lv['price']:,.0f} — no CRT")

    print(f"\nSetups found: {len(new_setups)} | Alerts: {len(alert_setups)}")

    # ── UPDATE SIMULATOR ───────────────────────────────────────────────────
    print("\nUpdating simulator...")
    update_simulator(state, cur_price, new_setups)
    print(f"  Balance: ${state['balance']:.2f} | "
          f"Active: {len(state['activeTrades'])}")

    # ── SEND ALERTS ────────────────────────────────────────────────────────
    if alert_setups:
        alert_setups.sort(key=lambda x: -x["score"])
        for s in alert_setups:
            send_telegram(format_msg(s, trend_1d, trend_4h))
            time.sleep(1)
    else:
        print("No new alerts")

    # ── SAVE STATE ─────────────────────────────────────────────────────────
    print("\nSaving to GitHub...")
    save_state(state, sha)

    print("=== Scan complete ===")

if __name__ == "__main__":
    main()
