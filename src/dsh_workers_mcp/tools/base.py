"""
The tool registry: what a tool is, and the helpers that its handler uses.

A tool is one `Tool` object: its name, its description, its input schema, a
one-line summary for the docs, and the handler that runs it. Adding a tool is
adding one object to the list in `session_tools`, `work_tools` or `state_tools`,
and a test. The server, the `tools/list` answer and the tool tables in the README
and the skill all read this list.

A handler is `handler(ctx, arguments) -> str`. It gets the dsh client and the state
directory in a `Context` and takes no module-level state, so a test passes a fake.
"""

from __future__ import annotations

import io
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from dsh_workers_mcp.transport import DshClient

if TYPE_CHECKING:
    from dsh_workers_mcp.state.core import State

SESSION_ARG = {
    "type": "string",
    "description": "Full session id or a unique substring (e.g. '4f27ceef').",
}


@dataclass
class Context:
    """What a handler may use: the dsh client, and the state directory when there is one."""

    client: DshClient
    state: State | None = None


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict
    summary: str            # one line for the tool tables in the docs
    blocks: str             # does the call wait? "no", "yes", "briefly", with a reason when it helps
    handler: Callable[[Context, dict], str]
    needs_state: bool = False   # the tool exists only when the server has a state directory

    def listing(self) -> dict:
        """The entry of this tool in the MCP `tools/list` answer."""
        return {"name": self.name, "description": self.description, "inputSchema": self.schema}


class Args:
    """dshctl's command functions take an argparse namespace; this stands in."""

    def __init__(self, client: DshClient, **kwargs: Any) -> None:
        self.client = client
        for key, value in kwargs.items():
            setattr(self, key, value)


def capture(func: Callable[[Any], Any], args: Args) -> str:
    """Run a dshctl command, returning what it printed to stdout."""
    buffer = io.StringIO()
    real_stdout = sys.stdout
    sys.stdout = buffer
    try:
        func(args)
    finally:
        sys.stdout = real_stdout
    return buffer.getvalue().strip()
