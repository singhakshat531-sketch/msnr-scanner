#!/usr/bin/env python3
"""
MSNR Scanner v9 — MSNR + 1H MSS Alerts
──────────────────────────────────────────────────────────────
EXACT STRATEGY:

  BIAS:     1W + 1D trend → direction
  LEVELS:   4H key levels (primary). 1D nearby = A+ grade.

  ALERT A — "Price at level"
    Price comes within 0.5% of a 4H key level.
    → Your cue to open 1H and watch.

  ALERT B — "1H MSS confirmed"
    After price reaches the 4H level:
    1. On 1H, price sweeps the level (wick beyond, close back inside)
       OR simply consolidates near it forming a swing
    2. Those 1H candles form a swing high (bearish) or swing low (bullish)
       — can be 1 candle or many consecutive candles
    3. A 1H candle BODY CLOSES beyond the swing low (bearish MSS)
       or swing high (bullish MSS)
    → MSS confirmed. Alert fires. You go to 5min for entry.

  MSS rule (strict):
    - Bearish MSS: candle CLOSE < swing low of the consecutive candles
    - Bullish MSS: candle CLOSE > swing high of the consecutive candles
    - WICK does not count. BODY CLOSE only.
──────────────────────────────────────────────────────────────
"""

import json, urllib.request, urllib.parse, os, time, base64
from datetime import datetime

# ── CONFIG ────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "1247283950")
GITHUB_PAT       = os.environ.get("PAT_TOKEN", "")
GITHUB_REPO      = "singhakshat531-sketch/msnr-scanner"
DATA_FILE        = "trades.json"

NEAR_PCT         = 0.5   # Alert A: price within 0.5% of 4H level
ZONE_PCT         = 2.0   # scan for MSS when price within 2% of level
SPAM_HOURS_A     = 4     # re-alert cooldown for Alert A
SPAM_HOURS_B     = 2     # re-alert cooldown for Alert B (MSS)
SWING_LOOKBACK   = 30    # 1H candles to look back for swing formation
SIM_START        = 1000.0
RISK_PCT         = {"aplus": 3.0, "a": 2.0, "b": 1.0}

# ── GITHUB ────────────────────────────────────────────────────
def github_read(filename):
    if not GITHUB_PAT:
        try:
            with open(filename) as f: return json.load(f), None
        except: return None, None
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"token {GITHUB_PAT}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "MSNR-Scanner/9.0"
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read())
        return json.loads(base64.b64decode(d["content"]).decode()), d["sha"]
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
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={
        "Authorization": f"token {GITHUB_PAT}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json",
        "User-Agent": "MSNR-Scanner/9.0"
    }, method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=15) as r: json.loads(r.read())
        print(f"  GitHub write OK"); return True
    except Exception as e:
        print(f"  GitHub write error: {e}"); return False

def load_state():
    data, sha = github_read(DATA_FILE)
    if data is None:
        data = {
            "balance": SIM_START, "activeTrades": [], "history": [],
            "equityCurve": [{"t": datetime.utcnow().isoformat(), "v": SIM_START}],
            "stats": {"totalTrades":0,"wins":0,"losses":0,"be":0,"netR":0.0,"bestR":0.0},
            "lastUpdated": datetime.utcnow().isoformat(),
            "levelAlerts": {}, "mssAlerts": {}
        }
    data.setdefault("levelAlerts", {})
    data.setdefault("mssAlerts", {})
    return data, sha

def save_state(data, sha):
    data["lastUpdated"] = datetime.utcnow().isoformat()
    return github_write(DATA_FILE, data, sha)

# ── FETCH ─────────────────────────────────────────────────────
def fetch_candles(symbol, interval, limit=200):
    endpoint  = "histoday" if interval in ("1d","1w") else "histohour"
    aggregate = {"1w":7,"1d":1,"4h":4,"1h":1}.get(interval, 1)
    url = (f"https://min-api.cryptocompare.com/data/v2/{endpoint}"
           f"?fsym={symbol}&tsym=USD&limit={limit}&aggregate={aggregate}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent":"MSNR-Scanner/9.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
        if data.get("Response") != "Success":
            print(f"  Error {symbol} {interval}: {data.get('Message','?')}"); return []
        result = [
            {"t":k["time"]*1000,"o":float(k["open"]),"h":float(k["high"]),
             "l":float(k["low"]),"c":float(k["close"])}
            for k in data["Data"]["Data"]
            if not (k["open"]==0 and k["close"]==0)
        ]
        print(f"  {symbol} {interval}: {len(result)} candles")
        return result
    except Exception as e:
        print(f"  Fetch error {symbol} {interval}: {e}"); return []

# ── TREND & BIAS ──────────────────────────────────────────────
def get_trend(candles, lookback=20):
    if len(candles) < lookback: return "UNKNOWN"
    c = [x["c"] for x in candles[-lookback:]]
    half = len(c)//2
    avg_s = sum(c[:half])/half
    avg_e = sum(c[half:])/half
    pct   = (avg_e - avg_s)/avg_s*100
    bull  = sum(1 for i in range(2,len(c)) if c[i]>c[i-1]>c[i-2])
    bear  = sum(1 for i in range(2,len(c)) if c[i]<c[i-1]<c[i-2])
    if pct>3  and bull>bear:        return "BULLISH"
    if pct<-3 and bear>bull:        return "BEARISH"
    if pct>2  and bull>bear*1.5:    return "BULLISH"
    if pct<-2 and bear>bull*1.5:    return "BEARISH"
    return "RANGING"

def get_bias(tr1w, tr1d):
    if tr1w=="BULLISH" and tr1d in ("BULLISH","RANGING"): return "BULLISH"
    if tr1w=="BEARISH" and tr1d in ("BEARISH","RANGING"): return "BEARISH"
    if tr1w=="RANGING":
        if tr1d=="BULLISH": return "BULLISH"
        if tr1d=="BEARISH": return "BEARISH"
    if tr1d=="BULLISH": return "BULLISH"
    if tr1d=="BEARISH": return "BEARISH"
    return "RANGING"

# ── KEY LEVELS ────────────────────────────────────────────────
def find_key_levels(candles, lb=2, max_dist_pct=20.0):
    if len(candles) < lb*2+2: return []
    closes = [c["c"] for c in candles]
    cur    = closes[-1]
    levels = []
    for i in range(lb, len(closes)-lb):
        is_a = all(closes[i]>closes[i-j] and closes[i]>closes[i+j] for j in range(1,lb+1))
        is_v = all(closes[i]<closes[i-j] and closes[i]<closes[i+j] for j in range(1,lb+1))
        if not is_a and not is_v: continue
        price = closes[i]
        dist  = abs(cur-price)/cur*100
        if dist > max_dist_pct: continue
        t = "A" if is_a else "V"
        wicks, dead = 0, False
        for fc in candles[i+1:]:
            if t=="A":
                if fc["c"]>price: dead=True; break
                if fc["h"]>=price: wicks+=1
            else:
                if fc["c"]<price: dead=True; break
                if fc["l"]<=price: wicks+=1
        if dead: continue
        hsl = sum(1 for pc in closes[:i] if abs(pc-price)/price<0.005)>=2
        levels.append({"type":t,"price":price,"fresh":wicks==0,"hsl":hsl,"dist":dist,"wicks":wicks})
    return sorted(levels, key=lambda x: x["dist"])[:10]

def grade_level(lv, has_1d):
    score = 0
    if lv["fresh"]:    score += 2
    if lv["hsl"]:      score += 1
    if has_1d:         score += 2
    if lv["dist"]<0.5: score += 1
    return "aplus" if score>=5 else "a" if score>=3 else "b"

# ── SPAM CONTROL ──────────────────────────────────────────────
def was_alerted(state, bucket, key, hours):
    ts = state.get(bucket,{}).get(key)
    if not ts: return False
    try:
        return (datetime.utcnow()-datetime.fromisoformat(ts)).total_seconds() < hours*3600
    except: return False

def mark_alert(state, bucket, key):
    state[bucket][key] = datetime.utcnow().isoformat()

# ── TELEGRAM ──────────────────────────────────────────────────
def send_telegram(msg):
    if not TELEGRAM_TOKEN:
        print("  [no token]\n" + msg); return
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id":TELEGRAM_CHAT_ID,"text":msg,"parse_mode":"HTML"}).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=10) as r:
            res = json.loads(r.read())
        print("  Telegram ✓" if res.get("ok") else f"  Telegram error: {res}")
    except Exception as e:
        print(f"  Telegram error: {e}")

# ── CORE: 1H MSS DETECTION ────────────────────────────────────
def find_1h_mss(h1, level):
    """
    Detects a 1H Market Structure Shift (MSS) at a 4H key level.

    For a BEARISH setup (A-level / resistance):
      1. Find 1H candles that swept or touched the 4H level
         (wick went above, or candle was at/near level)
      2. Those candles form a swing HIGH — a consecutive group
         that moved up into the level
      3. swing_low = lowest LOW of those swing candles
      4. MSS fires when: a subsequent 1H candle CLOSE < swing_low
         (body close below swing low = MSS, not just a wick)

    For a BULLISH setup (V-level / support):
      1. Find 1H candles that swept or touched the 4H level
         (wick went below, or candle was at/near level)
      2. Those candles form a swing LOW
      3. swing_high = highest HIGH of those swing candles
      4. MSS fires when: a subsequent 1H candle CLOSE > swing_high

    Returns dict with setup details, or None.
    """
    if len(h1) < 4: return None

    bull = level["type"] == "V"   # V = support = bullish setup
    lp   = level["price"]
    scan = h1[-SWING_LOOKBACK:]
    n    = len(scan)

    # ── Step 1: find candles that interacted with the level ────
    # "touched level" = wick reached within 1% of level, OR price
    # crossed the level (swept it)
    TOUCH_PCT = 1.0

    touched_idx = []
    for i, c in enumerate(scan):
        if bull:
            # For support: wick went down to/below level
            touched = (c["l"] <= lp * (1 + TOUCH_PCT/100))
        else:
            # For resistance: wick went up to/above level
            touched = (c["h"] >= lp * (1 - TOUCH_PCT/100))
        if touched:
            touched_idx.append(i)

    if not touched_idx: return None

    # ── Step 2: find the most recent swing formed at the level ─
    # Take the last touched candle and expand to include
    # all consecutive candles in that swing (working backwards)
    last_touch = touched_idx[-1]

    # Swing = the group of candles that moved into the level
    # For resistance (bearish): consecutive candles with rising closes
    # For support (bullish): consecutive candles with falling closes
    # Also allow single-candle swings (the sweep candle itself)

    swing_end   = last_touch
    swing_start = last_touch

    # Walk backwards to find start of swing
    for i in range(last_touch - 1, max(0, last_touch - 15), -1):
        c_cur  = scan[i]
        c_next = scan[i+1]
        if bull:
            # For support swing: candles should have been declining
            # (or at minimum not strongly rallying away)
            if c_cur["h"] < scan[swing_end]["h"] * 0.985:
                break  # too far away, swing starts here
        else:
            # For resistance swing: candles should have been rising
            if c_cur["l"] > scan[swing_end]["l"] * 1.015:
                break
        swing_start = i

    swing = scan[swing_start: swing_end + 1]
    if not swing: return None

    # The key levels of the swing
    swing_high = max(c["h"] for c in swing)
    swing_low  = min(c["l"] for c in swing)

    # ── Step 3: look for MSS candle AFTER the swing ────────────
    # MSS = body CLOSE beyond the swing extreme (away from level)
    for i in range(swing_end + 1, n):
        mss_candle = scan[i]

        if bull:
            # Bullish MSS: close ABOVE swing high
            # (price swept support, formed swing low, now breaks up)
            if mss_candle["c"] > swing_high:
                swept = any(c["l"] < lp for c in swing)
                return {
                    "bull":          True,
                    "swing_high":    swing_high,
                    "swing_low":     swing_low,
                    "swing_candles": len(swing),
                    "mss_close":     mss_candle["c"],
                    "mss_open":      mss_candle["o"],
                    "swept_level":   swept,
                    "broke":         "UP",
                    # MSS direction: close above swing HIGH = bullish
                    "signal": "LONG — 1H MSS bullish. Go 5min for retest entry.",
                }
        else:
            # Bearish MSS: close BELOW swing low
            # (price swept resistance, formed swing high, now breaks down)
            if mss_candle["c"] < swing_low:
                swept = any(c["h"] > lp for c in swing)
                return {
                    "bull":          False,
                    "swing_high":    swing_high,
                    "swing_low":     swing_low,
                    "swing_candles": len(swing),
                    "mss_close":     mss_candle["c"],
                    "mss_open":      mss_candle["o"],
                    "swept_level":   swept,
                    "broke":         "DOWN",
                    "signal": "SHORT — 1H MSS bearish. Go 5min for retest entry.",
                }

    return None


# ── ALERT FORMATTERS ──────────────────────────────────────────
def format_alert_a(lv, cur_price, grade, bias, tr1w, tr1d, has_1d):
    bull = lv["type"] == "V"
    gl   = "A+" if grade=="aplus" else grade.upper()
    pf   = lambda p: f"${p:,.0f}"
    dist = abs(cur_price-lv["price"])/lv["price"]*100
    return "\n".join([
        f"🔔 PRICE AT KEY LEVEL — WATCH 1H",
        f"",
        f"{'🟢 SUPPORT' if bull else '🔴 RESISTANCE'}  |  Grade: {gl}  |  BTCUSDT",
        f"",
        f"Level:  {pf(lv['price'])}  ({'FRESH' if lv['fresh'] else 'USED'})",
        f"Price:  {pf(cur_price)}  ({dist:.2f}% away)",
        f"TF:     4H {'V-Level' if bull else 'A-Level'}{' + 1D confluence ✓' if has_1d else ''}",
        f"HSL:    {'YES ✓' if lv['hsl'] else 'NO'}",
        f"Bias:   {bias}  (1W: {tr1w}  ·  1D: {tr1d})",
        f"",
        f"→ Open 1H chart now",
        f"→ Watch for sweep of this level",
        f"→ Alert B fires on 1H MSS confirmation",
        f"",
        f"{datetime.utcnow().strftime('%d %b %Y  %H:%M UTC')}",
    ])

def format_alert_b(lv, mss, cur_price, grade, bias, tr1w, tr1d, has_1d):
    bull = mss["bull"]
    gl   = "A+" if grade=="aplus" else grade.upper()
    pf   = lambda p: f"${p:,.0f}"
    swept_txt = "Swept level ✓" if mss["swept_level"] else "Touched level"
    return "\n".join([
        f"{'🚀' if bull else '💥'} 1H MSS CONFIRMED — GO TO 5MIN",
        f"",
        f"{'🟢 LONG' if bull else '🔴 SHORT'}  |  Grade: {gl}  |  BTCUSDT",
        f"",
        f"4H Level:     {pf(lv['price'])}  ({'FRESH' if lv['fresh'] else 'USED'})",
        f"Level type:   {'V-Level (Support)' if lv['type']=='V' else 'A-Level (Resistance)'}{' + 1D ✓' if has_1d else ''}",
        f"",
        f"Swing:        {pf(mss['swing_low'])} — {pf(mss['swing_high'])}",
        f"Swing size:   {mss['swing_candles']} × 1H candle(s)",
        f"Level swept:  {swept_txt}",
        f"",
        f"MSS candle:   closed {mss['broke']} @ {pf(mss['mss_close'])}",
        f"  {'↑ Body closed ABOVE swing high' if bull else '↓ Body closed BELOW swing low'}",
        f"Current:      {pf(cur_price)}",
        f"Bias:         {bias}  (1W: {tr1w}  ·  1D: {tr1d})",
        f"",
        f"→ {mss['signal']}",
        f"→ SL above/below the sweep wick",
        f"→ Target next 4H/1D level",
        f"",
        f"{datetime.utcnow().strftime('%d %b %Y  %H:%M UTC')}",
    ])


# ── SIMULATOR ─────────────────────────────────────────────────
def update_simulator(state, cur_price):
    still = []
    for t in state["activeTrades"]:
        bull    = t.get("bull", t["direction"]=="LONG")
        entry, sl, tp1, tp2 = t["rawEntry"], t["rawSL"], t["rawTP1"], t["rawTP2"]
        risk    = t.get("rawRisk", abs(entry-sl))
        if risk<=0: still.append(t); continue
        riskAmt = t.get("entryBalance",SIM_START)*(RISK_PCT.get(t["grade"],1.0)/100)
        posSize = riskAmt/risk
        phase   = t.get("phase","entry")
        if phase=="entry":
            if (bull and cur_price<=sl) or (not bull and cur_price>=sl):
                state["balance"]-=riskAmt; state["stats"]["losses"]+=1; state["stats"]["totalTrades"]+=1
                state["stats"]["netR"]=round(state["stats"]["netR"]-1,2)
                state["history"].insert(0,{**t,"result":"loss","pnl":round(-riskAmt,2),"pnlR":-1.0,
                    "closePrice":round(cur_price,2),"closedAt":datetime.utcnow().isoformat()})
                state["equityCurve"].append({"t":datetime.utcnow().isoformat(),"v":round(state["balance"],2)}); continue
            if (bull and cur_price>=tp1) or (not bull and cur_price<=tp1):
                profit=posSize*abs(tp1-entry)*0.5; state["balance"]+=profit
                t["phase"]="tp1"; t["rawSL"]=entry
                state["stats"]["netR"]=round(state["stats"]["netR"]+1.5,2)
                state["history"].insert(0,{**t,"result":"tp1","pnl":round(profit,2),"pnlR":1.5,
                    "closePrice":round(cur_price,2),"closedAt":datetime.utcnow().isoformat()})
                state["equityCurve"].append({"t":datetime.utcnow().isoformat(),"v":round(state["balance"],2)})
                still.append(t); continue
        elif phase=="tp1":
            if (bull and cur_price<=entry) or (not bull and cur_price>=entry):
                state["stats"]["be"]+=1; state["stats"]["totalTrades"]+=1
                state["history"].insert(0,{**t,"result":"be","pnl":0.0,"pnlR":0.0,
                    "closePrice":round(cur_price,2),"closedAt":datetime.utcnow().isoformat()})
                state["equityCurve"].append({"t":datetime.utcnow().isoformat(),"v":round(state["balance"],2)}); continue
            if (bull and cur_price>=tp2) or (not bull and cur_price<=tp2):
                profit=posSize*abs(tp2-entry)*0.5; netR=round(1.5+abs(tp2-entry)/risk*0.5,1)
                state["balance"]+=profit; state["stats"]["wins"]+=1; state["stats"]["totalTrades"]+=1
                state["stats"]["netR"]=round(state["stats"]["netR"]+netR,2)
                state["stats"]["bestR"]=max(state["stats"]["bestR"],netR)
                state["history"].insert(0,{**t,"result":"win","pnl":round(profit,2),"pnlR":netR,
                    "closePrice":round(cur_price,2),"closedAt":datetime.utcnow().isoformat()})
                state["equityCurve"].append({"t":datetime.utcnow().isoformat(),"v":round(state["balance"],2)}); continue
        still.append(t)
    state["activeTrades"]=still
    state["history"]=state["history"][:100]
    state["equityCurve"]=state["equityCurve"][-200:]


# ── MAIN ──────────────────────────────────────────────────────
def main():
    print(f"\n=== MSNR Scanner v9  {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} ===\n")
    state, sha = load_state()
    print(f"Balance: ${state['balance']:.2f} | Active trades: {len(state['activeTrades'])}")

    print("\nFetching candles...")
    w1 = fetch_candles("BTC","1w",52);  time.sleep(1)
    d1 = fetch_candles("BTC","1d",90);  time.sleep(1)
    h4 = fetch_candles("BTC","4h",200); time.sleep(1)
    h1 = fetch_candles("BTC","1h",100)

    if not (d1 and h4 and h1):
        print("Insufficient data — aborting"); return

    cur_price = h1[-1]["c"]
    tr1w = get_trend(w1, 12) if w1 else "UNKNOWN"
    tr1d = get_trend(d1, 20)
    bias = get_bias(tr1w, tr1d)

    print(f"\nBTC: ${cur_price:,.0f} | 1W:{tr1w} | 1D:{tr1d} | Bias:{bias}")

    if bias == "RANGING":
        print("Bias RANGING — no directional setups this run")
        save_state(state, sha); return

    lvl_4h = find_key_levels(h4, lb=2, max_dist_pct=15.0)
    lvl_1d = find_key_levels(d1, lb=2, max_dist_pct=20.0)
    print(f"4H levels: {len(lvl_4h)} | 1D levels: {len(lvl_1d)}")

    alerts_sent = 0

    for lv in lvl_4h:
        bull = lv["type"] == "V"
        if bull  and bias != "BULLISH": continue
        if not bull and bias != "BEARISH": continue

        has_1d = any(
            l["type"]==lv["type"] and abs(l["price"]-lv["price"])/lv["price"]*100 < 1.5
            for l in lvl_1d
        )
        grade  = grade_level(lv, has_1d)
        lv_key = f"BTC_{lv['type']}_{int(lv['price'])}"
        dist   = abs(cur_price - lv["price"]) / lv["price"] * 100

        print(f"\n  {lv['type']} ${lv['price']:,.0f} | "
              f"{'FRESH' if lv['fresh'] else 'USED'} | "
              f"Grade:{grade} | dist:{dist:.2f}%"
              f"{' | 1D+4H' if has_1d else ''}")

        # ── ALERT A: price just arrived at level ──────────────
        if dist <= NEAR_PCT:
            key_a = lv_key + "_AT"
            if not was_alerted(state, "levelAlerts", key_a, SPAM_HOURS_A):
                msg = format_alert_a(lv, cur_price, grade, bias, tr1w, tr1d, has_1d)
                send_telegram(msg)
                mark_alert(state, "levelAlerts", key_a)
                alerts_sent += 1
                print(f"  → ALERT A: price at level")

        # ── ALERT B: 1H MSS at this level ─────────────────────
        if dist <= ZONE_PCT:
            key_b = lv_key + "_MSS"
            if not was_alerted(state, "mssAlerts", key_b, SPAM_HOURS_B):
                mss = find_1h_mss(h1, lv)
                if mss:
                    msg = format_alert_b(lv, mss, cur_price, grade, bias, tr1w, tr1d, has_1d)
                    send_telegram(msg)
                    mark_alert(state, "mssAlerts", key_b)
                    alerts_sent += 1
                    print(f"  → ALERT B: 1H MSS {'bullish' if mss['bull'] else 'bearish'} "
                          f"| swing {mss['swing_candles']}c "
                          f"| broke {mss['broke']} @ ${mss['mss_close']:,.0f}")
                else:
                    print(f"  → In zone, no 1H MSS yet")

    print(f"\n{'No alerts this run' if alerts_sent==0 else str(alerts_sent)+' alert(s) sent'}")
    update_simulator(state, cur_price)
    save_state(state, sha)
    print("=== Done ===")

if __name__ == "__main__":
    main()
