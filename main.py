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
    # Example: multiple trading pairs & leverages
    {"symbol": "BTCUSDT", "leverage": 100},
    {"symbol": "ETHUSDT", "leverage": 100},
    # Add more pairs as needed
]

INTERVAL = "5"            # timeframe in minutes (string) e.g. "1", "5", "60", "240"
CANDLE_LIMIT = 6          # how many klines to request (>=5 recommended)
LOG_LEVEL = logging.INFO

# Money / risk parameters
START_BALANCE = 100.0            # starting paper balance (USD)
DAILY_RISK_PCT = 0.1             # percent of balance to lock as RF at day start (e.g. 0.01 = 1%)
RR = 2.0                          # reward multiple (2R)
MIN_SL_PCT = 0.0005              # minimum SL percent distance required (0.2%)
TP_BUFFER = 0.001                 # additional buffer on TP (kept 0.0 because you requested TP fixed)
# (You can set TP_BUFFER to 0.001 to add +0.1% if wanted in future)

# Bybit API (read-only for kline)
API_KEY = os.getenv("BYBIT_API_KEY", "")
API_SECRET = os.getenv("BYBIT_API_SECRET", "")
TESTNET = True   # set True if you want to use Bybit testnet (still paper mode)
# ===========================
# REAL TRADING SETTINGS
# ===========================
USE_REAL_TRADING = True       # False = paper, True = real orders
ACCOUNT_TYPE = "UNIFIED"      # For Bybit UTA
CATEGORY = "linear"           # USDT Perps
RF_PERCENT = 0.1             # 1% of balance locked per day

# ===========================
# LOGGING & STATE
# ===========================
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s | %(message)s")
logger = logging.getLogger("paper_fvg_bot")

# Global paper balance and daily locked RF
balance = float(START_BALANCE)
daily_rf = 0.0           # will be set at start of UTC day = balance * DAILY_RISK_PCT
current_day = None       # UTC date used to detect new day

# Connection (pybit)
session = HTTP(testnet=TESTNET, api_key=API_KEY, api_secret=API_SECRET)

# Per-symbol state
symbol_state = {}
for p in PAIRS:
    symbol_state[p["symbol"]] = {
        "buy_fvg": None,          # dict: {"low":..., "high":..., "tapped":bool, "created_at":timestamp}
        "sell_fvg": None,         # dict: {"high":..., "low":..., "tapped":bool, "created_at":timestamp}
        "buy_trade": None,        # dict if trade open else None: {"side":"BUY", "entry":, "sl":, "tp":, "opened_at":timestamp}
        "sell_trade": None,       # same for SELL
        "last_candle_time": 0     # track last processed closed candle (ms)
    }


# ===========================
# HELPERS
# ===========================
def now_ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def fetch_candles(symbol, interval=INTERVAL, limit=CANDLE_LIMIT):
    """
    Fetch klines from Bybit and return in chronological order (oldest -> newest)
    Each item is dict: {"time":ms, "o":, "h":, "l":, "c":}
    """
    try:
        resp = session.get_kline(category="linear", symbol=symbol, interval=interval, limit=limit)
        # expected resp["result"]["list"] where each c = [open_time, open, high, low, close, ...]
        raw = resp["result"]["list"]
        # reverse to chronological (oldest first) because Bybit returns newest first
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

def position_exists(symbol):
    try:
        resp = session.get_positions(category=CATEGORY, symbol=symbol)
        positions = resp["result"]["list"]

        for p in positions:
            if float(p["size"]) > 0:
                return True

    except Exception as e:
        logger.error(f"{symbol} | Error checking positions: {e}")

    return False

def lock_daily_rf_if_needed():
    global current_day, daily_rf

    utc_today = datetime.now(timezone.utc).date()

    if utc_today != current_day:
        current_day = utc_today

        real_balance = get_real_balance()
        daily_rf = round(real_balance * RF_PERCENT, 6)

        logger.info("===========================================")
        logger.info(f"NEW UTC DAY: {current_day}")
        logger.info(f"ACCOUNT BALANCE: ${real_balance:.4f}")
        logger.info(f"LOCKED DAILY RF: ${daily_rf:.4f}")
        logger.info("===========================================")
def log_candles(symbol, candles):
    """
    Log every retrieved candle (all fetched candles).
    """
    logger.info(f"{symbol} | Retrieved {len(candles)} candles (oldest -> newest).")
    for c in candles:
        t = datetime.utcfromtimestamp(c["time"] / 1000).strftime("%Y-%m-%d %H:%M:%S")
        logger.info(f"{symbol} | {t} | O:{c['open']} H:{c['high']} L:{c['low']} C:{c['close']}")


def simulate_and_resolve_trade(symbol, side, entry_index, entry, sl, tp, candles):
    """
    Simulate forward from entry_index+1 comparing high/low per candle.
    Conservative: SL hit takes precedence if both in same candle.
    Returns: ("WIN" or "LOSS" or "OPEN", exit_index)
    """
    for j in range(entry_index + 1, len(candles)):
        high = candles[j]["high"]
        low = candles[j]["low"]

        if side == "BUY":
            # SL first (conservative)
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
        session.set_leverage(
            category=CATEGORY,
            symbol=symbol,
            buyLeverage=str(leverage),
            sellLeverage=str(leverage)
        )
        logger.info(f"{symbol} | Leverage set to {leverage}x")
    except Exception as e:
        logger.warning(f"{symbol} | Leverage may already be set: {e}")

# ===========================
# CORE SYMBOL HANDLER
# ===========================
def handle_symbol(pair):
    """
    Process one pair: fetch candles, log, detect FVGs, manage taps, simulate opens and track active simulated trades.
    pair: {'symbol':..., 'leverage': ...}
    """
    global balance, daily_rf

    symbol = pair["symbol"]
    leverage = pair.get("leverage", 1)
    state = symbol_state[symbol]

    set_leverage(symbol, leverage)
    
    candles = fetch_candles(symbol, interval=INTERVAL, limit=CANDLE_LIMIT)
    if len(candles) < 5:
        logger.warning(f"{symbol} | Not enough candles fetched ({len(candles)}). Skipping this cycle.")
        return

    # Log every candle retrieved (oldest -> newest)
    log_candles(symbol, candles)

    # Determine non-repaint last_closed, prev1, prev2
    # candles are chronological: candles[-1] = newest (still forming), candles[-2] = last closed
    now_utc = datetime.now(timezone.utc) 
    current_candle_open = int(now_utc.timestamp() // (int(INTERVAL)*60)) * (int(INTERVAL)*60) * 1000  # Filter only candles that are strictly closed 
    closed_candles = [c for c in candles if c["time"] < current_candle_open]  
    if len(closed_candles) < 3:     
        logger.warning(f"{symbol} | Not enough strictly closed candles.")     
        return  
    last_closed = closed_candles[-1] 
    prev1 = closed_candles[-1] 
    prev2 = closed_candles[-3]
    logger.info(f"{symbol} | prev2 H:{prev2['high']} L:{prev2['low']} | prev1 H:{prev1['high']} L:{prev1['low']}")

    # Skip if we've already processed this same closed candle
    if last_closed["time"] == state["last_candle_time"]:
        # already processed
        return
    state["last_candle_time"] = last_closed["time"]

    # Every retrieval log the last_closed explicit
    t_last = datetime.utcfromtimestamp(last_closed["time"] / 1000).strftime("%Y-%m-%d %H:%M:%S")
    logger.info(f"{symbol} | Processing closed candle {t_last} | C={last_closed['close']}")

    # LOCK daily RF if new UTC day
    # (global RF locked once per UTC day)
    # call lock_daily_rf_if_needed outside per-symbol loop or here as safeguard
    # (we call it at top-level before processing pairs every cycle, but keep here defensive)
    # lock_daily_rf_if_needed()  # top-level already sets daily_rf

    # -----------------------
    # FVG DETECTION (non-repaint)
    # bullFVG: prev1.low > prev2.high
    # bearFVG: prev1.high < prev2.low
    # -----------------------
    bull_fvg = prev1["low"] > prev2["high"]
    bear_fvg = prev1["high"] < prev2["low"]

    # BUY FVG creation/replacement OR SL-update when trade active
    if bull_fvg:
        new_low = prev2["high"]    # fvg low boundary
        new_high = prev1["low"]    # fvg high boundary
        created_at = prev1["time"]
        logger.info(f"{symbol} | New BUY FVG detected: low={new_low} high={new_high}")

        # If no active buy trade -> replace idle buy_fvg with newest
        if state["buy_trade"] is None:
            state["buy_fvg"] = {"low": new_low, "high": new_high, "tapped": False, "created_at": created_at}
            logger.info(f"{symbol} | BUY FVG registered as active watcher (no active buy trade).")
        else:
            # If buy trade active -> possibly tighten SL (only SL update)
            bt = state["buy_trade"]
            if bt is not None:
                # favorable if new_low > current SL (tighten upward)
                if new_low > bt["sl"]:
                    old_sl = bt["sl"]
                    bt["sl"] = new_low
                    logger.info(f"{symbol} | BUY trade SL tightened from {old_sl} -> {bt['sl']} due to new BUY FVG")
                else:
                    logger.info(f"{symbol} | BUY trade open; new BUY FVG not favorable for SL (new_low={new_low} <= sl={bt['sl']})")

    # SELL FVG creation/replacement OR SL-update when trade active
    if bear_fvg:
        new_high = prev2["low"]   # fvg high boundary
        new_low = prev1["high"]   # fvg low boundary
        created_at = prev1["time"]
        logger.info(f"{symbol} | New SELL FVG detected: high={new_high} low={new_low}")

        if state["sell_trade"] is None:
            state["sell_fvg"] = {"high": new_high, "low": new_low, "tapped": False, "created_at": created_at}
            logger.info(f"{symbol} | SELL FVG registered as active watcher (no active sell trade).")
        else:
            st = state["sell_trade"]
            if st is not None:
                # favorable if new_high < current SL (tighten downward)
                if new_high < st["sl"]:
                    old_sl = st["sl"]
                    st["sl"] = new_high
                    logger.info(f"{symbol} | SELL trade SL tightened from {old_sl} -> {st['sl']} due to new SELL FVG")
                else:
                    logger.info(f"{symbol} | SELL trade open; new SELL FVG not favorable for SL (new_high={new_high} >= sl={st['sl']})")

    # -----------------------
    # TAP CHECK (entering the FVG range)
    # "enter" defined as candle range touching or overlapping FVG (wick in is enough)
    # -----------------------
    # Use last_closed for tap check (we are at closed candle)
    if state["buy_fvg"] and not state["buy_fvg"]["tapped"]:
        bf = state["buy_fvg"]
        if last_closed["low"] <= bf["high"] and last_closed["high"] >= bf["low"]:
            bf["tapped"] = True
            logger.info(f"{symbol} | BUY FVG TAPPED (candle touched the FVG range).")

    if state["sell_fvg"] and not state["sell_fvg"]["tapped"]:
        sf = state["sell_fvg"]
        if last_closed["low"] <= sf["high"] and last_closed["high"] >= sf["low"]:
            sf["tapped"] = True
            logger.info(f"{symbol} | SELL FVG TAPPED (candle touched the FVG range).")

    # -----------------------
    # CONFIRMATION & OPEN SIMULATED TRADE
    # Only open if:
    #   - buy_fvg exists and tapped and no buy trade open and close > fvg_high (confirm)
    #   - sell_fvg exists and tapped and no sell trade open and close < fvg_low (confirm)
    # Also require minimum SL distance percent (MIN_SL_PCT)
    # When opening: calculate entry, sl, tp (TP fixed at entry + (risk*RR) ; risk distance = entry - sl)
    # Use daily_rf (locked for the day) as the risk amount per trade in dollars
    # -----------------------
    # BUY confirmation
    if state["buy_fvg"] and state["buy_fvg"]["tapped"] and state["buy_trade"] is None:
        bf = state["buy_fvg"]
        if last_closed["close"] > bf["high"]:
            entry = last_closed["close"]
            sl = bf["low"]
            sl_pct = (entry - sl) / entry
            if sl_pct >= MIN_SL_PCT:
                tp = entry + (entry - sl) * RR + (entry * TP_BUFFER)
                logger.info(f"{symbol} | BUY CONFIRMED | entry={entry} sl={sl} tp={tp}")
                if USE_REAL_TRADING:
                    place_real_trade(symbol, "BUY", entry, sl, tp, leverage)
                else:
                    logger.info(f"{symbol} | BUY CONFIRMED (paper only)")
            else:
                logger.info(f"{symbol} | BUY confirmation ignored: SL too tight")
    # SELL confirmation
    if state["sell_fvg"] and state["sell_fvg"]["tapped"] and state["sell_trade"] is None:
        sf = state["sell_fvg"]
        if last_closed["close"] < sf["low"]:
            entry = last_closed["close"]
            sl = sf["high"]
            sl_pct = (sl - entry) / entry
            if sl_pct >= MIN_SL_PCT:
                tp = entry - (sl - entry) * RR - (entry * TP_BUFFER)
                logger.info(f"{symbol} | SELL CONFIRMED | entry={entry} sl={sl} tp={tp}")
                if USE_REAL_TRADING:
                    place_real_trade(symbol, "SELL", entry, sl, tp, leverage)
                else:
                    logger.info(f"{symbol} | SELL CONFIRMED (paper only)")
            else:
                logger.info(f"{symbol} | SELL confirmation ignored: SL too tight")
    # -----------------------
    # MONITOR OPEN TRADES (do NOT open a new trade on the same side if one is already open)
    # If a trade exists, check last_closed candle for TP / SL hit (SL checked first). Both buy and sell monitored independently.
    # When resolved, update balance by  +2*daily_rf on win, -1*daily_rf on loss, log it and clear the trade.
    # -----------------------
    # BUY trade monitoring
    if state["buy_trade"] is not None:
        bt = state["buy_trade"]
        # Check SL first (conservative)
        if last_closed["low"] <= bt["sl"]:
            balance -= daily_rf
            logger.info(f"{symbol} | BUY SL HIT (paper) | -${daily_rf:.4f} | New Balance=${balance:.4f}")
            state["buy_trade"] = None
            # after closing a buy, do NOT automatically replace buy_fvg; next new FVG will register normally
        elif last_closed["high"] >= bt["tp"]:
            balance += 2 * daily_rf
            logger.info(f"{symbol} | BUY TP HIT (paper) | +${2 * daily_rf:.4f} | New Balance=${balance:.4f}")
            state["buy_trade"] = None

    # SELL trade monitoring
    if state["sell_trade"] is not None:
        st = state["sell_trade"]
        if last_closed["high"] >= st["sl"]:
            balance -= daily_rf
            logger.info(f"{symbol} | SELL SL HIT (paper) | -${daily_rf:.4f} | New Balance=${balance:.4f}")
            state["sell_trade"] = None
        elif last_closed["low"] <= st["tp"]:
            balance += 2 * daily_rf
            logger.info(f"{symbol} | SELL TP HIT (paper) | +${2 * daily_rf:.4f} | New Balance=${balance:.4f}")
            state["sell_trade"] = None

    
def place_real_trade(symbol, side, entry, sl, tp, leverage):

    if position_exists(symbol):
        logger.info(f"{symbol} | Position already exists. Skipping new entry.")
        return

    try:
        sl_distance = abs(entry - sl)

        if sl_distance == 0:
            logger.warning(f"{symbol} | SL distance zero. Aborting trade.")
            return

        qty = daily_rf / sl_distance
        qty = qty * leverage
        qty = round(qty, 3)

        current_balance = get_real_balance()
        logger.info(f"{symbol} | Current Balance Before Trade: ${current_balance:.4f}")
        
        logger.info(f"{symbol} | Placing REAL {side} order | Qty={qty}")

        session.place_order(
            category=CATEGORY,
            symbol=symbol,
            side="Buy" if side == "BUY" else "Sell",
            orderType="Market",
            qty=str(qty),
            timeInForce="IOC"
        )

        session.set_trading_stop(
            category=CATEGORY,
            symbol=symbol,
            takeProfit=str(tp),
            stopLoss=str(sl)
        )

        logger.info(f"{symbol} | Order placed successfully | TP={tp} SL={sl}")

    except Exception as e:
        logger.error(f"{symbol} | Error placing order: {e}")

# ===========================
# MAIN LOOP
# ===========================
def main():
    global balance, daily_rf

    logger.info("LIVE PAPER FVG BOT (simulation) STARTED")
    real_balance = get_real_balance()
    logger.info(f"STARTUP BALANCE = ${real_balance:.4f}")
    # Lock initial daily RF for current UTC day
    lock_daily_rf_if_needed()

    try:
        while True:
            # wait until next candle close (UTC)
            wait = seconds_until_next_candle(INTERVAL)
            logger.info(f"Waiting {wait}s for next {INTERVAL}m candle close (UTC)...")
            time.sleep(wait + 0.8)  # small offset to ensure candle is closed on exchange

            # Lock per-day RF at start of UTC day if needed (one global RF for all pairs)
            lock_daily_rf_if_needed()

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
