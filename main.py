import yfinance as yf
import os
from twilio.rest import Client
from datetime import datetime
import pytz
import schedule
import time

# ============================================================
# CREDENTIALS — loaded from environment variables
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
# NSE STOCK UNIVERSE
# ============================================================
STOCKS = [
    "RELIANCE.NS","TCS.NS","HDFCBANK.NS","INFY.NS","ICICIBANK.NS",
    "HINDUNILVR.NS","SBIN.NS","BHARTIARTL.NS","ITC.NS","KOTAKBANK.NS",
    "LT.NS","AXISBANK.NS","ASIANPAINT.NS","MARUTI.NS","SUNPHARMA.NS",
    "TITAN.NS","ULTRACEMCO.NS","BAJFINANCE.NS","WIPRO.NS","HCLTECH.NS",
    "NESTLEIND.NS","POWERGRID.NS","NTPC.NS","TECHM.NS","ONGC.NS",
    "JSWSTEEL.NS","TATASTEEL.NS","COALINDIA.NS","ADANIENT.NS",
    "DIXON.NS","POLYCAB.NS","VOLTAS.NS","MPHASIS.NS",
    "PERSISTENT.NS","COFORGE.NS","LTTS.NS","TATAELXSI.NS","KPITTECH.NS",
    "APOLLOHOSP.NS","MAXHEALTH.NS","FORTIS.NS","LALPATHLAB.NS",
    "AUROPHARMA.NS","TORNTPHARM.NS","ALKEM.NS",
    "GODREJCP.NS","MARICO.NS","DABUR.NS","COLPAL.NS",
    "BAJAJFINSV.NS","MUTHOOTFIN.NS","CHOLAFIN.NS","MANAPPURAM.NS",
    "INDHOTEL.NS","IRCTC.NS","CONCOR.NS",
    "RAILTEL.NS","IRFC.NS","RVNL.NS",
    "AAVAS.NS","HOMEFIRST.NS","NAUKRI.NS","JUSTDIAL.NS",
    "CDSL.NS","BSE.NS","INDIGO.NS",
    "RECLTD.NS","PFC.NS","NHPC.NS","SJVN.NS",
    "SUZLON.NS","TRENT.NS","DMART.NS","PVRINOX.NS",
    "MOTHERSON.NS","BALKRISIND.NS","ESCORTS.NS",
]

# ============================================================
# STATE
# ============================================================
alerted_today  = set()
open_positions = {}

# ============================================================
# SCORING ENGINE
# ============================================================
def score_stock(symbol):
    try:
        ticker = yf.Ticker(symbol)
        hist   = ticker.history(period="6mo")
        info   = ticker.info

        if hist.empty or len(hist) < 50:
            return None

        current_price = hist["Close"].iloc[-1]
        if current_price < 10:
            return None

        avg_daily_value = (hist["Close"] * hist["Volume"]).rolling(20).mean().iloc[-1]
        recent_vol      = hist["Volume"].iloc[-3:].mean()
        avg_vol         = hist["Volume"].rolling(20).mean().iloc[-1]
        vol_ratio       = recent_vol / avg_vol if avg_vol > 0 else 0
        volume_wakeup   = vol_ratio >= 2.0 and avg_daily_value >= 500000

        if avg_daily_value < 1000000 and not volume_wakeup:
            return None

        score   = 0
        reasons = []

        # BUCKET 1: SMART MONEY TIMING (35 pts)
        delta      = hist["Close"].diff()
        gain       = delta.where(delta > 0, 0).rolling(14).mean()
        loss       = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rsi_series = 100 - (100 / (1 + gain / loss))
        rsi        = rsi_series.iloc[-1]
        rsi_up     = rsi > rsi_series.iloc[-2]

        if 30 <= rsi <= 45 and rsi_up:
            score += 20
            reasons.append("RSI turning up from oversold")
        elif 30 <= rsi <= 45:
            score += 12
            reasons.append("RSI in buy zone")
        elif rsi < 30 and rsi_up:
            score += 10
            reasons.append("RSI reversing from deeply oversold")

        if vol_ratio >= 2.0:
            score += 15
            reasons.append("Volume surge " + str(round(vol_ratio, 1)) + "x")
        elif vol_ratio >= 1.5:
            score += 10
            reasons.append("Volume pickup " + str(round(vol_ratio, 1)) + "x")
        elif volume_wakeup:
            score += 8
            reasons.append("Dead stock waking up")

        # BUCKET 2: ROOM TO RUN (35 pts)
        w52h = info.get("fiftyTwoWeekHigh", current_price)
        w52l = info.get("fiftyTwoWeekLow",  current_price)
        pos  = (current_price - w52l) / (w52h - w52l) if w52h != w52l else 0.5

        if 0.25 <= pos <= 0.55:
            score += 20
            reasons.append("52wk sweet spot — room to run")
        elif pos < 0.25:
            score += 12
            reasons.append("Near 52wk low — deep value")

        if len(hist) >= 200:
            ma200 = hist["Close"].rolling(200).mean().iloc[-1]
            ma50  = hist["Close"].rolling(50).mean().iloc[-1]
            if current_price > ma200:
                score += 10
                reasons.append("Above 200 DMA")
            elif current_price > ma200 * 0.97:
                score += 5
                reasons.append("Just below 200 DMA")
            if current_price < ma50 * 1.02:
                score += 5
                reasons.append("Near 50 DMA dip")

        # BUCKET 3: QUALITY AND VALUE (30 pts)
        pe = info.get("trailingPE", None)
        if pe and 0 < pe < 15:
            score += 12
            reasons.append("Cheap PE " + str(round(pe, 1)))
        elif pe and pe < 25:
            score += 9
            reasons.append("Good PE " + str(round(pe, 1)))
        elif pe and pe < 40:
            score += 5
            reasons.append("Fair PE " + str(round(pe, 1)))

        eq_growth  = info.get("earningsQuarterlyGrowth", None)
        rev_growth = info.get("revenueGrowth", None)
        if eq_growth and eq_growth > 0.20:
            score += 10
            reasons.append("Earnings up " + str(round(eq_growth * 100)) + "%")
        elif eq_growth and eq_growth > 0.10:
            score += 6
            reasons.append("Earnings growing")
        elif rev_growth and rev_growth > 0.15:
            score += 4
            reasons.append("Revenue growing")

        de = info.get("debtToEquity", None)
        if de is not None and de < 30:
            score += 8
            reasons.append("Low debt")
        elif de is not None and de < 80:
            score += 4

        # PROFIT MATH
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
            "symbol":     symbol.replace(".NS", ""),
            "score":      min(100, score),
            "price":      round(current_price, 2),
            "entry":      entry,
            "target":     target,
            "stop":       stop,
            "shares":     shares,
            "invested":   invested,
            "net_profit": net_profit,
            "net_pct":    net_pct,
            "rsi":        round(rsi, 1),
            "vol_ratio":  round(vol_ratio, 2),
            "reasons":    reasons[:3],
        }

    except Exception:
        return None


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
        try:
            data  = yf.Ticker(symbol + ".NS").history(period="1d")
            if data.empty:
                continue
            price = data["Close"].iloc[-1]
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
        except Exception:
            continue

    if exits:
        msg = "EXIT ALERT | " + datetime.now(IST).strftime("%d %b %I:%M %p") + "\n"
        msg += "=" * 28 + "\n"
        for e in exits:
            msg += e + "\n"
        msg += "\nGo to Groww and SELL now.\n5% Hunter Bot"
        send_whatsapp(msg)


# ============================================================
# MAIN SCAN
# ============================================================
def run_scan():
    now     = datetime.now(IST)
    now_str = now.strftime("%I:%M %p | %d %b")
    print("\n" + "=" * 50)
    print("SCANNING " + str(len(STOCKS)) + " STOCKS — " + now_str)
    print("=" * 50)

    monitor_positions()

    results = []
    for symbol in STOCKS:
        clean = symbol.replace(".NS", "")
        if clean in alerted_today:
            continue
        print("  " + clean + "...", end=" ")
        result = score_stock(symbol)
        if result and result["score"] >= MIN_SCORE:
            results.append(result)
            print("SCORE " + str(result["score"]))
        else:
            print(str(result["score"]) if result else "skip")

    results.sort(key=lambda x: x["score"], reverse=True)
    top = results[:8]

    if not top:
        print("\nNo stocks above " + str(MIN_SCORE) + " this scan.")
        return

    msg  = "SCAN | " + now_str + " | " + str(len(top)) + " stocks\n"
    msg += "=" * 28 + "\n\n"

    for i, r in enumerate(top):
        msg += "#" + str(i + 1) + " " + r["symbol"] + " | " + str(r["score"]) + "/100\n"
        msg += "E:Rs." + str(r["entry"]) + " T:Rs." + str(r["target"]) + " SL:Rs." + str(r["stop"]) + "\n"
        msg += "Invest:Rs." + str(r["invested"]) + " Net:Rs." + str(r["net_profit"]) + "(" + str(r["net_pct"]) + "%)\n"
        msg += ", ".join(r["reasons"][:2]) + "\n\n"

    msg += "5% Hunter Bot"
    send_whatsapp(msg)

    for r in top:
        alerted_today.add(r["symbol"])

    print("\nDone! Alert sent for " + str(len(top)) + " stocks.")


# ============================================================
# DAILY RESET
# ============================================================
def reset_daily():
    alerted_today.clear()
    print("Daily alert list reset.")


# ============================================================
# SCHEDULER
# ============================================================
def start_scheduler():
    print("5% HUNTER BOT — LIVE")
    print("Scanning hourly 10am-3pm IST Mon-Fri")

    for day in ["monday", "tuesday", "wednesday", "thursday", "friday"]:
        getattr(schedule.every(), day).at("10:00").do(run_scan)
        getattr(schedule.every(), day).at("11:00").do(run_scan)
        getattr(schedule.every(), day).at("12:00").do(run_scan)
        getattr(schedule.every(), day).at("13:00").do(run_scan)
        getattr(schedule.every(), day).at("14:00").do(run_scan)
        getattr(schedule.every(), day).at("15:00").do(run_scan)

    schedule.every().day.at("00:01").do(reset_daily)

    while True:
        schedule.run_pending()
        time.sleep(30)


# ============================================================
# ENTRY POINT
# ============================================================
if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "scan":
        run_scan()
    else:
        start_scheduler()
