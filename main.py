# !/usr/bin/env python3

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
from datetime import datetime, timezone, timedelta
from pybit.unified_trading import HTTP
import pandas as pd
import math

# ===========================
# CONFIG (CHANGE AS NEEDED)
# ===========================
PAIRS = []
symbol_specs = {}

INTERVAL = "30"
CANDLE_LIMIT = 6
LOG_LEVEL = logging.INFO

START_BALANCE = 100.0
DAILY_RISK_PCT = 0.05
RR = 2
MIN_SL_PCT = 0.001
TP_BUFFER = 0.001
SL_BUFFER = 0.001

API_KEY = os.getenv("BYBIT_API_KEY", "")
API_SECRET = os.getenv("BYBIT_API_SECRET", "")
TESTNET = False

USE_REAL_TRADING = False
ACCOUNT_TYPE = "UNIFIED"
CATEGORY = "linear"
RF_PERCENT = 0.05

leverage_set = {}

last_daily_check = {}
daily_fvg_state = {}

for p in PAIRS:
    last_daily_check[p["symbol"]] = None
    daily_fvg_state[p["symbol"]] = {
        "allow_buy": False,
        "allow_sell": False,
        "last_new_buy_fvg": None,
        "last_new_sell_fvg": None
    }

MAX_SYMBOLS = 50          # number of pairs to scan
MAX_ACTIVE_TRADES = 10    # maximum open positions
DEFAULT_LEVERAGE = 50

last_symbol_refresh_week = None

signal_queue = []

    
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

account_cache = {
    "positions": [],
    "wallet_balance": 0.0,
    "used_margin": 0.0,
    "last_update": None
}

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

def get_symbol_specs(symbol):
    if symbol in symbol_specs:
        return symbol_specs[symbol]

    info = session.get_instruments_info(
        category="linear",
        symbol=symbol
    )

    data = info["result"]["list"][0]

    specs = {
        "qty_step": float(data["lotSizeFilter"]["qtyStep"]),
        "min_qty": float(data["lotSizeFilter"]["minOrderQty"]),
        "tick_size": float(data["priceFilter"]["tickSize"]),
        "max_leverage": float(data["leverageFilter"]["maxLeverage"])
    }

    symbol_specs[symbol] = specs
    return specs

def fetch_top_symbols():

    resp = session.get_tickers(category="linear")
    tickers = resp["result"]["list"]

    symbols = []

    for t in tickers:

        sym = t["symbol"]

        if not sym.endswith("USDT"):
            continue

        vol = float(t["turnover24h"])

        symbols.append({
            "symbol": sym,
            "volume": vol
        })

    # rank by volume
    symbols.sort(key=lambda x: x["volume"], reverse=True)

    selected = symbols[:MAX_SYMBOLS]

    pairs = []

    for s in selected:
        pairs.append({
            "symbol": s["symbol"],
            "leverage": DEFAULT_LEVERAGE
        })

    return pairs

def refresh_symbol_universe_if_needed():

    global PAIRS, symbol_state, daily_fvg_state, last_daily_check, last_symbol_refresh_week

    now = datetime.now(timezone.utc)
    week = (now.year, now.isocalendar()[1])

    if week == last_symbol_refresh_week:
        return

    logger.info("Refreshing symbol universe using 24h volume")

    new_pairs = fetch_top_symbols()

    PAIRS = new_pairs

    symbol_state.clear()
    daily_fvg_state.clear()
    last_daily_check.clear()


    for p in PAIRS:

        sym = p["symbol"]

        if sym not in symbol_state:

            symbol_state[sym] = {
                "buy_fvg": None,
                "sell_fvg": None,
                "buy_trade": None,
                "sell_trade": None,
                "last_candle_time": 0,
                "buy_fvg_candle_time": None,
                "sell_fvg_candle_time": None,
            }

            daily_fvg_state[sym] = {
                "allow_buy": False,
                "allow_sell": False,
                "last_new_buy_fvg": None,
                "last_new_sell_fvg": None
            }

            last_daily_check[sym] = None

    last_symbol_refresh_week = week

    logger.info(f"Loaded {len(PAIRS)} top symbols")



def set_symbol_leverage(symbol, desired):

    specs = get_symbol_specs(symbol)

    lev = specs["max_leverage"]
    if leverage_set.get(symbol):
        return

    try:
        session.set_leverage(
            category="linear",
            symbol=symbol,
            buyLeverage=str(lev),
            sellLeverage=str(lev)
        )

        logger.info(f"{symbol} leverage set to {lev}")
        leverage_set[symbol] = True

    except Exception as e:

        if "110043" in str(e):
            logger.info(f"{symbol} leverage already set to {lev}")
        else:
            logger.error(f"{symbol} leverage error: {e}")

def ensure_hedge_mode():

    try:

        session.switch_position_mode(
            category="linear",
            coin="USDT",
            mode=3
        )

        logger.info("Hedge mode enabled")

    except Exception as e:

        if "110025" in str(e):
            logger.info("Hedge mode already enabled")
        else:
            logger.error(f"Hedge mode error: {e}")

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

def calculate_signal_score(entry, fvg_low, fvg_high):

    fvg_size = abs(fvg_high - fvg_low)

    # normalize to price so large coins don't dominate
    size_ratio = fvg_size / entry

    # bigger gap = stronger imbalance
    score = size_ratio * 1000

    return score


def position_exists(symbol, side):

    for pos in account_cache["positions"]:

        if pos["symbol"] == symbol:
            size = float(pos["size"])
            pos_side = pos["side"]

            if size > 0 and pos_side == side:
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
        siphon_amount = real_balance * 0
        siphoned_cash += siphon_amount

        effective_balance = real_balance 

        weekly_rf = round(effective_balance * RF_PERCENT, 6)

        logger.info("===========================================")
        logger.info(f"NEW WEEK LOCKED: {current_week}")
        logger.info(f"REAL BALANCE: ${real_balance:.4f}")
        logger.info(f"SIPHONED: ${siphon_amount:.4f}")
        logger.info(f"EFFECTIVE BALANCE: ${effective_balance:.4f}")
        logger.info(f"LOCKED WEEKLY RF: ${weekly_rf:.4f}")
        logger.info("===========================================")

def refresh_account_cache():
    global account_cache

    try:
        # Positions
        pos_resp = session.get_positions(
            category="linear",
            settleCoin="USDT"
        )

        account_cache["positions"] = pos_resp["result"]["list"]

        # Wallet / margin
        wallet = session.get_wallet_balance(accountType="UNIFIED")
        data = wallet["result"]["list"][0]

        account_cache["wallet_balance"] = float(data["totalEquity"])
        account_cache["used_margin"] = float(data["totalInitialMargin"])

        account_cache["last_update"] = datetime.now(timezone.utc)

    except Exception as e:
        logger.error(f"Account cache refresh failed: {e}")
        llllllllllll
def update_daily_bias(symbol):
    global daily_fvg_state, last_daily_check

    utc_plus_1 = timezone(timedelta(hours=1))
    now = datetime.now(utc_plus_1)
    today = now.date()

    # -------------------------
    # FIRST STARTUP RUN
    # -------------------------
    if last_daily_check[symbol] is None:
        logger.info(f"{symbol} | First startup daily bias scan")
        run_daily_fvg_scan(symbol, today)
        last_daily_check[symbol] = today
        return

    # -------------------------
    # ONLY RUN AT 01:00
    # -------------------------
    if not (now.hour == 1 and now.minute < 5):
        return

    # -------------------------
    # RUN ONCE PER DAY
    # -------------------------
    if last_daily_check[symbol] == today:
        return

    logger.info(f"{symbol} | Running scheduled daily bias scan")

    run_daily_fvg_scan(symbol, today)

    last_daily_check[symbol] = today

def process_signal_queue():

    global signal_queue

    if len(signal_queue) == 0:
        return

    # sort strongest first
    signal_queue.sort(key=lambda x: x["score"], reverse=True)

    open_positions = get_total_open_positions()
    slots_left = MAX_ACTIVE_TRADES - open_positions

    if slots_left <= 0:
        signal_queue = []
        return

    selected = signal_queue[:slots_left]

    for sig in selected:

        symbol = sig["symbol"]
        side = sig["side"]
        entry = sig["entry"]
        sl = sig["sl"]
        tp = sig["tp"]
        leverage = sig["leverage"]
        qty = sig["qty"]

        place_real_trade(
            symbol,
            side,
            entry,
            sl,
            tp,
            leverage,
            weekly_rf,
            qty
        )

    signal_queue = []


def run_daily_fvg_scan(symbol, today):

    yesterday = today - timedelta(days=1)
    
    # Expire old permissions
    if daily_fvg_state[symbol]["last_new_buy_fvg"]:
        age_days = (today - daily_fvg_state[symbol]["last_new_buy_fvg"]).days
        if age_days >= 2:
            daily_fvg_state[symbol]["allow_buy"] = False
            logger.info(f"{symbol} BUY FVG expired (2 days)")

    if daily_fvg_state[symbol]["last_new_sell_fvg"]:
        age_days = (today - daily_fvg_state[symbol]["last_new_sell_fvg"]).days
        if age_days >= 2:
            daily_fvg_state[symbol]["allow_sell"] = False
            logger.info(f"{symbol} SELL FVG expired (2 days)")

    resp = session.get_kline(
        category="linear",
        symbol=symbol,
        interval="D",
        limit=6
    )

    raw = resp["result"]["list"]
    candles = list(reversed(raw))

    df = pd.DataFrame([{
        "time": int(c[0]),
        "open": float(c[1]),
        "high": float(c[2]),
        "low": float(c[3]),
        "close": float(c[4])
    } for c in candles])

    if len(df) < 4:
        return

    c1 = df.iloc[-4]
    c3 = df.iloc[-2]
    c2 = df.iloc[-5]
    c4 = df.iloc[-3]

    sell_fvg_exists = c1["low"] > c3["high"]
    buy_fvg_exists = c1["high"] < c3["low"]

    prev_day_sell_fvg_exists = c2["low"] > c4["high"]
    prev_day_buy_fvg_exists = c2["high"] < c4["low"]

    if buy_fvg_exists:
        daily_fvg_state[symbol]["allow_buy"] = True
        daily_fvg_state[symbol]["last_new_buy_fvg"] = today
        logger.info(f"{symbol} Daily BUY FVG detected")

    if sell_fvg_exists:
        daily_fvg_state[symbol]["allow_sell"] = True
        daily_fvg_state[symbol]["last_new_sell_fvg"] = today
        logger.info(f"{symbol} Daily SELL FVG detected")
        
    if prev_day_buy_fvg_exists:
        daily_fvg_state[symbol]["allow_buy"] = True
        daily_fvg_state[symbol]["last_new_buy_fvg"] = yesterday
        logger.info(f"{symbol} Previous Day Daily BUY FVG detected")

    if prev_day_sell_fvg_exists:
        daily_fvg_state[symbol]["allow_sell"] = True
        daily_fvg_state[symbol]["last_new_sell_fvg"] = yesterday
        logger.info(f"{symbol} Previous Day Daily SELL FVG detected")
            
def log_candles(symbol, candles):
    logger.info(f"{symbol} | Retrieved {len(candles)} candles (oldest -> newest).")
    for c in candles:
        t = datetime.utcfromtimestamp(c["time"] / 1000).strftime("%Y-%m-%d %H:%M:%S")
        logger.info(f"{symbol} | {t} | O:{c['open']} H:{c['high']} L:{c['low']} C:{c['close']}")

def round_qty(symbol, qty):

    specs = get_symbol_specs(symbol)

    step = specs["qty_step"]
    min_qty = specs["min_qty"]

    qty = max(round(qty / step) * step, min_qty)

    return qty    

def get_margin_usage():
    total = account_cache["wallet_balance"]
    used = account_cache["used_margin"]
    return total, used
    
def margin_available_for_trade(required_margin):

    total, used = get_margin_usage()

    max_allowed = total * 0.8

    if used + required_margin > max_allowed:

        remaining = max_allowed - used

        if remaining <= 0:
            return False, 0

        return True, remaining

    return True, required_margin

def calculate_margin_required(price, qty, leverage):

    position_value = price * qty

    margin = position_value / leverage

    return margin

def trade_value_ok(price, qty):

    return price * qty >= 5

def fit_qty_to_margin(symbol, price, leverage, desired_qty):

    margin_needed = calculate_margin_required(price, desired_qty, leverage)

    ok, allowed_margin = margin_available_for_trade(margin_needed)

    if ok:
        return desired_qty

    # reduce qty to fit remaining margin

    new_position_value = allowed_margin * leverage
    new_qty = new_position_value / price

    new_qty = round_qty(symbol, new_qty)

    if not trade_value_ok(price, new_qty):
        return None

    return new_qty

def get_total_open_positions():

    count = 0

    for pos in account_cache["positions"]:
        if float(pos["size"]) > 0:
            count += 1

    return count
    
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

def calculate_liquidation_price(entry, qty, side, leverage, available_balance):
    """
    Approximate liquidation price for cross + hedge mode
    """

    position_value = entry * qty
    im = 1 / leverage  # initial margin rate
    mmr = 0.005        # your 0.5% maintenance margin

    extra_margin_ratio = available_balance / position_value

    effective_buffer = im + extra_margin_ratio - mmr

    if side == "Buy":  # LONG
        lp = entry * (1 - effective_buffer)
    else:  # SHORT
        lp = entry * (1 + effective_buffer)

    return lp

# ===========================
# CORE SYMBOL HANDLER
# ===========================
def handle_symbol(pair):
    global balance, weekly_rf

    symbol = pair["symbol"]
    leverage = pair.get("leverage", 1)
    state = symbol_state[symbol]
    
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
        mid = new_low + (new_high - new_low) * 0.5
        created_at = prev1["time"]
        state["buy_fvg"] = {
            "low": new_low,
            "high": new_high,
            "mid": mid,
            "tapped": False,
            "deepest_touch": None,
            "created_at": created_at}
        logger.info(f"{symbol} | New BUY FVG detected: low={new_low} high={new_high}")

        state["buy_fvg_candle_time"] = prev1["time"]
        if not position_exists(symbol, "Buy"):
            logger.info(f"{symbol} | BUY FVG registered as active watcher (no active buy trade).")
        else:
            bt = state["buy_trade"]
            buffered_new_sl = mid
            if buffered_new_sl > bt["sl"]:
                old_sl = bt["sl"]
                bt["sl"] = buffered_new_sl
                logger.info(f"{symbol} | BUY SL tightened {old_sl} -> {bt['sl']} (buffered)")
            else:
                logger.info(f"{symbol} | BUY trade open; new BUY FVG not favorable for SL (new_low={new_low} <= sl={bt['sl']})")

    # -----------------------
    # SELL FVG
    # -----------------------
    if bear_fvg:
        new_high = prev2["low"]
        new_low = prev1["high"]
        mid = new_high - (new_high - new_low) * 0.5
        created_at = prev1["time"]
        state["sell_fvg"] = {
            "high": new_high,
            "low": new_low,
            "mid": mid,
            "tapped": False,
            "deepest_touch": None,
            "created_at": created_at}
        logger.info(f"{symbol} | New SELL FVG detected: high={new_high} low={new_low}")

        state["sell_fvg_candle_time"] = prev1["time"]
        if not position_exists(symbol, "Sell"):
            logger.info(f"{symbol} | SELL FVG registered as active watcher (no active sell trade).")
        else:
            st = state["sell_trade"]
            buffered_new_sl = mid
            if buffered_new_sl < st["sl"]:
                old_sl = st["sl"]
                st["sl"] = buffered_new_sl
                logger.info(f"{symbol} | SELL SL tightened {old_sl} -> {st['sl']} (buffered)")
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
    if state["buy_fvg"] and state["buy_fvg"]["tapped"]:
        bf = state["buy_fvg"]
        if bf["low"] <= last_closed["low"] <= bf["high"]:
            if bf["deepest_touch"] is None:
                bf["deepest_touch"] = last_closed["low"]
            else:
                bf["deepest_touch"] = min(bf["deepest_touch"], last_closed["low"])

    if state["sell_fvg"] and not state["sell_fvg"]["tapped"]:
        sf = state["sell_fvg"]
        if last_closed["time"] != state["sell_fvg_candle_time"]:
            if last_closed["low"] <= sf["high"] and last_closed["high"] >= sf["low"]:
                sf["tapped"] = True
                logger.info(f"{symbol} | SELL FVG TAPPED (candle touched the FVG range).")
    if state["sell_fvg"] and state["sell_fvg"]["tapped"]:
        sf = state["sell_fvg"]
        if sf["low"] <= last_closed["high"] <= sf["high"]:
            if sf["deepest_touch"] is None:
                sf["deepest_touch"] = last_closed["high"]
            else:
                sf["deepest_touch"] = max(sf["deepest_touch"], last_closed["high"])

    # -----------------------
    # FVG INVALIDATION
    # -----------------------
    if state["buy_fvg"]:
        bf = state["buy_fvg"]
        if last_closed["low"] < bf["low"]:
            logger.info(f"{symbol} | BUY FVG INVALIDATED (closed below {bf['low']})")
            state["buy_fvg"] = None

    if state["sell_fvg"]:
        sf = state["sell_fvg"]
        if last_closed["high"] > sf["high"]:
            logger.info(f"{symbol} | SELL FVG INVALIDATED (closed above {sf['high']})")
            state["sell_fvg"] = None

    # -----------------------
    # CONFIRMATION (OPEN PAPER/REAL TRADE)
    # -----------------------
    # BUY confirmation
    if state["buy_fvg"] and state["buy_fvg"]["tapped"] and not position_exists(symbol, "Buy") and last_closed["time"] != state["buy_fvg_candle_time"]:
        bf = state["buy_fvg"]
        
        if bf["deepest_touch"] is None or bf["deepest_touch"] > bf["mid"]:
            logger.info(f"{symbol} | BUY ignored: price did not reach FVG mid")
            return
        
        if bf["deepest_touch"] is not None:
            extreme_not_touched = bf["deepest_touch"] > bf["low"]    # did not touch extreme low
            if not extreme_not_touched:
                logger.info(f"{symbol} | BUY ignored: touched extreme")
                return

        if not daily_fvg_state[symbol]["allow_buy"]:
            logger.info(f"{symbol} | Daily bias does not allow BUY, skipping")
            return
            
        if last_closed["close"] > bf["high"]:
            entry = last_closed["close"]
            
            deep = bf["deepest_touch"]
            
            if deep is None:
                logger.info(f"{symbol} | BUY ignored: no deepest touch recorded")
                return
            real_sl = deep * (1 - SL_BUFFER)
            
            risk_sl = real_sl * (1 - SL_BUFFER)
            
            risk_amount = weekly_rf
            raw_qty = risk_amount / abs(entry - risk_sl)
            
            specs = get_symbol_specs(symbol)
            step = specs["qty_step"]
            
            qty = round_qty(symbol, raw_qty)
            qty = fit_qty_to_margin(     
                symbol,     
                entry,   
                leverage, 
                qty) 
            if qty is None:  
                logger.info(f"{symbol} | Not enough margin for trade")  
                return

            if not trade_value_ok(entry, qty):
                logger.info(f"{symbol} | Trade value < $5. Skipping")
                return
            
            sl = real_sl  # this is what will be sent to exchange
            
            state["buy_fvg"] = None
            sl_pct = (entry - sl) / entry
            if sl_pct >= MIN_SL_PCT:
                tp = entry + (entry - sl) * RR + (entry * TP_BUFFER)
                logger.info(f"{symbol} | BUY CONFIRMED | entry={entry} sl={sl} tp={tp}")
                if USE_REAL_TRADING and position_exists(symbol, "Sell"):
                    try:
                        logger.info(f"{symbol} | Closing existing SELL before opening BUY")
                        session.place_order(
                            category=CATEGORY,
                            symbol=symbol,
                            side="Buy",
                            orderType="Market",
                            qty="0",
                            reduceOnly=True,
                            positionIdx=2
                        )
                        time.sleep(0.2)  # small delay for safety
                    except Exception as e:
                        logger.error(f"{symbol} | Failed closing SELL: {e}")
                if USE_REAL_TRADING:
                    available_balance = get_real_balance()  # use your balance function
                    risk_amount = weekly_rf  # your frozen risk
                    raw_qty = risk_amount / abs(entry - risk_sl)
                    
                    specs = get_symbol_specs(symbol)
                    step = specs["qty_step"]
                    
                    qty = round_qty(symbol, raw_qty)
                    qty = fit_qty_to_margin(     
                        symbol,     
                        entry,   
                        leverage, 
                        qty) 
                    if qty is None:  
                        logger.info(f"{symbol} | Not enough margin for trade")  
                        return

                    if not trade_value_ok(entry, qty):
                        logger.info(f"{symbol} | Trade value < $5. Skipping")
                        return
                    
                    lp = calculate_liquidation_price(
                        entry=entry,
                        qty=qty,
                        side="Buy",
                        leverage=leverage,
                        available_balance=available_balance)
                    
                    distance_to_lp_pct = abs((sl - lp) / entry)
                    if distance_to_lp_pct < 0.003:  # 0.1%
                        logger.info(f"{symbol} | Skipping BUY - SL too close to liquidation ({distance_to_lp_pct*100:.3f}%)")
                        return
                    score = calculate_signal_score(entry, bf["low"], bf["high"])
                    
                    signal_queue.append({
                        "symbol": symbol,
                        "side": "BUY",
                        "entry": entry,
                        "sl": sl,
                        "tp": tp,
                        "score": score,
                        "qty": qty,
                        "leverage": leverage})
                    logger.info(f"{symbol} BUY signal queued | score={score:.4f}")

                else:
                    signal_queue.append({
                        "symbol": symbol,
                        "side": "BUY",
                        "entry": entry,
                        "sl": sl,
                        "tp": tp,
                        "score": score,
                         "qty": qty,
                        "leverage": leverage})
                    logger.info(f"{symbol} BUY signal queued | score={score:.4f}")
            else:
                logger.info(f"{symbol} | BUY confirmation ignored: SL too tight")

    # SELL confirmation
    if state["sell_fvg"] and state["sell_fvg"]["tapped"] and not position_exists(symbol, "Sell") and last_closed["time"] != state["sell_fvg_candle_time"]:
        sf = state["sell_fvg"]
        
        if sf["deepest_touch"] is None or sf["deepest_touch"] < sf["mid"]:
            logger.info(f"{symbol} | SELL ignored: price did not reach FVG mid")
            return


        if sf["deepest_touch"] is not None:
            extreme_not_touched = sf["deepest_touch"] < sf["high"]
            if not extreme_not_touched:
                logger.info(f"{symbol} | SELL ignored: touched extreme")
                return

        if not daily_fvg_state[symbol]["allow_sell"]:
            logger.info(f"{symbol} | Daily bias does not allow SELL, skipping")
            return
            
        if last_closed["close"] < sf["low"]:
            entry = last_closed["close"]
            
            deep = sf["deepest_touch"]
            
            if deep is None:
                logger.info(f"{symbol} | SELL ignored: no deepest touch recorded")
                return
                
            real_sl = deep * (1 + SL_BUFFER)
                
            risk_sl = real_sl * (1 + SL_BUFFER)
            
            risk_amount = weekly_rf
            raw_qty = risk_amount / abs(entry - risk_sl)
            
            specs = get_symbol_specs(symbol)
            step = specs["qty_step"]
            
            qty = round_qty(symbol, raw_qty)  
            qty = fit_qty_to_margin(     
                symbol,   
                entry,    
                leverage,   
                qty) 
            if qty is None:  
                logger.info(f"{symbol} | Not enough margin for trade")  
                return

            if not trade_value_ok(entry, qty):
                logger.info(f"{symbol} | Trade value < $5. Skipping")
                return
                
            sl = real_sl

            state["sell_fvg"] = None
            sl_pct = (sl - entry) / entry
            if sl_pct >= MIN_SL_PCT:
                tp = entry - (sl - entry) * RR - (entry * TP_BUFFER)
                logger.info(f"{symbol} | SELL CONFIRMED | entry={entry} sl={sl} tp={tp}")
                if USE_REAL_TRADING and position_exists(symbol, "Buy"):
                    try:
                        logger.info(f"{symbol} | Closing existing BUY before opening SELL")
                        session.place_order(
                            category=CATEGORY,
                            symbol=symbol,
                            side="Sell",
                            orderType="Market",
                            qty="0",
                            reduceOnly=True,
                            positionIdx=1)
                        time.sleep(0.2)
                    except Exception as e:
                        logger.error(f"{symbol} | Failed closing BUY: {e}")
                if USE_REAL_TRADING:
                    available_balance = get_real_balance()  # use your balance function
                    risk_amount = weekly_rf  # your frozen risk
                    raw_qty = risk_amount / abs(entry - risk_sl)
                    
                    specs = get_symbol_specs(symbol)
                    step = specs["qty_step"]
                    
                    qty = round_qty(symbol, raw_qty) 
                    qty = fit_qty_to_margin(   
                        symbol,  
                        entry,   
                        leverage,
                        qty) 
                    if qty is None:   
                        logger.info(f"{symbol} | Not enough margin for trade")  
                        return

                    if not trade_value_ok(entry, qty):
                        logger.info(f"{symbol} | Trade value < $5. Skipping")
                        return
                        
                    lp = calculate_liquidation_price(
                        entry=entry,
                        qty=qty,
                        side="Sell",
                        leverage=leverage,
                        available_balance=available_balance)
                    
                    distance_to_lp_pct = abs((lp - sl) / entry)
                    
                    if distance_to_lp_pct < 0.003:
                        logger.info(f"{symbol} | Skipping SELL - SL too close to liquidation ({distance_to_lp_pct*100:.3f}%)")
                        return
                    score = calculate_signal_score(entry, sf["low"], sf["high"])
                    
                    signal_queue.append({
                        "symbol": symbol,
                        "side": "SELL",
                        "entry": entry,
                        "sl": sl,
                        "tp": tp,
                        "score": score,
                        "qty": qty,
                        "leverage": leverage})
                    logger.info(f"{symbol} SELL signal queued | score={score:.4f}")

                else:
                    signal_queue.append({
                        "symbol": symbol,
                        "side": "SELL",
                        "entry": entry,
                        "sl": sl,
                        "tp": tp,
                        "score": score,
                        "qty": qty,
                        "leverage": leverage})
                    logger.info(f"{symbol} SELL signal queued | score={score:.4f}")
            else:
                logger.info(f"{symbol} | SELL confirmation ignored: SL too tight")
    
def place_real_trade(symbol, side, entry, sl, tp, leverage, frozen_risk, qty):

    side = side.upper()

    if get_total_open_positions() >= MAX_ACTIVE_TRADES:
        logger.info(f"Max {MAX_ACTIVE_TRADES} open trades reached. Skipping.")
        return

    if side == "BUY":
        order_side = "Buy"
        position_idx = 1
    elif side == "SELL":
        order_side = "Sell"
        position_idx = 2
    else:
        logger.error(f"{symbol} | Invalid side: {side}")
        return

    if position_exists(symbol, order_side):
        logger.info(f"{symbol} | {side} position already exists. Skipping.")
        return

    if frozen_risk <= 0:
        logger.warning(f"{symbol} | Frozen risk is zero. Skipping trade.")
        return

    try:
        sl_distance = abs(entry - sl)

        if sl_distance <= 0:
            logger.warning(f"{symbol} | SL distance zero. Aborting.")
            return

        if qty <= 0:
            logger.warning(f"{symbol} | Invalid qty ({qty}). Aborting.")
            return

        logger.info(f"{symbol} | Frozen Risk: ${frozen_risk:.4f}")
        logger.info(f"{symbol} | Qty: {qty}")

        # =============================
        # SIMULATION MODE
        # =============================
        if not USE_REAL_TRADING:
            logger.info(f"[SIMULATED] {symbol} {side} | entry={entry} sl={sl} tp={tp} qty={qty}")
            refresh_account_cache()
            return

        # =============================
        # REAL EXECUTION
        # =============================
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

        session.set_trading_stop(
            category=CATEGORY,
            symbol=symbol,
            takeProfit=str(tp),
            stopLoss=str(sl),
            positionIdx=position_idx
        )

        refresh_account_cache()

        logger.info(f"{symbol} | REAL {side} ORDER PLACED | TP={tp} SL={sl}")

    except Exception as e:
        logger.error(f"{symbol} | Order error: {e}")# MAIN LOOP
# ===========================
def main():
    global balance, daily_rf

    logger.info("LIVE PAPER FVG BOT (simulation) STARTED")
    real_balance = get_real_balance()
    logger.info(f"STARTUP BALANCE = ${real_balance:.4f}")
    refresh_account_cache()
    ensure_hedge_mode()
    # Lock initial daily RF for current UTC day
    # update_daily_bias()

    refresh_symbol_universe_if_needed()

    for p in PAIRS:
        logger.info(f"Symbol loaded: {p['symbol']} | leverage: {p['leverage']}")
    
    for p in PAIRS:
        get_symbol_specs(p["symbol"])
        
    for p in PAIRS:
        set_symbol_leverage(p["symbol"], p["leverage"])
        
    lock_weekly_rf_if_needed()

    try:
        while True:
            refresh_symbol_universe_if_needed()
            
            # wait until next candle close (UTC)
            wait = seconds_until_next_candle(INTERVAL)
            logger.info(f"Waiting {wait}s for next {INTERVAL}m candle close (UTC)...")
            time.sleep(wait + 0.8)  # small offset to ensure candle is closed on exchange

            refresh_account_cache()
            
            for p in PAIRS:
                update_daily_bias(p["symbol"])
            
            # Lock per-day RF at start of UTC day if needed (one global RF for all pairs)
            lock_weekly_rf_if_needed()

            # Process each pair independently
            eligible_pairs = []
            
            for p in PAIRS:
                sym = p["symbol"]
                if daily_fvg_state[sym]["allow_buy"] or daily_fvg_state[sym]["allow_sell"]:
                    logger.info(f"{symbol} | Daily bias: buy={daily_fvg_state[symbol]['allow_buy']}, sell={daily_fvg_state[symbol]['allow_sell']}")
                    eligible_pairs.append(p)
            logger.info(f"Scanning {len(eligible_pairs)} eligible symbols out of {len(PAIRS)}")
            
            for p in eligible_pairs:
                try:
                    handle_symbol(p)
                    time.sleep(0.15)
                except Exception as e:
                    logger.exception(f"{p['symbol']} | Error in handle_symbol: {e}")
            if len(eligible_pairs) == 0:
                logger.info("No eligible pairs from daily bias. Skipping cycle.")
                continue
                    
            process_signal_queue()
            # small sleep to avoid rate-limit bursts
            time.sleep(0.2)

    except KeyboardInterrupt:
        logger.info("Stopped by user (KeyboardInterrupt). Final balance: ${:.4f}".format(balance))


if __name__ == "__main__":
    main()


