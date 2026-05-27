"""
trader.py  ·  Prosperity IMC Round 4  ·  r4_v20
─────────────────────────────────────────────────────────────────────────────
VARIANT: v19 + VEV_5400 scaled to 300 + extract-rebound profit lock
─────────────────────────────────────────────────────────────────────────────
v19 confirmed: open rush fills everything pre-t=1600 with no slippage.
Final PnL: +17,097. Per-contract edges held vs v18 (book not the constraint).
  VEV_5200: avg entry 118.61, liq 88.50 → +30.11 × 300 = +9,033
  VEV_5300: avg entry  57.33, liq 39.00 → +18.33 × 300 = +5,499
  VEV_5400: avg entry  19.70, liq 11.50 → +8.20  × 200 = +1,640
  VEV_5500: avg entry   6.32, liq  3.50 → +2.82  × 300 = +846

v19 giveback analysis: all options peaked simultaneously at t=86,600.
Extract rebounded +9 ticks after t=86,400 (5244→5253). Delta-weighted loss:
  ~2,869 in aggregate giveback (matches delta × size × rebound exactly).

v20 changes:
  VEV_5400 max_short: 200 → 300  (+100 × 8.20 expected = +820)
    Book filled -200 at t=1000 with no slippage — depth for 300 confirmed.
  PROFIT LOCK: track rolling-minimum extract over last 50k ticks.
    After ts > PROFIT_LOCK_AFTER, if extract rebounds PROFIT_LOCK_REBOUND ticks
    above rolling min → urgent-exit ALL option shorts.
    Saves ~2,500–2,800 giveback if late-session rebound pattern repeats.
    Set ENABLE_PROFIT_LOCK=False to disable (revert to hold-to-liq).
─────────────────────────────────────────────────────────────────────────────
"""

import json
import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from datamodel import Order, OrderDepth, TradingState


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 1A — LINEAR MM CONSTANTS  (unchanged from v1/v14)
# ═══════════════════════════════════════════════════════════════════════════

LINEAR_MM_CFG = {
    "HYDROGEL_PACK": {
        "warmup": 10, "take_edge": 8, "quote_edge": 7,
        "quote_inner_frac": 0.60, "clip": 5, "buy_clip": 3, "sell_clip": 5,
        "slope_sell_gate": -0.15, "reversal_pos_thresh": -20,
        "reversal_slope_up": 0.25, "reversal_buy_clip": 8,
        "reversal_late_slope": 0.15,
        "gamma": 8.7e-7, "sigma": 219.0, "T": 1.0,
        "max_pos": 120, "repair_ratio": 0.75,
    },
    "VELVETFRUIT_EXTRACT": {
        "warmup": 10, "take_edge": 4, "quote_edge": 2,
        "quote_inner_frac": 0.60, "clip": 4,
        "gamma": 6.6e-6, "sigma": 113.0, "T": 1.0,
        "max_pos": 55, "repair_ratio": 0.75,
    },
}

ENABLE_VEV5000_BUY  = False
VEV5300_RICH_GAP    = 6.0
ENABLE_HYDROGEL_MM  = True
ENABLE_VEX_MM       = True

# ═══════════════════════════════════════════════════════════════════════════
# SECTION 1B — OPTIONS CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════

STRIKES = [4000, 4500, 5000, 5100, 5200, 5300, 5400, 5500, 6000, 6500]
VEV_PRODUCTS    = [f"VEV_{k}" for k in STRIKES]
VOUCHER_STRIKES = {f"VEV_{k}": k for k in STRIKES}

POSITION_LIMITS: Dict[str, int] = {
    "HYDROGEL_PACK": 200,
    "VELVETFRUIT_EXTRACT": 200,
    **{f"VEV_{k}": 300 for k in STRIKES},
}

# v7: raised to 800 — full 300+300 deep ITM exposure, no hedge to offset
NET_DELTA_LIMIT: int = 800

MIN_EDGE_TO_TAKE: Dict[str, float] = {
    "HYDROGEL_PACK": 2.0,
    "VELVETFRUIT_EXTRACT": 2.0,
    **{f"VEV_{k}": 5.0 for k in STRIKES},
}

ALPHA_PRIORITY: Dict[str, int] = {
    "chain_arb": 5, "empirical_option": 4, "iv_arb": 3,
    "smile_arb": 3, "directional": 2, "mm": 1, "hedge": 0,
}

ACTIVE_STRIKE_CONFIG: Dict[str, dict] = {
    "VEV_5000": {"K": 5000, "edge_thresh": 3, "max_pos": 30,  "delta": 0.65},
    "VEV_5100": {"K": 5100, "edge_thresh": 4, "max_pos": 40,  "delta": 0.58},
    "VEV_5200": {"K": 5200, "edge_thresh": 4, "max_pos": 70,  "delta": 0.44},
    "VEV_5300": {"K": 5300, "edge_thresh": 4, "max_pos": 70,  "delta": 0.27},
    "VEV_5400": {"K": 5400, "edge_thresh": 3, "max_pos": 40,  "delta": 0.13},
    "VEV_5500": {"K": 5500, "edge_thresh": 2, "max_pos": 30,  "delta": 0.055},
}

VEV5300_RICH_GAP   : float = 6.0
VEV5300_STRONG_GAP : float = 6.0
VEV5300_EXIT_GAP   : float = 1.5
VEV5300_MAX_SHORT  : int   = 300   # v19: 250→300, +50×18.2 expected = +910
VEV5300_CLIP       : int   = 25

VEV5000_CHEAP_GAP  : float = 4.0
VEV5000_EXIT_GAP   : float = 1.5
VEV5000_MAX_LONG   : int   = 50
VEV5000_CLIP       : int   = 10

VEV5400_RICH_GAP   : float = 2.0
VEV5400_EXIT_GAP   : float = 0.5
VEV5400_MAX_SHORT  : int   = 300   # v20: 200→300, +100×8.2 expected = +820
VEV5400_CLIP       : int   = 20
ENABLE_VEV5400_SELL: bool  = True

OTM_SELL_STRIKES    = {"VEV_5500": 5500}
OTM_MIN_EDGE        : float = 0.5
OTM_SAFETY_BUFFER   : int   = 200
OTM_MAX_SHORT       : int   = 300
OTM_CLIP            : int   = 50
ENABLE_OTM_SELL     : bool  = True

# ── v13: Near-ATM rich sells — confirmed by GPT data ─────────────────────
# VEV_5200: avg bid 87.61, fair 77.33, edge +10.28 ticks → BIG opportunity
# VEV_5100: avg bid 158.78, fair 155.83, edge +2.95 ticks → moderate
# VEV_5000: bid BELOW fair → do not sell
# v19: VEV_5100 re-enabled for open-rush-only test.
# Previous backtest loss was from gradual entry at mid-session prices.
# Open rush fills at t<1700 when premium spike is largest (same pattern as 5200).
# Hard entry gate: VEV5100_NO_ENTRIES_AFTER=8000 (only first 8k ticks).
# If backtest still negative, flip ENABLE_VEV5100_SELL=False.
ENABLE_VEV5100_SELL   : bool = True
VEV5100_NO_ENTRIES_AFTER : int = 8_000   # only ride the opening premium spike

NEAR_ATM_SELL_CFG = {
    "VEV_5100": {"K": 5100, "edge_thresh": 3,  "max_short": 150, "clip": 30, "delta_est": 0.85},
    "VEV_5200": {"K": 5200, "edge_thresh": 4,  "max_short": 300, "clip": 30, "delta_est": 0.60},
    # VEV_5000 NOT SOLD — bid (248) is below our fair (250.5)
}
ENABLE_NEAR_ATM_SELL: bool = True

DAY_END_TS           : int = 100_000
NO_NEW_ENTRIES_AFTER : int = int(0.90 * DAY_END_TS)
FORCE_FLATTEN_AFTER  : int = int(0.97 * DAY_END_TS)
SIGNAL_COOLDOWN_TS   : int = 5_000

ENABLE_VEV5300_FADE    : bool  = True
ENABLE_EMPIRICAL_BROAD : bool  = False
EMPIRICAL_SELL_WHITELIST: tuple = ()

TTE_START     = 4   # Round 4
DAY_LENGTH    = 1_000_000
TICKS_PER_DAY = 10_000
BACHELIER_COMPARISON_LOG = False
IV_ARB_PRIOR_SIGMA_DAY   = 55.0
VEX_FV_ANCHOR            = 5250.0

OPEN_RUSH_UNTIL : int = 15_000   # v18: skip cooldown before this timestamp

# ── v20: Extract-rebound profit lock ─────────────────────────────────────
# After PROFIT_LOCK_AFTER, if extract rises PROFIT_LOCK_REBOUND ticks above
# its rolling minimum (tracked over PROFIT_LOCK_WINDOW ticks), close all
# option shorts via urgent exit. Captures the t=86,600 peak observed in
# v18/v19 before the +9-tick rebound gave back ~2,869.
# Set ENABLE_PROFIT_LOCK=False to hold to liquidation (safe fallback).
ENABLE_PROFIT_LOCK    : bool  = True
PROFIT_LOCK_AFTER     : int   = 80_000   # only arm after this timestamp
PROFIT_LOCK_REBOUND   : float = 6.0      # ticks above rolling min to trigger
PROFIT_LOCK_WINDOW    : int   = 50_000   # rolling window for extract min (in ts)
PROFIT_LOCK_COOLDOWN  : int   = 10_000   # don't re-trigger within this many ts

# Chain arb: full aggression, no hedge
CHAIN_INTRINSIC_TOL = 0.5   # trigger on small discounts
CHAIN_MONO_TOL      = 2.0
CHAIN_MAX_POS       = 300   # full position limit


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 2 — TRADE IDEA
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class TradeIdea:
    symbol:       str
    target_qty:   int
    fair_value:   float
    edge:         float
    confidence:   float
    delta:        float
    alpha_type:   str
    reason:       str  = ""
    urgent_exit:  bool = False
    intrinsic_cap: Optional[float] = None

    @property
    def score(self) -> float:
        return self.edge * self.confidence * ALPHA_PRIORITY.get(self.alpha_type, 1)


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 3 — ORDER BOOK HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def _best(ob: OrderDepth) -> Tuple[Optional[int], Optional[int]]:
    return (max(ob.buy_orders) if ob.buy_orders else None,
            min(ob.sell_orders) if ob.sell_orders else None)

def _mid(ob: OrderDepth) -> Optional[float]:
    bb, ba = _best(ob)
    if bb is not None and ba is not None: return (bb + ba) / 2.0
    if bb is not None: return float(bb)
    if ba is not None: return float(ba)
    return None

def _on_cooldown(vev: str, ts: int, cooldown_ts: dict) -> bool:
    """v18: bypass cooldown during open rush window."""
    if ts < OPEN_RUSH_UNTIL:
        return False   # no cooldown — fill as fast as possible at open
    return ts - cooldown_ts.get(vev, -999999) < SIGNAL_COOLDOWN_TS


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 4 — TTE=4 EXTRINSIC TABLE + FAIR VALUE
# ═══════════════════════════════════════════════════════════════════════════

_EXTRINSIC_TTE4: List[Tuple[Tuple[float, float], float]] = [
    ((-300, -250), 0.500), ((-250, -200), 0.500), ((-200, -150), 0.667),
    ((-150, -100), 2.667), ((-100,  -75), 6.333), (( -75,  -50), 14.667),
    (( -50,  -25), 30.667), (( -25,    0), 43.000), ((   0,   25), 43.000),
    ((  25,   75), 27.333), ((  75,  125), 15.500), (( 125,  175), 5.833),
    (( 175,  225), 3.333), (( 225,  275), 0.500), (( 275,  325), 0.500),
]

_EXTRINSIC_KNOTS: List[Tuple[float, float]] = [
    ((lo + hi) / 2.0, val) for (lo, hi), val in _EXTRINSIC_TTE4
]

def smooth_extrinsic_tte4(moneyness: float) -> float:
    knots = _EXTRINSIC_KNOTS
    if moneyness <= knots[0][0]:  return knots[0][1]
    if moneyness >= knots[-1][0]: return knots[-1][1]
    for i in range(len(knots) - 1):
        x0, y0 = knots[i]; x1, y1 = knots[i + 1]
        if x0 <= moneyness <= x1:
            return y0 + (y1 - y0) * (moneyness - x0) / (x1 - x0)
    return 0.0

def empirical_fair_value(S: float, K: float) -> float:
    return max(S - K, 0.0) + smooth_extrinsic_tte4(S - K)


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 5 — MATH PRIMITIVES
# ═══════════════════════════════════════════════════════════════════════════

def _ncdf(x): return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
def _npdf(x): return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)

def bachelier_call(S, K, T, sigma_abs):
    if T <= 0 or sigma_abs <= 0: return max(S - K, 0.0)
    vol_T = sigma_abs * math.sqrt(T); d = (S - K) / vol_T
    return (S - K) * _ncdf(d) + vol_T * _npdf(d)

def bachelier_delta(S, K, T, sigma_abs):
    if T <= 0 or sigma_abs <= 0: return 1.0 if S > K else 0.0
    return _ncdf((S - K) / (sigma_abs * math.sqrt(T)))


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 6 — VOL ESTIMATOR
# ═══════════════════════════════════════════════════════════════════════════

_VOL_WINDOW = 200

class VolEstimator:
    def __init__(self):
        self._prices: List[float] = []; self.sigma_tick = 0.0
    def update(self, price):
        self._prices.append(price)
        if len(self._prices) > _VOL_WINDOW + 1: self._prices.pop(0)
        n = len(self._prices)
        if n < 3: return
        diffs = [abs(self._prices[i] - self._prices[i-1]) for i in range(1, n)]
        self.sigma_tick = math.sqrt(sum(d*d for d in diffs) / len(diffs))
    def sigma_per_day(self): return self.sigma_tick * math.sqrt(TICKS_PER_DAY)
    def to_dict(self):   return {"prices": self._prices, "sigma_tick": self.sigma_tick}
    @classmethod
    def from_dict(cls, d):
        o = cls(); o._prices = d.get("prices", []); o.sigma_tick = d.get("sigma_tick", 0.0); return o


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 7 — KALMAN FILTER
# ═══════════════════════════════════════════════════════════════════════════

_KF_QL = 0.4; _KF_QS = 0.008; _KF_R = 10.0
VEX_DRIFT_ANCHOR = 0.65; VEX_DRIFT_SLOPE = 0.06

def kf_init(price):
    return {"l": price, "s": 0.0, "p00": 100.0, "p01": 0.0, "p11": 100.0}

def kf_step(st, obs):
    l, s = st["l"], st["s"]; p00, p01, p11 = st["p00"], st["p01"], st["p11"]
    lp = l + s; sp = s
    pp00 = p00 + 2*p01 + p11 + _KF_QL; pp01 = p01 + p11; pp11 = p11 + _KF_QS
    Sv = pp00 + _KF_R; K0 = pp00/Sv; K1 = pp01/Sv; inn = obs - lp
    return {"l": lp+K0*inn, "s": sp+K1*inn, "p00": (1-K0)*pp00, "p01": (1-K0)*pp01, "p11": pp11-K1*pp01}


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 8 — TTE HELPER
# ═══════════════════════════════════════════════════════════════════════════

def current_tte(timestamp):
    return max(TTE_START - timestamp / DAY_LENGTH, 1e-4)


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 9 — ALPHA LOGGING
# ═══════════════════════════════════════════════════════════════════════════

def build_alpha_log(ts, sym, S, K, bid, ask, fair, be, se, target, delta, conf, reason):
    return (f"[L1] t={ts:>8} | {sym:<10} | S={S:>7.1f} K={K:>5} | "
            f"bid={str(bid or '-'):>6} ask={str(ask or '-'):>6} | fair={fair:>7.2f} | "
            f"buy_edge={be:>+7.2f} sell_edge={se:>+7.2f} | "
            f"target={target:>+5} delta={delta:.3f} conf={conf:.2f} | {reason}")


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 10A — VEV_5300 RICH FADE
# ═══════════════════════════════════════════════════════════════════════════

def vev5300_rich_fade_module(state, extract_mid, cooldown_ts, log=True):
    vev = "VEV_5300"; K = 5300
    ts = state.timestamp; pos = state.position.get(vev, 0)
    ob = state.order_depths.get(vev)
    if ob is None: return []
    bid, ask = _best(ob); mid = _mid(ob)
    fair = empirical_fair_value(extract_mid, K)
    rich_gap = (mid - fair) if mid is not None else 0.0

    def _idea(target, edge, conf, reason, urgent=False):
        return TradeIdea(symbol=vev, target_qty=target, fair_value=fair,
                         edge=edge, confidence=conf, delta=0.27,
                         alpha_type="empirical_option", reason=reason, urgent_exit=urgent)

    if ts >= FORCE_FLATTEN_AFTER:
        if pos != 0:
            cheap = ask is not None and ask <= fair + 0.5
            urgent = cheap
            return [_idea(0, 0.0, 1.0, f"VEV5300_FLATTEN urgent={urgent}", urgent=urgent)]
        return []

    if pos < 0 and rich_gap < VEV5300_EXIT_GAP:
        return [_idea(0, abs(rich_gap), 0.8, f"VEV5300_EXIT gap={rich_gap:.2f}")]

    if ts >= NO_NEW_ENTRIES_AFTER or rich_gap < VEV5300_RICH_GAP: return []
    if pos <= -VEV5300_MAX_SHORT: return []
    if _on_cooldown(vev, ts, cooldown_ts): return []

    confidence = min(1.0, rich_gap / VEV5300_STRONG_GAP)
    target = max(-VEV5300_MAX_SHORT, pos - VEV5300_CLIP)
    cooldown_ts[vev] = ts
    return [_idea(target, rich_gap, confidence, f"VEV5300_RICH gap={rich_gap:.2f} fair={fair:.2f}")]


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 10C — VEV_5400 PASSIVE SELL
# ═══════════════════════════════════════════════════════════════════════════

def vev5400_passive_sell_module(state, extract_mid, cooldown_ts, log=True):
    if not ENABLE_VEV5400_SELL: return []
    vev = "VEV_5400"; K = 5400
    ts = state.timestamp; pos = state.position.get(vev, 0)
    ob = state.order_depths.get(vev)
    if ob is None: return []
    bid, ask = _best(ob); mid = _mid(ob)
    fair = empirical_fair_value(extract_mid, K)
    rich_gap = (mid - fair) if mid is not None else 0.0

    def _idea(target, edge, conf, reason, urgent=False):
        return TradeIdea(symbol=vev, target_qty=target, fair_value=fair,
                         edge=edge, confidence=min(conf, 0.7), delta=0.13,
                         alpha_type="empirical_option", reason=reason, urgent_exit=urgent)

    if ts >= FORCE_FLATTEN_AFTER:
        if pos != 0:
            cheap = ask is not None and ask <= fair + 0.5
            return [_idea(0, 0.0, 1.0, "VEV5400_FLATTEN", urgent=cheap)]
        return []

    if pos < 0 and rich_gap < VEV5400_EXIT_GAP:
        return [_idea(0, abs(rich_gap), 0.6, f"VEV5400_EXIT gap={rich_gap:.2f}")]

    if ts >= NO_NEW_ENTRIES_AFTER or rich_gap < VEV5400_RICH_GAP or pos <= -VEV5400_MAX_SHORT: return []
    if _on_cooldown(vev, ts, cooldown_ts): return []

    confidence = min(0.7, rich_gap / (VEV5400_RICH_GAP * 2))
    target = max(-VEV5400_MAX_SHORT, pos - VEV5400_CLIP)
    cooldown_ts[vev] = ts
    return [_idea(target, rich_gap, confidence, f"VEV5400_PASSIVE gap={rich_gap:.2f}")]


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 10D — DEEP OTM SELL MODULE  [v8 NEW]
# ═══════════════════════════════════════════════════════════════════════════
# VEV_6000 and VEV_6500 at TTE=4 with extract ~5250 are essentially worthless.
# Fair value from extrinsic table ≈ 0.5 ticks (clipped floor for moneyness < -300).
# Any bid > OTM_MIN_EDGE is pure profit if held to liquidation at 0.
#
# Safety: only sell if extract < K - OTM_SAFETY_BUFFER.
# At extract=5250: K-400=5600 for VEV_6000. 5250 < 5600 ✓ safe.
#                  K-400=6100 for VEV_6500. 5250 < 6100 ✓ safe.
# If extract spikes above 5600, suppress VEV_6000 selling to avoid assignment risk.
#
# No exit before EOD — these are expected to expire worthless.
# If they ARE in the money at expiry, we lose. Risk is extract going to 6000+.

def otm_sell_module(
    state:       TradingState,
    extract_mid: float,
    cooldown_ts: dict,
    log:         bool = True,
) -> List[TradeIdea]:
    if not ENABLE_OTM_SELL: return []
    ts    = state.timestamp
    ideas = []

    for vev, K in OTM_SELL_STRIKES.items():
        # Safety check: only sell if extract is comfortably below strike
        if extract_mid >= K - OTM_SAFETY_BUFFER:
            if log: print(f"[OTM_SELL] {vev} SKIP: extract={extract_mid:.1f} too close to K={K}")
            continue

        ob  = state.order_depths.get(vev)
        if ob is None: continue
        bid, ask = _best(ob)
        pos      = state.position.get(vev, 0)

        # Fair value is essentially 0 at this moneyness
        fair = empirical_fair_value(extract_mid, K)   # ≈ 0.5

        # Sell if there's ANY meaningful bid
        if bid is None or bid < OTM_MIN_EDGE:
            continue

        # Don't sell if already at max short
        if pos <= -OTM_MAX_SHORT:
            continue

        # No new entries after cutoff (let existing shorts ride to liquidation)
        if ts >= NO_NEW_ENTRIES_AFTER:
            continue

        cap_sell   = POSITION_LIMITS[vev] + pos
        target     = max(-OTM_MAX_SHORT, pos - OTM_CLIP)
        edge       = bid - fair   # basically = bid price since fair ≈ 0.5
        confidence = min(0.95, edge / 10.0)   # near-certain edge

        if log:
            print(f"[OTM_SELL] t={ts} {vev} K={K} S={extract_mid:.1f} "
                  f"bid={bid} fair={fair:.2f} edge={edge:.2f} "
                  f"target={target} pos={pos}")

        ideas.append(TradeIdea(
            symbol=vev, target_qty=target, fair_value=fair,
            edge=edge, confidence=confidence, delta=0.01,   # essentially no delta
            alpha_type="empirical_option",
            reason=f"OTM_SELL K={K} bid={bid} fair={fair:.2f} edge={edge:.2f}",
        ))

    return ideas


def near_atm_sell_module(state, extract_mid, cooldown_ts, log=True):
    """
    v13/v19: Sell near-ATM options confirmed rich at open.

    VEV_5200: avg entry 118.64 in v18, liq 88.50 → +30.14/contract at 300 = +9,042
    VEV_5100: open-rush only (entries stop at VEV5100_NO_ENTRIES_AFTER=8000).
              delta ~0.85 but extrinsic premium at open can be 10-20+ ticks.
              Gated early to avoid mid-session adverse fills.

    Uses same structure as vev5300_rich_fade_module.
    Exit when bid < fair + exit_buffer. EOD conditional flatten.
    """
    if not ENABLE_NEAR_ATM_SELL: return []
    ts = state.timestamp
    ideas = []

    for vev, cfg in NEAR_ATM_SELL_CFG.items():
        K          = cfg["K"]
        thresh     = cfg["edge_thresh"]
        max_short  = cfg["max_short"]
        clip       = cfg["clip"]
        delta_est  = cfg["delta_est"]

        ob = state.order_depths.get(vev)
        if ob is None: continue
        bid, ask = _best(ob)
        mid      = _mid(ob)
        pos      = state.position.get(vev, 0)
        fair     = empirical_fair_value(extract_mid, K)
        rich_gap = (mid - fair) if mid is not None else 0.0

        def _idea(target, edge, conf, reason, urgent=False):
            return TradeIdea(
                symbol=vev, target_qty=target, fair_value=fair,
                edge=edge, confidence=conf, delta=delta_est,
                alpha_type="empirical_option", reason=reason, urgent_exit=urgent,
            )

        # EOD conditional flatten
        if ts >= FORCE_FLATTEN_AFTER:
            if pos != 0:
                cheap = ask is not None and ask <= fair + 0.5
                return [_idea(0, 0.0, 1.0, f"{vev}_FLATTEN", urgent=cheap)]
            return []

        # Exit when edge disappears
        if pos < 0 and rich_gap < 1.0:
            ideas.append(_idea(0, abs(rich_gap), 0.8, f"{vev}_EXIT gap={rich_gap:.2f}"))
            continue

        # No new entries after cutoff
        if ts >= NO_NEW_ENTRIES_AFTER: continue
        # v19: VEV_5100 hard entry gate — only during open rush premium spike
        if vev == "VEV_5100" and (not ENABLE_VEV5100_SELL or ts >= VEV5100_NO_ENTRIES_AFTER):
            continue
        if rich_gap < thresh: continue
        if pos <= -max_short: continue
        if _on_cooldown(vev, ts, cooldown_ts): continue

        confidence = min(0.9, rich_gap / (thresh * 2))
        target     = max(-max_short, pos - clip)
        cooldown_ts[vev] = ts

        if log:
            print(f"[NEAR_ATM_SELL] t={ts} {vev} K={K} bid={bid} "
                  f"fair={fair:.2f} gap={rich_gap:.2f} target={target} pos={pos}")

        ideas.append(_idea(target, rich_gap, confidence,
                           f"{vev}_RICH gap={rich_gap:.2f} fair={fair:.2f}"))

    return ideas


def chain_arb_module(state, extract_mid, log=True):
    S = extract_mid
    bids, asks, mids_ = {}, {}, {}
    for vev in VOUCHER_STRIKES:
        ob = state.order_depths.get(vev)
        if ob is None: bids[vev] = asks[vev] = mids_[vev] = None
        else:
            bb, ba = _best(ob); bids[vev] = bb; asks[vev] = ba; mids_[vev] = _mid(ob)

    incremental = {}

    # Rule 1: Intrinsic (uses MID — more fills than bid-based)
    for vev, K in VOUCHER_STRIKES.items():
        intrinsic = max(S - K, 0.0)
        if intrinsic < 1.0: continue
        ba = asks[vev]
        if ba is not None and ba < intrinsic - CHAIN_INTRINSIC_TOL:
            qty = min(CHAIN_MAX_POS, POSITION_LIMITS[vev] - state.position.get(vev, 0))
            if qty > 0:
                incremental[vev] = incremental.get(vev, 0) + qty
                if log: print(f"[CHAIN] t={state.timestamp} {vev} INTRINSIC: "
                              f"S={S:.1f} K={K} intrinsic={intrinsic:.2f} ask={ba} qty={qty}")

    # Rule 2: Monotonicity
    sorted_vevs = sorted(VOUCHER_STRIKES.items(), key=lambda x: x[1])
    for i in range(len(sorted_vevs) - 1):
        v_lo, K_lo = sorted_vevs[i]; v_hi, K_hi = sorted_vevs[i + 1]
        mid_lo = mids_[v_lo]; mid_hi = mids_[v_hi]
        if mid_lo is None or mid_hi is None: continue
        if mid_lo >= mid_hi - CHAIN_MONO_TOL: continue
        ba_lo = asks[v_lo]; bb_hi = bids[v_hi]
        if ba_lo is None or bb_hi is None or ba_lo >= bb_hi: continue
        qty = min(CHAIN_MAX_POS, POSITION_LIMITS[v_lo] - state.position.get(v_lo, 0),
                  POSITION_LIMITS[v_hi] + state.position.get(v_hi, 0))
        if qty > 0:
            incremental[v_lo] = incremental.get(v_lo, 0) + qty
            incremental[v_hi] = incremental.get(v_hi, 0) - qty

    ideas = []
    for vev, inc_qty in incremental.items():
        if inc_qty == 0: continue
        pos = state.position.get(vev, 0); K = VOUCHER_STRIKES[vev]
        limit = POSITION_LIMITS[vev]
        abs_target = max(-limit, min(limit, pos + inc_qty))
        fair = empirical_fair_value(S, K) if vev in ACTIVE_STRIKE_CONFIG else max(S - K, 0.0)
        delta = ACTIVE_STRIKE_CONFIG[vev]["delta"] if vev in ACTIVE_STRIKE_CONFIG else (1.0 if S > K else 0.0)
        exec_price = asks[vev] if inc_qty > 0 else bids[vev]
        edge = abs(fair - exec_price) if exec_price is not None else 0.0
        intrinsic_val = max(S - K, 0.0) if inc_qty > 0 else None
        ideas.append(TradeIdea(
            symbol=vev, target_qty=abs_target, fair_value=fair,
            edge=edge, confidence=1.0, delta=delta,
            alpha_type="chain_arb", urgent_exit=True,
            intrinsic_cap=intrinsic_val,
            reason=f"chain_arb inc={inc_qty:+d} abs={abs_target:+d}",
        ))
    return ideas


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 12 — ALPHA STATE MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════

def linear_mm_state_init(state):
    st = {}
    for product in LINEAR_MM_CFG:
        ob = state.order_depths.get(product); mid = _mid(ob) if ob else 5000.0
        st[product] = {"kf": {"l": mid or 5000.0, "s": 0.0, "p00": 400.0, "p01": 0.0, "p11": 100.0}, "ticks": 0}
    return st

def alpha_state_init(state):
    ob = state.order_depths.get("VELVETFRUIT_EXTRACT"); mid = _mid(ob) if ob else VEX_FV_ANCHOR
    return {
        "vex_kf": kf_init(mid or VEX_FV_ANCHOR),
        "vol_est": VolEstimator().to_dict(),
        "cooldown_ts": {},
        "linear_mm": linear_mm_state_init(state),
        # v20: profit lock state
        "extract_history": [],   # list of (timestamp, mid) for rolling min
        "profit_lock_fired_ts": -1,   # timestamp lock last fired (-1 = never)
        "profit_lock_active": False,
    }

def alpha_state_update(raw_alpha, state):
    ob = state.order_depths.get("VELVETFRUIT_EXTRACT"); mid = _mid(ob) if ob else None
    kf = raw_alpha.get("vex_kf") or kf_init(mid or VEX_FV_ANCHOR)
    vol = VolEstimator.from_dict(raw_alpha.get("vol_est") or {})
    if mid is not None: kf = kf_step(kf, mid); vol.update(mid)

    # v20: maintain rolling extract history for profit lock
    ts = state.timestamp
    hist = raw_alpha.get("extract_history") or []
    if mid is not None:
        hist.append((ts, mid))
    # prune entries older than the rolling window
    cutoff = ts - PROFIT_LOCK_WINDOW
    hist = [(t, m) for t, m in hist if t >= cutoff]

    return {
        "vex_kf": kf, "vol_est": vol.to_dict(),
        "cooldown_ts": raw_alpha.get("cooldown_ts") or {},
        "linear_mm": raw_alpha.get("linear_mm") or {},
        "extract_history": hist,
        "profit_lock_fired_ts": raw_alpha.get("profit_lock_fired_ts", -1),
        "profit_lock_active": raw_alpha.get("profit_lock_active", False),
    }, mid


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 13 — RUN_ALPHA
# ═══════════════════════════════════════════════════════════════════════════

def check_profit_lock(state, alpha_state, extract_mid, log=True):
    """
    v20: Extract-rebound profit lock.
    Returns a list of urgent-exit TradeIdeas for all held option shorts
    if extract has rebounded PROFIT_LOCK_REBOUND ticks above its rolling min
    after PROFIT_LOCK_AFTER. One-shot per cooldown window.

    Logic:
      1. Only arm after PROFIT_LOCK_AFTER timestamp.
      2. Compute rolling min of extract over last PROFIT_LOCK_WINDOW ticks.
      3. If current extract > rolling_min + PROFIT_LOCK_REBOUND → fire.
      4. Cooldown prevents immediate re-fire.
    """
    if not ENABLE_PROFIT_LOCK: return [], alpha_state
    ts = state.timestamp
    if ts < PROFIT_LOCK_AFTER: return [], alpha_state
    if extract_mid is None: return [], alpha_state

    # Check cooldown
    last_fired = alpha_state.get("profit_lock_fired_ts", -1)
    if last_fired > 0 and ts - last_fired < PROFIT_LOCK_COOLDOWN:
        return [], alpha_state

    hist = alpha_state.get("extract_history", [])
    if not hist: return [], alpha_state

    rolling_min = min(m for _, m in hist)
    rebound = extract_mid - rolling_min

    if rebound < PROFIT_LOCK_REBOUND:
        return [], alpha_state

    # Fire: urgent-exit all option shorts
    ideas = []
    option_symbols = [f"VEV_{k}" for k in STRIKES]
    for sym in option_symbols:
        pos = state.position.get(sym, 0)
        if pos >= 0: continue   # not short, nothing to do
        ob = state.order_depths.get(sym)
        if ob is None: continue
        bid, ask = _best(ob)
        fair = empirical_fair_value(extract_mid, VOUCHER_STRIKES[sym])
        ideas.append(TradeIdea(
            symbol=sym, target_qty=0, fair_value=fair,
            edge=0.0, confidence=1.0,
            delta=ACTIVE_STRIKE_CONFIG[sym]["delta"] if sym in ACTIVE_STRIKE_CONFIG else 0.1,
            alpha_type="empirical_option",
            reason=f"PROFIT_LOCK rebound={rebound:.1f} rolling_min={rolling_min:.1f}",
            urgent_exit=True,
        ))

    if ideas:
        alpha_state["profit_lock_fired_ts"] = ts
        alpha_state["profit_lock_active"] = True
        if log:
            print(f"[PROFIT_LOCK] t={ts} extract={extract_mid:.1f} "
                  f"rolling_min={rolling_min:.1f} rebound={rebound:.1f} "
                  f"→ EXITING {len(ideas)} option shorts")

    return ideas, alpha_state


def run_alpha(state, alpha_state, log=True):
    updated_state, extract_mid = alpha_state_update(alpha_state, state)
    if extract_mid is None: return [], updated_state
    cooldown_ts = updated_state["cooldown_ts"]

    # v20: profit lock check — highest priority, overrides all entry signals
    lock_ideas, updated_state = check_profit_lock(state, updated_state, extract_mid, log=log)
    if lock_ideas:
        # Profit lock fired: only emit exit ideas, suppress all new entries
        updated_state["cooldown_ts"] = cooldown_ts
        return lock_ideas, updated_state

    ideas = []
    ideas += chain_arb_module(state, extract_mid, log=log)
    if ENABLE_VEV5300_FADE:
        ideas += vev5300_rich_fade_module(state, extract_mid, cooldown_ts, log=log)
    if ENABLE_VEV5400_SELL:
        ideas += vev5400_passive_sell_module(state, extract_mid, cooldown_ts, log=log)
    if ENABLE_OTM_SELL:
        ideas += otm_sell_module(state, extract_mid, cooldown_ts, log=log)
    if ENABLE_NEAR_ATM_SELL:
        ideas += near_atm_sell_module(state, extract_mid, cooldown_ts, log=log)
    updated_state["cooldown_ts"] = cooldown_ts
    return ideas, updated_state


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 14-16 — AGGREGATOR / CAPS / DELTA CORRECTION
# ═══════════════════════════════════════════════════════════════════════════

def aggregate(ideas, current_positions):
    CONFLICT_THRESHOLD = 0.3
    by_symbol = {}
    for idea in ideas: by_symbol.setdefault(idea.symbol, []).append(idea)
    resolved = {}
    for symbol, symbol_ideas in by_symbol.items():
        symbol_ideas.sort(key=lambda x: x.score, reverse=True)
        winner = symbol_ideas[0]
        if len(symbol_ideas) == 1:
            resolved[symbol] = winner; continue
        net_target = winner.target_qty; ws = winner.score
        for other in symbol_ideas[1:]:
            same_dir = (other.target_qty * winner.target_qty) >= 0
            if same_dir:
                net_target = (max(net_target, other.target_qty) if winner.target_qty >= 0
                              else min(net_target, other.target_qty))
            else:
                if ws > 0 and (other.score / ws) >= CONFLICT_THRESHOLD:
                    net_target += other.target_qty
        total_score = sum(i.score for i in symbol_ideas) or 1.0
        resolved[symbol] = TradeIdea(
            symbol=symbol, target_qty=net_target,
            fair_value=sum(i.fair_value*i.score for i in symbol_ideas)/total_score,
            edge=winner.edge, confidence=winner.confidence,
            delta=sum(i.delta*i.score for i in symbol_ideas)/total_score,
            alpha_type=winner.alpha_type,
            reason=f"resolved({len(symbol_ideas)}): {winner.reason}",
            urgent_exit=winner.urgent_exit or any(i.urgent_exit for i in symbol_ideas),
            intrinsic_cap=winner.intrinsic_cap,
        )
    return resolved

def apply_hard_limits(resolved, current_positions):
    capped = {}
    for symbol, idea in resolved.items():
        limit = POSITION_LIMITS.get(symbol)
        if limit is None: continue
        ct = max(-limit, min(limit, idea.target_qty))
        capped[symbol] = TradeIdea(
            symbol=symbol, target_qty=ct, fair_value=idea.fair_value, edge=idea.edge,
            confidence=idea.confidence, delta=idea.delta, alpha_type=idea.alpha_type,
            reason=idea.reason + (f" [CLIPPED {idea.target_qty}→{ct}]" if ct != idea.target_qty else ""),
            urgent_exit=idea.urgent_exit, intrinsic_cap=idea.intrinsic_cap,
        )
    return capped

def apply_delta_correction(capped, current_positions, extract_mid,
                           net_delta_limit=NET_DELTA_LIMIT,
                           extract_limit=POSITION_LIMITS["VELVETFRUIT_EXTRACT"]):
    result = dict(capped)
    option_delta = sum(result[f"VEV_{k}"].target_qty * result[f"VEV_{k}"].delta
                       for k in STRIKES if f"VEV_{k}" in result)
    extract_idea = result.get("VELVETFRUIT_EXTRACT")
    extract_target = extract_idea.target_qty if extract_idea else 0
    net_delta = option_delta + extract_target
    if abs(net_delta) <= net_delta_limit: return result
    req = (net_delta_limit if net_delta > net_delta_limit else -net_delta_limit) - net_delta
    new_et = max(-extract_limit, min(extract_limit, extract_target + req))
    hedge_fair = extract_mid if extract_mid is not None else (extract_idea.fair_value if extract_idea else 0.0)
    result["VELVETFRUIT_EXTRACT"] = TradeIdea(
        symbol="VELVETFRUIT_EXTRACT", target_qty=int(new_et),
        fair_value=hedge_fair, edge=0.0, confidence=1.0, delta=1.0,
        alpha_type="hedge", reason=f"delta_hedge net={net_delta:.1f}→{new_et}")
    return result


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 17 — PRICE-AWARE EXECUTOR
# ═══════════════════════════════════════════════════════════════════════════

def execute_orders(resolved, state):
    all_orders = {}
    for symbol, idea in resolved.items():
        current = state.position.get(symbol, 0)
        gap = idea.target_qty - current
        if gap == 0: continue
        if symbol not in state.order_depths:
            print(f"[WARN] No depth for {symbol}"); continue
        book = state.order_depths[symbol]
        product_orders = []; remaining = gap

        if idea.urgent_exit:
            if gap > 0 and book.sell_orders:
                cap = idea.intrinsic_cap
                for ask_price, ask_vol in sorted(book.sell_orders.items()):
                    if remaining <= 0: break
                    if cap is not None and ask_price >= cap:
                        print(f"[CHAIN_CAP] {symbol} stop ask={ask_price} >= cap={cap:.1f}")
                        break
                    take_vol = min(remaining, -ask_vol)
                    product_orders.append(Order(symbol, ask_price, take_vol))
                    remaining -= take_vol
                    print(f"[URGENT_EXIT] BUY {symbol} p={ask_price} qty={take_vol}")
            elif gap < 0 and book.buy_orders:
                for bid_price, bid_vol in sorted(book.buy_orders.items(), reverse=True):
                    if remaining >= 0: break
                    take_vol = max(remaining, -bid_vol)
                    product_orders.append(Order(symbol, bid_price, take_vol))
                    remaining -= take_vol
                    print(f"[URGENT_EXIT] SELL {symbol} p={bid_price} qty={take_vol}")
            if product_orders: all_orders[symbol] = product_orders
            continue

        min_edge = MIN_EDGE_TO_TAKE.get(symbol, 2.0)
        max_buy = idea.fair_value - min_edge
        min_sell = idea.fair_value + min_edge

        if gap > 0:
            if book.sell_orders:
                for p, v in sorted(book.sell_orders.items()):
                    if remaining <= 0 or p > max_buy: break
                    q = min(remaining, -v); product_orders.append(Order(symbol, p, q)); remaining -= q
            if remaining > 0:
                if book.buy_orders:
                    bb = max(book.buy_orders.keys())
                    ba = min(book.sell_orders.keys()) if book.sell_orders else float("inf")
                    pp = min(bb+1, int(max_buy), ba-1)
                else: pp = int(max_buy)
                if pp > 0: product_orders.append(Order(symbol, pp, remaining))
        else:
            if book.buy_orders:
                for p, v in sorted(book.buy_orders.items(), reverse=True):
                    if remaining >= 0 or p < min_sell: break
                    q = max(remaining, -v); product_orders.append(Order(symbol, p, q)); remaining -= q
            if remaining < 0:
                if book.sell_orders:
                    ba = min(book.sell_orders.keys())
                    bb = max(book.buy_orders.keys()) if book.buy_orders else 0
                    pp = max(ba-1, int(min_sell), bb+1)
                else: pp = int(min_sell)
                if pp > 0: product_orders.append(Order(symbol, pp, remaining))

        if product_orders: all_orders[symbol] = product_orders
    return all_orders


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 18 — DEBUG TABLE
# ═══════════════════════════════════════════════════════════════════════════

def build_debug_table(resolved, state):
    header = (f"{'Symbol':<22} | {'Mid':>6} | {'Fair':>6} | {'Bid':>6} | {'Ask':>6} | "
              f"{'BuyEdge':>8} | {'SellEdge':>9} | {'Delta':>6} | {'Target':>7} | Signal")
    rows = [header, "-" * len(header)]
    for symbol, idea in sorted(resolved.items()):
        book = state.order_depths.get(symbol)
        if book is None: continue
        bid = max(book.buy_orders.keys()) if book.buy_orders else None
        ask = min(book.sell_orders.keys()) if book.sell_orders else None
        mid = ((bid + ask) / 2) if (bid and ask) else (bid or ask or 0)
        be = idea.fair_value - ask if ask else float("nan")
        se = bid - idea.fair_value if bid else float("nan")
        sig = "BUY" if be > 0 else ("SELL" if se > 0 else "NONE")
        rows.append(f"{symbol:<22} | {mid:>6.1f} | {idea.fair_value:>6.1f} | "
                    f"{str(bid or '-'):>6} | {str(ask or '-'):>6} | "
                    f"{be:>+8.1f} | {se:>+9.1f} | "
                    f"{idea.delta:>6.2f} | {idea.target_qty:>+7} | {sig}")
    return "\n".join(rows)


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 20 — LINEAR MM
# ═══════════════════════════════════════════════════════════════════════════

def _linear_mm_kf_init(price):
    return {"l": price, "s": 0.0, "p00": 400.0, "p01": 0.0, "p11": 100.0}

def _linear_mm_one_product(product, cfg, kf, kf_ticks, ob, pos, limit, log):
    orders = []; best_bid, best_ask = _best(ob); mid = _mid(ob)
    if mid is None: return orders, kf, kf_ticks
    kf = kf_step(kf, mid); kf_ticks += 1
    if kf_ticks < cfg["warmup"]: return orders, kf, kf_ticks
    fair = kf["l"]; take_edge = cfg["take_edge"]; max_pos = cfg["max_pos"]
    buy_cap = max_pos - pos; sell_cap = max_pos + pos
    buy_clip = cfg.get("buy_clip", cfg["clip"]); sell_clip = cfg.get("sell_clip", cfg["clip"])

    if best_ask is not None and best_ask < fair - take_edge and buy_cap > 0:
        qty = min(abs(ob.sell_orders[best_ask]), buy_clip, buy_cap)
        orders.append(Order(product, best_ask, +qty))
    if best_bid is not None and best_bid > fair + take_edge and sell_cap > 0:
        qty = min(ob.buy_orders[best_bid], sell_clip, sell_cap)
        orders.append(Order(product, best_bid, -qty))

    skew = pos * cfg["gamma"] * cfg["sigma"]**2 * cfg["T"]; res = round(fair - skew)
    buy_qe = cfg["quote_edge"]; sell_qe = cfg.get("sell_quote_edge", buy_qe)

    if best_bid is not None and best_ask is not None:
        bid_px = min(best_bid+1, res-buy_qe, best_ask-1)
        ask_px = max(best_ask-1, res+sell_qe, best_bid+1)
    elif best_bid is not None:
        bid_px = min(best_bid+1, res-buy_qe); ask_px = res+sell_qe
    elif best_ask is not None:
        bid_px = res-buy_qe; ask_px = max(best_ask-1, res+sell_qe)
    else:
        bid_px = res-buy_qe; ask_px = res+sell_qe
    if bid_px >= ask_px:
        bid_px = res - max(1, buy_qe); ask_px = res + max(1, sell_qe)

    repair_ratio = cfg["repair_ratio"]; pos_ratio = abs(pos) / max_pos if max_pos > 0 else 1.0
    in_repair = pos_ratio > repair_ratio
    slope_gate = cfg.get("slope_sell_gate"); kf_slope = kf.get("s", 0.0)
    slope_bearish = slope_gate is not None and kf_slope < slope_gate

    reversal_mode = False
    rev_thresh = cfg.get("reversal_pos_thresh"); rev_up = cfg.get("reversal_slope_up")
    rev_late_up = cfg.get("reversal_late_slope")
    if rev_thresh is not None and rev_up is not None and pos <= rev_thresh:
        eff_up = rev_late_up if (rev_late_up and kf_ticks >= int(NO_NEW_ENTRIES_AFTER/10)) else rev_up
        if kf_slope > eff_up:
            reversal_mode = True; buy_clip = cfg.get("reversal_buy_clip", buy_clip)

    allow_bid = (not in_repair or pos < 0) and not slope_bearish
    allow_ask = (not in_repair or pos > 0) and not reversal_mode
    inner_frac = cfg["quote_inner_frac"]

    if buy_cap > 0 and allow_bid:
        inner = max(1, int(buy_cap * inner_frac)); outer = buy_cap - inner
        orders.append(Order(product, bid_px, +inner))
        if outer > 0: orders.append(Order(product, bid_px-1, +outer))
    if sell_cap > 0 and allow_ask:
        inner = max(1, int(sell_cap * inner_frac)); outer = sell_cap - inner
        orders.append(Order(product, ask_px, -inner))
        if outer > 0: orders.append(Order(product, ask_px+1, -outer))

    return orders, kf, kf_ticks

def run_linear_mm(state, alpha_state, log):
    mm_state = alpha_state.get("linear_mm") or linear_mm_state_init(state)
    all_orders = {}
    enabled = {}
    if ENABLE_HYDROGEL_MM: enabled["HYDROGEL_PACK"] = LINEAR_MM_CFG["HYDROGEL_PACK"]
    if ENABLE_VEX_MM: enabled["VELVETFRUIT_EXTRACT"] = LINEAR_MM_CFG["VELVETFRUIT_EXTRACT"]
    for product, cfg in enabled.items():
        ob = state.order_depths.get(product); pos = state.position.get(product, 0)
        if ob is None: continue
        pst = mm_state.get(product) or {"kf": _linear_mm_kf_init(_mid(ob) or 5000.0), "ticks": 0}
        limit = POSITION_LIMITS[product]
        orders, updated_kf, updated_ticks = _linear_mm_one_product(
            product, cfg, pst["kf"], pst["ticks"], ob, pos, limit, log)
        mm_state[product] = {"kf": updated_kf, "ticks": updated_ticks}
        if orders: all_orders[product] = orders
    alpha_state["linear_mm"] = mm_state
    return all_orders, alpha_state


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 21 — TRADER CLASS
# ═══════════════════════════════════════════════════════════════════════════

class Trader:
    def __init__(self): self.tick = 0

    def run(self, state: TradingState):
        self.tick += 1
        try:
            alpha_state = json.loads(state.traderData) if state.traderData else {}
        except (json.JSONDecodeError, TypeError):
            alpha_state = {}
        if not alpha_state: alpha_state = alpha_state_init(state)

        do_log = (self.tick % 5 == 0)
        extract_mid = (_mid(state.order_depths["VELVETFRUIT_EXTRACT"])
                       if "VELVETFRUIT_EXTRACT" in state.order_depths else None)

        # Pipeline A: Linear MM (Hydrogel + VEX)
        linear_orders, alpha_state = run_linear_mm(state, alpha_state, log=do_log)

        # Pipeline B: Options — pure unhedged deep ITM + VEV_5300 short
        ideas, alpha_state = run_alpha(state, alpha_state, log=do_log)
        resolved = aggregate(ideas, state.position)
        resolved = apply_hard_limits(resolved, state.position)
        resolved = apply_delta_correction(
            resolved, state.position,
            extract_mid=extract_mid,
            net_delta_limit=NET_DELTA_LIMIT,   # 800 — allows full deep ITM exposure
            extract_limit=POSITION_LIMITS["VELVETFRUIT_EXTRACT"],
        )
        options_orders = execute_orders(resolved, state)

        if self.tick % 50 == 0:
            print(f"\n[r4_v20 TICK {self.tick}]")
            print(build_debug_table(resolved, state))
            pos4000 = state.position.get("VEV_4000", 0)
            pos4500 = state.position.get("VEV_4500", 0)
            print(f"[ITM] VEV_4000={pos4000} VEV_4500={pos4500} total_delta={pos4000+pos4500}")

        all_orders = {}
        for sym, ords in linear_orders.items(): all_orders[sym] = ords
        for sym, ords in options_orders.items():
            if sym == "VELVETFRUIT_EXTRACT" and sym in all_orders:
                print(f"[MERGE] VEX: delta-correction overrides MM"); all_orders[sym] = ords
            elif sym in all_orders: all_orders[sym] = all_orders[sym] + ords
            else: all_orders[sym] = ords

        return all_orders, 0, json.dumps(alpha_state)