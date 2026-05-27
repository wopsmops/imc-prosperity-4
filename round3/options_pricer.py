"""
options_pricer.py  ·  Round 3  ·  GoldmanSnacks
────────────────────────────────────────────────────────────────────────────
Standalone Black-Scholes pricing module for VEV (Velvetfruit Extract Voucher)
call options, as used in the Round 3 trading bot.

Exports
-------
bs_call(S, K, T, sigma)             → theoretical call price
bs_put(S, K, T, sigma)              → theoretical put price  
bs_delta(S, K, T, sigma)            → option delta  ∈ [0, 1]
bs_gamma(S, K, T, sigma)            → option gamma
bs_vega(S, K, T, sigma)             → option vega
implied_vol(market_price, S, K, T)  → implied volatility (bisection)
annualised_vol(prices, ticks_per_day) → rolling log-return vol estimator

Competition context
-------------------
- Underlying:  VELVETFRUIT_EXTRACT  (S ≈ 5000–5500 in historical data)
- Strikes:     4000, 4500, 5000, 5100, 5200, 5300, 5400, 5500, 6000, 6500
- Style:       European calls (no early exercise; positions liquidated at round end)
- Risk-free:   r = 0  (competition setup; no discounting term)
- TTE Round 3: 5 competition days → T = 5/252 years
────────────────────────────────────────────────────────────────────────────
"""

import math
from typing import Optional, List


# ── Constants ────────────────────────────────────────────────────────────────

TRADING_DAYS_PER_YEAR: int   = 252
TICKS_PER_DAY:         int   = 10_000   # Prosperity timestamp rows per competition day
VOL_WINDOW:            int   = 200      # rolling window for vol estimation (ticks)

STRIKES: List[int] = [4000, 4500, 5000, 5100, 5200, 5300, 5400, 5500, 6000, 6500]


# ── Math primitives ───────────────────────────────────────────────────────────

def _ncdf(x: float) -> float:
    """Standard normal CDF via math.erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _npdf(x: float) -> float:
    """Standard normal PDF."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


# ── Black-Scholes pricing ─────────────────────────────────────────────────────

def bs_call(S: float, K: float, T: float, sigma: float) -> float:
    """
    Black-Scholes European call price (r = 0).

    Parameters
    ----------
    S     : current underlying price
    K     : strike price
    T     : time to expiry in years  (e.g. 5/252 for 5 trading days)
    sigma : annualised log-return volatility  (e.g. 0.30 for 30%)

    Returns
    -------
    Theoretical call price. Falls back to intrinsic max(S-K, 0) at expiry.
    """
    if T <= 0.0 or sigma <= 0.0:
        return max(S - K, 0.0)
    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * sigma ** 2 * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    return S * _ncdf(d1) - K * _ncdf(d2)


def bs_put(S: float, K: float, T: float, sigma: float) -> float:
    """
    Black-Scholes European put price via put-call parity (r = 0).

    C - P = S - K  →  P = C - S + K
    """
    return bs_call(S, K, T, sigma) - S + K


def bs_delta(S: float, K: float, T: float, sigma: float) -> float:
    """
    Black-Scholes call delta  ∂C/∂S = N(d₁).

    Used for hedging: a position of -n calls requires +n×delta units
    of the underlying to be delta-neutral.
    """
    if T <= 0.0 or sigma <= 0.0:
        return 1.0 if S > K else 0.0
    d1 = (math.log(S / K) + 0.5 * sigma ** 2 * T) / (sigma * math.sqrt(T))
    return _ncdf(d1)


def bs_gamma(S: float, K: float, T: float, sigma: float) -> float:
    """
    Black-Scholes gamma  ∂²C/∂S² = N'(d₁) / (S·σ·√T).

    Measures rate of delta change with underlying price.
    """
    if T <= 0.0 or sigma <= 0.0 or S <= 0.0:
        return 0.0
    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * sigma ** 2 * T) / (sigma * sqrt_T)
    return _npdf(d1) / (S * sigma * sqrt_T)


def bs_vega(S: float, K: float, T: float, sigma: float) -> float:
    """
    Black-Scholes vega  ∂C/∂σ = S·√T·N'(d₁).

    Measures call price sensitivity to a 1-unit change in sigma.
    """
    if T <= 0.0 or sigma <= 0.0:
        return 0.0
    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * sigma ** 2 * T) / (sigma * sqrt_T)
    return S * sqrt_T * _npdf(d1)


# ── Implied volatility ────────────────────────────────────────────────────────

def implied_vol(
    market_price: float,
    S: float,
    K: float,
    T: float,
    lo: float = 1e-6,
    hi: float = 20.0,
    tol: float = 1e-6,
    max_iter: int = 100,
) -> Optional[float]:
    """
    Implied volatility via bisection on the BS call price.

    Inverts bs_call(S, K, T, sigma) = market_price to find sigma.

    Returns None if the market price violates no-arbitrage bounds
    (i.e. market_price < intrinsic value).
    """
    intrinsic = max(S - K, 0.0)
    if T <= 0.0 or market_price < intrinsic - tol:
        return None

    for _ in range(max_iter):
        mid_vol = (lo + hi) * 0.5
        price   = bs_call(S, K, T, mid_vol)
        if abs(price - market_price) < tol:
            return mid_vol
        if price < market_price:
            lo = mid_vol
        else:
            hi = mid_vol

    return (lo + hi) * 0.5


# ── Volatility estimation ─────────────────────────────────────────────────────

def annualised_vol(
    prices: List[float],
    ticks_per_day: int = TICKS_PER_DAY,
) -> Optional[float]:
    """
    Estimate annualised log-return volatility from a price history.

    Computes per-tick log-return std, then scales to annual:
        sigma_annual = sigma_tick × √(ticks_per_day × TRADING_DAYS_PER_YEAR)

    Parameters
    ----------
    prices        : recent price observations (most recent last)
    ticks_per_day : number of observations per competition day

    Returns
    -------
    Annualised volatility, or None if fewer than 3 prices supplied.
    """
    n = len(prices)
    if n < 3:
        return None

    log_returns = [
        math.log(prices[i] / prices[i - 1])
        for i in range(1, n)
        if prices[i] > 0 and prices[i - 1] > 0
    ]
    if len(log_returns) < 2:
        return None

    mean = sum(log_returns) / len(log_returns)
    variance = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
    sigma_tick = math.sqrt(variance)

    ticks_per_year = ticks_per_day * TRADING_DAYS_PER_YEAR
    return sigma_tick * math.sqrt(ticks_per_year)


def tte_to_years(tte_days: float) -> float:
    """Convert time-to-expiry in competition days to fraction of a trading year."""
    return tte_days / TRADING_DAYS_PER_YEAR


# ── Example usage ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Round 3 parameters
    S     = 5250.0    # VELVETFRUIT_EXTRACT mid
    T     = tte_to_years(5)  # TTE = 5 competition days at start of Round 3
    sigma = 0.82      # typical annualised vol from historical data (~82%)

    print(f"Underlying: S={S},  T={T:.4f} years,  σ={sigma:.0%}\n")
    print(f"{'Strike':>8}  {'BS Fair':>10}  {'Delta':>7}  {'IV check':>10}")
    print("-" * 44)

    for K in STRIKES:
        fair  = bs_call(S, K, T, sigma)
        delta = bs_delta(S, K, T, sigma)
        # Round-trip: recover sigma from fair price
        iv    = implied_vol(fair, S, K, T)
        iv_str = f"{iv:.4f}" if iv is not None else "  N/A"
        print(f"{K:>8}  {fair:>10.2f}  {delta:>7.4f}  {iv_str:>10}")

    print("\nVEV_5300 rich-fade example:")
    K_target    = 5300
    market_ask  = 62.0   # hypothetical market ask
    fair_5300   = bs_call(S, K_target, T, sigma)
    gap         = market_ask - fair_5300
    print(f"  BS fair = {fair_5300:.2f},  market ask = {market_ask},  gap = {gap:+.2f}")
    if gap > 6.0:
        print("  → Signal: VEV_5300 RICH — enter short")
    else:
        print("  → No signal")
