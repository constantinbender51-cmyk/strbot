#!/usr/bin/env python3
import base64, hashlib, hmac, logging, os, sys, time, urllib.parse
from typing import Dict, List
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger()

try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass

KEY, SECRET = os.getenv("KRAKEN_FUTURES_KEY"), os.getenv("KRAKEN_FUTURES_SECRET")
BASE = "https://futures.kraken.com"
SYM      = "PF_XBTUSD"
LEVERAGE = 0.2          # fraction of equity to deploy (0.2 = 20%)

# ── auth ──────────────────────────────────────────────────────────────────────
_nc = 0
def _nonce():
    global _nc; _nc = (_nc + 1) % 10_000
    return f"{int(time.time()*1000)}{_nc:05d}"

def _sign(endpoint, nonce, body=""):
    path   = endpoint[12:] if endpoint.startswith("/derivatives") else endpoint
    digest = hashlib.sha256((body + nonce + path).encode()).digest()
    return base64.b64encode(hmac.new(base64.b64decode(SECRET), digest, hashlib.sha512).digest()).decode()

def req(method, endpoint, params=None):
    params, nonce, body = params or {}, _nonce(), ""
    url  = BASE + endpoint
    hdrs = {"APIKey": KEY, "Nonce": nonce, "User-Agent": "kp/1"}
    if method == "POST":
        body = urllib.parse.urlencode(params)
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    elif params:
        url += "?" + urllib.parse.urlencode(params)
    hdrs["Authent"] = _sign(endpoint, nonce, body)
    r = requests.request(method, url, headers=hdrs, data=body or None, timeout=10)
    if not r.ok: raise RuntimeError(f"{method} {endpoint} → {r.status_code}: {r.text}")
    return r.json()

# ── specs ─────────────────────────────────────────────────────────────────────
specs: Dict[str, Dict] = {}

def load_specs():
    for i in req("GET", "/derivatives/api/v3/instruments").get("instruments", []):
        specs[i["symbol"].upper()] = {
            "p": int(i.get("contractValueTradePrecision", 0)),
            "t": float(i.get("tickSize", 0.5)),
        }
    s = specs.get(SYM, {})
    log.info(f"Specs {SYM}: contractValueTradePrecision={s['p']}  tickSize={s['t']}")

def _t(sym): return specs.get(sym.upper(), {}).get("t", 0.5)
def _p(sym): return specs.get(sym.upper(), {}).get("p", 0)

def fmt_size(sym, v):
    p = _p(sym)
    if p >= 0: return f"{v:.{p}f}"
    d = 10**abs(p); return str(int(round(v/d)*d))

def fmt_price(sym, v):
    t = _t(sym); r = round(v/t)*t
    ts = f"{t:.10f}".rstrip("0")
    dec = len(ts.split(".")[1]) if "." in ts else 0
    return f"{r:.{dec}f}"

def tr(sym, v):          # tick-round to float
    t = _t(sym); return round(v/t)*t

def lr(sym, v):          # lot-round to float
    p = _p(sym)
    if p >= 0: return round(v, p)
    d = 10**abs(p); return float(round(v/d)*d)

# ── orders ────────────────────────────────────────────────────────────────────
order_ids: Dict[str, str] = {}

def _send(params, label=None):
    st = req("POST", "/derivatives/api/v3/sendorder", params).get("sendStatus", {})
    if st.get("status") != "placed":
        log.error(f"NOT placed: status={st.get('status')}  error={st.get('error')}  {st}")
        sys.exit(1)
    oid = st.get("order_id") or st.get("orderId", "")
    log.info(f"  ✓ {oid}")
    if label: order_ids[label] = oid
    return oid

def send_lmt(sym, side, size, price, label=None):
    sym = sym.upper()
    log.info(f"LMT {side.upper()} {fmt_size(sym,lr(sym,size))} {sym} @ {fmt_price(sym,price)}")
    return _send({"orderType":"lmt","symbol":sym.lower(),"side":side,
                  "size":fmt_size(sym,lr(sym,size)),"limitPrice":fmt_price(sym,price),
                  "cliOrdId":f"p_{int(time.time()*1000)}"}, label)

def send_stp(sym, side, size, stop_price, label=None):
    sym = sym.upper()
    log.info(f"STP {side.upper()} {fmt_size(sym,lr(sym,size))} {sym} stop@{fmt_price(sym,stop_price)}")
    return _send({"orderType":"stp","symbol":sym.lower(),"side":side,
                  "size":fmt_size(sym,lr(sym,size)),"stopPrice":fmt_price(sym,stop_price),
                  "cliOrdId":f"s_{int(time.time()*1000)}"}, label)

def edit_order(oid, sym, price, is_stop=False):
    pk = "stopPrice" if is_stop else "limitPrice"
    log.info(f"EDIT {oid}  {pk}={fmt_price(sym,price)}")
    r = req("POST", "/derivatives/api/v3/editorder", {"orderId":oid,"symbol":sym.lower(),pk:fmt_price(sym,price)})
    log.info(f"  → {r.get('editStatus',{}).get('status')}")

def cancel_order(oid):
    log.info(f"CANCEL {oid}")
    r = req("POST", "/derivatives/api/v3/cancelorder", {"order_id": oid})
    log.info(f"  → {r.get('cancelStatus',{}).get('status')}")

def cancel_all(sym=None):
    log.info(f"CANCEL ALL{' '+sym if sym else ''}")
    req("POST", "/derivatives/api/v3/cancelallorders", {"symbol":sym.lower()} if sym else {})

# ── positions & account ───────────────────────────────────────────────────────
def get_positions() -> List[Dict]:
    pos = req("GET", "/derivatives/api/v3/openpositions").get("openPositions", [])
    if not pos: log.info("  (flat)")
    for p in pos: log.info(f"  {p['symbol']}  {p['side']}  sz={p['size']}")
    return pos

def get_equity():
    accts = req("GET", "/derivatives/api/v3/accounts").get("accounts", {})
    flex  = accts.get("flex", {})
    eq = float(flex.get("marginEquity",0)) if flex else sum(float(v.get("marginEquity",0)) for v in accts.values())
    log.info(f"Equity: ${eq:,.2f}"); return eq

def get_mark(sym):
    for t in req("GET", "/derivatives/api/v3/tickers").get("tickers", []):
        if t["symbol"].upper() == sym.upper(): return float(t["markPrice"])
    raise ValueError(f"No ticker {sym}")

def close_position_lmt(sym):
    pos = next((p for p in get_positions() if p["symbol"].upper()==sym.upper()), None)
    if not pos: return log.info("No position to close")
    side  = "sell" if pos["side"]=="long" else "buy"
    mark  = get_mark(sym)
    return send_lmt(sym, side, float(pos["size"]),
                    tr(sym, mark*0.999 if side=="sell" else mark*1.001), label="close")

def close_all_mkt(symbols):
    """Close positions only for the given symbols (list or single string)."""
    syms = [s.upper() for s in ([symbols] if isinstance(symbols, str) else symbols)]
    for pos in get_positions():
        sym = pos["symbol"].upper()
        if sym not in syms: continue
        side = "sell" if pos["side"]=="long" else "buy"
        sz   = fmt_size(sym, lr(sym, float(pos["size"])))
        log.info(f"MKT CLOSE {sym} {side} {sz}")
        req("POST", "/derivatives/api/v3/sendorder",
            {"orderType":"mkt","symbol":sym.lower(),"side":side,"size":sz,
             "cliOrdId":f"c_{int(time.time()*1000)}"})

def wait_fill(sym, target_size, timeout=120, poll=3):
    """Poll openPositions until size is within 10% of target, or timeout."""
    log.info(f"Waiting fill: target={target_size} {sym} (max {timeout}s)")
    dead = time.time() + timeout
    while time.time() < dead:
        pos = next((p for p in req("GET", "/derivatives/api/v3/openpositions")
                    .get("openPositions", []) if p["symbol"].upper()==sym.upper()), None)
        actual = float(pos["size"]) if pos else 0.0
        log.info(f"  position size={actual}")
        if target_size > 0 and actual >= target_size * 0.9:
            log.info("  ✓ filled"); return True
        time.sleep(poll)
    log.warning("  timeout"); return False

# ── Binance OHLC ──────────────────────────────────────────────────────────────
def get_btc_ohlc_5m(pair="BTCUSDT"):
    now = int(time.time()*1000)
    r   = requests.get("https://api.binance.com/api/v3/klines",
                       params={"symbol":pair,"interval":"5m",
                               "startTime":now-86400000,"endTime":now,"limit":1000}, timeout=10)
    if not r.ok: raise RuntimeError(f"Binance {r.status_code}: {r.text}")
    data = [{"ts":k[0],"o":float(k[1]),"h":float(k[2]),"l":float(k[3]),"c":float(k[4]),"v":float(k[5])} for k in r.json()]
    log.info(f"Binance {pair} 5m: {len(data)} candles  last_close={data[-1]['c']}")
    return data

# ── startup sequence ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not KEY or not SECRET:
        log.error("Set KRAKEN_FUTURES_KEY and KRAKEN_FUTURES_SECRET"); sys.exit(1)

    get_btc_ohlc_5m()
    load_specs()
    equity = get_equity()

    mark = get_mark(SYM)
    size = lr(SYM, equity * LEVERAGE / mark)
    log.info(f"Mark: {mark}  Equity: {equity}  Size: {size} contracts (leverage={LEVERAGE})")
    get_positions()

    # place → edit → cancel a passive limit (tests edit + cancel by ID)
    eid = send_lmt(SYM, "buy", size, tr(SYM, mark*0.99),  label="entry")
    edit_order(eid, SYM, tr(SYM, mark*0.995))
    cancel_order(eid)

    # aggressive limit to actually fill
    eid2 = send_lmt(SYM, "buy", size, tr(SYM, mark*1.001), label="entry2")

    if wait_fill(SYM, size):
        sid = send_stp(SYM, "sell", size, tr(SYM, mark*0.98), label="stop")
        edit_order(sid, SYM, tr(SYM, mark*0.985), is_stop=True)
        close_position_lmt(SYM)          # opposite lmt, same size
        time.sleep(3)
        get_positions()                   # confirm flat
    else:
        log.info("Not filled — skipping stop/close")

    cancel_all(SYM)
    close_all_mkt(SYM)
    log.info(f"IDs: {order_ids}")
