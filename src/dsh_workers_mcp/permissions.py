"""Permission presets: read and set a session's preset, and the tool-policy overlay patch."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from dsh_workers_mcp.transport import DshClient, RpcError

def execute_command(session_id: str, line: str, client: DshClient) -> dict:
    """
    Run a host slash command against one session, without sending it to the model.

    This is the second RPC family (typert remotes): POST /api/<namespace>/<method>
    with the payload wrapped in exactly one `args` object. Sending the same line
    through `session.prompt` does NOT route it as a command -- the model receives
    it as ordinary text and starts a turn. Verified the hard way.
    """
    body = {"type": "client-request", "rpcId": f"cmd-{uuid.uuid4()}",
            "method": "commands/execute",
            "payload": {"args": {"agentId": session_id, "line": line, "images": []}}}
    envelope = client.post("/api/commands/execute", body, "commands/execute")
    result = envelope.get("result") or {}
    if not result.get("ok"):
        raise RpcError(f"commands/execute: {json.dumps(result.get('error'))}")
    value = result.get("value")
    if value is None:
        raise RpcError(f"{line.split()[0]!r} is not a registered command in this deployment")
    return value["result"]


_PRESET_LINE = re.compile(r"current preset (\S+) \(available: ([^)]*)\)")


def get_permission(session_id: str, client: DshClient) -> dict:
    text = execute_command(session_id, "/permission", client).get("text", "")
    match = _PRESET_LINE.search(text)
    if not match:
        raise RpcError(f"unexpected /permission reply: {text!r}")
    return {"preset": match.group(1),
            "available": [n.strip() for n in match.group(2).split(",") if n.strip()]}


def set_permission(session_id: str, preset: str, client: DshClient) -> dict:
    before = get_permission(session_id, client)
    if preset not in before["available"]:
        raise RpcError(f"unknown preset {preset!r}; available: {before['available']}")
    result = execute_command(session_id, f"/permission {preset}", client)
    if result.get("kind") == "error":
        raise RpcError(f"/permission {preset}: {result.get('text')}")
    after = get_permission(session_id, client)
    # Read back rather than trusting the command's success text: the preset a
    # session *reports* is derived from its knobs, so a mismatch here would mean
    # the switch did not take.
    if after["preset"] != preset:
        raise RpcError(f"switched to {preset!r} but session now reports {after['preset']!r}")
    return {"previous": before["preset"], **after}


def _overlay_dir() -> Path:
    """Installed: inside the package. From a checkout: overlays/ at the repository root."""
    here = Path(__file__).resolve().parent
    candidates = [here / "overlays" / "tool-policy", here.parent.parent / "overlays" / "tool-policy"]
    return next((c for c in candidates if (c / "policy.patch.yml").is_file()), candidates[0])


OVERLAY_DIR = _overlay_dir()


def render_policy_patch() -> str:
    """The tool-policy patch with the plugin's absolute path filled in (dsh needs one)."""
    template = OVERLAY_DIR / "policy.patch.yml"
    if not template.is_file():
        raise RpcError(f"the tool-policy overlay is not part of this installation ({template})")
    path = str(OVERLAY_DIR).replace("'", "''")      # the template single-quotes it (YAML)
    return template.read_text(encoding="utf-8").replace("@PLUGIN_DIR@", path)


