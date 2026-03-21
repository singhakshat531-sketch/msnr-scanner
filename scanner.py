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
ZONE_PCT         = 10.0  # scan for MSS when price within 10% of level
SPAM_HOURS_A     = 4     # re-alert cooldown for Alert A
SPAM_HOURS_B     = 2     # re-alert cooldown for Alert B (MSS)
SWING_LOOKBACK   = 200   # 1H candles to look back for swing formation
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
def find_key_levels(candles, max_dist_pct=20.0):
    """
    V-level: bear candle at swing low + bull confirmation
             level price = bear candle close
             level time  = bull candle open

    A-level: bull candle at swing high + bear confirmation
             level price = bull candle close
             level time  = bear candle open

    Only extreme swing points — not every bear/bull flip.
    """
    if len(candles) < 6: return []
    cur    = candles[-1]["c"]
    levels = []
    LB = 2

    for i in range(LB, len(candles) - LB - 1):
        c0 = candles[i]
        c1 = candles[i + 1]

        bear0 = c0["c"] < c0["o"]
        bull0 = c0["c"] > c0["o"]
        bull1 = c1["c"] > c1["o"]
        bear1 = c1["c"] < c1["o"]

        if bear0 and bull1:
            is_swing_low = all(
                c0["l"] < candles[i - j]["l"] and c0["l"] < candles[i + 1 + j]["l"]
                for j in range(1, LB + 1)
                if i + 1 + j < len(candles)
            )
            if not is_swing_low: continue
            price = c0["c"]; t = "V"; level_time = c1["t"]

        elif bull0 and bear1:
            is_swing_high = all(
                c0["h"] > candles[i - j]["h"] and c0["h"] > candles[i + 1 + j]["h"]
                for j in range(1, LB + 1)
                if i + 1 + j < len(candles)
            )
            if not is_swing_high: continue
            price = c0["c"]; t = "A"; level_time = c1["t"]

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
    if lv["fresh"]: score += 2
    if lv["hsl"]:   score += 1
    if has_1d:      score += 2
    if lv["dist"] < 0.5: score += 1
    return "aplus" if score >= 5 else "a" if score >= 3 else "b"


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
    """Same logic as find_mss in backtest with re-sweep recalculation."""
    if len(h1) < 4: return None
    bull      = level["type"] == "V"
    lp        = level["price"]
    level_ts  = level.get("ts", 0) * 1000 if level.get("ts", 0) < 1e12 else level.get("ts", 0)
    scan      = h1[-SWING_LOOKBACK:]
    n         = len(scan)

    def ts(unix_ms):
        dt = datetime.fromtimestamp(unix_ms / 1000, IST)
        return dt.strftime("%d %b  %I:%M %p")

    level_idx = 0
    for i in range(n):
        if scan[i]["t"] > level_ts:
            level_idx = i; break

    sweep_idx = None
    for i in range(level_idx, n - 2):
        c = scan[i]
        if bull and c["l"] < lp and c["c"] > lp:
            sweep_idx = i; break
        elif not bull and c["h"] > lp and c["c"] < lp:
            sweep_idx = i; break
    if sweep_idx is None: return None
    if sweep_idx <= level_idx: return None

    def calc_target(from_idx, to_idx):
        sweep_c = scan[to_idx]
        target  = None
        swing_i = to_idx
        if bull:
            best = None
            for i in range(from_idx, to_idx - 1):
                c0, c1 = scan[i], scan[i + 1]
                if c0["c"] > c0["o"] and c1["c"] < c1["o"]:
                    if best is None or c0["h"] > best:
                        best = c0["h"]; target = c0["h"]; swing_i = i
            if sweep_c["h"] > (target or 0):
                target = sweep_c["h"]; swing_i = to_idx
        else:
            best = None
            for i in range(from_idx, to_idx - 1):
                c0, c1 = scan[i], scan[i + 1]
                if c0["c"] < c0["o"] and c1["c"] > c1["o"]:
                    if best is None or c0["l"] < best:
                        best = c0["l"]; target = c0["l"]; swing_i = i
            if sweep_c["l"] < (target or float("inf")):
                target = sweep_c["l"]; swing_i = to_idx
        if target is None:
            target = sweep_c["h"] if bull else sweep_c["l"]; swing_i = to_idx
        return target, swing_i

    mss_target, swing_idx = calc_target(level_idx, sweep_idx)
    sweep_c        = scan[sweep_idx]
    prev_sweep_idx = sweep_idx

    for i in range(sweep_idx + 1, n):
        mc = scan[i]
        if bull:
            if mc["l"] < sweep_c["l"] and mc["c"] > lp:
                prev_sweep_idx = sweep_idx
                sweep_idx = i; sweep_c = mc
                mss_target, swing_idx = calc_target(prev_sweep_idx, sweep_idx)
                continue
            if mc["c"] > mss_target:
                ext = scan[swing_idx:sweep_idx + 1]
                return {
                    "bull":          True,
                    "signal":        "MSS",
                    "range_high":    mss_target,
                    "range_low":     min(c["l"] for c in ext),
                    "range_candles": len(ext),
                    "mss_close":     mc["c"],
                    "swept_level":   True,
                    "broke":         "ABOVE range high",
                    "sweep_time":    ts(sweep_c["t"]),
                    "sweep_wick":    sweep_c["l"],
                    "sweep_close":   sweep_c["c"],
                    "range_open":    ts(ext[0]["t"]),
                    "range_close":   ts(ext[-1]["t"]),
                    "mss_open":      ts(mc["t"]),
                }
        else:
            if mc["h"] > sweep_c["h"] and mc["c"] < lp:
                prev_sweep_idx = sweep_idx
                sweep_idx = i; sweep_c = mc
                mss_target, swing_idx = calc_target(prev_sweep_idx, sweep_idx)
                continue
            if mc["c"] < mss_target:
                ext = scan[swing_idx:sweep_idx + 1]
                return {
                    "bull":          False,
                    "signal":        "MSS",
                    "range_high":    max(c["h"] for c in ext),
                    "range_low":     mss_target,
                    "range_candles": len(ext),
                    "mss_close":     mc["c"],
                    "swept_level":   True,
                    "broke":         "BELOW range low",
                    "sweep_time":    ts(sweep_c["t"]),
                    "sweep_wick":    sweep_c["h"],
                    "sweep_close":   sweep_c["c"],
                    "range_open":    ts(ext[0]["t"]),
                    "range_close":   ts(ext[-1]["t"]),
                    "mss_open":      ts(mc["t"]),
                }
    return None


# ── ALERT FORMATTERS ──────────────────────────────────────────
def format_alert_a(lv, cur_price, grade, bias, tr1w, tr1d, has_1d):
    bull = lv["type"] == "V"
    gl   = "A+" if grade=="aplus" else grade.upper()
    pf   = lambda p: f"${p:,.0f}"
    shape = "V-shape" if bull else "A-shape"
    tf    = "1D+4H" if has_1d else "4H"
    return "\n".join([
        f"🔔 AT KEY LEVEL — WATCH 1H",
        f"",
        f"4H Level : {pf(lv['price'])}  ({shape} · {'Fresh' if lv['fresh'] else 'Used'} · {tf})",
        f"Price    : {pf(cur_price)}  ({abs(cur_price-lv['price'])/lv['price']*100:.2f}% away)",
        f"Bias     : {bias}  |  Grade: {gl}  |  BTCUSDT",
        f"",
        f"→ Open 1H, watch for sweep + MSS/BREAK",
        f"{now_ist()}",
    ])

def format_alert_b(lv, mss, cur_price, grade, bias, tr1w, tr1d, has_1d):
    bull   = mss["bull"]
    signal = mss.get("signal", "MSS")
    gl     = "A+" if grade=="aplus" else grade.upper()
    pf     = lambda p: f"${p:,.0f}"
    shape  = "V-shape" if bull else "A-shape"
    tf     = "1D+4H" if has_1d else "4H"
    n      = mss["range_candles"]
    rclr   = "red" if bull else "green"
    wick   = pf(mss["sweep_wick"])

    if signal == "MSS":
        header = f"🚀 LONG — SWEEP+MSS" if bull else f"💥 SHORT — SWEEP+MSS"
    else:
        header = f"⚡ LONG — BREAK" if bull else f"⚡ SHORT — BREAK"

    return "\n".join([
        f"{header}",
        f"",
        f"4H Level  : {pf(lv['price'])}  ({shape} · {'Fresh' if lv['fresh'] else 'Used'} · {tf} · {mss['sweep_time']} IST)",
        f"1H Sweep  : {mss['sweep_time']} IST  wick→{wick}  close→{pf(mss['sweep_close'])}",
        f"Ext Range : {n}x {rclr}  |  {pf(mss['range_low'])}—{pf(mss['range_high'])}  |  {mss['range_open']}→{mss['range_close']} IST",
        f"{signal}       : {mss['mss_open']} IST  close {pf(mss['mss_close'])}",
        f"",
        f"Bias: {bias}  |  Grade: {gl}  |  BTCUSDT",
        f"{now_ist()}",
    ])


# ── SIMULATOR ─────────────────────────────────────────────────
def auto_enter_trade(state, mss, lv, grade, bias, cur_price, h4_levels):
    """
    Auto enter trade on MSS confirmation:
    - Entry  = MSS candle close price
    - SL     = sweep candle low (LONG) / high (SHORT) with 0.1% buffer
    - TPs    = all 4H levels beyond entry in trade direction, sorted nearest first
    """
    bull    = mss["bull"]
    entry   = round(mss["mss_close"], 2)
    sl_raw  = mss.get("sweep_wick", mss.get("sweep_low", mss.get("sweep_high", entry)))
    buffer  = entry * 0.001  # 0.1% buffer on SL

    if bull:
        sl  = round(sl_raw * (1 - 0.001), 2)  # slightly below sweep low
        tps = sorted(
            [lv["price"] for lv in h4_levels if lv["price"] > entry and lv["type"] == "A"],
        )
    else:
        sl  = round(sl_raw * (1 + 0.001), 2)  # slightly above sweep high
        tps = sorted(
            [lv["price"] for lv in h4_levels if lv["price"] < entry and lv["type"] == "V"],
            reverse=True
        )

    if not tps: return  # no target levels found
    risk = abs(entry - sl)
    if risk <= 0: return

    trade_id = f"auto_{int(datetime.now(timezone.utc).timestamp())}"
    # avoid duplicate entries for same MSS
    if any(t.get("id") == trade_id or
           (t.get("rawEntry") == entry and t.get("bull") == bull)
           for t in state["activeTrades"]):
        return

    gl = "A+" if grade == "aplus" else grade.upper()
    trade = {
        "id":           trade_id,
        "bull":         bull,
        "direction":    "LONG" if bull else "SHORT",
        "grade":        gl,
        "level":        round(lv["price"], 2),
        "rawEntry":     entry,
        "rawSL":        sl,
        "rawRisk":      round(risk, 2),
        "tpLevels":     [round(t, 2) for t in tps],   # all TP levels
        "tpIndex":      0,                              # which TP we're targeting
        "rawTP1":       round(tps[0], 2),
        "rawTP2":       round(tps[1], 2) if len(tps) > 1 else round(tps[0], 2),
        "phase":        "entry",
        "entryBalance": round(state["balance"], 2),
        "enteredAt":    datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        "bias":         bias,
        "auto":         True,
    }
    state["activeTrades"].append(trade)
    print(f"  → AUTO TRADE: {'LONG' if bull else 'SHORT'} entry ${entry:,.0f} sl ${sl:,.0f} tp1 ${tps[0]:,.0f} ({len(tps)} levels)")


def update_simulator(state, cur_price):
    still = []
    for t in state["activeTrades"]:
        bull    = t.get("bull", t["direction"] == "LONG")
        entry   = t["rawEntry"]
        sl      = t["rawSL"]
        risk    = t.get("rawRisk", abs(entry - sl))
        if risk <= 0: still.append(t); continue

        riskAmt = t.get("entryBalance", SIM_START) * (RISK_PCT.get(t.get("grade","b").lower().replace("+","plus"), 1.0) / 100)
        posSize = riskAmt / risk
        phase   = t.get("phase", "entry")
        tpLevels = t.get("tpLevels", [t.get("rawTP1", entry), t.get("rawTP2", entry)])
        tpIndex  = t.get("tpIndex", 0)

        now_ts = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

        # ── SL hit ──────────────────────────────────────────
        if (bull and cur_price <= sl) or (not bull and cur_price >= sl):
            pnl = -riskAmt if phase == "entry" else 0.0
            result = "loss" if phase == "entry" else "be"
            if phase == "entry":
                state["balance"] -= riskAmt
                state["stats"]["losses"] += 1
                state["stats"]["netR"] = round(state["stats"]["netR"] - 1, 2)
            else:
                state["stats"]["be"] += 1
            state["stats"]["totalTrades"] += 1
            state["history"].insert(0, {**t, "result": result, "pnl": round(pnl, 2),
                "pnlR": -1.0 if result == "loss" else 0.0,
                "closePrice": round(cur_price, 2), "closedAt": now_ts})
            state["equityCurve"].append({"t": now_ts, "v": round(state["balance"], 2)})
            continue

        # ── TP level hit — trail to next ────────────────────
        if tpIndex < len(tpLevels):
            current_tp = tpLevels[tpIndex]
            if (bull and cur_price >= current_tp) or (not bull and cur_price <= current_tp):
                profit = posSize * abs(current_tp - entry)
                state["balance"] += profit
                netR = round(abs(current_tp - entry) / risk, 1)
                state["stats"]["netR"] = round(state["stats"]["netR"] + netR, 2)
                state["stats"]["bestR"] = max(state["stats"]["bestR"], netR)

                state["history"].insert(0, {**t, "result": f"tp{tpIndex+1}", "pnl": round(profit, 2),
                    "pnlR": netR, "closePrice": round(cur_price, 2), "closedAt": now_ts})
                state["equityCurve"].append({"t": now_ts, "v": round(state["balance"], 2)})

                # Check if more TP levels exist — trail
                if tpIndex + 1 < len(tpLevels):
                    # Trail SL to previous TP (or entry if first TP)
                    t["rawSL"]   = entry if tpIndex == 0 else tpLevels[tpIndex - 1]
                    t["tpIndex"] = tpIndex + 1
                    t["rawTP1"]  = tpLevels[tpIndex + 1]
                    t["phase"]   = f"tp{tpIndex+1}"
                    still.append(t)
                else:
                    # All TPs hit — final win
                    state["stats"]["wins"] += 1
                    state["stats"]["totalTrades"] += 1
                continue

        still.append(t)

    state["activeTrades"] = still
    state["history"]      = state["history"][:100]
    state["equityCurve"]  = state["equityCurve"][-200:]



# ── MAIN ──────────────────────────────────────────────────────
def main():
    print(f"\n=== MSNR Scanner v9  {datetime.now(timezone.utc).replace(tzinfo=None).strftime('%Y-%m-%d %H:%M UTC')} ===\n")
    state, sha = load_state()
    print(f"Balance: ${state['balance']:.2f} | Active trades: {len(state['activeTrades'])}")

    print("\nFetching candles...")
    w1 = fetch_candles("BTC","1w",52);  time.sleep(1)
    d1 = fetch_candles("BTC","1d",90);  time.sleep(1)
    h4 = fetch_candles("BTC","4h",200); time.sleep(1)
    h1 = fetch_candles("BTC","1h",250)

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

    lvl_4h = find_key_levels(h4, max_dist_pct=15.0)
    lvl_1d = find_key_levels(d1, max_dist_pct=15.0)
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
            # Find MSS first to get its timestamp for the unique key
            mss = find_1h_mss(h1, lv)
            if mss:
                # Key includes MSS candle time — so each unique MSS fires exactly once, forever
                mss_ts_str = str(mss.get("mss_open",""))
                key_b = lv_key + "_MSS_" + mss_ts_str.replace(" ","_").replace(":","")
                if not was_alerted(state, "mssAlerts", key_b, 999999):  # never expire
                    msg = format_alert_b(lv, mss, cur_price, grade, bias, tr1w, tr1d, has_1d)
                    send_telegram(msg)
                    mark_alert(state, "mssAlerts", key_b)
                    alerts_sent += 1
                    # Auto enter simulator trade
                    auto_enter_trade(state, mss, lv, grade, bias, cur_price, lvl_4h)
                    # Save last alert for website
                    state["lastAlert"] = {
                        "type":      "MSS",
                        "direction": "LONG" if mss["bull"] else "SHORT",
                        "level":     round(lv["price"], 2),
                        "grade":     "A+" if grade == "aplus" else grade.upper(),
                        "has1d":     has_1d,
                        "sweepTime": mss["sweep_time"],
                        "mssTime":   mss["mss_open"],
                        "mssClose":  round(mss["mss_close"], 2),
                        "bias":      bias,
                        "time":      datetime.now(IST).strftime("%d %b %Y  %I:%M %p IST"),
                    }
                    print(f"  → ALERT B: 1H MSS {'bullish' if mss['bull'] else 'bearish'} "
                          f"| range {mss['range_candles']}c "
                          f"| broke {mss['broke']} @ ${mss['mss_close']:,.0f}")
                else:
                    print(f"  → MSS already alerted")
            else:
                print(f"  → In zone, no 1H MSS yet")

    print(f"\n{'No alerts this run' if alerts_sent==0 else str(alerts_sent)+' alert(s) sent'}")
    update_simulator(state, cur_price)

    # ── Save current levels to state for website display ──────
    state["currentLevels"] = []
    for lv in lvl_4h:
        has_1d = any(
            l["type"] == lv["type"] and abs(l["price"] - lv["price"]) / lv["price"] * 100 < 1.0
            for l in lvl_1d
        )
        grade = grade_level(lv, has_1d)
        gl    = "A+" if grade == "aplus" else grade.upper()
        state["currentLevels"].append({
            "type":   lv["type"],
            "price":  round(lv["price"], 2),
            "fresh":  lv["fresh"],
            "hsl":    lv["hsl"],
            "has1d":  has_1d,
            "grade":  gl,
            "dist":   round(abs(cur_price - lv["price"]) / lv["price"] * 100, 2),
        })

    state["currentPrice"] = round(cur_price, 2)
    state["currentBias"]  = bias
    state["currentTrend"] = {"w1": tr1w, "d1": tr1d}
    state["scanTime"]     = datetime.now(IST).strftime("%d %b %Y  %I:%M %p IST")

    save_state(state, sha)
    print("=== Done ===")

if __name__ == "__main__":
    main()
