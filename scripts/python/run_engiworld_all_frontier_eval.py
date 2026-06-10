"""One-command Engiworld frontier-model evaluation launcher.

This script runs both Engiworld task splits with OleehyO/OSWorld's original
``msh-sandbox`` instance creation path:

* ``task/task-c`` -> PromptAgent ``--eval-mode cli``
* ``task/task-v`` -> PromptAgent ``--eval-mode gui``

The two splits are launched concurrently. Each split also uses
``run_multienv_engiworld_prompt.py --num_envs ...`` internally, so tasks inside
each split run in parallel as well.

Typical use from the OSWorld repo root:

    python scripts/python/run_engiworld_all_frontier_eval.py --model gpt-5.5

For larger runs, set separate concurrency for the two splits:

    python scripts/python/run_engiworld_all_frontier_eval.py \
      --model gpt-5.5 --num_envs_c 8 --num_envs_v 8
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

try:
    import dotenv
except Exception:  # pragma: no cover - dependency exists in normal OSWorld envs
    dotenv = None


REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_RUNNER = REPO_ROOT / "scripts" / "python" / "run_multienv_engiworld_prompt.py"
DEFAULT_PROVIDER = "msh-sandbox"


@dataclass(frozen=True)
class SplitConfig:
    label: str
    task_dir_name: str
    eval_mode: str
    num_envs: int


def _load_dotenv() -> None:
    if dotenv is not None:
        dotenv.load_dotenv(REPO_ROOT / ".env")


def _candidate_engiworld_roots() -> Iterable[Path]:
    env_root = os.environ.get("ENGIWORLD_ROOT")
    if env_root:
        yield Path(env_root)
    task_root = os.environ.get("ENGIWORLD_TASK_ROOT")
    if task_root:
        task_path = Path(task_root)
        if task_path.name in {"task-c", "task-v"}:
            yield task_path.parents[1]
        elif task_path.name == "task":
            yield task_path.parent
    yield REPO_ROOT.parent / "Engiworld"
    yield REPO_ROOT.parent.parent / "Engiworld"


def _resolve_engiworld_root(value: str | None) -> Path:
    if value:
        root = Path(value).expanduser().resolve()
        if not (root / "task").is_dir():
            raise FileNotFoundError(f"Engiworld root does not contain task/: {root}")
        return root

    for root in _candidate_engiworld_roots():
        root = root.expanduser().resolve()
        if (root / "task").is_dir():
            return root
    raise FileNotFoundError(
        "Cannot locate Engiworld root. Pass --engiworld_root or set "
        "ENGIWORLD_ROOT."
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


def _discover_tasks(
    task_root: Path, domains: list[str], task_selector: str | None
) -> dict[str, list[str]]:
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


def _write_eval_list(
    result_dir: Path,
    split: SplitConfig,
    domains: list[str],
    task_selector: str | None,
    meta: dict[str, list[str]],
    dry_run: bool,
) -> Path:
    eval_dir = result_dir / "_eval_lists"
    domain_part = "all" if domains == ["all"] else "_".join(domains)
    task_part = "all" if not task_selector else re.sub(
        r"[^A-Za-z0-9_.-]+", "_", task_selector.strip()
    )
    eval_list = (eval_dir / f"{split.label}_{domain_part}_{task_part}.json").resolve()
    if not dry_run:
        eval_dir.mkdir(parents=True, exist_ok=True)
        eval_list.write_text(
            json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return eval_list


def _build_child_cmd(
    args: argparse.Namespace,
    split: SplitConfig,
    task_root: Path,
    eval_list: Path,
    result_dir: Path,
) -> list[str]:
    cmd = [
        sys.executable,
        str(PROMPT_RUNNER),
        "--provider_name", args.provider_name,
        "--test_config_base_dir", str(task_root),
        "--test_all_meta_path", str(eval_list),
        "--domain", "all",
        "--model", args.model,
        "--eval-mode", split.eval_mode,
        "--max_steps", str(args.max_steps),
        "--max_trajectory_length", str(args.max_trajectory_length),
        "--num_envs", str(split.num_envs),
        "--screen_width", str(args.screen_width),
        "--screen_height", str(args.screen_height),
        "--sleep_after_execution", str(args.sleep_after_execution),
        "--temperature", str(args.temperature),
        "--llm_retries", str(args.llm_retries),
        "--llm_request_timeout", str(args.llm_request_timeout),
        "--rpm", str(args.rpm),
        "--bash_timeout", str(args.bash_timeout),
        "--spec", args.spec,
        "--reasoning_effort", args.reasoning_effort,
        "--result_dir", str(result_dir),
        "--log_level", args.log_level,
    ]
    if args.client_password:
        cmd.extend(["--client_password", args.client_password])
    if args.headless:
        cmd.append("--headless")
    if args.record_video:
        cmd.append("--record_video")
    if args.resume:
        cmd.append("--resume")
    if args.max_tokens is not None:
        cmd.extend(["--max_tokens", str(args.max_tokens)])
    if args.top_p is not None:
        cmd.extend(["--top-p", str(args.top_p)])
    return cmd


def _quote_cmd(cmd: list[str]) -> str:
    return " ".join(f'"{x}"' if " " in x else x for x in cmd)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run all Engiworld task-c and task-v evaluations.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--engiworld_root", help="Engiworld repo root. Defaults to a sibling Engiworld checkout.")
    parser.add_argument("--model", required=True, help="Model id passed to PromptAgent, e.g. gpt-5.5.")
    parser.add_argument("--provider_name", default=DEFAULT_PROVIDER, choices=[DEFAULT_PROVIDER])
    parser.add_argument("--mode", choices=["both", "cli", "gui"], default="both")
    parser.add_argument("--domains", nargs="+", default=["all"], help="Domains to run, or all.")
    parser.add_argument("--tasks", help="Task ids or ranges to run in every selected domain.")
    parser.add_argument("--result_dir", help="Result root. Defaults to prompt_agent_maxstep*_maxtoken*.")
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
    parser.add_argument(
        "--log_level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    )
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
    result_dir = Path(args.result_dir or _default_result_dir(args.max_steps, args.max_tokens)).resolve()
    per_split_default = args.num_envs if args.num_envs is not None else 1
    splits: list[SplitConfig] = []
    if args.mode in {"both", "cli"}:
        splits.append(
            SplitConfig(
                label="task-c",
                task_dir_name="task-c",
                eval_mode="cli",
                num_envs=args.num_envs_c or per_split_default,
            )
        )
    if args.mode in {"both", "gui"}:
        splits.append(
            SplitConfig(
                label="task-v",
                task_dir_name="task-v",
                eval_mode="gui",
                num_envs=args.num_envs_v or per_split_default,
            )
        )

    print(f"[engiworld-all] engiworld_root={engiworld_root}")
    print(f"[engiworld-all] result_dir={result_dir}")
    print(f"[engiworld-all] model={args.model}")

    commands: list[tuple[SplitConfig, list[str], Path, dict[str, list[str]]]] = []
    for split in splits:
        task_root = engiworld_root / "task" / split.task_dir_name
        if not task_root.is_dir():
            raise FileNotFoundError(f"Missing {split.label} root: {task_root}")
        domains = _discover_domains(task_root, args.domains)
        meta = _discover_tasks(task_root, domains, args.tasks)
        eval_list = _write_eval_list(result_dir, split, args.domains, args.tasks, meta, args.dry_run)
        cmd = _build_child_cmd(args, split, task_root, eval_list, result_dir)
        task_count = sum(len(v) for v in meta.values())
        print(
            f"[engiworld-all] {split.label}: eval_mode={split.eval_mode}, "
            f"domains={len(meta)}, tasks={task_count}, num_envs={split.num_envs}"
        )
        print(f"[engiworld-all] {split.label} eval_list={eval_list}")
        print(f"[engiworld-all] {split.label} command:")
        print(_quote_cmd(cmd))
        commands.append((split, cmd, eval_list, meta))

    if args.dry_run:
        return 0

    logs_dir = result_dir / "_launcher_logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    processes: list[tuple[SplitConfig, subprocess.Popen, object | None, object | None]] = []
    try:
        for split, cmd, _eval_list, _meta in commands:
            if args.stream_logs:
                stdout_handle = None
                stderr_handle = None
                proc = subprocess.Popen(cmd, cwd=REPO_ROOT)
            else:
                stdout_path = logs_dir / f"{split.label}_stdout.log"
                stderr_path = logs_dir / f"{split.label}_stderr.log"
                stdout_handle = stdout_path.open("w", encoding="utf-8", errors="replace")
                stderr_handle = stderr_path.open("w", encoding="utf-8", errors="replace")
                print(f"[engiworld-all] {split.label} stdout={stdout_path}")
                print(f"[engiworld-all] {split.label} stderr={stderr_path}")
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
            print(f"[engiworld-all] {split.label} exited with code {exit_codes[split.label]}")
        return 0 if all(code == 0 for code in exit_codes.values()) else 1
    finally:
        for _split, _proc, stdout_handle, stderr_handle in processes:
            if stdout_handle is not None:
                stdout_handle.close()
            if stderr_handle is not None:
                stderr_handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
