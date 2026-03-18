#!/usr/bin/env python3
"""
MSNR + CRT Scanner v5 — GitHub Actions
Elite Observer — High RR Setup Finder

New in v5:
- Persistent paper trading data stored in GitHub repo (trades.json)
- Data survives forever, accessible from any device
- Auto-updates open trades (TP1/TP2/SL tracking)
- Full simulation with real strategy exits
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
MIN_RR           = 3.0
SPAM_HOURS       = 6
SIM_START        = 1000.0
RISK_PCT         = {"aplus": 3.0, "a": 2.0, "b": 1.0}

# ── GITHUB FILE READ/WRITE ────────────────────────────────────────────────────
def github_read(filename):
    """Read a file from GitHub repo. Returns (content_dict, sha) or (None, None)."""
    if not GITHUB_PAT:
        print(f"  No GITHUB_PAT — reading {filename} locally if exists")
        try:
            with open(filename) as f:
                return json.load(f), None
        except:
            return None, None

    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"token {GITHUB_PAT}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "MSNR-Scanner/5.0"
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
    """Write a file to GitHub repo."""
    if not GITHUB_PAT:
        print(f"  No GITHUB_PAT — saving {filename} locally")
        with open(filename, "w") as f:
            json.dump(content, f, indent=2)
        return True

    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
    body = {
        "message": f"Update {filename} — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
        "content": base64.b64encode(json.dumps(content, indent=2).encode()).decode(),
        "branch": "main"
    }
    if sha:
        body["sha"] = sha

    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"token {GITHUB_PAT}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json",
            "User-Agent": "MSNR-Scanner/5.0"
        },
        method="PUT"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            result = json.loads(r.read())
        print(f"  GitHub write OK: {filename}")
        return True
    except Exception as e:
        print(f"  GitHub write error: {e}")
        return False

def load_state():
    """Load persistent simulation state from GitHub."""
    data, sha = github_read(DATA_FILE)
    if data is None:
        print("  No existing trades.json — creating fresh state")
        data = {
            "balance": SIM_START,
            "activeTrades": [],
            "history": [],
            "equityCurve": [{"t": datetime.utcnow().isoformat(), "v": SIM_START}],
            "alerts": {},
            "stats": {
                "totalTrades": 0,
                "wins": 0,
                "losses": 0,
                "be": 0,
                "netR": 0.0,
                "bestR": 0.0,
            },
            "lastUpdated": datetime.utcnow().isoformat()
        }
    return data, sha

def save_state(data, sha):
    """Save simulation state to GitHub."""
    data["lastUpdated"] = datetime.utcnow().isoformat()
    return github_write(DATA_FILE, data, sha)

# ── FETCH ─────────────────────────────────────────────────────────────────────
def fetch_candles(symbol, interval, limit=120):
    endpoint  = "histoday" if interval == "1d" else "histohour"
    aggregate = {"1d": 1, "4h": 4, "1h": 1}.get(interval, 1)
    url = (f"https://min-api.cryptocompare.com/data/v2/{endpoint}"
           f"?fsym={symbol}&tsym=USD&limit={limit}&aggregate={aggregate}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MSNR-Scanner/5.0"})
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
def get_trend(candles):
    if len(candles) < 10: return "UNKNOWN"
    c = [x["c"] for x in candles[-20:]]
    bull = sum(1 for i in range(2, len(c)) if c[i] > c[i-1] > c[i-2])
    bear = sum(1 for i in range(2, len(c)) if c[i] < c[i-1] < c[i-2])
    if bull > bear + 2: return "BULLISH"
    if bear > bull + 2: return "BEARISH"
    return "RANGING"

# ── MSNR LEVELS ───────────────────────────────────────────────────────────────
def find_msnr_levels(candles, lb=3, max_dist=12.0):
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
        levels.append({"type": t, "price": price, "freshness": "FRESH" if wicks == 0 else "UNFRESH",
                        "hsl": hsl, "dist": dist, "wicks": wicks})
    return sorted(levels, key=lambda x: x["dist"])[:8]

# ── CRT DETECTION ─────────────────────────────────────────────────────────────
def find_crt(c1h, level, trend):
    if len(c1h) < 15 or trend == "RANGING": return None
    bull = level["type"] == "V"
    if bull  and trend != "BULLISH": return None
    if not bull and trend != "BEARISH": return None
    for i in range(max(5, len(c1h) - 48), len(c1h) - 1):
        rs = i - 1
        while rs > 1:
            a, b = c1h[rs], c1h[rs-1]
            if bull     and a["c"] >= b["c"]: break
            if not bull and a["c"] <= b["c"]: break
            rs -= 1
        seg = c1h[rs:i+1]
        rH  = max(x["h"] for x in seg)
        rL  = min(x["l"] for x in seg)
        if (rH - rL) / c1h[-1]["c"] < 0.002: continue
        sw = c1h[i]; cf = c1h[i+1]
        crt2 = (sw["l"] <= level["price"] and sw["c"] > level["price"] and sw["c"] > rL) if bull else \
               (sw["h"] >= level["price"] and sw["c"] < level["price"] and sw["c"] < rH)
        if not crt2: continue
        body = abs(cf["c"] - cf["o"]); cr = cf["h"] - cf["l"]
        br   = body / cr if cr > 0 else 0
        crt3 = (cf["c"] > rH and br > 0.5) if bull else (cf["c"] < rL and br > 0.5)
        if not crt3: continue
        return {"sw": sw, "cf": cf, "br": br}
    return None

# ── BUILD SETUP ───────────────────────────────────────────────────────────────
def build_setup(lv, crt, trend, tf, conf4h, pair):
    bull  = lv["type"] == "V"
    entry = lv["price"]
    sl    = lv["price"] * (0.9992 if bull else 1.0008)
    # Use actual sweep candle for SL
    sl    = crt["sw"]["l"] * 0.9992 if bull else crt["sw"]["h"] * 1.0008
    risk  = abs(entry - sl)
    tp1   = entry + risk * 3  if bull else entry - risk * 3
    tp2   = entry + risk * 7  if bull else entry - risk * 7
    tp3   = entry + risk * 12 if bull else entry - risk * 12
    rr1   = abs(tp1 - entry) / risk
    if rr1 < MIN_RR: return None
    score = 0
    if lv["freshness"] == "FRESH": score += 2
    if lv["hsl"]:                  score += 1
    if conf4h:                     score += 2
    if crt["br"] > 0.65:           score += 1
    if lv["dist"] < 3:             score += 1
    grade = "aplus" if (tf == "1D" and conf4h) else \
            "a"     if (tf == "1D" or (tf == "4H" and conf4h)) else "b"
    pf = lambda p: round(p, 2)
    return {
        "id":         f"{pair}_{lv['type']}_{int(lv['price'])}",
        "pair":       pair,
        "direction":  "LONG" if bull else "SHORT",
        "grade":      grade,
        "gradeLabel": "A+" if grade == "aplus" else grade.upper(),
        "score":      score,
        "tf":         tf,
        "bull":       bull,
        "rawEntry":   pf(entry),
        "rawSL":      pf(sl),
        "rawTP1":     pf(tp1),
        "rawTP2":     pf(tp2),
        "rawTP3":     pf(tp3),
        "rawRisk":    pf(risk),
        "rr1":        round(rr1, 1),
        "rr2":        round(abs(tp2 - entry) / risk, 1),
        "freshness":  lv["freshness"],
        "hsl":        lv["hsl"],
        "confluence": bool(conf4h),
        "br":         round(crt["br"], 2),
        "trend":      trend,
        "detectedAt": datetime.utcnow().isoformat(),
    }

# ── SIMULATOR UPDATE ──────────────────────────────────────────────────────────
def update_simulator(state, cur_price, new_setups):
    """Update paper trading simulation with current price."""
    changed = False

    # Update existing active trades
    still_active = []
    for t in state["activeTrades"]:
        bull     = t["bull"]
        entry    = t["rawEntry"]
        sl       = t["rawSL"]
        tp1      = t["rawTP1"]
        tp2      = t["rawTP2"]
        risk     = t["rawRisk"]
        riskPct  = RISK_PCT.get(t["grade"], 1.0)
        riskAmt  = t["entryBalance"] * (riskPct / 100)
        posSize  = riskAmt / risk

        phase = t.get("phase", "entry")

        # ── Phase: watching for TP1 or SL ────────────────────────────────────
        if phase == "entry":
            if (bull and cur_price <= sl) or (not bull and cur_price >= sl):
                # Full SL hit
                loss = -riskAmt
                state["balance"] += loss
                state["stats"]["losses"] += 1
                state["stats"]["totalTrades"] += 1
                state["stats"]["netR"] = round(state["stats"]["netR"] - 1, 2)
                state["history"].insert(0, {**t, "result": "loss", "pnl": round(loss, 2),
                    "pnlR": -1.0, "closePrice": round(cur_price, 2),
                    "closedAt": datetime.utcnow().isoformat(), "note": "SL hit"})
                state["equityCurve"].append({"t": datetime.utcnow().isoformat(), "v": round(state["balance"], 2)})
                changed = True
                continue

            if (bull and cur_price >= tp1) or (not bull and cur_price <= tp1):
                # TP1 hit — close 50%, move SL to BE
                profit = posSize * abs(tp1 - entry) * 0.5
                state["balance"] += profit
                t["phase"]        = "tp1"
                t["rawSL"]        = entry  # BE
                t["remainPct"]    = 50
                state["stats"]["netR"] = round(state["stats"]["netR"] + 1.5, 2)
                state["history"].insert(0, {**t, "result": "tp1", "pnl": round(profit, 2),
                    "pnlR": 1.5, "closePrice": round(cur_price, 2),
                    "closedAt": datetime.utcnow().isoformat(), "note": "TP1 hit — 50% closed, SL to BE"})
                state["equityCurve"].append({"t": datetime.utcnow().isoformat(), "v": round(state["balance"], 2)})
                still_active.append(t)
                changed = True
                continue

        # ── Phase: TP1 hit, holding 50% with BE stop ─────────────────────────
        elif phase == "tp1":
            be = entry

            if (bull and cur_price <= be) or (not bull and cur_price >= be):
                # BE stop — zero loss, already booked 1.5R
                state["stats"]["be"] += 1
                state["stats"]["totalTrades"] += 1
                state["history"].insert(0, {**t, "result": "be", "pnl": 0.0,
                    "pnlR": 0.0, "closePrice": round(cur_price, 2),
                    "closedAt": datetime.utcnow().isoformat(), "note": "BE stop — closed at entry"})
                state["equityCurve"].append({"t": datetime.utcnow().isoformat(), "v": round(state["balance"], 2)})
                changed = True
                continue

            if (bull and cur_price >= tp2) or (not bull and cur_price <= tp2):
                # TP2 hit — close remaining 50%
                profit = posSize * abs(tp2 - entry) * 0.5
                state["balance"] += profit
                state["stats"]["wins"] += 1
                state["stats"]["totalTrades"] += 1
                netR = round(1.5 + abs(tp2 - entry) / risk * 0.5, 1)
                state["stats"]["netR"]  = round(state["stats"]["netR"] + netR, 2)
                state["stats"]["bestR"] = max(state["stats"]["bestR"], netR)
                state["history"].insert(0, {**t, "result": "win", "pnl": round(profit, 2),
                    "pnlR": netR, "closePrice": round(cur_price, 2),
                    "closedAt": datetime.utcnow().isoformat(), "note": f"TP2 hit — full close +{netR}R"})
                state["equityCurve"].append({"t": datetime.utcnow().isoformat(), "v": round(state["balance"], 2)})
                changed = True
                continue

        still_active.append(t)

    state["activeTrades"] = still_active

    # Enter new setups
    for s in new_setups:
        already_active = any(t["id"] == s["id"] for t in state["activeTrades"])
        already_done   = any(t["id"] == s["id"] for t in state["history"])
        if already_active or already_done:
            continue

        riskPct = RISK_PCT.get(s["grade"], 1.0)
        riskAmt = state["balance"] * (riskPct / 100)

        trade = {
            **s,
            "phase":        "entry",
            "remainPct":    100,
            "entryBalance": round(state["balance"], 2),
            "riskAmt":      round(riskAmt, 2),
            "riskPct":      riskPct,
            "enteredAt":    datetime.utcnow().isoformat(),
        }
        state["activeTrades"].append(trade)
        state["equityCurve"].append({"t": datetime.utcnow().isoformat(), "v": round(state["balance"], 2)})
        changed = True
        print(f"  Paper trade entered: {s['pair']} {s['direction']} {s['gradeLabel']} "
              f"Entry:${s['rawEntry']:,.0f} Risk:${riskAmt:.2f} ({riskPct}%)")

    # Keep history to last 100 trades
    state["history"] = state["history"][:100]
    # Keep equity curve to last 200 points
    state["equityCurve"] = state["equityCurve"][-200:]

    return changed

# ── ANTI-SPAM ─────────────────────────────────────────────────────────────────
def already_alerted(state, pair, tf, level_price):
    key = f"{pair}_{tf}_{int(level_price)}"
    alerts = state.get("alerts", {})
    if key not in alerts: return False
    last = datetime.fromisoformat(alerts[key])
    now  = datetime.now(timezone.utc).replace(tzinfo=None)
    return (now - last).total_seconds() < SPAM_HOURS * 3600

def mark_alerted(state, pair, tf, level_price):
    key = f"{pair}_{tf}_{int(level_price)}"
    state.setdefault("alerts", {})[key] = datetime.utcnow().isoformat()

# ── TELEGRAM ──────────────────────────────────────────────────────────────────
def send_telegram(msg):
    if not TELEGRAM_TOKEN:
        print("No token:"); print(msg); return
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"
    }).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=10) as r:
            res = json.loads(r.read())
        print("Telegram sent" if res.get("ok") else f"Telegram error: {res}")
    except Exception as e:
        print(f"Telegram error: {e}")

def format_msg(s):
    em = {"aplus": "A+", "a": "A", "b": "B"}
    de = "LONG" if s["direction"] == "LONG" else "SHORT"
    conf = f"YES @ ${s['rawEntry']:,.0f}" if s["confluence"] else "NO"
    pf = lambda p: f"${p:,.0f}"
    return "\n".join([
        f"MSNR + CRT SETUP — {s['pair']}",
        f"",
        f"Grade: {s['gradeLabel']}  |  {de}  |  {s['tf']} level",
        f"",
        f"Entry:  {pf(s['rawEntry'])}",
        f"SL:     {pf(s['rawSL'])}",
        f"TP1:    {pf(s['rawTP1'])}  (+{s['rr1']}R)",
        f"TP2:    {pf(s['rawTP2'])}  (+{s['rr2']}R)",
        f"TP3:    {pf(s['rawTP3'])}  (trail)",
        f"",
        f"Freshness:  {s['freshness']}",
        f"HSL:        {'YES' if s['hsl'] else 'NO'}",
        f"4H Conf:    {conf}",
        f"Body ratio: {s['br']}",
        f"Trend:      {s['trend']}",
        f"",
        f"{datetime.utcnow().strftime('%d %b %Y  %H:%M UTC')}",
        f"",
        f"Verify on TradingView then place orders.",
        f"singhakshat531-sketch.github.io/msnr-scanner",
    ])

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    print(f"=== MSNR Scanner v5  {datetime.utcnow().isoformat()} ===")

    # Load persistent state
    print("\nLoading state from GitHub...")
    state, sha = load_state()
    print(f"  Balance: ${state['balance']:.2f} | Active: {len(state['activeTrades'])} | History: {len(state['history'])}")

    # Fetch BTC data
    print("\nFetching BTC data...")
    d1 = fetch_candles("BTC", "1d", 90);  time.sleep(2)
    h4 = fetch_candles("BTC", "4h", 150); time.sleep(2)
    h1 = fetch_candles("BTC", "1h", 150)

    if not (d1 and h4 and h1):
        print("BTC: Insufficient data"); return

    cur_price = h1[-1]["c"]
    tr        = get_trend(d1)
    print(f"BTC: ${cur_price:,.0f} | Trend: {tr}")

    # Detect levels
    lvl_1d = find_msnr_levels(d1, lb=2, max_dist=12)
    lvl_4h = find_msnr_levels(h4, lb=3, max_dist=8)
    print(f"Levels — 1D: {len(lvl_1d)}  4H: {len(lvl_4h)}")

    # Detect setups
    new_setups  = []
    alert_setups = []
    seen = set()

    # 1D levels
    for lv in lvl_1d:
        key = f"1D_{lv['type']}_{int(lv['price'])}"
        if key in seen: continue
        conf4h = next((l for l in lvl_4h if l["type"] == lv["type"] and
                       abs(l["price"] - lv["price"]) / lv["price"] * 100 <= 1), None)
        crt = find_crt(h1, lv, tr)
        if crt:
            s = build_setup(lv, crt, tr, "1D", conf4h, "BTCUSDT")
            if s:
                new_setups.append(s)
                seen.add(key)
                if not already_alerted(state, "BTCUSDT", "1D", lv["price"]):
                    alert_setups.append(s)
                    mark_alerted(state, "BTCUSDT", "1D", lv["price"])
                print(f"  SETUP 1D {lv['type']}-level ${lv['price']:,.0f} {s['direction']} {s['gradeLabel']}")
        else:
            print(f"  1D {lv['type']}-level ${lv['price']:,.0f} — no CRT")

    # 4H levels
    for lv in lvl_4h:
        key = f"4H_{lv['type']}_{int(lv['price'])}"
        if key in seen: continue
        if any(abs(s["rawEntry"] - lv["price"]) / lv["price"] < 0.01 for s in new_setups): continue
        crt = find_crt(h1, lv, tr)
        if crt:
            s = build_setup(lv, crt, tr, "4H", None, "BTCUSDT")
            if s:
                new_setups.append(s)
                seen.add(key)
                if not already_alerted(state, "BTCUSDT", "4H", lv["price"]):
                    alert_setups.append(s)
                    mark_alerted(state, "BTCUSDT", "4H", lv["price"])
                print(f"  SETUP 4H {lv['type']}-level ${lv['price']:,.0f} {s['direction']} {s['gradeLabel']}")
        else:
            print(f"  4H {lv['type']}-level ${lv['price']:,.0f} — no CRT")

    print(f"\nNew setups: {len(new_setups)} | Alerts to send: {len(alert_setups)}")

    # Update simulator
    print("\nUpdating simulator...")
    changed = update_simulator(state, cur_price, new_setups)
    print(f"  Balance: ${state['balance']:.2f} | Active: {len(state['activeTrades'])}")

    # Send Telegram alerts
    if alert_setups:
        alert_setups.sort(key=lambda x: -x["score"])
        for s in alert_setups:
            send_telegram(format_msg(s))
            time.sleep(1)
    else:
        print("No new alerts to send")

    # Save state to GitHub
    print("\nSaving state to GitHub...")
    save_state(state, sha)

    print("=== Scan complete ===")

if __name__ == "__main__":
    main()
