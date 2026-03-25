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
                "reason": "",
            }
            for t in reversed(closed)
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

    crypto_feed_data = [
        {
            "id":                m["id"],
            "question":          m["question"],
            "yes_price":         m["yes_price"],
            "liquidity_usd":     m["liquidity_usd"],
            "hours_until_close": m["hours_until_close"],
            "spot_usd":          m.get("spot_usd"),
            "sigma_daily":       m.get("sigma_daily"),
            "coin_id":           m.get("coin_id", ""),
        }
        for m in state.crypto_feed
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
        "cryptoFeed":   crypto_feed_data,
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
    try:
        await server.serve()
    except (OSError, SystemExit) as exc:
        logger.warning(
            f"Web server could not start on port {port} "
            f"({exc.__class__.__name__}: {exc}). "
            f"Dashboard disabled — kill any stale process with: "
            f"kill $(lsof -ti tcp:{port})"
        )


# ─── Embedded dashboard HTML ─────────────────────────────────────────────────
# Single self-contained file — no build step, no npm, no node.
# Opens a WebSocket to /ws and re-renders the full dashboard on every message.

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Polybot Dashboard</title>
<style>
/* ── Reset & base ─────────────────────────────────────────────────── */
*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%;background:#0d1117;font-family:'Courier New',monospace;color:#cdd9e5;font-size:11px;overflow:hidden}

/* ── App shell ─────────────────────────────────────────────────────── */
#app{height:100vh;display:flex;flex-direction:column;padding:6px;gap:5px;overflow:hidden}

/* ── Header bar ────────────────────────────────────────────────────── */
#hdr{flex-shrink:0;height:34px;background:#0f1923;border:1px solid #00d4ff;border-radius:5px;
  display:flex;align-items:center;padding:0 12px;gap:12px;overflow:hidden}

/* ── Zone grid — fixed structure, modules are placed inside zones ─── */
#main{flex:1;min-height:0;display:grid;
  grid-template-columns:190px 1fr 248px;
  grid-template-rows:2fr 1fr 1fr;
  gap:5px}

/* Zone grid positions */
#z-left{grid-row:1/4;grid-column:1}
#z-ct  {grid-row:1/3;grid-column:2}
#z-cb  {grid-row:3/4;grid-column:2}
#z-rt  {grid-row:1/2;grid-column:3}
#z-rm  {grid-row:2/3;grid-column:3}
#z-rb  {grid-row:3/4;grid-column:3}

/* Zone is a transparent slot that holds one module */
.zone{display:flex;flex-direction:column;min-height:0;border-radius:5px;transition:outline 80ms}
.zone.drag-over{outline:1.5px dashed rgba(0,212,255,.55);outline-offset:-2px}

/* Module fills its zone exactly */
.module{flex:1;min-height:0;display:flex;flex-direction:column;
  background:#0f1923;border:1px solid rgba(0,212,255,.12);border-radius:5px;overflow:hidden}

/* Module header — the grab handle */
.mh{flex-shrink:0;padding:5px 10px;border-bottom:1px solid rgba(0,212,255,.10);
  display:flex;align-items:center;gap:6px;font-size:10px;font-weight:700;letter-spacing:.08em;
  cursor:grab;user-select:none}
.mh:active{cursor:grabbing}
.mh .grip{color:#1e2d3d;margin-left:auto;font-size:13px;letter-spacing:-1px;transition:color .15s}
.mh:hover .grip{color:#4a5568}

/* Module body — the scrollable content area */
.mb{flex:1;min-height:0;overflow-y:auto;padding:6px 10px;
  scrollbar-width:thin;scrollbar-color:#1e2d3d #0f1923}
.mb::-webkit-scrollbar{width:4px}
.mb::-webkit-scrollbar-track{background:#0f1923}
.mb::-webkit-scrollbar-thumb{background:#1e2d3d;border-radius:2px}

/* ── Event log strip ───────────────────────────────────────────────── */
#log{flex-shrink:0;height:112px;background:#0f1923;border:1px solid rgba(255,255,255,.07);
  border-radius:5px;display:flex;flex-direction:column;overflow:hidden}
#loghdr{flex-shrink:0;padding:4px 10px;font-size:9px;color:#4a5568;font-weight:700;
  letter-spacing:.08em;border-bottom:1px solid rgba(255,255,255,.05)}
#logbody{flex:1;min-height:0;overflow-y:auto;padding:3px 10px;
  scrollbar-width:thin;scrollbar-color:#1e2d3d #0f1923}

/* ── Shared components ─────────────────────────────────────────────── */
.sep{border-top:1px solid rgba(255,255,255,.06);margin:5px 0}
.kv{display:flex;justify-content:space-between;align-items:center;padding:2px 0}
.kl{color:#718096;cursor:help;border-bottom:1px dotted rgba(0,212,255,.25)}
.kl:hover{border-bottom-color:rgba(0,212,255,.7)}
.gh{font-size:9px;color:#4a5568;font-weight:700;letter-spacing:.06em;cursor:help}
.gr{border-bottom:1px solid rgba(255,255,255,.04);padding:3px 0}
.oc{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
[data-tip]{cursor:help}
.tab{cursor:pointer;padding:3px 10px;font-size:9px;font-weight:700;letter-spacing:.08em;
  border:1px solid rgba(0,212,255,.18);border-radius:3px;background:transparent;color:#4a5568;
  font-family:'Courier New',monospace}
.tab.active{border-color:#00d4ff;color:#00d4ff;background:rgba(0,212,255,.08)}
.tab:hover:not(.active){color:#cdd9e5;border-color:rgba(0,212,255,.4)}

/* ── Tooltip ───────────────────────────────────────────────────────── */
#tip{position:fixed;z-index:9999;background:#0b1620;border:1px solid #00d4ff;border-radius:6px;
  padding:8px 11px;font-size:10.5px;line-height:1.55;color:#cdd9e5;width:220px;
  pointer-events:none;display:none;box-shadow:0 6px 28px rgba(0,212,255,.22)}
#tip b{color:#00d4ff;font-size:9px;font-weight:700;letter-spacing:.08em;display:block;margin-bottom:3px}
#conn{position:fixed;bottom:8px;right:10px;font-size:9px;color:#4a5568;font-family:'Courier New',monospace}
</style>
</head>
<body>
<div id="app">
  <div id="hdr"></div>
  <div id="main">
    <div class="zone" id="z-left" data-zone="left"></div>
    <div class="zone" id="z-ct"   data-zone="ct"></div>
    <div class="zone" id="z-cb"   data-zone="cb"></div>
    <div class="zone" id="z-rt"   data-zone="rt"></div>
    <div class="zone" id="z-rm"   data-zone="rm"></div>
    <div class="zone" id="z-rb"   data-zone="rb"></div>
  </div>
  <div id="log">
    <div id="loghdr">◈ EVENT LOG</div>
    <div id="logbody"></div>
  </div>
</div>
<div id="tip"><b id="tl"></b><span id="tb"></span></div>
<div id="conn">○ connecting</div>

<script>
// ── Palette ───────────────────────────────────────────────────────────────────
const C={cyan:'#00d4ff',green:'#3ddc84',red:'#ff5c5c',yellow:'#ffd166',magenta:'#c77dff',blue:'#58a6ff',orange:'#ff9a3c',dim:'#4a5568',muted:'#718096',text:'#cdd9e5',white:'#e8edf2'};

// ── Tooltips ──────────────────────────────────────────────────────────────────
const TIPS={
  balance:['BALANCE','Cash not currently deployed in any open position.'],
  openVal:['OPEN VALUE','Total cost basis of all currently open positions combined.'],
  nav:['NET ASSET VALUE','Balance + mark-to-market value of all open positions.'],
  totalPnl:['TOTAL P&L','Cumulative profit/loss vs the starting paper balance.'],
  closed:['CLOSED TRADES','Total number of positions that have been fully exited.'],
  winRate:['WIN RATE','Percentage of closed trades where exit price exceeded entry price.'],
  openPos:max=>[`OPEN POSITIONS`,`Active positions. Bot stops opening new ones after ${max} (circuit breaker).`],
  avgWin:['AVG WIN','Mean P&L in dollars across all profitable closed trades.'],
  avgLoss:['AVG LOSS','Mean P&L in dollars across all losing closed trades.'],
  kelly:['KELLY FRACTION','Optimal position size per Kelly Criterion. Use ¼ Kelly in practice until 50+ trades.'],
  market:['MARKET','The Polymarket question being traded.'],
  scanNum:['SCAN NUMBER','Number of complete scan cycles since the bot started.'],
  interval:['SCAN INTERVAL','Seconds between full Gamma API + forecast cycles.'],
  forecasts:['FORECASTS','City weather forecasts fetched from Open-Meteo this scan.'],
  markets:['TOTAL MARKETS','All active Gamma markets before liquidity and price filters.'],
  wx:['WEATHER TRADER','Compares Open-Meteo forecast to Polymarket implied probability. Enters when gap exceeds 8%.'],
  crypto:['CRYPTO TRADER','Log-normal price model vs Polymarket implied probability. Enters when gap exceeds 10%.'],
  edge:['EDGE','Model probability minus market price. Minimum threshold to open a position.'],
  mktPct:['MARKET %','Current Polymarket implied probability — what the crowd is pricing.'],
  mdlPct:['MODEL %','Our model estimate from forecast or price data.'],
  side:['SIDE','YES = bet it happens. NO = bet it does not.'],
  entry:['ENTRY PRICE','Price paid when the position was opened. 0.0–1.0 scale = 0–100% probability.'],
  now:['CURRENT PRICE','Latest Polymarket price for this outcome. Updates each scan.'],
  unreal:['UNREALISED P&L','What you would make or lose if you closed this position right now.'],
  hrs:['HOURS TO CLOSE','Time until this market resolves. Under 4h triggers a time-stop exit.'],
  yesPx:['YES PRICE','Implied probability YES resolves. 0.30 means market thinks 30% chance.'],
  noPx:['NO PRICE','Complement of YES. 1 minus YES price.'],
  liq:['LIQUIDITY','Total liquidity in USD. We skip markets below $500.'],
  reason:['EXIT REASON','Why this position was closed.'],
  scanTime:['SCAN DURATION','End-to-end time for the last scan: Gamma fetch + forecasts + evaluation.'],
  dailyPnl:['TODAY P&L','Net profit and loss from all trades opened and closed since midnight UTC.'],
  bestEdge:['BEST EDGE TODAY','Largest edge opportunity detected across all of today scan cycles.'],
  navHist:['NAV HISTORY','Net Asset Value trend across the last 20 scan cycles. Green = growing.'],
  spot:['SPOT PRICE','Current CoinGecko spot price in USD. Cached for 60 seconds.'],
  sigma:['DAILY VOL (σ)','30-day rolling daily volatility from log returns of closing prices.'],
};

// Tooltip
const tip=document.getElementById('tip'),tl=document.getElementById('tl'),tb=document.getElementById('tb');
document.addEventListener('mousemove',e=>{
  const el=e.target.closest('[data-tip]');
  if(!el){tip.style.display='none';return;}
  const raw=TIPS[el.dataset.tip];
  if(!raw){tip.style.display='none';return;}
  const info=typeof raw==='function'?raw(lastData&&lastData.maxPositions||10):raw;
  tl.textContent=info[0];tb.textContent=info[1];
  tip.style.display='block';
  const tw=224,th=tip.offsetHeight||80;
  let x=e.clientX+14,y=e.clientY-th-10;
  if(y<4)y=e.clientY+18;
  if(x+tw>window.innerWidth-4)x=e.clientX-tw-14;
  tip.style.left=x+'px';tip.style.top=y+'px';
});
document.addEventListener('mouseleave',()=>{tip.style.display='none';});

// ── Zone layout (persisted to localStorage) ───────────────────────────────────
// Zones are fixed grid slots. Modules are draggable content cards.
// Dragging a module header swaps two modules between zones.
const DEFAULT_LAYOUT={left:'scanner',ct:'positions',cb:'opportunities',rt:'pnl',rm:'wxfeed',rb:'closed'};
let layout=JSON.parse(localStorage.getItem('pb-zones')||'null')||{...DEFAULT_LAYOUT};

const MOD_META={
  scanner:      {col:'#00d4ff',title:'SCANNER'},
  positions:    {col:'#3ddc84',title:'OPEN POSITIONS'},
  opportunities:{col:'#ffd166',title:'OPPORTUNITIES'},
  pnl:          {col:'#c77dff',title:'P&L METRICS'},
  wxfeed:       {col:'#ff9a3c',title:'WX FEED'},
  closed:       {col:'#58a6ff',title:'CLOSED'},
};

// ── Drag & drop ───────────────────────────────────────────────────────────────
let dragSrc=null,isDragging=false;

function setupDnd(){
  document.querySelectorAll('.mh').forEach(h=>{
    h.setAttribute('draggable','true');
    h.addEventListener('dragstart',e=>{
      dragSrc=h.closest('.zone').dataset.zone;
      isDragging=true;
      e.dataTransfer.effectAllowed='move';
    });
    h.addEventListener('dragend',()=>{isDragging=false;});
  });
  document.querySelectorAll('.zone').forEach(z=>{
    z.addEventListener('dragover',e=>{e.preventDefault();z.classList.add('drag-over');});
    z.addEventListener('dragleave',e=>{if(!z.contains(e.relatedTarget))z.classList.remove('drag-over');});
    z.addEventListener('drop',e=>{
      e.preventDefault();z.classList.remove('drag-over');
      const dst=z.dataset.zone;
      if(dragSrc&&dst!==dragSrc){
        [layout[dst],layout[dragSrc]]=[layout[dragSrc],layout[dst]];
        localStorage.setItem('pb-zones',JSON.stringify(layout));
        if(lastData)fullRender(lastData);
      }
      dragSrc=null;isDragging=false;
    });
  });
}

// ── Helpers ───────────────────────────────────────────────────────────────────
const pn=n=>String(n).padStart(2,'0');
function fmtNow(){const d=new Date();return`${pn(d.getUTCHours())}:${pn(d.getUTCMinutes())}:${pn(d.getUTCSeconds())} UTC`;}
function t(id,html){return`<span data-tip="${id}">${html}</span>`;}
function pc(v){return v>=0?C.green:C.red;}
function hc(h){return h<4?C.red:h<12?C.yellow:C.dim;}
function ec(e){return e>=0.25?C.green:e>=0.12?C.yellow:C.muted;}
function spark(vals,len=16){
  const c=' ▁▂▃▄▅▆▇█';
  if(!vals||!vals.length)return'─'.repeat(len);
  const lo=Math.min(...vals),hi=Math.max(...vals),sp=hi-lo||1;
  return vals.slice(-len).map(v=>c[Math.round(((v-lo)/sp)*8)]).join('');
}
function bar(lbl,cnt,tot,col){
  const f=tot>0?Math.round((cnt/tot)*12):0;
  const b='█'.repeat(f)+'░'.repeat(Math.max(0,12-f));
  return`<div style="display:flex;align-items:center;gap:5px;padding:1px 0">
    <span style="color:${col};width:26px;flex-shrink:0">${lbl}</span>
    <span style="color:${col};opacity:.5;flex:1;overflow:hidden;font-size:10px">${b}</span>
    <span style="color:${C.dim};width:22px;text-align:right">${cnt}</span>
  </div>`;
}
function kv(id,lbl,val){return`<div class="kv"><span class="kl" data-tip="${id}">${lbl}</span><span style="font-weight:600">${val}</span></div>`;}
function fmtSpot(n){if(n==null)return'—';if(n>=1000)return'$'+Math.round(n).toLocaleString();if(n>=1)return'$'+n.toFixed(2);return'$'+n.toFixed(4);}

// Question condensers
function condense(q){
  var cityM=q.match(/in ([A-Za-z ]+?) be/i);
  var city=cityM?cityM[1].trim():'';
  var tempM=q.match(/([0-9]+(?:[- ][0-9]+)?)[ ]*[°]?([CF])\\b/i);
  var temp='';
  if(tempM){var r=tempM[1].replace(/ *- */g,'-');temp=r+'°'+tempM[2].toUpperCase();}
  if(/or below/i.test(q)&&temp)temp='≤'+temp;else if(/or higher|or above/i.test(q)&&temp)temp='≥'+temp;
  var dateM=q.match(/on (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* ([0-9]{1,2})/i);
  var date=dateM?(dateM[1][0].toUpperCase()+dateM[1].slice(1,3).toLowerCase()+' '+dateM[2]):'';
  return [city,temp,date].filter(Boolean).join(' · ');
}
const CMAP={bitcoin:'BTC',ethereum:'ETH',solana:'SOL',ripple:'XRP',xrp:'XRP',dogecoin:'DOGE',doge:'DOGE',bnb:'BNB'};
const CRYPTO_RE=/\\b(bitcoin|ethereum|solana|ripple|xrp|dogecoin|doge|bnb)\\b/i;
function condenseCrypto(q){
  const coinM=q.match(CRYPTO_RE);
  const coin=coinM?(CMAP[coinM[1].toLowerCase()]||coinM[1].toUpperCase()):'';
  const betM=q.match(/between \\$?([\\d,.]+[kmb]?) (?:and|-|–) \\$?([\\d,.]+[kmb]?)/i);
  if(betM)return`${coin} · $${betM[1]}–$${betM[2]}`;
  const abvM=q.match(/(?:above|higher than|over) \\$?([\\d,.]+[kmb]?)/i);
  if(abvM)return`${coin} · >$${abvM[1]}`;
  const blwM=q.match(/(?:below|lower than|under) \\$?([\\d,.]+[kmb]?)/i);
  if(blwM)return`${coin} · <$${blwM[1]}`;
  return coin+' · '+q.slice(0,35);
}

// ── Tab ───────────────────────────────────────────────────────────────────────
let currentPage='wx';
function showPage(p){
  currentPage=p;
  document.querySelectorAll('.tab').forEach(b=>b.classList.toggle('active',b.dataset.page===p));
  if(lastData)fullRender(lastData);
}

// ── Module content builders (return HTML for .mb) ─────────────────────────────

function modScanner(S){
  const {scanNum,status,lastScan,interval,forecasts,total,wx,crypto,politics,sports,other,scanHistory,dailyPnl,dailyOpened,dailyClosed,bestEdge}=S;
  return`
    ${kv('scanNum','STATUS',`<span style="color:${status==='RUNNING'?C.green:C.yellow}">${status}</span>`)}
    ${kv('scanNum','SCAN #',`<span style="color:${C.cyan}">${scanNum}</span>`)}
    ${kv('interval','LAST',`<span style="color:${C.dim}">${lastScan}</span>`)}
    ${kv('interval','INTERVAL',`<span style="color:${C.dim}">${interval}s</span>`)}
    ${kv('forecasts','FORECASTS',`<span style="color:${C.blue}">${forecasts}</span>`)}
    <div class="sep"></div>
    ${kv('markets','MARKETS',String(total))}
    ${bar('WX',wx,total,C.cyan)}
    ${bar('B',crypto,total,C.yellow)}
    ${bar('POL',politics,total,C.magenta)}
    ${bar('SPT',sports,total,C.green)}
    ${bar('OTH',other,total,C.dim)}
    <div class="sep"></div>
    <div style="color:${C.dim};font-size:9px;margin-bottom:3px">${t('scanTime','SCAN TIME')}</div>
    <div style="color:${C.blue};font-size:11px">${spark(scanHistory)}</div>
    <div class="sep"></div>
    <div style="color:${C.dim};font-size:9px;margin-bottom:3px">TODAY</div>
    ${kv('dailyPnl','P&L',`<span style="color:${pc(dailyPnl)}">${dailyPnl>=0?'&#9650;':'&#9660;'} $${dailyPnl>=0?'+':''}${(dailyPnl||0).toFixed(2)}</span>`)}
    ${kv('openPos','OPENED',`<span style="color:${C.cyan}">${dailyOpened}</span>`)}
    ${kv('closed','CLOSED',`<span style="color:${C.blue}">${dailyClosed}</span>`)}
    ${kv('bestEdge','BEST EDGE',`<span style="color:${C.green}">+${((bestEdge||0)*100).toFixed(1)}%</span>`)}`;
}

function modPositions(S,fm){
  const isCrypto=currentPage==='crypto';
  const positions=(S.positions||[]).filter(p=>isCrypto?CRYPTO_RE.test(p.q):!CRYPTO_RE.test(p.q));
  const cf=isCrypto?condenseCrypto:condense;
  if(!positions.length)return`<div style="color:${C.dim};text-align:center;padding:20px 0">no open positions</div>`;
  const hdr=`<div style="display:grid;grid-template-columns:90px 1fr 42px 54px 54px 84px 46px;gap:0 6px;margin-bottom:4px">
    ${[['ID','scanNum'],['MARKET','market'],['SIDE','side'],['ENTRY','entry'],['NOW','now'],['UNREAL','unreal'],['AGE','hrs']].map(([h,id])=>`<span class="gh" data-tip="${id}">${h}</span>`).join('')}
  </div><div style="border-bottom:1px solid rgba(255,255,255,.06);margin-bottom:3px"></div>`;
  const rows=positions.map(p=>{
    const m=fm[p.mid],cy=m?m.yes:null,cs=cy!=null?(p.side==='YES'?cy:1-cy):null,ur=cs!=null?((cs-p.entry)*p.shares):null;
    const age=p.openedTs?Math.floor((Date.now()/1000-p.openedTs)/60):null;
    const ageStr=age==null?'—':age<60?age+'m':Math.floor(age/60)+'h';
    return`<div style="display:grid;grid-template-columns:90px 1fr 42px 54px 54px 84px 46px;gap:0 6px" class="gr">
      <span style="color:${C.dim}">${p.id.slice(0,10)}</span>
      <span style="color:${C.text}" class="oc" title="${p.q}">${cf(p.q)}</span>
      <span style="color:${p.side==='YES'?C.green:C.red};text-align:center">${t('side',p.side)}</span>
      <span style="color:${C.yellow};text-align:right">${t('entry',p.entry.toFixed(3))}</span>
      <span style="color:${C.white};text-align:right">${t('now',cs!=null?cs.toFixed(3):'—')}</span>
      <span style="color:${ur!=null?(ur>=0?C.green:C.red):C.dim};text-align:right">${t('unreal',ur!=null?`${ur>=0?'&#9650;':'&#9660;'} $${ur>=0?'+':''}${ur.toFixed(2)}`:'—')}</span>
      <span style="color:${C.dim};text-align:right">${ageStr}</span>
    </div>`;
  }).join('');
  return hdr+rows;
}

function modOpportunities(S){
  const isCrypto=currentPage==='crypto';
  const opps=(S.opps||[]).filter(o=>isCrypto?o.strat!=='WX':o.strat==='WX');
  const cf=isCrypto?condenseCrypto:condense;
  if(!opps.length)return`<div style="color:${C.dim};text-align:center;padding:12px 0">scanning...</div>`;
  const hdr=`<div style="display:grid;grid-template-columns:32px 1fr 42px 54px 54px 58px;gap:0 6px;margin-bottom:4px">
    ${[['ST','wx'],['MARKET','market'],['SIDE','side'],['MKT%','mktPct'],['MDL%','mdlPct'],['EDGE','edge']].map(([h,id])=>`<span class="gh" data-tip="${id}">${h}</span>`).join('')}
  </div><div style="border-bottom:1px solid rgba(255,255,255,.06);margin-bottom:3px"></div>`;
  const rows=opps.map(o=>`
    <div style="display:grid;grid-template-columns:32px 1fr 42px 54px 54px 58px;gap:0 6px" class="gr">
      <span data-tip="${isCrypto?'crypto':'wx'}" style="color:${C.blue}">${o.strat}</span>
      <span style="color:${C.text}" class="oc" title="${o.q}">${cf(o.q)}</span>
      <span style="color:${o.side==='YES'?C.green:C.red};text-align:center">${t('side',o.side)}</span>
      <span style="color:${C.muted};text-align:right">${t('mktPct',((o.mkt||0)*100).toFixed(1)+'%')}</span>
      <span style="color:${C.white};text-align:right">${t('mdlPct',((o.mdl||0)*100).toFixed(1)+'%')}</span>
      <span style="color:${ec(o.edge||0)};text-align:right">${t('edge','+'+((o.edge||0)*100).toFixed(1)+'%')}</span>
    </div>`).join('');
  return hdr+rows;
}

function modPnl(S){
  const tr=S.trader||{},nav=S.navHistory||[];
  return`
    ${kv('balance','BALANCE',`$${(tr.balance||0).toFixed(2)}`)}
    ${kv('openVal','OPEN VAL',`<span style="color:${C.blue}">$${(tr.openVal||0).toFixed(2)}</span>`)}
    ${kv('nav','NAV',`<span style="color:${C.cyan}">$${(tr.nav||0).toFixed(2)}</span>`)}
    <div class="sep"></div>
    ${kv('totalPnl','TOTAL P&L',`<span style="color:${pc(tr.totalPnl||0)}">${(tr.totalPnl||0)>=0?'&#9650;':'&#9660;'} $${(tr.totalPnl||0)>=0?'+':''}${(tr.totalPnl||0).toFixed(2)} (${(tr.pnlPct||0)>=0?'+':''}${(tr.pnlPct||0).toFixed(2)}%)</span>`)}
    <div style="margin:3px 0">${t('navHist',`<span style="color:${(tr.totalPnl||0)>=0?C.green:C.red};font-size:11px">${spark(nav)}</span>`)}</div>
    <div class="sep"></div>
    ${kv('closed','CLOSED',`${tr.closed||0} <span style="color:${C.green}">&#10003;${tr.wins||0}</span> <span style="color:${C.red}">&#10007;${tr.losses||0}</span>`)}
    ${kv('winRate','WIN RATE',`<span style="color:${(tr.winRate||0)>=.55?C.green:(tr.winRate||0)>=.45?C.yellow:C.red}">${((tr.winRate||0)*100).toFixed(0)}%</span>`)}
    ${kv('openPos','OPEN POS',`<span style="color:${C.cyan}">${tr.openPos||0}</span><span style="color:${C.dim}">/${S.maxPositions||10}</span>`)}
    ${tr.avgWin!=null?kv('avgWin','AVG WIN',`<span style="color:${C.green}">$+${tr.avgWin.toFixed(2)}</span>`):''}
    ${tr.avgLoss!=null?kv('avgLoss','AVG LOSS',`<span style="color:${C.red}">$${tr.avgLoss.toFixed(2)}</span>`):''}
    ${kv('kelly','KELLY',`<span style="color:${C.yellow}">~25% <span style="color:${C.dim}">(¼ kelly)</span></span>`)}`;
}

function modWxfeed(S){
  const isCrypto=currentPage==='crypto';
  const feed=isCrypto?(S.cryptoFeed||[]):(S.feed||[]);
  const cf=isCrypto?condenseCrypto:condense;
  if(!feed.length)return`<div style="color:${C.dim};text-align:center;padding:12px 0">fetching...</div>`;

  let prefix='';
  if(isCrypto){
    const coinMap={};
    feed.forEach(m=>{
      if(m.coin_id&&m.spot_usd!=null&&!coinMap[m.coin_id])coinMap[m.coin_id]={spot:m.spot_usd,sigma:m.sigma_daily,count:0};
      if(m.coin_id&&coinMap[m.coin_id])coinMap[m.coin_id].count++;
    });
    const CL={bitcoin:'BTC',ethereum:'ETH',solana:'SOL',ripple:'XRP',dogecoin:'DOGE',bnb:'BNB'};
    if(Object.keys(coinMap).length){
      prefix=Object.entries(coinMap).map(([cid,d])=>
        `<div class="gr" style="display:grid;grid-template-columns:36px 1fr 70px 50px;gap:0 6px">
          <span style="color:${C.yellow};font-weight:700">${CL[cid]||cid.slice(0,4).toUpperCase()}</span>
          <span style="color:${C.white};text-align:right">${t('spot',fmtSpot(d.spot))}</span>
          <span style="color:${C.dim};text-align:right">${t('sigma',d.sigma!=null?'σ '+(d.sigma*100).toFixed(1)+'%/d':'σ —')}</span>
          <span style="color:${C.dim};text-align:right;font-size:9px">${d.count}mkt</span>
        </div>`
      ).join('')+'<div class="sep"></div>';
    }
  }

  const cols=isCrypto?'1fr 46px 62px 38px':'1fr 46px 46px 62px 38px';
  const hdrs=isCrypto?[['MARKET','market'],['YES','yesPx'],['LIQ','liq'],['HRS','hrs']]
                     :[['MARKET','market'],['YES','yesPx'],['NO','noPx'],['LIQ','liq'],['HRS','hrs']];
  const hdr=`<div style="display:grid;grid-template-columns:${cols};gap:0 5px;margin-bottom:4px">
    ${hdrs.map(([h,id],i)=>`<span class="gh" data-tip="${id}" style="text-align:${i?'right':'left'}">${h}</span>`).join('')}
  </div><div style="border-bottom:1px solid rgba(255,255,255,.06);margin-bottom:3px"></div>`;

  const rows=feed.map(m=>{
    const yes=m.yes_price||0,hrs=m.hours_until_close||0,liq=m.liquidity_usd||0;
    const cells=isCrypto
      ?`<span style="color:${yes>=.5?C.green:C.red};text-align:right">${t('yesPx',yes.toFixed(3))}</span>
         <span style="color:${C.dim};text-align:right">${t('liq','$'+liq.toLocaleString())}</span>
         <span style="color:${hc(hrs)};text-align:right">${t('hrs',Math.round(hrs)+'h')}</span>`
      :`<span style="color:${yes>=.5?C.green:C.red};text-align:right">${t('yesPx',yes.toFixed(3))}</span>
         <span style="color:${C.dim};text-align:right">${t('noPx',(1-yes).toFixed(3))}</span>
         <span style="color:${C.dim};text-align:right">${t('liq','$'+liq.toLocaleString())}</span>
         <span style="color:${hc(hrs)};text-align:right">${t('hrs',Math.round(hrs)+'h')}</span>`;
    return`<div style="display:grid;grid-template-columns:${cols};gap:0 5px" class="gr">
      <span style="color:${C.text}" class="oc" title="${m.question||''}">${cf(m.question||'')}</span>${cells}
    </div>`;
  }).join('');
  return prefix+hdr+rows;
}

function modClosed(S){
  const closed=S.closed||[];
  if(!closed.length)return`<div style="color:${C.dim};text-align:center;padding:16px 0">no closed trades</div>`;
  const hdr=`<div style="display:grid;grid-template-columns:90px 40px 52px 52px 1fr;gap:0 5px;margin-bottom:4px">
    ${[['ID','scanNum'],['SIDE','side'],['IN','entry'],['OUT','now'],['P&L','totalPnl']].map(([h,id],i)=>`<span class="gh" data-tip="${id}" style="text-align:${i>1?'right':'left'}">${h}</span>`).join('')}
  </div><div style="border-bottom:1px solid rgba(255,255,255,.06);margin-bottom:3px"></div>`;
  const rows=closed.map(tr=>
    `<div style="display:grid;grid-template-columns:90px 40px 52px 52px 1fr;gap:0 5px" class="gr">
      <span style="color:${C.dim}">${tr.id.slice(0,10)}</span>
      <span style="color:${tr.side==='YES'?C.green:C.red}">${tr.side}</span>
      <span style="color:${C.dim};text-align:right">${(tr.entry||0).toFixed(3)}</span>
      <span style="color:${C.white};text-align:right">${(tr.exit||0).toFixed(3)}</span>
      <span data-tip="reason" style="color:${pc(tr.pnl||0)};text-align:right">${(tr.pnl||0)>=0?'&#10003;':'&#10007;'} $${(tr.pnl||0)>=0?'+':''}${(tr.pnl||0).toFixed(2)}</span>
    </div>`
  ).join('');
  return hdr+rows;
}

// ── Zone renderer — places a module into a zone ───────────────────────────────
function renderZone(zoneId,S,fm){
  const modId=layout[zoneId]||'scanner';
  const meta=MOD_META[modId]||{col:C.dim,title:modId.toUpperCase()};
  const el=document.getElementById('z-'+zoneId);
  if(!el)return;

  // Count badge
  const counts={
    positions:(S.positions||[]).filter(p=>currentPage==='crypto'?CRYPTO_RE.test(p.q):!CRYPTO_RE.test(p.q)).length,
    opportunities:(S.opps||[]).filter(o=>currentPage==='crypto'?o.strat!=='WX':o.strat==='WX').length,
    wxfeed:currentPage==='crypto'?(S.cryptoFeed||[]).length:(S.feed||[]).length,
    closed:(S.closed||[]).length,
  };
  const cnt=counts[modId]!=null?`&nbsp;<span style="color:${C.white}">${counts[modId]}</span>`:'';

  const BUILDERS={scanner:()=>modScanner(S),positions:()=>modPositions(S,fm),opportunities:()=>modOpportunities(S),pnl:()=>modPnl(S),wxfeed:()=>modWxfeed(S),closed:()=>modClosed(S)};
  const body=BUILDERS[modId]?BUILDERS[modId]():'';

  el.innerHTML=`<div class="module">
    <div class="mh" style="color:${meta.col}">
      &#9672; ${meta.title}${cnt}
      <span class="grip" title="Drag to move a different zone">&#8942;&#8942;</span>
    </div>
    <div class="mb">${body}</div>
  </div>`;
}

// ── Full render ───────────────────────────────────────────────────────────────
let lastData=null,lastScanNum=-1;

function fullRender(S){
  lastData=S;
  const fm={};
  [...(S.feed||[]),...(S.cryptoFeed||[])].forEach(m=>{fm[m.id]={yes:m.yes_price||0,hrs:m.hours_until_close||0};});

  // Header
  const sh=S.scanHistory||[],lastDur=sh.length?sh[sh.length-1].toFixed(1):'-';
  document.getElementById('hdr').innerHTML=`
    <span id="hpulse" style="color:${C.green}">&#9679; SCANNING</span>
    <span style="color:${C.dim}">
      scan&nbsp;<span style="color:${C.cyan}">#${S.scanNum}</span>
      &nbsp;&#183;&nbsp;next in&nbsp;<span id="hcountdown" style="color:${C.cyan}">${S.nextScanIn}</span>s
      &nbsp;&#183;&nbsp;last&nbsp;<span style="color:${C.yellow}">${lastDur}s</span>
    </span>
    <div style="display:flex;gap:5px;margin-left:8px">
      <button class="tab${currentPage==='wx'?' active':''}" data-page="wx" onclick="showPage('wx')">&#9672; WEATHER</button>
      <button class="tab${currentPage==='crypto'?' active':''}" data-page="crypto" onclick="showPage('crypto')">&#9672; CRYPTO</button>
    </div>
    <span style="margin-left:auto;color:${C.dim}">&#9201;&nbsp;<span id="hclock" style="color:${C.white}">${fmtNow()}</span></span>`;

  // Zones
  ['left','ct','cb','rt','rm','rb'].forEach(z=>renderZone(z,S,fm));

  // Event log
  const lvC={INFO:C.text,TRADE:C.cyan,EXIT:C.magenta,GOOD:C.green,WARN:C.yellow,ERROR:C.red};
  const lvI={INFO:'·',TRADE:'◉',EXIT:'◎',GOOD:'✓',WARN:'⚠',ERROR:'✗'};
  const lb=document.getElementById('logbody');
  lb.innerHTML=(S.events||[]).map(e=>{
    const lc=lvC[e.lv]||C.text,li=lvI[e.lv]||'·';
    return`<div style="display:flex;gap:10px;padding:1px 0">
      <span style="color:${C.dim};flex-shrink:0">${e.ts}</span>
      <span style="color:${lc};flex-shrink:0">${li}</span>
      <span style="color:${lc}">${e.msg}</span>
    </div>`;
  }).join('');
  lb.scrollTop=lb.scrollHeight;

  setupDnd();
}

// ── Tick update (clock + countdown only — no full re-render) ──────────────────
let pulse=true;
function tickUpdate(nextScanIn){
  pulse=!pulse;
  const hp=document.getElementById('hpulse');if(hp)hp.textContent=(pulse?'● ':'○ ')+'SCANNING';
  const hcd=document.getElementById('hcountdown');if(hcd)hcd.textContent=nextScanIn;
  const hck=document.getElementById('hclock');if(hck)hck.textContent=fmtNow();
}

// ── WebSocket ─────────────────────────────────────────────────────────────────
let ws,reconnectTimer,tickTimer;
let lastMsgAt=0;
const conn=document.getElementById('conn');

function onMessage(S){
  lastMsgAt=Date.now();
  if(lastScanNum!==S.scanNum){lastScanNum=S.scanNum;fullRender(S);}
  lastData=S;
  tickUpdate(S.nextScanIn);
}

function connect(){
  clearInterval(tickTimer);
  const proto=location.protocol==='https:'?'wss:':'ws:';
  ws=new WebSocket(`${proto}//${location.host}/ws`);
  ws.onopen=()=>{
    conn.innerHTML='<span style="color:#3ddc84">&#9899; live</span>';
    clearTimeout(reconnectTimer);
    tickTimer=setInterval(()=>{
      if(lastData)tickUpdate(lastData.nextScanIn>0?lastData.nextScanIn-1:0);
      const lag=Date.now()-lastMsgAt;
      if(lag>5000)conn.innerHTML=`<span style="color:#ffd166">&#9899; stale ${(lag/1000).toFixed(0)}s</span>`;
      else conn.innerHTML='<span style="color:#3ddc84">&#9899; live</span>';
    },1000);
  };
  ws.onmessage=e=>{try{onMessage(JSON.parse(e.data));}catch(err){console.error(err);}};
  ws.onclose=()=>{clearInterval(tickTimer);conn.innerHTML='<span style="color:#ff5c5c">&#9899; reconnecting...</span>';reconnectTimer=setTimeout(connect,2000);};
  ws.onerror=()=>ws.close();
}
connect();
</script>
</body>
</html>"""