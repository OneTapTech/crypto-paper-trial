#!/usr/bin/env python3
"""Read-only trial status; exits nonzero if feeds are stale or missing."""
import datetime as dt, os, sqlite3, sys
from pathlib import Path
p=Path(os.environ.get('PAPER_DB',Path(__file__).with_name('trial.sqlite3')))
if not p.exists(): sys.exit('FAIL: database missing')
c=sqlite3.connect(p.resolve().as_uri()+'?mode=ro',uri=True); c.row_factory=sqlite3.Row
if c.execute('PRAGMA quick_check').fetchone()[0]!='ok':
    sys.exit('FAIL: SQLite integrity check')
failures=[]
for s in ('established','new'):
    a=c.execute('SELECT initial FROM portfolios WHERE strategy=?',(s,)).fetchone()
    e=c.execute('SELECT ts,message FROM events WHERE strategy=? AND agent=? ORDER BY id DESC LIMIT 1',(s,'scanner')).fetchone()
    x=c.execute('SELECT ts FROM snapshots WHERE strategy=? ORDER BY id DESC LIMIT 1',(s,)).fetchone()
    print(f'{s}: portfolio={bool(a)}, last scanner={dict(e) if e else None}, last snapshot={dict(x) if x else None}')
    if not a or a['initial']!=50 or not x:
        failures.append(f'{s}: missing $50 portfolio or successful cycle')
        continue
    age=(dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(x['ts'])).total_seconds()
    if age>900:
        failures.append(f'{s}: snapshot stale ({age:.0f}s)')
    if not e or e['message'].startswith('DATA ERROR:'):
        failures.append(f'{s}: scanner error')
if failures:
    sys.exit('FAIL: '+'; '.join(failures))
print('PASS: both $50 portfolios have recent snapshots and successful scanner cycles')
