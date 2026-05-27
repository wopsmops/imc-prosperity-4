"""
trader.py  ·  Round 3  ·  GoldmanSnacks
────────────────────────────────────────────────────────────────────────────
Two independent pipelines:

  PIPELINE A — Linear market making  (Hydrogel + Velvetfruit Extract)
    Model:     Kalman-smoothed fair value, no fixed anchor
    Execution: Dual-sided passive quoting + conservative clip-taking
    Features:  Inventory skew (Avellaneda-Stoikov), slope gate,
               short-reversal repair, asymmetric clip sizing

  PIPELINE B — Options alpha  (10 × VEV call options)
    Pricing:   Black-Scholes (r=0) with live annualised vol estimate
    Signal:    VEV_5300 rich-fade — short when market > BS fair + threshold
    Delta:     Net portfolio delta tracked; hedged via VELVETFRUIT_EXTRACT
    Execution: TradeIdea abstraction → priority aggregator → price-aware fill

Black-Scholes model
    C = S·N(d₁) − K·N(d₂)     (r = 0, European call)
    d₁ = [ln(S/K) + ½σ²T] / (σ√T)
    σ  = annualised log-return vol estimated from rolling VEX price history
    T  = tte_days / 252
────────────────────────────────────────────────────────────────────────────
"""

import json
import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from datamodel import Order, OrderDepth, TradingState


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 1A — LINEAR MM CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════

LINEAR_MM_CFG: Dict[str, dict] = {
    "HYDROGEL_PACK": {
        "warmup":             10,
        "take_edge":           8,
        "quote_edge":          7,
        "quote_inner_frac":   0.60,
        "buy_clip":            3,    # smaller — buy-side edge decays quickly
        "sell_clip":           5,
        "slope_sell_gate":    -0.15, # suppress bids when KF slope < this
        "reversal_pos_thresh":-20,   # trigger short-reversal repair at this pos
        "reversal_slope_up":   0.25, # KF slope above this = bullish rebound
        "reversal_buy_clip":   8,
        "gamma":             8.7e-7,
        "sigma":             219.0,
        "T":                   1.0,
        "max_pos":            120,
    },
    "VELVETFRUIT_EXTRACT": {
        "warmup":             10,
        "take_edge":           4,
        "quote_edge":          2,
        "quote_inner_frac":   0.60,
        "buy_clip":            4,
        "sell_clip":           4,
        "gamma":             6.6e-6,
        "sigma":             113.0,
        "T":                   1.0,
        "max_pos":             55,
    },
}

# ═══════════════════════════════════════════════════════════════════════════
# SECTION 1B — OPTIONS CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════

STRIKES: List[int] = [4000, 4500, 5000, 5100, 5200, 5300, 5400, 5500, 6000, 6500]
VEV_PRODUCTS        = [f"VEV_{k}" for k in STRIKES]
VOUCHER_STRIKES     = {f"VEV_{k}": k for k in STRIKES}

POSITION_LIMITS: Dict[str, int] = {
    "HYDROGEL_PACK":       200,
    "VELVETFRUIT_EXTRACT": 200,
    **{f"VEV_{k}": 300 for k in STRIKES},
}

# Black-Scholes / vol parameters
TTE_START:            int   = 5       # competition days remaining at Round 3 start
TRADING_DAYS_PER_YEAR: int  = 252
TICKS_PER_DAY:        int   = 10_000
VOL_WINDOW:           int   = 200     # rolling window for vol estimation
VOL_PRIOR:            float = 0.82    # fallback annualised vol before estimator warms up

# VEV_5300 rich-fade parameters
VEV5300_RICH_GAP:  float = 6.0   # enter short when market > BS fair + this
VEV5300_EXIT_GAP:  float = 1.5   # exit when market − BS fair < this
VEV5300_MAX_SHORT: int   = 80
VEV5300_CLIP:      int   = 15

# Net delta limit across all VEV positions (hedged via VEX)
NET_DELTA_LIMIT: int = 400

# Timing gates (timestamps within one competition day = 100,000 units)
DAY_END_TS:           int = 100_000
NO_NEW_ENTRIES_AFTER: int = int(0.90 * DAY_END_TS)   # 90,000
FORCE_FLATTEN_AFTER:  int = int(0.97 * DAY_END_TS)   # 97,000

SIGNAL_COOLDOWN_TS: int = 5_000   # min timestamp gap between repeat signals

ALPHA_PRIORITY: Dict[str, float] = {
    "chain_arb":   3.0,
    "vev5300_fade": 2.0,
    "linear_mm":   1.0,
}

MIN_EDGE_TO_TAKE: Dict[str, float] = {
    **{f"VEV_{k}": 1.5 for k in STRIKES},
    "VELVETFRUIT_EXTRACT": 0.5,
}


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 2 — TRADE IDEA
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class TradeIdea:
    """
    Interface between alpha modules and the order aggregator.

    target_qty   : desired absolute position (executor computes gap vs current)
    edge         : expected profit per unit at current prices
    confidence   : 0–1 scalar; scales priority in conflict resolution
    delta        : option delta (N(d1) for calls; 1.0 for delta-1 products)
    alpha_type   : key into ALPHA_PRIORITY
    urgent_exit  : bypass edge filter, lift/hit unconditionally (EOD flatten only)
    """
    symbol:      str
    target_qty:  int
    fair_value:  float
    edge:        float
    confidence:  float
    delta:       float
    alpha_type:  str
    reason:      str  = ""
    urgent_exit: bool = False

    @property
    def score(self) -> float:
        return self.edge * self.confidence * ALPHA_PRIORITY.get(self.alpha_type, 1.0)


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 3 — ORDER BOOK HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def _best(ob: OrderDepth) -> Tuple[Optional[int], Optional[int]]:
    bb = max(ob.buy_orders)  if ob.buy_orders  else None
    ba = min(ob.sell_orders) if ob.sell_orders else None
    return bb, ba


def _mid(ob: OrderDepth) -> Optional[float]:
    bb, ba = _best(ob)
    if bb is not None and ba is not None:
        return (bb + ba) / 2.0
    if bb is not None: return float(bb)
    if ba is not None: return float(ba)
    return None


def _vwmid(ob: OrderDepth, levels: int = 3) -> Optional[float]:
    bids = sorted(ob.buy_orders.items(),  reverse=True)[:levels]
    asks = sorted(ob.sell_orders.items())[:levels]
    if not bids or not asks:
        return None
    b_val = sum(p * q      for p, q in bids);  b_vol = sum(q      for _, q in bids)
    a_val = sum(p * abs(q) for p, q in asks);  a_vol = sum(abs(q) for _, q in asks)
    if b_vol == 0 or a_vol == 0:
        return None
    return (b_val / b_vol + a_val / a_vol) / 2.0


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 4 — BLACK-SCHOLES PRICING  [PRIMARY OPTIONS MODEL]
# ═══════════════════════════════════════════════════════════════════════════

def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _npdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_call(S: float, K: float, T: float, sigma: float) -> float:
    """
    Black-Scholes European call price with r=0.
    C = S·N(d₁) − K·N(d₂)
    """
    if T <= 0.0 or sigma <= 0.0 or S <= 0.0:
        return max(S - K, 0.0)
    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * sigma ** 2 * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    return S * _ncdf(d1) - K * _ncdf(d2)


def bs_delta(S: float, K: float, T: float, sigma: float) -> float:
    """Black-Scholes call delta N(d₁). Used for portfolio delta tracking."""
    if T <= 0.0 or sigma <= 0.0 or S <= 0.0:
        return 1.0 if S > K else 0.0
    d1 = (math.log(S / K) + 0.5 * sigma ** 2 * T) / (sigma * math.sqrt(T))
    return _ncdf(d1)


def implied_vol(
    market_price: float, S: float, K: float, T: float,
    lo: float = 1e-6, hi: float = 20.0, tol: float = 1e-5,
) -> Optional[float]:
    """Implied volatility via bisection. Returns None if price violates no-arb bounds."""
    if T <= 0.0 or market_price < max(S - K, 0.0) - tol:
        return None
    for _ in range(80):
        mid_vol = (lo + hi) * 0.5
        p = bs_call(S, K, T, mid_vol)
        if abs(p - market_price) < tol:
            return mid_vol
        lo, hi = (mid_vol, hi) if p < market_price else (lo, mid_vol)
    return (lo + hi) * 0.5


def tte_to_years(tte_days: float) -> float:
    return tte_days / TRADING_DAYS_PER_YEAR


def bs_fair_value(S: float, K: float, tte_days: float, sigma_annual: float) -> float:
    """BS call fair value. Primary entry point for options pricing in this bot."""
    return bs_call(S, K, tte_to_years(tte_days), sigma_annual)


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 5 — ROLLING VOLATILITY ESTIMATOR
# ═══════════════════════════════════════════════════════════════════════════

class VolEstimator:
    """
    Estimates annualised log-return volatility from rolling VEX price history.

    sigma_annual = sigma_tick × √(TICKS_PER_DAY × TRADING_DAYS_PER_YEAR)

    Falls back to VOL_PRIOR until the window has enough observations.
    """

    def __init__(self) -> None:
        self._log_returns: List[float] = []
        self._last_price:  Optional[float] = None
        self.sigma_annual: float = VOL_PRIOR

    def update(self, price: float) -> None:
        if self._last_price is not None and self._last_price > 0 and price > 0:
            self._log_returns.append(math.log(price / self._last_price))
            if len(self._log_returns) > VOL_WINDOW:
                self._log_returns.pop(0)
        self._last_price = price
        n = len(self._log_returns)
        if n >= 10:
            mean = sum(self._log_returns) / n
            variance = sum((r - mean) ** 2 for r in self._log_returns) / max(n - 1, 1)
            sigma_tick = math.sqrt(variance)
            ticks_per_year = TICKS_PER_DAY * TRADING_DAYS_PER_YEAR
            self.sigma_annual = sigma_tick * math.sqrt(ticks_per_year)

    def to_dict(self) -> dict:
        return {"lr": self._log_returns, "lp": self._last_price, "sa": self.sigma_annual}

    @classmethod
    def from_dict(cls, d: dict) -> "VolEstimator":
        obj = cls()
        obj._log_returns  = d.get("lr", [])
        obj._last_price   = d.get("lp")
        obj.sigma_annual  = d.get("sa", VOL_PRIOR)
        return obj


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 6 — KALMAN FILTER  (linear MM fair value)
# ═══════════════════════════════════════════════════════════════════════════

_KF_QL = 0.4;  _KF_QS = 0.008;  _KF_R = 10.0


def kf_init(price: float) -> dict:
    return {"l": price, "s": 0.0, "p00": 100.0, "p01": 0.0, "p11": 100.0}


def kf_step(st: dict, obs: float) -> dict:
    l, s          = st["l"], st["s"]
    p00, p01, p11 = st["p00"], st["p01"], st["p11"]
    lp = l + s;  sp = s
    pp00 = p00 + 2*p01 + p11 + _KF_QL
    pp01 = p01 + p11
    pp11 = p11 + _KF_QS
    Sv   = pp00 + _KF_R
    K0   = pp00 / Sv;  K1 = pp01 / Sv
    inn  = obs - lp
    return {
        "l":   lp  + K0*inn, "s":   sp  + K1*inn,
        "p00": (1-K0)*pp00,  "p01": (1-K0)*pp01,
        "p11": pp11 - K1*pp01,
    }


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 7 — TTE HELPER
# ═══════════════════════════════════════════════════════════════════════════

def current_tte(timestamp: int) -> float:
    """
    Remaining time to expiry in competition days.
    TTE decreases by 1 per 100,000 timestamps.
    Starts at TTE_START = 5 at timestamp 0.
    """
    elapsed_days = timestamp / DAY_END_TS
    return max(TTE_START - elapsed_days, 0.0)


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 8 — VEV_5300 RICH-FADE MODULE
# ═══════════════════════════════════════════════════════════════════════════

def run_vev5300_fade(
    ob: OrderDepth,
    pos: int,
    S: float,
    tte_days: float,
    sigma: float,
    cooldown_until: int,
    timestamp: int,
) -> Optional[TradeIdea]:
    """
    Short VEV_5300 when market price exceeds Black-Scholes fair value by
    VEV5300_RICH_GAP ticks. Exit when gap normalises below VEV5300_EXIT_GAP.

    Entry signal:  mid − BS_fair ≥ VEV5300_RICH_GAP
    Exit  signal:  mid − BS_fair <  VEV5300_EXIT_GAP  (unwind short)
    """
    if timestamp < cooldown_until:
        return None
    if timestamp >= NO_NEW_ENTRIES_AFTER and pos >= 0:
        return None

    symbol   = "VEV_5300"
    K        = 5300
    fair     = bs_fair_value(S, K, tte_days, sigma)
    delta    = bs_delta(S, K, tte_to_years(tte_days), sigma)
    bb, ba   = _best(ob)
    mid      = _mid(ob)

    if mid is None:
        return None

    gap = mid - fair

    # Forced unwind at EOD
    if timestamp >= FORCE_FLATTEN_AFTER and pos < 0:
        return TradeIdea(
            symbol=symbol, target_qty=0, fair_value=fair,
            edge=abs(gap), confidence=1.0, delta=delta,
            alpha_type="vev5300_fade", reason="eod_flatten", urgent_exit=True,
        )

    # Exit: gap has normalised
    if pos < 0 and gap < VEV5300_EXIT_GAP:
        return TradeIdea(
            symbol=symbol, target_qty=0, fair_value=fair,
            edge=fair - (ba or fair), confidence=0.8, delta=delta,
            alpha_type="vev5300_fade", reason=f"exit gap={gap:.2f}",
        )

    # Entry: option is rich vs BS fair
    if gap >= VEV5300_RICH_GAP and pos > -VEV5300_MAX_SHORT:
        confidence = min(1.0, (gap - VEV5300_RICH_GAP) / VEV5300_RICH_GAP + 0.5)
        new_target = max(pos - VEV5300_CLIP, -VEV5300_MAX_SHORT)
        edge_per_unit = gap - VEV5300_EXIT_GAP
        return TradeIdea(
            symbol=symbol, target_qty=new_target, fair_value=fair,
            edge=edge_per_unit, confidence=confidence, delta=delta,
            alpha_type="vev5300_fade", reason=f"rich gap={gap:.2f} sigma={sigma:.3f}",
        )

    return None


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 9 — CHAIN ARBITRAGE MODULE
# ═══════════════════════════════════════════════════════════════════════════

def run_chain_arb(
    order_depths: Dict[str, OrderDepth],
    positions: Dict[str, int],
    S: float,
    tte_days: float,
    sigma: float,
) -> List[TradeIdea]:
    """
    Check for static no-arbitrage violations across the VEV option chain:
    - Monotonicity: lower strike call must be worth at least as much as higher
    - Convexity (butterfly): mid-strike must be ≤ weighted avg of neighbours

    Trades the cheap leg of any detected violation.
    """
    ideas: List[TradeIdea] = []

    mids: Dict[str, Optional[float]] = {}
    for sym in VEV_PRODUCTS:
        ob = order_depths.get(sym)
        mids[sym] = _mid(ob) if ob else None

    for i, k_lo in enumerate(STRIKES[:-1]):
        k_hi  = STRIKES[i + 1]
        sym_lo = f"VEV_{k_lo}";  sym_hi = f"VEV_{k_hi}"
        m_lo  = mids.get(sym_lo); m_hi  = mids.get(sym_hi)
        if m_lo is None or m_hi is None:
            continue

        # Monotonicity: C(K_lo) ≥ C(K_hi)  (lower strike → higher value)
        arb_gap = m_hi - m_lo
        if arb_gap > 2.0:
            pos_lo = positions.get(sym_lo, 0)
            fair = bs_fair_value(S, k_lo, tte_days, sigma)
            delta = bs_delta(S, k_lo, tte_to_years(tte_days), sigma)
            if pos_lo < POSITION_LIMITS.get(sym_lo, 300):
                ideas.append(TradeIdea(
                    symbol=sym_lo, target_qty=min(pos_lo + 5, POSITION_LIMITS.get(sym_lo, 300)),
                    fair_value=fair, edge=arb_gap, confidence=0.9,
                    delta=delta, alpha_type="chain_arb",
                    reason=f"mono_arb {sym_lo}<{sym_hi} gap={arb_gap:.1f}",
                ))

    return ideas


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 10 — DELTA CORRECTION (hedge via VEX)
# ═══════════════════════════════════════════════════════════════════════════

def run_delta_correction(
    vex_ob: OrderDepth,
    vex_pos: int,
    positions: Dict[str, int],
    order_depths: Dict[str, OrderDepth],
    S: float,
    tte_days: float,
    sigma: float,
) -> Optional[TradeIdea]:
    """
    Compute net portfolio delta from all VEV positions and hedge with VEX.

    net_delta = Σ_k  pos_k × delta_k(S, K, T, σ)
    hedge_target = −net_delta (using VEX as delta-1 instrument)
    """
    net_delta = 0.0
    T = tte_to_years(tte_days)
    for sym, K in VOUCHER_STRIKES.items():
        p = positions.get(sym, 0)
        if p == 0:
            continue
        d = bs_delta(S, K, T, sigma)
        net_delta += p * d

    hedge_target = int(-net_delta)
    gap = hedge_target - vex_pos

    if abs(gap) < 5 or abs(net_delta) <= NET_DELTA_LIMIT:
        return None

    bb, ba = _best(vex_ob)
    mid = _mid(vex_ob)
    if mid is None:
        return None

    cfg = LINEAR_MM_CFG["VELVETFRUIT_EXTRACT"]
    return TradeIdea(
        symbol="VELVETFRUIT_EXTRACT",
        target_qty=max(-cfg["max_pos"], min(cfg["max_pos"], hedge_target)),
        fair_value=mid,
        edge=0.5,
        confidence=1.0,
        delta=1.0,
        alpha_type="chain_arb",   # elevated priority so hedge fires before MM
        reason=f"delta_hedge net_delta={net_delta:.1f}",
    )


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 11 — PRICE-AWARE EXECUTOR
# ═══════════════════════════════════════════════════════════════════════════

def execute_idea(
    idea: TradeIdea,
    ob: OrderDepth,
    pos: int,
    limit: int,
) -> List[Order]:
    """
    Convert a TradeIdea into Order objects.

    gap > 0 → need to buy;  gap < 0 → need to sell.
    Crosses the spread for urgent exits; uses min-edge filter otherwise.
    """
    orders: List[Order] = []
    gap   = idea.target_qty - pos
    bb, ba = _best(ob)
    min_edge = MIN_EDGE_TO_TAKE.get(idea.symbol, 1.0)

    if gap > 0:  # buy
        qty = min(gap, limit - pos)
        if qty <= 0:
            return []
        if idea.urgent_exit and ba is not None:
            orders.append(Order(idea.symbol, ba, +qty))
        elif ba is not None and (ba < idea.fair_value + min_edge or idea.urgent_exit):
            orders.append(Order(idea.symbol, ba, +qty))

    elif gap < 0:  # sell
        qty = min(-gap, limit + pos)
        if qty <= 0:
            return []
        if idea.urgent_exit and bb is not None:
            orders.append(Order(idea.symbol, bb, -qty))
        elif bb is not None and (bb > idea.fair_value - min_edge or idea.urgent_exit):
            orders.append(Order(idea.symbol, bb, -qty))

    return orders


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 12 — LINEAR MM MODULE  (Pipeline A)
# ═══════════════════════════════════════════════════════════════════════════

def run_linear_mm(
    product: str,
    ob: OrderDepth,
    pos: int,
    kf: dict,
    timestamp: int,
) -> List[Order]:
    """
    Avellaneda-Stoikov market maker using Kalman-smoothed fair value.
    Asymmetric clips, slope gate, short-reversal repair.
    """
    cfg     = LINEAR_MM_CFG[product]
    limit   = POSITION_LIMITS[product]
    bb, ba  = _best(ob)
    if bb is None and ba is None:
        return []

    orders:   List[Order] = []
    buy_cap   = min(limit - pos, cfg["max_pos"] - pos)
    sell_cap  = min(limit + pos, cfg["max_pos"] + pos)
    fv        = kf["l"]
    slope     = kf["s"]

    # Slope gate — suppress buys when trending down
    if slope < cfg.get("slope_sell_gate", -999):
        buy_cap = 0

    # Short-reversal repair
    if pos <= cfg.get("reversal_pos_thresh", -999) and slope > cfg.get("reversal_slope_up", 999):
        sell_cap = 0
        buy_cap  = cfg.get("reversal_buy_clip", cfg["buy_clip"])

    # Aggressive taking
    take_edge = cfg["take_edge"]
    if ba is not None and ba < fv - take_edge and buy_cap > 0:
        qty = min(abs(ob.sell_orders[ba]), cfg["buy_clip"], buy_cap)
        orders.append(Order(product, ba, +qty));  buy_cap -= qty

    if bb is not None and bb > fv + take_edge and sell_cap > 0:
        qty = min(ob.buy_orders[bb], cfg["sell_clip"], sell_cap)
        orders.append(Order(product, bb, -qty));  sell_cap -= qty

    # Reservation price with A-S inventory skew
    skew = pos * cfg["gamma"] * cfg["sigma"] ** 2 * cfg["T"]
    res  = round(fv - skew)
    qe   = cfg["quote_edge"]

    if bb is not None and ba is not None:
        bid_px = min(bb + 1, res - max(1, qe // 2))
        ask_px = max(ba - 1, res + max(1, qe // 2))
        bid_px = min(bid_px, ba - 1);  ask_px = max(ask_px, bb + 1)
    else:
        bid_px = res - qe;  ask_px = res + qe

    frac = cfg["quote_inner_frac"]
    if buy_cap > 0:
        inner = max(1, int(buy_cap * frac))
        orders.append(Order(product, bid_px,     +inner))
        outer = buy_cap - inner
        if outer > 0:
            orders.append(Order(product, bid_px - 1, +outer))
    if sell_cap > 0:
        inner = max(1, int(sell_cap * frac))
        orders.append(Order(product, ask_px,     -inner))
        outer = sell_cap - inner
        if outer > 0:
            orders.append(Order(product, ask_px + 1, -outer))

    return orders


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 13 — TRADER (main entry point)
# ═══════════════════════════════════════════════════════════════════════════

class Trader:
    def run(self, state: TradingState):
        raw = json.loads(state.traderData) if state.traderData else {}
        ts  = state.timestamp

        # ── Restore persistent state ─────────────────────────────────────
        linear_kf: Dict[str, dict] = raw.get("linear_kf", {})
        linear_ticks: Dict[str, int] = raw.get("linear_ticks", {})
        vol_est_state: dict = raw.get("vol_est", {})
        cooldown_until: int = raw.get("cooldown_until", 0)

        vol_est = VolEstimator.from_dict(vol_est_state) if vol_est_state else VolEstimator()

        # ── Update vol estimator from VEX ────────────────────────────────
        vex_ob  = state.order_depths.get("VELVETFRUIT_EXTRACT")
        vex_mid = _mid(vex_ob) if vex_ob else None
        if vex_mid is not None:
            vol_est.update(vex_mid)

        S      = vex_mid if vex_mid is not None else 5250.0
        sigma  = vol_est.sigma_annual
        tte    = current_tte(ts)
        orders_out: Dict[str, List[Order]] = {}

        # ── PIPELINE A: Linear market making ────────────────────────────
        for product in ("HYDROGEL_PACK", "VELVETFRUIT_EXTRACT"):
            ob  = state.order_depths.get(product)
            pos = state.position.get(product, 0)
            if ob is None:
                orders_out[product] = [];  continue

            mid = _vwmid(ob) or _mid(ob)
            if mid is not None:
                kf = linear_kf.get(product)
                kf = kf_init(mid) if kf is None else kf_step(kf, mid)
                linear_kf[product]   = kf
                linear_ticks[product] = linear_ticks.get(product, 0) + 1

            kf    = linear_kf.get(product)
            ticks = linear_ticks.get(product, 0)
            cfg   = LINEAR_MM_CFG[product]

            if kf and ticks >= cfg["warmup"]:
                orders_out[product] = run_linear_mm(product, ob, pos, kf, ts)
            else:
                orders_out[product] = []

        # ── PIPELINE B: Options alpha ────────────────────────────────────
        positions = state.position
        all_ideas: List[TradeIdea] = []

        # 1. VEV_5300 rich-fade
        ob_5300 = state.order_depths.get("VEV_5300")
        pos_5300 = positions.get("VEV_5300", 0)
        if ob_5300:
            idea = run_vev5300_fade(ob_5300, pos_5300, S, tte, sigma, cooldown_until, ts)
            if idea:
                all_ideas.append(idea)
                if not idea.urgent_exit:
                    cooldown_until = ts + SIGNAL_COOLDOWN_TS

        # 2. Chain arbitrage
        chain_ideas = run_chain_arb(state.order_depths, positions, S, tte, sigma)
        all_ideas.extend(chain_ideas)

        # 3. Delta correction via VEX
        vex_pos = positions.get("VELVETFRUIT_EXTRACT", 0)
        if vex_ob:
            hedge_idea = run_delta_correction(
                vex_ob, vex_pos, positions, state.order_depths, S, tte, sigma
            )
            if hedge_idea:
                all_ideas.append(hedge_idea)

        # Sort by score, execute top idea per symbol
        all_ideas.sort(key=lambda x: x.score, reverse=True)
        executed_symbols: set = set()

        for idea in all_ideas:
            if idea.symbol in executed_symbols:
                continue
            # VEX linear MM is skipped if delta-correction already claimed it
            if idea.symbol == "VELVETFRUIT_EXTRACT" and idea.symbol in executed_symbols:
                continue

            ob    = state.order_depths.get(idea.symbol)
            pos   = positions.get(idea.symbol, 0)
            limit = POSITION_LIMITS.get(idea.symbol, 300)
            if ob is None:
                continue

            new_orders = execute_idea(idea, ob, pos, limit)
            if new_orders:
                existing  = orders_out.get(idea.symbol, [])
                orders_out[idea.symbol] = existing + new_orders
                executed_symbols.add(idea.symbol)

        # Ensure all VEV products have an entry
        for sym in VEV_PRODUCTS:
            if sym not in orders_out:
                orders_out[sym] = []

        # ── Persist state ────────────────────────────────────────────────
        new_state = {
            "linear_kf":     linear_kf,
            "linear_ticks":  linear_ticks,
            "vol_est":       vol_est.to_dict(),
            "cooldown_until": cooldown_until,
        }

        trader_data = json.dumps(new_state)
        conversions = 0
        return orders_out, conversions, trader_data
