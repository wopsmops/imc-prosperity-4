# Round 2 - Growing Your Outpost

**Planet:** Intara  
**Phase:** Qualifier (final round before leaderboard reset)

## Algorithmic challenge

Same products as Round 1:

| Product | Position limit |
|---------|---------------|
| `ASH_COATED_OSMIUM` | 80 |
| `INTARIAN_PEPPER_ROOT` | 80 |

**New mechanic - Market Access Fee (MAF):** Bidding for 25% extra order book flow. The top 50% of bids across all teams are accepted. Accepted teams receive additional quotes that fit naturally into the existing order book depth. The fee is a one-time deduction from Round 2 profit:

```
profit = round_2_pnl - bid   (if bid accepted, i.e. bid >= median)
profit = round_2_pnl         (if bid rejected)
```

Implemented by adding a `bid()` method to `class Trader`:
```python
class Trader:
    def bid(self):
        return <amount>
    def run(self, state):
        ...
```

Bids are only compared at final simulation, not during local testing. Negative bids are treated as zero.

## Manual challenge - Invest & Expand

Allocate 50,000 XIRECs across three growth pillars (percentages, must sum to <= 100%):

| Pillar | Formula | Range |
|--------|---------|-------|
| Research | `200,000 * ln(1 + x) / ln(101)` | 0 to 200,000 (logarithmic) |
| Scale | `7 * x / 100` | 0 to 7 (linear) |
| Speed | rank-based multiplier | 0.1 to 0.9 |

Speed is competitive: highest investor gets 0.9, lowest gets 0.1, rest scaled linearly by rank.

**Scoring:** `PnL = Research * Scale * Speed - budget_used`
