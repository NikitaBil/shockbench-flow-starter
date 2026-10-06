# Demand calculation examples

These examples use native commodity units and absolute, 1-based weeks. All
assumptions are explicit and no confidence value is inferred.

## Sink demand

At week 1, published forecast is 12 units due week 1, observed stock is 4,
and a confirmed 3-unit arrival is due week 1. The sequential coverage is
`12 - 4 - 3 = 5` units, so the plan requests 5 units due week 1. If the
published `static.sinks.pi` is 80, marginal damage is
`80 USD / unit / week`. The arrival is counted once. Forecast demand is not
realized demand.

## Fab and OSAT BOM

For a Fab with `w_scr=2`, `tau=8`, `e=0.002 GWh/output`, a grounded output
target of 100 units requires `100 * (1 + 2/8) = 125` input units and
`100 * 0.002 = 0.2 GWh` energy. With the associated grid's fuel share 0.5,
the fuel target is `0.2 * 0.5 = 0.1 GWh`. The Fab output target cannot exceed
published downstream package demand or observed effective Fab capacity over
the configured horizon. An OSAT with effective throughput 40, compatible
downstream demand 23, and 8 units of finished package inventory targets at
most 15 raw package units, not 40. Fab output inventory and OSAT raw input
inventory are netted before any wafer target is created.

## Indirect shortage damage

For a grid with `VOLL=4,125,277.26 USD/MWh` and fuel share `0.5 GWh fuel / GWh
generation`, one GWh of fuel shortage is valued at
`4,125,277.26 * 0.5 * 1,000 = 2,062,638,630 USD/GWh fuel/week` under the explicit
assumption that the fuel shortfall reduces generation by the published share.
This is a marginal lost-service proxy; it is not the fuel's purchase price.
For other production inputs, the current documented fallback inherits the
largest sink penalty under a 1:1 BOM proxy. Neither proxy is calibrated, so
`confidence` remains `None`.

To reproduce an episode's structured output, build the state and needs with
`StateBuilder` and `NeedPlanner`, then call
`NeedPlanner.export_examples(needs)`. Output contains priority rank and each
need's assumptions, provenance and cost.
