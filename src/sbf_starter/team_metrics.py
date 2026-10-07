"""Read-only metrics from completed comparisons and public diagnostic traces.

No policy loading, scenario generation, hidden state or score recomputation.
"""

import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean


def percentile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    position = (len(values) - 1) * fraction
    low = math.floor(position)
    high = math.ceil(position)
    return values[low] + (values[high] - values[low]) * (position - low)


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def trace_metrics(records, constructor_cpu=0.0):
    """Aggregate per-week records. Need volumes are repeated planner requests.

    They are not unique external demand or confirmed deliveries. Unmet reasons
    can overlap, so their volumes cannot be added to obtain a total deficit.
    """
    records = list(records)
    quantities, nodes, resources = defaultdict(Counter), {}, {}
    transport, shed_nodes = (
        defaultdict(
            lambda: {
                "samples": [],
                "unknown_quantity_records": 0,
                "unknown_timing_records": 0,
                "quantity_source_counts": Counter(),
            }
        ),
        defaultdict(list),
    )
    reasons, unmet_reasons = Counter(), Counter()
    issue_counts = Counter()
    costs, costs_coverage = Counter(), Counter()
    stages = defaultdict(list)
    cpu = []
    unlinked_unmet = 0
    weeks_with_outcomes = 0
    costs_complete = bool(records)
    for record in records:
        cpu.append(record["act_cpu_seconds"])
        for stage, seconds in (record.get("stage_cpu_seconds") or {}).items():
            stages[stage].append(seconds)
        state = record.get("state") or {}
        issue_counts.update(state.get("issues", []))
        issue_counts.update(record.get("planner_issues", []))
        for field in ("available_stock", "backlog"):
            for row in state.get(field, []):
                key = (row["node"], row["commodity"], row["unit"], field)
                aggregate = nodes.setdefault(key, {"values": [], "unknown": 0, "estimated": 0})
                quantity = row["quantity"]
                if quantity["value"] is None:
                    aggregate["unknown"] += 1
                else:
                    aggregate["values"].append(quantity["value"])
                    aggregate["estimated"] += int(quantity["source"] == "estimated")
        needs = {need["need_id"]: need for need in record.get("needs") or []}
        # Resolve commodity labels from state/flows without parsing human messages.
        labels = {
            row["commodity_id"]: (row["commodity"], row["unit"])
            for field in ("available_stock", "backlog")
            for row in state.get(field, [])
        }
        labels.update(
            {row["commodity_id"]: (row["commodity"], row["unit"]) for row in record.get("requested_flows", [])}
        )
        for field in ("pipeline", "queues", "arrivals"):
            weekly = Counter()
            for lot in state.get(field, []):
                commodity, unit = labels.get(lot["commodity_id"], (str(lot["commodity_id"]), "unknown"))
                aggregate = transport[field, commodity, unit]
                aggregate["quantity_source_counts"].update([lot["quantity"]["source"]])
                quantity = lot["quantity"]["value"]
                if quantity is None:
                    aggregate["unknown_quantity_records"] += 1
                else:
                    weekly[field, commodity, unit] += quantity
                if field == "arrivals" and lot.get("arrival_week") is None:
                    aggregate["unknown_timing_records"] += 1
            for key, value in weekly.items():
                transport[key]["samples"].append(value)
        for need in needs.values():
            key = labels.get(need["commodity_id"], (str(need["commodity_id"]), "unknown"))
            quantities[key]["planned_need_quantity"] += need["quantity"]
            quantities[key]["need_records"] += 1
            if need["due_week"] < record["week"]:
                quantities[key]["overdue_need_quantity"] += need["quantity"]
        allocation = record.get("allocation") or {}
        reasons.update(item["code"] for item in allocation.get("reasons", []))
        for item in allocation.get("unmet_needs", []):
            need = needs.get(item["need_id"])
            unmet_reasons.update(set(item["reason"].split(",")))
            if need is None:
                unlinked_unmet += 1
                continue
            key = labels.get(need["commodity_id"], (str(need["commodity_id"]), "unknown"))
            quantities[key]["unmet_planned_quantity"] += item["remaining_quantity"]
            for code in set(item["reason"].split(",")):
                quantities[key][f"unmet_reason_quantity/{code}"] += item["remaining_quantity"]
        for item in allocation.get("resource_usage", []):
            key = (item["kind"], item["resource_index"], item.get("pool"), item["unit"])
            aggregate = resources.setdefault(
                key, {"utilizations": [], "unknown_limit_records": 0, "over_limit_records": 0, "zero_limit_records": 0}
            )
            limit = item["limit"]
            if limit is None:
                aggregate["unknown_limit_records"] += 1
            elif limit > 0:
                aggregate["utilizations"].append(item["used"] / limit)
                aggregate["over_limit_records"] += int(item["used"] > limit + 1e-9 * max(1.0, limit))
            else:
                aggregate["zero_limit_records"] += 1
                aggregate["over_limit_records"] += int(item["used"] > 1e-9)
        for flow in record.get("requested_flows", []):
            quantities[flow["commodity"], flow["unit"]]["requested_quantity"] += flow["requested_quantity"]
        outcomes = record.get("outcomes")
        if outcomes is None:
            costs_complete = False
            continue
        weeks_with_outcomes += 1
        for row in outcomes.get("shed_gwh", []):
            if row["quantity"] is not None:
                shed_nodes[row["node"]].append(row["quantity"])
        for name, value in outcomes["cost_components_usd"].items():
            if value is not None:
                costs[name] += value
                costs_coverage[name] += 1
            else:
                costs_complete = False
        if not outcomes["cost_components_usd"]:
            costs_complete = False
        for flow in outcomes["executed_flows"]:
            request, executed = flow["requested_quantity"], flow["executed_quantity"]
            aggregate = quantities[flow["commodity"], flow["unit"]]
            if request is not None and executed is not None:
                aggregate["paired_clip_requested_quantity"] += request
                aggregate["executed_quantity"] += executed
                aggregate["clipped_quantity"] += max(0.0, request - executed)
                aggregate["observed_clip_slot_weeks"] += 1
        for sink in outcomes["sinks"]:
            aggregate = quantities[sink["commodity"], sink["unit"]]
            if sink["demand"] is not None and sink["served"] is not None:
                aggregate["observed_demand_quantity"] += sink["demand"]
                aggregate["observed_served_quantity"] += sink["served"]
                aggregate["observed_sink_records"] += 1
            if sink["lost"] is not None:
                aggregate["observed_lost_quantity"] += sink["lost"]
                aggregate["observed_lost_records"] += 1
    commodity_rows = []
    for (commodity, unit), values in sorted(quantities.items()):
        commodity_rows.append(
            {
                "commodity": commodity,
                "unit": unit,
                **values,
                "planned_unmet_fraction": ratio(values["unmet_planned_quantity"], values["planned_need_quantity"]),
                "execution_fraction": ratio(values["executed_quantity"], values["paired_clip_requested_quantity"]),
                "demand_service_fraction": ratio(
                    values["observed_served_quantity"], values["observed_demand_quantity"]
                ),
            }
        )
    resource_rows = []
    for key, aggregate in resources.items():
        utilization = aggregate.pop("utilizations")
        resource_rows.append(
            dict(zip(("kind", "index", "pool", "unit"), key))
            | aggregate
            | {
                "reported_records_with_positive_limit": len(utilization),
                "mean_utilization": mean(utilization) if utilization else None,
                "max_utilization": max(utilization) if utilization else None,
                "saturated_records": sum(value >= 0.99 for value in utilization),
            }
        )
    allocated = reasons["allocated_current_resources"]
    node_rows = [
        dict(zip(("node", "commodity", "unit", "field"), key))
        | {
            "mean_known_quantity": mean(value["values"]) if value["values"] else None,
            "last_known_quantity": value["values"][-1] if value["values"] else None,
            "max_known_quantity": max(value["values"]) if value["values"] else None,
            "known_records": len(value["values"]),
            "unknown_records": value["unknown"],
            "estimated_records": value["estimated"],
        }
        for key, value in sorted(nodes.items())
    ]
    stage_rows = {
        name: {"total_seconds": sum(values), "p95_seconds": percentile(values, 0.95), "max_seconds": max(values)}
        for name, values in stages.items()
    }
    all_costs_known = (
        costs_complete
        and weeks_with_outcomes == len(records)
        and all(count == len(records) for count in costs_coverage.values())
        and bool(costs_coverage)
    )
    salvage = sum(record.get("salvage_usd", 0.0) for record in records)
    cost = sum(record["step_cost_usd"] for record in records)
    return {
        "weeks": len(records),
        "cost_usd": cost,
        "outcome_weeks": weeks_with_outcomes,
        "cost_components_usd": dict(costs) if costs_coverage else None,
        "cost_component_observed_weeks": dict(costs_coverage),
        "cost_reconciliation_residual_usd": sum(costs.values()) - salvage - cost if all_costs_known else None,
        "terminal_salvage_usd": salvage if weeks_with_outcomes else None,
        "commodity_metrics": commodity_rows,
        "node_metrics": node_rows,
        "resource_metrics": resource_rows,
        "transport_metrics": [
            {
                "block": key[0],
                "commodity": key[1],
                "unit": key[2],
                "mean_quantity_on_known_lot_weeks": mean(value["samples"]) if value["samples"] else None,
                "peak_quantity": max(value["samples"]) if value["samples"] else None,
                "known_lot_week_records": len(value["samples"]),
                "unknown_quantity_records": value["unknown_quantity_records"],
                "unknown_timing_records": value["unknown_timing_records"],
                "quantity_source_counts": dict(value["quantity_source_counts"]),
            }
            for key, value in sorted(transport.items())
        ],
        "shed_by_grid": [
            {"node": node, "observed_shed_gwh": sum(values), "observed_weeks": len(values)}
            for node, values in sorted(shed_nodes.items())
        ],
        "reason_counts": dict(reasons),
        "unmet_reason_counts": dict(unmet_reasons),
        "unlinked_unmet_records": unlinked_unmet,
        "issue_counts": dict(issue_counts),
        "estimated_late_assignment_fraction": ratio(reasons["eta_late"], allocated),
        "cpu_local": {
            "constructor_seconds": constructor_cpu,
            "first_week_with_constructor_seconds": constructor_cpu + cpu[0] if cpu else None,
            "act_total_seconds": sum(cpu),
            "act_p95_seconds": percentile(cpu, 0.95),
            "act_max_seconds": max(cpu) if cpu else None,
            "stages": stage_rows,
        },
    }


def comparison_metrics(result, target=0.75):
    if result.get("status") != "completed" or not result.get("comparison"):
        return {"usable": False, "status": result.get("status"), "gate": "missing_completed_score"}
    comparison, settings = result["comparison"], result["settings"]
    candidate, baseline = comparison["a"], comparison["b"]
    for score in (candidate, baseline):
        if not math.isfinite(score["rss"]) or not math.isfinite(score["cost_usd"]):
            raise ValueError("nonfinite score/cost cannot support a target gate")
        if score.get("interval") and any(not math.isfinite(value) for value in score["interval"]):
            raise ValueError("nonfinite score interval")
    if not math.isfinite(comparison["difference"]) or (
        comparison.get("interval") and any(not math.isfinite(value) for value in comparison["interval"])
    ):
        raise ValueError("nonfinite comparison interval/difference")
    reasons = []
    if candidate["quick"] or baseline["quick"] or settings.get("quick"):
        reasons.append("quick_not_valid_for_target")
    if not settings.get("cpu_budget"):
        reasons.append("official_style_cpu_metering_missing")
    if candidate["fallback_weeks"]:
        reasons.append("candidate_fallback")
    if candidate["rss"] < target:
        reasons.append("score_below_target")
    if not candidate.get("interval") or candidate["interval"][0] < target:
        reasons.append("target_not_confirmed_by_score_interval")
    if not comparison.get("interval") or comparison["interval"][0] <= 0:
        reasons.append("improvement_not_confirmed_by_paired_interval")
    reference = {row["episode"]: row for row in baseline["per_episode"]}
    if len(reference) != len(baseline["per_episode"]):
        raise ValueError("duplicate baseline episode")
    episodes, harms = [], defaultdict(list)
    for row in candidate["per_episode"]:
        other = reference.get(row["episode"])
        if other is None or any(row[key] != other[key] for key in ("stratum", "J_naive_cents", "J_clairvoyant_cents")):
            raise ValueError("candidate/baseline episode references do not match")
        delta = (row["J_policy_cents"] - other["J_policy_cents"]) / 100
        episodes.append(
            {
                "episode": row["episode"],
                "harm_level": row["stratum"],
                "candidate_cost_usd": row["J_policy_cents"] / 100,
                "baseline_cost_usd": other["J_policy_cents"] / 100,
                "extra_cost_usd": delta,
            }
        )
        harms[row["stratum"]].append(delta)
    if len(reference) != len(episodes) or len({row["episode"] for row in episodes}) != len(episodes):
        raise ValueError("candidate/baseline episode sets do not match")
    return {
        "usable": True,
        "task": settings["task"],
        "entropy": settings["entropy"],
        "settings": settings,
        "submission_sha256": result["submission_sha256"],
        "rss": candidate["rss"],
        "score_interval": candidate["interval"],
        "target": target,
        "score_gap_to_target": max(0.0, target - candidate["rss"]),
        "baseline_rss": baseline["rss"],
        "difference": comparison["difference"],
        "paired_interval": comparison["interval"],
        "fallback_weeks": candidate["fallback_weeks"],
        "candidate_mean_cost_usd": candidate["cost_usd"],
        "baseline_mean_cost_usd": baseline["cost_usd"],
        "extra_mean_cost_fraction": ratio(candidate["cost_usd"] - baseline["cost_usd"], baseline["cost_usd"]),
        "episode_win_fraction": ratio(sum(row["extra_cost_usd"] < 0 for row in episodes), len(episodes)),
        "worst_episodes": sorted(episodes, key=lambda row: row["extra_cost_usd"], reverse=True),
        "harm_level_raw_extra_cost_usd": {
            str(level): {"episodes": len(values), "mean": mean(values)} for level, values in sorted(harms.items())
        },
        "gate": "candidate_for_holdout_validation" if not reasons else "do_not_promote",
        "gate_reasons": reasons,
    }


def diagnostic_metrics(folder):
    folder = Path(folder).resolve()
    summary = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
    if summary.get("status") != "completed" or summary.get("submissions_unchanged") is not True:
        return {"usable": False, "source": str(folder), "status": summary.get("status")}
    result = {
        "usable": True,
        "source": str(folder),
        "settings": summary["settings"],
        "submission_sha256": summary["submission_sha256"],
        "candidate": [],
        "baseline": [],
    }
    for label in ("candidate", "baseline"):
        for episode in summary[label]:
            path = folder / f"{label}-{episode['episode']}.jsonl"
            with path.open(encoding="utf-8") as stream:
                metrics = trace_metrics(
                    (json.loads(line) for line in stream), episode.get("constructor_cpu_seconds", 0)
                )
            if metrics["weeks"] != episode["weeks"] or not math.isclose(
                metrics["cost_usd"], episode["cost_usd"], rel_tol=1e-12, abs_tol=0.01
            ):
                raise ValueError(f"trace and summary do not agree: {path}")
            result[label].append({"episode": episode["episode"], **metrics})
    return result


def matching_sources(score, diagnostic):
    return (
        score.get("usable")
        and diagnostic.get("usable")
        and all(score[key] == diagnostic["settings"][key] for key in ("task", "entropy"))
        and score["submission_sha256"] == diagnostic["submission_sha256"]
    )


def number(value):
    return "unknown" if value is None else f"{value:,.4f}"


def render_report(result):
    lines = [
        "# Team Metrics Report",
        "",
        f"Target RSS: **{result['target']:.4f}**",
        "",
        "Read-only analysis. No policy changes, scoring, upload or promotion performed.",
        "",
        "## Scored Comparisons",
        "",
    ]
    for score in result["comparisons"]:
        lines += [f"### {score['source']}", ""]
        if not score["usable"]:
            lines += [f"Not scored: {score['status']}. Excluded from target evidence.", ""]
            continue
        lines += [
            f"Task: {score['task']}; root: {score['entropy']}; RSS: **{number(score['rss'])}**; "
            f"baseline: **{number(score['baseline_rss'])}**.",
            f"Gap to target: {number(score['score_gap_to_target'])}; paired difference: "
            f"{number(score['difference'])}; interval: {score['paired_interval']}.",
            f"Score interval: {score['score_interval']}; fallback weeks: {score['fallback_weeks']}.",
            f"Mean cost: candidate USD {number(score['candidate_mean_cost_usd'])}; "
            f"baseline USD {number(score['baseline_mean_cost_usd'])}.",
            f"Episode win fraction: {number(score['episode_win_fraction'])}.",
            f"Gate: **{score['gate']}**. Reasons: {', '.join(score['gate_reasons']) or 'none'}.",
            "",
            f"Candidate SHA256: `{score['submission_sha256']['candidate']}`",
            "",
            "| Episode | Harm | Extra Cost USD vs Baseline |",
            "|---:|---:|---:|",
        ]
        for row in score["worst_episodes"][:8]:
            lines.append(f"| {row['episode']} | {row['harm_level']} | {number(row['extra_cost_usd'])} |")
        lines += ["", "Raw cost deltas by harm level (not reweighted RSS):", ""]
        for level, row in score["harm_level_raw_extra_cost_usd"].items():
            lines.append(f"- Harm {level}: {row['episodes']} episodes, mean extra USD {number(row['mean'])}.")
        lines.append("")
    lines += ["## Diagnostic Replays", ""]
    for diagnostic in result["diagnostics"]:
        lines += [f"### {diagnostic['source']}", ""]
        if not diagnostic["usable"]:
            lines += ["Incomplete, failed or sources changed; excluded.", ""]
            continue
        lines += [
            f"Matching score sources: {diagnostic['matching_score_sources'] or 'none'}.",
            "Matching hashes/root do not turn a local replay into isolated scoring.",
            "",
        ]
        for label in ("candidate", "baseline"):
            for episode in diagnostic[label]:
                cpu = episode["cpu_local"]
                lines += [
                    f"#### {label} Episode {episode['episode']}",
                    "",
                    f"Cost USD {number(episode['cost_usd'])}; outcome coverage "
                    f"{episode['outcome_weeks']}/{episode['weeks']} weeks.",
                    f"Local CPU: first act + constructor {number(cpu['first_week_with_constructor_seconds'])} s; "
                    f"act p95 {number(cpu['act_p95_seconds'])} s; max {number(cpu['act_max_seconds'])} s.",
                    f"Late assignment estimate fraction: {number(episode['estimated_late_assignment_fraction'])}.",
                    f"Cost reconciliation residual USD: {number(episode['cost_reconciliation_residual_usd'])}.",
                    "",
                    "| Commodity | Unit | Requested | Planned Unmet | Executed / Requested | Served / Demand |",
                    "|---|---|---:|---:|---:|---:|",
                ]
                for row in episode["commodity_metrics"]:
                    lines.append(
                        f"| {row['commodity']} | {row['unit']} | {number(row.get('requested_quantity', 0))} | "
                        f"{number(row['planned_unmet_fraction'])} | {number(row['execution_fraction'])} | "
                        f"{number(row['demand_service_fraction'])} |"
                    )
                lines += ["", "Unmet reason counts (overlapping, not causal cost attribution):", ""]
                for code, count in Counter(episode["unmet_reason_counts"]).most_common(8):
                    lines.append(f"- `{code}`: {count}")
                lines += ["", "Known cost components (USD; coverage may be partial):", ""]
                for name, amount in (episode["cost_components_usd"] or {}).items():
                    lines.append(
                        f"- {name}: {number(amount)}; observed "
                        f"{episode['cost_component_observed_weeks'][name]}/{episode['weeks']} weeks."
                    )
                if episode["cost_components_usd"] is None:
                    lines.append("- Unknown: rerun diagnostics with trace schema v2.")
                lines += ["", "Local stage CPU totals (seconds):", ""]
                for stage, values in cpu["stages"].items():
                    lines.append(f"- {stage}: {number(values['total_seconds'])}, p95 {number(values['p95_seconds'])}.")
                if not cpu["stages"]:
                    lines.append("- Unknown: rerun diagnostics with trace schema v2.")
                lines += ["", "Largest mean known backlog nodes (ranking, not a sum across units):", ""]
                for row in sorted(
                    (
                        row
                        for row in episode["node_metrics"]
                        if row["field"] == "backlog" and row["mean_known_quantity"] is not None
                    ),
                    key=lambda row: row["mean_known_quantity"],
                    reverse=True,
                )[:6]:
                    lines.append(
                        f"- {row['node']}/{row['commodity']}: {number(row['mean_known_quantity'])} "
                        f"{row['unit']}; unknown records {row['unknown_records']}."
                    )
                lines += ["", "Reported resource saturation:", ""]
                for row in sorted(episode["resource_metrics"], key=lambda row: row["saturated_records"], reverse=True)[
                    :6
                ]:
                    lines.append(
                        f"- {row['kind']}[{row['index']}]/{row['pool']}: saturated "
                        f"{row['saturated_records']}/{row['reported_records_with_positive_limit']} records; "
                        f"unknown limit {row['unknown_limit_records']}; "
                        f"over limit {row['over_limit_records']}."
                    )
                lines += ["", "Observed shed by grid (GWh):", ""]
                if not episode["shed_by_grid"]:
                    lines.append("- Unknown or no observed grid records.")
                for row in episode["shed_by_grid"]:
                    lines.append(
                        f"- {row['node']}: {number(row['observed_shed_gwh'])} "
                        f"over {row['observed_weeks']} observed weeks."
                    )
                lines += ["", "Stock and transport volume/provenance details are in metrics.json.", ""]
        baseline_episodes = {row["episode"]: row for row in diagnostic["baseline"]}
        lines += ["Replay component differences: candidate minus baseline, matched episode IDs.", ""]
        for episode in diagnostic["candidate"]:
            baseline = baseline_episodes.get(episode["episode"])
            if baseline is None:
                continue
            for component, amount in (episode["cost_components_usd"] or {}).items():
                other = (baseline["cost_components_usd"] or {}).get(component)
                if (
                    other is not None
                    and episode["cost_component_observed_weeks"][component] == episode["weeks"]
                    and (baseline["cost_component_observed_weeks"][component] == baseline["weeks"])
                ):
                    lines.append(f"- Episode {episode['episode']} {component}: extra USD {number(amount - other)}.")
        lines.append("")
    lines += [
        "## Interpretation And Next Experiments",
        "",
        "1. Inspect shortage/shed cost deltas and service by commodity before optimizing freight.",
        "2. Rank backlog and starving stock nodes; check upstream replenishment and BOM/timing units.",
        "3. Check execution clipping and reported resource saturation; requests are not shipments.",
        "4. Ablate queue ETA, planning horizon, safety stock and production separately on a tuning root.",
        "5. Never promote a regression. Validate winners on a fresh root for Small/Full, with CPU checks.",
        "",
        "## Limits",
        "",
        "- Target gates are local evidence checks, not guarantees of leaderboard RSS >= 0.75.",
        "- Per-week need quantities repeat forecast demand and must not be called a real-world service rate.",
        "- Different commodities/units are never summed. Observed service covers only visible sink rows.",
        "- ETA-on-time reasons are conditional predictions, not realized on-time deliveries.",
        "- Resource utilization covers only reported resources, not every resource in the simulator.",
        "- Local CPU includes taps; it is not the isolated runner's measured CPU or fallback result.",
        "- Diagnostic policy seed salting is not reproduced; no new RSS or intervals are computed here.",
        "- Unknown outcomes in old traces remain unknown. Repeated runs are not independent scenarios.",
        "",
    ]
    return "\n".join(lines)


def build_report(comparison_paths=(), diagnostic_folders=(), target=0.75):
    if (
        isinstance(target, bool)
        or not isinstance(target, (int, float))
        or not math.isfinite(target)
        or not 0 <= target <= 1
    ):
        raise ValueError("target must be a finite number in [0, 1]")
    if not comparison_paths and not diagnostic_folders:
        raise ValueError("provide at least one comparison or diagnostics path")
    scores = []
    for path in comparison_paths:
        path = Path(path)
        score = comparison_metrics(json.loads(path.read_text(encoding="utf-8")), target)
        scores.append({"source": str(path.resolve()), **score})
    diagnostics = []
    for folder in diagnostic_folders:
        diagnostic = diagnostic_metrics(folder)
        diagnostic["matching_score_sources"] = [
            score["source"] for score in scores if matching_sources(score, diagnostic)
        ]
        diagnostics.append(diagnostic)
    return {"schema_version": 1, "target": target, "comparisons": scores, "diagnostics": diagnostics}
