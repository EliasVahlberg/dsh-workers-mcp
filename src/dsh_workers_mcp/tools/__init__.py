"""
The tools of the server, in one list.

Each group module defines its tools as `Tool` objects. `all_tools` joins them in the
order that clients see them. The server, the tool tables in the docs and the tests
read this list.
"""

from __future__ import annotations

from dsh_workers_mcp.tools.base import Args, Context, Tool, capture
from dsh_workers_mcp.tools.session_tools import TOOLS as SESSION_TOOLS
from dsh_workers_mcp.tools.state_tools import TOOLS as STATE_TOOLS
from dsh_workers_mcp.tools.work_tools import TOOLS as WORK_TOOLS

# The order in tools/list. A name that is not here goes last, in the order it was defined.
ORDER = [
    "dsh_list", "dsh_status", "dsh_grep", "dsh_search", "dsh_task", "dsh_ask", "dsh_wait",
    "dsh_models", "dsh_create", "dsh_pending", "dsh_answer", "dsh_fork", "dsh_goal",
    "dsh_cancel", "dsh_name", "dsh_permission", "dsh_approve",
    "dsh_roster", "dsh_tasks", "dsh_digest", "dsh_brief",
]


def all_tools() -> list[Tool]:
    tools = [*SESSION_TOOLS, *WORK_TOOLS, *STATE_TOOLS]
    rank = {name: index for index, name in enumerate(ORDER)}
    return sorted(tools, key=lambda tool: rank.get(tool.name, len(ORDER)))


__all__ = ["Args", "Context", "Tool", "all_tools", "capture"]
