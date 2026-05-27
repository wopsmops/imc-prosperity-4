# Round 4 - The More The Merrier

**Planet:** Solvenar  
**Phase:** Final

## Algorithmic challenge

Same products as Round 3. Position limits unchanged.

**New mechanic - counterparty visibility:** The `buyer` and `seller` fields on the `Trade` class are now populated with participant IDs. Previously these were always `None`. Teams can use this information to study the behaviour of specific market participants and adjust strategy accordingly.

```python
class Trade:
    symbol:    str
    price:     int
    quantity:  int
    buyer:     str | None   # now populated
    seller:    str | None   # now populated
    timestamp: int
```

TTE at Round 4 start: 4 days.

## Manual challenge - Exotic Options on Aether Crystal

One-time manual portfolio on `AETHER_CRYSTAL`. Products available:

| Product | Type |
|---------|------|
| `AETHER_CRYSTAL` | Underlying (GBM, sigma=251% annualised, r=0) |
| 2-week call / put | Vanilla European |
| 3-week call / put | Vanilla European |
| Chooser option (3-week) | After 2 weeks, holder chooses call or put |
| Binary put | Fixed payoff if underlying below strike at expiry |
| Knock-out put | Standard put that becomes worthless if underlying breaches barrier |

Positions are held to expiry (no intraday trading). PnL is marked against the average fair value across 100 simulations of the underlying. Contract size is 3,000 (PnL multiplier). Underlying follows discrete GBM at 4 steps per trading day, 252 days per year. Knock-out barrier is checked only at discrete steps, not continuously.
