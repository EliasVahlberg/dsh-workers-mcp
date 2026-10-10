"""Models: select a model for a session without changing the global default of dsh."""

from __future__ import annotations

import os
from pathlib import Path

from dsh_workers_mcp.transport import DshClient, RpcError

def settings_path() -> Path:
    return Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh") / "settings.yaml"


_DEFAULT_MODEL_KEYS = ("provider", "model", "reasoningEffort")


def default_model_lines(text: str) -> dict[str, str]:
    """The `agent-default-model:` block's own key lines, verbatim, by key."""
    found: dict[str, str] = {}
    inside = False
    for line in text.splitlines():
        if line.startswith("agent-default-model:"):
            inside = True
            continue
        if inside:
            if line and not line[0].isspace():
                break
            key = line.strip().split(":", 1)[0]
            if key in _DEFAULT_MODEL_KEYS and not line.strip().startswith("#"):
                found[key] = line
    return found


def describe_default(lines: dict[str, str]) -> str:
    values = {k: v.split(":", 1)[1].strip() for k, v in lines.items()}
    text = f"{values.get('provider', '?')}/{values.get('model', '?')}"
    if "reasoningEffort" in values:
        text += f" (reasoningEffort {values['reasoningEffort']})"
    return text


def restore_default_model(before: str) -> str | None:
    """
    Undo `session.selectModel`'s side effect on the global default.

    dsh persists every per-session pin as `agent-default-model` (api-proxy
    selectModel -> saveDefaultModelSelection, unconditional), so pinning one
    reviewer to another model silently re-defaults every later session. Only
    the block's key lines are put back; the rest of the file, including any
    edit made meanwhile, is left as dsh wrote it.

    Even a pin that matches the default changes the file: dsh saves the
    resolved selection, which adds `reasoningEffort` for models that have one.
    Returns what dsh had written when a restore happened, else None.
    """
    path = settings_path()
    after = path.read_text()
    old, new = default_model_lines(before), default_model_lines(after)
    if old == new or not old:
        return None
    out: list[str] = []
    inside, pending = False, dict(old)
    for line in after.splitlines(keepends=True):
        if line.startswith("agent-default-model:"):
            inside = True
        elif inside and line.strip() and not line[0].isspace():
            # Leaving the block: keys dsh dropped go back at its end.
            out.extend(v + "\n" for v in pending.values())
            pending, inside = {}, False
        elif inside and not line.strip().startswith("#"):
            key = line.strip().split(":", 1)[0]
            if key in _DEFAULT_MODEL_KEYS:
                # Replace in place, so the block keeps its order and comments.
                if key in pending:
                    out.append(pending.pop(key) + "\n")
                continue
        out.append(line)
    if inside:
        out.extend(v + "\n" for v in pending.values())
    path.write_text("".join(out))
    if default_model_lines(path.read_text()) != old:
        raise RpcError(f"could not restore agent-default-model in {path}; check it by hand")
    return describe_default(new)


def select_model(session_id: str, client: DshClient, provider: str | None = None,
                 model: str | None = None, effort: str | None = None) -> dict:
    """
    Pin a session's model and/or reasoning effort, then undo dsh's side effect.

    `session.selectModel` needs provider and model, so an effort-only change
    re-sends the session's current ones. dsh saves every selection as the
    global default (see restore_default_model); this puts the default back.
    """
    if not (provider and model):
        current = client.rpc("session.models", {"sessionId": session_id}).get("current") or {}
        provider, model = provider or current.get("provider"), model or current.get("model")
        if not (provider and model):
            raise RpcError("cannot tell this session's current model; pass provider and model")
    payload = {"sessionId": session_id, "provider": provider, "model": model}
    if effort:
        payload["reasoningEffort"] = effort
    try:
        before = settings_path().read_text()
    except OSError:
        before = None
    client.rpc("session.selectModel", payload)
    selected = client.rpc("session.models", {"sessionId": session_id}).get("current") or {}
    out = {"selectedModel": f"{selected.get('provider')}/{selected.get('model')}",
           "reasoningEffort": selected.get("reasoningEffort")}
    if effort and selected.get("reasoningEffort") != effort:
        out["warning"] = f"asked for reasoning effort {effort!r}, session reports {out['reasoningEffort']!r}"
    if before is None:
        out["defaultModel"] = f"not checked: cannot read {settings_path()}"
    else:
        wrote = restore_default_model(before)
        kept = describe_default(default_model_lines(before))
        out["defaultModel"] = (f"unchanged: {kept}" if wrote is None else
                               f"restored to {kept} (dsh had saved the pin as the default: {wrote})")
    return out


