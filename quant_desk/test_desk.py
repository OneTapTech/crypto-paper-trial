import json
import math
import sqlite3
import tempfile
import unittest
from pathlib import Path

from quant_desk.governance import (database, digest, register, transition,
                                   validation_failures, size_quantity, risk_preflight)
from quant_desk.research import make_spec, normalized_candles, replay, DEFAULTS
from quant_desk.runner import complete_development_prefix


class GovernanceTests(unittest.TestCase):
    def setUp(self):
        self.db=database(':memory:')
        self.spec=make_spec('BTC','PF_XBTUSD','2026-11-08T08:00:00+00:00','2026-12-08T08:00:00+00:00')
        register(self.db,self.spec)

    def test_spec_cannot_be_changed(self):
        modified=json.loads(json.dumps(self.spec)); modified['stop']['distance']='0.01'
        with self.assertRaises(ValueError): register(self.db,modified)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE strategies SET spec_json='{}'")

    def test_no_skip_or_unverified_promotion(self):
        with self.assertRaises(ValueError): transition(self.db,self.spec['id'],1,'PAPER','Engineer','skip')
        transition(self.db,self.spec['id'],1,'BACKTESTED','Validator','mechanical dev gate',{})
        with self.assertRaises(ValueError): transition(self.db,self.spec['id'],1,'VALIDATED','Validator','fake evidence',{})

    def test_retirement_and_alias_veto(self):
        transition(self.db,self.spec['id'],1,'RETIRED','Validator','failed development',{'pnl':-5})
        alias=dict(self.spec,id='New Name')
        row=register(self.db,alias)
        self.assertEqual(row['status'],'RETIRED')
        self.assertIn('Graveyard veto',row['reason'])
        with self.assertRaises(ValueError): transition(self.db,self.spec['id'],1,'SPEC','Analyst','resurrection')

    def test_history_hash_chain(self):
        previous='0'*64
        for row in self.db.execute('SELECT * FROM history ORDER BY seq'):
            row=dict(row);row.pop('seq');event_hash=row.pop('event_hash')
            self.assertEqual(row['previous_hash'],previous)
            self.assertEqual(digest(row),event_hash)
            previous=event_hash

    def test_missing_evidence_never_passes(self):
        failures=validation_failures({})
        self.assertIn('at least 300 unseen closed trades per spec version',failures)
        self.assertIn('net profit factor > 1.25',failures)

    def test_thresholds_and_nonfinite_evidence(self):
        self.assertIn('net profit factor > 1.25',validation_failures({'net_profit_factor':1.25}))
        self.assertIn('net profit factor > 1.25',validation_failures({'net_profit_factor':math.inf}))
        self.assertIn('at least 300 unseen closed trades per spec version',validation_failures({'unseen_closed_trades':True}))

    def test_sizing_counts_costs_and_fixed_budget(self):
        qty=size_quantity(10000,100,98,6,5,0)
        self.assertAlmostEqual(qty*(2+.22),50)
        self.assertEqual(size_quantity(10000,100,98,6,5,0,existing_strategy_risk=75),0)
        qty=size_quantity(10000,100,98,6,5,0,gross_notional=29950)
        self.assertLessEqual(qty,.5)

    def test_risk_breakers(self):
        def pre(**kwargs):
            args=dict(equity=10000,day_start=10000,high_water=10000,open_risk=0,
                      gross_notional=0,strategy_risk=0,correlations=[],fresh=True)
            args.update(kwargs);return risk_preflight(**args)
        self.assertEqual(pre(),[])
        self.assertEqual(pre(open_risk=200,strategy_risk=50),[])
        self.assertTrue(pre(open_risk=150,strategy_risk=50))
        cases=[{'equity':9800},{'high_water':12000},{'open_risk':300},
               {'gross_notional':30001},{'strategy_risk':75.01},
               {'correlations':[.7,.8]},{'correlations':[None]},
               {'fresh':False},{'costs_multiple':2},{'bad_trade_count':40},
               {'regime_on':False},{'strategy_dd':.16,'backtest_dd':.1},
               {'equity':float('nan')}]
        for case in cases:
            with self.subTest(case=case): self.assertTrue(pre(**case))


class ReplayTests(unittest.TestCase):
    def test_coverage_policy_never_resumes_after_a_gap(self):
        rows=[{'time':i*14400} for i in range(150)]
        rates={h:.01 for h in range(0,150*14400,3600)}
        rates.pop(120*14400+3600)
        retained,exclusion=complete_development_prefix(rows,rates)
        self.assertEqual(len(retained),120)
        self.assertEqual(exclusion['excluded_bars'],30)
    def data(self):
        rows=[]
        for i in range(100):
            # After a long warmup, force a volume-confirmed upward breakout.
            op=100 if i<60 else 110
            close=110 if i==60 else op
            rows.append(dict(time=i*14400,open=op,high=max(op,close)+1,
                             low=min(op,close)-1,close=close,volume=100 if i!=60 else 1000))
        rates={h:.001 for h in range(-86400,100*14400,3600)}
        return rows,rates

    def test_future_entry_and_conservative_stop(self):
        rows,rates=self.data()
        # Entry next bar; both barriers hit, choose stop; adverse gap at 50.
        rows[61].update(open=50,low=1,high=200,close=110)
        result=replay(rows,rates)
        trade=result['trades'][0]
        self.assertEqual(trade['entry_index'],trade['signal_index']+1)
        self.assertEqual(trade['signal_index'],60)
        self.assertEqual(trade['reason'],'stop')
        self.assertGreater(trade['fees'],0)
        self.assertGreater(trade['funding'],0)
        self.assertEqual(result['unseen_trades'],0)

    def test_funding_gap_blocks_instead_of_dropping_trade(self):
        rows,rates=self.data();rates.pop(61*14400)
        with self.assertRaises(ValueError): replay(rows,rates)

    def test_unknown_future_funding_cannot_change_entry_sizing(self):
        rows,rates=self.data()
        a=replay(rows,rates)['trades'][0]
        changed=dict(rates);changed[61*14400]=1000
        b=replay(rows,changed)['trades'][0]
        self.assertEqual(a['quantity'],b['quantity'])
        self.assertLess(b['net_pnl'],a['net_pnl'])

    def test_future_prices_do_not_change_earlier_decisions(self):
        rows,rates=self.data()
        a=replay(rows,rates)['trades'][0]
        changed=[dict(c) for c in rows]
        changed[-1].update(open=900,high=1000,low=850,close=950)
        b=replay(changed,rates)['trades'][0]
        self.assertEqual(a,b)

    def test_incomplete_duplicate_and_gapped_candles(self):
        rows,_=self.data()
        payload={'candles':rows}
        payload={'candles':[{**c,'time':c['time']*1000} for c in rows]}
        # Include 100 complete bars plus an unfinished bar.
        extra={**payload['candles'][-1],'time':100*14400*1000}
        clean=normalized_candles({'candles':payload['candles']+[extra]},100*14400+10)
        self.assertEqual(len(clean),100)
        with self.assertRaises(ValueError): normalized_candles({'candles':payload['candles']+[extra,extra]},101*14400)
        with self.assertRaises(ValueError): normalized_candles({'candles':payload['candles'][:50]+payload['candles'][51:]+[extra]},101*14400)


if __name__=='__main__': unittest.main()
