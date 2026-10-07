"""Offline paired weekly diagnostics, not scoring or causal attribution."""

import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

from sbf_starter.team_metrics import number, ratio


def numeric(value):
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
    ):
        raise ValueError("trace values must be finite numbers or None")
    return value


def delta(candidate, baseline):
    candidate, baseline = numeric(candidate), numeric(baseline)
    return None if candidate is None or baseline is None else candidate - baseline


def indexed(rows, keys):
    result = {}
    for row in rows:
        key = tuple(row[name] for name in keys)
        if key in result:
            raise ValueError(f"duplicate diagnostic row: {key}")
        result[key] = row
    return result


def execution_totals(record):
    """Own visible execution; zero actions imply no new flow on that slot.

    This inference does not fill hidden positive requests and does not describe
    automatic queue releases, arrivals or final deliveries.
    """
    outcomes = record["outcomes"]
    requested = indexed(record["requested_flows"], ("slot_id",))
    executed = indexed(outcomes["executed_flows"], ("slot_id",))
    if requested.keys() - executed.keys():
        raise ValueError("requested slot absent from outcome layout")
    groups = defaultdict(
        lambda: {
            "requested": 0.0,
            "visible_execution": 0.0,
            "positive_request_slots": 0,
            "unknown_positive_request_slots": 0,
            "zero_request_inferences": 0,
        }
    )
    for slot, row in executed.items():
        group = groups[row["commodity"], row["unit"]]
        request_row = requested.get(slot)
        request = 0.0 if request_row is None else numeric(request_row["requested_quantity"])
        if request is None or request < 0:
            raise ValueError("action request must be nonnegative and known")
        if request_row and (request_row["commodity"], request_row["unit"]) != (row["commodity"], row["unit"]):
            raise ValueError("slot commodity/unit changed")
        quantity = numeric(row["executed_quantity"])
        group["requested"] += request
        group["positive_request_slots"] += int(request > 0)
        if quantity is None:
            if request > 0:
                group["unknown_positive_request_slots"] += 1
            else:
                group["zero_request_inferences"] += 1
        else:
            if quantity < 0 or quantity > request + 1e-8 * max(1.0, request):
                raise ValueError("execution exceeds recorded request")
            group["visible_execution"] += quantity
    return [
        {
            "commodity": key[0],
            "unit": key[1],
            **value,
            "execution_complete": value["unknown_positive_request_slots"] == 0,
            "executed": value["visible_execution"] if value["unknown_positive_request_slots"] == 0 else None,
        }
        for key, value in sorted(groups.items())
    ]


def weekly_pair(candidate, baseline):
    if candidate["week"] != baseline["week"]:
        raise ValueError("weekly records are not aligned")
    if not candidate.get("outcomes") or not baseline.get("outcomes"):
        raise ValueError("timeline requires trace schema v2 outcomes for both policies")
    a, b = candidate["outcomes"], baseline["outcomes"]
    components = {
        name: delta(a["cost_components_usd"].get(name), b["cost_components_usd"].get(name))
        for name in sorted(a["cost_components_usd"].keys() | b["cost_components_usd"].keys())
    }
    grids = []
    aa, bb = indexed(a["shed_gwh"], ("node",)), indexed(b["shed_gwh"], ("node",))
    for key in sorted(aa.keys() | bb.keys()):
        av, bv = aa.get(key, {}).get("quantity"), bb.get(key, {}).get("quantity")
        grids.append(
            {
                "node": key[0],
                "candidate_shed_gwh": numeric(av),
                "baseline_shed_gwh": numeric(bv),
                "extra_shed_gwh": delta(av, bv),
            }
        )
    sinks = []
    keys = ("node", "commodity", "unit")
    aa, bb = indexed(a["sinks"], keys), indexed(b["sinks"], keys)
    for key in sorted(aa.keys() | bb.keys()):
        ar, br = aa.get(key, {}), bb.get(key, {})
        ad, bd = numeric(ar.get("demand")), numeric(br.get("demand"))
        if ad is not None and bd is not None and not math.isclose(ad, bd, rel_tol=1e-10, abs_tol=1e-6):
            raise ValueError("observed demand differs between paired scenario traces")
        av, bv = numeric(ar.get("served")), numeric(br.get("served"))
        sinks.append(
            dict(zip(keys, key))
            | {
                "candidate_demand": ad,
                "baseline_demand": bd,
                "candidate_served": av,
                "baseline_served": bv,
                "extra_served": delta(av, bv),
                "extra_lost": delta(ar.get("lost"), br.get("lost")),
                "candidate_service_fraction": ratio(av, ad) if av is not None else None,
                "baseline_service_fraction": ratio(bv, bd) if bv is not None else None,
            }
        )
    allocation = candidate.get("allocation") or {}
    reasons = Counter(row["code"] for row in allocation.get("reasons", []))
    unmet = Counter(code for row in allocation.get("unmet_needs", []) for code in set(row["reason"].split(",")))
    if candidate["step_cost_usd"] is None or baseline["step_cost_usd"] is None:
        raise ValueError("weekly net cost must be known")
    return {
        "week": candidate["week"],
        "candidate_cost_usd": numeric(candidate["step_cost_usd"]),
        "baseline_cost_usd": numeric(baseline["step_cost_usd"]),
        "extra_cost_usd": delta(candidate["step_cost_usd"], baseline["step_cost_usd"]),
        "component_deltas_usd": components,
        "extra_salvage_usd": delta(candidate.get("salvage_usd"), baseline.get("salvage_usd")),
        "grids": grids,
        "sinks": sinks,
        "candidate_flows": execution_totals(candidate),
        "baseline_flows": execution_totals(baseline),
        "candidate_unmet_reason_counts": dict(unmet),
        "candidate_reason_counts": dict(reasons),
        "estimated_late_assignment_fraction": ratio(reasons["eta_late"], reasons["allocated_current_resources"]),
    }


def episode_timeline(candidate, baseline, threshold_usd=1_000_000):
    weeks = []
    cumulative = 0.0
    if len(candidate) != len(baseline) or not candidate:
        raise ValueError("paired traces must have equal nonzero lengths")
    expected = list(range(1, len(candidate) + 1))
    if [row["week"] for row in candidate] != expected or [row["week"] for row in baseline] != expected:
        raise ValueError("weeks must be unique, contiguous and ordered from 1")
    for a, b in zip(candidate, baseline):
        row = weekly_pair(a, b)
        cumulative += row["extra_cost_usd"]
        row["cumulative_extra_cost_usd"] = cumulative
        weeks.append(row)
    first = {}
    for key in ("cost", "shortage", "shed"):

        def value(row):
            return row["extra_cost_usd"] if key == "cost" else row["component_deltas_usd"].get(key)

        first[key] = next((row["week"] for row in weeks if value(row) is not None and value(row) > threshold_usd), None)
    grid_totals, sink_totals = {}, {}
    for week in weeks:
        for row in week["grids"]:
            aggregate = grid_totals.setdefault(
                row["node"],
                {"node": row["node"], "paired_observed_weeks": 0, "extra_shed_gwh": 0.0, "first_extra_shed_week": None},
            )
            if row["extra_shed_gwh"] is not None:
                aggregate["paired_observed_weeks"] += 1
                aggregate["extra_shed_gwh"] += row["extra_shed_gwh"]
                if row["extra_shed_gwh"] > 1e-6 and aggregate["first_extra_shed_week"] is None:
                    aggregate["first_extra_shed_week"] = week["week"]
        for row in week["sinks"]:
            key = row["node"], row["commodity"], row["unit"]
            aggregate = sink_totals.setdefault(
                key,
                dict(zip(("node", "commodity", "unit"), key))
                | {"paired_observed_weeks": 0, "extra_served": 0.0, "first_less_served_week": None},
            )
            if row["extra_served"] is not None:
                aggregate["paired_observed_weeks"] += 1
                aggregate["extra_served"] += row["extra_served"]
                if row["extra_served"] < -1e-6 and aggregate["first_less_served_week"] is None:
                    aggregate["first_less_served_week"] = week["week"]
    for aggregate in list(grid_totals.values()) + list(sink_totals.values()):
        if not aggregate["paired_observed_weeks"]:
            field = "extra_served" if "commodity" in aggregate else "extra_shed_gwh"
            aggregate[field] = None
    onset_weeks = sorted(
        {
            week + offset
            for week in first.values()
            if week is not None
            for offset in (-1, 0, 1)
            if 1 <= week + offset <= len(weeks)
        }
    )
    return {
        "weeks": weeks,
        "onset_context_weeks": onset_weeks,
        "extra_total_cost_usd": cumulative,
        "first_week_above_threshold": first,
        "first_cumulative_cost_above_threshold_week": next(
            (row["week"] for row in weeks if row["cumulative_extra_cost_usd"] > threshold_usd), None
        ),
        "worst_cost_weeks": sorted(weeks, key=lambda row: row["extra_cost_usd"], reverse=True)[:8],
        "grid_totals": list(grid_totals.values()),
        "sink_totals": list(sink_totals.values()),
    }


def file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def build_timeline(folder, threshold_usd=1_000_000):
    if (
        isinstance(threshold_usd, bool)
        or not isinstance(threshold_usd, (int, float))
        or not math.isfinite(threshold_usd)
        or threshold_usd < 0
    ):
        raise ValueError("threshold_usd must be finite and nonnegative")
    folder = Path(folder).resolve()
    summary_path = folder / "summary.json"
    summary_hash = file_hash(summary_path)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if summary.get("status") != "completed" or summary.get("submissions_unchanged") is not True:
        raise ValueError("use completed diagnostics with unchanged submissions")
    by_label = {label: indexed(summary[label], ("episode",)) for label in ("candidate", "baseline")}
    if not by_label["candidate"] or by_label["candidate"].keys() != by_label["baseline"].keys():
        raise ValueError("diagnostic episode IDs must match")
    hashes = {str(summary_path): summary_hash}
    paths = {
        (label, episode): folder / f"{label}-{episode[0]}.jsonl" for label in by_label for episode in by_label[label]
    }
    hashes.update({str(path): file_hash(path) for path in paths.values()})
    episodes = []
    for episode in sorted(by_label["candidate"]):
        records = {}
        for label in by_label:
            with paths[label, episode].open(encoding="utf-8") as stream:
                records[label] = [json.loads(line) for line in stream]
            expected = by_label[label][episode]
            costs = [numeric(row["step_cost_usd"]) for row in records[label]]
            if any(cost is None for cost in costs):
                raise ValueError("weekly net cost must be known")
            total = math.fsum(costs)
            if len(records[label]) != expected["weeks"] or not math.isclose(
                total, expected["cost_usd"], rel_tol=1e-12, abs_tol=0.01
            ):
                raise ValueError("trace totals/length differ from diagnostic summary")
        episodes.append(
            {"episode": episode[0], **episode_timeline(records["candidate"], records["baseline"], threshold_usd)}
        )
    if any(file_hash(Path(path)) != value for path, value in hashes.items()):
        raise RuntimeError("diagnostic source changed while reading")
    return {
        "schema_version": 1,
        "source": str(folder),
        "source_file_sha256": hashes,
        "settings": summary["settings"],
        "submission_sha256": summary["submission_sha256"],
        "threshold_usd": threshold_usd,
        "episodes": episodes,
        "limitations": [
            "Independent policy trajectories on the same scenario, not identical observations.",
            "First deterioration and coincident refusals do not establish causality.",
            "Partial observation coverage is reported; hidden positive execution remains unknown.",
            "Zero action implies no new dispatch, not no queue releases or arrivals.",
            "No new RSS, bootstrap interval, official CPU enforcement or upload.",
        ],
    }


def render_timeline(result):
    lines = [
        "# Weekly Regression Report",
        "",
        f"Task: {result['settings']['task']}; "
        f"root: {result['settings']['entropy']}; seed: {result['settings'].get('seed', 'unknown')}.",
        f"First cost/component regression threshold: USD {number(result['threshold_usd'])} per week.",
        "First grid/sink change markers use 1e-6 native units; they do not establish a persistent trend.",
        f"Candidate SHA256: `{result['submission_sha256']['candidate']}`",
        f"Baseline SHA256: `{result['submission_sha256']['baseline']}`",
        "",
        "Positive cost/shed deltas are worse; negative served deltas are worse. No new score computed.",
        "",
    ]
    for episode in result["episodes"]:
        lines += [
            f"## Episode {episode['episode']}",
            "",
            f"Net extra cost USD: {number(episode['extra_total_cost_usd'])}.",
            f"First weekly regressions: {episode['first_week_above_threshold']}.",
            f"First cumulative gap above threshold: week {episode['first_cumulative_cost_above_threshold_week']}.",
            "",
            "### Worst Cost Weeks",
            "",
            "| Week | Extra Net Cost USD | Extra Shortage USD | Extra Shed USD | Cumulative Gap USD |",
            "|---:|---:|---:|---:|---:|",
        ]
        for week in episode["worst_cost_weeks"]:
            lines.append(
                f"| {week['week']} | {number(week['extra_cost_usd'])} | "
                f"{number(week['component_deltas_usd'].get('shortage'))} | "
                f"{number(week['component_deltas_usd'].get('shed'))} | "
                f"{number(week['cumulative_extra_cost_usd'])} |"
            )
        lines += [
            "",
            "### Grid Deterioration",
            "",
            "| Grid | Extra Shed GWh | First Extra Shed Week | Paired Observed Weeks |",
            "|---|---:|---:|---:|",
        ]
        for row in sorted(
            episode["grid_totals"],
            key=lambda row: row["extra_shed_gwh"] if row["extra_shed_gwh"] is not None else -math.inf,
            reverse=True,
        ):
            lines.append(
                f"| {row['node']} | {number(row['extra_shed_gwh'])} | "
                f"{row['first_extra_shed_week']} | {row['paired_observed_weeks']} |"
            )
        lines += [
            "",
            "### Sink Deterioration",
            "",
            "| Sink | Commodity | Unit | Extra Served | First Less Served Week | Paired Weeks |",
            "|---|---|---|---:|---:|---:|",
        ]
        for row in sorted(
            episode["sink_totals"],
            key=lambda row: (
                row["commodity"],
                row["unit"],
                row["extra_served"] if row["extra_served"] is not None else math.inf,
            ),
        ):
            lines.append(
                f"| {row['node']} | {row['commodity']} | {row['unit']} | {number(row['extra_served'])} | "
                f"{row['first_less_served_week']} | {row['paired_observed_weeks']} |"
            )
        selected = set(episode["first_week_above_threshold"].values()) - {None}
        selected.update(row["week"] for row in episode["worst_cost_weeks"][:3])
        lines += ["", "### Candidate Decision Context", ""]
        for week in episode["weeks"]:
            if week["week"] in selected:
                lines += [
                    f"- Week {week['week']}: unmet reasons "
                    f"{Counter(week['candidate_unmet_reason_counts']).most_common(5)}; "
                    f"estimated late assignment fraction {number(week['estimated_late_assignment_fraction'])}."
                ]
        lines += [
            "",
            "### Onset Window: Before, During And After First Regression",
            "",
            "Flows are new dispatches, not final deliveries. Unknown positive execution is not zero.",
            "",
        ]
        for week in episode["weeks"]:
            if week["week"] not in episode["onset_context_weeks"]:
                continue
            lines += [
                f"#### Week {week['week']}",
                "",
                "| Commodity | Unit | Candidate Requested | Baseline Requested | Candidate Executed | "
                "Baseline Executed | Extra Executed |",
                "|---|---|---:|---:|---:|---:|---:|",
            ]
            a = indexed(week["candidate_flows"], ("commodity", "unit"))
            b = indexed(week["baseline_flows"], ("commodity", "unit"))
            for key in sorted(a.keys() | b.keys()):
                aa, bb = a.get(key, {}), b.get(key, {})
                lines.append(
                    f"| {key[0]} | {key[1]} | {number(aa.get('requested'))} | "
                    f"{number(bb.get('requested'))} | {number(aa.get('executed'))} | "
                    f"{number(bb.get('executed'))} | {number(delta(aa.get('executed'), bb.get('executed')))} |"
                )
            lines += ["", "Observed additional shed by grid (GWh):", ""]
            positive_grids = sorted(
                (row for row in week["grids"] if row["extra_shed_gwh"] is not None and row["extra_shed_gwh"] > 1e-6),
                key=lambda row: row["extra_shed_gwh"],
                reverse=True,
            )
            for row in positive_grids:
                lines.append(f"- {row['node']}: +{number(row['extra_shed_gwh'])}.")
            if not positive_grids:
                lines.append("- No observed positive delta; hidden rows remain unknown in timeline.json.")
            lines += ["", "Observed service deterioration, ordered within commodity/unit:", ""]
            negative_sinks = sorted(
                (row for row in week["sinks"] if row["extra_served"] is not None and row["extra_served"] < -1e-6),
                key=lambda row: (row["commodity"], row["unit"], row["extra_served"]),
            )
            for row in negative_sinks:
                lines.append(
                    f"- {row['node']}/{row['commodity']}: extra served {number(row['extra_served'])} "
                    f"{row['unit']}; candidate service {number(row['candidate_service_fraction'])}, "
                    f"baseline {number(row['baseline_service_fraction'])}."
                )
            if not negative_sinks:
                lines.append("- No observed negative delta; hidden rows remain unknown in timeline.json.")
            lines.append("")
        lines += [
            "",
            "### All Weeks",
            "",
            "| Week | Extra Net Cost USD | Extra Shortage USD | Extra Shed USD | Cumulative Gap USD |",
            "|---:|---:|---:|---:|---:|",
        ]
        for week in episode["weeks"]:
            lines.append(
                f"| {week['week']} | {number(week['extra_cost_usd'])} | "
                f"{number(week['component_deltas_usd'].get('shortage'))} | "
                f"{number(week['component_deltas_usd'].get('shed'))} | "
                f"{number(week['cumulative_extra_cost_usd'])} |"
            )
        lines += ["", "Per-week sink/grid values, requested/executed volumes and coverage are in timeline.json.", ""]
    lines += ["## Limits", ""] + [f"- {text}" for text in result["limitations"]] + [""]
    return "\n".join(lines)
