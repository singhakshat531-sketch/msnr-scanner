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
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

def now_ist():
    return datetime.now(IST).strftime('%d %b %Y  %I:%M %p IST')

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
        "message": f"scanner {datetime.now(timezone.utc).replace(tzinfo=None).strftime('%Y-%m-%d %H:%M UTC')}",
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
            "equityCurve": [{"t": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(), "v": SIM_START}],
            "stats": {"totalTrades":0,"wins":0,"losses":0,"be":0,"netR":0.0,"bestR":0.0},
            "lastUpdated": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            "levelAlerts": {}, "mssAlerts": {}
        }
    data.setdefault("levelAlerts", {})
    data.setdefault("mssAlerts", {})
    return data, sha

def save_state(data, sha):
    data["lastUpdated"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
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
    """
    V-level: bearish candle confirmed by next bullish candle
             → level price = bearish candle's CLOSE
    A-level: bullish candle confirmed by next bearish candle
             → level price = bullish candle's CLOSE
    """
    if len(candles) < 4: return []
    cur    = candles[-1]["c"]
    levels = []

    for i in range(len(candles) - 2):
        c0 = candles[i]
        c1 = candles[i + 1]

        is_bearish = c0["c"] < c0["o"]
        is_bullish = c0["c"] > c0["o"]
        conf_bull  = c1["c"] > c1["o"]
        conf_bear  = c1["c"] < c1["o"]

        if is_bearish and conf_bull:
            price = c0["c"]; t = "V"
        elif is_bullish and conf_bear:
            price = c0["c"]; t = "A"
        else:
            continue

        dist = abs(cur - price) / cur * 100
        if dist > max_dist_pct: continue

        wicks, dead = 0, False
        for fc in candles[i + 2:]:
            if t == "A":
                if fc["c"] > price: dead = True; break
                if fc["h"] >= price: wicks += 1
            else:
                if fc["c"] < price: dead = True; break
                if fc["l"] <= price: wicks += 1
        if dead: continue

        hsl = sum(1 for c in candles[:i] if abs(c["c"] - price) / price < 0.005) >= 2

        levels.append({
            "type":  t,
            "price": price,
            "fresh": wicks == 0,
            "hsl":   hsl,
            "dist":  dist,
            "wicks": wicks,
        })

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
        return (datetime.now(timezone.utc).replace(tzinfo=None)-datetime.fromisoformat(ts)).total_seconds() < hours*3600
    except: return False

def mark_alert(state, bucket, key):
    state[bucket][key] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

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
    EXACT LOGIC (confirmed from chart 19 Mar):

    BULLISH setup (V-level / support):
      1. Price sweeps or touches the 4H level (wick below or within 1%)
      2. After the sweep, look for the most recent group of consecutive
         BEARISH 1H candles (close < open) — these form the external range
         Can be 1 candle or many, but ALL must be bearish (same direction)
      3. external range HIGH = highest high of those consecutive bearish candles
      4. MSS = next 1H candle BODY CLOSES ABOVE that range high
         (close > range high — body, not wick)

    BEARISH setup (A-level / resistance):
      1. Price sweeps or touches the 4H level (wick above or within 1%)
      2. After the sweep, look for consecutive BULLISH 1H candles
      3. external range LOW = lowest low of those consecutive bullish candles
      4. MSS = next 1H candle BODY CLOSES BELOW that range low
    """
    if len(h1) < 4: return None

    bull = level["type"] == "V"
    lp   = level["price"]
    scan = h1[-SWING_LOOKBACK:]
    n    = len(scan)

    # Step 1: find the LAST valid sweep before MSS
    # Valid = wick went through the level AND closed back inside
    # Always use the LAST sweep — if price swept, consolidated, swept again,
    # the LAST sweep is always the reference point for the external range.
    TOUCH_PCT = 1.5
    last_touch_idx = None

    for i in range(n - 3, -1, -1):
        c = scan[i]
        if bull and c["l"] < lp and c["c"] > lp:    # wick below, closed above
            last_touch_idx = i; break
        elif not bull and c["h"] > lp and c["c"] < lp:  # wick above, closed below
            last_touch_idx = i; break

    # Fallback: touch within 1.5% that closed back inside
    if last_touch_idx is None:
        for i in range(n - 3, -1, -1):
            c = scan[i]
            if bull and c["l"] <= lp * (1 + TOUCH_PCT / 100) and c["c"] > lp:
                last_touch_idx = i; break
            elif not bull and c["h"] >= lp * (1 - TOUCH_PCT / 100) and c["c"] < lp:
                last_touch_idx = i; break

    if last_touch_idx is None: return None

    swept = scan[last_touch_idx]["l"] < lp if bull else scan[last_touch_idx]["h"] > lp

    # Step 2: find the MOST RECENT group of consecutive same-direction candles
    # after the last sweep. No window limit — consolidation can happen between
    # sweep and range (e.g. swept, consolidated, swept again, THEN range formed).
    # We scan everything after the sweep and keep the LAST group = freshest range.
    best_range_start = None
    best_range_end   = None
    i = last_touch_idx + 1

    while i < n - 1:
        c = scan[i]
        is_dir = (c["c"] < c["o"]) if bull else (c["c"] > c["o"])
        if is_dir:
            grp_start = i
            grp_end   = i
            j = i + 1
            while j < n - 1:
                cj = scan[j]
                if (cj["c"] < cj["o"]) if bull else (cj["c"] > cj["o"]):
                    grp_end = j
                    j += 1
                else:
                    break
            best_range_start = grp_start
            best_range_end   = grp_end
            i = grp_end + 1
        else:
            i += 1

    if best_range_start is None: return None

    ext_range  = scan[best_range_start: best_range_end + 1]
    range_high = max(c["h"] for c in ext_range)
    range_low  = min(c["l"] for c in ext_range)

    # Helper: unix-ms timestamp → short IST string for display
    def ts(unix_ms):
        dt = datetime.fromtimestamp(unix_ms / 1000, IST)
        return dt.strftime('%d %b  %I:%M %p')

    sweep_candle = scan[last_touch_idx]

    # Step 3: scan candles after range for MSS or BREAK
    # MSS   = range extreme swept (wick, closes back inside) THEN body breaks
    # BREAK = body breaks range extreme directly, no sweep first
    range_swept = False

    for i in range(best_range_end + 1, n):
        mss_c = scan[i]
        if bull:
            # Track range low sweep
            if mss_c["l"] < range_low and mss_c["c"] > range_low:
                range_swept = True
            if mss_c["c"] > range_high:
                signal = "MSS" if range_swept else "BREAK"
                return {
                    "bull":          True,
                    "signal":        signal,
                    "range_high":    range_high,
                    "range_low":     range_low,
                    "range_candles": len(ext_range),
                    "mss_close":     mss_c["c"],
                    "swept_level":   swept,
                    "broke":         "ABOVE range high",
                    "sweep_time":    ts(sweep_candle["t"]),
                    "sweep_wick":    sweep_candle["l"],
                    "sweep_close":   sweep_candle["c"],
                    "range_open":    ts(ext_range[0]["t"]),
                    "range_close":   ts(ext_range[-1]["t"]),
                    "mss_open":      ts(mss_c["t"]),
                }
        else:
            # Track range high sweep
            if mss_c["h"] > range_high and mss_c["c"] < range_high:
                range_swept = True
            if mss_c["c"] < range_low:
                signal = "MSS" if range_swept else "BREAK"
                return {
                    "bull":          False,
                    "signal":        signal,
                    "range_high":    range_high,
                    "range_low":     range_low,
                    "range_candles": len(ext_range),
                    "mss_close":     mss_c["c"],
                    "swept_level":   swept,
                    "broke":         "BELOW range low",
                    "sweep_time":    ts(sweep_candle["t"]),
                    "sweep_wick":    sweep_candle["h"],
                    "sweep_close":   sweep_candle["c"],
                    "range_open":    ts(ext_range[0]["t"]),
                    "range_close":   ts(ext_range[-1]["t"]),
                    "mss_open":      ts(mss_c["t"]),
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
        f"{now_ist()}",
    ])

def format_alert_b(lv, mss, cur_price, grade, bias, tr1w, tr1d, has_1d):
    bull       = mss["bull"]
    signal     = mss.get("signal", "MSS")
    gl         = "A+" if grade=="aplus" else grade.upper()
    pf         = lambda p: f"${p:,.0f}"
    swept_txt  = "Swept ✓" if mss["swept_level"] else "Touched"
    n          = mss["range_candles"]
    rdir       = "bearish" if bull else "bullish"
    retest_lvl = pf(mss["range_high"]) if bull else pf(mss["range_low"])
    wick_label = f"Wick low : {pf(mss['sweep_wick'])}" if bull else f"Wick high: {pf(mss['sweep_wick'])}"
    if signal == "MSS":
        emoji  = "🚀 LONG — SWEEP+MSS" if bull else "💥 SHORT — SWEEP+MSS"
    else:
        emoji  = "⚡ LONG — BREAK" if bull else "⚡ SHORT — BREAK"
    sig_line = "── MSS  [1H candle] ──────────" if signal == "MSS" else "── BREAK  [1H candle] ────────"
    return "\n".join([
        f"{emoji} | Go to 5min",
        f"",
        f"Grade: {gl}{' | 1D+4H ✓' if has_1d else ''}  |  BTCUSDT",
        f"",
        f"── 4H LEVEL ──────────────────",
        f"Level     : {pf(lv['price'])} ({'Fresh' if lv['fresh'] else 'Used'}{', HSL' if lv['hsl'] else ''})",
        f"",
        f"── SWEEP  [1H candle] ────────",
        f"Time      : {mss['sweep_time']} IST",
        f"{wick_label}",
        f"Close     : {pf(mss['sweep_close'])}  ← closed back inside level ✓",
        f"Status    : {swept_txt}",
        f"",
        f"── EXT RANGE  [1H candles] ───",
        f"Candles   : {n} consecutive {rdir}",
        f"From      : {mss['range_open']} IST",
        f"To        : {mss['range_close']} IST",
        f"Range     : {pf(mss['range_low'])} — {pf(mss['range_high'])}",
        f"",
        f"{sig_line}",
        f"Time      : {mss['mss_open']} IST",
        f"Close     : {pf(mss['mss_close'])}  ({mss['broke']})",
        f"",
        f"Now       : {pf(cur_price)}",
        f"Bias      : {bias}  (1W {tr1w} · 1D {tr1d})",
        f"",
        f"→ 5min: watch retest of {retest_lvl}",
        f"→ SL behind the sweep wick",
        f"",
        f"{now_ist()}",
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
                    "closePrice":round(cur_price,2),"closedAt":datetime.now(timezone.utc).replace(tzinfo=None).isoformat()})
                state["equityCurve"].append({"t":datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),"v":round(state["balance"],2)}); continue
            if (bull and cur_price>=tp1) or (not bull and cur_price<=tp1):
                profit=posSize*abs(tp1-entry)*0.5; state["balance"]+=profit
                t["phase"]="tp1"; t["rawSL"]=entry
                state["stats"]["netR"]=round(state["stats"]["netR"]+1.5,2)
                state["history"].insert(0,{**t,"result":"tp1","pnl":round(profit,2),"pnlR":1.5,
                    "closePrice":round(cur_price,2),"closedAt":datetime.now(timezone.utc).replace(tzinfo=None).isoformat()})
                state["equityCurve"].append({"t":datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),"v":round(state["balance"],2)})
                still.append(t); continue
        elif phase=="tp1":
            if (bull and cur_price<=entry) or (not bull and cur_price>=entry):
                state["stats"]["be"]+=1; state["stats"]["totalTrades"]+=1
                state["history"].insert(0,{**t,"result":"be","pnl":0.0,"pnlR":0.0,
                    "closePrice":round(cur_price,2),"closedAt":datetime.now(timezone.utc).replace(tzinfo=None).isoformat()})
                state["equityCurve"].append({"t":datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),"v":round(state["balance"],2)}); continue
            if (bull and cur_price>=tp2) or (not bull and cur_price<=tp2):
                profit=posSize*abs(tp2-entry)*0.5; netR=round(1.5+abs(tp2-entry)/risk*0.5,1)
                state["balance"]+=profit; state["stats"]["wins"]+=1; state["stats"]["totalTrades"]+=1
                state["stats"]["netR"]=round(state["stats"]["netR"]+netR,2)
                state["stats"]["bestR"]=max(state["stats"]["bestR"],netR)
                state["history"].insert(0,{**t,"result":"win","pnl":round(profit,2),"pnlR":netR,
                    "closePrice":round(cur_price,2),"closedAt":datetime.now(timezone.utc).replace(tzinfo=None).isoformat()})
                state["equityCurve"].append({"t":datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),"v":round(state["balance"],2)}); continue
        still.append(t)
    state["activeTrades"]=still
    state["history"]=state["history"][:100]
    state["equityCurve"]=state["equityCurve"][-200:]


# ── MAIN ──────────────────────────────────────────────────────
def main():
    print(f"\n=== MSNR Scanner v9  {datetime.now(timezone.utc).replace(tzinfo=None).strftime('%Y-%m-%d %H:%M UTC')} ===\n")
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
                          f"| range {mss['range_candles']}c "
                          f"| broke {mss['broke']} @ ${mss['mss_close']:,.0f}")
                else:
                    print(f"  → In zone, no 1H MSS yet")

    print(f"\n{'No alerts this run' if alerts_sent==0 else str(alerts_sent)+' alert(s) sent'}")
    update_simulator(state, cur_price)
    save_state(state, sha)
    print("=== Done ===")

if __name__ == "__main__":
    main()
