#!/usr/bin/env python3
"""
MSNR + CRT Scanner v7 — GitHub Actions
Elite Observer

STRATEGY (as designed):
────────────────────────────────────────────────
STEP 1 — 1D + 4H MSNR LEVELS
  Find fresh V-levels (support) and A-levels (resistance) on 4H.
  Optionally confirm with 1D level in same direction.

STEP 2 — 1H CRT AT THE LEVEL  (Accumulation → Manipulation → Distribution)
  A) ACCUMULATION: 2–8 consecutive 1H candles form a tight range NEAR the 4H level.
     Range must overlap or touch the 4H level.
  B) MANIPULATION / SWEEP: A 1H candle's WICK pierces BEYOND the 4H level
     (takes liquidity/stops) but the candle CLOSES BACK INSIDE the range.
     → This is the classic CRT sweep candle.
  C) DISTRIBUTION / BREAK: The NEXT 1H candle after the sweep closes
     BEYOND the OPPOSITE side of the accumulated range
     (range break in the expected direction).

ALERT fires when ALL THREE phases are confirmed and recent (within last 6 candles).

TELEGRAM message clearly shows: Level → Sweep → Range → Break → Entry
────────────────────────────────────────────────
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
SPAM_HOURS       = 4
SIM_START        = 1000.0
RISK_PCT         = {"aplus": 3.0, "a": 2.0, "b": 1.0}

# CRT Detection Parameters
CRT_ACCUM_MIN    = 2      # minimum candles for accumulation range
CRT_ACCUM_MAX    = 10     # maximum candles to look back for range
CRT_RANGE_MAX_PCT= 1.5    # accumulation range must be tight (max % wide)
CRT_LEVEL_TOUCH  = 0.8    # range must be within 0.8% of 4H level
CRT_SWEEP_MIN_PCT= 0.05   # wick must pierce at least 0.05% beyond level
CRT_BREAK_MIN    = 0.3    # body ratio for distribution candle (> 30% body)
CRT_RECENT_BARS  = 8      # distribution must be in last 8 bars

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
        "User-Agent": "MSNR-Scanner/7.0"
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
            "User-Agent": "MSNR-Scanner/7.0"
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
            "stats": {"totalTrades": 0, "wins": 0, "losses": 0, "be": 0, "netR": 0.0, "bestR": 0.0},
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
    aggregate = {"1d": 1, "4h": 4, "1h": 1}.get(interval, 1)
    url = (f"https://min-api.cryptocompare.com/data/v2/{endpoint}"
           f"?fsym={symbol}&tsym=USD&limit={limit}&aggregate={aggregate}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MSNR-Scanner/7.0"})
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
    bull  = sum(1 for i in range(2, len(c)) if c[i] > c[i-1] > c[i-2])
    bear  = sum(1 for i in range(2, len(c)) if c[i] < c[i-1] < c[i-2])
    if pct > 3  and bull > bear:       return "BULLISH"
    if pct < -3 and bear > bull:       return "BEARISH"
    if pct > 2  and bull > bear * 1.5: return "BULLISH"
    if pct < -2 and bear > bull * 1.5: return "BEARISH"
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
    """
    Find V-levels (support) and A-levels (resistance) using close prices.
    V-level: close is lower than lb candles on each side (valley)
    A-level: close is higher than lb candles on each side (peak)
    Freshness: no candle has closed beyond the level since it formed.
    HSL: level was tested 2+ times before (historical significance).
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
            "type": t, "price": price,
            "freshness": "FRESH" if wicks == 0 else "UNFRESH",
            "hsl": hsl, "dist": dist, "wicks": wicks
        })

    return sorted(levels, key=lambda x: x["dist"])[:8]

# ── SPAM CONTROL ──────────────────────────────────────────────────────────────
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
    if lv["dist"] < 0.5:           score += 1
    grade = "aplus" if score >= 5 else "a" if score >= 3 else "b"
    return grade, score

# ── CRT DETECTION ─────────────────────────────────────────────────────────────
def find_crt_setup(c1h, level, bias):
    """
    Detect a full CRT (Candle Range Theory) setup at a 4H MSNR level.

    Three phases on 1H chart:
    ─────────────────────────────────────────────────────────
    PHASE A — ACCUMULATION
      Find a group of 2–10 consecutive 1H candles that form a
      tight range (max CRT_RANGE_MAX_PCT% wide) that TOUCHES
      or OVERLAPS the 4H level.

    PHASE B — MANIPULATION / SWEEP
      After the range, a candle's WICK pierces BEYOND the 4H level
      (below level for V/bullish, above for A/bearish) by at least
      CRT_SWEEP_MIN_PCT%, but the candle CLOSES BACK INSIDE the
      accumulation range. This is the classic liquidity grab.

    PHASE C — DISTRIBUTION / RANGE BREAK
      The candle immediately after the sweep closes BEYOND the
      OPPOSITE side of the accumulation range (away from the level),
      confirming the CRT and signalling entry.
      It must have a meaningful body (> CRT_BREAK_MIN body ratio).

    Returns dict with all trade parameters if setup found, else None.
    """
    if len(c1h) < CRT_ACCUM_MIN + 2: return None

    bull = level["type"] == "V"   # bullish setup at support
    lp   = level["price"]
    scan = c1h[-50:]              # look at last 50 1H candles
    n    = len(scan)

    # We need at least: accum_min candles + 1 sweep + 1 break
    for dist_idx in range(CRT_ACCUM_MIN + 1, n):
        dist_candle = scan[dist_idx]

        # ── PHASE C check first (most recent, filters fast) ──────────
        if dist_idx < n - CRT_RECENT_BARS:
            continue  # distribution must be recent

        body   = abs(dist_candle["c"] - dist_candle["o"])
        cr     = dist_candle["h"] - dist_candle["l"]
        br     = body / cr if cr > 0 else 0

        if br < CRT_BREAK_MIN:
            continue  # not a meaningful breakout candle

        # Distribution candle must close AWAY from the level
        if bull and dist_candle["c"] < dist_candle["o"]:
            continue  # need bullish close for bullish setup
        if not bull and dist_candle["c"] > dist_candle["o"]:
            continue  # need bearish close for bearish setup

        # ── PHASE B — find the sweep candle (one before distribution) ─
        sweep_idx = dist_idx - 1
        sweep     = scan[sweep_idx]

        if bull:
            # Wick below the 4H level (stop hunt below support)
            sweep_pierced = sweep["l"] < lp * (1 - CRT_SWEEP_MIN_PCT / 100)
            # But closed back above the level (inside range)
            sweep_closed_inside = sweep["c"] > lp
        else:
            # Wick above the 4H level (stop hunt above resistance)
            sweep_pierced = sweep["h"] > lp * (1 + CRT_SWEEP_MIN_PCT / 100)
            # But closed back below the level (inside range)
            sweep_closed_inside = sweep["c"] < lp

        if not sweep_pierced or not sweep_closed_inside:
            continue

        # ── PHASE A — find accumulation range before sweep ────────────
        # Look back up to CRT_ACCUM_MAX candles before sweep
        best_accum = None
        for start_idx in range(max(0, sweep_idx - CRT_ACCUM_MAX), sweep_idx - CRT_ACCUM_MIN + 1):
            seg = scan[start_idx:sweep_idx]  # candles forming range
            if len(seg) < CRT_ACCUM_MIN:
                continue

            rH = max(c["h"] for c in seg)
            rL = min(c["l"] for c in seg)
            range_pct = (rH - rL) / rL * 100

            if range_pct > CRT_RANGE_MAX_PCT:
                continue  # range too wide — not accumulation

            # Range must touch / overlap the 4H level
            if bull:
                # V-level (support): range low should be near the level
                dist_to_level = abs(rL - lp) / lp * 100
            else:
                # A-level (resistance): range high should be near the level
                dist_to_level = abs(rH - lp) / lp * 100

            if dist_to_level > CRT_LEVEL_TOUCH:
                continue  # range too far from level

            best_accum = {"seg": seg, "rH": rH, "rL": rL,
                          "start": start_idx, "candles": len(seg)}
            break  # take the widest valid range

        if best_accum is None:
            continue

        rH = best_accum["rH"]
        rL = best_accum["rL"]

        # Confirm distribution broke OUT of the range
        if bull and dist_candle["c"] <= rH:
            continue  # didn't break above range top
        if not bull and dist_candle["c"] >= rL:
            continue  # didn't break below range bottom

        # ── BUILD TRADE PARAMETERS ────────────────────────────────────
        # Entry: at the 4H level (limit order on retest)
        entry = lp

        if bull:
            # SL below the sweep wick low with small buffer
            sl    = sweep["l"] * 0.9992
            risk  = abs(entry - sl)
            if risk <= 0: continue
            tp1   = entry + risk * 3.0
            tp2   = entry + risk * 7.0
        else:
            # SL above the sweep wick high with small buffer
            sl    = sweep["h"] * 1.0008
            risk  = abs(sl - entry)
            if risk <= 0: continue
            tp1   = entry - risk * 3.0
            tp2   = entry - risk * 7.0

        rr1 = abs(tp1 - entry) / risk
        rr2 = abs(tp2 - entry) / risk

        if rr1 < 2.5:
            continue  # minimum RR not met

        return {
            # Phase details
            "phase_a_candles":  best_accum["candles"],
            "phase_a_rH":       round(rH, 2),
            "phase_a_rL":       round(rL, 2),
            "phase_a_range_pct": round((rH - rL) / rL * 100, 3),
            "sweep_low":        round(sweep["l"], 2),
            "sweep_high":       round(sweep["h"], 2),
            "sweep_close":      round(sweep["c"], 2),
            "dist_close":       round(dist_candle["c"], 2),
            "dist_br":          round(br, 2),
            # Trade levels
            "entry":  round(entry, 2),
            "sl":     round(sl, 2),
            "tp1":    round(tp1, 2),
            "tp2":    round(tp2, 2),
            "risk":   round(risk, 2),
            "rr1":    round(rr1, 1),
            "rr2":    round(rr2, 1),
            # Meta
            "bull":   bull,
        }

    return None


# ── TELEGRAM ──────────────────────────────────────────────────────────────────
def send_telegram(msg):
    if not TELEGRAM_TOKEN:
        print("No token — message:"); print(msg); return
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text":    msg,
        "parse_mode": "HTML"
    }).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=10) as r:
            res = json.loads(r.read())
        print("Telegram sent ✓" if res.get("ok") else f"Telegram error: {res}")
    except Exception as e:
        print(f"Telegram error: {e}")


def format_crt_alert(lv, crt, bias, grade, tr1d, tr4h, lv_1d_target, cur_price):
    """
    Format the Telegram alert clearly showing all 3 CRT phases.
    """
    bull  = crt["bull"]
    gradelabel = "A+" if grade == "aplus" else grade.upper()
    pf    = lambda p: f"${p:,.0f}"
    tp2   = lv_1d_target["price"] if lv_1d_target else crt["tp2"]
    arrow = "🟢 LONG" if bull else "🔴 SHORT"

    lines = [
        f"⚡ MSNR + CRT SETUP",
        f"",
        f"{arrow}  |  Grade: {gradelabel}  |  BTCUSDT",
        f"━━━━━━━━━━━━━━━━━━━━━━━━",
        f"",
        f"🏛  4H MSNR LEVEL",
        f"   {lv['type']}-Level:  {pf(lv['price'])}  ({lv['freshness']})",
        f"   HSL: {'YES ✓' if lv['hsl'] else 'NO'}  |  Dist: {lv['dist']:.2f}%",
        f"   Bias: {bias}  (1D:{tr1d} · 4H:{tr4h})",
        f"",
        f"📦  PHASE A — ACCUMULATION",
        f"   {crt['phase_a_candles']} × 1H candles formed range",
        f"   Range: {pf(crt['phase_a_rL'])} – {pf(crt['phase_a_rH'])}",
        f"   Width: {crt['phase_a_range_pct']:.2f}%",
        f"",
        f"🪤  PHASE B — SWEEP (Manipulation)",
    ]

    if bull:
        lines += [
            f"   Wick swept BELOW level → {pf(crt['sweep_low'])}",
            f"   Closed back ABOVE → {pf(crt['sweep_close'])}",
            f"   Stops taken, smart money accumulated ✓",
        ]
    else:
        lines += [
            f"   Wick swept ABOVE level → {pf(crt['sweep_high'])}",
            f"   Closed back BELOW → {pf(crt['sweep_close'])}",
            f"   Stops taken, smart money distributed ✓",
        ]

    lines += [
        f"",
        f"🚀  PHASE C — DISTRIBUTION (Range Break)",
        f"   1H close: {pf(crt['dist_close'])}",
        f"   Body ratio: {crt['dist_br']:.0%}  (strong momentum)",
        f"",
        f"━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📐  TRADE LEVELS",
        f"   Entry:  {pf(crt['entry'])}  (limit at 4H level)",
        f"   SL:     {pf(crt['sl'])}",
        f"   TP1:    {pf(crt['tp1'])}  (+{crt['rr1']}R)",
        f"   TP2:    {pf(tp2)}",
        f"",
        f"📌  Current price: {pf(cur_price)}",
        f"",
        f"→ Set limit order at {pf(crt['entry'])}",
        f"→ SL below sweep wick",
        f"→ Scale out at TP1, ride to TP2",
        f"",
        f"{datetime.utcnow().strftime('%d %b %Y  %H:%M UTC')}",
        f"singhakshat531-sketch.github.io/msnr-scanner",
    ]

    return "\n".join(lines)


# ── SIMULATOR UPDATE ──────────────────────────────────────────────────────────
def update_simulator(state, cur_price, new_setups=None):
    if new_setups:
        for sd in new_setups:
            # avoid duplicate active trades
            if any(t["id"] == sd["id"] for t in state["activeTrades"]):
                continue
            riskPct = RISK_PCT.get(sd["grade"], 1.0)
            riskAmt = state["balance"] * (riskPct / 100)
            state["activeTrades"].append({
                **sd,
                "phase":        "entry",
                "entryBalance": state["balance"],
                "riskAmt":      round(riskAmt, 2),
                "riskPct":      riskPct,
                "enteredAt":    datetime.utcnow().strftime("%d %b %H:%M UTC"),
            })

    still_active = []
    for t in state["activeTrades"]:
        bull   = t.get("bull", t["direction"] == "LONG")
        entry  = t["rawEntry"]
        sl     = t["rawSL"]
        tp1    = t["rawTP1"]
        tp2    = t["rawTP2"]
        risk   = t.get("rawRisk", abs(entry - sl))
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
                state["history"].insert(0, {**t, "result": "loss",
                    "pnl": round(-riskAmt, 2), "pnlR": -1.0,
                    "closePrice": round(cur_price, 2),
                    "closedAt": datetime.utcnow().isoformat(), "note": "SL hit"})
                state["equityCurve"].append({"t": datetime.utcnow().isoformat(),
                    "v": round(state["balance"], 2)})
                continue
            if (bull and cur_price >= tp1) or (not bull and cur_price <= tp1):
                profit = posSize * abs(tp1 - entry) * 0.5
                state["balance"] += profit
                t["phase"]  = "tp1"
                t["rawSL"]  = entry
                state["stats"]["netR"] = round(state["stats"]["netR"] + 1.5, 2)
                state["history"].insert(0, {**t, "result": "tp1",
                    "pnl": round(profit, 2), "pnlR": 1.5,
                    "closePrice": round(cur_price, 2),
                    "closedAt": datetime.utcnow().isoformat(),
                    "note": "TP1 — 50% closed, SL to BE"})
                state["equityCurve"].append({"t": datetime.utcnow().isoformat(),
                    "v": round(state["balance"], 2)})
                still_active.append(t); continue

        elif phase == "tp1":
            if (bull and cur_price <= entry) or (not bull and cur_price >= entry):
                state["stats"]["be"]          += 1
                state["stats"]["totalTrades"] += 1
                state["history"].insert(0, {**t, "result": "be",
                    "pnl": 0.0, "pnlR": 0.0,
                    "closePrice": round(cur_price, 2),
                    "closedAt": datetime.utcnow().isoformat(), "note": "BE stop"})
                state["equityCurve"].append({"t": datetime.utcnow().isoformat(),
                    "v": round(state["balance"], 2)})
                continue
            if (bull and cur_price >= tp2) or (not bull and cur_price <= tp2):
                profit = posSize * abs(tp2 - entry) * 0.5
                netR   = round(1.5 + abs(tp2 - entry) / risk * 0.5, 1)
                state["balance"] += profit
                state["stats"]["wins"]        += 1
                state["stats"]["totalTrades"] += 1
                state["stats"]["netR"]         = round(state["stats"]["netR"] + netR, 2)
                state["stats"]["bestR"]        = max(state["stats"]["bestR"], netR)
                state["history"].insert(0, {**t, "result": "win",
                    "pnl": round(profit, 2), "pnlR": netR,
                    "closePrice": round(cur_price, 2),
                    "closedAt": datetime.utcnow().isoformat(),
                    "note": f"TP2 hit +{netR}R"})
                state["equityCurve"].append({"t": datetime.utcnow().isoformat(),
                    "v": round(state["balance"], 2)})
                continue

        still_active.append(t)

    state["activeTrades"] = still_active
    state["history"]      = state["history"][:100]
    state["equityCurve"]  = state["equityCurve"][-200:]


# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    print(f"=== MSNR+CRT Scanner v7  {datetime.utcnow().isoformat()} ===")

    print("\nLoading state...")
    state, sha = load_state()
    print(f"  Balance: ${state['balance']:.2f} | "
          f"Active: {len(state['activeTrades'])} | "
          f"History: {len(state['history'])}")

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

    # ── DETECT MSNR LEVELS ────────────────────────────────────────────
    lvl_4h = find_msnr_levels(h4, lb=2, max_dist=15.0)
    lvl_1d = find_msnr_levels(d1, lb=2, max_dist=20.0)

    print(f"\n4H MSNR Levels: {len(lvl_4h)}")
    for l in lvl_4h:
        print(f"  {l['type']}-level ${l['price']:,.0f} | "
              f"{l['freshness']} | HSL:{l['hsl']} | dist:{l['dist']:.2f}%")

    # ── SCAN FOR CRT SETUPS AT EACH LEVEL ─────────────────────────────
    alerts_sent = 0

    if bias != "RANGING" and h1:
        for lv in lvl_4h:
            bull = lv["type"] == "V"
            if bull  and bias != "BULLISH": continue
            if not bull and bias != "BEARISH": continue

            key = f"BTCUSDT_{lv['type']}_{int(lv['price'])}"
            if already_alerted(state, key):
                print(f"  {lv['type']}-level ${lv['price']:,.0f} — alerted recently, skip")
                continue

            # Find nearest 1D level in same direction for TP2
            lv_1d_target = next((
                l for l in sorted(lvl_1d, key=lambda x: abs(x["price"] - lv["price"]))
                if l["type"] == lv["type"]
                and ((bull and l["price"] > lv["price"])
                     or (not bull and l["price"] < lv["price"]))
            ), None)

            grade, score = grade_level(lv, lv_1d_target)

            print(f"\n  Checking {lv['type']}-level ${lv['price']:,.0f} "
                  f"[{grade.upper()} score={score}] for CRT...")

            crt = find_crt_setup(h1, lv, bias)

            if crt:
                print(f"  ✓ CRT CONFIRMED!")
                print(f"    Phase A: {crt['phase_a_candles']}c range "
                      f"${crt['phase_a_rL']:,.0f}–${crt['phase_a_rH']:,.0f} "
                      f"({crt['phase_a_range_pct']:.2f}% wide)")
                if bull:
                    print(f"    Phase B: Sweep to ${crt['sweep_low']:,.0f}, "
                          f"closed ${crt['sweep_close']:,.0f}")
                else:
                    print(f"    Phase B: Sweep to ${crt['sweep_high']:,.0f}, "
                          f"closed ${crt['sweep_close']:,.0f}")
                print(f"    Phase C: Break close ${crt['dist_close']:,.0f} "
                      f"(body {crt['dist_br']:.0%})")
                print(f"    Entry ${crt['entry']:,.0f} | SL ${crt['sl']:,.0f} | "
                      f"TP1 ${crt['tp1']:,.0f} ({crt['rr1']}R)")

                msg = format_crt_alert(lv, crt, bias, grade, tr1d, tr4h, lv_1d_target, cur_price)
                send_telegram(msg)
                mark_alerted(state, key)
                alerts_sent += 1

                # Add to simulator
                setup_data = {
                    "id":         key,
                    "pair":       "BTCUSDT",
                    "direction":  "LONG" if bull else "SHORT",
                    "grade":      grade,
                    "gradeLabel": "A+" if grade == "aplus" else grade.upper(),
                    "bull":       bull,
                    "setupType":  "MSNR_CRT",
                    "rawEntry":   crt["entry"],
                    "rawSL":      crt["sl"],
                    "rawTP1":     crt["tp1"],
                    "rawTP2":     crt["tp2"],
                    "rawRisk":    crt["risk"],
                    "rr1":        crt["rr1"],
                    "rr2":        crt["rr2"],
                    "detectedAt": datetime.utcnow().isoformat(),
                }
                update_simulator(state, cur_price, [setup_data])
            else:
                print(f"  ✗ No CRT setup yet at this level")

    if alerts_sent == 0:
        print("\nNo CRT setups confirmed this run")

    print("\nUpdating simulator open trades...")
    update_simulator(state, cur_price)
    print(f"  Balance: ${state['balance']:.2f}")

    print("\nSaving to GitHub...")
    save_state(state, sha)
    print("=== Scan complete ===")


if __name__ == "__main__":
    main()
