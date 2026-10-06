# V2 — current network and data quality

Vitya's network/delivery track. `NetworkTracker` extends the V1 helper module;
it does not change the Agent, forecast demand, read warnings or assign flows.

```python
network = StaticNetwork(config)  # once per episode
tracker = NetworkTracker(config, network, max_history_age=4)
snapshot = tracker.update(observation)  # once per week, in increasing order
status = snapshot.routes[slot_id]
capacity = snapshot.fields["graph_now.u"]
```

## Observation fields

The tracker reads `week`, `action_mask` and graph_now fields `u`, `c`, `tau`,
`prohibited`, `tariff`, `open`, `kappa.tb`, `kappa.ct`, `war_risk`, with their
observed masks. `action_mask.observed` is a single flag, not a per-slot array.
Shapes come from config. StaticNetwork's public API is unchanged.

Each field provides read-only arrays `values`, `observed`, `source`,
`age_weeks` and `nominal`. Nominal and current/estimated values are separate.
Hidden input values, including zeroes, are ignored. Invalid observed values
raise an explicit error without partially updating history.

| DataSource | Meaning |
| --- | --- |
| CURRENT | Value observed this week |
| HISTORY | Most recent observation, within max_history_age weeks |
| NOMINAL | Public nominal parameter or labelled reset assumption |
| DERIVED | Hidden kappa computed from nominal k_c*mu and resolved openness |

`observed` stays false for all estimates. `age_weeks=-1` means no remembered
observation is being used. Grid coupling capacities are structurally undefined:
their nominal `u` is NaN; no cargo action route contains such an edge.

## Blackout policy

Default assumption: retain the most recent observed value for up to four weeks,
then return to the nominal estimate. The limit is configurable and needs team
validation, not interpreted as proof that a disruption ends after four weeks.
Reset prohibitions initialize the nominal sanction matrix. Zero tariff,
openness one and war-risk class zero are nominal assumptions, not observations.
No future announcements or hidden environment data are used.

Visible action_mask or fully visible prohibition rows take precedence over stale
history. A visible prohibition on any route edge blocks that route. An all-valid
blackout action_mask with observed=0 does not establish legal availability.

## Independent constraints

RouteStatus exposes:

- `sanction_allowed` and `permission_observed`: estimated/current legal
  permission, separate from physical capacity;
- `entry_capacity`: capacity of the action's first edge, before resource sharing;
- `snapshot_throughput`: minimum of all route edges and relevant chokepoint
  pool capacities under this snapshot;
- `zero_capacity_edges`, `closed_chokepoints`: separate physical obstacles;
- `uncertain_fields`: route-relevant fields using an estimate anywhere.

The path throughput is a planning estimate, not a guaranteed reservation or
executed quantity. A downstream closure does not make the entry action illegal
and may change before cargo reaches it. No volume is assigned by this module.

graph_now.kappa already equals `(k_c * mu_pool) * open` in the benchmark.
Observed kappa is used directly, never multiplied by open again. Hidden kappa
is derived using that same public formula, with an explicit quality label.

## Verification

```text
uv run pytest tests/test_current_network.py tests/test_network.py -q
```

Focused cases cover downstream sanctions, legal dispatch into a currently closed
lane, fractional openness without double scaling, blackout memory expiry,
visible-mask precedence, derived capacity, immutable snapshots and malformed
observed values. V1's real Tiny/Small/Full mapping tests are retained.

V2 changes network estimates only. After Agent integration, freeze the prior
version and use paired `sbf compare` on Small and Full, enabling only the current
constraint response; inspect requested/executed clipping and CPU as well as RSS.
Until then, module tests and native snapshot runs do not establish policy benefit.
