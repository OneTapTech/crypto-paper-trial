"""Immutable registry, validation gates and portfolio risk preflight.

No method in this module places an order or promotes a live strategy.
Evidence checks are necessary, not a substitute for reproducible artifact review.
"""
import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

STATUSES = ['IDEA', 'SPEC', 'BACKTESTED', 'VALIDATED', 'PAPER', 'LIVE-SMALL', 'LIVE']


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def database(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(Path(__file__).with_name('schema.sql').read_text())
    return db


def graveyard_matches(db, family, fingerprint):
    # Family match also catches renamed / cosmetically reparameterized ideas.
    return [dict(r) for r in db.execute(
        'SELECT * FROM graveyard WHERE family=? OR fingerprint=?', (family, fingerprint))]


def register(db, spec):
    sid, version = spec['id'], spec['version']
    existing = db.execute('SELECT * FROM strategies WHERE id=? AND version=?', (sid, version)).fetchone()
    if existing:
        if existing['spec_sha256'] != digest(spec):
            raise ValueError('Spec changed without a new version')
        return dict(existing)
    family = spec['family']
    fp = digest({'family': family, 'entry': spec['entry'], 'exit': spec['exit'],
                 'stop': spec['stop'], 'regime_switch': spec['regime_switch']})
    stamp = now()
    db.execute('''INSERT INTO strategies VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
               (sid, version, 'Engineer', family, spec['hypothesis'], spec['mechanism'],
                spec['market'], spec['timeframe'], fp, canonical(spec), digest(spec),
                'IDEA', stamp, stamp, 'Registered before development test', 1))
    append_history(db, sid, version, None, 'IDEA', 'Analyst', 'Hypothesis registered', None)
    matches = graveyard_matches(db, family, fp)
    if matches:
        transition(db, sid, version, 'RETIRED', 'Validator',
                   'Graveyard veto: mechanism/family already rejected; changed causal evidence required',
                   {'matches': [m['strategy_id'] for m in matches]})
    else:
        transition(db, sid, version, 'SPEC', 'Engineer', 'Deterministic rules frozen; graveyard search clear')
    db.commit()
    return dict(db.execute('SELECT * FROM strategies WHERE id=? AND version=?', (sid, version)).fetchone())


def append_history(db, sid, version, before, after, actor, reason, evidence):
    previous = db.execute('SELECT event_hash FROM history ORDER BY seq DESC LIMIT 1').fetchone()
    previous = previous[0] if previous else '0' * 64
    event = dict(strategy_id=sid, version=version, at=now(), from_status=before,
                 to_status=after, actor=actor, reason=reason,
                 evidence_sha256=digest(evidence) if evidence is not None else None,
                 previous_hash=previous)
    db.execute('''INSERT INTO history(strategy_id,version,at,from_status,to_status,actor,reason,
                  evidence_sha256,previous_hash,event_hash) VALUES(?,?,?,?,?,?,?,?,?,?)''',
               (*event.values(), digest(event)))


def transition(db, sid, version, target, actor, reason, evidence=None):
    row = dict(db.execute('SELECT * FROM strategies WHERE id=? AND version=?', (sid, version)).fetchone())
    current = row['status']
    if current == 'RETIRED':
        raise ValueError('Retired versions cannot be resurrected')
    if target != 'RETIRED':
        if target != STATUSES[STATUSES.index(current) + 1]:
            raise ValueError('Cannot skip a lifecycle gate')
        # Higher stages deliberately unavailable to this pilot, even with a forged report.
        if target in ('VALIDATED', 'PAPER', 'LIVE-SMALL', 'LIVE'):
            raise ValueError('Pilot promotion locked: independent artifact review and a separate promotion implementation required')
    db.execute('UPDATE strategies SET status=?, updated_at=?, reason=? WHERE id=? AND version=?',
               (target, now(), reason, sid, version))
    append_history(db, sid, version, current, target, actor, reason, evidence)
    if target == 'RETIRED':
        db.execute('INSERT INTO graveyard VALUES(NULL,?,?,?,?,?,?,?,?)',
                   (sid, version, row['family'], row['fingerprint'], row['hypothesis'],
                    now(), reason, canonical(evidence or {})))


def validation_failures(e):
    """Strict manifest checklist; absent, non-finite or unverifiable fields fail closed.

    Actual VALIDATED promotion remains locked until full artifact verifier exists.
    """
    failures = []
    def finite(x):
        return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
    for name in ('point_in_time_verified', 'leakage_checks_passed', 'all_costs_included',
                 'holdout_sealed_before_test', 'reproduced_independently',
                 'all_trials_logged', 'serial_dependence_corrected'):
        if e.get(name) is not True:
            failures.append(name)
    n = e.get('unseen_closed_trades')
    if not isinstance(n, int) or isinstance(n, bool) or n < 300:
        failures.append('at least 300 unseen closed trades per spec version')
    pf = e.get('net_profit_factor')
    if not finite(pf) or pf <= 1.25:
        failures.append('net profit factor > 1.25')
    windows = e.get('walk_forward', [])
    if len(windows) != 3 or any(
        not finite(w.get('net_profit_factor')) or w['net_profit_factor'] <= 1.25 or
        not finite(w.get('net_pnl')) or w['net_pnl'] <= 0 or
        w.get('sealed') is not True for w in windows):
        failures.append('three sealed, stable positive walk-forward windows')
    regimes = e.get('regimes', [])
    if not regimes or any(r.get('switch_off_verified') is not True and
                         (not finite(r.get('net_pnl')) or r['net_pnl'] <= 0 or
                          r.get('adequate_sample') is not True) for r in regimes):
        failures.append('every active regime profitable with adequate sample, or switched off')
    variants = e.get('perturbations', [])
    expected = {(p, d) for p in e.get('tunable_parameters', []) for d in (-0.2, 0.2)}
    actual = {(v.get('parameter'), v.get('change')) for v in variants}
    if not expected or not expected.issubset(actual) or any(
        not finite(v.get('net_profit_factor')) or v['net_profit_factor'] <= 1.25 or
        not finite(v.get('net_pnl')) or v['net_pnl'] <= 0 for v in variants):
        failures.append('all tunable parameters survive both +/-20% changes')
    dsr = e.get('deflated_sharpe_probability')
    if not finite(dsr) or not 0.95 <= dsr <= 1:
        failures.append('deflated Sharpe probability >= 0.95')
    if not isinstance(e.get('total_trials'), int) or e['total_trials'] < 1:
        failures.append('trial count missing')
    for name in ('dataset_sha256', 'code_sha256', 'spec_sha256', 'trial_ledger_sha256'):
        value = e.get(name, '')
        if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
            failures.append(name)
    return failures


def size_quantity(equity, entry, stop, fee_bps, slippage_bps, funding_reserve,
                  existing_strategy_risk=0, existing_open_risk=0, gross_notional=0):
    values = [equity, entry, stop, fee_bps, slippage_bps, funding_reserve,
              existing_strategy_risk, existing_open_risk, gross_notional]
    if not all(math.isfinite(v) for v in values) or min(equity, entry, stop) <= 0:
        raise ValueError('Invalid sizing inputs')
    if min(fee_bps, slippage_bps, funding_reserve, existing_strategy_risk,
           existing_open_risk, gross_notional) < 0:
        raise ValueError('Negative risk/cost input')
    # Sizing upper bound only. The portfolio preflight also enforces the strict
    # 25% share of actual aggregate open risk across a proposed allocation batch.
    budget = min(equity * 0.005, equity * 0.03 * 0.25 - existing_strategy_risk,
                 equity * 0.03 - existing_open_risk - 0.01)
    unit_risk = abs(entry - stop) + entry * 2 * (fee_bps + slippage_bps) / 10000 + funding_reserve
    if budget <= 0 or unit_risk <= 0:
        return 0.0
    return max(0.0, min(budget / unit_risk, (3 * equity - gross_notional) / entry))


def risk_preflight(equity, day_start, high_water, open_risk, gross_notional,
                   strategy_risk, correlations, fresh, costs_multiple=1,
                   strategy_dd=0, backtest_dd=None, bad_trade_count=0, regime_on=True):
    reasons = []
    vals = [equity, day_start, high_water, open_risk, gross_notional, strategy_risk,
            costs_multiple, strategy_dd, bad_trade_count]
    if not all(isinstance(x, (float, int)) and math.isfinite(x) for x in vals) or min(equity, day_start, high_water) <= 0:
        return ['invalid/non-finite risk state']
    if min(open_risk, gross_notional, strategy_risk, costs_multiple, strategy_dd, bad_trade_count) < 0:
        reasons.append('negative risk state')
    if equity <= day_start * 0.98:
        reasons.append('daily loss >= 2%: global stop until next Chicago day')
    if equity <= high_water * 0.88:
        reasons.append('drawdown >= 12%: global stop; owner review required')
    if open_risk >= equity * 0.03:
        reasons.append('total open risk must be strictly below 3%')
    if strategy_risk > equity * 0.03 * 0.25:
        reasons.append('strategy exceeds 25% of fixed total risk budget')
    if strategy_risk > open_risk * 0.25:
        reasons.append('strategy exceeds 25% of actual deployed open risk')
    if gross_notional > equity * 3:
        reasons.append('gross leverage > 3x')
    if not fresh:
        reasons.append('missing or stale data')
    # List is the pair correlations of proposed strategy vs existing strategies.
    if any(x is None or not isinstance(x, (int, float)) or not math.isfinite(x) for x in correlations):
        reasons.append('unknown correlation treated as blocked')
    elif sum(abs(x) > 0.6 for x in correlations) >= 2:
        reasons.append('would create a correlated cluster larger than two')
    if costs_multiple >= 2:
        reasons.append('execution costs doubled')
    if strategy_dd and (backtest_dd is None or not math.isfinite(backtest_dd)):
        reasons.append('missing strategy drawdown baseline')
    elif backtest_dd is not None and strategy_dd > 1.5 * backtest_dd:
        reasons.append('strategy drawdown > 1.5x backtest')
    if bad_trade_count >= 40:
        reasons.append('40 trades below pre-registered expectations')
    if not regime_on:
        reasons.append('regime disappeared')
    return reasons
