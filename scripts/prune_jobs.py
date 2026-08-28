"""Remove mirrored runs that carry no draft.

`api.jobs` mirrors every finished run to `outputs/jobs/`, including the ones
that failed to fetch or produced nothing. Those are not runs you return to, and
they bury the ones that are — this clears them out.

Reports by default and deletes only with `--apply`, because it removes the
operator's own files and "I ran it to see" should never be destructive.

Usage:
    uv run python scripts/prune_jobs.py            # report only
    uv run python scripts/prune_jobs.py --apply    # actually delete
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.history import list_runs, prune_empty  # noqa: E402
from api.jobs import jobs_dir  # noqa: E402


def main() -> None:
    apply = "--apply" in sys.argv
    directory = jobs_dir()
    total = len(list(directory.glob("*.json"))) if directory.exists() else 0
    keep = len(list_runs(limit=10_000))
    doomed = prune_empty(dry_run=not apply)

    print(f"{directory}")
    print(f"  mirrored runs:      {total}")
    print(f"  carry a draft:      {keep}")
    print(f"  no draft:           {len(doomed)}")

    if not doomed:
        print("\nnothing to remove.")
        return
    if apply:
        print(f"\nremoved {len(doomed)} file(s).")
    else:
        for path in doomed[:5]:
            print(f"    {Path(path).name}")
        if len(doomed) > 5:
            print(f"    … and {len(doomed) - 5} more")
        print("\nnothing was deleted. re-run with --apply to remove them.")


if __name__ == "__main__":
    main()
