"""Deterministic development replay. Never an unseen-data validation result.

Features only access completed candles through t, fills start at t+1 open.
Stops take priority if stop and target share a bar; gaps fill adversely.
Funding is a conservative debit: observed hourly absolute cash-rate magnitude,
no funding credits, full-bar charges even on intrabar exits. Missing hours veto.
"""
import math
import statistics
from .governance import digest

DEFAULTS = dict(lookback=24, atr_period=14, trend_period=50, stop_atr=2.0,
                max_hold=24, volume_multiplier=1.25)


def make_spec(market, symbol, created, expiry):
    return {
        'id': f'TREND24-{market}', 'version': 1, 'family': 'volume-confirmed-channel-breakout-v1',
        'market': market, 'reference_symbol': symbol, 'timeframe': '4h',
        'created_at': created, 'expiry_date': expiry, 'mode': 'paper-research',
        'hypothesis': 'A 24-bar channel breakout with above-median volume and aligned 50-bar trend has positive net expectancy over at most 24 bars.',
        'mechanism': 'Persistent directional order flow following a volatility/channel expansion; falsified by nonpositive net expectancy after conservative costs.',
        'parameters': DEFAULTS,
        'entry': {'decision': 'after candle t closes', 'fill': 'next candle open plus adverse slippage',
                  'long': 'close[t] > max(high[t-24:t]) AND close[t] > mean(close[t-49:t+1]) AND volume[t] >= 1.25 * median(volume[t-24:t])',
                  'short': 'close[t] < min(low[t-24:t]) AND close[t] < mean(close[t-49:t+1]) AND volume[t] >= 1.25 * median(volume[t-24:t])',
                  'one_position_per_strategy': True},
        'exit': {'take_profit': '2R from actual entry fill', 'timeout': '24 completed bars after entry',
                 'intrabar_priority': 'stop first', 'force_close_at_dataset_end': True},
        'stop': {'type': 'fixed', 'distance': '2 * mean(true_range[t-13:t+1]) at signal',
                 'gap': 'worse of stop price and bar open, plus adverse slippage'},
        'size': {'max_equity_risk_fraction': 0.005, 'include_round_trip_costs': True,
                 'max_strategy_risk_fraction_of_total_budget': 0.25,
                 'max_gross_portfolio_leverage': 3, 'live_small_fraction': 0.2},
        'max_slippage': {'bps_per_side': 10, 'action_if_exceeded': 'reject entry'},
        'filters': {'fresh_book_seconds': 60, 'fresh_mark_seconds': 120,
                    'spread_bps_max': 10, 'depth_multiple_of_order_min': 10,
                    'macro_event_blackout_minutes_before_after': 60,
                    'missing_required_data': 'reject entry',
                    'required': ['closed_ohlcv', 'funding_history', 'current_book', 'verified_macro_calendar']},
        'regime_switch': {'on': '50-bar SMA aligned with breakout direction and ATR/close in [0.002,0.10]',
                          'off': 'missing data, extreme volatility, flat trend, macro blackout or risk stop'},
        'implementation': 'quant_desk/research.py:replay',
        'pilot_limitations': ['Development replay does not implement book or macro filters without point-in-time historical data.',
                              'No live executor; no VALIDATED or PAPER promotion in this pilot.'],
        'immutable': True,
    }


def variants():
    yield 'base', dict(DEFAULTS)
    for key, value in DEFAULTS.items():
        for change in (-0.2, 0.2):
            modified = dict(DEFAULTS)
            modified[key] = round(value * (1 + change)) if isinstance(value, int) else value * (1 + change)
            yield f'{key}:{change:+.0%}', modified


def normalized_candles(payload, asof, step=14400):
    rows = []
    seen = set()
    for c in payload['candles']:
        stamp = int(c['time']) // 1000
        if stamp in seen:
            raise ValueError('duplicate candle')
        seen.add(stamp)
        if stamp + step > asof:
            continue
        row = {'time': stamp, **{k: float(c[k]) for k in ('open', 'high', 'low', 'close', 'volume')}}
        if not all(math.isfinite(row[k]) for k in row) or min(row[k] for k in ('open','high','low','close')) <= 0 or row['volume'] < 0:
            raise ValueError('invalid candle values')
        if row['high'] < max(row['open'],row['close'],row['low']) or row['low'] > min(row['open'],row['close']):
            raise ValueError('inconsistent OHLC')
        rows.append(row)
    rows.sort(key=lambda r: r['time'])
    if len(rows) < 100:
        raise ValueError('insufficient completed candles')
    if any(b['time'] - a['time'] != step for a, b in zip(rows, rows[1:])):
        raise ValueError('candle gap: no forward filling permitted')
    return rows


def replay(candles, funding, params=None, fee_bps=6, slip_bps=5):
    p = params or DEFAULTS
    if min(fee_bps, slip_bps) < 0 or not all(math.isfinite(x) for x in (fee_bps,slip_bps)):
        raise ValueError('invalid costs')
    look, atr_n, trend = p['lookback'], p['atr_period'], p['trend_period']
    fee, slip = fee_bps / 10000, slip_bps / 10000
    warmup = max(look, atr_n + 1, trend)
    pos = None
    pending = None
    trades, equity_curve = [], []
    realized, equity, hwm, max_dd = 0.0, 10000.0, 10000.0, 0.0
    skipped = 0
    trs = [c['high']-c['low'] if i == 0 else max(c['high']-c['low'], abs(c['high']-candles[i-1]['close']),abs(c['low']-candles[i-1]['close'])) for i,c in enumerate(candles)]
    for i, c in enumerate(candles):
        if pending:
            side, distance, signal_index = pending
            price = c['open'] * (1 + side * slip)
            known_hours = list(range(c['time']-86400,c['time'],3600))
            if any(h not in funding for h in known_hours):
                raise ValueError('funding history unavailable for as-of sizing reserve')
            reserve = max(abs(funding[h]) for h in known_hours) * p['max_hold'] * 4
            unit_risk = distance + price * 2 * (fee + slip) + reserve
            quantity = min(max(equity,0) * 0.005 / unit_risk, max(equity,0) * 3 / price)
            pos = dict(side=side, entry=price, stop=price-side*distance, target=price+side*2*distance,
                       quantity=quantity, entry_index=i, signal_index=signal_index, entry_time=c['time'],
                       funding=0.0, entry_fee=price*quantity*fee)
            pending = None
        if pos:
            side = pos['side']
            # Exact hourly rates are outcomes/costs, never signal features.
            hours = list(range(c['time'],c['time']+14400,3600))
            if any(h not in funding for h in hours):
                raise ValueError('funding gap during an open trade')
            pos['funding'] += sum(abs(funding[h]) for h in hours) * pos['quantity']
            stop_hit = c['low'] <= pos['stop'] if side == 1 else c['high'] >= pos['stop']
            target_hit = c['high'] >= pos['target'] if side == 1 else c['low'] <= pos['target']
            raw_exit, reason = None, None
            if stop_hit:
                raw_exit = min(pos['stop'],c['open']) if side == 1 else max(pos['stop'],c['open'])
                reason = 'stop'
            elif target_hit:
                raw_exit, reason = pos['target'], 'target'
            elif i - pos['entry_index'] + 1 >= p['max_hold']:
                raw_exit, reason = c['close'], 'timeout'
            elif i == len(candles)-1:
                raw_exit, reason = c['close'], 'end_of_sample'
            if raw_exit is not None:
                fill = raw_exit * (1-side*slip)
                fees = pos['entry_fee'] + fill*pos['quantity']*fee
                pnl = side*(fill-pos['entry'])*pos['quantity']-fees-pos['funding']
                trades.append({**pos, 'exit_time': c['time']+14400, 'exit_index': i,
                               'exit': fill, 'net_pnl': pnl, 'fees': fees, 'reason':reason,
                               'label':'BACKTEST-DEVELOPMENT'})
                realized += pnl
                pos = None
        unrealized = (pos['side']*(c['close']-pos['entry'])*pos['quantity']-pos['entry_fee']-pos['funding']) if pos else 0
        equity = 10000+realized+unrealized
        hwm = max(hwm,equity)
        max_dd = max(max_dd,1-equity/hwm)
        equity_curve.append({'time':c['time']+14400,'equity':equity})
        if pos or i < warmup or i == len(candles)-1:
            continue
        atr = statistics.mean(trs[i-atr_n+1:i+1])
        sma = statistics.mean(r['close'] for r in candles[i-trend+1:i+1])
        if not 0.002 <= atr/c['close'] <= 0.1:
            continue
        prior = candles[i-look:i]
        if c['volume'] < statistics.median(r['volume'] for r in prior)*p['volume_multiplier']:
            continue
        side = 1 if c['close'] > max(r['high'] for r in prior) and c['close'] > sma else (
            -1 if c['close'] < min(r['low'] for r in prior) and c['close'] < sma else 0)
        if side:
            next_time = candles[i+1]['time']
            # Missing funding blocks the whole replay rather than dropping bad trades.
            if any(h not in funding for h in range(next_time,next_time+14400,3600)):
                raise ValueError('funding unavailable at entry simulation')
            pending = (side,atr*p['stop_atr'],i)
    gains = sum(max(t['net_pnl'],0) for t in trades)
    losses = -sum(min(t['net_pnl'],0) for t in trades)
    pnl = sum(t['net_pnl'] for t in trades)
    return {'label':'BACKTEST-DEVELOPMENT', 'unseen_trades':0, 'closed_trades':len(trades),
            'net_pnl_usd':pnl, 'net_return_pct':pnl/100,
            'profit_factor':gains/losses if losses > 0 else None,
            'profit_factor_note':'null if no losing trades; not an automatic pass',
            'max_drawdown_pct':100*max_dd, 'fees_usd':sum(t['fees'] for t in trades),
            'conservative_funding_debit_usd':sum(t['funding'] for t in trades),
            'trade_sha256':digest(trades), 'trades':trades,'equity_curve':equity_curve,
            'parameters':p, 'ignored_signals':skipped,
            'limitations':['not unseen; no original publication/latency archive',
                           'historical order book, macro blackout and market impact not modeled',
                           'funding credits omitted; full-bar funding charged on intrabar exits',
                           'funding reserve uses only the preceding 24 known hourly rates',
                           'standalone diagnostic, not a risk-governed combined portfolio']}
