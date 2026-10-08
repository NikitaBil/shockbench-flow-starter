"""Compare saved weekly traces without running policies or the environment."""

import json
from datetime import datetime
from pathlib import Path

import fire

from sbf_starter.team_timeline import build_timeline, render_timeline


def main(diagnostics, threshold_usd=1_000_000, out=None):
    source = Path(diagnostics).resolve()
    output = Path(out or f"outputs/14_team_timeline/{datetime.now():%Y-%m-%d_%H-%M-%S_%f}").resolve()
    if output.exists():
        raise FileExistsError("choose a new timeline output directory")
    if source in output.parents:
        raise ValueError("output must remain outside source diagnostics")
    if any((parent / "agent.py").exists() for parent in output.parents):
        raise ValueError("output must remain outside submission folders")
    result = build_timeline(source, threshold_usd)
    output.mkdir(parents=True)
    (output / "timeline.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    (output / "report.md").write_text(render_timeline(result), encoding="utf-8")
    print(f"Saved read-only timeline: {output}")
    for episode in result["episodes"]:
        print(
            f"Episode {episode['episode']}: first cost/shortage/shed regression weeks "
            f"{episode['first_week_above_threshold']}"
        )


if __name__ == "__main__":
    fire.Fire(main)
