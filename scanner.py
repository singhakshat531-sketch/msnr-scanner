#!/usr/bin/env python3
"""
MSNR + MSS Backtest — Last 30 Days
────────────────────────────────────
Replays historical data and logs every setup detected.
For each alert it prints:
  - Date/time in IST
  - 4H level that was swept
  - External range (consecutive candles)
  - MSS candle close price

You can then open TradingView at that exact time and verify.
────────────────────────────────────
"""

import urllib.request, json, time
from datetime import datetime, timezone, timedelta

IST       = timezone(timedelta(hours=5, minutes=30))
TOUCH_PCT = 1.5
ZONE_PCT  = 3.0
SWING_LOOKBACK = 200

def now_ist(ts):
    return datetime.fromtimestamp(ts, IST).strftime('%d %b %Y  %I:%M %p IST')

def fetch(interval, limit):
    endpoint  = "histoday" if interval in ("1d","1w") else "histohour"
    aggregate = {"1w":7,"1d":1,"4h":4,"1h":1}[interval]
    url = (f"https://min-api.cryptocompare.com/data/v2/{endpoint}"
           f"?fsym=BTC&tsym=USD&limit={limit}&aggregate={aggregate}")
    req = urllib.request.Request(url, headers={"User-Agent":"MSNR-Backtest/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read())
    return [
        {"t": k["time"], "o": float(k["open"]), "h": float(k["high"]),
         "l": float(k["low"]), "c": float(k["close"])}
        for k in data["Data"]["Data"]
        if not (k["open"]==0 and k["close"]==0)
    ]

def get_trend(candles, lb=20):
    if len(candles) < lb: return "UNKNOWN"
    c = [x["c"] for x in candles[-lb:]]
    half = len(c)//2
    pct  = (sum(c[half:])/half - sum(c[:half])/half) / (sum(c[:half])/half) * 100
    bull = sum(1 for i in range(2,len(c)) if c[i]>c[i-1]>c[i-2])
    bear = sum(1 for i in range(2,len(c)) if c[i]<c[i-1]<c[i-2])
    if pct>3  and bull>bear:       return "BULLISH"
    if pct<-3 and bear>bull:       return "BEARISH"
    if pct>2  and bull>bear*1.5:   return "BULLISH"
    if pct<-2 and bear>bull*1.5:   return "BEARISH"
    return "RANGING"

def get_bias(tr1w, tr1d):
    if tr1w=="BULLISH" and tr1d in ("BULLISH","RANGING"): return "BULLISH"
    if tr1w=="BEARISH" and tr1d in ("BEARISH","RANGING"): return "BEARISH"
    if tr1d=="BULLISH": return "BULLISH"
    if tr1d=="BEARISH": return "BEARISH"
    return "RANGING"

def find_levels(candles, max_dist=30.0):
    """
    A-level: bull candle (c0) + bear candle (c1) → price = c0 close
    V-level: bear candle (c0) + bull candle (c1) → price = c0 close
    Level time = c1 open (confirmation candle open)
    """
    if len(candles) < 3: return []
    cur    = candles[-1]["c"]
    levels = []
    for i in range(len(candles) - 1):
        c0 = candles[i]
        c1 = candles[i + 1]
        bull0 = c0["c"] > c0["o"]
        bear0 = c0["c"] < c0["o"]
        bull1 = c1["c"] > c1["o"]
        bear1 = c1["c"] < c1["o"]

        if bull0 and bear1:          # A-level
            price = c0["c"]
            t     = "A"
        elif bear0 and bull1:        # V-level
            price = c0["c"]
            t     = "V"
        else:
            continue

        dist = abs(cur - price) / cur * 100
        if dist > max_dist: continue

        # freshness — track wicks only, don't kill levels
        wicks, dead = 0, False
        for fc in candles[i + 2:]:
            if t == "A":
                if fc["c"] > price: dead = True; break
                if fc["h"] > price: wicks += 1
            else:
                if fc["c"] < price: dead = True; break
                if fc["l"] < price: wicks += 1
        if dead: continue

        levels.append({
            "type":    t,
            "price":   price,
            "fresh":   wicks == 0,
            "hsl":     False,
            "dist":    dist,
            "ts":      c1["t"] + 4*3600,
            "c0_open": c0["t"],   # FIX: added to match scanner level dict for alert formatting
        })
    return sorted(levels, key=lambda x: x["dist"])

def find_mss(h1_window, level):
    """
    Same logic as find_1h_mss in scanner:
    - Collect ALL valid sweeps after level formation
    - Any re-sweep (wick through level + close back inside) resets the active sweep
    - MSS target recalculated from prev_sweep to new_sweep on each re-sweep
    """
    bull      = level["type"] == "V"
    lp        = level["price"]
    level_ts  = level.get("ts", 0)
    scan      = h1_window[-SWING_LOOKBACK:]
    n         = len(scan)

    level_idx = None
    for i in range(n):
        if scan[i]["t"] >= level_ts:
            level_idx = i; break
    if level_idx is None: return None

    def calc_target(from_idx, to_idx):
        sweep_c = scan[to_idx]
        target  = None; swing_i = to_idx
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

    # First valid sweep
    sweep_idx = None
    for i in range(level_idx, n - 1):
        c = scan[i]
        if bull and c["l"] < lp and c["c"] > lp:
            sweep_idx = i; break
        elif not bull and c["h"] > lp and c["c"] < lp:
            sweep_idx = i; break
    if sweep_idx is None: return None
    if sweep_idx <= level_idx: return None

    prev_sweep_idx = level_idx  # consistent with scanner: start from level confirmation
    sweep_c        = scan[sweep_idx]
    mss_target, swing_idx = calc_target(prev_sweep_idx, sweep_idx)

    for i in range(sweep_idx + 1, n):
        mc = scan[i]
        if bull:
            # Re-sweep: wick below level + close back above (level-touch, not deeper-wick)
            if mc["l"] < lp and mc["c"] > lp:
                prev_sweep_idx = sweep_idx
                sweep_idx = i; sweep_c = mc
                mss_target, swing_idx = calc_target(prev_sweep_idx, sweep_idx)
                continue
            if mc["c"] > mss_target:
                ext = scan[swing_idx:sweep_idx + 1]
                return {
                    "bull":          True,
                    "signal":        "MSS",
                    "swept":         True,
                    "sweep_ts":      sweep_c["t"],
                    "sweep_low":     sweep_c["l"],
                    "sweep_close":   sweep_c["c"],
                    "range_candles": len(ext),
                    "range_open_ts": ext[0]["t"],
                    "range_close_ts":ext[-1]["t"],
                    "rH":            mss_target,
                    "rL":            min(c["l"] for c in ext),
                    "mss_ts":        mc["t"],
                    "mss_close":     mc["c"],
                }
        else:
            # Re-sweep: wick above level + close back below (level-touch, not higher-wick)
            if mc["h"] > lp and mc["c"] < lp:
                prev_sweep_idx = sweep_idx
                sweep_idx = i; sweep_c = mc
                mss_target, swing_idx = calc_target(prev_sweep_idx, sweep_idx)
                continue
            if mc["c"] < mss_target:
                ext = scan[swing_idx:sweep_idx + 1]
                return {
                    "bull":          False,
                    "signal":        "MSS",
                    "swept":         True,
                    "sweep_ts":      sweep_c["t"],
                    "sweep_high":    sweep_c["h"],
                    "sweep_close":   sweep_c["c"],
                    "range_candles": len(ext),
                    "range_open_ts": ext[0]["t"],
                    "range_close_ts":ext[-1]["t"],
                    "rH":            max(c["h"] for c in ext),
                    "rL":            mss_target,
                    "mss_ts":        mc["t"],
                    "mss_close":     mc["c"],
                }
    return None



# ── MAIN ──────────────────────────────────────────────────────
print("Fetching 30 days of data...")
w1  = fetch("1w", 52);  time.sleep(0.5)
d1  = fetch("1d", 90);  time.sleep(0.5)
h4  = fetch("4h", 200); time.sleep(0.5)
h1  = fetch("1h", 480)  # 20 days of 1H candles
start_str = now_ist(h1[0]["t"])
end_str   = now_ist(h1[-1]["t"])
print(f"Got {len(h1)} 1H candles — from {start_str} to {end_str}\n")

# Detailed debug for 21 Mar A-level
print("=== TESTING 21 MAR LEVEL ===")
lvls_all = find_levels(h4, max_dist=30.0)
target_lv = next((lv for lv in lvls_all if abs(lv["price"]-84000)<3000 and lv["type"]=="A"), None)
if not target_lv:
    # Fallback: find any A-level formed around 21 Mar
    target_lv = next((lv for lv in sorted(lvls_all, key=lambda x: abs(x["ts"]-1742479800))
                      if lv["type"]=="A"), None)
if target_lv:
    lp = target_lv["price"]
    level_ts = target_lv["ts"]
    print(f"Level: A ${lp:,.0f} confirmed={now_ist(level_ts)}")
    level_idx = next((i for i,c in enumerate(h1) if c["t"] >= level_ts), 0)
    print(f"level_idx={level_idx} = {now_ist(h1[level_idx]['t'])}")
    print("1H candles after level (SHORT setup — look for H>level, C<level):")
    for c in h1[level_idx:level_idx+30]:
        is_sweep = c["h"] > lp and c["c"] < lp
        print(f"  {now_ist(c['t'])} O={c['o']:,.0f} H={c['h']:,.0f} L={c['l']:,.0f} C={c['c']:,.0f} {'<< SWEEP' if is_sweep else ''}")
    mss_test = find_mss(h1, target_lv)
    print(f"MSS result: {'YES — ' + now_ist(mss_test['mss_ts']) if mss_test else 'NOT FOUND'}")
else:
    print("No A-level found near 21 Mar")
print("============================\n")

# ── ISOLATED TEST: print all 4H candles around 21 Mar ──────────
print("=== 4H CANDLES AROUND 21 MAR ===")
for c in h4:
    if 1742479800 <= c["t"] <= 1742652600:  # 21 Mar 00:00 IST to 23 Mar 00:00 IST (UTC seconds)
        bull = c["c"] > c["o"]
        print(f"  {now_ist(c['t'])}  O={c['o']:,.0f} H={c['h']:,.0f} L={c['l']:,.0f} C={c['c']:,.0f}  {'BULL' if bull else 'BEAR'}")
print("=================================\n")

alerts = []
seen   = set()

STEP = 1
crash_logged = False
for idx in range(100, len(h1), STEP):
    # Quick check outside try - any level formed after 21 Mar?
    h4_quick = [c for c in h4 if c["t"] <= h1[idx]["t"]]
    lvls_quick = find_levels(h4_quick, max_dist=30.0)
    for lv in lvls_quick:
        if lv["ts"] >= 1742479800:  # 21 Mar 2025 00:00 IST (UTC 18:30 20 Mar)
            print(f"FOUND {lv['type']} ${lv['price']:,.0f} formed={now_ist(lv['ts'])} at h1idx={idx}")
            break
    try:
        h1_window = h1[:idx+1]
        cur       = h1_window[-1]

        d1_now = [c for c in d1 if c["t"] <= cur["t"]]
        w1_now = [c for c in w1 if c["t"] <= cur["t"]]
        h4_now = [c for c in h4 if c["t"] <= cur["t"]]

        if len(d1_now)<20 or len(h4_now)<10: continue

        tr1w = get_trend(w1_now, 12)
        tr1d = get_trend(d1_now, 20)
        bias = get_bias(tr1w, tr1d)

        if idx == len(h1) - 1:
            print(f"LAST CANDLE: {now_ist(cur['t'])} bias={bias}")
            lvls_debug = find_levels(h4_now, max_dist=30.0)
            print(f"4H levels: {len(lvls_debug)}")
            for lv in sorted(lvls_debug, key=lambda x: x['ts'], reverse=True)[:10]:
                mss = find_mss(h1_window, lv)
                print(f"  {lv['type']} ${lv['price']:,.0f} formed={now_ist(lv['ts'])} mss={'YES' if mss else 'no'}")

        lvls_4h = find_levels(h4_now, max_dist=30.0)
        lvls_1d = find_levels(d1_now, max_dist=30.0)

        # Show levels confirmed after 20 Mar 2025 00:00 UTC
        for lv in lvls_4h:
            if lv["ts"] > 1742428800:  # 20 Mar 2025 00:00 UTC
                mss_r = find_mss(h1_window, lv)
                print(f"NEW [{now_ist(cur['t'])}] {lv['type']} ${lv['price']:,.0f} formed={now_ist(lv['ts'])} mss={'YES' if mss_r else 'no'}")

        for lv in lvls_4h:
            mss = find_mss(h1_window, lv)
            if not mss: continue

            key = str(int(lv["price"])) + "_" + str(mss.get("mss_ts", mss.get("mss_open","")))
            if key in seen: continue
            seen.add(key)

            has_1d = any(
                l["type"] == lv["type"] and abs(l["price"] - lv["price"]) / lv["price"] * 100 < 1.0
                for l in lvls_1d
            )

            if lv["fresh"] and has_1d:  grade = "A+"
            elif lv["fresh"] or has_1d: grade = "A"
            else:                        grade = "B"

            alerts.append({
                "bias":   bias,
                "tr1w":   tr1w,
                "tr1d":   tr1d,
                "level":  lv,
                "has_1d": has_1d,
                "grade":  grade,
                "mss":    mss,
                "cur":    cur["c"],
            })
    except Exception as e:
        if not crash_logged:
            print(f"CRASH at idx={idx} candle={now_ist(h1[idx]['t'])}: {e}")
            import traceback; traceback.print_exc()
            crash_logged = True


# ── POST LOOP DEBUG ───────────────────────────────────────────
with open("debug_levels.txt", "w") as dbf:
    dbf.write("=== DEBUG: All 4H levels at end of data ===\n")
    lvls_final = find_levels(h4, max_dist=30.0)
    dbf.write(f"Total: {len(lvls_final)}\n")
    for lv in sorted(lvls_final, key=lambda x: x['ts']):
        dbf.write(f"  {lv['type']} ${lv['price']:,.0f}  formed={now_ist(lv['ts'])}\n")
print("Debug written to debug_levels.txt")

# ── PRINT RESULTS ─────────────────────────────────────────────
print("")
print("=" * 55)
print("  MSNR BACKTEST — Last 30 Days  |  " + str(len(alerts)) + " setups")
print("=" * 55)

for i, a in enumerate(alerts, 1):
    lv   = a["level"]
    mss  = a["mss"]
    bull = mss.get("bull", True)
    wick = mss.get("sweep_low", mss.get("sweep_wick", 0)) if bull else mss.get("sweep_high", mss.get("sweep_wick", 0))
    shape = "V-shape" if bull else "A-shape"
    tf    = "1D+4H" if a["has_1d"] else "4H"
    signal = mss.get("signal", "MSS")
    sweep_ts   = mss.get("sweep_ts", 0)
    mss_ts     = mss.get("mss_ts", 0)
    range_open = mss.get("range_open_ts", 0)
    range_close= mss.get("range_close_ts", 0)
    rL = mss.get("rL", 0)
    rH = mss.get("rH", 0)

    if signal == "MSS":
        header = "LONG  — SWEEP+MSS" if bull else "SHORT — SWEEP+MSS"
    else:
        header = "LONG  — BREAK    " if bull else "SHORT — BREAK    "

    print("")
    print("-" * 55)
    print("  #" + str(i) + "  " + header + "  |  Grade: " + a["grade"])
    print("")
    print("  4H Level  : $" + f"{lv['price']:,.0f}" + "  (" + shape + " · " + ("Fresh" if lv["fresh"] else "Used") + " · " + tf + ")")
    print("  Formed    : " + now_ist(lv["ts"]))
    print("")
    print("  Sweep     : " + now_ist(sweep_ts))
    print("              wick $" + f"{wick:,.0f}" + "  close $" + f"{mss.get('sweep_close',0):,.0f}")
    print("")
    print("  Ext Range : " + str(mss.get("range_candles",0)) + " candles  |  $" + f"{rL:,.0f}" + " — $" + f"{rH:,.0f}")
    print("              " + now_ist(range_open) + "  to  " + now_ist(range_close))
    print("")
    print("  " + signal + "       : " + now_ist(mss_ts))
    print("              close $" + f"{mss.get('mss_close',0):,.0f}")
    print("")
    print("  Bias: " + a["bias"] + "  (1W:" + a["tr1w"] + " 1D:" + a["tr1d"] + ")")

print("")
print("=" * 55)
