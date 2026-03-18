#!/usr/bin/env python3
"""
MSNR + CRT Scanner — GitHub Actions
Runs every 30 minutes. Sends Telegram alert when setup detected.
"""

import json
import urllib.request
import urllib.parse
import os
from datetime import datetime

# ── CONFIG ──────────────────────────────────────────────────────────────────
# These are loaded from GitHub Secrets — you never hardcode tokens here
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "1247283950")
TWELVE_DATA_KEY = os.environ.get("TWELVE_DATA_KEY", "")  # optional, for Gold

PAIRS = ["BTCUSDT"]  # add XAUUSD if you have Twelve Data key
MIN_RR = 3.0
LOOKBACK = 3  # candles each side for A/V level detection

# ── FETCH KLINES — CoinGecko + fallback (no API key, no geo-block) ───────────
def fetch_binance(symbol, interval, limit=120):
    """
    Uses CoinGecko API — completely free, no API key, no geo-restrictions.
    Converts OHLC data to candle format compatible with scanner.
    """
    # Map symbol to CoinGecko coin id
    coin_map = {"BTCUSDT": "bitcoin", "XAUUSD": "gold"}
    coin = coin_map.get(symbol, "bitcoin")

    # Map interval to CoinGecko days parameter
    # CoinGecko OHLC: 1=1d, 7=4h, 14=1h granularity (approximate)
    interval_days = {"1d": 90, "4h": 16, "1h": 2}
    days = interval_days.get(interval, 7)

    url = f"https://api.coingecko.com/api/v3/coins/{coin}/ohlc?vs_currency=usd&days={days}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        if not data or not isinstance(data, list):
            print(f"CoinGecko empty response {symbol} {interval}")
            return []
        # data = [[timestamp, open, high, low, close], ...]
        candles = [{"t": k[0], "o": float(k[1]), "h": float(k[2]),
                    "l": float(k[3]), "c": float(k[4])} for k in data]
        # Return last `limit` candles
        return candles[-limit:]
    except Exception as e:
        print(f"CoinGecko fetch error {symbol} {interval}: {e}")
        return []

# ── FETCH TWELVE DATA (Gold) — with CoinGecko fallback ───────────────────────
def fetch_twelve(symbol, interval, outputsize=120):
    # Try Twelve Data first if key exists
    if TWELVE_DATA_KEY:
        url = f"https://api.twelvedata.com/time_series?symbol={symbol}&interval={interval}&outputsize={outputsize}&apikey={TWELVE_DATA_KEY}"
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                data = json.loads(r.read())
            if "values" in data:
                vals = list(reversed(data["values"]))
                return [{"t": i, "o": float(v["open"]), "h": float(v["high"]),
                         "l": float(v["low"]), "c": float(v["close"])} for i, v in enumerate(vals)]
        except Exception as e:
            print(f"Twelve Data fetch error {symbol} {interval}: {e}")
    # Fallback: CoinGecko for gold
    print(f"Using CoinGecko fallback for {symbol} {interval}")
    return fetch_binance("XAUUSD", interval, outputsize)

# ── TREND DETECTION ──────────────────────────────────────────────────────────
def get_trend(candles):
    if len(candles) < 10:
        return "UNKNOWN"
    closes = [c["c"] for c in candles[-20:]]
    bull = sum(1 for i in range(2, len(closes)) if closes[i] > closes[i-1] > closes[i-2])
    bear = sum(1 for i in range(2, len(closes)) if closes[i] < closes[i-1] < closes[i-2])
    if bull > bear + 2:
        return "BULLISH"
    if bear > bull + 2:
        return "BEARISH"
    return "RANGING"

# ── MSNR LEVEL DETECTION ─────────────────────────────────────────────────────
def find_msnr_levels(candles, lb=LOOKBACK):
    """Find A-levels (peaks) and V-levels (valleys) on line chart (closes only)."""
    if len(candles) < lb * 2 + 2:
        return []
    closes = [c["c"] for c in candles]
    levels = []
    for i in range(lb, len(closes) - lb):
        is_a = all(closes[i] > closes[i-j] and closes[i] > closes[i+j] for j in range(1, lb+1))
        is_v = all(closes[i] < closes[i-j] and closes[i] < closes[i+j] for j in range(1, lb+1))
        if not is_a and not is_v:
            continue
        price = closes[i]
        level_type = "A" if is_a else "V"
        # Check freshness
        future = candles[i+1:]
        wicks = 0
        dead = False
        for fc in future:
            if level_type == "A":
                if fc["c"] > price:
                    dead = True
                    break
                if fc["h"] >= price:
                    wicks += 1
            else:
                if fc["c"] < price:
                    dead = True
                    break
                if fc["l"] <= price:
                    wicks += 1
        if dead:
            continue
        freshness = "FRESH" if wicks == 0 else "UNFRESH"
        # HSL check
        prev_closes = closes[:i]
        hsl = sum(1 for pc in prev_closes if abs(pc - price) / price < 0.005) >= 2
        # Distance from current price
        cur = candles[-1]["c"]
        dist_pct = abs(cur - price) / cur * 100
        if dist_pct < 8:  # only levels within 8% of current price
            levels.append({
                "type": level_type,
                "price": price,
                "freshness": freshness,
                "hsl": hsl,
                "dist_pct": dist_pct,
                "idx": i
            })
    # Sort by distance, return closest levels
    return sorted(levels, key=lambda x: x["dist_pct"])[:5]

# ── CRT PATTERN DETECTION ────────────────────────────────────────────────────
def find_crt_setup(candles_1h, msnr_levels, trend_dir, pair):
    """
    Scan 1H candles for CRT pattern at each MSNR level.
    Returns list of valid setups found.
    """
    setups = []
    if len(candles_1h) < 15:
        return []
    if trend_dir == "RANGING":
        return []

    for level in msnr_levels:
        is_bull = level["type"] == "V"
        # Only trade with trend
        if is_bull and trend_dir != "BULLISH":
            continue
        if not is_bull and trend_dir != "BEARISH":
            continue

        # Scan recent 1H candles for CRT (look at last 24 candles)
        scan_start = max(5, len(candles_1h) - 24)
        for i in range(scan_start, len(candles_1h) - 1):
            # Define external range — find start of consecutive directional move
            range_start = i - 1
            while range_start > 1:
                a = candles_1h[range_start]
                b = candles_1h[range_start - 1]
                if is_bull and a["c"] >= b["c"]:
                    break
                if not is_bull and a["c"] <= b["c"]:
                    break
                range_start -= 1

            range_slice = candles_1h[range_start:i+1]
            range_high = max(c["h"] for c in range_slice)
            range_low = min(c["l"] for c in range_slice)
            range_size = range_high - range_low

            # Skip tiny ranges
            cur_price = candles_1h[-1]["c"]
            if range_size / cur_price < 0.002:
                continue

            sweep = candles_1h[i]
            if i + 1 >= len(candles_1h):
                continue
            confirm = candles_1h[i + 1]

            # CRT-2: wick sweeps MSNR level, CLOSE back inside range
            if is_bull:
                crt2 = (sweep["l"] <= level["price"] and
                        sweep["c"] > level["price"] and
                        sweep["c"] > range_low)
            else:
                crt2 = (sweep["h"] >= level["price"] and
                        sweep["c"] < level["price"] and
                        sweep["c"] < range_high)

            if not crt2:
                continue

            # CRT-3: decisive body closes BEYOND entire external range
            body_size = abs(confirm["c"] - confirm["o"])
            candle_range = confirm["h"] - confirm["l"]
            body_ratio = body_size / candle_range if candle_range > 0 else 0

            if is_bull:
                crt3 = confirm["c"] > range_high and body_ratio > 0.5
            else:
                crt3 = confirm["c"] < range_low and body_ratio > 0.5

            if not crt3:
                continue

            # Valid CRT setup found — calculate levels
            entry = level["price"]
            if is_bull:
                sl = sweep["l"] * 0.9992
                risk = abs(entry - sl)
                tp1 = entry + risk * 3
                tp2 = entry + risk * 7
            else:
                sl = sweep["h"] * 1.0008
                risk = abs(sl - entry)
                tp1 = entry - risk * 3
                tp2 = entry - risk * 7

            rr1 = abs(tp1 - entry) / risk
            rr2 = abs(tp2 - entry) / risk

            if rr1 < MIN_RR:
                continue

            # Grade
            score = 0
            if level["freshness"] == "FRESH":
                score += 2
            if level["hsl"]:
                score += 1
            if body_ratio > 0.65:
                score += 1
            if i >= len(candles_1h) - 6:  # very recent
                score += 1

            grade = "A+" if score >= 5 else "A" if score >= 3 else "B"

            # Format prices
            is_btc = "BTC" in pair
            def pf(p):
                return f"${p:,.0f}" if is_btc else f"${p:,.2f}"

            setups.append({
                "pair": pair,
                "direction": "LONG" if is_bull else "SHORT",
                "grade": grade,
                "entry": pf(entry),
                "sl": pf(sl),
                "tp1": pf(tp1),
                "tp2": pf(tp2),
                "rr1": f"{rr1:.1f}",
                "rr2": f"{rr2:.1f}",
                "level_price": pf(level["price"]),
                "level_type": level["type"],
                "freshness": level["freshness"],
                "hsl": level["hsl"],
                "body_ratio": f"{body_ratio:.2f}",
                "trend": trend_dir,
                "score": score,
            })
            break  # one setup per level

    return sorted(setups, key=lambda x: -x["score"])

# ── TELEGRAM ─────────────────────────────────────────────────────────────────
def send_telegram(message):
    if not TELEGRAM_TOKEN:
        print("No Telegram token — skipping message")
        print("MESSAGE WOULD BE:")
        print(message)
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=10) as r:
            result = json.loads(r.read())
            if result.get("ok"):
                print("Telegram message sent ✓")
                return True
            else:
                print(f"Telegram error: {result}")
                return False
    except Exception as e:
        print(f"Telegram send error: {e}")
        return False

def format_message(setups):
    lines = ["⚡ <b>MSNR + CRT SETUP DETECTED</b>", ""]
    for s in setups:
        grade_emoji = "🟢" if s["grade"] == "A+" else "🔵" if s["grade"] == "A" else "🟡"
        dir_emoji = "📈" if s["direction"] == "LONG" else "📉"
        lines += [
            f"{grade_emoji} <b>{s['pair']} · {s['direction']} · {s['grade']}</b> {dir_emoji}",
            f"",
            f"<b>Entry:</b> {s['entry']}",
            f"<b>Stop Loss:</b> {s['sl']}",
            f"<b>TP1:</b> {s['tp1']} <i>(+{s['rr1']}R)</i>",
            f"<b>TP2:</b> {s['tp2']} <i>(+{s['rr2']}R)</i>",
            f"",
            f"<b>Level:</b> {s['level_type']}-Level @ {s['level_price']}",
            f"<b>Freshness:</b> {s['freshness']}",
            f"<b>HSL:</b> {'✓ YES' if s['hsl'] else '✗ NO'}",
            f"<b>Body Ratio:</b> {s['body_ratio']}",
            f"<b>Trend:</b> {s['trend']}",
            f"",
            f"────────────────",
            f"",
        ]
    lines += [
        f"🕐 {datetime.utcnow().strftime('%d %b %Y · %H:%M UTC')}",
        f"",
        f"→ Place 3 limit orders · Walk away",
        f"→ Check scanner for full analysis",
    ]
    return "\n".join(lines)

# ── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    print(f"=== MSNR Scanner running at {datetime.utcnow().isoformat()} ===")
    all_setups = []

    # ── BTC ──
    print("Fetching BTC data...")
    btc_1d = fetch_binance("BTCUSDT", "1d", 60)
    btc_4h = fetch_binance("BTCUSDT", "4h", 120)
    btc_1h = fetch_binance("BTCUSDT", "1h", 120)

    if btc_1d and btc_4h and btc_1h:
        btc_trend = get_trend(btc_1d)
        print(f"BTC trend: {btc_trend}")
        btc_levels_1d = find_msnr_levels(btc_1d)
        btc_levels_4h = find_msnr_levels(btc_4h)
        print(f"BTC 1D levels found: {len(btc_levels_1d)}")
        print(f"BTC 4H levels found: {len(btc_levels_4h)}")
        # Use 1D levels as primary (entry levels)
        btc_setups = find_crt_setup(btc_1h, btc_levels_1d, btc_trend, "BTCUSDT")
        if btc_setups:
            print(f"BTC setups found: {len(btc_setups)}")
            all_setups.extend(btc_setups)
        else:
            print("BTC: No CRT setup detected")
    else:
        print("BTC: Insufficient data")

    # ── GOLD (if key available) ──
    if TWELVE_DATA_KEY:
        print("Fetching Gold data...")
        gold_1d = fetch_twelve("XAU/USD", "1day", 60)
        gold_4h = fetch_twelve("XAU/USD", "4h", 120)
        gold_1h = fetch_twelve("XAU/USD", "1h", 120)

        if gold_1d and gold_4h and gold_1h:
            gold_trend = get_trend(gold_1d)
            print(f"Gold trend: {gold_trend}")
            gold_levels_1d = find_msnr_levels(gold_1d)
            print(f"Gold 1D levels found: {len(gold_levels_1d)}")
            gold_setups = find_crt_setup(gold_1h, gold_levels_1d, gold_trend, "XAUUSD")
            if gold_setups:
                print(f"Gold setups found: {len(gold_setups)}")
                all_setups.extend(gold_setups)
            else:
                print("Gold: No CRT setup detected")
        else:
            print("Gold: Insufficient data or API error")
    else:
        print("Gold: No Twelve Data key — skipping")

    # ── SEND ALERT ──
    if all_setups:
        print(f"\nTotal setups found: {len(all_setups)}")
        msg = format_message(all_setups)
        send_telegram(msg)
    else:
        print("\nNo setups found this run — no alert sent")

    print("=== Scan complete ===")

if __name__ == "__main__":
    main()
