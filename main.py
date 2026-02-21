INTERVAL = "1"
ROUNDING = 5
FALLBACK = 0.90

START_BALANCE = 100

RISK_NORMAL = 0.1
RISK_RECOVERY = 0.1

TP_NORMAL = 0.004
TP_RECOVERY = 0.004

SL_PCT = 0.005
QTY_SL_DIST_PCT = 0.006

EMA_LOOKBACK = 200
MIN_SL_DIST = 0.002      # 0.2%
RR = 2.0
TP_BUFFER = 0.001        # +0.1%

DAILY_RISK_PCT = 0.1   # 5% of balance used for all trades today

import pandas as pd

df = pd.read_csv("data.csv")

balance = START_BALANCE

buy_trade = None
sell_trade = None

buy_fvg = None
sell_fvg = None

def log(msg):
    print(msg)

from datetime import datetime

current_day = None
daily_start_balance = None
daily_risk_amount = None

for i in range(4,len(df)):

    o = df.open[i]
    h = df.high[i]
    l = df.low[i]
    c = df.close[i]
    t = df.time[i]
    candle_day = pd.to_datetime(t).date()
    # NEW DAY DETECTED
    if candle_day != current_day:
        current_day = candle_day
        daily_start_balance = balance
        daily_risk_amount = balance * DAILY_RISK_PCT
        
        log("NEW DAY STARTED")
        log(f"DAILY START BALANCE: {daily_start_balance}")
        log(f"DAILY RISK LOCKED: {daily_risk_amount}")
    log(f"\n==== {t} ====")
    log(f"O:{o} H:{h} L:{l} C:{c}")

    # -------------------------
    # NON-REPAINT FVG DETECTION
    # -------------------------
    bullFVG = df.low[i-1] > df.high[i-3]
    bearFVG = df.high[i-1] < df.low[i-3]

    # BUY FVG
    if bullFVG:
        new_low  = df.high[i-3]
        new_high = df.low[i-1]

        if buy_trade is None:
            buy_fvg = {
                "low": new_low,
                "high": new_high,
                "tapped": False
            }
            log("NEW BUY FVG")

        elif buy_trade:
            if new_low > buy_trade["sl"]:
                buy_trade["sl"] = new_low
                risk = buy_trade["entry"] - buy_trade["sl"]
                buy_trade["tp"] = buy_trade["entry"] + (risk*RR) + (buy_trade["entry"]*TP_BUFFER)
                log("BUY SL UPDATED")

    # SELL FVG
    if bearFVG:
        new_high = df.low[i-3]
        new_low  = df.high[i-1]

        if sell_trade is None:
            sell_fvg = {
                "high": new_high,
                "low": new_low,
                "tapped": False
            }
            log("NEW SELL FVG")

        elif sell_trade:
            if new_high < sell_trade["sl"]:
                sell_trade["sl"] = new_high
                risk = sell_trade["sl"] - sell_trade["entry"]
                sell_trade["tp"] = sell_trade["entry"] - (risk*RR) - (sell_trade["entry"]*TP_BUFFER)
                log("SELL SL UPDATED")

    # -------------------------
    # TAP CHECK
    # -------------------------
    if buy_fvg and not buy_fvg["tapped"]:
        if l <= buy_fvg["high"] and h >= buy_fvg["low"]:
            buy_fvg["tapped"] = True
            log("BUY TAP")

    if sell_fvg and not sell_fvg["tapped"]:
        if l <= sell_fvg["high"] and h >= sell_fvg["low"]:
            sell_fvg["tapped"] = True
            log("SELL TAP")

    # -------------------------
    # CONFIRMATION
    # -------------------------
    if buy_fvg and buy_fvg["tapped"] and buy_trade is None:
        if c > buy_fvg["high"]:

            entry = c
            sl = buy_fvg["low"]
            sl_pct = (entry-sl)/entry

            if sl_pct >= MIN_SL_DIST:

                risk_amt = daily_risk_amount
                risk = entry-sl
                tp = entry + (risk*RR) + (entry*TP_BUFFER)

                buy_trade = {
                    "entry":entry,
                    "sl":sl,
                    "tp":tp,
                    "risk_amt":risk_amt
                }

                log("BUY OPEN")

    if sell_fvg and sell_fvg["tapped"] and sell_trade is None:
        if c < sell_fvg["low"]:

            entry = c
            sl = sell_fvg["high"]
            sl_pct = (sl-entry)/entry

            if sl_pct >= MIN_SL_DIST:

                risk_amt = daily_risk_amount
                risk = sl-entry
                tp = entry - (risk*RR) - (entry*TP_BUFFER)

                sell_trade = {
                    "entry":entry,
                    "sl":sl,
                    "tp":tp,
                    "risk_amt":risk_amt
                }

                log("SELL OPEN")

    # -------------------------
    # MONITOR BUY
    # -------------------------
    if buy_trade:
        if l <= buy_trade["sl"]:
            balance -= buy_trade["risk_amt"]
            log("BUY SL HIT")
            log(f"BALANCE: {balance}")
            buy_trade=None

        elif h >= buy_trade["tp"]:
            balance += (buy_trade["risk_amt"]*2)
            log("BUY TP HIT")
            log(f"BALANCE: {balance}")
            buy_trade=None

    # -------------------------
    # MONITOR SELL
    # -------------------------
    if sell_trade:
        if h >= sell_trade["sl"]:
            balance -= sell_trade["risk_amt"]
            log("SELL SL HIT")
            log(f"BALANCE: {balance}")
            sell_trade=None

        elif l <= sell_trade["tp"]:
            balance += (sell_trade["risk_amt"]*2)
            log("SELL TP HIT")
            log(f"BALANCE: {balance}")
            sell_trade=None
