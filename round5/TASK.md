# Round 5 - The Final Stretch

**Planet:** Solvenar  
**Phase:** Final

## Algorithmic challenge

50 new products replace all previous ones. Products are split across 10 categories of 5, each with a position limit of 10:

| Category | Products |
|----------|----------|
| Galaxy Sounds Recorders | `GALAXY_SOUNDS_DARK_MATTER`, `_BLACK_HOLES`, `_PLANETARY_RINGS`, `_SOLAR_WINDS`, `_SOLAR_FLAMES` |
| Vertical Sleeping Pods | `SLEEP_POD_SUEDE`, `_LAMB_WOOL`, `_POLYESTER`, `_NYLON`, `_COTTON` |
| Organic Microchips | `MICROCHIP_CIRCLE`, `_OVAL`, `_SQUARE`, `_RECTANGLE`, `_TRIANGLE` |
| Purification Pebbles | `PEBBLES_XS`, `_S`, `_M`, `_L`, `_XL` |
| Domestic Robots | `ROBOT_VACUUMING`, `_MOPPING`, `_DISHES`, `_LAUNDRY`, `_IRONING` |
| UV-Visors | `UV_VISOR_YELLOW`, `_AMBER`, `_ORANGE`, `_RED`, `_MAGENTA` |
| Instant Translators | `TRANSLATOR_SPACE_GRAY`, `_ASTRO_BLACK`, `_ECLIPSE_CHARCOAL`, `_GRAPHITE_MIST`, `_VOID_BLUE` |
| Construction Panels | `PANEL_1X2`, `_2X2`, `_1X4`, `_2X4`, `_4X4` |
| Liquid Breath Oxygen Shakes | `OXYGEN_SHAKE_MORNING_BREATH`, `_EVENING_BREATH`, `_MINT`, `_CHOCOLATE`, `_GARLIC` |
| Protein Snack Packs | `SNACKPACK_CHOCOLATE`, `_VANILLA`, `_PISTACHIO`, `_STRAWBERRY`, `_RASPBERRY` |

Each category has its own market dynamics. Some have strong embedded patterns; others are closer to noise. Teams were expected to identify which categories offered exploitable inefficiencies.

Products from previous rounds cannot be traded in Round 5.

## Manual challenge - Ashflow Alpha (Ignith exchange)

One-day trade on the Ignith exchange using news from the Ashflow Alpha source. 9 goods available. Budget: 1,000,000 XIRECs.

Fee structure: `fee = (volume / 100)^2 * budget` per product. Higher allocation to a single product increases its cost non-linearly.

Returns are partially endogenous: high collective buying pressure on a product raises its actual return within a defined range. Teams had to assess news sentiment, filter already-priced-in information, and account for other teams' likely positions.
