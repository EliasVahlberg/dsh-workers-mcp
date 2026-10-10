"""
Shape check for the dsh API.

dsh does not report a usable version (see `transport.py`), so drift is detected
by shape. `REQUIRED` lists, for each RPC method that this package calls, the
fields that the code reads and their types. `check` compares a response with it.

* `CheckedClient` wraps a `DshClient`. It checks the first responses of each
  method, records a problem in `drift` and never raises, so a changed API does
  not stop a working call. `dsh_brief` and `dshctl` report the drift.
* `skeleton` reduces a value to its keys and types, with no content. The recorder
  (`scripts/record-shapes.py`) stores skeletons of real responses in
  `contracts/dsh-shapes.json`. A test checks that `REQUIRED` fits the recording and
  fits the fake that the tests use.

A type is one of `str`, `int`, `float`, `bool`, `null`, `any`, or `A|B` for a field
that can have either type. A dict means the key must exist and hold a dict with
those keys. A list of one element means a list whose elements must fit it.
"""

from __future__ import annotations

from typing import Any

from dsh_workers_mcp.transport import DshClient

ITEMS_SEEN = 50       # list elements merged into one skeleton
CHECKS_PER_METHOD = 3  # responses checked for each method

REQUIRED: dict[str, Any] = {
    "session.list": {"items": [{"sessionId": "str", "running": "bool", "projections": {"values": {}}}]},
    "session.history": {"events": [{"event": {"seq": "int", "type": "str"}}], "hasMore": "bool"},
    "session.models": {"current": {}},
    "session.create": {"sessionId": "str"},
    "session.fork": {"sessionId": "str"},
    "session.rename": {"title": "str", "seq": "int"},
    "session.cancel": {"accepted": "bool"},
}

_TYPES = {str: "str", bool: "bool", int: "int", float: "float", type(None): "null"}


def skeleton(value: Any) -> Any:
    """The keys and types of `value`, with no content."""
    if isinstance(value, dict):
        return {key: skeleton(item) for key, item in value.items()}
    if isinstance(value, list):
        merged: Any = None
        for item in value[:ITEMS_SEEN]:
            merged = _merge(merged, skeleton(item))
        return [] if merged is None else [merged]
    return _TYPES.get(type(value), "any")


def _merge(a: Any, b: Any) -> Any:
    """Union of two skeletons: the keys of both, and the types of both."""
    if a is None or b is None:
        return b if a is None else a
    for kind, join in ((dict, _merge_dicts), (list, _merge_lists), (str, _merge_types)):
        if isinstance(a, kind) and isinstance(b, kind):
            return join(a, b)
    return "any"


def _merge_dicts(a: Any, b: Any) -> Any:
    return {key: _merge(a.get(key), b.get(key)) for key in {**a, **b}}


def _merge_lists(a: Any, b: Any) -> Any:
    if not (a or b):
        return []
    return [_merge(a[0] if a else None, b[0] if b else None)]


def _merge_types(a: Any, b: Any) -> Any:
    return "|".join(sorted(set(a.split("|")) | set(b.split("|"))))


def conforms(found: Any, required: Any, path: str = "") -> list[str]:
    """Problems between a skeleton and a requirement. An empty list means it fits."""
    if isinstance(required, dict):
        return _conform_dict(found, required, path)
    if isinstance(required, list):
        return _conform_list(found, required, path)
    return _conform_type(found, required, path)


def _conform_dict(found: Any, required: dict, path: str) -> list[str]:
    if not isinstance(found, dict):
        return [f"{path.rstrip('.') or 'value'} should be an object"]
    problems: list[str] = []
    for key, wanted in required.items():
        if key in found:
            problems += conforms(found[key], wanted, f"{path}{key}.")
        else:
            problems.append(f"{path}{key} is missing")
    return problems


def _conform_list(found: Any, required: list, path: str) -> list[str]:
    here = path.rstrip(".")
    if not isinstance(found, list):
        return [f"{here or 'value'} should be a list"]
    if found and required:
        return conforms(found[0], required[0], f"{here}[].")
    return []


def _conform_type(found: Any, required: str, path: str) -> list[str]:
    if required == "any":
        return []
    parts = set(found.split("|")) if isinstance(found, str) else {"object"}
    if parts <= set(required.split("|")):
        return []
    return [f"{path.rstrip('.')} is {'|'.join(sorted(parts))}, expected {required}"]


def check(method: str, value: Any) -> list[str]:
    """Problems in one response, each prefixed with the method. Unknown methods give none."""
    if method not in REQUIRED:
        return []
    return [f"{method}: {problem}" for problem in conforms(skeleton(value), REQUIRED[method])]


class CheckedClient:
    """A `DshClient` that checks the shape of the first responses of each method."""

    def __init__(self, inner: DshClient) -> None:
        self.inner = inner
        self.base = inner.base
        self.drift: dict[str, list[str]] = {}
        self._seen: dict[str, int] = {}

    def rpc(self, method: str, payload: dict | None = None, timeout: float = 120.0) -> dict:
        value = self.inner.rpc(method, payload, timeout)
        if method in REQUIRED and self._seen.get(method, 0) < CHECKS_PER_METHOD:
            self._seen[method] = self._seen.get(method, 0) + 1
            problems = check(method, value)
            if problems:
                self.drift[method] = problems
        return value

    def post(self, path: str, body: dict, label: str, timeout: float = 30.0) -> dict:
        return self.inner.post(path, body, label, timeout)

    def stream(self, path: str, settle: float = 1.5, max_seconds: float = 8.0) -> list[dict]:
        return self.inner.stream(path, settle, max_seconds)

    def drift_lines(self) -> list[str]:
        return [problem for problems in self.drift.values() for problem in problems]
