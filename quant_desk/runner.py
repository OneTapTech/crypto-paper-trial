"""Public GET data collection, frozen experiments, registry and daily brief.

Run: python -m quant_desk.runner --state /path/to/quant-state
Persistent state must be restored before running. Missing state markers fail closed
in the workflow. No trading credentials, API keys, LLM calls, or order endpoints.
"""
import argparse
import concurrent.futures
import hashlib
import json
import os
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .governance import canonical, database, digest, now, register, transition, validation_failures
from .research import make_spec, normalized_candles, replay, variants

ROOT = Path(__file__).resolve().parent
BASE = 'https://futures.kraken.com'


def get_json(url):
    # Strict host and GET allowlist. External content is data, never instructions.
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != 'https' or parsed.netloc != 'futures.kraken.com':
        raise ValueError('Unapproved data host')
    if not parsed.path.startswith(('/derivatives/api/v3/tickers', '/derivatives/api/v3/orderbook',
                                   '/derivatives/api/v3/historical-funding-rates', '/api/charts/v1/')):
        raise ValueError('Unapproved data endpoint')
    req = urllib.request.Request(url, headers={'User-Agent':'OneTapTech-paper-research/1.0'})
    with urllib.request.urlopen(req, timeout=20) as response:
        if urllib.parse.urlparse(response.url).netloc != parsed.netloc:
            raise ValueError('Unexpected redirect')
        raw = response.read(12_000_001)
        if len(raw) > 12_000_000:
            raise ValueError('Data response too large')
    data = json.loads(raw)
    if data.get('result') == 'error' or data.get('errors'):
        raise ValueError('Source returned an error: '+str(data.get('errors',data.get('error'))))
    return data, now()


def sources(config, asof, funding_markets=None):
    yield '_ALL', 'tickers', BASE+'/derivatives/api/v3/tickers'
    for market, symbol in config['markets'].items():
        yield market, 'book', BASE+'/derivatives/api/v3/orderbook?'+urllib.parse.urlencode({'symbol':symbol})
        yield market, 'candles', BASE+f'/api/charts/v1/trade/{symbol}/4h?count=600'
        if funding_markets is None or market in funding_markets:
            yield market, 'funding', BASE+'/derivatives/api/v3/historical-funding-rates?'+urllib.parse.urlencode({'symbol':symbol})
        for metric in ('open-interest','liquidation-volume'):
            yield market, metric, BASE+f'/api/charts/v1/analytics/{symbol}/{metric}?since={asof-30*86400}&interval=14400&to={asof}'


def collect(db, cycle_id, state, config, asof):
    raw_dir = state/'raw'/cycle_id
    raw_dir.mkdir(parents=True)
    data, issues = {}, []
    funding_markets={m for m in config['markets'] if not db.execute(
        'SELECT 1 FROM experiments WHERE strategy_id=? AND version=1 LIMIT 1',(f'TREND24-{m}',)).fetchone()}
    jobs = list(sources(config, asof, funding_markets))
    def run(job):
        market, kind, url = job
        try:
            payload, received = get_json(url)
            raw = canonical(payload)
            path = raw_dir/f'{market}-{kind}.json'
            path.write_text(raw+'\n')
            source_time = payload.get('serverTime')
            if source_time:
                age = datetime.fromisoformat(received)-datetime.fromisoformat(source_time.replace('Z','+00:00'))
                if abs(age.total_seconds()) > 120:
                    raise ValueError('Source clock stale or inconsistent')
            return (job,payload,received,source_time,hashlib.sha256(raw.encode()).hexdigest(),str(path.relative_to(state)),None)
        except Exception as exc:
            return (job,None,now(),None,None,None,f'{type(exc).__name__}: {exc}')
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(run, jobs))
    for (market,kind,url),payload,received,source_time,sha,path,error in results:
        db.execute('''INSERT INTO sources(cycle_id,market,kind,url,received_at,source_time,
                      historical_availability,sha256,raw_path,status,error) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                   (cycle_id,market,kind,url,received,source_time,
                    'archived on receipt; original historical publication timestamps unverified',
                    sha,path,'ERROR' if error else 'RECEIVED',error))
        if error:
            issues.append(f'{market}/{kind}: {error}')
        else:
            data[(market,kind)] = payload
    for kind,reason in [('on-chain-flows','No timestamped, entity-labeled multi-chain flow provider connected'),
                        ('macro-calendar','No parsed, verified release-time calendar connected; official links in handbook only'),
                        ('historical-orderbook','Current snapshots only; no point-in-time depth archive'),
                        ('publication-time','Historical data downloaded now cannot certify original availability or revisions')]:
        db.execute('''INSERT INTO sources(cycle_id,market,kind,received_at,historical_availability,status,error)
                      VALUES(?,?,?,?,?,?,?)''',(cycle_id,'_ALL',kind,now(),'unknown','GAP',reason))
        issues.append(kind+': '+reason)
    db.commit()
    return data,issues


def funding_rates(payload, asof):
    result={}
    for r in payload['rates']:
        t=int(datetime.fromisoformat(r['timestamp'].replace('Z','+00:00')).timestamp())
        if t > asof:
            continue
        if t in result:
            raise ValueError('Duplicate funding timestamp')
        value=float(r['fundingRate'])
        if not __import__('math').isfinite(value):
            raise ValueError('Non-finite funding')
        result[t]=value
    return result


def complete_development_prefix(candles, rates):
    """Data-integrity rule, independent of returns; no selective trade deletion.

    Stop at the first incomplete funding bar, do not resume after it. Retain the
    exclusion record in the brief so this cannot masquerade as full coverage.
    """
    for i,c in enumerate(candles):
        missing=[h for h in range(c['time'],c['time']+14400,3600) if h not in rates]
        if missing:
            if i < 100:
                raise ValueError('First funding gap leaves fewer than 100 complete development bars')
            return candles[:i], {'first_missing_hour_utc':datetime.fromtimestamp(missing[0],timezone.utc).isoformat(),
                                 'excluded_bars':len(candles)-i,'policy':'stop whole replay before first incomplete funding bar; no rate imputation'}
    return candles,None


def summarize_market(ticker, book, candles, asof):
    bids=[(float(p),float(q)) for p,q in book['orderBook']['bids'] if float(q)>0]
    asks=[(float(p),float(q)) for p,q in book['orderBook']['asks'] if float(q)>0]
    bid=max(p for p,q in bids); ask=min(p for p,q in asks)
    if bid <= 0 or ask <= bid:
        raise ValueError('Invalid or crossed book')
    mid=(bid+ask)/2
    recent=candles[-1]
    average=sum(c['close'] for c in candles[-50:])/50
    regime='uptrend' if recent['close']>average*1.005 else ('downtrend' if recent['close']<average*.995 else 'range')
    return {'mark':ticker['markPrice'], 'mark_observed_at':now(),
            'ticker_last_trade_time':ticker.get('lastTime'),
            'price_change_24h_pct':ticker.get('change24h'),
            'relative_funding_hourly':ticker.get('relativeFundingRate'),
            'open_interest_base_units':ticker.get('openInterest'),
            'spread_bps':(ask-bid)/mid*10000,
            'bid_depth_10bps_usd':sum(p*q for p,q in bids if p>=mid*.999),
            'ask_depth_10bps_usd':sum(p*q for p,q in asks if p<=mid*1.001),
            'regime':regime,'regime_method':'last completed 4h close vs 50-bar SMA +/-0.5%',
            'last_candle_end':datetime.fromtimestamp(recent['time']+14400,timezone.utc).isoformat(),
            'completed_candles':len(candles),'observed_asof':datetime.fromtimestamp(asof,timezone.utc).isoformat()}


def export(db,state,cycle_id,markets,issues,config):
    registry=[dict(r) for r in db.execute('SELECT * FROM strategies ORDER BY id,version')]
    history=[dict(r) for r in db.execute('SELECT * FROM history ORDER BY seq')]
    graveyard=[dict(r) for r in db.execute('SELECT * FROM graveyard ORDER BY id')]
    experiments=[dict(r) for r in db.execute('SELECT * FROM experiments ORDER BY started_at,id')]
    for name,value in [('registry.json',registry),('history.json',history),('graveyard.json',graveyard)]:
        (state/name).write_text(json.dumps(value,indent=2)+'\n')
    active=[r for r in registry if r['status']!='RETIRED']
    stamp=now()
    brief={'cycle_id':cycle_id,'generated_at_utc':stamp,
           'generated_at_chicago':datetime.fromisoformat(stamp).astimezone(ZoneInfo(config['timezone'])).isoformat(),
           'mode':'PAPER RESEARCH; no eligible strategies or positions',
           'market_regimes':markets,'data_gaps':issues,'registry_count':len(registry),
           'tests_run_this_cycle':db.execute('SELECT count(*) FROM experiments WHERE cycle_id=?',(cycle_id,)).fetchone()[0],
           'lifetime_development_tests':len(experiments),'unseen_validation_tests':0,
           'strategies':[{k:r[k] for k in ('id','version','status','reason')} for r in registry],
           'paper':{'initial_equity_usd':10000,'equity_usd':10000,'pnl_usd':0,'fills':0,'open_risk_usd':0,'gross_leverage':0},
           'live':{'connected':False,'pnl_usd':None},
           'decisions_needed':['Confirm $10,000 simulated capital and execution venue/account eligibility before any exchange integration.',
                               'Identify Grok Bot product/API if Grok integration is desired; current runtime is deterministic Python on GitHub Actions.'],
           'next_action':'Archive point-in-time inputs and build independent validation; never bypass the 300-trade or 21-day paper gates.',
           'operational_limitations':['Promotion and execution intentionally locked in this pilot.',
                                      'No on-chain flow provider or verified macro-calendar parser.',
                                      'Scheduled jobs are periodic and may be delayed, not an always-on execution server.']}
    (state/'brief.json').write_text(json.dumps(brief,indent=2)+'\n')
    lines=['# Quant desk daily brief', '',f'Generated: {brief["generated_at_chicago"]} (America/Chicago)',
           '', '**PAPER RESEARCH. Live execution disconnected.**', '',
           '| Market | Mark USD | Regime | Spread bps | Completed 4h bars |',
           '|---|---:|---|---:|---:|']
    for m,s in markets.items():
        lines.append(f'| {m} | {s["mark"]:,.2f} | {s["regime"]} | {s["spread_bps"]:.2f} | {s["completed_candles"]} |')
    lines += ['', '| Strategy | Version | Status | Reason |', '|---|---:|---|---|']
    for r in registry:
        lines.append(f'| {r["id"]} | {r["version"]} | {r["status"]} | {r["reason"]} |')
    lines += ['',f'Development tests this cycle: {brief["tests_run_this_cycle"]}. Total unique tests: {len(experiments)}. Unseen validation tests: 0.',
              '', '| Strategy | BACKTEST development net P&L | Net PF | Closed trades | PAPER P&L | LIVE P&L |',
              '|---|---:|---:|---:|---:|---|']
    for r in registry:
        base=next((json.loads(e['result_json']) for e in reversed(experiments)
                   if e['strategy_id']==r['id'] and json.loads(e['parameters_json'])==json.loads(r['spec_json'])['parameters']),None)
        if base:
            pf=f'{base["profit_factor"]:.3f}' if base['profit_factor'] is not None else 'N/A'
            lines.append(f'| {r["id"]} | ${base["net_pnl_usd"]:.2f} | {pf} | {base["closed_trades"]} | $0.00 (no fills) | N/A |')
    lines += ['', 'Backtests are independent exploratory replays on reused development data. Do not add their P&L together or treat it as paper performance.',
              '', 'Paper cash/equity: **$10,000 / $10,000**. Paper P&L: **$0.00**, fills: **0**. Open risk: **$0**, leverage: **0x**.',
              '', 'No strategy has earned VALIDATED or PAPER status. No 21-day strategy paper clock has started.',
              '', '## Data gaps / blocks', '']
    lines.extend('- '+x for x in issues)
    lines += ['', '## Decisions / next actions','']
    lines.extend('- '+x for x in brief['decisions_needed'])
    lines += ['',brief['next_action'],'']
    (state/'brief.md').write_text('\n'.join(lines))
    (state/'registry.md').write_text('\n'.join(lines[lines.index('| Strategy | Version | Status | Reason |'):lines.index(f'Development tests this cycle: {brief["tests_run_this_cycle"]}. Total unique tests: {len(experiments)}. Unseen validation tests: 0.')])+'\n')
    return brief


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--state', type=Path, required=True)
    args=parser.parse_args()
    state=args.state.resolve();state.mkdir(parents=True,exist_ok=True)
    config=json.loads((ROOT/'config.json').read_text())
    if config['live_enabled'] or config['execution_venue'] is not None or config['mode']!='paper-research':
        raise SystemExit('Paper-only invariant violated')
    db=database(state/'desk.sqlite3')
    # This pilot never creates positions; never quietly reset an externally edited ledger.
    if db.execute('SELECT count(*) FROM paper_fills').fetchone()[0]:
        raise SystemExit('Unexpected fills in research-only pilot: operator review required')
    stamp=now();cycle_id=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+str(os.getpid())
    try:
        sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    except Exception:
        sha='unknown'
    code_manifest={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in sorted(ROOT.rglob('*')) if p.is_file() and p.suffix in ('.py','.sql','.json')}
    code_hash=digest(code_manifest)
    (state/'code-manifest.json').write_text(json.dumps({'git_head':sha,'source_sha256':code_hash,'files':code_manifest},indent=2)+'\n')
    db.execute('INSERT INTO cycles(id,started_at,code_sha,config_sha,mode) VALUES(?,?,?,?,?)',
               (cycle_id,stamp,code_hash,digest(config),'paper-research'));db.commit()
    # Reuse specs frozen in the first cycle; expiry cannot silently slide forward.
    for market,symbol in config['markets'].items():
        old=db.execute('SELECT spec_json FROM strategies WHERE id=? AND version=1',(f'TREND24-{market}',)).fetchone()
        spec=json.loads(old[0]) if old else make_spec(market,symbol,stamp,(datetime.fromisoformat(stamp)+timedelta(days=30)).isoformat())
        register(db,spec)
        (state/f'spec-{market}.json').write_text(json.dumps(spec,indent=2)+'\n')
    asof=int(time.time())
    data,issues=collect(db,cycle_id,state,config,asof)
    tickers={t['symbol']:t for t in data.get(('_ALL','tickers'),{}).get('tickers',[])}
    market_summary={}
    stage_decisions=[]
    for market,symbol in config['markets'].items():
        sid=f'TREND24-{market}'
        try:
            candles=normalized_candles(data[(market,'candles')],asof)
            if asof-(candles[-1]['time']+14400)>14400+300:
                raise ValueError('Stale completed candles')
            market_summary[market]=summarize_market(tickers[symbol],data[(market,'book')],candles,asof)
            row=dict(db.execute('SELECT * FROM strategies WHERE id=? AND version=1',(sid,)).fetchone())
            if row['status']=='RETIRED':
                continue
            if datetime.fromisoformat(json.loads(row['spec_json'])['expiry_date']) <= datetime.now(timezone.utc):
                transition(db,sid,1,'RETIRED','Auditor','Spec expired; no automatic renewal')
                continue
            # Never retest development parameters on each new scan: that would
            # quietly turn forward observations into another optimization loop.
            old_count=db.execute('SELECT count(*) FROM experiments WHERE strategy_id=? AND version=1',(sid,)).fetchone()[0]
            if old_count == len(list(variants())):
                continue
            if old_count:
                raise ValueError('Incomplete experiment batch: independent review required before resuming')
            rates=funding_rates(data[(market,'funding')],asof)
            candles, exclusion=complete_development_prefix(candles,rates)
            if exclusion:
                issues.append(f'{market}/development coverage: {canonical(exclusion)}')
            dataset_hash=digest({'candles':candles,'funding':rates})
            outcomes=[]
            # Check graveyard again immediately before testing (initial batch was preregistered).
            for variant,params in variants():
                experiment_id=digest({'strategy':sid,'version':1,'split':'development','parameters':params,'data':dataset_hash})
                result=replay(candles,rates,params,
                              config['development']['fee_bps_per_side_assumed'],
                              config['development']['slippage_bps_per_side_assumed'])
                result_path=state/'experiments'/f'{experiment_id}.json'
                result_path.parent.mkdir(exist_ok=True)
                result_path.write_text(json.dumps(result,indent=2)+'\n')
                summary={k:v for k,v in result.items() if k not in ('trades','equity_curve')}
                summary.update(variant=variant, artifact_path=str(result_path.relative_to(state)),
                               code_sha256=code_hash,coverage_exclusion=exclusion,
                               first_bar_utc=datetime.fromtimestamp(candles[0]['time'],timezone.utc).isoformat(),
                               last_bar_end_utc=datetime.fromtimestamp(candles[-1]['time']+14400,timezone.utc).isoformat())
                db.execute('INSERT INTO experiments VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (experiment_id,cycle_id,sid,1,'development',canonical(params),row['spec_sha256'],
                            dataset_hash,now(),canonical(summary)))
                outcomes.append(summary)
            base=outcomes[0]
            # Development gate is separate from the much stricter VALIDATED gate.
            # Insufficient sample = waiting, never a pass; a measured loss = reject.
            if base['net_pnl_usd'] <= 0 or (base['profit_factor'] is not None and base['profit_factor'] <= 1):
                stage_decisions.append((sid,'RETIRED','Development falsification: nonpositive net expectancy / PF <= 1 after modeled costs',base))
            elif base['closed_trades'] < 20 or base['profit_factor'] is None:
                db.execute('UPDATE strategies SET reason=? WHERE id=? AND version=1',
                           ('Waiting: development sample <20 closed trades or PF undefined; no validation attempted',sid))
            else:
                stage_decisions.append((sid,'BACKTESTED',
                           'Development replay positive with >=20 trades; VALIDATED blocked: 0 certified unseen trades, PIT/depth/macro gaps and independent review missing',base))
            db.commit()
        except Exception as exc:
            issues.append(f'{market}/research blocked: {type(exc).__name__}: {exc}')
            db.execute("UPDATE strategies SET reason=? WHERE id=? AND version=1 AND status='SPEC'",
                       ('Blocked by data/operating error: '+str(exc),sid))
            db.commit()
    # All symbols and variants were registered before this batch. Apply its
    # decisions together, then prohibit future tests of retired mechanisms.
    for sid,target,reason,evidence in stage_decisions:
        transition(db,sid,1,target,'Validator',reason,evidence)
    db.commit()
    brief=export(db,state,cycle_id,market_summary,issues,config)
    db.execute('INSERT INTO portfolio_snapshots VALUES(?,?,?,?,?,?,?,?,?,?)',
               (cycle_id,now(),'PAPER',10000,0,0,0,0,'RESEARCH_ONLY',canonical({'fills':0,'execution_locked':True})))
    db.execute('UPDATE cycles SET completed_at=?,health=?,summary_json=? WHERE id=?',
               (now(),'BLOCKED' if issues else 'OK',canonical(brief),cycle_id));db.commit()
    (state/'state-marker.json').write_text(json.dumps({'schema_version':1,'mode':'paper-research','last_cycle':cycle_id})+'\n')
    print((state/'brief.md').read_text())


if __name__=='__main__':
    main()
