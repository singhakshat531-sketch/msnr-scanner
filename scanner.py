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

def find_levels(candles, max_dist=15.0):
    """
    V-level (swing low):
      - candle[i] is bear AND its LOW is lower than 2 candles on each side
      - candle[i+1] is bull (confirmation)
      - level price = bear candle CLOSE
      - level time  = bull candle open (confirmation candle open)

    A-level (swing high):
      - candle[i] is bull AND its HIGH is higher than 2 candles on each side
      - candle[i+1] is bear (confirmation)
      - level price = bull candle CLOSE
      - level time  = bear candle open (confirmation candle open)

    Only extreme swing points — not every bear/bull flip.
    """
    if len(candles) < 6: return []
    cur    = candles[-1]["c"]
    levels = []
    LB = 2  # how many candles each side must be higher/lower

    for i in range(LB, len(candles) - LB - 1):
        c0 = candles[i]
        c1 = candles[i + 1]

        bear0 = c0["c"] < c0["o"]
        bull0 = c0["c"] > c0["o"]
        bull1 = c1["c"] > c1["o"]
        bear1 = c1["c"] < c1["o"]

        # V-level: bear candle at swing low + bull confirmation
        if bear0 and bull1:
            # swing low = c0 low is lower than LB candles on each side
            is_swing_low = all(
                c0["l"] < candles[i - j]["l"] and c0["l"] < candles[i + 1 + j]["l"]
                for j in range(1, LB + 1)
                if i + 1 + j < len(candles)
            )
            if not is_swing_low: continue
            price = c0["c"]
            t     = "V"
            level_time = c1["t"]

        # A-level: bull candle at swing high + bear confirmation
        elif bull0 and bear1:
            is_swing_high = all(
                c0["h"] > candles[i - j]["h"] and c0["h"] > candles[i + 1 + j]["h"]
                for j in range(1, LB + 1)
                if i + 1 + j < len(candles)
            )
            if not is_swing_high: continue
            price = c0["c"]
            t     = "A"
            level_time = c1["t"]

        else:
            continue

        dist = abs(cur - price) / cur * 100
        if dist > max_dist: continue

        # Fresh = no close beyond level since formed
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
            "ts":    level_time,
        })

    return sorted(levels, key=lambda x: x["dist"])[:10]


def find_mss(h1_window, level):
    """
    LONG (V-level):
      1. Find 1H sweep: wick below 4H level, close above it
      2. External range starts from sweep candle
         - range_low  = sweep candle low (deepest point)
         - range_high = highest high of all candles since sweep
      3. Re-sweep rule: if any candle CLOSES below sweep candle low
         → that becomes the new sweep candle, reset range
      4. MSS = candle BODY closes ABOVE range high → alert

    SHORT (A-level): mirror, flipped.
    """
    bull = level["type"] == "V"
    lp   = level["price"]
    scan = h1_window[-SWING_LOOKBACK:]
    n    = len(scan)

    # Find first valid sweep (searching backwards to get most recent)
    last_touch_idx = None
    for i in range(n - 3, -1, -1):
        c = scan[i]
        if bull and c["l"] < lp and c["c"] > lp:
            last_touch_idx = i; break
        elif not bull and c["h"] > lp and c["c"] < lp:
            last_touch_idx = i; break

    if last_touch_idx is None: return None

    # Now replay forward from that sweep, allowing re-sweeps to reset
    sweep_idx = last_touch_idx
    sweep_c   = scan[sweep_idx]
    rH        = sweep_c["h"]   # range high starts at sweep candle high
    rL        = sweep_c["l"]   # range low = sweep candle low (the deepest point)

    for i in range(sweep_idx + 1, n):
        mc = scan[i]

        if bull:
            # Re-sweep: candle closes BELOW sweep low → reset
            if mc["c"] < rL:
                # Only reset if this candle also swept the 4H level
                if mc["l"] < lp and mc["c"] > lp:
                    sweep_idx = i
                    sweep_c   = mc
                    rH        = mc["h"]
                    rL        = mc["l"]
                else:
                    # Closed below range low but not a valid sweep → invalidate
                    return None

            # Update range high
            rH = max(rH, mc["h"])

            # MSS: body closes ABOVE range high (check before updating rH)
            if mc["c"] > rH and i > sweep_idx + 1:
                ext = scan[sweep_idx:i]
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
                    "rH":            max(c["h"] for c in ext),
                    "rL":            min(c["l"] for c in ext),
                    "mss_ts":        mc["t"],
                    "mss_close":     mc["c"],
                }

        else:
            # Re-sweep: candle closes ABOVE sweep high → reset
            if mc["c"] > rH:
                if mc["h"] > lp and mc["c"] < lp:
                    sweep_idx = i
                    sweep_c   = mc
                    rH        = mc["h"]
                    rL        = mc["l"]
                else:
                    return None

            rL = min(rL, mc["l"])

            # MSS: body closes BELOW range low
            if mc["c"] < rL and i > sweep_idx + 1:
                ext = scan[sweep_idx:i]
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
                    "rL":            min(c["l"] for c in ext),
                    "mss_ts":        mc["t"],
                    "mss_close":     mc["c"],
                }

    return None

# ── MAIN ──────────────────────────────────────────────────────
print("Fetching 30 days of data...")
w1  = fetch("1w", 52);  time.sleep(0.5)
d1  = fetch("1d", 90);  time.sleep(0.5)
h4  = fetch("4h", 300); time.sleep(0.5)
h1  = fetch("1h", 720)  # 30 days of 1H candles
start_str = now_ist(h1[0]["t"])
end_str   = now_ist(h1[-1]["t"])
print(f"Got {len(h1)} 1H candles — from {start_str} to {end_str}\n")

alerts = []
seen   = set()

STEP = 1
for idx in range(100, len(h1), STEP):
    h1_window = h1[:idx+1]
    cur       = h1_window[-1]

    d1_now = [c for c in d1 if c["t"] <= cur["t"]]
    w1_now = [c for c in w1 if c["t"] <= cur["t"]]
    h4_now = [c for c in h4 if c["t"] <= cur["t"]]

    if len(d1_now)<20 or len(h4_now)<10: continue

    tr1w = get_trend(w1_now, 12)
    tr1d = get_trend(d1_now, 20)
    bias = get_bias(tr1w, tr1d)
    if bias == "RANGING": continue

    lvls_4h = find_levels(h4_now, max_dist=15.0)
    lvls_1d = find_levels(d1_now, max_dist=20.0)

    # Debug: print levels found on last candle only
    if idx == len(h1) - 1:
        print(f"\nDEBUG — final candle: BTC=${cur['c']:,.0f}  bias={bias}")
        print(f"4H levels found: {len(lvls_4h)}")
        for lv in lvls_4h:
            print(f"  {lv['type']} ${lv['price']:,.0f}  dist={lv['dist']:.2f}%  fresh={lv['fresh']}")
        print(f"1D levels found: {len(lvls_1d)}")

    for lv in lvls_4h:
        bull = lv["type"] == "V"
        if bull  and bias != "BULLISH": continue
        if not bull and bias != "BEARISH": continue

        dist = abs(cur["c"] - lv["price"]) / lv["price"] * 100
        if dist > ZONE_PCT: continue

        mss = find_mss(h1_window, lv)
        if not mss:
            if idx > len(h1) - 5:  # only last 4 candles
                print(f"  no MSS for {lv['type']} ${lv['price']:,.0f} dist={dist:.2f}%")
            continue

        key = str(int(lv["price"])) + "_" + str(mss["mss_ts"])
        if key in seen: continue
        seen.add(key)

        # 1D confluence — 1D level within 1% of this 4H level?
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


# ── HTML REPORT ───────────────────────────────────────────────

# ── PRINT RESULTS ─────────────────────────────────────────────
print("")
print("=" * 55)
print("  MSNR BACKTEST — Last 30 Days  |  " + str(len(alerts)) + " setups")
print("=" * 55)

for i, a in enumerate(alerts, 1):
    lv   = a["level"]
    mss  = a["mss"]
    bull = mss["bull"]
    wick = mss.get("sweep_low", 0) if bull else mss.get("sweep_high", 0)
    shape = "V-shape" if bull else "A-shape"
    tf    = "1D+4H" if a["has_1d"] else "4H"
    signal = mss.get("signal", "MSS")

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
    print("  Sweep     : " + now_ist(mss["sweep_ts"]))
    print("              wick $" + f"{wick:,.0f}" + "  close $" + f"{mss['sweep_close']:,.0f}")
    print("")
    print("  Ext Range : " + str(mss["range_candles"]) + " candles  |  $" + f"{mss['rL']:,.0f}" + " — $" + f"{mss['rH']:,.0f}")
    print("              " + now_ist(mss["range_open_ts"]) + "  to  " + now_ist(mss["range_close_ts"]))
    print("")
    print("  " + signal + "       : " + now_ist(mss["mss_ts"]))
    print("              close $" + f"{mss['mss_close']:,.0f}")
    print("")
    print("  Bias: " + a["bias"] + "  (1W:" + a["tr1w"] + " 1D:" + a["tr1d"] + ")")

print("")
print("=" * 55)
