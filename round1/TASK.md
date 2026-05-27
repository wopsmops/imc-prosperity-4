# Round 1 - Trading Groundwork

**Planet:** Intara  
**Phase:** Qualifier (target: 200,000 XIRECs net PnL before Round 3)

## Algorithmic challenge

Two products available:

| Product | Position limit | Notes |
|---------|---------------|-------|
| `ASH_COATED_OSMIUM` | 80 | Volatile, possible hidden pattern |
| `INTARIAN_PEPPER_ROOT` | 80 | Steady value, slow-growing |

Submitted as a single Python file implementing `class Trader` with a `run(state: TradingState)` method. State persisted between ticks via `traderData` string.

## Manual challenge - Exchange Auction

Two one-time auctions for `DRYLAND_FLAX` and `EMBER_MUSHROOM`. Submit a single limit order (price, quantity) for each. The exchange clears at the price that maximises total traded volume. All filled inventory is bought back at a fixed price immediately after:

- `DRYLAND_FLAX`: buyback at 30 per unit (no fees)
- `EMBER_MUSHROOM`: buyback at 20 per unit (fee: 0.10 per unit traded)

Submitted last, so last in queue at any price level joined.
