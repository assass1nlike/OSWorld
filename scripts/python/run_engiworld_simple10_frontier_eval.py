"""Run a small Engiworld frontier-model smoke set.

This script keeps the same per-task execution path as
``run_engiworld_all_frontier_eval.py``:

* ``task/task-c`` -> PromptAgent ``--eval-mode cli``
* ``task/task-v`` -> PromptAgent ``--eval-mode gui``
* provider defaults to OleehyO/OSWorld's original ``msh-sandbox``
* max steps defaults to 200

The only difference is the eval list: each split runs a curated set of 3
simple tasks instead of the full corpus.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from run_engiworld_all_frontier_eval import (
    DEFAULT_PROVIDER,
    PROMPT_RUNNER,
    REPO_ROOT,
    SplitConfig,
    _build_child_cmd,
    _default_result_dir,
    _load_dotenv,
    _quote_cmd,
    _resolve_engiworld_root,
)


SIMPLE3_C: dict[str, list[str]] = {
    "altium-designer": ["task-01"],
    "blender": ["task-01"],
    "eagle": ["task-01"],
}

SIMPLE3_V: dict[str, list[str]] = {
    "blender": ["task-01"],
    "eagle": ["task-01"],
    "zbrush": ["task-01"],
}


def _validate_meta(task_root: Path, meta: dict[str, list[str]]) -> None:
    missing: list[str] = []
    for domain, task_ids in meta.items():
        for task_id in task_ids:
            task_json = task_root / domain / task_id / f"{task_id}.json"
            if not task_json.is_file():
                missing.append(str(task_json))
    if missing:
        raise FileNotFoundError(
            "Simple3 eval list references missing task json(s):\n"
            + "\n".join(missing)
        )


def _write_eval_list(
    result_dir: Path,
    split: SplitConfig,
    meta: dict[str, list[str]],
    dry_run: bool,
) -> Path:
    eval_list = (result_dir / "_eval_lists" / f"{split.label}_simple3.json").resolve()
    if not dry_run:
        eval_list.parent.mkdir(parents=True, exist_ok=True)
        eval_list.write_text(
            json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return eval_list


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run 3 simple task-c and 3 simple task-v Engiworld evaluations.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--engiworld_root", help="Engiworld repo root. Defaults to a sibling Engiworld checkout.")
    parser.add_argument("--model", required=True, help="Model id passed to PromptAgent, e.g. gpt-5.5.")
    parser.add_argument("--provider_name", default=DEFAULT_PROVIDER, choices=[DEFAULT_PROVIDER])
    parser.add_argument("--mode", choices=["both", "cli", "gui"], default="both")
    parser.add_argument("--result_dir", help="Result root. Defaults to prompt_agent_simple3_maxstep*_maxtoken*.")
    parser.add_argument("--num_envs", type=int, default=None, help="Concurrency for each split unless overridden.")
    parser.add_argument("--num_envs_c", type=int, default=None, help="Concurrency for task-c / cli.")
    parser.add_argument("--num_envs_v", type=int, default=None, help="Concurrency for task-v / gui.")
    parser.add_argument("--max_steps", type=int, default=200)
    parser.add_argument("--max_trajectory_length", type=int, default=3)
    parser.add_argument("--max_tokens", type=int, default=None)
    parser.add_argument("--temperature", type=float, default=1.0)
    parser.add_argument("--top-p", type=float, default=None)
    parser.add_argument("--llm_retries", type=int, default=10)
    parser.add_argument("--llm_request_timeout", type=int, default=1800)
    parser.add_argument("--rpm", type=int, default=0)
    parser.add_argument("--screen_width", type=int, default=1920)
    parser.add_argument("--screen_height", type=int, default=1080)
    parser.add_argument("--sleep_after_execution", type=float, default=1.0)
    parser.add_argument("--client_password", default="")
    parser.add_argument("--bash_timeout", type=int, default=120)
    parser.add_argument("--spec", default="g4il.2xlarge")
    parser.add_argument("--reasoning_effort", type=str, default="high", choices=["low", "medium", "high"])
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--record_video", action="store_true")
    parser.add_argument("--log_level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
    parser.add_argument("--dry-run", action="store_true", help="Print commands without launching evaluations.")
    parser.add_argument(
        "--stream_logs",
        action="store_true",
        help="Let child processes write to this console instead of per-split log files.",
    )
    return parser.parse_args()


def main() -> int:
    _load_dotenv()
    args = parse_args()

    engiworld_root = _resolve_engiworld_root(args.engiworld_root)
    default_dir = _default_result_dir(args.max_steps, args.max_tokens)
    result_dir_name = "prompt_agent_simple3_" + default_dir.removeprefix("prompt_agent_")
    result_dir = Path(args.result_dir or result_dir_name).resolve()

    per_split_default = args.num_envs if args.num_envs is not None else 1
    split_specs: list[tuple[SplitConfig, dict[str, list[str]]]] = []
    if args.mode in {"both", "cli"}:
        split_specs.append((
            SplitConfig("task-c", "task-c", "cli", args.num_envs_c or per_split_default),
            SIMPLE3_C,
        ))
    if args.mode in {"both", "gui"}:
        split_specs.append((
            SplitConfig("task-v", "task-v", "gui", args.num_envs_v or per_split_default),
            SIMPLE3_V,
        ))

    print(f"[engiworld-simple3] engiworld_root={engiworld_root}")
    print(f"[engiworld-simple3] result_dir={result_dir}")
    print(f"[engiworld-simple3] model={args.model}")
    print(f"[engiworld-simple3] provider_name={args.provider_name}")
    print(f"[engiworld-simple3] prompt_runner={PROMPT_RUNNER}")

    commands: list[tuple[SplitConfig, list[str]]] = []
    for split, meta in split_specs:
        task_root = engiworld_root / "task" / split.task_dir_name
        _validate_meta(task_root, meta)
        eval_list = _write_eval_list(result_dir, split, meta, args.dry_run)
        cmd = _build_child_cmd(args, split, task_root, eval_list, result_dir)
        task_count = sum(len(v) for v in meta.values())
        print(
            f"[engiworld-simple3] {split.label}: eval_mode={split.eval_mode}, "
            f"domains={len(meta)}, tasks={task_count}, num_envs={split.num_envs}"
        )
        print(f"[engiworld-simple3] {split.label} eval_list={eval_list}")
        print(f"[engiworld-simple3] {split.label} command:")
        print(_quote_cmd(cmd))
        commands.append((replace(split), cmd))

    if args.dry_run:
        return 0

    logs_dir = result_dir / "_launcher_logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    processes: list[tuple[SplitConfig, subprocess.Popen, object | None, object | None]] = []
    try:
        for split, cmd in commands:
            if args.stream_logs:
                stdout_handle = None
                stderr_handle = None
                proc = subprocess.Popen(cmd, cwd=REPO_ROOT)
            else:
                stdout_path = logs_dir / f"{split.label}_stdout.log"
                stderr_path = logs_dir / f"{split.label}_stderr.log"
                stdout_handle = stdout_path.open("w", encoding="utf-8", errors="replace")
                stderr_handle = stderr_path.open("w", encoding="utf-8", errors="replace")
                print(f"[engiworld-simple3] {split.label} stdout={stdout_path}")
                print(f"[engiworld-simple3] {split.label} stderr={stderr_path}")
                proc = subprocess.Popen(
                    cmd,
                    cwd=REPO_ROOT,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    text=True,
                )
            processes.append((split, proc, stdout_handle, stderr_handle))

        exit_codes: dict[str, int] = {}
        for split, proc, _stdout_handle, _stderr_handle in processes:
            exit_codes[split.label] = proc.wait()
            print(f"[engiworld-simple3] {split.label} exited with code {exit_codes[split.label]}")
        return 0 if all(code == 0 for code in exit_codes.values()) else 1
    finally:
        for _split, _proc, stdout_handle, stderr_handle in processes:
            if stdout_handle is not None:
                stdout_handle.close()
            if stderr_handle is not None:
                stderr_handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
