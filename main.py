import ccxt
import pandas as pd
import os
import ta
import time
import math
import json
import schedule
import traceback

from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv('API_KEY')
secret = os.getenv('SECRET')


def count_sig_digits(precision):
    # Count digits after decimal point if it's a fraction
    if precision < 1:
        return abs(int(round(math.log10(precision))))
    else:
        return 1  # Treat whole numbers like 1, 10, 100 as 1 sig digit

def round_to_sig_figs(num, sig_figs):
    if num == 0:
        return 0
    return round(num, sig_figs - int(math.floor(math.log10(abs(num)))) - 1)

def check_trade_signal(exchange, symbol):
    ohlcv = exchange.fetch_ohlcv(symbol, '1h', limit=10)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

    df['EMA_9'] = df['close'].ewm(span=9, adjust=False).mean()
    df['EMA_21'] = df['close'].ewm(span=21, adjust=False).mean()

    atr = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=10).average_true_range()
    rsi = ta.momentum.RSIIndicator(df['close'], window=10).rsi()

    df['atr'] = atr
    df['rsi'] = rsi

    latest_close = df['close'].iloc[-1]
    latest_atr = df['atr'].iloc[-1]
    normalized_atr = latest_atr / latest_close
    ema_9 = df['EMA_9'].iloc[-1]
    ema_21 = df['EMA_21'].iloc[-1]
    rsi_now = df['rsi'].iloc[-1]
    rsi_prev = df['rsi'].iloc[-2]

    trend = "uptrend" if ema_9 > ema_21 else "downtrend" if ema_9 < ema_21 else "sideways"

    should_trade, side = False, None
    if normalized_atr > 0.015:
        if trend == "downtrend" and rsi_now <= 30:
            should_trade, side = True, 'buy'
        elif trend == "uptrend" and rsi_now >= 70:
            should_trade, side = True, 'sell'

    return should_trade, side, {
        'atr': latest_atr,
        'atr_norm': normalized_atr,
        'ema_9': ema_9,
        'ema_21': ema_21,
        'rsi_now': rsi_now,
        'rsi_prev': rsi_prev,
        'trend': trend
    }

# Helper function to get open long/short counts
def get_open_position_counts(exchange, all_symbols):
    positions = exchange.fetch_positions(symbols=all_symbols)
    open_positions = [pos for pos in positions if pos.get('contracts') and abs(float(pos['contracts'])) > 0]
    short_positions = [
        pos for pos in open_positions
        if (pos.get('side') == 'short') or
        ('size' in pos and float(pos['size']) < 0) or
        ('info' in pos and pos['info'].get('side', '').lower() == 'sell')
    ]
    long_positions = [
        pos for pos in open_positions
        if (pos.get('side') == 'long') or
        ('size' in pos and float(pos['size']) > 0) or
        ('info' in pos and pos['info'].get('side', '').lower() == 'buy')
    ]
    return open_positions, len(short_positions), len(long_positions)

# Trading parameters
usdt_value = 1.5
leverage = 10
fromPercnt = 0.1  #20%

def calculateLiquidationTargPrice(_liqprice, _entryprice, _percnt, _round):
    return round_to_sig_figs(_entryprice + (_liqprice - _entryprice) * _percnt, _round)

def place_market_then_liquidation_limit_order(exchange, symbol, side, usdt_value, leverage):
    try:
        # Fetch market price
        ticker = exchange.fetch_ticker(symbol)
        market_price = ticker['last']
        base_amount = usdt_value / market_price

        # Try setting isolated margin mode
        try:
            exchange.set_margin_mode('isolated', symbol)
        except Exception as e:
            print(f"⚠️ Failed to set margin mode for {symbol}: {e}")

        # Try setting leverage
        try:
            exchange.set_leverage(leverage, symbol)
        except Exception as e:
            print(f"⚠️ Failed to set leverage for {symbol}: {e}")

        print(f"🔔 ORDER → {symbol} | {side.upper()} | Price: {market_price:.4f} | Qty: {base_amount:.5f}")

        # Attempt market order without posSide
        try:
            order = exchange.create_order(
                symbol=symbol,
                type='market',
                side=side,
                amount=base_amount,
                params={'reduceOnly': False}
            )
            print(f"✅ Market Order Placed: {order}")
        except ccxt.BaseError as e:
            if 'TE_ERR_INCONSISTENT_POS_MODE' in str(e):
                print("🔁 Retrying market order with posSide...")
                pos_side = 'Long' if side == 'buy' else 'Short'
                order = exchange.create_order(
                    symbol=symbol,
                    type='market',
                    side=side,
                    amount=base_amount,
                    params={
                        'reduceOnly': False,
                        'posSide': pos_side
                    }
                )
                print(f"✅ Market Order (with posSide) Placed: {order}")
            else:
                raise

        # 🔍 Fetch liquidation price
        positions = exchange.fetch_positions([symbol])
        pos_side_str = 'Long' if side == 'buy' else 'Short'
        position = next((p for p in positions if p['side'] == pos_side_str.lower()), None)

        if position and float(position.get('liquidationPrice') or 0):
            liquidation_price = float(position.get('liquidationPrice') or 0)
            entry_price = float(position.get('entryPrice') or 0)
            mark_price = float(position.get('markPrice') or 0)
            contracts = float(position.get('contracts') or 0)
            reEntryLeverage = float(position.get("leverage") or 1)
            notional = float(position.get('notional') or 0)
            # Price
            price_precision_val = exchange.markets[symbol]['precision']['price']
            price_sig_digits = count_sig_digits(price_precision_val)
            # amount
            amount_precision_val = exchange.markets[symbol]['precision']['amount']
            print("Price Precision: ", price_precision_val, " sig value: ", price_sig_digits)
            amount_sig_digits = count_sig_digits(amount_precision_val)
            
            double_notional = notional * 2
            order_amount = double_notional / mark_price
            order_amount = round_to_sig_figs(order_amount, amount_sig_digits)
            print(f"💡 Liquidation Price: {liquidation_price}")
            try:
                print("Calculation Re-entry Target Price")
                targetPrice = calculateLiquidationTargPrice(entry_price, liquidation_price, fromPercnt, price_sig_digits)
                print("Target Price: ", targetPrice)
            

                # Try placing limit order without posSide first
                try:
                    limit_order = exchange.create_order(
                        symbol=symbol,
                        type='limit',
                        side=side,
                        amount=order_amount,
                        price=targetPrice
                    )
                    print(f"📌 Limit Order Placed at Liquidation Price: {limit_order}")
                except ccxt.BaseError as e:
                    if 'TE_ERR_INCONSISTENT_POS_MODE' in str(e):
                        print("🔁 Retrying limit order with posSide...")
                        limit_order = exchange.create_order(
                            symbol=symbol,
                            type='limit',
                            side=side,
                            amount=order_amount,
                            price=targetPrice,
                            params={
                                'posSide': pos_side_str
                            }
                        )
                        print(f"📌 Limit Order (with posSide) Placed at Liquidation Price: {limit_order}")
                    else:
                        raise
            except Exception as e:
                print(f"❌ Error in getting liquidation target price for {symbol}: {e}")
        else:
            print("⚠️ Could not retrieve liquidation price. Skipping limit order.")

    except Exception as e:
        print(f"❌ Error in order flow for {symbol}: {e}")


# MAIN LOOP
def main():
    try:
        exchange = ccxt.phemex({
            'apiKey': api_key,
            'secret': secret,
            'enableRateLimit': True,
            'options': {'defaultType': 'swap'},
        })

        MAX_NO_SELL_TRADE = 8
        MAX_NO_BUY_TRADE = 2

        markets = exchange.load_markets()
        all_symbols = [s for s in markets if s.endswith(':USDT') and markets[s]['type'] == 'swap']

        # Initial fetch of positions
        open_positions, _, _ = get_open_position_counts(exchange, all_symbols)
        opened_symbols = {pos['symbol'] for pos in open_positions}
        symbols_not_opened = [s for s in all_symbols if s not in opened_symbols]

        for symbol in symbols_not_opened:
            try:
                signal, side, details = check_trade_signal(exchange, symbol)
                print(f"{symbol} → Signal: {signal}, Side: {side}, Trend: {details['trend']}")

                if signal:
                    _, short_count, long_count = get_open_position_counts(exchange, all_symbols)
                    # Respect max trades
                    if short_count >= MAX_NO_SELL_TRADE and side == 'sell':
                        print(f"❌ Skip {symbol}: sell limit reached ({short_count})")
                        continue
                    if long_count >= MAX_NO_BUY_TRADE and side == 'buy':
                        print(f"❌ Skip {symbol}: buy limit reached ({long_count})")
                        continue
                    if side == 'buy':
                        print("No buy for now!!")
                        continue
                    
                    try:
                        place_market_then_liquidation_limit_order(exchange, symbol, side, usdt_value, leverage)
                    except Exception as e:
                        print(f"❌ {symbol} → General Error: {e}")


                
            except Exception as e:
                print("Error inside job:")
                traceback.print_exc()
                
    except Exception as e:
        print(f"Main function error: {e}")
        traceback.print_exc()

schedule.every(6).seconds.do(main)

while True:
    try:
        schedule.run_pending()
        time.sleep(1)
    except Exception as e:
        print("Scheduler crashed:")
        traceback.print_exc()
        print("Retrying in 10 seconds...")
        time.sleep(8)
