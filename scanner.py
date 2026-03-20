#!/usr/bin/env python3
"""
MSNR Scanner v8
──────────────────────────────────────────────────────────────
STRATEGY (exact):

  1. BIAS       — 1W + 1D trend gives overall direction (bull/bear)

  2. KEY LEVELS — 4H MSNR levels are primary.
                  1D MSNR levels are secondary.
                  When a 4H level also has a 1D level nearby → A+ grade.

  3. ALERT A    — Price arrives within 0.5% of a fresh 4H level.
                  "Price is AT the level — watch 1H reaction."

  4. ALERT B    — On 1H, consecutive candles have been moving INTO
                  the 4H level (forming an external range/swing).
                  When that 1H range breaks EITHER side →
                  "1H range broken — check 5min for entry."

That's it. Entry is manual (5min retest). Scanner only alerts A and B.
──────────────────────────────────────────────────────────────
"""

import json, urllib.request, urllib.parse, os, time, base64
from datetime import datetime, timezone

# ── CONFIG ────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "1247283950")
GITHUB_PAT       = os.environ.get("PAT_TOKEN", "")
GITHUB_REPO      = "singhakshat531-sketch/msnr-scanner"
DATA_FILE        = "trades.json"

NEAR_PCT         = 0.5   # Alert A: price within 0.5% of level
SPAM_HOURS_A     = 4     # don't re-alert same level within 4h
SPAM_HOURS_B     = 2     # don't re-alert same range break within 2h
RANGE_LOOKBACK   = 20    # how many 1H candles to look back for range
RANGE_MIN_CANDLES= 2     # minimum candles to form a swing
SIM_START        = 1000.0
RISK_PCT         = {"aplus": 3.0, "a": 2.0, "b": 1.0}

# ── GITHUB STATE ──────────────────────────────────────────────
def github_read(filename):
    if not GITHUB_PAT:
        try:
            with open(filename) as f: return json.load(f), None
        except: return None, None
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"token {GITHUB_PAT}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "MSNR-Scanner/8.0"
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
            "User-Agent": "MSNR-Scanner/8.0"
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
        data = {
            "balance": SIM_START,
            "activeTrades": [],
            "history": [],
            "equityCurve": [{"t": datetime.utcnow().isoformat(), "v": SIM_START}],
            "stats": {"totalTrades": 0, "wins": 0, "losses": 0, "be": 0, "netR": 0.0, "bestR": 0.0},
            "lastUpdated": datetime.utcnow().isoformat(),
            "levelAlerts": {},
            "rangeAlerts": {}
        }
    data.setdefault("levelAlerts", {})
    data.setdefault("rangeAlerts", {})
    return data, sha

def save_state(data, sha):
    data["lastUpdated"] = datetime.utcnow().isoformat()
    return github_write(DATA_FILE, data, sha)

# ── FETCH CANDLES ─────────────────────────────────────────────
def fetch_candles(symbol, interval, limit=200):
    endpoint  = "histoday" if interval in ("1d", "1w") else "histohour"
    aggregate = {"1w": 7, "1d": 1, "4h": 4, "1h": 1}.get(interval, 1)
    url = (f"https://min-api.cryptocompare.com/data/v2/{endpoint}"
           f"?fsym={symbol}&tsym=USD&limit={limit}&aggregate={aggregate}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MSNR-Scanner/8.0"})
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
        print(f"  {symbol} {interval}: {len(result)} candles")
        return result
    except Exception as e:
        print(f"  Fetch error {symbol} {interval}: {e}")
        return []

# ── TREND / BIAS ──────────────────────────────────────────────
def get_trend(candles, lookback=20):
    if len(candles) < lookback: return "UNKNOWN"
    c    = [x["c"] for x in candles[-lookback:]]
    half = len(c) // 2
    avg_s = sum(c[:half]) / half
    avg_e = sum(c[half:]) / half
    pct   = (avg_e - avg_s) / avg_s * 100
    bull  = sum(1 for i in range(2, len(c)) if c[i] > c[i-1] > c[i-2])
    bear  = sum(1 for i in range(2, len(c)) if c[i] < c[i-1] < c[i-2])
    if pct > 3  and bull > bear:        return "BULLISH"
    if pct < -3 and bear > bull:        return "BEARISH"
    if pct > 2  and bull > bear * 1.5:  return "BULLISH"
    if pct < -2 and bear > bull * 1.5:  return "BEARISH"
    return "RANGING"

def get_bias(tr1w, tr1d):
    if tr1w == "BULLISH" and tr1d in ("BULLISH", "RANGING"): return "BULLISH"
    if tr1w == "BEARISH" and tr1d in ("BEARISH", "RANGING"): return "BEARISH"
    if tr1w == "RANGING":
        if tr1d == "BULLISH": return "BULLISH"
        if tr1d == "BEARISH": return "BEARISH"
    if tr1d == "BULLISH": return "BULLISH"
    if tr1d == "BEARISH": return "BEARISH"
    return "RANGING"

# ── MSNR KEY LEVELS ───────────────────────────────────────────
def find_key_levels(candles, lb=2, max_dist_pct=20.0):
    if len(candles) < lb * 2 + 2: return []
    closes = [c["c"] for c in candles]
    cur    = closes[-1]
    levels = []

    for i in range(lb, len(closes) - lb):
        is_a = all(closes[i] > closes[i-j] and closes[i] > closes[i+j] for j in range(1, lb+1))
        is_v = all(closes[i] < closes[i-j] and closes[i] < closes[i+j] for j in range(1, lb+1))
        if not is_a and not is_v: continue

        price = closes[i]
        dist  = abs(cur - price) / cur * 100
        if dist > max_dist_pct: continue

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
            "type": t, "price": price,
            "fresh": wicks == 0,
            "hsl": hsl,
            "dist": dist,
            "wicks": wicks
        })

    return sorted(levels, key=lambda x: x["dist"])[:10]

# ── GRADE ─────────────────────────────────────────────────────
def grade_level(lv_4h, has_1d_confluence):
    score = 0
    if lv_4h["fresh"]:        score += 2
    if lv_4h["hsl"]:          score += 1
    if has_1d_confluence:     score += 2
    if lv_4h["dist"] < 0.5:  score += 1
    return "aplus" if score >= 5 else "a" if score >= 3 else "b"

# ── SPAM CONTROL ──────────────────────────────────────────────
def was_alerted(state, bucket, key, hours):
    ts = state.get(bucket, {}).get(key)
    if not ts: return False
    try:
        elapsed = (datetime.utcnow() - datetime.fromisoformat(ts)).total_seconds()
        return elapsed < hours * 3600
    except: return False

def mark_alert(state, bucket, key):
    state[bucket][key] = datetime.utcnow().isoformat()

# ── TELEGRAM ──────────────────────────────────────────────────
def send_telegram(msg):
    if not TELEGRAM_TOKEN:
        print("  [no token] msg:\n" + msg); return
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg,
        "parse_mode": "HTML"
    }).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=10) as r:
            res = json.loads(r.read())
        print("  Telegram ✓" if res.get("ok") else f"  Telegram error: {res}")
    except Exception as e:
        print(f"  Telegram error: {e}")

# ── ALERT A: Price at level ───────────────────────────────────
def format_alert_a(lv, cur_price, grade, bias, tr1w, tr1d, has_1d):
    bull = lv["type"] == "V"
    gl   = "A+" if grade == "aplus" else grade.upper()
    pf   = lambda p: f"${p:,.0f}"
    dist = abs(cur_price - lv["price"]) / lv["price"] * 100
    return "\n".join([
        f"🔔 PRICE AT KEY LEVEL",
        f"",
        f"{'🟢 SUPPORT' if bull else '🔴 RESISTANCE'}  |  Grade: {gl}  |  BTCUSDT",
        f"",
        f"Level:   {pf(lv['price'])}  ({'FRESH' if lv['fresh'] else 'USED'})",
        f"Price:   {pf(cur_price)}  ({dist:.2f}% away)",
        f"TF:      4H {'V-Level' if bull else 'A-Level'}{' + 1D ✓' if has_1d else ''}",
        f"HSL:     {'YES ✓' if lv['hsl'] else 'NO'}",
        f"Bias:    {bias}  (1W:{tr1w} · 1D:{tr1d})",
        f"",
        f"→ Watch 1H reaction",
        f"→ Alert B will fire when 1H range breaks",
        f"",
        f"{datetime.utcnow().strftime('%d %b %Y  %H:%M UTC')}",
    ])

# ── ALERT B: 1H range break ───────────────────────────────────
def find_1h_range_break(h1, level):
    """
    Find consecutive 1H candles that moved INTO the 4H level (the swing).
    Fire when that range breaks EITHER side.
    """
    if len(h1) < RANGE_MIN_CANDLES + 2: return None
    bull = level["type"] == "V"
    lp   = level["price"]
    scan = h1[-RANGE_LOOKBACK:]
    n    = len(scan)
    TOUCH_PCT = 1.5

    # Find candles touching/near the level
    at_idx = []
    for i, c in enumerate(scan):
        if bull:
            in_zone = abs(c["l"] - lp) / lp * 100 <= TOUCH_PCT or c["l"] <= lp <= c["h"]
        else:
            in_zone = abs(c["h"] - lp) / lp * 100 <= TOUCH_PCT or c["l"] <= lp <= c["h"]
        if in_zone:
            at_idx.append(i)

    if not at_idx: return None

    # Take last contiguous cluster
    cluster = [at_idx[-1]]
    for idx in reversed(at_idx[:-1]):
        if cluster[0] - idx <= 3:
            cluster.insert(0, idx)

    if len(cluster) < RANGE_MIN_CANDLES: return None
    if cluster[-1] >= n - 1: return None

    seg = scan[cluster[0]: cluster[-1] + 1]
    rH  = max(c["h"] for c in seg)
    rL  = min(c["l"] for c in seg)

    # Check candles after cluster for break
    for i in range(cluster[-1] + 1, n):
        bc = scan[i]
        broke_up   = bc["c"] > rH
        broke_down = bc["c"] < rL
        if not broke_up and not broke_down: continue

        if bull:
            direction  = "BULLISH ✅" if broke_up else "⚠️ LEVEL FAILED (bearish)"
            signal     = "BUY SETUP — retest range high" if broke_up else "LEVEL FAILED — possible short"
            bull_setup = broke_up
        else:
            direction  = "BEARISH ✅" if broke_down else "⚠️ LEVEL FAILED (bullish)"
            signal     = "SELL SETUP — retest range low" if broke_down else "LEVEL FAILED — possible long"
            bull_setup = not broke_down

        return {
            "rH": rH, "rL": rL,
            "range_candles": len(seg),
            "broke_up": broke_up,
            "broke_down": broke_down,
            "break_close": bc["c"],
            "direction": direction,
            "signal": signal,
            "bull_setup": bull_setup
        }

    return None

def format_alert_b(lv, rb, cur_price, grade, bias, tr1w, tr1d):
    bull_setup = rb["bull_setup"]
    gl  = "A+" if grade == "aplus" else grade.upper()
    pf  = lambda p: f"${p:,.0f}"
    emoji = "🚀" if bull_setup else "💥"
    return "\n".join([
        f"{emoji} 1H RANGE BROKEN — CHECK 5MIN",
        f"",
        f"{'🟢 LONG' if bull_setup else '🔴 SHORT'}  |  Grade: {gl}  |  BTCUSDT",
        f"",
        f"4H Level:   {pf(lv['price'])}  ({'FRESH' if lv['fresh'] else 'USED'})",
        f"Direction:  {rb['direction']}",
        f"",
        f"1H Range:   {pf(rb['rL'])} — {pf(rb['rH'])}  ({rb['range_candles']} candles)",
        f"Broke:      {'UP' if rb['broke_up'] else 'DOWN'}  @ {pf(rb['break_close'])}",
        f"Current:    {pf(cur_price)}",
        f"Bias:       {bias}  (1W:{tr1w} · 1D:{tr1d})",
        f"",
        f"→ {rb['signal']}",
        f"→ Go to 5min chart",
        f"→ Enter on retest + confirmation",
        f"",
        f"{datetime.utcnow().strftime('%d %b %Y  %H:%M UTC')}",
    ])

# ── SIMULATOR ─────────────────────────────────────────────────
def update_simulator(state, cur_price):
    still = []
    for t in state["activeTrades"]:
        bull    = t.get("bull", t["direction"] == "LONG")
        entry, sl, tp1, tp2 = t["rawEntry"], t["rawSL"], t["rawTP1"], t["rawTP2"]
        risk    = t.get("rawRisk", abs(entry - sl))
        if risk <= 0: still.append(t); continue
        riskAmt = t.get("entryBalance", SIM_START) * (RISK_PCT.get(t["grade"], 1.0) / 100)
        posSize = riskAmt / risk
        phase   = t.get("phase", "entry")
        if phase == "entry":
            if (bull and cur_price <= sl) or (not bull and cur_price >= sl):
                state["balance"] -= riskAmt
                state["stats"]["losses"] += 1; state["stats"]["totalTrades"] += 1
                state["stats"]["netR"] = round(state["stats"]["netR"] - 1, 2)
                state["history"].insert(0, {**t, "result":"loss","pnl":round(-riskAmt,2),"pnlR":-1.0,"closePrice":round(cur_price,2),"closedAt":datetime.utcnow().isoformat()})
                state["equityCurve"].append({"t":datetime.utcnow().isoformat(),"v":round(state["balance"],2)}); continue
            if (bull and cur_price >= tp1) or (not bull and cur_price <= tp1):
                profit = posSize * abs(tp1-entry) * 0.5; state["balance"] += profit
                t["phase"]="tp1"; t["rawSL"]=entry
                state["stats"]["netR"] = round(state["stats"]["netR"]+1.5,2)
                state["history"].insert(0,{**t,"result":"tp1","pnl":round(profit,2),"pnlR":1.5,"closePrice":round(cur_price,2),"closedAt":datetime.utcnow().isoformat()})
                state["equityCurve"].append({"t":datetime.utcnow().isoformat(),"v":round(state["balance"],2)}); still.append(t); continue
        elif phase == "tp1":
            if (bull and cur_price <= entry) or (not bull and cur_price >= entry):
                state["stats"]["be"]+=1; state["stats"]["totalTrades"]+=1
                state["history"].insert(0,{**t,"result":"be","pnl":0.0,"pnlR":0.0,"closePrice":round(cur_price,2),"closedAt":datetime.utcnow().isoformat()})
                state["equityCurve"].append({"t":datetime.utcnow().isoformat(),"v":round(state["balance"],2)}); continue
            if (bull and cur_price >= tp2) or (not bull and cur_price <= tp2):
                profit=posSize*abs(tp2-entry)*0.5; netR=round(1.5+abs(tp2-entry)/risk*0.5,1)
                state["balance"]+=profit; state["stats"]["wins"]+=1; state["stats"]["totalTrades"]+=1
                state["stats"]["netR"]=round(state["stats"]["netR"]+netR,2); state["stats"]["bestR"]=max(state["stats"]["bestR"],netR)
                state["history"].insert(0,{**t,"result":"win","pnl":round(profit,2),"pnlR":netR,"closePrice":round(cur_price,2),"closedAt":datetime.utcnow().isoformat()})
                state["equityCurve"].append({"t":datetime.utcnow().isoformat(),"v":round(state["balance"],2)}); continue
        still.append(t)
    state["activeTrades"]=still; state["history"]=state["history"][:100]; state["equityCurve"]=state["equityCurve"][-200:]

# ── MAIN ──────────────────────────────────────────────────────
def main():
    print(f"\n=== MSNR Scanner v8  {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} ===\n")
    state, sha = load_state()
    print(f"Balance: ${state['balance']:.2f} | Active: {len(state['activeTrades'])}")

    print("\nFetching candles...")
    w1 = fetch_candles("BTC","1w",52);  time.sleep(1)
    d1 = fetch_candles("BTC","1d",90);  time.sleep(1)
    h4 = fetch_candles("BTC","4h",200); time.sleep(1)
    h1 = fetch_candles("BTC","1h",100)

    if not (d1 and h4 and h1):
        print("Insufficient data"); return

    cur_price = h1[-1]["c"]
    tr1w = get_trend(w1, 12) if w1 else "UNKNOWN"
    tr1d = get_trend(d1, 20)
    bias = get_bias(tr1w, tr1d)

    print(f"\nBTC: ${cur_price:,.0f} | 1W:{tr1w} | 1D:{tr1d} | Bias:{bias}")

    if bias == "RANGING":
        print("Bias RANGING — skipping")
        save_state(state, sha); return

    lvl_4h = find_key_levels(h4, lb=2, max_dist_pct=15.0)
    lvl_1d = find_key_levels(d1, lb=2, max_dist_pct=20.0)
    print(f"4H levels: {len(lvl_4h)} | 1D levels: {len(lvl_1d)}")

    alerts_sent = 0
    for lv in lvl_4h:
        bull = lv["type"] == "V"
        if bull and bias != "BULLISH": continue
        if not bull and bias != "BEARISH": continue

        has_1d = any(
            l["type"] == lv["type"] and abs(l["price"]-lv["price"])/lv["price"]*100 < 1.5
            for l in lvl_1d
        )
        grade  = grade_level(lv, has_1d)
        lv_key = f"BTC_{lv['type']}_{int(lv['price'])}"
        dist   = abs(cur_price - lv["price"]) / lv["price"] * 100

        print(f"\n  {lv['type']} ${lv['price']:,.0f} | {'FRESH' if lv['fresh'] else 'USED'} | "
              f"Grade:{grade} | dist:{dist:.2f}% {'| 1D+4H' if has_1d else ''}")

        # ALERT A
        if dist <= NEAR_PCT and not was_alerted(state, "levelAlerts", lv_key+"_AT", SPAM_HOURS_A):
            msg = format_alert_a(lv, cur_price, grade, bias, tr1w, tr1d, has_1d)
            send_telegram(msg)
            mark_alert(state, "levelAlerts", lv_key+"_AT")
            alerts_sent += 1
            print(f"  → ALERT A sent")

        # ALERT B
        if dist <= 2.0 and not was_alerted(state, "rangeAlerts", lv_key+"_BREAK", SPAM_HOURS_B):
            rb = find_1h_range_break(h1, lv)
            if rb:
                msg = format_alert_b(lv, rb, cur_price, grade, bias, tr1w, tr1d)
                send_telegram(msg)
                mark_alert(state, "rangeAlerts", lv_key+"_BREAK")
                alerts_sent += 1
                print(f"  → ALERT B sent: range broke {'UP' if rb['broke_up'] else 'DOWN'}")
            else:
                print(f"  → In zone, no range break yet")

    print(f"\n{'No alerts' if alerts_sent==0 else str(alerts_sent)+' alert(s) sent'}")
    update_simulator(state, cur_price)
    save_state(state, sha)
    print("=== Done ===")

if __name__ == "__main__":
    main()
