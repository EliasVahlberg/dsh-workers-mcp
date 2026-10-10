"""
A mechanical digest of what a worker did: files touched, commands run, checks
and their outcome, errors and refusals, and (when the task has a worktree) what
changed in git since the dispatch.

No model is involved. The digest is computed from the event log the server
already mirrors, so it is deterministic, free, and cannot invent. It exists
because a lead's job on a return is to verify, and the things that matter (no
check ran after the last edit, a write outside the worktree, a refused call) are
what a prose summary tends to smooth over.

Two kinds of evidence, kept apart:

* Tool calls are exact. An `edit` or `write` call names its file.
* Shell commands are parsed, which is a heuristic. A command is split on `&&`,
  `;` and pipes, wrappers (`cd`, `timeout`, `env`) are skipped, and each piece
  is checked for redirects, in-place editors and the like. Scripts whose intent
  is in their body (`python -c`, heredocs) are reported as `opaque`, not
  guessed. For "what changed", git (see `git_changes`) is the better witness:
  it sees the result whatever the command was.

Stdlib only, like the rest of the package.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

# ------------------------------------------------------------------ tool names

# dsh tools that change a named file, and the argument that names it.
EDIT_TOOLS = {"edit", "write", "multiedit", "notebook_edit", "apply_patch"}
PATH_KEYS = ("file_path", "path", "filePath", "notebook_path")

# --------------------------------------------------------------- shell parsing

SHELLS = {"bash", "sh", "zsh", "dash"}
INTERPRETERS = {"python", "perl", "ruby", "node", "php", "lua"}
WRAPPERS = {"time", "nohup", "nice", "ionice", "command", "exec", "sudo", "env",
            "timeout", "stdbuf", "setsid"}
WRITE_VERBS = {"mv", "cp", "rm", "rmdir", "mkdir", "touch", "tee", "patch", "truncate",
               "ln", "dd", "install", "chmod", "chown", "unlink", "rsync", "unzip", "wget"}
# `git` subcommands that rewrite the working tree.
GIT_TREE_WRITES = {"apply", "checkout", "restore", "reset", "clean", "stash", "merge",
                   "rebase", "pull", "cherry-pick", "rm", "mv", "revert", "am", "switch"}
# A command that only reads is never a check, even if its text mentions one.
READ_ONLY_VERBS = {"cat", "head", "tail", "less", "more", "ls", "grep", "rg", "find", "wc",
                   "echo", "printf", "sed", "awk", "diff", "stat", "file", "git", "pwd",
                   "cd", "which", "type", "sort", "uniq", "cut", "tr", "xxd", "od"}

# Tools that are checks by themselves (verb match) ...
CHECK_VERBS = {"pytest", "py.test", "tox", "nox", "ruff", "flake8", "mypy", "pylint",
               "eslint", "tsc", "shellcheck", "ctest", "jest", "vitest", "rspec", "phpunit"}
# ... and compound commands, anchored at the start of one command.
CHECK_PATTERNS = [
    r"^python\S*\s+-m\s+(pytest|unittest)\b",
    r"^make\b.*\b(test|tests|check|verify|lint|pytest|unit|e2e)\b",
    r"^(\./)?(gradlew?|mvnw?|ant)\b.*\b(test|check|verify)\b",
    r"^(npm|yarn|pnpm|bun)\s+(run\s+)?(test|lint|check)\b",
    r"^cargo\s+(test|clippy|check)\b",
    r"^go\s+(test|vet)\b",
]

_ENV_ASSIGN = re.compile(r"[A-Za-z_]\w*=.*")
_HEREDOC = re.compile(r"(?<!<)<<(?!<)-?\s*(['\"]?)([A-Za-z_][\w.-]*)\1")
_REDIRECT = {">", ">>", ">|", "&>", "&>>"}
_OPERATOR_CHARS = set("();&|")
_SED_INPLACE = re.compile(r"-[nEsrzue]*i[nEsrzue]*(\.\S+)?")
_PERL_INPLACE = re.compile(r"-[pnlaswWe0]*i[pnlaswWe0]*(\.\S+)?")
_CURL_OUT = re.compile(r"-[A-Za-z]*[oO]")
_EXIT_LINE = re.compile(r"\n?\[exit code: (-?\d+)\]\s*$")
_JOB_STATUS = re.compile(r"\[status: ([^,\]]+)(?:, exit code: (-?\d+))?\]\s*$")
_JOB_STARTED = re.compile(r"started background job (\S+)")


def _unq(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"":
        return token[1:-1]
    return token


def _flatten(command: str) -> str:
    """Drop heredoc bodies and join the lines, so one tokenizer pass sees it all."""
    out: list[str] = []
    pending: list[str] = []
    for line in command.split("\n"):
        if pending:
            if line.strip() == pending[0]:
                pending.pop(0)
            continue
        out.append(line)
        pending.extend(m.group(2) for m in _HEREDOC.finditer(line))
    return " ; ".join(line for line in out if line.strip())


def _tokens(text: str) -> list[str]:
    lex = shlex.shlex(text, posix=False, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        return list(lex)
    except ValueError:  # an unbalanced quote: fall back to whitespace
        return text.split()


def _segments(tokens: list[str]) -> list[list[str]]:
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token and set(token) <= _OPERATOR_CHARS:
            segments.append([])
        else:
            segments[-1].append(token)
    return [s for s in segments if s]


def _split_redirects(tokens: list[str]) -> tuple[list[str], bool, bool]:
    """Words, whether a redirect writes a file, whether a heredoc feeds stdin."""
    words: list[str] = []
    writes = heredoc = False
    i = 0
    while i < len(tokens):
        token = tokens[i]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        if token in _REDIRECT:
            if words and words[-1].isdigit():
                words.pop()  # the "2" of 2>file
            if nxt and nxt not in ("/dev/null", "/dev/stderr", "/dev/stdout") \
                    and not nxt.startswith("&"):
                writes = True
            i += 2
        elif token == ">&":
            if words and words[-1].isdigit():
                words.pop()
            if nxt and not (nxt.isdigit() or nxt == "-") and nxt != "/dev/null":
                writes = True
            i += 2
        elif token in ("<", "<<", "<<<", "<<-"):
            heredoc = heredoc or token.startswith("<<") and token != "<<<"
            i += 2
        else:
            words.append(token)
            i += 1
    return words, writes, heredoc


def _unwrap(words: list[str]) -> list[str]:
    i = 0
    while i < len(words):
        word = _unq(words[i])
        if _ENV_ASSIGN.fullmatch(word):
            i += 1
            continue
        base = PurePosixPath(word).name
        if base not in WRAPPERS:
            break
        i += 1
        while i < len(words):
            nxt = _unq(words[i])
            if base == "timeout" and nxt in ("-s", "-k", "--signal", "--kill-after"):
                i += 2
            elif nxt.startswith("-") or _ENV_ASSIGN.fullmatch(nxt) and base == "env" \
                    or base == "timeout" and re.fullmatch(r"\d+(\.\d+)?[smhd]?", nxt) \
                    or base in ("nice", "ionice") and nxt.isdigit():
                i += 1
            else:
                break
    return words[i:]


def _git_subcommand(args: list[str]) -> str | None:
    i = 0
    while i < len(args):
        if args[i] in ("-C", "-c", "--git-dir", "--work-tree"):
            i += 2
        elif args[i].startswith("-"):
            i += 1
        else:
            return args[i]
    return None


def _classify(verb: str, args: list[str], heredoc: bool) -> tuple[bool, bool]:
    """(writes, opaque) for one simple command. `args` are unquoted."""
    if verb in WRITE_VERBS:
        return verb != "wget" or "--spider" not in args, False
    if verb == "sed":
        return any(a == "--in-place" or a.startswith("--in-place=")
                   or _SED_INPLACE.fullmatch(a) for a in args), False
    if verb == "perl" and any(_PERL_INPLACE.fullmatch(a) for a in args):
        return True, False
    if verb == "find":
        if "-delete" in args:
            return True, False
        if "-exec" in args:
            after = args[args.index("-exec") + 1:]
            return bool(after) and (PurePosixPath(after[0]).name in WRITE_VERBS
                                    or after[0] == "sed"), False
    if verb == "curl":
        return any(_CURL_OUT.fullmatch(a) or a in ("--output", "--remote-name")
                   for a in args), False
    if verb == "tar":
        return bool(args) and bool(re.fullmatch(r"-?[A-Za-z]*x[A-Za-z]*", args[0])), False
    if verb == "git":
        sub = _git_subcommand(args)
        if sub == "stash":
            rest = args[args.index("stash") + 1:]
            return not (rest and rest[0] in ("list", "show")), False
        return sub in GIT_TREE_WRITES, False
    if verb == "xargs":
        rest = [a for a in args if not a.startswith("-")]
        return (_classify(PurePosixPath(rest[0]).name, rest[1:], False)
                if rest else (False, False))
    if verb in SHELLS:
        if "-c" in args and args.index("-c") + 1 < len(args):
            inner = analyze(args[args.index("-c") + 1])
            return any(s["writes"] for s in inner), any(s["opaque"] for s in inner)
        return False, heredoc or "-" in args
    base = re.sub(r"[\d.]+$", "", verb)
    if base in INTERPRETERS:
        return False, heredoc or "-c" in args or "-e" in args or "-" in args
    return False, False


def analyze(command: str) -> list[dict]:
    """
    Split a shell command into simple commands and describe each:
    `verb`, `text` (verb and arguments), `writes` (changes files: a redirect, an
    in-place editor, mv, rm, a git command that rewrites the tree...) and
    `opaque` (a script whose body decides: python -c, a heredoc to an interpreter).
    A heuristic: the tests show what it does and does not catch.
    """
    result: list[dict] = []
    for segment in _segments(_tokens(_flatten(command))):
        words, redirect_writes, heredoc = _split_redirects(segment)
        words = _unwrap(words)
        if not words or words[0].startswith("#"):
            continue
        verb = PurePosixPath(_unq(words[0])).name
        args = [_unq(w) for w in words[1:]]
        writes, opaque = _classify(verb, args, heredoc)
        result.append({"verb": verb, "text": " ".join([verb, *args]),
                       "raw": " ".join([_unq(words[0]), *args]),
                       "writes": writes or redirect_writes, "opaque": opaque})
    return result


def check_spans(acceptance: str | None) -> list[str]:
    """
    The commands an acceptance line names: backticked spans of two or more words
    whose first word is not a file name. "`make test` passes" gives ["make test"].
    """
    spans = []
    for span in re.findall(r"`([^`]+)`", acceptance or ""):
        span = " ".join(span.split())
        first = span.split(" ", 1)[0]
        if " " in span and not (re.search(r"\.\w+$", first) and "/" not in first):
            spans.append(span)
    return spans


def check_via(segment: dict, patterns: list[str], spans: list[str]) -> str | None:
    """Why this command counts as a check ("acceptance", "pattern"), or None."""
    verb, text = segment["verb"], segment["text"]
    if verb in READ_ONLY_VERBS:
        return None
    if any(span in text for span in spans):
        return "acceptance"
    if verb in CHECK_VERBS or any(re.search(p, text) for p in CHECK_PATTERNS) \
            or any(re.search(p, segment.get("raw", text)) for p in patterns):
        return "pattern"
    return None


# ------------------------------------------------------------------ the digest

MAX_LIST = 12
TAIL_CHARS = 140
CMD_CHARS = 140


def _text(content: list) -> str:
    parts = []
    for item in content or []:
        for inner in item.get("content", []) if isinstance(item, dict) else []:
            if isinstance(inner, dict) and inner.get("type") == "text":
                parts.append(inner.get("text", ""))
    return "".join(parts)


def _is_error(content: list) -> bool:
    return any(isinstance(i, dict) and i.get("isError") for i in content or [])


_FAILED_OUTPUT = re.compile(
    r"\b[1-9]\d* (failed|errors?)\b|^FAILED\b|\*\*\* .*Error \d+|BUILD FAILED|^FAIL\b",
    re.MULTILINE)


def _output_failed(text: str) -> bool:
    """Does the end of a check's output say it failed? (The exit status may be hidden.)"""
    return bool(_FAILED_OUTPUT.search("\n".join(text.strip().splitlines()[-25:])))


def _last_line(text: str) -> str:
    for line in reversed(text.strip().splitlines()):
        if line.strip() and not line.startswith("[exit code:") and not line.startswith("[status:"):
            return line.strip()[:TAIL_CHARS]
    return ""


def _short(text: str, limit: int = CMD_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


_CD_PREFIX = re.compile(r"^\s*(cd\s+\S+\s*(&&|;)\s*)+")
_NOISE_VERBS = {"cd", "pwd", "true", ":", "set", "export", ""}


def _display(command: str, limit: int = CMD_CHARS) -> str:
    """A command for a human: heredoc bodies and a leading `cd <dir> &&` removed."""
    return _short(_CD_PREFIX.sub("", _flatten(command)), limit)


def _common_dir(paths: list[str]) -> str | None:
    """The directory shared by all absolute paths, shown once instead of per path."""
    absolute = [PurePosixPath(p).parent for p in paths if p.startswith("/")]
    if len(absolute) != len(paths) or len(paths) < 2:
        return None
    common = Path(*[p.parts for p in absolute][0])
    for parent in absolute[1:]:
        while common != common.parent and common not in (parent, *parent.parents):
            common = common.parent
    return str(common) if len(common.parts) > 2 else None


def _rel(path: str, root: str | None) -> str:
    if root and path.startswith(root.rstrip("/") + "/"):
        return path[len(root.rstrip("/")) + 1:]
    return path


@dataclass
class _Fold:
    """What the tool calls of a window add up to."""

    tools: dict[str, int] = field(default_factory=dict)
    edited: dict[str, int] = field(default_factory=dict)
    errors: list[dict] = field(default_factory=list)
    refused: int = 0
    commands: list[dict] = field(default_factory=list)
    goals: list[dict] = field(default_factory=list)
    jobs: dict[str, str] = field(default_factory=dict)       # background job id -> callId
    job_done: dict[str, tuple[str, int | None]] = field(default_factory=dict)


def _turn_window(events: list[dict], turns: int) -> tuple[int, int, list[dict]] | None:
    """The events of the last `turns` numbered turns, as (first turn, last turn, events)."""
    numbered = [n for n in (e.get("data", {}).get("turn") for e in events) if isinstance(n, int)]
    if not numbered:
        return None
    first = max(min(numbered), max(numbered) - max(turns, 1) + 1)
    window = [e for e in events if isinstance(e.get("data", {}).get("turn"), int)
              and e["data"]["turn"] >= first]
    return first, max(numbered), window


def _index_events(window: list[dict]) -> tuple[dict[str, dict], dict[str, dict], int, str | None]:
    """The calls and results of a window by call id, the number of steps and how the last turn ended."""
    calls: dict[str, dict] = {}
    results: dict[str, dict] = {}
    steps = 0
    ended = None
    for event in window:
        data, kind = event["data"], event["type"]
        if kind == "tool/call":
            calls[data["callId"]] = {**data, "seq": event["seq"]}
        elif kind == "tool/result":
            message = data.get("message", {})
            cid: Any = (message.get("source") or {}).get("callId")
            content = message.get("content", [])
            results[cid] = {"text": _text(content), "error": _is_error(content)
                            or bool(data.get("error"))}
        elif kind == "step/start":
            steps += 1
        elif kind == "turn/end":
            ended = ((data.get("reason") or {}).get("kind")) or ended
    return calls, results, steps, ended


def _call_arguments(call: dict) -> dict:
    try:
        return json.loads(call.get("arguments") or "{}")
    except ValueError:
        return {}


def _fold_calls(calls: dict[str, dict], results: dict[str, dict], worktree: str | None) -> _Fold:
    fold = _Fold()
    for cid, call in calls.items():
        name, result = call["name"], results.get(cid, {"text": "", "error": False})
        fold.tools[name] = fold.tools.get(name, 0) + 1
        args = _call_arguments(call)
        if result["error"]:
            _fold_error(fold, call, result)
        if name in EDIT_TOOLS and not result["error"]:
            _fold_edit(fold, call, args, worktree)
        elif name == "bash" and not result["error"]:
            _fold_bash(fold, cid, call, args, result)
        elif name == "create_goal" and not result["error"]:
            _fold_goal(fold, call, args)
        elif name == "job_output":
            _fold_job_output(fold, call, args, result)
    return fold


def _fold_error(fold: _Fold, call: dict, result: dict) -> None:
    fold.refused += 1 if "[tool-policy" in result["text"] else 0
    fold.errors.append({"tool": call["name"], "seq": call["seq"], "text": _short(result["text"], 160)})


def _fold_edit(fold: _Fold, call: dict, args: dict, worktree: str | None) -> None:
    path = next((args[k] for k in PATH_KEYS if isinstance(args.get(k), str)), None)
    if path:
        key = _rel(path, worktree)
        fold.edited[key] = fold.edited.get(key, 0) + 1
        fold.commands.append({"seq": call["seq"], "edit": True})


def _fold_bash(fold: _Fold, cid: str, call: dict, args: dict, result: dict) -> None:
    started = _JOB_STARTED.search(result["text"])
    if started:
        fold.jobs[started.group(1)] = cid
    fold.commands.append({
        "seq": call["seq"], "cid": cid, "command": args.get("command", ""),
        "segments": analyze(args.get("command", "")), "text": result["text"],
        "background": started.group(1) if started else None})


def _fold_goal(fold: _Fold, call: dict, args: dict) -> None:
    # A goal starts autonomous rounds after the turn ends (see the skill).
    # The lead creates goals with the goal RPC, which leaves no tool call,
    # so a create_goal call in the log is a goal the worker made itself.
    fold.goals.append({"seq": call["seq"], "objective": _short(str(args.get("objective", "")), 120),
                       **({"maxGoalRounds": args["max_goal_rounds"]}
                          if isinstance(args.get("max_goal_rounds"), int) else {})})


def _fold_job_output(fold: _Fold, call: dict, args: dict, result: dict) -> None:
    done = _JOB_STATUS.search(result["text"])
    if done and args.get("job_id"):
        fold.job_done[args["job_id"]] = (done.group(1), int(done.group(2)) if done.group(2) else None)
        fold.jobs.setdefault(args["job_id"], "")
        fold.commands.append({"seq": call["seq"], "job": args["job_id"], "text": result["text"]})


def _verb_counts(shell: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for c in shell:
        for s in c["segments"]:
            verb = re.sub(r"[\d.]+$", "", s["verb"])
            verb = verb if verb in INTERPRETERS else s["verb"]      # python3.11 -> python
            if verb not in _NOISE_VERBS and verb.replace("-", "").replace(".", "").isalnum():
                counts[verb] = counts.get(verb, 0) + 1
    return counts


def _job_result(c: dict, fold: _Fold) -> tuple[int | None, str, str]:
    """(exit code, status, output) of a command that ran as a background job."""
    state, code = fold.job_done.get(c["background"], ("running", None))
    status = ("ok" if state == "completed" and code == 0
              else "running" if state == "running" else "failed")
    # the finished job's output is the last line worth showing
    finished = next((j for j in fold.commands if j.get("job") == c["background"]
                     and _JOB_STATUS.search(j["text"])), None)
    return code, status, finished["text"] if finished else c["text"]


def _command_result(c: dict, fold: _Fold) -> tuple[int | None, str, str]:
    """(exit code, status, output) of one shell command. A background job is followed to its end."""
    if c["background"]:
        return _job_result(c, fold)
    m = _EXIT_LINE.search(c["text"])
    if m:
        return int(m.group(1)), "failed" if int(m.group(1)) else "ok", c["text"]
    return None, "ok", c["text"]


def _check_entry(c: dict, exit_code: int | None, status: str) -> dict:
    entry = {"cmd": _display(c["command"], 100),
             "ok": status == "ok" if status != "running" else None, "seq": c["seq"]}
    if exit_code not in (None, 0):
        entry["exit"] = exit_code
    return entry


def _checks_and_failures(shell: list[dict], fold: _Fold, patterns: list[str],
                         spans: list) -> tuple[list[dict], list[dict]]:
    checks: list[dict] = []
    failures: list[dict] = []
    for c in shell:
        via = next((v for v in (check_via(s, patterns, spans) for s in c["segments"]) if v), None)
        exit_code, status, text = _command_result(c, fold)
        entry = _check_entry(c, exit_code, status)
        if via:
            # A pipeline (`make test | tail`) reports the last command's status, so
            # a failing run can come back without an exit line. The output says so.
            if status == "ok" and _output_failed(text):
                entry.update(ok=False, outputFailed=True)
            entry.update(via=via, tail=_last_line(text))
            checks.append(entry)
        elif status == "failed":
            entry["tail"] = _last_line(text)
            failures.append(entry)
    return checks, failures


def _verification(checks: list[dict], writes: list[dict], commands: list[dict]) -> dict:
    last_edit = max([c["seq"] for c in writes] + [c["seq"] for c in commands if c.get("edit")],
                    default=None)
    if not checks:
        return {"checkedAfterLastEdit": False if last_edit is not None else None,
                "note": "no command matched as a check"}
    after = [c for c in checks if last_edit is None or c["seq"] > last_edit]
    return {"checkedAfterLastEdit": bool(after) if last_edit is not None else None,
            "lastCheckOk": checks[-1]["ok"]}


def _cap(items: list, n: int = MAX_LIST) -> list:
    return items[:n] + ([f"+{len(items) - n} more"] if len(items) > n else [])


def _optional_sections(digest: dict, fold: _Fold, writes: list[dict], opaque: list[dict],
                       failures: list[dict], worktree: str | None) -> None:
    """Add the parts of a digest that exist only when there is something to say."""
    if writes:
        digest["shell"]["writeCommands"] = _cap([_display(c["command"], 100) for c in writes], 3)
    if opaque:
        digest["shell"]["opaqueCommands"] = _cap([_display(c["command"], 100) for c in opaque], 3)
    if failures:
        digest["failed"] = failures[-3:]
    _findings(digest, fold, worktree)


def _findings(digest: dict, fold: _Fold, worktree: str | None) -> None:
    if fold.errors:
        digest["errors"] = {"count": len(fold.errors), "refused": fold.refused,
                            "last": fold.errors[-2:]}
    if fold.goals:
        digest["goalsCreatedByWorker"] = fold.goals[-3:]
    outside = sorted(p for p in fold.edited if p.startswith("/")) if worktree else []
    if outside:
        digest["editedOutsideWorktree"] = _cap(outside)


def _edited_view(fold: _Fold, worktree: str | None) -> dict:
    """The `root` and `edited` entries: paths relative to the worktree, or to their common root."""
    root = None if worktree else _common_dir(list(fold.edited))
    shown = [(_rel(p, root) if root else p, n) for p, n in fold.edited.items()]
    return {**({"root": root} if root else {}),
            "edited": _cap([f"{p} x{n}" if n > 1 else p for p, n in shown], 10)}


def build(events: list[dict], *, turns: int = 1, worktree: str | None = None,
          acceptance: str | None = None, patterns: list[str] | None = None) -> dict:
    """
    Digest the last `turns` turns of a worker's mirrored events (oldest first).
    `worktree` is the task's worktree, `acceptance` its acceptance text, and
    `patterns` extra regexes (from the project) that mark a command as a check.
    """
    span = _turn_window(events, turns)
    if span is None:
        return {"turns": None, "note": "no turns in the mirrored history yet"}
    first_turn, last_turn, window = span
    calls, results, steps, ended = _index_events(window)
    fold = _fold_calls(calls, results, worktree)

    shell = [c for c in fold.commands if "segments" in c]
    writes = [c for c in shell if any(s["writes"] for s in c["segments"])]
    opaque = [c for c in shell if any(s["opaque"] for s in c["segments"])]
    checks, failures = _checks_and_failures(shell, fold, patterns or [], check_spans(acceptance))

    digest: dict = {
        "turns": [first_turn, last_turn], "ended": ended, "steps": steps,
        "tools": dict(sorted(fold.tools.items(), key=lambda kv: -kv[1])),
        **_edited_view(fold, worktree),
        "shell": {"commands": len(shell), "writes": len(writes), "opaque": len(opaque),
                  "byVerb": dict(sorted(_verb_counts(shell).items(), key=lambda kv: -kv[1])[:8])},
        "checks": checks[-4:], "verification": _verification(checks, writes, fold.commands),
    }
    _optional_sections(digest, fold, writes, opaque, failures, worktree)
    return digest


# ------------------------------------------------------------------------- git

def _git(worktree: str, *args: str) -> str | None:
    try:
        done = subprocess.run(["git", "-C", worktree, *args], capture_output=True,
                              text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def git_snapshot(worktree: str | None) -> dict | None:
    """Where a worktree stands, recorded at dispatch so the return can be compared."""
    if not worktree or not Path(worktree).is_dir():
        return None
    head = _git(worktree, "rev-parse", "HEAD")
    if head is None:
        return None
    return {"head": head,
            "dirty": sorted((_git(worktree, "diff", "--name-only", "HEAD") or "").splitlines())[:200],
            "untracked": sorted((_git(worktree, "ls-files", "--others",
                                      "--exclude-standard") or "").splitlines())[:200]}


def git_changes(worktree: str | None, before: dict | None) -> dict | None:
    """
    What changed in the worktree since `before` (a `git_snapshot`): commits,
    files, and line counts. It sees edits made by any means, which a parsed shell
    command cannot promise. Build output is invisible (it is ignored by git).
    """
    if not worktree or not before or not Path(worktree).is_dir():
        return None
    head = _git(worktree, "rev-parse", "HEAD")
    if head is None:
        return None
    commits = _git(worktree, "rev-list", "--count", f"{before['head']}..HEAD")
    tracked = set((_git(worktree, "diff", "--name-only", before["head"]) or "").splitlines())
    untracked = set((_git(worktree, "ls-files", "--others", "--exclude-standard") or "")
                    .splitlines()) - set(before.get("untracked", []))
    stat = _git(worktree, "diff", "--shortstat", before["head"]) or ""
    numbers = {k: int(m.group(1)) for k, m in (
        ("insertions", re.search(r"(\d+) insertion", stat)),
        ("deletions", re.search(r"(\d+) deletion", stat))) if m}
    uncommitted = len((_git(worktree, "status", "--porcelain") or "").splitlines())
    files = sorted(tracked | untracked)
    return {"head": head[:10], "commits": int(commits) if commits and commits.isdigit() else None,
            "files": files[:MAX_LIST] + ([f"+{len(files) - MAX_LIST} more"]
                                         if len(files) > MAX_LIST else []),
            "filesChanged": len(files), **numbers, "uncommitted": uncommitted}
