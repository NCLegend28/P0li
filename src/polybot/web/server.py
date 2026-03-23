"""
FastAPI WebSocket server for the web dashboard.

Runs as a background asyncio task alongside the scan loop.
Every second it serialises the live DashboardState into JSON
and broadcasts it to all connected browser clients.

Usage (handled automatically by cli.py):
  The server starts on http://localhost:8765 when WEB_ENABLED=true in .env
  Open http://localhost:8765 in any browser to see the live dashboard.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import re

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from loguru import logger
from polybot.config import settings

_RICH_RE = re.compile(r'\[/?[^\]]*\]')

def _strip_rich(text: str) -> str:
    """Strip all Rich markup tags like [cyan], [bold], [/] from a string."""
    return _RICH_RE.sub('', text)

_LEVEL_MAP = [
    ('[OPEN]',  'TRADE'), ('◉',       'TRADE'),
    ('[EXIT]',  'EXIT'),  ('◎',       'EXIT'),
    ('⚠',       'WARN'),  ('ERROR',   'ERROR'),
    ('✗',       'ERROR'), ('✓',       'GOOD'),
]

def _detect_level(raw: str) -> str:
    for marker, level in _LEVEL_MAP:
        if marker in raw:
            return level
    return 'INFO'

if TYPE_CHECKING:
    from polybot.ui.dashboard import DashboardState

app = FastAPI(title="Polybot Dashboard")
app.state.dash_state = None   # typed slot, no module-level global

_connections: set[WebSocket] = set()


def set_dashboard_state(state: "DashboardState") -> None:
    app.state.dash_state = state


# ─── Serialiser ───────────────────────────────────────────────────────────────

def _serialise(state: "DashboardState") -> dict:
    """
    Convert DashboardState into a plain JSON-serialisable dict.
    The browser dashboard reads exactly these keys.
    """
    trader = state.trader

    if trader:
        starting  = trader._starting_balance()
        open_val  = sum(t.size_usd for t in trader.positions.values())
        nav       = trader.balance + open_val
        pnl       = nav - starting
        pnl_pct   = (pnl / starting * 100) if starting else 0.0
        closed    = trader.closed_trades
        wins      = sum(1 for t in closed if (t.exit_price or 0) > t.entry_price)
        losses    = len(closed) - wins
        decided   = [t for t in closed if t.exit_price is not None
                     and abs((t.exit_price or 0) - t.entry_price) > 0.001]
        win_rate  = (sum(1 for t in decided if (t.exit_price or 0) > t.entry_price) / len(decided)
                     if decided else 0.0)
        win_pnls  = [t.pnl_usd for t in closed if t.pnl_usd > 0]
        loss_pnls = [t.pnl_usd for t in closed if t.pnl_usd < 0]
        avg_win   = sum(win_pnls)  / len(win_pnls)  if win_pnls  else None
        avg_loss  = sum(loss_pnls) / len(loss_pnls) if loss_pnls else None

        positions_data = [
            {
                "id":       t.id,
                "mid":      t.market_id,
                "q":        t.question,
                "side":     str(t.side),
                "entry":    t.entry_price,
                "shares":   t.shares,
                "size":     t.size_usd,
                "opened":   t.opened_at.isoformat() if t.opened_at else "",
                "openedTs": int(t.opened_at.timestamp()) if t.opened_at else 0,
            }
            for t in trader.positions.values()
        ]

        closed_data = [
            {
                "id":     t.id,
                "side":   str(t.side),
                "entry":  t.entry_price,
                "exit":   t.exit_price or 0.0,
                "pnl":    t.pnl_usd,
                "reason": "",   # reason not stored on trade — inferred from exit signals
            }
            for t in reversed(closed[-8:])
        ]

        trader_data = {
            "balance":  round(trader.balance, 2),
            "openVal":  round(open_val, 2),
            "nav":      round(nav, 2),
            "totalPnl": round(pnl, 2),
            "pnlPct":   round(pnl_pct, 2),
            "closed":   len(closed),
            "wins":     wins,
            "losses":   losses,
            "winRate":  round(win_rate, 4),
            "openPos":  len(trader.positions),
            "avgWin":   round(avg_win,  2) if avg_win  is not None else None,
            "avgLoss":  round(avg_loss, 2) if avg_loss is not None else None,
        }
    else:
        positions_data = []
        closed_data    = []
        trader_data    = {
            "balance": 1000.0, "openVal": 0.0, "nav": 1000.0,
            "totalPnl": 0.0, "pnlPct": 0.0,
            "closed": 0, "wins": 0, "losses": 0, "winRate": 0.0,
            "openPos": 0, "avgWin": None, "avgLoss": None,
        }

    opps_data = [
        {
            "q":     o.market.question,
            "side":  str(o.side),
            "mkt":   round(o.market_price, 4),
            "mdl":   round(o.model_probability, 4),
            "edge":  round(o.edge, 4),
            "strat": "WX" if o.strategy == "weather_trader" else o.strategy[:3].upper(),
        }
        for o in state.opportunities
    ]

    last_scan = (state.last_scan_at.strftime("%H:%M:%S")
                 if state.last_scan_at else "—")

    events = []
    for raw in list(state.event_log):
        clean = _strip_rich(raw)
        # Timestamp is the first 8 chars of cleaned string
        ts  = clean[:8].strip()
        msg = clean[8:].strip()
        events.append({"ts": ts, "lv": _detect_level(raw), "msg": msg})

    return {
        "scanNum":      state.scan_number,
        "status":       "PAUSED" if state.is_paused else "RUNNING",
        "lastScan":     last_scan,
        "interval":     state.scan_interval,
        "forecasts":    state.forecasts_fetched,
        "total":        state.total_markets,
        "wx":           state.weather_mkts,
        "crypto":       state.crypto_mkts,
        "politics":     state.politics_mkts,
        "sports":       state.sports_mkts,
        "other":        state.other_mkts,
        "scanHistory":  list(state.scan_duration_history),
        "navHistory":   list(state.nav_history),
        "dailyPnl":     round(state.daily_pnl, 2),
        "dailyOpened":  state.daily_trades_opened,
        "dailyClosed":  state.daily_trades_closed,
        "bestEdge":     round(state.best_edge_today, 4),
        "nextScanIn":   int(state.next_scan_in),
        "positions":    positions_data,
        "feed":         state.market_feed,
        "opps":         opps_data,
        "closed":       closed_data,
        "trader":       trader_data,
        "events":       events,
        "maxPositions": settings.max_open_positions,
    }


# ─── WebSocket endpoint ───────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    _connections.add(ws)
    logger.info(f"Browser connected. Active connections: {len(_connections)}")
    try:
        while True:
            if app.state.dash_state is not None:
                payload = json.dumps(_serialise(app.state.dash_state))
                await ws.send_text(payload)
            await asyncio.sleep(1.0)
    except asyncio.CancelledError:
        pass   # clean shutdown — not an error
    except WebSocketDisconnect:
        pass   # browser closed tab
    except (RuntimeError, OSError) as e:
        logger.debug(f"WebSocket send error: {e}")
    finally:
        _connections.discard(ws)
        logger.info(f"Browser disconnected. Active connections: {len(_connections)}")


# ─── Health probe ─────────────────────────────────────────────────────────────

@app.get("/health")
async def health_check() -> JSONResponse:
    """Standard liveness probe — returns 200 when the server is up."""
    from datetime import datetime, timezone
    state = app.state.dash_state
    return JSONResponse({
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "scanner": {
            "running": bool(state and not state.is_paused),
            "scan_number": state.scan_number if state else 0,
        },
    })


# ─── HTML dashboard ───────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return _DASHBOARD_HTML


# ─── Server runner ────────────────────────────────────────────────────────────

async def run_server(host: str = "0.0.0.0", port: int = 8765) -> None:
    import uvicorn
    config = uvicorn.Config(
        app,
        host      = host,
        port      = port,
        log_level = "warning",   # quiet — scan loop owns the terminal
        access_log= False,
    )
    server = uvicorn.Server(config)
    logger.info(f"Web dashboard → http://localhost:{port}")
    await server.serve()


# ─── Embedded dashboard HTML ─────────────────────────────────────────────────
# Single self-contained file — no build step, no npm, no node.
# Opens a WebSocket to /ws and re-renders the full dashboard on every message.

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Polybot Dashboard</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%;background:#0d1117;font-family:'Courier New',monospace;color:#cdd9e5;font-size:11px;overflow:hidden}
#wrap{position:relative;padding:7px;height:100vh;display:flex;flex-direction:column;overflow:hidden;box-sizing:border-box}
#root{flex:1;min-height:0;display:flex;flex-direction:column;overflow:hidden}
#maingrid{flex:1;min-height:0;overflow:hidden}
#tip{position:absolute;z-index:9999;background:#0b1620;border:1px solid #00d4ff;border-radius:6px;
  padding:8px 11px;font-size:10.5px;line-height:1.55;color:#cdd9e5;width:220px;pointer-events:none;
  display:none;box-shadow:0 6px 28px rgba(0,212,255,.22)}
#tip b{color:#00d4ff;font-size:9px;font-weight:700;letter-spacing:.08em;display:block;margin-bottom:3px}
#conn{position:fixed;bottom:10px;right:12px;font-size:9px;color:#4a5568;font-family:'Courier New',monospace}
.panel{background:#0f1923;border:1px solid rgba(0,212,255,.12);border-radius:5px;display:flex;flex-direction:column;overflow:visible}
.ph{border-bottom:1px solid rgba(0,212,255,.12);padding:5px 10px;display:flex;align-items:center;gap:5px;font-size:10px;font-weight:700;letter-spacing:.08em;flex-shrink:0}
.pb{padding:7px 10px;flex:1;overflow:visible}
.sep{border-top:1px solid rgba(255,255,255,.06);margin:5px 0}
.kv{display:flex;justify-content:space-between;align-items:center;padding:2px 0}
.kl{color:#718096;cursor:help;border-bottom:1px dotted rgba(0,212,255,.3)}
.kl:hover{border-bottom-color:rgba(0,212,255,.8)}
.gh{font-size:9px;color:#4a5568;font-weight:700;letter-spacing:.06em;cursor:help}
.gr{border-bottom:1px solid rgba(255,255,255,.04);padding:3px 0}
.oc{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
[data-tip]{cursor:help;border-bottom:1px dotted rgba(0,212,255,.3)}
[data-tip]:hover{border-bottom-color:rgba(0,212,255,.8)}
</style>
</head>
<body>
<div id="wrap">
  <div id="tip"><b id="tl"></b><span id="tb"></span></div>
  <div id="root"><div style="color:#4a5568;padding:40px;text-align:center">Connecting to scanner...</div></div>
</div>
<div id="conn">&#9711; connecting</div>

<script>
const C={cyan:'#00d4ff',green:'#3ddc84',red:'#ff5c5c',yellow:'#ffd166',magenta:'#c77dff',blue:'#58a6ff',orange:'#ff9a3c',dim:'#4a5568',muted:'#718096',text:'#cdd9e5',white:'#e8edf2'};

const TIPS={
  balance:  ['BALANCE','Cash not currently deployed in any open position.'],
  openVal:  ['OPEN VALUE','Total cost basis of all currently open positions combined.'],
  nav:      ['NET ASSET VALUE','Balance + mark-to-market value of all open positions.'],
  totalPnl: ['TOTAL P&L','Cumulative profit/loss vs the $1,000 starting paper balance.'],
  closed:   ['CLOSED TRADES','Total number of positions that have been fully exited.'],
  winRate:  ['WIN RATE','Percentage of closed trades where exit price exceeded entry price.'],
  openPos:  (max)=>[`OPEN POSITIONS`,`Active positions. Bot stops opening new ones after ${max} (circuit breaker).`],
  avgWin:   ['AVG WIN','Mean P&L in dollars across all profitable closed trades.'],
  avgLoss:  ['AVG LOSS','Mean P&L in dollars across all losing closed trades.'],
  kelly:    ['KELLY FRACTION','Optimal position size per Kelly Criterion. Use quarter Kelly in practice until 50+ trades.'],
  market:   ['MARKET','The Polymarket question being traded. Hover a row to see the full question text as a browser tooltip.'],
  scanNum:  ['SCAN NUMBER','Number of complete scan cycles since the bot started.'],
  interval: ['SCAN INTERVAL','Seconds between full Gamma API + Open-Meteo forecast cycles.'],
  forecasts:['FORECASTS','City weather forecasts fetched from Open-Meteo this scan.'],
  markets:  ['TOTAL MARKETS','All active Gamma markets before liquidity and price filters are applied.'],
  wx:       ['WEATHER TRADER','Compares Open-Meteo forecast to Polymarket implied probability. Enters when gap exceeds 8%.'],
  edge:     ['EDGE','Model probability minus market price. Minimum 8% required to open a position.'],
  mktPct:   ['MARKET %','Current Polymarket implied probability, what the crowd is pricing.'],
  mdlPct:   ['MODEL %','Our Normal distribution model estimate from Open-Meteo forecast data.'],
  side:     ['SIDE','YES = bet it happens. NO = bet it does not. We mostly trade NO on narrow temperature brackets.'],
  entry:    ['ENTRY PRICE','Price paid when the position was opened. 0.0 to 1.0 scale equals 0 to 100% probability.'],
  now:      ['CURRENT PRICE','Latest Polymarket price for this outcome. Updates each scan cycle.'],
  unreal:   ['UNREALISED P&L','What you would make or lose if you closed this position right now.'],
  hrs:      ['HOURS TO CLOSE','Time until this market resolves. Under 4h triggers a time-stop exit.'],
  yesPx:    ['YES PRICE','Implied probability YES resolves. 0.30 means market thinks 30% chance.'],
  noPx:     ['NO PRICE','Complement of YES. 1 minus YES. Our position price when trading the NO side.'],
  liq:      ['LIQUIDITY','Total liquidity in USD. We skip markets below $500.'],
  reason:   ['EXIT REASON','Why this position was closed: profit_target, edge_collapse, time_stop, or market_closed.'],
  scanTime: ['SCAN DURATION','End-to-end time for the last scan: Gamma fetch plus forecasts plus strategy evaluation.'],
  dailyPnl: ['TODAY P&L','Net profit and loss from all trades opened and closed since midnight UTC.'],
  bestEdge: ['BEST EDGE TODAY','Largest edge opportunity detected across all of today scan cycles.'],
  navHist:  ['NAV HISTORY','Net Asset Value trend across the last 20 scan cycles. Green means growing.'],
};

// ── Tooltip (position:absolute within #wrap — never clipped) ──────────────────
const tip=document.getElementById('tip'),tl=document.getElementById('tl'),tb=document.getElementById('tb');
const wrap=document.getElementById('wrap');
wrap.addEventListener('mousemove',e=>{
  const el=e.target.closest('[data-tip]');
  if(!el){tip.style.display='none';return;}
  const raw=TIPS[el.dataset.tip];
  if(!raw){tip.style.display='none';return;}
  const info=typeof raw==='function'?(raw(lastData&&lastData.maxPositions||10)):raw;
  tl.textContent=info[0];tb.textContent=info[1];
  tip.style.display='block';
  const wr=wrap.getBoundingClientRect(),tw=224,th=tip.offsetHeight||80;
  let x=e.clientX-wr.left+14,y=e.clientY-wr.top-th-10;
  if(y<4)y=e.clientY-wr.top+18;
  if(x+tw>wr.width-4)x=e.clientX-wr.left-tw-14;
  tip.style.left=x+'px';tip.style.top=y+'px';
});
wrap.addEventListener('mouseleave',()=>{tip.style.display='none';});

// ── Helpers ───────────────────────────────────────────────────────────────────
const pn=n=>String(n).padStart(2,'0');
function fmtNow(){const d=new Date();return`${pn(d.getUTCHours())}:${pn(d.getUTCMinutes())}:${pn(d.getUTCSeconds())} UTC`;}
function t(id,html){return`<span data-tip="${id}">${html}</span>`;}
function pc(v){return v>=0?C.green:C.red;}
function hc(h){return h<4?C.red:h<12?C.yellow:C.dim;}
function ec(e){return e>=0.25?C.green:e>=0.12?C.yellow:C.muted;}
function spark(vals,len=14){
  const c=' ▁▂▃▄▅▆▇█';
  if(!vals||!vals.length)return'─'.repeat(len);
  const lo=Math.min(...vals),hi=Math.max(...vals),sp=hi-lo||1;
  return vals.slice(-len).map(v=>c[Math.round(((v-lo)/sp)*8)]).join('');
}
function bar(lbl,cnt,tot,col){
  const f=tot>0?Math.round((cnt/tot)*12):0;
  const b='█'.repeat(f)+'░'.repeat(Math.max(0,12-f));
  return`<div style="display:flex;align-items:center;gap:5px;padding:1px 0"><span style="color:${col};width:26px;flex-shrink:0">${lbl}</span><span style="color:${col};opacity:.55;flex:1;overflow:hidden;font-size:10px">${b}</span><span style="color:${C.dim};width:22px;text-align:right">${cnt}</span></div>`;
}
function kv(id,lbl,val){
  return`<div class="kv"><span class="kl" data-tip="${id}">${lbl}</span><span style="font-weight:600">${val}</span></div>`;
}
function condense(q){
  var cityM=q.match(/in ([A-Za-z ]+?) be/i);
  var city=cityM?cityM[1].trim():'';
  var tempM=q.match(/([0-9]+(?:[- ][0-9]+)?)[ ]*[°]?([CF])\b/i);
  var temp='';
  if(tempM){var r=tempM[1].replace(/ *- */g,'-');temp=r+'°'+tempM[2].toUpperCase();}
  if(/or below/i.test(q)&&temp)temp='≤'+temp;
  else if(/or higher|or above/i.test(q)&&temp)temp='≥'+temp;
  var dateM=q.match(/on (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* ([0-9]{1,2})/i);
  var date=dateM?(dateM[1][0].toUpperCase()+dateM[1].slice(1,3).toLowerCase()+' '+dateM[2]):'';
  return [city,temp,date].filter(Boolean).join(' · ');
}

// ── Two-tier rendering ────────────────────────────────────────────────────────
// fullRender(): builds the entire DOM — called once on first data, then only
//               when scanNum changes (i.e. after a new scan completes).
// tickUpdate(): updates only the elements that change every second:
//               clock, countdown, pulse. Everything else stays put.

let lastScanNum = -1;
let lastData    = null;

function fullRender(S){
  const {scanNum,status,lastScan,interval,forecasts,total,wx,crypto,politics,sports,other,
    scanHistory,navHistory,dailyPnl,dailyOpened,dailyClosed,bestEdge,
    positions,feed,opps,closed,trader,events}=S;
  const fm={};(feed||[]).forEach(m=>{const yp=typeof m.yes_price==='number'?m.yes_price:(m.yes||0);fm[m.id]={yes:yp,hrs:m.hours_until_close||m.hrs||0};});
  const lvC={INFO:C.text,TRADE:C.cyan,EXIT:C.magenta,GOOD:C.green,WARN:C.yellow,ERROR:C.red};
  const lvI={INFO:'·',TRADE:'◉',EXIT:'◎',GOOD:'✓',WARN:'⚠',ERROR:'✗'};

  document.getElementById('root').innerHTML=`
  <div style="background:#0f1923;border:1px solid ${C.cyan};border-radius:5px;padding:5px 12px;margin-bottom:6px;display:flex;align-items:center;gap:14px;flex-wrap:wrap">
    <span id="hpulse" style="color:${C.green};font-size:12px">&#9711; SCANNING</span>
    <span style="color:${C.dim}">
      ${t('scanNum',`scan <span style="color:${C.cyan}">#${scanNum}</span>`)}
      &nbsp;·&nbsp; next in <span id="hcountdown" style="color:${C.cyan}">--</span>s
      &nbsp;·&nbsp; ${t('scanTime',`last <span style="color:${C.yellow}">${scanHistory&&scanHistory.length?(scanHistory[scanHistory.length-1]).toFixed(1):'-'}s</span>`)}
    </span>
    <span style="margin-left:auto;color:${C.dim}">&#9201; <span id="hclock" style="color:${C.white}">${fmtNow()}</span></span>
  </div>

  <div id="maingrid" style="display:grid;grid-template-columns:190px 1fr 248px;gap:5px;margin-bottom:5px;overflow:visible;flex:1;min-height:0">

    <div class="panel" style="grid-row:1/4">
      <div class="ph" style="color:${C.cyan}">&#9672; SCANNER</div>
      <div class="pb">
        ${kv('scanNum','STATUS',`<span style="color:${status==='RUNNING'?C.green:C.yellow}">${status}</span>`)}
        ${kv('scanNum','SCAN #',`<span style="color:${C.cyan}">${scanNum}</span>`)}
        ${kv('interval','LAST',`<span style="color:${C.dim}">${lastScan}</span>`)}
        ${kv('interval','INTERVAL',`<span style="color:${C.dim}">${interval}s</span>`)}
        ${kv('forecasts','FORECASTS',`<span style="color:${C.blue}">${forecasts}</span>`)}
        <div class="sep"></div>
        ${kv('markets','MARKETS',`${total}`)}
        ${bar('WX',wx,total,C.cyan)}${bar('B',crypto,total,C.yellow)}${bar('POL',politics,total,C.magenta)}${bar('SPT',sports,total,C.green)}${bar('OTH',other,total,C.dim)}
        <div class="sep"></div>
        <div style="color:${C.dim};font-size:9px;margin-bottom:3px">${t('scanTime','SCAN TIME')}</div>
        <div style="color:${C.blue};font-size:11px">${spark(scanHistory)}</div>
        <div class="sep"></div>
        <div style="color:${C.dim};font-size:9px;margin-bottom:3px">TODAY</div>
        ${kv('dailyPnl','P&L',`<span style="color:${pc(dailyPnl)}">${dailyPnl>=0?'&#9650;':'&#9660;'} $${dailyPnl>=0?'+':''}${(dailyPnl||0).toFixed(2)}</span>`)}
        ${kv('openPos','OPENED',`<span style="color:${C.cyan}">${dailyOpened}</span>`)}
        ${kv('closed','CLOSED',`<span style="color:${C.blue}">${dailyClosed}</span>`)}
        ${kv('bestEdge','BEST EDGE',`<span style="color:${C.green}">+${((bestEdge||0)*100).toFixed(1)}%</span>`)}
      </div>
    </div>

    <div class="panel">
      <div class="ph" style="color:${C.green}">&#9672; OPEN POSITIONS &nbsp;<span style="color:${C.white}">${(positions||[]).length}</span></div>
      <div class="pb">
        <div style="display:grid;grid-template-columns:90px 1fr 42px 54px 54px 84px 46px;gap:0 6px;margin-bottom:4px">
          ${[['ID','scanNum'],['MARKET','market'],['SIDE','side'],['ENTRY','entry'],['NOW','now'],['UNREAL','unreal'],['AGE','hrs']].map(([h,id])=>`<span class="gh" data-tip="${id}">${h}</span>`).join('')}
        </div>
        <div style="border-bottom:1px solid rgba(255,255,255,.06);margin-bottom:3px"></div>
        ${(positions||[]).length===0?`<div style="color:${C.dim};text-align:center;padding:12px 0">no open positions</div>`:''}
        ${(positions||[]).map(p=>{
          const m=fm[p.mid];const cy=m?m.yes:null;
          const cs=cy!=null?(p.side==='YES'?cy:1-cy):null;
          const ur=cs!=null?((cs-p.entry)*p.shares):null;
          const hrs=m?m.hrs:null;
          return`<div style="display:grid;grid-template-columns:90px 1fr 42px 54px 54px 84px 46px;gap:0 6px" class="gr">
            <span style="color:${C.dim}">${p.id.slice(0,10)}</span>
            <span style="color:${C.text}" class="oc" title="${p.q}">${condense(p.q)}</span>
            <span style="color:${p.side==='YES'?C.green:C.red};text-align:center">${t('side',p.side)}</span>
            <span style="color:${C.yellow};text-align:right">${t('entry',p.entry.toFixed(3))}</span>
            <span style="color:${C.white};text-align:right">${t('now',cs!=null?cs.toFixed(3):'&#8212;')}</span>
            <span style="color:${ur!=null?(ur>=0?C.green:C.red):C.dim};text-align:right">${t('unreal',ur!=null?`${ur>=0?'&#9650;':'&#9660;'} $${ur>=0?'+':''}${ur.toFixed(2)}`:'&#8212;')}</span>
            <span style="color:${C.dim};text-align:right" title="Market closes in ${hrs!=null?hrs.toFixed(1)+'h':'unknown'}">
              ${t('hrs',(()=>{if(!p.openedTs)return'&#8212;';const age=Math.floor((Date.now()/1000-p.openedTs)/60);return age<60?age+'m':Math.floor(age/60)+'h';})())}
            </span>
          </div>`;
        }).join('')}
      </div>
    </div>

    <div class="panel">
      <div class="ph" style="color:${C.magenta}">&#9672; P&amp;L METRICS</div>
      <div class="pb">
        ${kv('balance','BALANCE',`$${(trader.balance||0).toFixed(2)}`)}
        ${kv('openVal','OPEN VAL',`<span style="color:${C.blue}">$${(trader.openVal||0).toFixed(2)}</span>`)}
        ${kv('nav','NAV',`<span style="color:${C.cyan}">$${(trader.nav||0).toFixed(2)}</span>`)}
        <div class="sep"></div>
        ${kv('totalPnl','TOTAL P&amp;L',`<span style="color:${pc(trader.totalPnl||0)}">${(trader.totalPnl||0)>=0?'&#9650;':'&#9660;'} $${(trader.totalPnl||0)>=0?'+':''}${(trader.totalPnl||0).toFixed(2)} (${(trader.pnlPct||0)>=0?'+':''}${(trader.pnlPct||0).toFixed(2)}%)</span>`)}
        <div style="margin:3px 0">${t('navHist',`<span style="color:${(trader.totalPnl||0)>=0?C.green:C.red};font-size:11px">${spark(navHistory)}</span>`)}</div>
        <div class="sep"></div>
        ${kv('closed','CLOSED',`${trader.closed||0} <span style="color:${C.green}">&#10003;${trader.wins||0}</span> <span style="color:${C.red}">&#10007;${trader.losses||0}</span>`)}
        ${kv('winRate','WIN RATE',`<span style="color:${(trader.winRate||0)>=.55?C.green:(trader.winRate||0)>=.45?C.yellow:C.red}">${((trader.winRate||0)*100).toFixed(0)}%</span>`)}
        ${kv('openPos','OPEN POS',`<span style="color:${C.cyan}">${trader.openPos||0}</span><span style="color:${C.dim}">/${S.maxPositions||10}</span>`)}
        ${trader.avgWin!=null?kv('avgWin','AVG WIN',`<span style="color:${C.green}">$+${trader.avgWin.toFixed(2)}</span>`):''}
        ${trader.avgLoss!=null?kv('avgLoss','AVG LOSS',`<span style="color:${C.red}">$${trader.avgLoss.toFixed(2)}</span>`):''}
        ${kv('kelly','KELLY',`<span style="color:${C.yellow}">~25% <span style="color:${C.dim}">(&#188; kelly)</span></span>`)}
      </div>
    </div>

    <div class="panel">
      <div class="ph" style="color:${C.yellow}">&#9672; OPPORTUNITIES &nbsp;<span style="color:${C.white}">${(opps||[]).length}</span></div>
      <div class="pb">
        <div style="display:grid;grid-template-columns:32px 1fr 42px 54px 54px 58px;gap:0 6px;margin-bottom:4px">
          ${[['STRAT','wx'],['MARKET','market'],['SIDE','side'],['MKT%','mktPct'],['MDL%','mdlPct'],['EDGE','edge']].map(([h,id])=>`<span class="gh" data-tip="${id}">${h}</span>`).join('')}
        </div>
        <div style="border-bottom:1px solid rgba(255,255,255,.06);margin-bottom:3px"></div>
        ${(opps||[]).length===0?`<div style="color:${C.dim};text-align:center;padding:8px 0">scanning...</div>`:''}
        ${(opps||[]).slice(0,6).map(o=>`
          <div style="display:grid;grid-template-columns:32px 1fr 42px 54px 54px 58px;gap:0 6px" class="gr">
            <span data-tip="wx" style="color:${C.blue}">${o.strat}</span>
            <span style="color:${C.text}" class="oc" title="${o.q}">${o.q}</span>
            <span style="color:${o.side==='YES'?C.green:C.red};text-align:center">${t('side',o.side)}</span>
            <span style="color:${C.muted};text-align:right">${t('mktPct',((o.mkt||0)*100).toFixed(1)+'%')}</span>
            <span style="color:${C.white};text-align:right">${t('mdlPct',((o.mdl||0)*100).toFixed(1)+'%')}</span>
            <span style="color:${ec(o.edge||0)};text-align:right">${t('edge','+'+((o.edge||0)*100).toFixed(1)+'%')}</span>
          </div>`).join('')}
      </div>
    </div>

    <div class="panel">
      <div class="ph" style="color:${C.orange}">&#9672; WX FEED &nbsp;<span style="color:${C.white}">${(feed||[]).length}</span></div>
      <div class="pb">
        <div style="display:grid;grid-template-columns:1fr 46px 46px 62px 38px;gap:0 5px;margin-bottom:4px">
          ${[['MARKET','scanNum'],['YES','yesPx'],['NO','noPx'],['LIQ','liq'],['HRS','hrs']].map(([h,id],i)=>`<span class="gh" data-tip="${id}" style="display:block;text-align:${i>0?'right':'left'}">${h}</span>`).join('')}
        </div>
        <div style="border-bottom:1px solid rgba(255,255,255,.06);margin-bottom:3px"></div>
        ${(feed||[]).slice(0,6).map(m=>{
          const yes=m.yes_price||m.yes||0,hrs=m.hours_until_close||m.hrs||0,liq=m.liquidity_usd||m.liq||0;
          return`<div style="display:grid;grid-template-columns:1fr 46px 46px 62px 38px;gap:0 5px" class="gr">
            <span style="color:${C.text}" class="oc" title="${m.question||m.q||''}">${condense(m.question||m.q||'')}</span>
            <span style="color:${yes>=.5?C.green:C.red};text-align:right">${t('yesPx',yes.toFixed(3))}</span>
            <span style="color:${C.dim};text-align:right">${t('noPx',(1-yes).toFixed(3))}</span>
            <span style="color:${C.dim};text-align:right">${t('liq','$'+liq.toLocaleString())}</span>
            <span style="color:${hc(hrs)};text-align:right">${t('hrs',Math.round(hrs)+'h')}</span>
          </div>`;
        }).join('')}
      </div>
    </div>

    <div class="panel">
      <div class="ph" style="color:${C.blue}">&#9672; CLOSED &nbsp;<span style="color:${C.white}">${(closed||[]).length}</span></div>
      <div class="pb">
        <div style="display:grid;grid-template-columns:90px 40px 52px 52px 82px;gap:0 5px;margin-bottom:4px">
          ${[['ID','scanNum'],['SIDE','side'],['IN','entry'],['OUT','now'],['P&amp;L','totalPnl']].map(([h,id],i)=>`<span class="gh" data-tip="${id}" style="display:block;text-align:${i>1?'right':'left'}">${h}</span>`).join('')}
        </div>
        <div style="border-bottom:1px solid rgba(255,255,255,.06);margin-bottom:3px"></div>
        ${(closed||[]).length===0?`<div style="color:${C.dim};text-align:center;padding:8px 0">no closed trades</div>`:''}
        ${(closed||[]).map(tr=>`
          <div style="display:grid;grid-template-columns:90px 40px 52px 52px 82px;gap:0 5px" class="gr">
            <span style="color:${C.dim}">${tr.id.slice(0,10)}</span>
            <span style="color:${tr.side==='YES'?C.green:C.red}">${tr.side}</span>
            <span style="color:${C.dim};text-align:right">${(tr.entry||0).toFixed(3)}</span>
            <span style="color:${C.white};text-align:right">${(tr.exit||0).toFixed(3)}</span>
            <span data-tip="reason" style="color:${pc(tr.pnl||0)};text-align:right">${(tr.pnl||0)>=0?'&#10003;':'&#10007;'} $${(tr.pnl||0)>=0?'+':''}${(tr.pnl||0).toFixed(2)}</span>
          </div>`).join('')}
      </div>
    </div>
  </div>

  <div id="eventlogpanel" style="background:#0f1923;border:1px solid rgba(255,255,255,.06);border-radius:5px;padding:6px 10px;display:flex;flex-direction:column;min-height:0;flex-shrink:0;height:130px">
    <div style="color:${C.dim};font-size:9px;font-weight:700;letter-spacing:.08em;margin-bottom:5px;flex-shrink:0">&#9672; EVENT LOG</div>
    <div style="flex:1;min-height:0;overflow-y:auto;scrollbar-width:thin;scrollbar-color:#4a5568 #0f1923" id="eventlog">
    ${(events||[]).map(e=>{
      const lc=lvC[e.lv]||C.text;const li=lvI[e.lv]||'·';
      return`<div style="display:flex;gap:10px;padding:1px 0;align-items:center">
        <span style="color:${C.dim};flex-shrink:0;font-size:10px">${e.ts||''}</span>
        <span style="color:${lc};flex-shrink:0">${li}</span>
        <span style="color:${lc};word-break:break-word">${e.msg||''}</span>
      </div>`;
    }).join('')}
    </div>
  </div>`;
  const el=document.getElementById('eventlog');if(el)el.scrollTop=el.scrollHeight;
}

// ── Tick update — only clock, countdown, pulse ────────────────────────────────
let pulse=true;
function tickUpdate(nextScanIn){
  pulse=!pulse;
  const hp=document.getElementById('hpulse');
  const hc=document.getElementById('hcountdown');
  const hck=document.getElementById('hclock');
  if(hp) hp.textContent=(pulse?'● ':'○ ')+'SCANNING';
  if(hc) hc.textContent=nextScanIn;
  if(hck) hck.textContent=fmtNow();
}

// ── WebSocket with auto-reconnect ─────────────────────────────────────────────
let ws,reconnectTimer,tickTimer;
const conn=document.getElementById('conn');

function onMessage(S){
  // Full re-render only on first connect or when scan number changes
  if(lastScanNum!==S.scanNum){
    lastScanNum=S.scanNum;
    fullRender(S);
  }
  lastData=S;
  // Always update the fast elements immediately
  tickUpdate(S.nextScanIn);
}

function connect(){
  clearInterval(tickTimer);
  const proto=location.protocol==='https:'?'wss:':'ws:';
  ws=new WebSocket(`${proto}//${location.host}/ws`);
  ws.onopen=()=>{
    conn.innerHTML='<span style="color:#3ddc84">&#9899; live</span>';
    clearTimeout(reconnectTimer);
    // Keep clock and pulse running between WS messages
    tickTimer=setInterval(()=>{if(lastData)tickUpdate(lastData.nextScanIn);},1000);
  };
  ws.onmessage=e=>{
    try{onMessage(JSON.parse(e.data));}catch(err){console.error(err);}
  };
  ws.onclose=()=>{
    clearInterval(tickTimer);
    conn.innerHTML='<span style="color:#ff5c5c">&#9899; reconnecting...</span>';
    reconnectTimer=setTimeout(connect,2000);
  };
  ws.onerror=()=>ws.close();
}
connect();
</script>
</body>
</html>"""