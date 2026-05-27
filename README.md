# IMC Prosperity 4 - GoldmanSnacks

[Evgenii Kondratenko](https://github.com/k-evgenii) · [Artem Veklich](https://veklich.com)

IMC Prosperity 4 is a global algorithmic trading competition run by IMC Trading. Teams write Python bots to trade fictional assets on a simulated exchange across 5 rounds, with each round introducing new products and market mechanics.

**#2119 / 18,803 teams** | **#225 in the UK** | 82,576 XIRECs total  
`#2900` algorithmic | `#1522` manual | 30,703 participants | 117 countries

---

## Rounds

| Round | Products | Approach |
|-------|----------|----------|
| 1 | `ASH_COATED_OSMIUM`, `INTARIAN_PEPPER_ROOT` | Kalman filter market maker + directional long |
| 2 | Same + Market Access Fee | Drift-adjusted MM + stateful entry model |
| 3 | `HYDROGEL_PACK`, `VELVETFRUIT_EXTRACT`, 10x VEV options | Linear MM + Black-Scholes options pricing |
| 4 | Same + counterparty IDs | Opening-rush option shorts + profit lock |
| 5 | 50 products across 10 categories | Multi-strategy statistical arbitrage |

Rounds 1-2 were qualifiers (threshold: 200,000 XIRECs). Rounds 3-5 were a fresh-start final phase.

---

## Round 1 - Kalman Market Maker

**Products:** `ASH_COATED_OSMIUM` | `INTARIAN_PEPPER_ROOT` | [Round brief](round1/TASK.md)

Two independent strategies running in the same bot.

**Ash-Coated Osmium** used a two-state Kalman filter (level + slope) blended with a structural anchor at 10,000 to estimate fair value. Passive quotes were placed in two levels (65% inner / 35% outer) around an Avellaneda-Stoikov reservation price with inventory skew. When mid drifted above fair, quotes shifted down a tick to lean toward selling, and vice versa. Taking fired when the best available price crossed fair by more than one tick.

**Intarian Pepper Root** was treated as a directional long. The bot swept to the full position limit at market before timestamp 10,000 and held for the rest of the round. Pepper had a clear upward drift that made market-making less attractive than a simple carry position.

---

## Round 2 - Drift-Adjusted Market Maker

**Products:** Same as Round 1 + Market Access Fee | [Round brief](round2/TASK.md)

A meaningful evolution of Round 1 rather than a rebuild.

**Osmium** fair value now incorporated Kalman slope directly:
```
fv = 10000 + 0.65 * ((kf_level - 10000) + 0.06 * kf_slope)
```
Taking became edge-scaled: small edge (< 2.4 ticks) used 10% of available capacity, medium 85%, large 100%. This stopped the bot burning position on borderline signals.

**Pepper Root** gained a stateful entry model. Rather than always hitting the best ask, the bot tracked best-ask size EMA and ask-stack ratio each tick and chose an execution route (take / improve / join) based on current book conditions.

**Market Access Fee:** Round 2 included a blind auction where the top 50% of bids received 25% more order book flow. The bid was set at 1,756 XIRECs based on estimated incremental flow value and likely clustering around round numbers.

---

## Round 3 - Black-Scholes Options Pricer

**Products:** `HYDROGEL_PACK` | `VELVETFRUIT_EXTRACT` | `VEV_4000` through `VEV_6500` | [Round brief](round3/TASK.md)

Two fully independent pipelines in one bot.

### Pipeline A - Linear market making

Kalman-smoothed market making on Hydrogel and Velvetfruit Extract, calibrated separately for each product. Sell clips were intentionally larger than buy clips based on markout analysis showing the buy side lost edge faster. A slope gate suppressed new bids when Hydrogel was trending down. A short-reversal repair module kicked in when a short position coincided with slope turning bullish.

### Pipeline B - Black-Scholes options pricing

VEV vouchers are European call options on VELVETFRUIT_EXTRACT. Fair value was calculated using Black-Scholes with zero risk-free rate:

```
C = S*N(d1) - K*N(d2)
d1 = [ln(S/K) + 0.5*sigma^2*T] / (sigma*sqrt(T))
d2 = d1 - sigma*sqrt(T)
```

Implied volatility was estimated live from a rolling window of log-returns on the underlying and annualised. A bisection IV solver inverted market prices back to implied vol for cross-strike comparison.

The main signal was a **VEV_5300 rich-fade**: short when market price exceeded Black-Scholes fair value by 6 or more ticks, exit when the gap closed below 1.5 ticks. Net portfolio delta was tracked across all VEV positions and hedged back toward zero using VELVETFRUIT_EXTRACT.

A `TradeIdea` abstraction unified signals across modules, each carrying a target position, edge estimate, confidence, and BS delta. A priority-weighted aggregator resolved conflicts before execution.

See [round3/options_pricer.py](round3/options_pricer.py) for a standalone implementation of the Black-Scholes pricer, Greeks, and IV solver.

Passive market-making diagnostics for each strike are in [round3/passive_mm_graphs/](round3/passive_mm_graphs/).

---

## Round 4 - Opening-Rush Option Shorts

**Products:** Same as Round 3 + counterparty IDs in trade history | [Round brief](round4/TASK.md)

Historical data showed VEV options at strikes 5100-5500 were consistently overpriced relative to Black-Scholes fair value at session open. The strategy removed the signal cooldown for the first ~15,000 timestamps so the bot could short those strikes as fast as possible at open, then hold to liquidation.

A profit lock was added on top: the bot tracked a rolling minimum of VELVETFRUIT_EXTRACT over the last 50,000 timestamps and urgently exited all option shorts if the underlying rebounded above a threshold after timestamp 80,000. This was designed to protect gains from the late-session giveback pattern observed in backtesting.

---

## Round 5 - Multi-Strategy Statistical Arbitrage

**Products:** 50 new products across 10 categories (position limit: 10 per product) | [Round brief](round5/TASK.md)

With 50 products and tight position limits, the approach was to systematically scan all categories in research notebooks, find structural relationships, and merge the best signals into one final submission. Research notebooks for each category are in [round5/](round5/).

Six strategies ran concurrently in the final bot:

**Pebbles (5-leg basket)** - The five pebble sizes summed to a structural fair value near 50,000. A volume-weighted rolling mean (window 500) was blended 90/10 with this anchor. Z-score entry on basket deviation, market-taking only, symmetric long and short.

**UV Visors (lead-lag)** - UV_VISOR_RED consistently led UV_VISOR_YELLOW by around 250 ticks. The bot scored lagged RED moves with a z-score and traded YELLOW inversely above 2.25 standard deviations.

**Microchips (relative value)** - Signal: log(TRIANGLE / avg(SQUARE, RECTANGLE)). Long TRIANGLE / short SQUARE+RECTANGLE at z > 2.5. Exits were aggressive after passive exits showed significant losses in testing.

**Sleep Pods (materials spread)** - Synthetic materials (POLYESTER, NYLON) versus natural materials (SUEDE, LAMB WOOL, COTTON). Z-score mean reversion, entry at 1.25 standard deviations.

**Panels (pair basket)** - PANEL_2X4 + PANEL_4X4 joint basket, z-score mean reversion at 1.5 standard deviations.

**Snackpacks** - Signal: CHOCOLATE + VANILLA - RASPBERRY. Traded VANILLA short and RASPBERRY long on high z-scores. CHOCOLATE was kept as a signal-only input after its short side proved unreliable.

State was persisted across ticks using `jsonpickle`. All positions force-flattened at timestamp 995,000.

---

## Repository structure

```
imc-prosperity-4/
├── README.md
├── datamodel.py                    # IMC type definitions
├── explore_0.ipynb                 # VEV options research (Round 3)
│
├── round1/
│   ├── TASK.md
│   └── trader.py
├── round2/
│   ├── TASK.md
│   └── trader.py
├── round3/
│   ├── TASK.md
│   ├── trader.py
│   ├── options_pricer.py
│   └── passive_mm_graphs/          # Per-strike option diagnostics (12 plots)
├── round4/
│   ├── TASK.md
│   └── trader.py
└── round5/
    ├── TASK.md
    ├── trader.py
    └── *.ipynb                     # Per-category research notebooks
```

---

## Stack

**Submissions:** Python only (no external libraries allowed in IMC bots)

**Algorithms:** Kalman filter, Avellaneda-Stoikov inventory model, Black-Scholes pricing, bisection IV solver, rolling volatility estimator, z-score mean reversion, lead-lag momentum

**Research:** pandas, numpy, jupyter, prosperity4btest (local backtester), statsmodels
