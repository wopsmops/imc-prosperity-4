# Round 3 - Gloves Off

**Planet:** Solvenar  
**Phase:** Final (leaderboard reset, all teams start at 0)

## Algorithmic challenge

Three new products:

| Product | Position limit | Notes |
|---------|---------------|-------|
| `HYDROGEL_PACK` | 200 | Delta-1, similar to previous rounds |
| `VELVETFRUIT_EXTRACT` | 200 | Delta-1, underlying for VEV options |
| `VEV_4000` through `VEV_6500` | 300 each | European call options, 10 strikes |

**VEV vouchers** are call options on VELVETFRUIT_EXTRACT. Strike price is in the product name. They cannot be exercised early and all open positions are liquidated at fair value at round end.

Time to expiry (TTE) counts down one competition day per round. At the start of Round 3, TTE = 5 days. Each round represents one day, so by Round 7 (end) TTE = 0.

```
TTE at Round 3 start: 5 days
One competition day = 100,000 timestamp units
T in years = TTE_days / 252
```

## Manual challenge - The Celestial Gardeners' Guild

Trade ornamental bio-pods against counterparties with reserve prices uniformly distributed between 670 and 920 (increments of 5). Submit two bids:

- **Bid 1:** trades with any counterparty whose reserve price is below your bid
- **Bid 2:** trades if above reserve price AND above the mean of all teams' second bids; penalised if above reserve but below the mean

All acquired inventory is automatically sold at 920 after the round.
