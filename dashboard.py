#!/usr/bin/env python3
"""Read-only local dashboard and portable HTML report for the paper trial."""
import argparse
from contextlib import closing
from http.server import BaseHTTPRequestHandler,HTTPServer
from urllib.parse import urlparse
import json,sqlite3,os,html
from pathlib import Path
DB=Path(os.environ.get('PAPER_DB',Path(__file__).with_name('trial.sqlite3')))
HTML='''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Crypto Paper Trial</title><style>body{background:#0d1421;color:#e6edf7;font:15px system-ui;margin:0;padding:25px;max-width:1100px;margin:auto}h1{margin-bottom:0}.sub{color:#9fb0c8}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px;margin:20px 0}.card{background:#192437;border:1px solid #34445d;border-radius:12px;padding:18px}.big{font-size:29px;font-weight:700}table{border-collapse:collapse;width:100%;font-size:13px}th,td{border-bottom:1px solid #34445d;padding:9px;text-align:left}th{color:#9fb0c8}section{overflow-x:auto;margin:24px 0}canvas{width:100%;height:210px;background:#192437;border-radius:12px}small{color:#9fb0c8}.warn{color:#ffc676}</style></head><body><h1>Six-Agent Crypto Paper Trial</h1><p class="sub">SIMULATION ONLY · No broker or wallet connectivity · Each strategy starts with $50</p><p class="warn" id="status">Loading data...</p><div class="grid" id="cards"></div><h2>Equity history</h2><canvas id="chart" width="900" height="230"></canvas><section><h2>Positions</h2><table><thead><tr><th>Strategy</th><th>Token</th><th>Qty</th><th>Entry</th><th>Latest mark</th></tr></thead><tbody id="positions"></tbody></table></section><section><h2>Paper trades</h2><table><thead><tr><th>UTC</th><th>Strategy</th><th>Side</th><th>Token</th><th>Fill</th><th>Fee</th><th>Realized P/L</th><th>Reason</th></tr></thead><tbody id="trades"></tbody></table></section><section><h2>Agent activity</h2><table><thead><tr><th>UTC</th><th>Agent</th><th>Strategy</th><th>Message</th></tr></thead><tbody id="events"></tbody></table></section><script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money=n=>'$'+Number(n||0).toFixed(2);
function rows(id,arr,keys){document.getElementById(id).innerHTML=arr.map(r=>'<tr>'+keys.map(k=>'<td>'+esc(typeof k==='function'?k(r):r[k])+'</td>').join('')+'</tr>').join('')||'<tr><td colspan="9">No records yet</td></tr>'}
function draw(snaps){const c=document.getElementById('chart'),ctx=c.getContext('2d');ctx.clearRect(0,0,c.width,c.height);const groups=['established','new'],colors=['#6aa9ff','#ffc16a'];groups.forEach((g,i)=>{const a=snaps.filter(x=>x.strategy===g);if(!a.length)return;const values=a.map(x=>Number(x.equity));const min=Math.min(40,...values)-2,max=Math.max(60,...values)+2;ctx.strokeStyle=colors[i];ctx.lineWidth=3;ctx.beginPath();a.forEach((p,j)=>{let x=40+(j/Math.max(1,a.length-1))*(c.width-70),y=190-(Number(p.equity)-min)/(max-min)*155;j?ctx.lineTo(x,y):ctx.moveTo(x,y)});ctx.stroke();ctx.fillStyle=colors[i];ctx.fillText(g+' '+money(values.at(-1)),50,25+i*22)});}
const embedded=document.getElementById('trial-data');
async function refresh(){try{const d=embedded?JSON.parse(embedded.textContent):await(await fetch('/api')).json();document.getElementById('status').textContent=(embedded?'Saved snapshot':'Last refresh: '+new Date().toLocaleString())+' · '+(d.snapshots.length?'Data collected':'Awaiting first market-data cycle')+' · New-token safety filter FAILS CLOSED';document.getElementById('cards').innerHTML=d.portfolios.map(p=>{let s=d.snapshots.filter(x=>x.strategy===p.strategy).at(-1);let eq=s?.equity??p.cash;return '<div class="card"><b>'+esc(p.strategy.toUpperCase())+'</b><div class="big">'+money(eq)+'</div><small>Net '+money(eq-p.initial)+' · Cash '+money(p.cash)+' · Starting '+money(p.initial)+'</small></div>'}).join('');rows('positions',d.positions,['strategy','symbol',r=>Number(r.qty).toFixed(7),r=>Number(r.entry).toFixed(6),r=>Number(r.mark).toFixed(6)]);rows('trades',d.trades,['ts','strategy','side','symbol',r=>Number(r.price).toFixed(6),r=>Number(r.fee).toFixed(5),r=>money(r.realized),'reason']);rows('events',d.events,['ts','agent','strategy','message']);draw(d.snapshots)}catch(e){document.getElementById('status').textContent='Dashboard error: '+e.message}}refresh();if(!embedded)setInterval(refresh,30000);
</script></body></html>'''
def getdata():
 if not DB.exists():return dict(portfolios=[],positions=[],trades=[],events=[],snapshots=[])
 with closing(sqlite3.connect(DB.resolve().as_uri()+'?mode=ro',uri=True)) as c:
  c.row_factory=sqlite3.Row
  def q(sql):return [dict(x) for x in c.execute(sql)]
  return dict(portfolios=q('SELECT * FROM portfolios'),positions=q('SELECT p.*,COALESCE(x.price,p.entry) mark FROM positions p LEFT JOIN prices x ON x.strategy=p.strategy AND x.symbol=p.symbol'),trades=q('SELECT * FROM trades ORDER BY id DESC LIMIT 200'),events=q('SELECT * FROM events ORDER BY id DESC LIMIT 100'),snapshots=q('SELECT * FROM (SELECT * FROM snapshots ORDER BY id DESC LIMIT 1000) ORDER BY id'))
def export(path):
 data=json.dumps(getdata(),allow_nan=False).replace('<',r'\u003c')
 html=HTML.replace('<script>','<script id="trial-data" type="application/json">'+data+'</script><script>',1)
 path.write_text(html,encoding='utf-8')
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  api=urlparse(self.path).path=='/api';body=(json.dumps(getdata()) if api else HTML).encode();self.send_response(200);self.send_header('Content-Type','application/json' if api else 'text/html; charset=utf-8');self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(body)
if __name__=='__main__':
 parser=argparse.ArgumentParser()
 parser.add_argument('--export',type=Path,help='Write a standalone report containing the latest database snapshot')
 args=parser.parse_args()
 if args.export:
  export(args.export)
 else:
  host=os.environ.get('DASH_HOST','127.0.0.1');port=int(os.environ.get('DASH_PORT','8080'));print(f'Paper dashboard: http://{host}:{port}');HTTPServer((host,port),Handler).serve_forever()
