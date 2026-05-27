"""
trader.py  ·  Variant B  ·  Goldman Snacks
─────────────────────────────────────────────────────────────────────────────
Changes vs baseline (10,387 live):
  ✔  Price-deviation skew added to passive quote placement.
       When mid > fv+2: shift both quotes DOWN 1 tick
         → ask becomes more attractive to sell into, captures mean-reversion
       When mid < fv-2: shift both quotes UP 1 tick
         → bid becomes more attractive to buy into, captures mean-reversion
       Neutral when mid within ±2 of fv: no shift applied.
  All other parameters identical to baseline.
─────────────────────────────────────────────────────────────────────────────
"""

from datamodel import OrderDepth, TradingState, Order
from typing import Dict, List, Optional
import json


class Logger:
    def __init__(self): self.logs = ""
    def print(self, *args, sep=" ", end="\n"):
        self.logs += sep.join(map(str, args)) + end
    def flush(self, state, orders, conversions, trader_data):
        print(json.dumps({
            "state": state, "orders": orders, "conversions": conversions,
            "traderData": trader_data, "logs": self.logs,
        }, default=lambda o: o.__dict__, separators=(",", ":")))
        self.logs = ""

logger = Logger()


LIMITS    = {"ASH_COATED_OSMIUM": 80, "INTARIAN_PEPPER_ROOT": 80}
OSMIUM_FV = 10_000

KF_QL, KF_QS, KF_R = 0.5, 0.008, 10.0
OSMIUM_WARMUP = 0

AS_GAMMA = 0.000473
AS_SIGMA = 0.982
AS_T     = 27.4
AS_HS    = 7

TAKE_EDGE = 1             # unchanged from baseline

# CHANGE: deviation threshold for applying directional quote shift
DEV_SKEW_THRESHOLD = 2   # ticks from FV before shift activates

PEPPER_OPEN_BUY_UNTIL = 10_000


def kf_init(price: float) -> dict:
    return {"l": price, "s": 0.0, "p00": 100.0, "p01": 0.0, "p11": 100.0}

def kf_step(st: dict, obs: float, ql: float, qs: float, r: float) -> dict:
    l, s          = st["l"],   st["s"]
    p00, p01, p11 = st["p00"], st["p01"], st["p11"]
    lp   = l + s;  sp = s
    pp00 = p00 + 2*p01 + p11 + ql
    pp01 = p01 + p11
    pp11 = p11 + qs
    S  = pp00 + r
    K0 = pp00 / S;  K1 = pp01 / S
    inn = obs - lp
    return {
        "l":   lp  + K0*inn,  "s":   sp  + K1*inn,
        "p00": (1 - K0)*pp00, "p01": (1 - K0)*pp01,
        "p11": pp11 - K1*pp01,
    }


def get_best(ob: OrderDepth):
    best_bid = max(ob.buy_orders.keys())  if ob.buy_orders  else None
    best_ask = min(ob.sell_orders.keys()) if ob.sell_orders else None
    return best_bid, best_ask

def get_vwmid(ob: OrderDepth) -> Optional[float]:
    bids = sorted(ob.buy_orders.items(), reverse=True)[:3]
    asks = sorted(ob.sell_orders.items())[:3]
    if not bids or not asks:
        return None
    b_num = sum(p * q      for p, q in bids)
    b_den = sum(    q      for _, q in bids)
    a_num = sum(p * abs(q) for p, q in asks)
    a_den = sum(    abs(q) for _, q in asks)
    if b_den == 0 or a_den == 0:
        return None
    return (b_num / b_den + a_num / a_den) / 2.0

def get_mid(ob: OrderDepth) -> Optional[float]:
    mid = get_vwmid(ob)
    if mid is not None:
        return mid
    best_bid, best_ask = get_best(ob)
    if best_bid is not None and best_ask is not None:
        return (best_bid + best_ask) / 2.0
    if best_bid is not None:  return float(best_bid)
    if best_ask is not None:  return float(best_ask)
    return None

def get_wall_mid(ob: OrderDepth) -> Optional[float]:
    if not ob.buy_orders or not ob.sell_orders:
        return None
    wall_bid = max(ob.buy_orders.items(),  key=lambda x: x[1])[0]
    wall_ask = max(ob.sell_orders.items(), key=lambda x: abs(x[1]))[0]
    return (wall_bid + wall_ask) / 2.0


def trade_osmium(product: str, ob: OrderDepth, pos: int,
                 limit: int, kf: dict) -> List[Order]:
    best_bid, best_ask = get_best(ob)
    if best_bid is None and best_ask is None:
        return []

    orders:  List[Order] = []
    buy_cap  = limit - pos
    sell_cap = limit + pos

    # Inventory guard — unchanged from baseline
    pos_ratio = abs(pos) / limit
    if pos_ratio > 0.75:
        scale = max(0.0, 1.0 - pos_ratio)
        if pos > 0:
            buy_cap = int(buy_cap * scale)
        else:
            sell_cap = int(sell_cap * scale)

    fv  = 0.7 * kf["l"] + 0.3 * OSMIUM_FV
    mid = get_mid(ob)

    # Taking — unchanged from baseline
    if best_ask is not None and best_ask < fv - TAKE_EDGE and buy_cap > 0:
        qty = min(abs(ob.sell_orders[best_ask]), buy_cap)
        orders.append(Order(product, best_ask, +qty))
        buy_cap -= qty
        logger.print(f"OSM TAKE BUY  ask={best_ask}  fv={fv:.1f}  qty={qty}")

    if best_bid is not None and best_bid > fv + TAKE_EDGE and sell_cap > 0:
        qty = min(ob.buy_orders[best_bid], sell_cap)
        orders.append(Order(product, best_bid, -qty))
        sell_cap -= qty
        logger.print(f"OSM TAKE SELL bid={best_bid}  fv={fv:.1f}  qty={qty}")

    skew = pos * AS_GAMMA * AS_SIGMA**2 * AS_T
    res  = round(fv - skew)

    # CHANGE: price-deviation skew shifts quotes toward expected mean-reversion
    # When price is above FV, we expect it to fall → shift quotes down to sell more
    # When price is below FV, we expect it to rise → shift quotes up to buy more
    dev_shift = 0
    if mid is not None:
        dev = mid - fv
        if dev > DEV_SKEW_THRESHOLD:
            dev_shift = -1   # price above FV: shift quotes down
        elif dev < -DEV_SKEW_THRESHOLD:
            dev_shift = +1   # price below FV: shift quotes up

    if best_bid is not None and best_ask is not None:
        bid_px = min(best_bid + 1, res - max(1, AS_HS // 2)) + dev_shift
        ask_px = max(best_ask - 1, res + max(1, AS_HS // 2)) + dev_shift
        # Safety: never cross the spread
        bid_px = min(bid_px, best_ask - 1)
        ask_px = max(ask_px, best_bid + 1)
    else:
        bid_px = res - AS_HS + dev_shift
        ask_px = res + AS_HS + dev_shift

    if buy_cap > 0:
        inner = max(1, int(buy_cap * 0.65))
        outer = buy_cap - inner
        orders.append(Order(product, bid_px,     +inner))
        if outer > 0:
            orders.append(Order(product, bid_px - 1, +outer))
    if sell_cap > 0:
        inner = max(1, int(sell_cap * 0.65))
        outer = sell_cap - inner
        orders.append(Order(product, ask_px,     -inner))
        if outer > 0:
            orders.append(Order(product, ask_px + 1, -outer))

    logger.print(
        f"OSM pos={pos:+d}  fv={fv:.1f}  res={res}  dev_shift={dev_shift:+d}  "
        f"q={bid_px}/{ask_px}  cap={buy_cap}/{sell_cap}"
    )
    return orders


def trade_pepper(product: str, ob: OrderDepth, pos: int,
                 limit: int, timestamp: int) -> List[Order]:
    best_bid, best_ask = get_best(ob)
    buy_cap = limit - pos
    if buy_cap <= 0:
        return []
    if timestamp <= PEPPER_OPEN_BUY_UNTIL:
        if best_ask is not None:
            logger.print(f"PEP OPEN_BUY  t={timestamp}  qty={buy_cap}  ask={best_ask}")
            return [Order(product, best_ask, +buy_cap)]
        return []
    orders: List[Order] = []
    for ask_price in sorted(ob.sell_orders.keys()):
        if buy_cap <= 0:
            break
        qty = min(abs(ob.sell_orders[ask_price]), buy_cap)
        orders.append(Order(product, ask_price, +qty))
        buy_cap -= qty
    logger.print(f"PEP HOLD  pos={pos}  buy_cap_remaining={buy_cap}")
    return orders


def log_named_bots(state: TradingState) -> None:
    for prod, trades in state.market_trades.items():
        for t in trades:
            buyer  = getattr(t, "buyer",  "") or ""
            seller = getattr(t, "seller", "") or ""
            for agent, side in [(buyer, "BUY"), (seller, "SELL")]:
                if agent and agent not in ("", "SUBMISSION"):
                    logger.print(f"NAMEDBOT {agent:<12} {side}  {prod}  p={t.price}  q={t.quantity}  ts={state.timestamp}")


class Trader:
    def run(self, state: TradingState):
        raw     = json.loads(state.traderData) if state.traderData else {}
        tick    = raw.get("tick", 0)
        prev_ts = raw.get("prev_ts", -1)
        new_day = (tick > 0) and (state.timestamp < prev_ts)

        orders_out: Dict[str, List[Order]] = {}
        new_state: dict = {"tick": tick + 1, "prev_ts": state.timestamp}

        prd = "INTARIAN_PEPPER_ROOT"
        ob  = state.order_depths.get(prd)
        pos = state.position.get(prd, 0)
        orders_out[prd] = trade_pepper(prd, ob, pos, LIMITS[prd], state.timestamp) if ob else []

        prd = "ASH_COATED_OSMIUM"
        ob  = state.order_depths.get(prd)
        pos = state.position.get(prd, 0)
        mid = get_mid(ob) if ob else None

        ash_kf    = None if new_day else raw.get("ash_kf")
        ash_ticks = 0    if new_day else raw.get("ash_ticks", 0)

        if mid is not None:
            ash_kf     = kf_init(mid) if ash_kf is None else ash_kf
            ash_kf     = kf_step(ash_kf, mid, KF_QL, KF_QS, KF_R)
            ash_ticks += 1

        if ob and ash_kf and ash_ticks >= OSMIUM_WARMUP:
            wm = get_wall_mid(ob)
            if wm is not None:
                logger.print(f"OSM wall_mid={wm:.1f}  kf={ash_kf['l']:.1f}  Δ={wm-ash_kf['l']:+.1f}")
            orders_out[prd] = trade_osmium(prd, ob, pos, LIMITS[prd], ash_kf)
        else:
            orders_out[prd] = []

        if ash_kf:
            new_state["ash_kf"] = ash_kf
        new_state["ash_ticks"] = ash_ticks

        log_named_bots(state)
        trader_data = json.dumps(new_state)
        conversions = 0
        logger.flush(state, orders_out, conversions, trader_data)
        return orders_out, conversions, trader_data