#!/usr/bin/env python3
"""
MSNR + CRT Scanner — GitHub Actions
BTC only mode — CryptoCompare free API — no key needed.
"""

import json
import urllib.request
import urllib.parse
import os
import time
from datetime import datetime

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "1247283950")
MIN_RR      = 3.0
LOOKBACK_1D = 2
LOOKBACK_4H = 3
LOOKBACK_1H = 3

def fetch_candles(symbol, interval, limit=120):
    endpoint  = "histoday" if interval == "1d" else "histohour"
    aggregate = 1 if interval == "1h" else (4 if interval == "4h" else 1)
    url = (f"https://min-api.cryptocompare.com/data/v2/{endpoint}"
           f"?fsym={symbol}&tsym=USD&limit={limit}&aggregate={aggregate}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MSNR-Scanner/1.0"})
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

def get_trend(candles):
    if len(candles) < 10: return "UNKNOWN"
    c = [x["c"] for x in candles[-20:]]
    bull = sum(1 for i in range(2,len(c)) if c[i]>c[i-1]>c[i-2])
    bear = sum(1 for i in range(2,len(c)) if c[i]<c[i-1]<c[i-2])
    if bull > bear+2: return "BULLISH"
    if bear > bull+2: return "BEARISH"
    return "RANGING"

def find_msnr_levels(candles, lb=3):
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
        if dist > 10: continue
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
        hsl = sum(1 for pc in closes[:i] if abs(pc-price)/price<0.005) >= 2
        levels.append({"type":t,"price":price,"freshness":"FRESH" if wicks==0 else "UNFRESH",
                        "hsl":hsl,"dist":dist})
    return sorted(levels, key=lambda x:x["dist"])[:6]

def find_crt_setups(c1h, levels, trend, pair):
    setups = []
    if len(c1h)<15 or trend=="RANGING": return []
    for lv in levels:
        bull = lv["type"]=="V"
        if bull and trend!="BULLISH": continue
        if not bull and trend!="BEARISH": continue
        for i in range(max(5,len(c1h)-30), len(c1h)-1):
            rs = i-1
            while rs>1:
                a,b = c1h[rs],c1h[rs-1]
                if bull and a["c"]>=b["c"]: break
                if not bull and a["c"]<=b["c"]: break
                rs -= 1
            seg = c1h[rs:i+1]
            rH  = max(x["h"] for x in seg)
            rL  = min(x["l"] for x in seg)
            if (rH-rL)/c1h[-1]["c"] < 0.002: continue
            sw, cf = c1h[i], c1h[i+1]
            crt2 = (sw["l"]<=lv["price"] and sw["c"]>lv["price"] and sw["c"]>rL) if bull else \
                   (sw["h"]>=lv["price"] and sw["c"]<lv["price"] and sw["c"]<rH)
            if not crt2: continue
            body = abs(cf["c"]-cf["o"]); cr = cf["h"]-cf["l"]
            br   = body/cr if cr>0 else 0
            crt3 = (cf["c"]>rH and br>0.5) if bull else (cf["c"]<rL and br>0.5)
            if not crt3: continue
            entry = lv["price"]
            if bull:
                sl=sw["l"]*0.9992; risk=abs(entry-sl); tp1=entry+risk*3; tp2=entry+risk*7
            else:
                sl=sw["h"]*1.0008; risk=abs(sl-entry); tp1=entry-risk*3; tp2=entry-risk*7
            rr1=abs(tp1-entry)/risk; rr2=abs(tp2-entry)/risk
            if rr1<MIN_RR: continue
            score = (2 if lv["freshness"]=="FRESH" else 0)+(1 if lv["hsl"] else 0)+ \
                    (1 if br>0.65 else 0)+(1 if i>=len(c1h)-6 else 0)
            grade = "A+" if score>=5 else "A" if score>=3 else "B"
            pf = lambda p: f"${p:,.0f}"
            setups.append({"pair":pair,"direction":"LONG" if bull else "SHORT","grade":grade,
                "entry":pf(entry),"sl":pf(sl),"tp1":pf(tp1),"tp2":pf(tp2),
                "rr1":f"{rr1:.1f}","rr2":f"{rr2:.1f}","level":pf(lv["price"]),
                "level_type":lv["type"],"freshness":lv["freshness"],"hsl":lv["hsl"],
                "br":f"{br:.2f}","trend":trend,"score":score})
            break
    return sorted(setups,key=lambda x:-x["score"])

def send_telegram(msg):
    if not TELEGRAM_TOKEN:
        print("No token — printing message:"); print(msg); return
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id":TELEGRAM_CHAT_ID,"text":msg,"parse_mode":"HTML"}).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=10) as r:
            res = json.loads(r.read())
        print("Telegram sent ✓" if res.get("ok") else f"Telegram error: {res}")
    except Exception as e:
        print(f"Telegram exception: {e}")

def format_msg(setups):
    em = {"A+":"🟢","A":"🔵","B":"🟡"}
    lines = ["⚡ <b>MSNR + CRT SETUP DETECTED</b>",""]
    for s in setups:
        de = "📈" if s["direction"]=="LONG" else "📉"
        lines += [f"{em.get(s['grade'],'⚪')} <b>{s['pair']} · {s['direction']} · {s['grade']}</b> {de}","",
            f"<b>Entry:</b>  {s['entry']}",f"<b>SL:</b>     {s['sl']}",
            f"<b>TP1:</b>    {s['tp1']}  <i>(+{s['rr1']}R)</i>",
            f"<b>TP2:</b>    {s['tp2']}  <i>(+{s['rr2']}R)</i>","",
            f"<b>Level:</b>     {s['level_type']}-Level @ {s['level']}",
            f"<b>Freshness:</b> {s['freshness']}",
            f"<b>HSL:</b>       {'✓' if s['hsl'] else '✗'}",
            f"<b>Body ratio:</b> {s['br']}","─────────────────",""]
    lines += [f"🕐 {datetime.utcnow().strftime('%d %b %Y · %H:%M UTC')}","","→ Place 3 limit orders · Walk away"]
    return "\n".join(lines)

def main():
    print(f"=== MSNR Scanner {datetime.utcnow().isoformat()} ===")
    all_setups = []

    print("\nFetching BTC data...")
    d1 = fetch_candles("BTC","1d",60)
    time.sleep(2)
    h4 = fetch_candles("BTC","4h",120)
    time.sleep(2)
    h1 = fetch_candles("BTC","1h",120)

    if d1 and h4 and h1:
        tr = get_trend(d1)
        print(f"BTC trend: {tr}")
        lvl_1d = find_msnr_levels(d1, lb=LOOKBACK_1D)
        lvl_4h = find_msnr_levels(h4, lb=LOOKBACK_4H)
        print(f"BTC 1D levels: {len(lvl_1d)}")
        print(f"BTC 4H levels: {len(lvl_4h)}")
        setups = find_crt_setups(h1, lvl_1d, tr, "BTCUSDT")
        if setups:
            print(f"BTC setups found: {len(setups)}")
            for s in setups:
                print(f"  → {s['direction']} {s['grade']} Entry:{s['entry']} TP2:{s['tp2']} +{s['rr2']}R")
            all_setups.extend(setups)
        else:
            print("BTC: No CRT setup detected")
    else:
        print("BTC: Insufficient data")

    print("\nGold: Skipped (BTC-only mode)")
    print()

    if all_setups:
        send_telegram(format_msg(all_setups))
    else:
        print("No setups — no alert sent")
    print("=== Scan complete ===")

if __name__ == "__main__":
    main()
