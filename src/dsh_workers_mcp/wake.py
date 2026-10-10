"""
dsh-wake: run the lead's client again when something needs the lead.

The lead runs only when a person types. Nothing wakes it when a worker returns or
blocks, so every task boundary is also a boundary for the user. This is a thin
loop, not a service. It reads `dsh_brief`, decides whether anything needs the
lead, and if so runs a command that you give it, which starts the lead's client
non-interactively with a resume prompt.

The command is a template (`--cmd`), so the driver is not tied to one client. It
fills in `{prompt}` (quoted for the shell) and `{state_dir}`.

Rules, all logged to `<state dir>/wake.jsonl` (one line for every fire, skip and
stop, with the reason):

* Wake only when `needsAction` has lines, and only if they differ from the
  lines at the last wake. A lead that was woken and could not clear an item (it
  waits for the user) is not woken again for the same item.
* Stop when the milestone file starts with DONE or BLOCKED, when `--budget-hours`
  have passed, or after `--max-fires` wakes.
* A wake is a hint. The woken lead starts with `dsh_brief`, which is the read.

`--dry-run` makes one pass, prints what it would run and runs nothing.

Not verified against a real client. How a non-interactive run treats a tool call
that is not pre-approved, and whether the resume flag of a client keeps the
context of the lead across wakes, depend on the client. Test your command by hand
before you leave the driver running.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path
from collections.abc import Callable

from dsh_workers_mcp import brief, state as st
from dsh_workers_mcp.transport import HttpClient

DEFAULT_PROMPT = ("Continue the milestone. Call dsh_brief first and act on needsAction. "
                  "Read orchestration/REMINDER.md if you have lost the method.")
STOP_WORDS = ("DONE", "BLOCKED")


def milestone_stop(path: Path | None) -> str | None:
    """'DONE' or 'BLOCKED' when the first non-empty line of the milestone file starts with it."""
    if path is None or not path.is_file():
        return None
    for line in path.read_text(errors="replace").splitlines():
        if line.strip():
            head = line.strip().lstrip("#*- ").upper()
            return next((w for w in STOP_WORDS if head.startswith(w)), None)
    return None


def decide(needs: list[str] | None, *, last_needs: list[str] | None, fires: int,
           elapsed_hours: float, budget_hours: float, max_fires: int,
           milestone: str | None) -> tuple[str, str]:
    """Return (action, reason). The action is "fire", "skip" or "stop"."""
    if elapsed_hours >= budget_hours:
        return "stop", f"budget: {budget_hours:g} hours spent"
    if fires >= max_fires:
        return "stop", f"budget: {max_fires} wakes done"
    if milestone:
        return "stop", f"the milestone file says {milestone}"
    if needs is None:
        return "skip", "dsh_brief failed"
    if not needs:
        return "skip", "nothing needs the lead"
    if last_needs is not None and sorted(needs) == sorted(last_needs):
        return "skip", "the same items as at the last wake"
    return "fire", "; ".join(needs[:3]) + (f" (+{len(needs) - 3} more)" if len(needs) > 3 else "")


def log(path: Path, action: str, reason: str, **extra) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "action": action,
                                 "reason": reason, **extra}) + "\n")


def render(template: str, prompt: str, state_dir: Path) -> str:
    quoted_dir = shlex.quote(str(state_dir))
    return template.replace("{prompt}", shlex.quote(prompt)).replace("{state_dir}", quoted_dir)


def run(args: argparse.Namespace, *, get_needs: Callable[[], list[str] | None],
        runner: Callable[[str], int] = lambda cmd: subprocess.run(cmd, shell=True).returncode,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic) -> int:
    state_dir = Path(args.state_dir)
    wake_log = state_dir / "wake.jsonl"
    milestone = Path(args.milestone) if args.milestone else None
    started, fires, last_needs = clock(), 0, None
    while True:
        needs = get_needs()
        action, reason = decide(
            needs, last_needs=last_needs, fires=fires, elapsed_hours=(clock() - started) / 3600,
            budget_hours=args.budget_hours, max_fires=args.max_fires, milestone=milestone_stop(milestone))
        log(wake_log, action, reason, **({"dryRun": True} if args.dry_run else {}))
        if args.dry_run and action == "skip":
            print(f"dsh-wake: would skip ({reason})")
        if action == "stop":
            print(f"dsh-wake: stop ({reason})")
            return 0
        if action == "fire":
            command = render(args.cmd, args.prompt, state_dir)
            if args.dry_run:
                print(f"dsh-wake: would run: {command}")
            else:
                fires += 1
                last_needs = needs
                code = runner(command)
                log(wake_log, "ended", f"the command exited with {code}", exit=code)
        if args.dry_run:
            return 0
        sleep(args.interval)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dsh-wake", description=__doc__.split("\n\n")[0])
    p.add_argument("--cmd", required=True,
                   help="command template that starts the lead's client; "
                        "{prompt} and {state_dir} are filled in")
    p.add_argument("--prompt", default=DEFAULT_PROMPT, help="the resume prompt")
    p.add_argument("--state-dir", default=None,
                   help="orchestrator state directory (default: ./.orchestrator)")
    p.add_argument("--milestone",
                   help="a file whose first line starts with DONE or BLOCKED to stop the driver")
    p.add_argument("--interval", type=float, default=60.0, help="seconds between checks")
    p.add_argument("--budget-hours", type=float, default=8.0)
    p.add_argument("--max-fires", type=int, default=50)
    p.add_argument("--base", default=None, help="dsh base URL")
    p.add_argument("--dry-run", action="store_true", help="one pass; print the command, run nothing")
    args = p.parse_args(argv)
    args.state_dir = str(st.resolve_state_dir(args.state_dir))
    state = st.State(Path(args.state_dir), client=HttpClient(args.base) if args.base else None)

    def get_needs() -> list[str] | None:
        try:
            return brief.cmd_brief(state, {"live": True})["needsAction"]
        except Exception as exc:                      # a failed read is a skip, not a crash
            log(Path(args.state_dir) / "wake.jsonl", "error", f"{exc.__class__.__name__}: {exc}")
            return None

    return run(args, get_needs=get_needs)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
