#!/usr/bin/env python3
"""Paper-only crypto strategy runner. No wallet, keys, or order endpoints."""
import argparse, datetime as dt, json, math, os, sqlite3, time, urllib.request
from pathlib import Path
DB=Path(os.environ.get('PAPER_DB',Path(__file__).with_name('trial.sqlite3')))
FEE=.001; SLIP={'new':.015,'established':.002}; CAPITAL=50.0
AGENTS=['scanner','rug_filter','sizer','executor','logger','equity']
ESTABLISHED_SYMBOLS=('BTC','ETH','SOL','LINK','AVAX')
def now(): return dt.datetime.now(dt.timezone.utc).isoformat()
def fetch(url):
 req=urllib.request.Request(url,headers={'User-Agent':'PaperTrialResearch/1.0','Accept':'application/json'})
 with urllib.request.urlopen(req,timeout=8) as r:return json.load(r)
def conn():
 c=sqlite3.connect(DB);c.row_factory=sqlite3.Row
 c.executescript('''CREATE TABLE IF NOT EXISTS portfolios(strategy TEXT PRIMARY KEY,cash REAL NOT NULL,initial REAL NOT NULL);
 CREATE TABLE IF NOT EXISTS positions(strategy TEXT,symbol TEXT,qty REAL,entry REAL,opened TEXT,peak REAL,PRIMARY KEY(strategy,symbol));
 CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY AUTOINCREMENT,ts TEXT,strategy TEXT,symbol TEXT,side TEXT,qty REAL,price REAL,fee REAL,slippage REAL,reason TEXT,realized REAL);
 CREATE TABLE IF NOT EXISTS signals(id INTEGER PRIMARY KEY AUTOINCREMENT,ts TEXT,strategy TEXT,symbol TEXT,decision TEXT,reason TEXT);
 CREATE TABLE IF NOT EXISTS snapshots(id INTEGER PRIMARY KEY AUTOINCREMENT,ts TEXT,strategy TEXT,equity REAL,cash REAL,positions_value REAL);
 CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,ts TEXT,agent TEXT,strategy TEXT,message TEXT);
 CREATE TABLE IF NOT EXISTS prices(strategy TEXT,symbol TEXT,price REAL,ts TEXT,PRIMARY KEY(strategy,symbol));''')
 for s in ('new','established'):c.execute('INSERT OR IGNORE INTO portfolios VALUES(?,?,?)',(s,CAPITAL,CAPITAL))
 c.commit();return c
def event(c,a,s,msg):c.execute('INSERT INTO events(ts,agent,strategy,message) VALUES(?,?,?,?)',(now(),a,s,str(msg)[:500]))
def signal(c,s,sym,decision,reason):c.execute('INSERT INTO signals(ts,strategy,symbol,decision,reason) VALUES(?,?,?,?,?)',(now(),s,sym,decision,reason[:500]));event(c,'logger',s,f'{sym}: {decision}: {reason}')
def coingecko_markets():
 # CoinGecko market feed. Price change over 24h is a coarse momentum input, not a backtest.
 url='https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&ids=bitcoin,ethereum,solana,chainlink,avalanche-2&price_change_percentage=24h'
 data=fetch(url);out=[]
 if not isinstance(data,list) or {d['symbol'].upper() for d in data}!=set(ESTABLISHED_SYMBOLS):
  raise ValueError('CoinGecko did not return all five markets')
 for d in data:
  p=d.get('current_price');ch=d.get('price_change_percentage_24h')
  volume=float(d['total_volume'])
  if not p or float(p)<=0 or ch is None or not all(map(math.isfinite,(float(p),float(ch),volume))):
   raise ValueError(f"Invalid CoinGecko market for {d['symbol']}")
  out.append(dict(symbol=d['symbol'].upper(),price=float(p),change=float(ch),volume=volume))
 return out
def established():
 try:
  return coingecko_markets(),'CoinGecko'
 except Exception as primary_error:
  out=[]
  for symbol in ESTABLISHED_SYMBOLS:
   stats=fetch(f'https://api.exchange.coinbase.com/products/{symbol}-USD/stats')
   price,opening,base_volume=(float(stats[k]) for k in ('last','open','volume'))
   if not all(map(math.isfinite,(price,opening,base_volume))) or min(price,opening)<=0 or base_volume<0:
    raise ValueError(f'Invalid Coinbase market for {symbol}')
   out.append(dict(symbol=symbol,price=price,change=(price/opening-1)*100,volume=base_volume*price))
  return out,f'Coinbase Exchange fallback (CoinGecko: {primary_error})'
def newtokens():
 # DexScreener latest profiles -> token pairs; public APIs can rate-limit or change.
 profiles=fetch('https://api.dexscreener.com/token-profiles/latest/v1')
 if not isinstance(profiles,list):
  raise ValueError('Invalid DexScreener profiles response')
 out=[];seen=set();pair_errors=0;pair_responses=0
 for profile in profiles[:35]:
  chain=profile.get('chainId');addr=profile.get('tokenAddress')
  if not chain or not addr:continue
  try:pairs=fetch(f'https://api.dexscreener.com/token-pairs/v1/{chain}/{addr}')
  except Exception:
   pair_errors+=1
   continue
  if not isinstance(pairs,list):
   pair_errors+=1
   continue
  pair_responses+=1
  for p in sorted(pairs,key=lambda x:float((x.get('liquidity') or {}).get('usd') or 0),reverse=True)[:1]:
   key=p.get('pairAddress');created=p.get('pairCreatedAt');base=p.get('baseToken') or {}
   if not key or key in seen:continue
   seen.add(key)
   try:
    age=(time.time()*1000-float(created))/3600000 if created else 9999
    out.append(dict(symbol=f"{base.get('symbol','?')}:{chain}:{key[:8]}",price=float(p.get('priceUsd') or 0),change=float((p.get('priceChange') or {}).get('h1') or 0),volume=float((p.get('volume') or {}).get('h24') or 0),liquidity=float((p.get('liquidity') or {}).get('usd') or 0),age=age,concentration=None,chain=chain))
   except (TypeError,ValueError):continue
 if profiles and not pair_responses:
  raise RuntimeError(f'No usable DexScreener pair responses ({pair_errors} errors)')
 return out,f'DexScreener ({pair_responses} pair responses, {pair_errors} errors)'
def filter_token(t):
 # Fail closed: public DexScreener metadata does not verify contract permissions, sellability or holder concentration.
 if t['price']<=0:return False,'No valid USD price'
 if t['age']<1 or t['age']>48:return False,'Outside 1–48h age window'
 if t['liquidity']<100000:return False,'Liquidity below $100,000'
 if t['volume']<100000:return False,'24h volume below $100,000'
 if t['concentration'] is None:return False,'BLOCKED: holder concentration and contract/sellability checks unavailable'
 return True,'Token passed all checks'
def getpositions(c,s):return list(c.execute('SELECT * FROM positions WHERE strategy=?',(s,)))
def cash(c,s):return c.execute('SELECT cash FROM portfolios WHERE strategy=?',(s,)).fetchone()['cash']
def trade(c,s,sym,side,mark,reason,qty=None):
 slip=SLIP[s];price=mark*(1+slip if side=='BUY' else 1-slip)
 if side=='BUY':
  if len(getpositions(c,s))>=2:return
  budget=min(cash(c,s)*.20,10.0)
  qty=budget/(price*(1+FEE));cost=qty*price;fee=cost*FEE
  if qty<=0 or cost+fee>cash(c,s):return
  c.execute('UPDATE portfolios SET cash=cash-? WHERE strategy=?',(cost+fee,s))
  c.execute('INSERT INTO positions VALUES(?,?,?,?,?,?)',(s,sym,qty,price,now(),mark))
  realized=0
 else:
  pos=c.execute('SELECT * FROM positions WHERE strategy=? AND symbol=?',(s,sym)).fetchone()
  if not pos:return
  qty=pos['qty'];proceeds=qty*price;fee=proceeds*FEE
  c.execute('UPDATE portfolios SET cash=cash+? WHERE strategy=?',(proceeds-fee,s))
  c.execute('DELETE FROM positions WHERE strategy=? AND symbol=?',(s,sym))
  realized=(price-pos['entry'])*qty-fee-pos['entry']*qty*FEE
 c.execute('INSERT INTO trades(ts,strategy,symbol,side,qty,price,fee,slippage,reason,realized) VALUES(?,?,?,?,?,?,?,?,?,?)',(now(),s,sym,side,qty,price,fee,slip,reason,realized))
 event(c,'executor',s,f'PAPER {side} {sym}: qty={qty:.7f}, fill={price:.6f}, fee={fee:.5f}, reason={reason}')
def cycle(c,s,feed):
 candidates,source=feed()
 event(c,'scanner',s,f'Received {len(candidates)} market observations from {source}')
 observed={t['symbol']:t for t in candidates}
 for t in candidates:
  c.execute('INSERT OR REPLACE INTO prices VALUES(?,?,?,?)',(s,t['symbol'],t['price'],now()))
 for pos in getpositions(c,s):
  t=observed.get(pos['symbol'])
  if not t:continue
  mark=t['price'];peak=max(pos['peak'],mark)
  c.execute('UPDATE positions SET peak=? WHERE strategy=? AND symbol=?',(peak,s,pos['symbol']))
  ret=mark/pos['entry']-1
  if ret<=-.06:trade(c,s,pos['symbol'],'SELL',mark,'Stop loss at -6%')
  elif ret>=.12:trade(c,s,pos['symbol'],'SELL',mark,'Take profit at +12%')
  elif mark/peak-1<=-.05:trade(c,s,pos['symbol'],'SELL',mark,'Trailing stop -5% from peak')
  elif (dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(pos['opened'])).total_seconds()>86400:trade(c,s,pos['symbol'],'SELL',mark,'24h time exit')
 if s=='new':
  for t in candidates[:20]:
   ok,why=filter_token(t);signal(c,s,t['symbol'],'PASS' if ok else 'REJECT',why)
   if ok and t['change']>8 and t['symbol'] not in [p['symbol'] for p in getpositions(c,s)]:trade(c,s,t['symbol'],'BUY',t['price'],'Screened new-token momentum')
 else:
  for t in candidates:
   if t['symbol'] in [p['symbol'] for p in getpositions(c,s)]:continue
   if t['volume']<=10000000:
    signal(c,s,t['symbol'],'SKIP','24h USD volume at or below $10 million')
   elif 1.0<=t['change']<=8.0:
    signal(c,s,t['symbol'],'BUY_SIGNAL',f'24h momentum {t["change"]:.2f}%')
    trade(c,s,t['symbol'],'BUY',t['price'],f'24h momentum {t["change"]:.2f}%')
   else:signal(c,s,t['symbol'],'SKIP',f'24h momentum {t["change"]:.2f}% outside 1–8%')
 pv=0
 for p in getpositions(c,s):
  row=c.execute('SELECT price FROM prices WHERE strategy=? AND symbol=?',(s,p['symbol'])).fetchone()
  pv+=p['qty']*(row['price'] if row else p['entry'])
 balance=cash(c,s)
 c.execute('INSERT INTO snapshots(ts,strategy,equity,cash,positions_value) VALUES(?,?,?,?,?)',(now(),s,balance+pv,balance,pv))
 event(c,'equity',s,f'Equity ${balance+pv:.4f}; cash ${balance:.4f}; open positions {len(getpositions(c,s))}')
 c.commit()
 print(f'{s}: {len(candidates)} observations from {source}; equity ${balance+pv:.4f}',flush=True)
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--once',action='store_true');ap.add_argument('--interval',type=int,default=300);args=ap.parse_args()
 c=conn();print('PAPER ONLY; NO ORDER EXECUTION; database:',DB,flush=True)
 while True:
  for s,feed in [('established',established),('new',newtokens)]:
   try:cycle(c,s,feed)
   except Exception as ex:
    c.rollback()
    event(c,'scanner',s,'DATA ERROR: '+repr(ex))
    c.commit()
    print(s,'error',ex,flush=True)
  if args.once:break
  time.sleep(max(60,args.interval))
if __name__=='__main__':main()
