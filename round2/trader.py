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
from typing import Dict, List, Optional, Tuple
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

KF_QL, KF_QS, KF_R = 0.4, 0.008, 10.0
OSMIUM_WARMUP = 0
OSM_DRIFT_ANCHOR = 0.65
OSM_DRIFT_SLOPE = 0.06

AS_GAMMA = 0.000473
AS_SIGMA = 0.982
AS_T     = 27.4
AS_HS    = 7

TAKE_EDGE = 1             # unchanged from baseline

# CHANGE: deviation threshold for applying directional quote shift
DEV_SKEW_THRESHOLD = 2   # ticks from FV before shift activates

PEPPER_OPEN_BUY_UNTIL = 10_000
PEP_ASK_SZ_ALPHA      = 0.20

PEP_KF_QL, PEP_KF_QS, PEP_KF_R = 0.3, 0.003, 6.0
PEP_SLOPE_TAKE_THRESH = 0.25   # slope ticks/tick above which we force-take


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


def pepper_state_init(ob: OrderDepth) -> dict:
    _, best_ask = get_best(ob)
    best_ask_sz = abs(ob.sell_orders[best_ask]) if best_ask is not None else 1.0
    mid = get_mid(ob)
    return {
        "best_ask_sz_ema": float(max(best_ask_sz, 1.0)),
        "pep_kf": kf_init(mid) if mid is not None else None,
    }


def pepper_state_update(st: dict, ob: OrderDepth) -> dict:
    _, best_ask = get_best(ob)
    prev_ema = st.get("best_ask_sz_ema", 1.0)
    best_ask_sz = abs(ob.sell_orders[best_ask]) if best_ask is not None else prev_ema
    best_ask_sz_ema = PEP_ASK_SZ_ALPHA * best_ask_sz + (1.0 - PEP_ASK_SZ_ALPHA) * prev_ema
    mid = get_mid(ob)
    pep_kf = st.get("pep_kf")
    if mid is not None:
        pep_kf = kf_init(mid) if pep_kf is None else kf_step(pep_kf, mid, PEP_KF_QL, PEP_KF_QS, PEP_KF_R)
    return {
        "best_ask_sz_ema": max(best_ask_sz_ema, 1.0),
        "pep_kf": pep_kf,
    }


def pepper_entry_features(ob: OrderDepth, st: dict) -> dict:
    best_bid, best_ask = get_best(ob)
    spread = (best_ask - best_bid) if best_bid is not None and best_ask is not None else 2.0
    asks = sorted(ob.sell_orders.items())[:3]
    best_ask_sz = abs(ob.sell_orders[best_ask]) if best_ask is not None else 0.0
    size_ema = max(st.get("best_ask_sz_ema", 1.0), 1.0)
    ask_gap_2 = (asks[1][0] - asks[0][0]) if len(asks) >= 2 else spread
    ask_stack_ratio = sum(abs(q) for _, q in asks) / size_ema
    return {
        "spread": float(spread),
        "best_ask_sz_ratio": best_ask_sz / size_ema,
        "ask_gap_2": float(ask_gap_2),
        "ask_stack_ratio": ask_stack_ratio,
    }


def pepper_add_route(orders: List[Order], product: str, ob: OrderDepth, qty: int, route: str) -> int:
    if qty <= 0:
        return 0
    bb, ba = get_best(ob)
    if route == "take":
        if ba is not None:
            orders.append(Order(product, ba, +qty))
            return qty
        return 0
    if route == "improve":
        if bb is not None and ba is not None and bb + 1 < ba:
            orders.append(Order(product, bb + 1, +qty))
            return qty
        if ba is not None:
            orders.append(Order(product, ba, +qty))
            return qty
        return 0
    if route == "join":
        if bb is not None:
            orders.append(Order(product, bb, +qty))
            return qty
        if ba is not None:
            orders.append(Order(product, ba, +qty))
            return qty
        return 0
    if route == "sit":
        if bb is not None:
            orders.append(Order(product, bb - 1, +qty))
            return qty
        if ba is not None:
            orders.append(Order(product, ba, +qty))
            return qty
        return 0
    return 0


def pepper_model_plan(f: dict, slope: float = 0.0) -> List[Tuple[str, float]]:
    spread = f["spread"]
    ask_stack = f["ask_stack_ratio"]

    # Strong uptrend: capture position immediately, accept some slippage
    if slope > PEP_SLOPE_TAKE_THRESH:
        if spread >= 3.0:
            return [("improve", 0.35), ("take", 0.65)]
        return [("take", 1.0)]

    # Wide spread: passive improve captures edge, take fills remainder
    if spread >= 3.0:
        return [("improve", 0.55), ("take", 0.45)]

    # Thin supply: improve (bid+1) — join is too passive for a trending asset
    if ask_stack < 1.40:
        return [("improve", 1.0)]

    # Moderate supply: lean toward taking but mix in passive
    if ask_stack < 2.00:
        return [("improve", 0.35), ("take", 0.65)]

    # Deep supply: take
    return [("take", 1.0)]


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

    mid = get_mid(ob)
    trend_component = (kf["l"] - OSMIUM_FV) + OSM_DRIFT_SLOPE * kf["s"]
    fv = OSMIUM_FV + OSM_DRIFT_ANCHOR * trend_component
    dev = (mid - fv) if mid is not None else 0.0

    # Scale taking by mispricing so tiny edges do not consume full capacity.
    if best_ask is not None and best_ask < fv - TAKE_EDGE and buy_cap > 0:
        edge = fv - best_ask
        take_scale = 0.10 if edge < 2.4 else (0.85 if edge < 3.4 else 1.0)
        qty = min(abs(ob.sell_orders[best_ask]), max(1, int(buy_cap * take_scale)))
        orders.append(Order(product, best_ask, +qty))
        buy_cap -= qty
        logger.print(f"OSM TAKE BUY  ask={best_ask}  fv={fv:.1f}  edge={edge:.2f}  qty={qty}")

    if best_bid is not None and best_bid > fv + TAKE_EDGE and sell_cap > 0:
        edge = best_bid - fv
        take_scale = 0.10 if edge < 2.4 else (0.85 if edge < 3.4 else 1.0)
        qty = min(ob.buy_orders[best_bid], max(1, int(sell_cap * take_scale)))
        orders.append(Order(product, best_bid, -qty))
        sell_cap -= qty
        logger.print(f"OSM TAKE SELL bid={best_bid}  fv={fv:.1f}  edge={edge:.2f}  qty={qty}")

    skew = pos * AS_GAMMA * AS_SIGMA**2 * AS_T
    res  = round(fv - skew)

    if best_bid is not None and best_ask is not None:
        base_bid_px = min(best_bid + 1, res - max(1, AS_HS // 2))
        base_ask_px = max(best_ask - 1, res + max(1, AS_HS // 2))
    else:
        base_bid_px = res - AS_HS
        base_ask_px = res + AS_HS

    bid_shift = 0
    ask_shift = 0
    if mid is not None:
        if dev > DEV_SKEW_THRESHOLD:
            bid_shift = -1
            ask_shift = -1
        elif dev < -DEV_SKEW_THRESHOLD:
            bid_shift = +1
            ask_shift = +1

    bid_px = base_bid_px + bid_shift
    ask_px = base_ask_px + ask_shift
    if best_bid is not None and best_ask is not None:
        bid_px = min(bid_px, best_ask - 1)
        ask_px = max(ask_px, best_bid + 1)

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
        f"OSM pos={pos:+d}  fv={fv:.1f}  drift={trend_component:+.2f}  "
        f"dev={dev:+.1f}  res={res}  q={bid_px}/{ask_px}  cap={buy_cap}/{sell_cap}"
    )
    return orders


def trade_pepper(product: str, ob: OrderDepth, pos: int,
                 limit: int, timestamp: int, pep_state: dict) -> List[Order]:
    buy_cap = limit - pos
    if buy_cap <= 0:
        return []
    if timestamp <= PEPPER_OPEN_BUY_UNTIL:
        f = pepper_entry_features(ob, pep_state)
        plan = pepper_model_plan(f)
        remaining = buy_cap
        orders: List[Order] = []
        for idx, (route, frac) in enumerate(plan):
            if remaining <= 0:
                break
            if idx == len(plan) - 1:
                qty = remaining
            else:
                qty = max(0, min(remaining, int(round(buy_cap * frac))))
            if qty <= 0:
                continue
            used = pepper_add_route(orders, product, ob, qty, route)
            remaining -= used

        logger.print(
            f"PEP depth_ladder  t={timestamp}  cap={buy_cap}  spread={f['spread']:.1f}  "
            f"gap2={f['ask_gap_2']:.1f}  thin={f['best_ask_sz_ratio']:.2f}  stack={f['ask_stack_ratio']:.2f}  "
            f"orders={[(o.price, o.quantity) for o in orders]}")
        return orders
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
    def bid(self) -> int:
        """
        MAF bid — Round 2.
        Estimated incremental value ~2,700 XIRECs for 1M run.
        1756 beats round-number clusters, costs 6 extra vs 1750.
        """
        return 1756

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
        pep_state = None if new_day else raw.get("pep_state")
        if ob:
            pep_state = (
                pepper_state_init(ob)
                if pep_state is None
                else pepper_state_update(pep_state, ob)
            )
            orders_out[prd] = trade_pepper(prd, ob, pos, LIMITS[prd], state.timestamp, pep_state)
            new_state["pep_state"] = pep_state
        else:
            orders_out[prd] = []

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