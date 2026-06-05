"""Convenience launcher for Engiworld frontier-model GUI evaluations.

This script is a thin wrapper around ``run_multienv_engiworld_prompt.py``. It
keeps the underlying PromptAgent runner unchanged, but fills in the conventions
we use for Engiworld model evaluations:

* provider defaults to ``volcengine`` so instances are created from task
  snapshots automatically;
* eval mode defaults to OleehyO-style ``gui``;
* ``max_steps`` defaults to 100;
* ``max_tokens`` defaults to the vendor default and is omitted from the child
  command;
* ``result_dir`` defaults to ``prompt_agent_maxstep100_maxtokendefault``;
* if no eval-list JSON is supplied, one is generated from the task directories.

The output directory layout follows OleehyO's Engiworld runner:

    prompt_agent_maxstep100_maxtokendefault/gui/gpt-5.5/eagle/task-01
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable

try:
    import dotenv
except Exception:  # pragma: no cover - dependency exists in normal OSWorld envs
    dotenv = None


REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_RUNNER = REPO_ROOT / "scripts" / "python" / "run_multienv_engiworld_prompt.py"


def _load_dotenv() -> None:
    if dotenv is not None:
        dotenv.load_dotenv(REPO_ROOT / ".env")


def _candidate_task_roots() -> Iterable[Path]:
    env_root = os.environ.get("ENGIWORLD_TASK_ROOT")
    if env_root:
        yield Path(env_root)
    yield REPO_ROOT.parent / "Engiworld" / "task" / "task-v"
    yield REPO_ROOT.parent.parent / "Engiworld" / "task" / "task-v"


def _resolve_task_root(value: str | None) -> Path:
    if value:
        root = Path(value).expanduser().resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"task root does not exist: {root}")
        return root

    for root in _candidate_task_roots():
        if root.is_dir():
            return root.resolve()
    raise FileNotFoundError(
        "Cannot locate Engiworld task-v root. Pass --task_root or set "
        "ENGIWORLD_TASK_ROOT."
    )


def _parse_task_selector(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    selected: list[str] = []
    for part in re.split(r"[\s,;]+", raw.strip()):
        if not part:
            continue
        m = re.fullmatch(r"(?:task-)?(\d+)-(?:task-)?(\d+)", part)
        if m:
            start, end = int(m.group(1)), int(m.group(2))
            if end < start:
                raise ValueError(f"invalid descending task range: {part}")
            selected.extend(f"task-{i:02d}" for i in range(start, end + 1))
            continue
        m = re.fullmatch(r"(?:task-)?(\d+)", part)
        if m:
            selected.append(f"task-{int(m.group(1)):02d}")
            continue
        selected.append(part)
    return selected


def _natural_task_key(task_id: str) -> tuple[int, str]:
    m = re.match(r"task-(\d+)", task_id)
    return (int(m.group(1)) if m else 10**9, task_id)


def _discover_domains(task_root: Path, requested: list[str]) -> list[str]:
    if requested and requested != ["all"]:
        domains = requested
    else:
        domains = [p.name for p in task_root.iterdir() if p.is_dir()]
    missing = [d for d in domains if not (task_root / d).is_dir()]
    if missing:
        raise FileNotFoundError(
            f"domain directory not found under {task_root}: {', '.join(missing)}"
        )
    return sorted(domains)


def _discover_tasks(task_root: Path, domains: list[str], task_selector: str | None) -> dict[str, list[str]]:
    requested_tasks = _parse_task_selector(task_selector)
    meta: dict[str, list[str]] = {}
    for domain in domains:
        domain_dir = task_root / domain
        if requested_tasks is None:
            task_ids = [
                p.name
                for p in domain_dir.iterdir()
                if p.is_dir() and (p / f"{p.name}.json").is_file()
            ]
        else:
            task_ids = []
            for task_id in requested_tasks:
                task_dir = domain_dir / task_id
                if not (task_dir / f"{task_id}.json").is_file():
                    raise FileNotFoundError(
                        f"task json not found: {task_dir / f'{task_id}.json'}"
                    )
                task_ids.append(task_id)
        task_ids = sorted(dict.fromkeys(task_ids), key=_natural_task_key)
        if task_ids:
            meta[domain] = task_ids
    if not meta:
        raise RuntimeError(f"No tasks found under {task_root} for domains={domains!r}")
    return meta


def _default_result_dir(max_steps: int, max_tokens: int | None) -> str:
    token_part = "maxtokendefault" if max_tokens is None else f"maxtoken{max_tokens}"
    return f"prompt_agent_maxstep{max_steps}_{token_part}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Engiworld frontier-model PromptAgent evaluations.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--task_root", help="Engiworld task-v root, e.g. /path/to/Engiworld/task/task-v")
    parser.add_argument("--eval_list", help="Existing eval-list JSON. If omitted, one is generated.")
    parser.add_argument("--domains", nargs="+", default=["all"], help="Domains to run, or all.")
    parser.add_argument("--tasks", help="Task ids or ranges, e.g. 'task-01,task-03' or '1-5'.")
    parser.add_argument("--model", required=True, help="Model id passed to PromptAgent, e.g. gpt-5.5.")
    parser.add_argument("--provider_name", default="volcengine", choices=["volcengine", "msh-sandbox"])
    parser.add_argument("--eval-mode", dest="eval_mode", default="gui", choices=["gui", "cli"])
    parser.add_argument("--result_dir", help="Result root. Defaults to prompt_agent_maxstep*_maxtoken*.")
    parser.add_argument("--num_envs", type=int, default=1)
    parser.add_argument("--max_steps", type=int, default=100)
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
    parser.add_argument("--bash_timeout", type=int, default=30)
    parser.add_argument("--reasoning_effort", type=str, default="high", choices=["low", "medium", "high"])
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--record_video", action="store_true")
    parser.add_argument("--log_level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
    parser.add_argument("--dry-run", action="store_true", help="Print the generated eval list and child command without running it.")
    return parser.parse_args()


def main() -> int:
    _load_dotenv()
    args = parse_args()
    task_root = _resolve_task_root(args.task_root)
    result_dir = args.result_dir or _default_result_dir(args.max_steps, args.max_tokens)

    if args.eval_list:
        eval_list = Path(args.eval_list).expanduser().resolve()
        if not eval_list.is_file():
            raise FileNotFoundError(f"eval list does not exist: {eval_list}")
    else:
        domains = _discover_domains(task_root, args.domains)
        meta = _discover_tasks(task_root, domains, args.tasks)
        eval_dir = Path(result_dir) / "_eval_lists"
        eval_dir.mkdir(parents=True, exist_ok=True)
        domain_part = "all" if args.domains == ["all"] else "_".join(args.domains)
        task_part = "all" if not args.tasks else re.sub(r"[^A-Za-z0-9_.-]+", "_", args.tasks.strip())
        eval_list = (eval_dir / f"{domain_part}_{task_part}.json").resolve()
        eval_list.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    child_cmd = [
        sys.executable,
        str(PROMPT_RUNNER),
        "--provider_name", args.provider_name,
        "--test_config_base_dir", str(task_root),
        "--test_all_meta_path", str(eval_list),
        "--domain", "all",
        "--model", args.model,
        "--eval-mode", args.eval_mode,
        "--max_steps", str(args.max_steps),
        "--max_trajectory_length", str(args.max_trajectory_length),
        "--num_envs", str(args.num_envs),
        "--screen_width", str(args.screen_width),
        "--screen_height", str(args.screen_height),
        "--sleep_after_execution", str(args.sleep_after_execution),
        "--temperature", str(args.temperature),
        "--llm_retries", str(args.llm_retries),
        "--llm_request_timeout", str(args.llm_request_timeout),
        "--rpm", str(args.rpm),
        "--bash_timeout", str(args.bash_timeout),
        "--reasoning_effort", args.reasoning_effort,
        "--result_dir", result_dir,
        "--log_level", args.log_level,
    ]
    if args.client_password:
        child_cmd.extend(["--client_password", args.client_password])
    if args.headless:
        child_cmd.append("--headless")
    if args.record_video:
        child_cmd.append("--record_video")
    if args.resume:
        child_cmd.append("--resume")
    if args.max_tokens is not None:
        child_cmd.extend(["--max_tokens", str(args.max_tokens)])
    if args.top_p is not None:
        child_cmd.extend(["--top-p", str(args.top_p)])

    print(f"[engiworld] task_root={task_root}")
    print(f"[engiworld] eval_list={eval_list}")
    print(f"[engiworld] result_dir={Path(result_dir).resolve()}")
    print("[engiworld] command:")
    print(" ".join(f'"{x}"' if " " in x else x for x in child_cmd))

    if args.dry_run:
        return 0
    return subprocess.call(child_cmd, cwd=REPO_ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
