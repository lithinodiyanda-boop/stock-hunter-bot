import os
import io
import time
import zipfile
import requests
import schedule
from datetime import datetime, timedelta
from twilio.rest import Client
import pytz

# ============================================================
# CREDENTIALS — from Railway environment variables
# ============================================================
TWILIO_ACCOUNT_SID   = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN    = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_WHATSAPP_FROM = os.environ["TWILIO_WHATSAPP_FROM"]
YOUR_WHATSAPP_TO     = os.environ["YOUR_WHATSAPP_TO"]

# ============================================================
# SETTINGS
# ============================================================
TOTAL_CAPITAL  = 100000
MAX_POSITION   = 25000
GROSS_TARGET   = 0.065
STOP_LOSS      = 0.060
MIN_SCORE      = 55
IST            = pytz.timezone("Asia/Kolkata")

# ============================================================
# STATE
# ============================================================
alerted_today    = set()
open_positions   = {}
nse_data_cache   = {}   # symbol -> list of daily closes/volumes
fund_cache       = {}   # symbol -> fundamentals

# ============================================================
# STEP 1 — FETCH NSE BHAVCOPY (all 2000+ stocks in one call)
# ============================================================
def fetch_nse_bhavcopy(date):
    url = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{}_F_0000.csv.zip".format(
        date.strftime("%Y%m%d")
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer":    "https://www.nseindia.com",
        "Accept":     "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        r = requests.get(url, headers=headers, timeout=30)
        if r.status_code == 200:
            z = zipfile.ZipFile(io.BytesIO(r.content))
            csv = z.read(z.namelist()[0]).decode("utf-8")
            return csv
        return None
    except Exception:
        return None


def parse_bhavcopy(csv_text):
    """Parse bhavcopy CSV into dict: symbol -> {close, volume, open, high, low}"""
    result = {}
    lines  = csv_text.strip().split("\n")
    if not lines:
        return result

    header = [h.strip().upper() for h in lines[0].split(",")]

    try:
        # UDiFF format (new since July 2024)
        sym_idx   = next((i for i, h in enumerate(header) if h in ["TCKRSYMB", "SYMBOL"]), None)
        ser_idx   = next((i for i, h in enumerate(header) if h in ["SCTYSRS", "SERIES"]), None)
        close_idx = next((i for i, h in enumerate(header) if h in ["CLSPRIC", "CLOSE_PRICE", "CLOSE"]), None)
        open_idx  = next((i for i, h in enumerate(header) if h in ["OPNPRIC", "OPEN_PRICE", "OPEN"]), None)
        high_idx  = next((i for i, h in enumerate(header) if h in ["HGHPRIC", "HIGH_PRICE", "HIGH"]), None)
        low_idx   = next((i for i, h in enumerate(header) if h in ["LWPRIC", "LOW_PRICE", "LOW"]), None)
        vol_idx   = next((i for i, h in enumerate(header) if h in ["TTLTRADGVOL", "TTL_TRD_QNTY", "VOLUME"]), None)

        if any(x is None for x in [sym_idx, ser_idx, close_idx, vol_idx]):
            print("Missing required columns. Available: " + str(header))
            return result
    except ValueError as e:
        print("Header parse error: " + str(e))
        print("Available headers: " + str(header))
        return result

    for line in lines[1:]:
        cols = line.split(",")
        if len(cols) <= max(sym_idx, close_idx, vol_idx):
            continue
        try:
            series = cols[ser_idx].strip()
            if series != "EQ":  # only equity series
                continue
            symbol = cols[sym_idx].strip()
            close  = float(cols[close_idx].strip())
            volume = float(cols[vol_idx].strip())
            open_  = float(cols[open_idx].strip())
            high   = float(cols[high_idx].strip())
            low    = float(cols[low_idx].strip())
            result[symbol] = {
                "close": close, "volume": volume,
                "open": open_, "high": high, "low": low
            }
        except Exception:
            continue

    return result


def load_nse_history(days=90):
    """Load last N trading days of bhavcopy data into cache"""
    global nse_data_cache
    print("Loading NSE historical data for last " + str(days) + " trading days...")

    daily_data = {}  # date -> {symbol -> data}
    loaded     = 0
    attempts   = 0
    check_date = datetime.now(IST) - timedelta(days=1)

    while loaded < days and attempts < days + 20:
        attempts += 1
        if check_date.weekday() >= 5:  # skip weekends
            check_date -= timedelta(days=1)
            continue

        csv = fetch_nse_bhavcopy(check_date)
        if csv:
            parsed = parse_bhavcopy(csv)
            if parsed:
                daily_data[check_date.date()] = parsed
                loaded += 1
                if loaded % 10 == 0:
                    print("  Loaded " + str(loaded) + " days...")
        time.sleep(0.3)
        check_date -= timedelta(days=1)

    print("Loaded " + str(loaded) + " days of NSE data covering " + str(len(daily_data)) + " trading days")

    # Restructure: symbol -> sorted list of {date, close, volume, ...}
    nse_data_cache = {}
    dates_sorted   = sorted(daily_data.keys(), reverse=True)

    for date in dates_sorted:
        for symbol, data in daily_data[date].items():
            if symbol not in nse_data_cache:
                nse_data_cache[symbol] = []
            nse_data_cache[symbol].append({
                "date":   date,
                "close":  data["close"],
                "volume": data["volume"],
                "open":   data["open"],
                "high":   data["high"],
                "low":    data["low"],
            })

    print("Cache built for " + str(len(nse_data_cache)) + " symbols")
    return len(nse_data_cache)


def refresh_today():
    """Add today's data to cache — called after market close"""
    today = datetime.now(IST)
    csv   = fetch_nse_bhavcopy(today)
    if not csv:
        # Try yesterday if market just closed
        csv = fetch_nse_bhavcopy(today - timedelta(days=1))
    if csv:
        parsed = parse_bhavcopy(csv)
        date   = today.date()
        for symbol, data in parsed.items():
            if symbol in nse_data_cache:
                # Remove if already have today
                nse_data_cache[symbol] = [d for d in nse_data_cache[symbol] if d["date"] != date]
                nse_data_cache[symbol].insert(0, {"date": date, **data})
            else:
                nse_data_cache[symbol] = [{"date": date, **data}]
        print("Today's data refreshed for " + str(len(parsed)) + " symbols")


# ============================================================
# STEP 2 — FETCH FUNDAMENTALS FROM SCREENER.IN
# ============================================================
def fetch_fundamentals(symbol):
    if symbol in fund_cache:
        return fund_cache[symbol]

    try:
        url     = "https://www.screener.in/company/" + symbol + "/consolidated/"
        headers = {"User-Agent": "Mozilla/5.0"}
        r       = requests.get(url, headers=headers, timeout=15)

        if r.status_code != 200:
            url = "https://www.screener.in/company/" + symbol + "/"
            r   = requests.get(url, headers=headers, timeout=15)

        if r.status_code != 200:
            return default_fundamentals()

        text = r.text
        fund = default_fundamentals()

        # Extract PE ratio
        if "Stock P/E" in text:
            try:
                idx   = text.index("Stock P/E")
                chunk = text[idx:idx + 200]
                nums  = [float(s) for s in chunk.replace(",", "").split() if s.replace(".", "").isdigit()]
                if nums:
                    fund["pe"] = nums[0]
            except Exception:
                pass

        # Extract Debt to Equity
        if "Debt to equity" in text:
            try:
                idx   = text.index("Debt to equity")
                chunk = text[idx:idx + 200]
                nums  = [float(s) for s in chunk.replace(",", "").split() if s.replace(".", "").replace("-","").isdigit()]
                if nums:
                    fund["de"] = nums[0]
            except Exception:
                pass

        # Extract ROE
        if "Return on equity" in text:
            try:
                idx   = text.index("Return on equity")
                chunk = text[idx:idx + 200]
                nums  = [float(s) for s in chunk.replace(",", "").split() if s.replace(".", "").replace("-","").isdigit()]
                if nums:
                    fund["roe"] = nums[0]
            except Exception:
                pass

        fund_cache[symbol] = fund
        time.sleep(0.5)  # polite rate limiting
        return fund

    except Exception:
        return default_fundamentals()


def default_fundamentals():
    return {"pe": 0, "de": 0, "roe": 0}


# ============================================================
# STEP 3 — SCORING ENGINE
# ============================================================
def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50  # neutral if not enough data
    gains  = []
    losses = []
    for i in range(period):
        change = closes[i] - closes[i + 1]  # newer - older
        if change > 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100
    rs  = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)


def score_stock(symbol):
    history = nse_data_cache.get(symbol)
    if not history or len(history) < 15:
        return None

    closes  = [d["close"]  for d in history]
    volumes = [d["volume"] for d in history]

    current_price = closes[0]

    # Skip penny stocks under Rs.5
    if current_price < 5:
        return None

    # Liquidity check — volume in shares, value = price x shares
    vol_count       = min(20, len(volumes))
    recent_vol      = sum(volumes[:3]) / 3
    avg_vol         = sum(volumes[:vol_count]) / vol_count
    vol_ratio       = recent_vol / avg_vol if avg_vol > 0 else 0

    # Daily traded value in rupees
    avg_daily_value = current_price * avg_vol

    # Volume wakeup: dead stock coming alive
    volume_wakeup = vol_ratio >= 2.0 and avg_daily_value >= 100000

    # Minimum liquidity: Rs.1 lakh daily OR volume wakeup
    if avg_daily_value < 100000 and not volume_wakeup:
        return None

    score   = 0
    reasons = []

    # ── BUCKET 1: SMART MONEY TIMING (35 pts) ──
    rsi      = calculate_rsi(closes)
    rsi_prev = calculate_rsi(closes[1:])
    rsi_up   = rsi > rsi_prev

    if 30 <= rsi <= 45 and rsi_up:
        score += 20
        reasons.append("RSI turning up from oversold (" + str(rsi) + ")")
    elif 30 <= rsi <= 45:
        score += 12
        reasons.append("RSI in buy zone (" + str(rsi) + ")")
    elif rsi < 30 and rsi_up:
        score += 10
        reasons.append("RSI reversing from deeply oversold (" + str(rsi) + ")")

    if vol_ratio >= 2.0:
        score += 15
        reasons.append("Volume surge " + str(round(vol_ratio, 1)) + "x — smart money")
    elif vol_ratio >= 1.5:
        score += 10
        reasons.append("Volume pickup " + str(round(vol_ratio, 1)) + "x")
    elif volume_wakeup:
        score += 8
        reasons.append("Dead stock waking up")

    # ── BUCKET 2: ROOM TO RUN (35 pts) ──
    w52h = max(closes[:min(252, len(closes))])
    w52l = min(closes[:min(252, len(closes))])
    pos  = (current_price - w52l) / (w52h - w52l) if w52h != w52l else 0.5

    if 0.25 <= pos <= 0.55:
        score += 20
        reasons.append("52wk sweet spot " + str(round(pos * 100)) + "% — room to run")
    elif pos < 0.25:
        score += 12
        reasons.append("Near 52wk low — deep value")

    if len(closes) >= 200:
        ma200 = sum(closes[:200]) / 200
        ma50  = sum(closes[:50])  / 50
        if current_price > ma200:
            score += 10
            reasons.append("Above 200 DMA — uptrend intact")
        elif current_price > ma200 * 0.97:
            score += 5
            reasons.append("Just below 200 DMA")
        if current_price < ma50 * 1.02:
            score += 5
            reasons.append("Near 50 DMA dip")
    elif len(closes) >= 50:
        ma50 = sum(closes[:50]) / 50
        if current_price < ma50 * 1.02:
            score += 5
            reasons.append("Near 50 DMA dip")

    # ── BUCKET 3: QUALITY AND VALUE (30 pts) ──
    fund = fetch_fundamentals(symbol)

    pe = fund["pe"]
    if 0 < pe < 15:
        score += 12
        reasons.append("Cheap PE " + str(round(pe, 1)))
    elif 0 < pe < 25:
        score += 9
        reasons.append("Good PE " + str(round(pe, 1)))
    elif 0 < pe < 40:
        score += 5
        reasons.append("Fair PE " + str(round(pe, 1)))

    de = fund["de"]
    if 0 < de < 0.5:
        score += 8
        reasons.append("Very low debt")
    elif 0 < de < 1.0:
        score += 5
        reasons.append("Low debt")
    elif 0 < de < 2.0:
        score += 2

    roe = fund["roe"]
    if roe > 20:
        score += 10
        reasons.append("Strong ROE " + str(round(roe)) + "%")
    elif roe > 12:
        score += 6
        reasons.append("Good ROE " + str(round(roe)) + "%")

    # ── PROFIT MATH ──
    entry      = round(current_price * 1.002, 2)
    target     = round(entry * (1 + GROSS_TARGET), 2)
    stop       = round(entry * (1 - STOP_LOSS), 2)
    shares     = int(MAX_POSITION / entry)
    invested   = round(shares * entry, 2)
    net_profit = round(shares * (target - entry) * 0.85 - invested * 0.005, 2)
    net_pct    = round((net_profit / invested) * 100, 2) if invested > 0 else 0

    if shares < 1:
        return None

    return {
        "symbol":     symbol,
        "score":      min(100, score),
        "price":      round(current_price, 2),
        "entry":      entry,
        "target":     target,
        "stop":       stop,
        "shares":     shares,
        "invested":   invested,
        "net_profit": net_profit,
        "net_pct":    net_pct,
        "rsi":        rsi,
        "vol_ratio":  round(vol_ratio, 2),
        "reasons":    reasons[:3],
    }


# ============================================================
# WHATSAPP
# ============================================================
def send_whatsapp(message):
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    chunks = [message[i:i + 1550] for i in range(0, len(message), 1550)]
    for chunk in chunks:
        client.messages.create(
            from_=TWILIO_WHATSAPP_FROM,
            to=YOUR_WHATSAPP_TO,
            body=chunk
        )
        time.sleep(1)
    print("WhatsApp sent (" + str(len(chunks)) + " message(s))")


# ============================================================
# MONITOR OPEN POSITIONS
# ============================================================
def monitor_positions():
    if not open_positions:
        return
    exits = []
    for symbol, pos in list(open_positions.items()):
        history = nse_data_cache.get(symbol)
        if not history:
            continue
        price = history[0]["close"]
        days  = (datetime.now(IST).date() - pos["entry_date"]).days

        if price >= pos["target"]:
            exits.append("TARGET HIT - SELL " + symbol + " @ Rs." + str(round(price, 2)) + " | 5%+ done in " + str(days) + " days")
            del open_positions[symbol]
        elif price <= pos["stop"]:
            exits.append("STOP LOSS - SELL " + symbol + " @ Rs." + str(round(price, 2)) + " | Cut loss now")
            del open_positions[symbol]
        elif days >= 55:
            exits.append("TIME STOP - SELL " + symbol + " @ Rs." + str(round(price, 2)) + " | Day 55, free capital")
            del open_positions[symbol]

    if exits:
        msg  = "EXIT ALERT | " + datetime.now(IST).strftime("%d %b %I:%M %p") + "\n"
        msg += "=" * 28 + "\n"
        for e in exits:
            msg += e + "\n"
        msg += "\nGo to Groww and SELL now.\n5% Hunter Bot"
        send_whatsapp(msg)


# ============================================================
# MAIN SCAN
# ============================================================
def run_scan():
    if not nse_data_cache:
        print("No data in cache yet. Loading...")
        load_nse_history(60)

    now     = datetime.now(IST)
    now_str = now.strftime("%I:%M %p | %d %b")
    total   = len(nse_data_cache)

    print("\n" + "=" * 50)
    print("SCANNING " + str(total) + " STOCKS — " + now_str)
    print("=" * 50)

    # Refresh today's prices first
    refresh_today()
    monitor_positions()

    results = []
    count   = 0

    for symbol in list(nse_data_cache.keys()):
        if symbol in alerted_today:
            continue
        count += 1
        if count % 100 == 0:
            print("  Processed " + str(count) + "/" + str(total) + "...")

        result = score_stock(symbol)
        if result and result["score"] >= MIN_SCORE:
            results.append(result)

    results.sort(key=lambda x: x["score"], reverse=True)
    top = results[:8]

    print("\nFound " + str(len(results)) + " stocks above score " + str(MIN_SCORE))

    if not top:
        print("No alerts to send this scan.")
        return

    msg  = "SCAN | " + now_str + " | " + str(len(top)) + " stocks\n"
    msg += "=" * 28 + "\n\n"

    for i, r in enumerate(top):
        msg += "#" + str(i + 1) + " " + r["symbol"] + " | " + str(r["score"]) + "/100\n"
        msg += "E:Rs." + str(r["entry"]) + " T:Rs." + str(r["target"]) + " SL:Rs." + str(r["stop"]) + "\n"
        msg += "Invest:Rs." + str(r["invested"]) + " Net:Rs." + str(r["net_profit"]) + "(" + str(r["net_pct"]) + "%)\n"
        msg += ", ".join(r["reasons"][:2]) + "\n\n"

    msg += "5% Hunter Bot | " + str(total) + " stocks scanned"
    send_whatsapp(msg)

    for r in top:
        alerted_today.add(r["symbol"])

    print("Alert sent for " + str(len(top)) + " stocks!")


# ============================================================
# DAILY RESET + DATA REFRESH
# ============================================================
def daily_reset():
    alerted_today.clear()
    fund_cache.clear()
    print("Daily reset done.")


def morning_data_load():
    """Load fresh data every morning before market opens"""
    print("Morning data load starting...")
    load_nse_history(90)
    print("Morning data load complete. Ready for scanning.")


# ============================================================
# SCHEDULER
# ============================================================
def start_scheduler():
    print("5% HUNTER BOT — NSE EDITION")
    print("Data: NSE Bhavcopy + Screener.in")
    print("Universe: ALL NSE listed stocks")
    print("Scans: Hourly 10am-3pm IST Mon-Fri")
    print("Loading initial data...\n")

    # Load data on startup
    load_nse_history(90)

    for day in ["monday", "tuesday", "wednesday", "thursday", "friday"]:
        getattr(schedule.every(), day).at("09:00").do(morning_data_load)
        getattr(schedule.every(), day).at("10:00").do(run_scan)
        getattr(schedule.every(), day).at("11:00").do(run_scan)
        getattr(schedule.every(), day).at("12:00").do(run_scan)
        getattr(schedule.every(), day).at("13:00").do(run_scan)
        getattr(schedule.every(), day).at("14:00").do(run_scan)
        getattr(schedule.every(), day).at("15:00").do(run_scan)

    schedule.every().day.at("00:01").do(daily_reset)

    while True:
        schedule.run_pending()
        time.sleep(30)


# ============================================================
# ENTRY POINT
# ============================================================
if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "scan":
        print("Loading NSE data first...")
        load_nse_history(60)
        run_scan()

    elif len(sys.argv) > 1 and sys.argv[1] == "test":
        print("Loading 10 days of NSE data for test...")
        load_nse_history(10)
        symbols = list(nse_data_cache.keys())[:5]
        print("Scoring first 5 stocks: " + str(symbols))
        for s in symbols:
            r = score_stock(s)
            if r:
                print(s + " | Score: " + str(r["score"]) + " | Rs." + str(r["price"]))

    else:
        start_scheduler()
