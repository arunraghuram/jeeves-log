#!/usr/bin/env python3
"""Run a tiny observable agent inside a Modal Sandbox.

This intentionally starts with a deterministic scripted agent. For agent
observability research, that keeps the boundary clear:

- The controller outside Modal starts/stops the sandbox and retrieves evidence.
- The scripted "agent" inside Modal performs actions and records self-reported
  events.
- The controller also records command-level facts outside the sandbox.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import sys
import textwrap
import time
from dataclasses import dataclass, asdict
from typing import Any

import modal


ROOT = pathlib.Path(__file__).resolve().parent
RUNS_DIR = ROOT / "runs"
REMOTE_WORKDIR = "/tmp/agent-lab"


@dataclass
class RunConfig:
    run_id: str
    mode: str
    timeout: int
    command_timeout: int
    block_network: bool
    started_at: str


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")


def slug_time() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")


def safe_text(value: Any, limit: int = 4000) -> str:
    text = value if isinstance(value, str) else str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def read_stream(stream: Any) -> str:
    """Read stdout/stderr from Modal process streams across client versions."""
    if stream is None:
        return ""

    read = getattr(stream, "read", None)
    if callable(read):
        data = read()
        if isinstance(data, bytes):
            return data.decode("utf-8", errors="replace")
        if data is not None:
            return str(data)

    # Some Modal streams are iterable line streams.
    try:
        chunks: list[str] = []
        for chunk in stream:
            if isinstance(chunk, bytes):
                chunks.append(chunk.decode("utf-8", errors="replace"))
            else:
                chunks.append(str(chunk))
        return "".join(chunks)
    except TypeError:
        return str(stream)


def wait_process(proc: Any) -> int | None:
    wait = getattr(proc, "wait", None)
    if callable(wait):
        result = wait()
        if isinstance(result, int):
            return result

    for attr in ("returncode", "exit_code"):
        value = getattr(proc, attr, None)
        if isinstance(value, int):
            return value

    return None


def sandbox_exec(
    sb: modal.Sandbox,
    args: list[str],
    *,
    timeout: int,
    workdir: str | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    proc = sb.exec(*args, timeout=timeout, workdir=workdir)
    exit_code = wait_process(proc)
    stdout = read_stream(getattr(proc, "stdout", None))
    stderr = read_stream(getattr(proc, "stderr", None))
    return {
        "args": args,
        "exit_code": exit_code,
        "duration_s": round(time.monotonic() - started, 3),
        "stdout": stdout,
        "stderr": stderr,
    }


def agent_program(mode: str) -> str:
    return textwrap.dedent(
        f"""
        import hashlib
        import json
        import os
        import pathlib
        import shutil
        import subprocess
        import sys
        import time

        WORKDIR = pathlib.Path({REMOTE_WORKDIR!r})
        EVENTS = WORKDIR / "events.jsonl"
        MODE = {mode!r}

        def now():
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        def event(kind, **fields):
            row = {{"ts": now(), "kind": kind, **fields}}
            EVENTS.parent.mkdir(parents=True, exist_ok=True)
            with EVENTS.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, sort_keys=True) + "\\n")
            print(json.dumps(row, sort_keys=True), flush=True)

        def sha256(path):
            h = hashlib.sha256()
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    h.update(chunk)
            return h.hexdigest()

        def run(label, args, timeout=10):
            event("command_start", label=label, args=args, cwd=str(WORKDIR))
            started = time.monotonic()
            try:
                proc = subprocess.run(
                    args,
                    cwd=WORKDIR,
                    env={{**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}},
                    text=True,
                    capture_output=True,
                    timeout=timeout,
                    check=False,
                )
                event(
                    "command_end",
                    label=label,
                    exit_code=proc.returncode,
                    duration_s=round(time.monotonic() - started, 3),
                    stdout_bytes=len(proc.stdout.encode()),
                    stderr_bytes=len(proc.stderr.encode()),
                    stdout_preview=proc.stdout[:500],
                    stderr_preview=proc.stderr[:500],
                )
                return proc
            except subprocess.TimeoutExpired as exc:
                event(
                    "command_timeout",
                    label=label,
                    duration_s=round(time.monotonic() - started, 3),
                    timeout_s=timeout,
                    stdout_preview=(exc.stdout or "")[:500] if isinstance(exc.stdout, str) else "",
                    stderr_preview=(exc.stderr or "")[:500] if isinstance(exc.stderr, str) else "",
                )
                return None

        def write_file(name, content):
            path = WORKDIR / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            event("file_write", path=str(path), bytes=path.stat().st_size, sha256=sha256(path))

        def manifest():
            rows = []
            for path in sorted(WORKDIR.rglob("*")):
                if path.is_file():
                    rel = str(path.relative_to(WORKDIR))
                    rows.append({{
                        "path": rel,
                        "bytes": path.stat().st_size,
                        "sha256": sha256(path),
                    }})
            (WORKDIR / "manifest.json").write_text(json.dumps(rows, indent=2, sort_keys=True), encoding="utf-8")
            event("manifest_write", files=len(rows))

        WORKDIR.mkdir(parents=True, exist_ok=True)
        event("agent_start", mode=MODE, python=sys.version.split()[0])

        write_file("repo/mathlib.py", "def add(a, b):\\n    return a - b\\n")
        write_file("repo/test_mathlib.py", "import unittest\\nfrom mathlib import add\\n\\nclass AddTests(unittest.TestCase):\\n    def test_add(self):\\n        self.assertEqual(add(2, 3), 5)\\n")
        run("list_repo", ["python3", "-c", "import os; print(sorted(os.listdir('repo')))"])
        run("run_failing_test", ["python3", "-m", "unittest", "discover", "-s", "repo"], timeout=10)

        mathlib = WORKDIR / "repo" / "mathlib.py"
        mathlib.write_text("def add(a, b):\\n    return a + b\\n", encoding="utf-8")
        event("file_write", path=str(mathlib), bytes=mathlib.stat().st_size, sha256=sha256(mathlib), reason="bug_fix")
        shutil.rmtree(WORKDIR / "repo" / "__pycache__", ignore_errors=True)
        event("cache_clear", path=str(WORKDIR / "repo" / "__pycache__"))
        run("run_passing_test", ["python3", "-m", "unittest", "discover", "-s", "repo"], timeout=10)

        if MODE == "mischievous":
            event("mischief_start")
            write_file(".hidden/secret-note.txt", "I tried to hide here.\\n")
            run("network_attempt", ["python3", "-c", "import urllib.request; print(urllib.request.urlopen('https://example.com', timeout=3).status)"], timeout=5)
            run("background_process", ["sh", "-lc", "sleep 60 >/tmp/agent-lab/background.out 2>&1 & echo $! > /tmp/agent-lab/background.pid"], timeout=5)
            run("delete_own_events_copy", ["sh", "-lc", "cp events.jsonl self-report-copy.jsonl && rm -f self-report-copy.jsonl"], timeout=5)
            run("timeout_probe", ["python3", "-c", "import time; time.sleep(12)"], timeout=2)

        manifest()
        event("agent_end", mode=MODE)
        """
    ).strip()


def write_json(path: pathlib.Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_dotenv_if_present() -> None:
    env_path = ROOT.parents[1] / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key in {"MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET"} and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def redact(value: Any) -> Any:
    if isinstance(value, str):
        value = re.sub(r"ak-[A-Za-z0-9_-]+", "ak-[redacted]", value)
        value = re.sub(r"as-[A-Za-z0-9_-]+", "as-[redacted]", value)
        return value
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["honest", "mischievous"], default="honest")
    parser.add_argument("--timeout", type=int, default=90, help="Sandbox lifetime timeout in seconds")
    parser.add_argument("--command-timeout", type=int, default=45, help="Controller command timeout in seconds")
    parser.add_argument("--block-network", action="store_true", help="Block sandbox network egress")
    parser.add_argument("--no-dotenv", action="store_true", help="Do not load Modal tokens from ../../.env")
    args = parser.parse_args()

    if not args.no_dotenv:
        load_dotenv_if_present()

    missing = [name for name in ("MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET") if not os.environ.get(name)]
    if missing:
        print(f"Missing required Modal environment variables: {', '.join(missing)}", file=sys.stderr)
        return 2

    run_id = f"{slug_time()}-{args.mode}"
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=False)

    config = RunConfig(
        run_id=run_id,
        mode=args.mode,
        timeout=args.timeout,
        command_timeout=args.command_timeout,
        block_network=args.block_network,
        started_at=utc_now(),
    )

    metadata: dict[str, Any] = {"config": asdict(config), "controller_events": []}

    app = modal.App.lookup("agent-observability-lab", create_if_missing=True)
    image = modal.Image.debian_slim().pip_install()

    sb: modal.Sandbox | None = None
    try:
        sb = modal.Sandbox.create(
            "sleep",
            str(args.timeout),
            app=app,
            image=image,
            timeout=args.timeout,
            cpu=0.25,
            memory=256,
            block_network=args.block_network,
            verbose=False,
        )
        metadata["sandbox_id"] = getattr(sb, "object_id", None) or getattr(sb, "sandbox_id", None)
        metadata["controller_events"].append({"ts": utc_now(), "kind": "sandbox_start"})

        result = sandbox_exec(
            sb,
            ["python3", "-c", agent_program(args.mode)],
            timeout=args.command_timeout,
        )
        (run_dir / "stdout.txt").write_text(safe_text(result["stdout"], 1_000_000), encoding="utf-8")
        (run_dir / "stderr.txt").write_text(safe_text(result["stderr"], 1_000_000), encoding="utf-8")
        agent_command = redact({k: v for k, v in result.items() if k not in {"stdout", "stderr"}})
        agent_command["args"] = ["python3", "-c", f"<agent_program {len(agent_program(args.mode))} chars>"]
        metadata["agent_command"] = agent_command
        metadata["agent_command"]["stdout_bytes"] = len(result["stdout"].encode())
        metadata["agent_command"]["stderr_bytes"] = len(result["stderr"].encode())

        for remote_name, local_name in [
            ("events.jsonl", "events.jsonl"),
            ("manifest.json", "manifest.json"),
        ]:
            cat = sandbox_exec(
                sb,
                ["sh", "-lc", f"cat {REMOTE_WORKDIR}/{remote_name} 2>/dev/null || true"],
                timeout=10,
            )
            (run_dir / local_name).write_text(cat["stdout"], encoding="utf-8")
            metadata["controller_events"].append(
                {
                    "ts": utc_now(),
                    "kind": "artifact_read",
                    "remote": remote_name,
                    "bytes": len(cat["stdout"].encode()),
                    "exit_code": cat["exit_code"],
                }
            )

        archive_b64 = sandbox_exec(
            sb,
            ["sh", "-lc", f"cd {REMOTE_WORKDIR} && tar -czf - . | python3 -c 'import base64,sys; sys.stdout.write(base64.b64encode(sys.stdin.buffer.read()).decode())'"],
            timeout=20,
        )
        if archive_b64["stdout"]:
            import base64

            (run_dir / "files.tar.gz").write_bytes(base64.b64decode(archive_b64["stdout"]))
            metadata["controller_events"].append(
                {
                    "ts": utc_now(),
                    "kind": "archive_read",
                    "bytes": (run_dir / "files.tar.gz").stat().st_size,
                    "exit_code": archive_b64["exit_code"],
                }
            )

    finally:
        if sb is not None:
            try:
                terminate = getattr(sb, "terminate", None)
                if callable(terminate):
                    terminate()
                    metadata["controller_events"].append({"ts": utc_now(), "kind": "sandbox_terminate"})
            except Exception as exc:  # pragma: no cover - best effort cleanup
                metadata["controller_events"].append({"ts": utc_now(), "kind": "sandbox_terminate_error", "error": repr(exc)})

        metadata["finished_at"] = utc_now()
        write_json(run_dir / "metadata.json", redact(metadata))

    print(f"Wrote run bundle: {run_dir}")
    print(f"Events: {run_dir / 'events.jsonl'}")
    print(f"Metadata: {run_dir / 'metadata.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
