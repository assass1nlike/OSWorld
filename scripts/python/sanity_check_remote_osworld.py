r"""Sanity-check a running OSWorld server on a remote VM/ECS instance.

This script intentionally bypasses DesktopEnv providers such as msh-sandbox.
It talks directly to the OSWorld server inside an already-running image:

    http://<host>:5000

Typical usage:

    python scripts/python/sanity_check_remote_osworld.py \
      --host 1.2.3.4 \
      --task-json D:\localwork\Engiworld\task\task-v\zbrush\task-17\task-17.json

It performs the same setup-style operations that task JSON config uses:

    upload_file -> POST /setup/upload
    launch      -> POST /setup/launch
    execute     -> POST /setup/execute
    open        -> POST /setup/open_file

It also verifies /platform and /screenshot, saves a screenshot locally, and can
optionally evaluate simple OSWorld task evaluators of the common form:

    postconfig upload_file eval.py
    result:   {"type": "vm_command_line", "command": "..."}
    expected: {"type": "rule", "rules": {"expected": "..."}}
    func:     "exact_match"

For data-side ground-truth checks, use --gt-check. It runs setup, uploads every
file under <task_dir>/ground_truth to the VM desktop, then runs eval.py.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

import requests


class SanityError(RuntimeError):
    pass


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _short_text(text: str, limit: int = 500) -> str:
    text = text.replace("\r", "\\r").replace("\n", "\\n")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def request_ok(resp: requests.Response, *, label: str) -> requests.Response:
    if 200 <= resp.status_code < 300:
        return resp
    body = resp.text or ""
    raise SanityError(f"{label} failed: HTTP {resp.status_code}: {_short_text(body)}")


def resolve_local_paths(node: Any, asset_root: Path) -> list[dict[str, Any]]:
    """Rewrite task local_path fields in-place and return metadata."""
    resolved: list[dict[str, Any]] = []

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            for key, value in list(x.items()):
                if key == "local_path" and isinstance(value, str):
                    p = Path(value)
                    if not p.is_absolute():
                        p = (asset_root / p).resolve()
                    exists = p.exists()
                    size = p.stat().st_size if exists and p.is_file() else None
                    x[key] = str(p)
                    resolved.append(
                        {
                            "original": value,
                            "resolved": str(p),
                            "exists": exists,
                            "size": size,
                        }
                    )
                else:
                    walk(value)
        elif isinstance(x, list):
            for item in x:
                walk(item)

    walk(node)
    return resolved


class RemoteOSWorldClient:
    def __init__(self, base_url: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def get(self, path: str, *, timeout: float | None = None) -> requests.Response:
        return self.session.get(self.base_url + path, timeout=timeout or self.timeout)

    def post_json(self, path: str, payload: dict[str, Any], *, timeout: float | None = None) -> requests.Response:
        return self.session.post(
            self.base_url + path,
            json=payload,
            timeout=timeout or self.timeout,
        )

    def platform(self) -> str:
        resp = request_ok(self.get("/platform"), label="GET /platform")
        return resp.text.strip()

    def screenshot(self, dest: Path) -> None:
        resp = request_ok(self.get("/screenshot", timeout=60), label="GET /screenshot")
        content_type = resp.headers.get("content-type", "")
        if not resp.content:
            raise SanityError("GET /screenshot returned an empty body")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
        print(f"[ok] screenshot saved: {dest} ({len(resp.content)} bytes, {content_type})")

    def screen_size(self) -> dict[str, Any] | None:
        resp = self.session.post(self.base_url + "/screen_size", timeout=self.timeout)
        if resp.status_code == 200:
            try:
                return resp.json()
            except Exception:
                return {"raw": resp.text}
        print(f"[warn] POST /screen_size failed: HTTP {resp.status_code}: {_short_text(resp.text)}")
        return None

    def upload_file(self, local_path: str, remote_path: str) -> None:
        p = Path(local_path)
        if not p.exists() or not p.is_file():
            raise SanityError(f"local file missing: {p}")
        print(f"[setup] upload_file {p} -> {remote_path}")
        with p.open("rb") as fh:
            resp = self.session.post(
                self.base_url + "/setup/upload",
                data={"file_path": remote_path},
                files={"file_data": (Path(remote_path).name or p.name, fh)},
                timeout=(10, 600),
            )
        request_ok(resp, label=f"POST /setup/upload {remote_path}")
        print(f"[ok] uploaded {p.stat().st_size} bytes")

    def launch(self, command: str | list[str], shell: bool = False) -> None:
        print(f"[setup] launch shell={shell}: {command}")
        resp = self.post_json(
            "/setup/launch",
            {"command": command, "shell": shell},
            timeout=60,
        )
        request_ok(resp, label="POST /setup/launch")
        print(f"[ok] launch response: {_short_text(resp.text)}")

    def execute(self, command: str | list[str], shell: bool = False, timeout: float = 180.0) -> dict[str, Any]:
        print(f"[setup] execute shell={shell}: {command}")
        resp = self.post_json(
            "/setup/execute",
            {"command": command, "shell": shell},
            timeout=timeout,
        )
        request_ok(resp, label="POST /setup/execute")
        data = resp.json()
        print(
            "[ok] execute returncode={rc} output={out} error={err}".format(
                rc=data.get("returncode"),
                out=_short_text(str(data.get("output", "")), 300),
                err=_short_text(str(data.get("error", "")), 300),
            )
        )
        return data

    def open_file(self, path: str, timeout: float = 1810.0) -> None:
        print(f"[setup] open {path}")
        resp = self.post_json("/setup/open_file", {"path": path}, timeout=timeout)
        request_ok(resp, label="POST /setup/open_file")
        print(f"[ok] open response: {_short_text(resp.text)}")

    def change_wallpaper(self, path: str) -> None:
        print(f"[setup] change_wallpaper {path}")
        resp = self.post_json("/setup/change_wallpaper", {"path": path}, timeout=60)
        request_ok(resp, label="POST /setup/change_wallpaper")
        print(f"[ok] wallpaper response: {_short_text(resp.text)}")

    def activate_window(self, parameters: dict[str, Any]) -> None:
        print(f"[setup] activate_window {parameters}")
        resp = self.post_json("/setup/activate_window", parameters, timeout=60)
        request_ok(resp, label="POST /setup/activate_window")
        print(f"[ok] activate response: {_short_text(resp.text)}")

    def close_window(self, parameters: dict[str, Any]) -> None:
        print(f"[setup] close_window {parameters}")
        resp = self.post_json("/setup/close_window", parameters, timeout=60)
        request_ok(resp, label="POST /setup/close_window")
        print(f"[ok] close response: {_short_text(resp.text)}")


def run_setup_steps(
    client: RemoteOSWorldClient,
    steps: list[dict[str, Any]],
    *,
    skip_types: set[str],
) -> None:
    for index, step in enumerate(steps or [], start=1):
        typ = step.get("type")
        params = step.get("parameters") or {}
        if typ in skip_types:
            print(f"[skip] setup step {index}: {typ}")
            continue
        print(f"[step {index}/{len(steps)}] type={typ}")

        if typ == "upload_file":
            for file_spec in params.get("files") or []:
                client.upload_file(file_spec["local_path"], file_spec["path"])
        elif typ == "launch":
            client.launch(params["command"], shell=_boolish(params.get("shell", False)))
        elif typ == "execute":
            client.execute(
                params["command"],
                shell=_boolish(params.get("shell", False)),
                timeout=float(params.get("timeout", 180)),
            )
        elif typ == "open":
            client.open_file(params["path"])
        elif typ == "change_wallpaper":
            client.change_wallpaper(params["path"])
        elif typ == "activate_window":
            client.activate_window(params)
        elif typ == "close_window":
            client.close_window(params)
        elif typ == "download":
            run_download_step(client, params)
        else:
            raise SanityError(f"unsupported setup type {typ!r} at step {index}")


def infer_desktop_dir(task: dict[str, Any]) -> str:
    task_id = str(task.get("id", ""))
    if "-windows" in task_id:
        return r"C:\Users\user\Desktop"
    if "-ubuntu" in task_id:
        return "/home/user/Desktop"
    raise SanityError(
        f"cannot infer desktop path from task id {task_id!r}; pass --desktop-dir explicitly"
    )


def remote_join(directory: str, filename: str) -> str:
    sep = "\\" if "\\" in directory or ":" in directory else "/"
    normalized = filename.replace("/", sep).replace("\\", sep)
    return directory.rstrip("\\/") + sep + normalized


def upload_ground_truth(
    client: RemoteOSWorldClient,
    *,
    gt_dir: Path,
    desktop_dir: str,
) -> list[dict[str, Any]]:
    if not gt_dir.exists() or not gt_dir.is_dir():
        raise SanityError(f"ground_truth directory not found: {gt_dir}")
    files = sorted(p for p in gt_dir.rglob("*") if p.is_file())
    if not files:
        raise SanityError(f"ground_truth directory is empty: {gt_dir}")

    uploaded: list[dict[str, Any]] = []
    print(f"[gt] uploading {len(files)} ground-truth file(s) from {gt_dir}")
    for p in files:
        relative_name = p.relative_to(gt_dir).as_posix()
        remote_path = remote_join(desktop_dir, relative_name)
        client.upload_file(str(p), remote_path)
        uploaded.append({"local_path": str(p), "remote_path": remote_path, "size": p.stat().st_size})
    return uploaded


def run_download_step(client: RemoteOSWorldClient, params: dict[str, Any]) -> None:
    """Implement SetupController._download_setup enough for smoke checks."""
    for file_spec in params.get("files") or []:
        url = file_spec["url"]
        remote_path = file_spec["path"]
        print(f"[setup] download locally then upload {url} -> {remote_path}")
        with requests.get(url, stream=True, timeout=300) as resp:
            request_ok(resp, label=f"GET {url}")
            suffix = Path(remote_path).suffix
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp_path = Path(tmp.name)
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        tmp.write(chunk)
        try:
            client.upload_file(str(tmp_path), remote_path)
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass


def maybe_evaluate(client: RemoteOSWorldClient, task: dict[str, Any], *, skip_types: set[str]) -> None:
    evaluator = task.get("evaluator") or {}
    postconfig = evaluator.get("postconfig") or []
    if postconfig:
        print("[eval] running evaluator postconfig")
        run_setup_steps(client, postconfig, skip_types=skip_types)

    func = evaluator.get("func")
    result_cfg = evaluator.get("result") or {}
    expected_cfg = evaluator.get("expected") or {}

    if func != "exact_match":
        raise SanityError(f"sanity evaluator supports only func='exact_match', got {func!r}")
    if result_cfg.get("type") != "vm_command_line":
        raise SanityError(f"sanity evaluator supports only result.type='vm_command_line', got {result_cfg.get('type')!r}")
    if expected_cfg.get("type") != "rule":
        raise SanityError(f"sanity evaluator supports only expected.type='rule', got {expected_cfg.get('type')!r}")

    command = result_cfg["command"]
    shell = _boolish(result_cfg.get("shell", False))
    data = client.execute(command, shell=shell, timeout=180)
    actual = data.get("output", "")
    expected = (expected_cfg.get("rules") or {}).get("expected")
    ok = actual == expected
    print(f"[eval] actual  : {actual!r}")
    print(f"[eval] expected: {expected!r}")
    print(f"[eval] exact_match: {ok}")
    if not ok:
        raise SanityError("evaluator exact_match failed")


def run_evaluator(client: RemoteOSWorldClient, task: dict[str, Any], *, skip_types: set[str]) -> dict[str, Any]:
    evaluator = task.get("evaluator") or {}
    postconfig = evaluator.get("postconfig") or []
    if postconfig:
        print("[eval] running evaluator postconfig")
        run_setup_steps(client, postconfig, skip_types=skip_types)

    func = evaluator.get("func")
    result_cfg = evaluator.get("result") or {}
    expected_cfg = evaluator.get("expected") or {}

    if func != "exact_match":
        raise SanityError(f"sanity evaluator supports only func='exact_match', got {func!r}")
    if result_cfg.get("type") != "vm_command_line":
        raise SanityError(f"sanity evaluator supports only result.type='vm_command_line', got {result_cfg.get('type')!r}")
    if expected_cfg.get("type") != "rule":
        raise SanityError(f"sanity evaluator supports only expected.type='rule', got {expected_cfg.get('type')!r}")

    command = result_cfg["command"]
    shell = _boolish(result_cfg.get("shell", False))
    data = client.execute(command, shell=shell, timeout=180)
    actual = data.get("output", "")
    expected = (expected_cfg.get("rules") or {}).get("expected")
    ok = actual == expected
    print(f"[eval] actual  : {actual!r}")
    print(f"[eval] expected: {expected!r}")
    print(f"[eval] exact_match: {ok}")
    return {
        "passed": ok,
        "actual": actual,
        "expected": expected,
        "returncode": data.get("returncode"),
        "error": data.get("error", ""),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="ECS public/private IP or hostname")
    parser.add_argument("--server-port", type=int, default=5000)
    parser.add_argument("--vnc-port", type=int, default=5910)
    parser.add_argument("--task-json", required=True, type=Path)
    parser.add_argument(
        "--asset-root",
        type=Path,
        default=None,
        help="Base directory for relative local_path values. Default: task_json.parent.parent",
    )
    parser.add_argument("--out-dir", type=Path, default=Path("logs") / "remote-sanity")
    parser.add_argument("--skip-config", action="store_true", help="Only check server endpoints and local paths")
    parser.add_argument("--evaluate", action="store_true", help="Run supported evaluator after setup")
    parser.add_argument(
        "--gt-check",
        action="store_true",
        help="Run setup, upload <task_dir>/ground_truth/* to desktop, then run evaluator",
    )
    parser.add_argument("--gt-dir", type=Path, default=None, help="Override ground_truth directory")
    parser.add_argument("--desktop-dir", default=None, help="Override remote desktop/output directory")
    parser.add_argument("--result-json", type=Path, default=None, help="Write structured result JSON")
    parser.add_argument(
        "--skip-type",
        action="append",
        default=[],
        help="Skip a setup type, e.g. --skip-type launch. Can be repeated.",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    task_path = args.task_json.resolve()
    if not task_path.exists():
        raise SanityError(f"task JSON not found: {task_path}")
    asset_root = (args.asset_root or task_path.parent.parent).resolve()

    task = json.loads(task_path.read_text(encoding="utf-8"))
    local_paths = resolve_local_paths(task, asset_root)
    missing = [item for item in local_paths if not item["exists"]]

    print(f"[task] json       : {task_path}")
    print(f"[task] id         : {task.get('id')}")
    print(f"[task] snapshot   : {task.get('snapshot')}")
    print(f"[task] asset_root : {asset_root}")
    print(f"[task] local_path : {len(local_paths)} found")
    for item in local_paths:
        status = "ok" if item["exists"] else "missing"
        print(f"       [{status}] {item['original']} -> {item['resolved']} size={item['size']}")
    if missing:
        raise SanityError(f"{len(missing)} local_path value(s) are missing; first={missing[0]['resolved']}")

    base_url = f"http://{args.host}:{args.server_port}"
    client = RemoteOSWorldClient(base_url, timeout=args.timeout)
    print(f"[remote] osworld server: {base_url}")
    print(f"[remote] noVNC guess   : http://{args.host}:{args.vnc_port}/vnc.html")

    platform = client.platform()
    print(f"[ok] platform: {platform}")
    screen_size = client.screen_size()
    if screen_size is not None:
        print(f"[ok] screen_size: {screen_size}")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = args.out_dir / stamp
    screenshot_path = out_dir / "before_setup.png"
    client.screenshot(screenshot_path)

    skip_types = set(args.skip_type or [])
    result: dict[str, Any] = {
        "task_json": str(task_path),
        "task_id": task.get("id"),
        "snapshot": task.get("snapshot"),
        "host": args.host,
        "server_port": args.server_port,
        "platform": platform,
        "screen_size": screen_size,
        "status": "error",
        "error": None,
        "ground_truth": [],
        "eval": None,
        "out_dir": str(out_dir),
    }

    if not args.skip_config:
        print("[setup] running task config")
        run_setup_steps(client, task.get("config") or [], skip_types=skip_types)
        client.screenshot(out_dir / "after_setup.png")
    else:
        print("[skip] task config")

    failure_message: str | None = None
    if args.gt_check:
        gt_dir = (args.gt_dir or (task_path.parent / "ground_truth")).resolve()
        desktop_dir = args.desktop_dir or infer_desktop_dir(task)
        result["ground_truth"] = upload_ground_truth(
            client,
            gt_dir=gt_dir,
            desktop_dir=desktop_dir,
        )
        eval_result = run_evaluator(client, task, skip_types=skip_types)
        result["eval"] = eval_result
        result["status"] = "pass" if eval_result["passed"] else "fail"
        if not eval_result["passed"]:
            failure_message = "ground-truth evaluator check failed"
    elif args.evaluate:
        eval_result = run_evaluator(client, task, skip_types=skip_types)
        result["eval"] = eval_result
        result["status"] = "pass" if eval_result["passed"] else "fail"
        if not eval_result["passed"]:
            failure_message = "evaluator exact_match failed"
    else:
        result["status"] = "pass"

    if args.result_json:
        args.result_json.parent.mkdir(parents=True, exist_ok=True)
        args.result_json.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[ok] result JSON saved: {args.result_json}")
    if failure_message:
        result["error"] = failure_message
        if args.result_json:
            args.result_json.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        raise SanityError(failure_message)

    print("[done] remote sanity check completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
