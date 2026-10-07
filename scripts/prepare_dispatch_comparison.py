"""Export matched allocator stages for teammate scoring; never execute agents.

Run with uv run python scripts/prepare_dispatch_comparison.py. Existing output
directories are refused so frozen comparisons cannot be overwritten.
"""

import argparse
import hashlib
import io
import json
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

from shockbench_flow_agent.submission import build_submission, check_zip


ROOT = Path(__file__).resolve().parents[1]
STAGES = {"before": "0e9b92b", "search": "f39133b", "dispatch": "727c6d9", "candidate": "a8ab160"}
PARAMS = {"allocation_enabled": True, "queue_eta_enabled": True}


def main(out=None):
    target = (
        Path(out).resolve()
        if out
        else ROOT / "outputs" / "14_active_dispatch" / datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")
    )
    target.mkdir(parents=True, exist_ok=False)
    manifest = {
        "params": PARAMS,
        "official_tests_run": False,
        "rss_measured": False,
        "note": "before is a matched current-parent allocator, not the original October 6 frozen candidate",
        "stages": {},
    }
    for name, ref in STAGES.items():
        revision = subprocess.check_output(["git", "rev-parse", ref], cwd=ROOT, text=True).strip()
        archived = subprocess.check_output(["git", "archive", "--format=zip", revision, "agents/team_agent"], cwd=ROOT)
        folder = target / name
        folder.mkdir()
        with zipfile.ZipFile(io.BytesIO(archived)) as source:
            for item in source.infolist():
                if item.is_dir():
                    continue
                relative = PurePosixPath(item.filename).relative_to("agents/team_agent")
                if ".." in relative.parts:
                    raise ValueError("unsafe archived path")
                path = folder.joinpath(*relative.parts)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(source.read(item))
        (folder / "params.json").write_text(json.dumps(PARAMS, indent=2) + "\n", encoding="utf-8", newline="\n")
        packed = target / (name + ".zip")
        build_submission(folder, packed)
        submission = check_zip(packed)
        manifest["stages"][name] = {
            "commit": revision,
            "sha256": hashlib.sha256(packed.read_bytes()).hexdigest(),
            "archive_check_passed": True,
            "file_count": len(submission.files),
            "unpacked_bytes": submission.total_bytes,
        }
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(target)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", help="New output directory; must not already exist")
    main(**vars(parser.parse_args()))
