"""Build metrics.json and report.md from existing results; no simulator run."""

import json
from datetime import datetime
from pathlib import Path

import fire

from sbf_starter.team_metrics import build_report, render_report


def paths(value):
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


def main(comparison=None, diagnostics=None, target=0.75, out=None):
    """Paths may be strings or Fire list literals, e.g. --comparison='["a.json","b.json"]'."""
    comparisons, folders = paths(comparison), paths(diagnostics)
    output = Path(out or f"outputs/13_team_metrics/{datetime.now():%Y-%m-%d_%H-%M-%S_%f}").resolve()
    if output.exists():
        raise FileExistsError("choose a new metrics output directory")
    inputs = [Path(path).resolve() for path in comparisons] + [Path(folder).resolve() for folder in folders]
    if any(source.is_dir() and (output == source or source in output.parents) for source in inputs):
        raise ValueError("metrics output must remain outside diagnostic source folders")
    result = build_report(comparisons, folders, target)
    output.mkdir(parents=True)
    (output / "metrics.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    (output / "report.md").write_text(render_report(result), encoding="utf-8")
    print(f"Saved read-only metrics: {output}")


if __name__ == "__main__":
    fire.Fire(main)
