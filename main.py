#!/usr/bin/env python3
"""
LIVE PAPER FVG BOT (simulation only)

Requirements:
    pip install pybit pandas
Environment:
    Set BYBIT API_KEY and BYBIT_API_SECRET in environment if you want to use Bybit (read-only for kline).
Notes:
    - This is a paper/simulation bot: it does NOT place real orders.
    - RF (risk factor in $) is locked once per UTC day and used unchanged for all trades that day.
    - SL updates only (TP remains fixed at entry calculation).
"""

import os
import time
import logging
from datetime import datetime, timezone
from pybit.unified_trading import HTTP
import pandas as pd

# ===========================
# CONFIG (CHANGE AS NEEDED)
# ===========================
PAIRS = [
    {"symbol": "BTCUSDT", "leverage": 100}
]

INTERVAL = "30"
CANDLE_LIMIT = 6
LOG_LEVEL = logging.INFO

START_BALANCE = 100.0
DAILY_RISK_PCT = 0.05
RR = 1.0
MIN_SL_PCT = 0.001
TP_BUFFER = 0.001
SL_BUFFER = 0.001

API_KEY = os.getenv("BYBIT_API_KEY", "")
API_SECRET = os.getenv("BYBIT_API_SECRET", "")
TESTNET = False

USE_REAL_TRADING = True
ACCOUNT_TYPE = "UNIFIED"
CATEGORY = "linear"
RF_PERCENT = 0.05

# ===========================
# LOGGING & STATE
# ===========================
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s | %(message)s")
logger = logging.getLogger("paper_fvg_bot")

balance = float(START_BALANCE)
daily_rf = 0.0
current_day = None

weekly_rf = 0.0
current_week = None
siphoned_cash = 0.0

session = HTTP(testnet=TESTNET, api_key=API_KEY, api_secret=API_SECRET)

symbol_state = {}
for p in PAIRS:
    symbol_state[p["symbol"]] = {
        "buy_fvg": None,
        "sell_fvg": None,
        "buy_trade": None,
        "sell_trade": None,
        "last_candle_time": 0,
        "buy_fvg_candle_time": None,
        "sell_fvg_candle_time": None,
    }

# ===========================
# HELPERS
# ===========================
def now_ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def fetch_candles(symbol, interval=INTERVAL, limit=CANDLE_LIMIT):
    try:
        resp = session.get_kline(category="linear", symbol=symbol, interval=interval, limit=limit)
        raw = resp["result"]["list"]
        candles = list(reversed(raw))
        parsed = []
        for c in candles:
            parsed.append({
                "time": int(c[0]),
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4])
            })
        return parsed
    except Exception as e:
        logger.error(f"{symbol} | Error fetching candles: {e}")
        return []


def seconds_until_next_candle(interval_minutes):
    now = datetime.now(timezone.utc)
    sec = int(interval_minutes) * 60
    seconds_into_cycle = (now.minute * 60 + now.second) % sec
    wait = sec - seconds_into_cycle
    if wait <= 0:
        wait += sec
    return wait


def get_real_balance():
    try:
        resp = session.get_wallet_balance(accountType=ACCOUNT_TYPE)
        coins = resp["result"]["list"][0]["coin"]
        for c in coins:
            if c["coin"] == "USDT":
                return float(c["walletBalance"])
    except Exception as e:
        logger.error(f"Error fetching balance: {e}")
    return 0.0


def position_exists(symbol, side):
    response = session.get_positions(
        category="linear",
        symbol=symbol
    )

    positions = response["result"]["list"]

    for pos in positions:
        size = float(pos["size"])
        position_side = pos["side"]  # "Buy" or "Sell"

        if size > 0 and position_side == side:
            return True

    return False

def lock_weekly_rf_if_needed():
    global current_week, weekly_rf, siphoned_cash

    now = datetime.now(timezone.utc)
    week = (now.year, now.isocalendar()[1])

    if week != current_week:
        current_week = week

        real_balance = get_real_balance()

        # siphon 25%
        siphon_amount = real_balance * 0.25
        siphoned_cash += siphon_amount

        effective_balance = real_balance * 0.75

        weekly_rf = round(effective_balance * RF_PERCENT, 6)

        logger.info("===========================================")
        logger.info(f"NEW WEEK LOCKED: {current_week}")
        logger.info(f"REAL BALANCE: ${real_balance:.4f}")
        logger.info(f"SIPHONED: ${siphon_amount:.4f}")
        logger.info(f"EFFECTIVE BALANCE: ${effective_balance:.4f}")
        logger.info(f"LOCKED WEEKLY RF: ${weekly_rf:.4f}")
        logger.info("===========================================")

def log_candles(symbol, candles):
    logger.info(f"{symbol} | Retrieved {len(candles)} candles (oldest -> newest).")
    for c in candles:
        t = datetime.utcfromtimestamp(c["time"] / 1000).strftime("%Y-%m-%d %H:%M:%S")
        logger.info(f"{symbol} | {t} | O:{c['open']} H:{c['high']} L:{c['low']} C:{c['close']}")


def simulate_and_resolve_trade(symbol, side, entry_index, entry, sl, tp, candles):
    for j in range(entry_index + 1, len(candles)):
        high = candles[j]["high"]
        low = candles[j]["low"]

        if side == "BUY":
            if low <= sl:
                return "LOSS", j
            if high >= tp:
                return "WIN", j
        elif side == "SELL":
            if high >= sl:
                return "LOSS", j
            if low <= tp:
                return "WIN", j
    return "OPEN", None


def set_leverage(symbol, leverage):
    try:
        session.set_leverage(category=CATEGORY, symbol=symbol,
                             buyLeverage=str(leverage), sellLeverage=str(leverage))
        logger.info(f"{symbol} | Leverage set to {leverage}x")
    except Exception as e:
        logger.warning(f"{symbol} | Leverage may already be set: {e}")


# ===========================
# CORE SYMBOL HANDLER
# ===========================
def handle_symbol(pair):
    global balance, weekly_rf

    symbol = pair["symbol"]
    leverage = pair.get("leverage", 1)
    state = symbol_state[symbol]

    set_leverage(symbol, leverage)
    
    candles = fetch_candles(symbol, interval=INTERVAL, limit=CANDLE_LIMIT)
    if len(candles) < 5:
        logger.warning(f"{symbol} | Not enough candles fetched ({len(candles)}). Skipping this cycle.")
        return

    log_candles(symbol, candles)

    now_utc = datetime.now(timezone.utc)
    current_candle_open = int(now_utc.timestamp() // (int(INTERVAL) * 60)) * (int(INTERVAL) * 60) * 1000
    closed_candles = [c for c in candles if c["time"] < current_candle_open]
    if len(closed_candles) < 3:
        logger.warning(f"{symbol} | Not enough strictly closed candles.")
        return

    last_closed = closed_candles[-1]
    prev1 = closed_candles[-1]
    prev2 = closed_candles[-3]
    logger.info(f"{symbol} | prev2 H:{prev2['high']} L:{prev2['low']} | prev1 H:{prev1['high']} L:{prev1['low']}")

    if last_closed["time"] == state["last_candle_time"]:
        return
    state["last_candle_time"] = last_closed["time"]

    t_last = datetime.utcfromtimestamp(last_closed["time"] / 1000).strftime("%Y-%m-%d %H:%M:%S")
    logger.info(f"{symbol} | Processing closed candle {t_last} | C={last_closed['close']}")

    bull_fvg = prev1["low"] > prev2["high"]
    bear_fvg = prev1["high"] < prev2["low"]

    # -----------------------
    # BUY FVG
    # -----------------------
    if bull_fvg:
        new_low = prev2["high"]
        new_high = prev1["low"]
        created_at = prev1["time"]
        logger.info(f"{symbol} | New BUY FVG detected: low={new_low} high={new_high}")

        state["buy_fvg_candle_time"] = prev1["time"]
        if state["buy_trade"] is None:
            state["buy_fvg"] = {"low": new_low, "high": new_high, "tapped": False, "created_at": created_at}
            logger.info(f"{symbol} | BUY FVG registered as active watcher (no active buy trade).")
        else:
            bt = state["buy_trade"]
            if new_low > bt["sl"]:
                old_sl = bt["sl"]
                bt["sl"] = new_low
                logger.info(f"{symbol} | BUY trade SL tightened from {old_sl} -> {bt['sl']} due to new BUY FVG")
            else:
                logger.info(f"{symbol} | BUY trade open; new BUY FVG not favorable for SL (new_low={new_low} <= sl={bt['sl']})")

    # -----------------------
    # SELL FVG
    # -----------------------
    if bear_fvg:
        new_high = prev2["low"]
        new_low = prev1["high"]
        created_at = prev1["time"]
        logger.info(f"{symbol} | New SELL FVG detected: high={new_high} low={new_low}")

        state["sell_fvg_candle_time"] = prev1["time"]
        if state["sell_trade"] is None:
            state["sell_fvg"] = {"high": new_high, "low": new_low, "tapped": False, "created_at": created_at}
            logger.info(f"{symbol} | SELL FVG registered as active watcher (no active sell trade).")
        else:
            st = state["sell_trade"]
            if new_high < st["sl"]:
                old_sl = st["sl"]
                st["sl"] = new_high
                logger.info(f"{symbol} | SELL trade SL tightened from {old_sl} -> {st['sl']} due to new SELL FVG")
            else:
                logger.info(f"{symbol} | SELL trade open; new SELL FVG not favorable for SL (new_high={new_high} >= sl={st['sl']})")

    # -----------------------
    # TAP CHECK
    # -----------------------
    if state["buy_fvg"] and not state["buy_fvg"]["tapped"]:
        bf = state["buy_fvg"]
        if last_closed["time"] != state["buy_fvg_candle_time"]:
            if last_closed["low"] <= bf["high"] and last_closed["high"] >= bf["low"]:
                bf["tapped"] = True
                logger.info(f"{symbol} | BUY FVG TAPPED (candle touched the FVG range).")

    if state["sell_fvg"] and not state["sell_fvg"]["tapped"]:
        sf = state["sell_fvg"]
        if last_closed["time"] != state["sell_fvg_candle_time"]:
            if last_closed["low"] <= sf["high"] and last_closed["high"] >= sf["low"]:
                sf["tapped"] = True
                logger.info(f"{symbol} | SELL FVG TAPPED (candle touched the FVG range).")

    # -----------------------
    # FVG INVALIDATION
    # -----------------------
    if state["buy_fvg"]:
        bf = state["buy_fvg"]
        if last_closed["close"] < bf["low"]:
            logger.info(f"{symbol} | BUY FVG INVALIDATED (closed below {bf['low']})")
            state["buy_fvg"] = None

    if state["sell_fvg"]:
        sf = state["sell_fvg"]
        if last_closed["close"] > sf["high"]:
            logger.info(f"{symbol} | SELL FVG INVALIDATED (closed above {sf['high']})")
            state["sell_fvg"] = None

    # -----------------------
    # CONFIRMATION (OPEN PAPER/REAL TRADE)
    # -----------------------
    # BUY confirmation
    if state["buy_fvg"] and state["buy_fvg"]["tapped"] and state["buy_trade"] is None and last_closed["time"] != state["buy_fvg_candle_time"]:
        bf = state["buy_fvg"]
        if last_closed["close"] > bf["high"]:
            entry = last_closed["close"]
            sl = bf["low"] * 0.999
            state["buy_fvg"] = None
            sl_pct = (entry - sl) / entry
            if sl_pct >= MIN_SL_PCT:
                tp = entry + (entry - sl) * RR + (entry * TP_BUFFER)
                logger.info(f"{symbol} | BUY CONFIRMED | entry={entry} sl={sl} tp={tp}")
                if USE_REAL_TRADING:
                    place_real_trade(symbol, "BUY", entry, sl, tp, leverage, weekly_rf)
                else:
                    state["buy_trade"] = {"side": "BUY","entry": entry,"sl": sl,"tp": tp,"opened_at": last_closed["time"]}
                    logger.info(f"{symbol} | BUY CONFIRMED (paper only)")
            else:
                logger.info(f"{symbol} | BUY confirmation ignored: SL too tight")

    # SELL confirmation
    if state["sell_fvg"] and state["sell_fvg"]["tapped"] and state["sell_trade"] is None and last_closed["time"] != state["sell_fvg_candle_time"]:
        sf = state["sell_fvg"]
        if last_closed["close"] < sf["low"]:
            entry = last_closed["close"]
            sl = sf["high"] 
            state["sell_fvg"] = None
            sl_pct = (sl - entry) / entry
            if sl_pct >= MIN_SL_PCT:
                tp = entry - (sl - entry) * RR - (entry * TP_BUFFER)
                logger.info(f"{symbol} | SELL CONFIRMED | entry={entry} sl={sl} tp={tp}")
                if USE_REAL_TRADING:
                    place_real_trade(symbol, "SELL", entry, sl, tp, leverage, weekly_rf)
                else:
                    state["sell_trade"] = {"side": "SELL","entry": entry,"sl": sl,"tp": tp,"opened_at": last_closed["time"]}
                    logger.info(f"{symbol} | SELL CONFIRMED (paper only)")
            else:
                logger.info(f"{symbol} | SELL confirmation ignored: SL too tight")
    
def place_real_trade(symbol, side, entry, sl, tp, leverage, frozen_risk):

    side = side.upper()

    # ----------------------------
    # Determine side + positionIdx
    # ----------------------------
    if side == "BUY":
        order_side = "Buy"
        position_idx = 1
    elif side == "SELL":
        order_side = "Sell"
        position_idx = 2
    else:
        logger.error(f"{symbol} | Invalid side: {side}")
        return

    # ----------------------------
    # Check if SAME DIRECTION exists
    # ----------------------------
    if position_exists(symbol, order_side):
        logger.info(f"{symbol} | {side} position already exists. Skipping.")
        return

    # ----------------------------
    # Validate risk
    # ----------------------------
    if frozen_risk <= 0:
        logger.warning(f"{symbol} | Frozen risk is zero. Skipping trade.")
        return

    try:
        sl_distance = abs(entry - sl)

        if sl_distance <= 0:
            logger.warning(f"{symbol} | SL distance zero. Aborting.")
            return

        # Apply buffer ONLY to position sizing
        buffered_sl_distance = (
            sl_distance
            + (sl_distance * 0.001)
        )

        qty = frozen_risk / buffered_sl_distance
        qty = round(qty, 3)

        if qty <= 0:
            logger.warning(f"{symbol} | Calculated qty invalid ({qty}). Aborting.")
            return

        logger.info(f"{symbol} | Frozen Risk: ${frozen_risk:.4f}")
        logger.info(f"{symbol} | Qty Calculated: {qty}")

        # ----------------------------
        # PLACE MARKET ORDER
        # ----------------------------
        order_response = session.place_order(
            category=CATEGORY,
            symbol=symbol,
            side=order_side,
            orderType="Market",
            qty=str(qty),
            timeInForce="IOC",
            positionIdx=position_idx
        )

        logger.info(f"{symbol} | Order response: {order_response}")

        # ----------------------------
        # SET TP/SL FOR THAT SIDE ONLY
        # ----------------------------
        session.set_trading_stop(
            category=CATEGORY,
            symbol=symbol,
            takeProfit=str(tp),
            stopLoss=str(sl),
            positionIdx=position_idx
        )

        logger.info(f"{symbol} | REAL {side} ORDER PLACED | TP={tp} SL={sl}")

    except Exception as e:
        logger.error(f"{symbol} | Order error: {e}")# ===========================
# MAIN LOOP
# ===========================
def main():
    global balance, daily_rf

    logger.info("LIVE PAPER FVG BOT (simulation) STARTED")
    real_balance = get_real_balance()
    logger.info(f"STARTUP BALANCE = ${real_balance:.4f}")
    # Lock initial daily RF for current UTC day
    lock_weekly_rf_if_needed()

    try:
        while True:
            # wait until next candle close (UTC)
            wait = seconds_until_next_candle(INTERVAL)
            logger.info(f"Waiting {wait}s for next {INTERVAL}m candle close (UTC)...")
            time.sleep(wait + 0.8)  # small offset to ensure candle is closed on exchange

            # Lock per-day RF at start of UTC day if needed (one global RF for all pairs)
            lock_weekly_rf_if_needed()

            # Process each pair independently
            for p in PAIRS:
                try:
                    handle_symbol(p)
                except Exception as e:
                    logger.exception(f"{p['symbol']} | Error in handle_symbol: {e}")

            # small sleep to avoid rate-limit bursts
            time.sleep(0.2)

    except KeyboardInterrupt:
        logger.info("Stopped by user (KeyboardInterrupt). Final balance: ${:.4f}".format(balance))


if __name__ == "__main__":
    main()


