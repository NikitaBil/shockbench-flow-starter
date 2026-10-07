# PR: Validate VOLL dimensions, feature flags, and upstream timing

## Summary

This change adds regression coverage for the optional indirect shortage cost
model and its experiment configuration. VOLL remains disabled by default.

## Changes

- Enabled VOLL now raises `ValueError` when fuel or a 1:1 BOM link has
  incompatible or missing units, rather than silently producing or omitting a
  dimensionally invalid estimate.
- The `shortage_cost_model` flag must be a boolean. Paired enabled and disabled
  experiment presets share all other parameters so comparisons isolate the
  effect of the VOLL model.
- Tests check the grid-fuel formula against schema VOLL plus the maximum
  connected Fab marginal value, verify unit-mismatch failures, and confirm the
  model stays off by default.
- A deterministic upstream schedule test checks that demand week minus OSAT
  production lead, Fab-to-OSAT transit, and Fab production lead yields the
  expected Fab input order week and meets the OSAT input receipt SLA.

## Validation

Run the analytics, BOM timing, and delivery regression suites with:

```sh
uv run pytest tests/test_team_analytics.py tests/test_nominal_bom.py tests/test_delivery.py
```

The enabled preset is an experiment configuration, not a production default.
It should be evaluated only on instances whose unit mappings pass the strict
dimensional checks.
