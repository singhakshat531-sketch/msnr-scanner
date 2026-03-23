#!/usr/bin/env python3
"""
MSNR Scanner v10 — Clean Rebuild
══════════════════════════════════════════════════════════════════

STRATEGY (exact):

  4H KEY LEVELS
  ─────────────
  A-level : bull candle (c0) → bear candle (c1)
            price  = c0.close
            confirmed at c1.open + 4h (c1 fully closed)
            DEAD if any later 4H candle CLOSES above price

  V-level : bear candle (c0) → bull candle (c1)
            price  = c0.close
            confirmed at c1.open + 4h
            DEAD if any later 4H candle CLOSES below price

  freshness: 0 wicks through price after formation = FRESH

  1H SWEEP + MSS
  ──────────────
  Only look at 1H candles AFTER the level confirmation timestamp.

  SHORT sweep (A-level): 1H wick > level AND close < level
  LONG  sweep (V-level): 1H wick < level AND close > level

  Re-sweep: if another sweep candle appears before MSS fires,
            it becomes the new active sweep. MSS target is
            recalculated from prev_sweep_idx → new_sweep_idx.

  MSS target (SHORT): highest swing high between prev_sweep and
                      active sweep. Swing high = bull candle
                      followed by bear candle → c0.high.
                      If sweep candle.high > any found swing →
                      use sweep candle.high instead.
                      If no swings at all → sweep candle.high.

  MSS target (LONG):  lowest swing low between prev_sweep and
                      active sweep. Swing low = bear candle
                      followed by bull candle → c0.low.
                      Fall back to sweep candle.low.

  MSS fires: 1H candle BODY CLOSES beyond the target.
             SHORT → close < target
             LONG  → close > target

  ALERT GATE
  ──────────
  Alert fires only if MSS candle open time is within last 2h.
  Prevents re-alerting stale setups on scanner startup.
  Each MSS event is keyed by level_price + mss_candle_time.
  Never re-alerted once sent.

  GRADE
  ─────
  Fresh level   → +2
  1D confluence → +2  (same-type 1D level within 1.5% of price)
  HSL           → +1  (future: hardcoded False for now)
  Dist < 0.5%   → +1
  ≥5 = A+,  ≥3 = A,  else B

  Bias adds confluence to grade but does NOT block alerts.
  A SHORT at a bearish-bias level scores higher, but a SHORT
  at a bullish-bias level still fires if sweep+MSS are valid.

══════════════════════════════════════════════════════════════════
"""

import json, urllib.request, urllib.parse, os, time, base64
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

def now_ist():
    return datetime.now(IST).strftime('%d %b %Y  %I:%M %p IST')

def fmt_ts(ts_ms):
    return datetime.fromtimestamp(ts_ms / 1000, IST).strftime('%d %b  %I:%M %p')

# ── CONFIG ────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
GITHUB_PAT       = os.environ.get("PAT_TOKEN", "")
GITHUB_REPO      = "singhakshat531-sketch/msnr-scanner"
DATA_FILE        = "trades.json"

SIM_START        = 1000.0
RISK_PCT         = {"A+": 3.0, "A": 2.0, "B": 1.0}
MSS_RECENT_HOURS = 2      # only alert if MSS fired within last 2h
H1_LOOKBACK      = 300    # 1H candles to scan for sweep+MSS

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
        "User-Agent": "MSNR-Scanner/10"
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
        "message": f"scan {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "content": base64.b64encode(json.dumps(content, indent=2).encode()).decode(),
        "branch": "main"
    }
    if sha: body["sha"] = sha
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={
        "Authorization": f"token {GITHUB_PAT}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json",
        "User-Agent": "MSNR-Scanner/10"
    }, method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=15) as r: json.loads(r.read())
        print("  GitHub write OK"); return True
    except Exception as e:
        print(f"  GitHub write error: {e}"); return False

def load_state():
    data, sha = github_read(DATA_FILE)
    if data is None:
        data = {
            "balance":      SIM_START,
            "activeTrades": [],
            "history":      [],
            "equityCurve":  [{"t": datetime.now(timezone.utc).isoformat(), "v": SIM_START}],
            "stats":        {"totalTrades": 0, "wins": 0, "losses": 0, "be": 0, "netR": 0.0, "bestR": 0.0},
            "mssAlerts":    {},
            "levelAlerts":  {},
        }
    data.setdefault("mssAlerts", {})
    data.setdefault("levelAlerts", {})
    return data, sha

def save_state(data, sha):
    data["lastUpdated"] = datetime.now(timezone.utc).isoformat()
    return github_write(DATA_FILE, data, sha)

# ── FETCH ─────────────────────────────────────────────────────
def fetch_candles(symbol, interval, limit=200):
    endpoint  = "histoday" if interval in ("1d", "1w") else "histohour"
    aggregate = {"1w": 7, "1d": 1, "4h": 4, "1h": 1}.get(interval, 1)
    url = (f"https://min-api.cryptocompare.com/data/v2/{endpoint}"
           f"?fsym={symbol}&tsym=USD&limit={limit}&aggregate={aggregate}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MSNR-Scanner/10"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
        if data.get("Response") != "Success":
            print(f"  API error {symbol} {interval}: {data.get('Message', '?')}"); return []
        candles = [
            {"t": k["time"] * 1000, "o": float(k["open"]), "h": float(k["high"]),
             "l": float(k["low"]),  "c": float(k["close"])}
            for k in data["Data"]["Data"]
            if not (k["open"] == 0 and k["close"] == 0)
        ]
        print(f"  {symbol} {interval}: {len(candles)} candles, "
              f"last={fmt_ts(candles[-1]['t']) if candles else 'none'}")
        return candles
    except Exception as e:
        print(f"  Fetch error {symbol} {interval}: {e}"); return []

# ── TREND & BIAS ──────────────────────────────────────────────
def get_trend(candles, lookback=20):
    if len(candles) < lookback: return "UNKNOWN"
    c    = [x["c"] for x in candles[-lookback:]]
    half = len(c) // 2
    avg_s = sum(c[:half]) / half
    avg_e = sum(c[half:]) / half
    pct   = (avg_e - avg_s) / avg_s * 100
    bull  = sum(1 for i in range(2, len(c)) if c[i] > c[i-1] > c[i-2])
    bear  = sum(1 for i in range(2, len(c)) if c[i] < c[i-1] < c[i-2])
    if pct > 3  and bull > bear:      return "BULLISH"
    if pct < -3 and bear > bull:      return "BEARISH"
    if pct > 2  and bull > bear*1.5:  return "BULLISH"
    if pct < -2 and bear > bull*1.5:  return "BEARISH"
    return "RANGING"

def get_bias(tr1w, tr1d):
    """
    Bias: confluence only. Does NOT block alerts — only affects grade.
    """
    if tr1w == "BULLISH" and tr1d in ("BULLISH", "RANGING"): return "BULLISH"
    if tr1w == "BEARISH" and tr1d in ("BEARISH", "RANGING"): return "BEARISH"
    if tr1w == "RANGING":
        if tr1d == "BULLISH": return "BULLISH"
        if tr1d == "BEARISH": return "BEARISH"
    if tr1d == "BULLISH": return "BULLISH"
    if tr1d == "BEARISH": return "BEARISH"
    return "RANGING"

# ── 4H KEY LEVELS ─────────────────────────────────────────────
MIN_BODY_PCT = 0.1   # c0 body must be at least 0.1% of price
DEDUP_PCT    = 0.5   # levels of same type within 0.5% → keep most recent only

def find_key_levels(candles):
    """
    Scan all 4H candle pairs for A/V levels.
    Rules:
    - c0 must have a real body (>= MIN_BODY_PCT of price)
    - Kill any level where a subsequent 4H candle closes beyond the price
    - Deduplicate: same-type levels within DEDUP_PCT% → keep most recent
    - Sort newest confirmed first
    """
    if len(candles) < 2: return []
    raw = []

    for i in range(len(candles) - 1):
        c0 = candles[i]
        c1 = candles[i + 1]

        bull0 = c0["c"] > c0["o"]
        bear0 = c0["c"] < c0["o"]
        bull1 = c1["c"] > c1["o"]
        bear1 = c1["c"] < c1["o"]

        if bull0 and bear1:
            ltype = "A"
        elif bear0 and bull1:
            ltype = "V"
        else:
            continue

        price = c0["c"]

        # Minimum body size — filters doji and near-doji candles
        body_pct = abs(c0["c"] - c0["o"]) / c0["o"] * 100
        if body_pct < MIN_BODY_PCT:
            continue

        # Dead level check
        dead  = False
        wicks = 0
        for fc in candles[i + 2:]:
            if ltype == "A":
                if fc["c"] > price: dead = True; break
                if fc["h"] > price: wicks += 1
            else:
                if fc["c"] < price: dead = True; break
                if fc["l"] < price: wicks += 1

        if dead: continue

        raw.append({
            "type":         ltype,
            "price":        price,
            "fresh":        wicks == 0,
            "hsl":          False,
            # CryptoCompare time = open time of candle
            # Level confirmed when c1 fully closes = c1 open + 4h
            "confirmed_ts": c1["t"] + 4 * 3600 * 1000,
            "c0_open_ts":   c0["t"],
        })

    # Deduplicate — same type within DEDUP_PCT% → keep most recent (highest confirmed_ts)
    raw.sort(key=lambda x: x["confirmed_ts"], reverse=True)
    levels = []
    for lv in raw:
        duplicate = any(
            l["type"] == lv["type"]
            and abs(l["price"] - lv["price"]) / lv["price"] * 100 < DEDUP_PCT
            for l in levels
        )
        if not duplicate:
            levels.append(lv)

    return levels  # already sorted newest first

def grade_level(lv, has_1d, bias, cur_price):
    """
    Grade based on level quality. Bias adds confluence but doesn't block.
    """
    score = 0
    bull  = lv["type"] == "V"

    if lv["fresh"]: score += 2
    if has_1d:      score += 2
    if lv["hsl"]:   score += 1
    dist = abs(cur_price - lv["price"]) / lv["price"] * 100
    if dist < 0.5:  score += 1

    # Bias confluence
    if (bull and bias == "BULLISH") or (not bull and bias == "BEARISH"):
        score += 1

    return "A+" if score >= 5 else "A" if score >= 3 else "B"

# ── 1H SWEEP + MSS ────────────────────────────────────────────
def calc_mss_target(scan, from_idx, to_idx, bull):
    """
    Find MSS target by scanning the window from prev_sweep to sweep candle.
    Take the MOST RECENT valid swing pair (closest to sweep candle).

    SHORT (bull=False): most recent bear+bull pair → c0.low
    LONG  (bull=True):  most recent bull+bear pair → c0.high
    Fallback: sweep candle extreme if no pair found.
    """
    sweep_c = scan[to_idx]

    if bull:
        # LONG: scan forwards, track last found bull+bear → c0.high
        target = None
        for j in range(from_idx, to_idx - 1):
            c0, c1 = scan[j], scan[j + 1]
            if c0["c"] > c0["o"] and c1["c"] < c1["o"]:
                target = c0["h"]   # keep updating — last one wins
        return target if target is not None else sweep_c["h"]
    else:
        # SHORT: scan forwards, track last found bear+bull → c0.low
        target = None
        for j in range(from_idx, to_idx - 1):
            c0, c1 = scan[j], scan[j + 1]
            if c0["c"] < c0["o"] and c1["c"] > c1["o"]:
                target = c0["l"]   # keep updating — last one wins
        return target if target is not None else sweep_c["l"]

def find_mss(h1, level):
    """
    Scan 1H candles for sweep + MSS at the given 4H key level.

    Returns dict with all setup details, or None if no valid setup.
    """
    lp              = level["price"]
    # confirmed_ts = c1 open + 4h = exact moment level confirms
    # First valid 1H candle has open time >= confirmed_ts
    confirmed_ts    = level["confirmed_ts"]
    bull            = level["type"] == "V"

    # Exclude last candle — it may still be forming (not yet closed)
    scan = [c for c in h1[-H1_LOOKBACK:-1] if c["t"] >= confirmed_ts]
    n    = len(scan)
    if n < 2: return None

    # ── Find first sweep ──────────────────────────────────────
    sweep_idx = None
    for i in range(n - 1):
        c = scan[i]
        if bull and c["l"] < lp and c["c"] > lp:
            sweep_idx = i; break
        elif not bull and c["h"] > lp and c["c"] < lp:
            sweep_idx = i; break

    if sweep_idx is None: return None

    prev_sweep_idx = 0            # start of confirmed window
    sweep_c        = scan[sweep_idx]
    mss_target     = calc_mss_target(scan, prev_sweep_idx, sweep_idx, bull)

    # ── Scan for re-sweeps and MSS ────────────────────────────
    mss_result = None

    for i in range(sweep_idx + 1, n):
        mc = scan[i]

        if bull:
            if mc["l"] < lp and mc["c"] > lp:
                prev_sweep_idx = sweep_idx
                sweep_idx      = i
                sweep_c        = mc
                mss_target     = calc_mss_target(scan, prev_sweep_idx, sweep_idx, bull)
                mss_result     = None  # new sweep resets MSS
                continue
            if mss_result is None and mc["c"] > mss_target and mc["c"] > lp:
                mss_result = {
                    "bull":        True,
                    "sweep_ts":    sweep_c["t"],
                    "sweep_wick":  sweep_c["l"],
                    "sweep_close": sweep_c["c"],
                    "mss_ts":      mc["t"],
                    "mss_close":   mc["c"],
                    "mss_target":  mss_target,
                }
        else:
            if mc["h"] > lp and mc["c"] < lp:
                prev_sweep_idx = sweep_idx
                sweep_idx      = i
                sweep_c        = mc
                mss_target     = calc_mss_target(scan, prev_sweep_idx, sweep_idx, bull)
                mss_result     = None  # new sweep resets MSS
                continue
            if mss_result is None and mc["c"] < mss_target and mc["c"] < lp:
                mss_result = {
                    "bull":        False,
                    "sweep_ts":    sweep_c["t"],
                    "sweep_wick":  sweep_c["h"],
                    "sweep_close": sweep_c["c"],
                    "mss_ts":      mc["t"],
                    "mss_close":   mc["c"],
                    "mss_target":  mss_target,
                }

    return mss_result

# ── ALERT HELPERS ─────────────────────────────────────────────
def already_alerted(state, key):
    return key in state["mssAlerts"]

def mark_alerted(state, key):
    state["mssAlerts"][key] = datetime.now(timezone.utc).isoformat()

def mss_is_recent(mss_ts_ms):
    """True if MSS candle is within last MSS_RECENT_HOURS hours."""
    age = (datetime.now(timezone.utc).timestamp() * 1000 - mss_ts_ms) / 3600000
    return age <= MSS_RECENT_HOURS

def sweep_is_fresh(sweep_ts_ms, max_hours=24):
    """True if sweep candle is within last max_hours. Stale sweeps = invalid setups."""
    age = (datetime.now(timezone.utc).timestamp() * 1000 - sweep_ts_ms) / 3600000
    return age <= max_hours

def alert_key(mss):
    """Unique key per MSS event — keyed by MSS candle time only.
    Prevents double-alerting when two near-identical levels find the same MSS candle."""
    return f"MSS_{mss['mss_ts']}"

# ── TELEGRAM ──────────────────────────────────────────────────
def send_telegram(msg):
    if not TELEGRAM_TOKEN:
        print("  [no token]\n" + msg); return
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"
    }).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=10) as r:
            res = json.loads(r.read())
        print("  Telegram ✓" if res.get("ok") else f"  Telegram error: {res}")
    except Exception as e:
        print(f"  Telegram error: {e}")

def format_alert(lv, mss, grade, bias, has_1d, cur_price):
    bull   = mss["bull"]
    pf     = lambda p: f"${p:,.0f}"
    gl     = grade
    shape  = "V" if bull else "A"
    conf   = "1D+4H ✓" if has_1d else "4H only"
    fresh  = "Fresh" if lv["fresh"] else "Used"
    header = "🚀 LONG  — SWEEP + MSS" if bull else "💥 SHORT — SWEEP + MSS"

    # c0 open time for TradingView navigation
    c0_time  = fmt_ts(lv["c0_open_ts"])
    sw_time  = fmt_ts(mss["sweep_ts"])
    mss_time = fmt_ts(mss["mss_ts"])

    direction_bias = "WITH bias ✓" if (
        (bull and bias == "BULLISH") or (not bull and bias == "BEARISH")
    ) else "AGAINST bias"

    return "\n".join([
        header,
        "─" * 32,
        "",
        f"Level    {pf(lv['price'])}  [{shape}-shape · {fresh} · {conf}]",
        f"Signal   {c0_time} IST  ← open on TradingView",
        "",
        f"Sweep    {sw_time} IST",
        f"  wick → {pf(mss['sweep_wick'])}   close → {pf(mss['sweep_close'])}",
        "",
        f"MSS      {mss_time} IST",
        f"  target → {pf(mss['mss_target'])}   close → {pf(mss['mss_close'])}",
        "",
        "─" * 32,
        f"Grade: {gl}   Bias: {bias} ({direction_bias})",
        f"BTCUSDT · {now_ist()}",
    ])

# ── SIMULATOR ─────────────────────────────────────────────────
def auto_enter_trade(state, lv, mss, grade, bias, all_levels):
    """
    Auto enter sim trade on MSS confirmation.
    Entry  = MSS candle close
    SL     = sweep candle extreme (wick) with 0.1% buffer
    TPs    = all valid 4H levels beyond entry in trade direction
    """
    bull   = mss["bull"]
    entry  = round(mss["mss_close"], 2)
    sl_raw = mss["sweep_wick"]

    if bull:
        sl  = round(sl_raw * 0.999, 2)
        tps = sorted([l["price"] for l in all_levels
                      if l["type"] == "A" and l["price"] > entry])
    else:
        sl  = round(sl_raw * 1.001, 2)
        tps = sorted([l["price"] for l in all_levels
                      if l["type"] == "V" and l["price"] < entry], reverse=True)

    if not tps:
        print("  → No TP levels found, skipping auto trade"); return

    risk = abs(entry - sl)
    if risk <= 0: return

    trade_id = f"t_{int(datetime.now(timezone.utc).timestamp())}"
    # Deduplicate
    if any(t.get("id") == trade_id or
           (abs(t.get("rawEntry", 0) - entry) < 10 and t.get("bull") == bull)
           for t in state["activeTrades"]):
        return

    trade = {
        "id":           trade_id,
        "bull":         bull,
        "direction":    "LONG" if bull else "SHORT",
        "grade":        grade,
        "level":        round(lv["price"], 2),
        "rawEntry":     entry,
        "rawSL":        sl,
        "rawRisk":      round(risk, 2),
        "tpLevels":     [round(t, 2) for t in tps],
        "tpIndex":      0,
        "rawTP1":       round(tps[0], 2),
        "rawTP2":       round(tps[1], 2) if len(tps) > 1 else round(tps[0], 2),
        "phase":        "entry",
        "entryBalance": round(state["balance"], 2),
        "enteredAt":    datetime.now(timezone.utc).isoformat(),
        "bias":         bias,
    }
    state["activeTrades"].append(trade)
    print(f"  → Auto trade: {'LONG' if bull else 'SHORT'} "
          f"entry {entry:,.0f}  sl {sl:,.0f}  tp1 {tps[0]:,.0f}  ({len(tps)} targets)")

def update_simulator(state, cur_price):
    """Check active trades against current price. Hit SL or TP → close."""
    still    = []
    risk_map = {"A+": 3.0, "A": 2.0, "B": 1.0}
    now_ts   = datetime.now(timezone.utc).isoformat()

    for t in state["activeTrades"]:
        bull     = t["bull"]
        entry    = t["rawEntry"]
        sl       = t["rawSL"]
        risk     = t["rawRisk"]
        phase    = t.get("phase", "entry")
        tp_lvls  = t.get("tpLevels", [t.get("rawTP1", entry)])
        tp_idx   = t.get("tpIndex", 0)
        risk_pct = risk_map.get(t.get("grade", "B"), 1.0)
        risk_amt = t.get("entryBalance", SIM_START) * risk_pct / 100

        if risk <= 0: still.append(t); continue

        # SL hit
        if (bull and cur_price <= sl) or (not bull and cur_price >= sl):
            pnl    = 0.0 if phase != "entry" else -risk_amt
            result = "be" if phase != "entry" else "loss"
            if result == "loss":
                state["balance"] -= risk_amt
                state["stats"]["losses"]    += 1
                state["stats"]["netR"]      = round(state["stats"]["netR"] - 1, 2)
            else:
                state["stats"]["be"]        += 1
            state["stats"]["totalTrades"]   += 1
            state["history"].insert(0, {**t, "result": result,
                "pnl": round(pnl, 2), "pnlR": -1.0 if result == "loss" else 0.0,
                "closePrice": round(cur_price, 2), "closedAt": now_ts})
            state["equityCurve"].append({"t": now_ts, "v": round(state["balance"], 2)})
            continue

        # TP hit
        if tp_idx < len(tp_lvls):
            tp = tp_lvls[tp_idx]
            if (bull and cur_price >= tp) or (not bull and cur_price <= tp):
                pos_size = risk_amt / risk
                profit   = pos_size * abs(tp - entry)
                net_r    = round(abs(tp - entry) / risk, 1)
                state["balance"] += profit
                state["stats"]["netR"]  = round(state["stats"]["netR"] + net_r, 2)
                state["stats"]["bestR"] = max(state["stats"]["bestR"], net_r)
                state["history"].insert(0, {**t, "result": f"tp{tp_idx+1}",
                    "pnl": round(profit, 2), "pnlR": net_r,
                    "closePrice": round(cur_price, 2), "closedAt": now_ts})
                state["equityCurve"].append({"t": now_ts, "v": round(state["balance"], 2)})

                if tp_idx + 1 < len(tp_lvls):
                    # Trail SL to entry (first TP) or previous TP
                    t["rawSL"]   = entry if tp_idx == 0 else tp_lvls[tp_idx - 1]
                    t["tpIndex"] = tp_idx + 1
                    t["phase"]   = f"tp{tp_idx + 1}"
                    still.append(t)
                else:
                    state["stats"]["wins"]        += 1
                    state["stats"]["totalTrades"] += 1
                continue

        still.append(t)

    state["activeTrades"] = still
    state["history"]      = state["history"][:100]
    state["equityCurve"]  = state["equityCurve"][-200:]

# ── MAIN ──────────────────────────────────────────────────────
def main():
    print(f"\n=== MSNR Scanner v10  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} ===\n")

    state, sha = load_state()
    print(f"Balance: ${state['balance']:.2f}  |  Active trades: {len(state['activeTrades'])}\n")

    # ── Fetch data ────────────────────────────────────────────
    print("Fetching candles...")
    w1 = fetch_candles("BTC", "1w", 52);  time.sleep(1)
    d1 = fetch_candles("BTC", "1d", 90);  time.sleep(1)
    h4 = fetch_candles("BTC", "4h", 200); time.sleep(1)
    h1 = fetch_candles("BTC", "1h", 300)

    if not h4 or not h1:
        print("Insufficient data — aborting"); return

    cur_price = h1[-1]["c"]
    tr1w      = get_trend(w1, 12) if w1 else "UNKNOWN"
    tr1d      = get_trend(d1, 20) if d1 else "UNKNOWN"
    tr4h      = get_trend(h4, 42)
    bias      = get_bias(tr1w, tr1d)

    print(f"\nBTC: ${cur_price:,.0f}  |  1W:{tr1w}  1D:{tr1d}  4H:{tr4h}  |  Bias:{bias}\n")

    # ── Find all valid 4H levels ──────────────────────────────
    h4_levels = find_key_levels(h4)
    d1_levels = find_key_levels(d1) if d1 else []
    print(f"Valid 4H levels: {len(h4_levels)}  |  Valid 1D levels: {len(d1_levels)}\n")

    alerts_sent = 0

    for lv in h4_levels:
        bull = lv["type"] == "V"
        lp   = lv["price"]
        dist = abs(cur_price - lp) / lp * 100

        has_1d = any(
            l["type"] == lv["type"]
            and abs(l["price"] - lp) / lp * 100 < 1.5
            for l in d1_levels
        )
        grade = grade_level(lv, has_1d, bias, cur_price)

        print(f"  {lv['type']} ${lp:,.0f}  {'FRESH' if lv['fresh'] else 'USED'}  "
              f"grade:{grade}  dist:{dist:.1f}%"
              f"{'  [1D+4H]' if has_1d else ''}")

        # ── Scan for sweep + MSS ──────────────────────────────
        mss = find_mss(h1, lv)
        if not mss:
            print(f"    → no MSS yet")
            continue

        key = alert_key(mss)

        if already_alerted(state, key):
            print(f"    → MSS found but already alerted  ({fmt_ts(mss['mss_ts'])} IST)")
            continue

        if not mss_is_recent(mss["mss_ts"]):
            print(f"    → MSS found but stale  ({fmt_ts(mss['mss_ts'])} IST)")
            mark_alerted(state, key)
            continue

        if not sweep_is_fresh(mss["sweep_ts"]):
            print(f"    → Sweep too old ({fmt_ts(mss['sweep_ts'])} IST) — skipping")
            mark_alerted(state, key)
            continue

        # ── Fire alert ────────────────────────────────────────
        print(f"    → {'LONG' if bull else 'SHORT'} MSS  "
              f"sweep:{fmt_ts(mss['sweep_ts'])}  mss:{fmt_ts(mss['mss_ts'])}  "
              f"close:{mss['mss_close']:,.0f}")

        msg = format_alert(lv, mss, grade, bias, has_1d, cur_price)
        send_telegram(msg)
        mark_alerted(state, key)
        alerts_sent += 1

        # Enter sim trade
        auto_enter_trade(state, lv, mss, grade, bias, h4_levels)

        # Save last alert for dashboard
        state["lastAlert"] = {
            "type":       "MSS",
            "direction":  "LONG" if bull else "SHORT",
            "level":      round(lp, 2),
            "grade":      grade,
            "has1d":      has_1d,
            "sweepTime":  fmt_ts(mss["sweep_ts"]),
            "mssTime":    fmt_ts(mss["mss_ts"]),
            "mssClose":   round(mss["mss_close"], 2),
            "mssTarget":  round(mss["mss_target"], 2),
            "sweepWick":  round(mss["sweep_wick"], 2),
            "bias":       bias,
            "c0Time":     fmt_ts(lv["c0_open_ts"]),
            "time":       now_ist(),
        }

    print(f"\n{'No new alerts' if alerts_sent == 0 else str(alerts_sent) + ' alert(s) sent'}")

    # ── Update simulator ──────────────────────────────────────
    update_simulator(state, cur_price)

    # ── Save levels for dashboard ─────────────────────────────
    state["currentLevels"] = [
        {
            "type":   lv["type"],
            "price":  round(lv["price"], 2),
            "fresh":  lv["fresh"],
            "hsl":    lv["hsl"],
            "has1d":  any(
                l["type"] == lv["type"]
                and abs(l["price"] - lv["price"]) / lv["price"] * 100 < 1.5
                for l in d1_levels
            ),
            "grade":  grade_level(lv, any(
                l["type"] == lv["type"]
                and abs(l["price"] - lv["price"]) / lv["price"] * 100 < 1.5
                for l in d1_levels
            ), bias, cur_price),
            "dist":   round(abs(cur_price - lv["price"]) / lv["price"] * 100, 2),
        }
        for lv in sorted(h4_levels, key=lambda x: abs(cur_price - x["price"]))
    ]

    state["currentPrice"] = round(cur_price, 2)
    state["currentBias"]  = bias
    state["currentTrend"] = {"w1": tr1w, "d1": tr1d, "h4": tr4h}
    state["scanTime"]     = now_ist()

    save_state(state, sha)
    print("\n=== Done ===")

if __name__ == "__main__":
    main()
