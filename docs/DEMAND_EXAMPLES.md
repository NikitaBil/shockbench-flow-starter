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

For a Fab with `w_scr=2 weeks` and `tau=8 weeks`, a grounded output target
of 100 units requires 100 nominal input units under the current 1:1 BOM proxy.
`w_scr` is a scrap observation window, not a material loss rate, and `tau`
is a duration; neither scales nominal BOM quantities. With `e=0.002
GWh/output`, the target requires `100 * 0.002 = 0.2 GWh` energy. With the
associated grid's fuel share 0.5, the fuel target is `0.2 * 0.5 = 0.1 GWh`.
The Fab output target cannot exceed
published downstream package demand or observed effective Fab capacity over
the configured horizon. An OSAT with effective throughput 40, compatible
downstream demand 23, and 8 units of finished package inventory targets at
most 15 raw package units, not 40. Fab output inventory and OSAT raw input
inventory are netted before any wafer target is created.

## Indirect shortage damage

The optional `shortage_cost_model` is disabled by default. Sink demand uses
published `pi` in USD per native unit per weekly cost period. For an enabled
grid fuel estimate, the schema's VOLL is USD/GWh (not USD/MWh); fuel stock is
also GWh, and the simulator caps each generation segment by its fuel stock.
Thus one marginal GWh of fuel shortage can remove at most one GWh of
generation. The model's rate is `VOLL + max(pi * R / e)` across connected Fabs
reachable through a unit-compatible 1:1 BOM, where `pi` is USD/output/week,
`R` is dimensionless restoration, and `e` is GWh/output. Each term is therefore
USD/GWh/week. Fuel share limits the segment's generation cap and is not a
conversion ratio. Unknown energy units, Fab restoration, or a compatible
downstream penalty leave the indirect estimate unavailable. This is a
marginal proxy under the simulator's linear segment and Fab production
assumptions, not a calibrated purchase or social cost.

For a production input, the enabled model propagates the downstream sink `pi`
only along declared 1:1 links whose native units match. It returns no estimate
when that chain cannot be established. The allocator applies the resulting
USD/(input unit * week) rate to late input units and weeks. Confidence remains
`None`; the model is disabled until episode-level unit validation is complete.

To reproduce an episode's structured output, build the state and needs with
`StateBuilder` and `NeedPlanner`, then call
`NeedPlanner.export_examples(needs)`. Output contains priority rank and each
need's assumptions, provenance and cost.
