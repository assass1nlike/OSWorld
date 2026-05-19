r"""Batch ground-truth checks against a running remote OSWorld server.

This is a thin wrapper around sanity_check_remote_osworld.py --gt-check.
It scans task JSON files and writes per-task result JSON plus summary files.

Example:

    python scripts/python/batch_gt_check_remote_osworld.py \
      --host 115.191.44.207 \
      --software-dir D:\localwork\Engiworld\task\task-v\altium-designer
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def discover_tasks(software_dir: Path) -> list[Path]:
    tasks: list[Path] = []
    for path in sorted(software_dir.glob("task-*/task-*.json")):
        if path.parent.name == path.stem:
            tasks.append(path)
    return tasks


def load_result(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"status": "error", "error": "result JSON was not written"}
    return json.loads(path.read_text(encoding="utf-8"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--server-port", type=int, default=5000)
    parser.add_argument("--vnc-port", type=int, default=5910)
    parser.add_argument("--software-dir", required=True, type=Path)
    parser.add_argument("--out-dir", type=Path, default=Path("logs") / "remote-gt-batch")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--task-timeout", type=int, default=900)
    parser.add_argument("--skip-type", action="append", default=[])
    parser.add_argument("--desktop-dir", default=None, help="Override remote desktop/output directory")
    parser.add_argument("--limit", type=int, default=0, help="Run only first N tasks; 0 means all")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    software_dir = args.software_dir.resolve()
    if not software_dir.exists() or not software_dir.is_dir():
        raise SystemExit(f"software dir not found: {software_dir}")

    tasks = discover_tasks(software_dir)
    if args.limit:
        tasks = tasks[: args.limit]
    if not tasks:
        raise SystemExit(f"no task JSON files found under {software_dir}")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = (args.out_dir / software_dir.name / stamp).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    script = Path(__file__).resolve().parent / "sanity_check_remote_osworld.py"
    rows: list[dict[str, Any]] = []

    print(f"[batch] host        : {args.host}:{args.server_port}")
    print(f"[batch] software_dir: {software_dir}")
    print(f"[batch] tasks       : {len(tasks)}")
    print(f"[batch] out_dir     : {out_dir}")

    for index, task_json in enumerate(tasks, start=1):
        task_name = task_json.parent.name
        result_json = out_dir / task_name / "result.json"
        log_file = out_dir / task_name / "run.log"
        result_json.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            sys.executable,
            str(script),
            "--host",
            args.host,
            "--server-port",
            str(args.server_port),
            "--vnc-port",
            str(args.vnc_port),
            "--task-json",
            str(task_json),
            "--timeout",
            str(args.timeout),
            "--gt-check",
            "--result-json",
            str(result_json),
        ]
        for typ in args.skip_type:
            cmd.extend(["--skip-type", typ])
        if args.desktop_dir:
            cmd.extend(["--desktop-dir", args.desktop_dir])

        print(f"[{index}/{len(tasks)}] {task_name}")
        started = time.time()
        try:
            proc = subprocess.run(
                cmd,
                cwd=Path(__file__).resolve().parents[2],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=args.task_timeout,
            )
            log_file.write_text(proc.stdout, encoding="utf-8")
            result = load_result(result_json)
            status = result.get("status", "error")
            error = result.get("error")
            rc = proc.returncode
        except subprocess.TimeoutExpired as exc:
            output = exc.stdout or ""
            log_file.write_text(output + f"\n[TIMEOUT after {args.task_timeout}s]\n", encoding="utf-8")
            status = "error"
            error = f"timeout after {args.task_timeout}s"
            rc = -1
            result = {"status": status, "error": error}

        eval_info = result.get("eval") or {}
        row = {
            "task": task_name,
            "task_json": str(task_json),
            "status": status,
            "passed": eval_info.get("passed"),
            "actual": repr(eval_info.get("actual")),
            "expected": repr(eval_info.get("expected")),
            "returncode": rc,
            "duration_sec": round(time.time() - started, 2),
            "error": error or "",
            "result_json": str(result_json),
            "log_file": str(log_file),
        }
        rows.append(row)
        print(f"    -> {status} passed={row['passed']} rc={rc} duration={row['duration_sec']}s")

    jsonl_path = out_dir / "summary.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    csv_path = out_dir / "summary.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    passed = sum(1 for row in rows if row["status"] == "pass")
    failed = len(rows) - passed
    print(f"[batch] pass={passed} fail_or_error={failed} total={len(rows)}")
    print(f"[batch] summary JSONL: {jsonl_path}")
    print(f"[batch] summary CSV  : {csv_path}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
