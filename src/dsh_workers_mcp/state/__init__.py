"""
Orchestrator state on disk: what the MCP server observes, written by the tool
so the orchestrator cannot forget to write it.

Principle: the server writes FACTS it observed; the orchestrator writes
JUDGMENT (roles, notes, task status beyond what a turn end implies), and only
through the dsh_roster / dsh_tasks tools, never by editing the files, so the
two authors cannot clobber each other.

Layout (all under one state directory; see resolve_state_dir):

    logs/YYYY-MM-DD.jsonl        every tools/call: arguments, result, duration
    history/<sessionId>/
        events.jsonl             append-only mirror of the dsh session log (no chunks)
        mirror.json              the mirror's lastSeq cursor
        state.json               "last observed" snapshot for catching up
    .locks/                      advisory locks for read-modify-write
    roster.json                  managed workers: `auto` (server) + `notes` (orchestrator)
    tasks.json                   tasks: structured fields + dispatch records

Everything cached here is "last observed", stamped with observedAt. While the
dsh server is up, the live tools stay the source of truth; this is what
survives the orchestrator's own session ending, and what can be audited
without dsh running.

Modules, in the order of their imports:

    store     the files, locks and atomic writes; `Store`
    usage     what a task cost, from the numbers that dsh reports
    history   the history mirror, state.json and the compaction watch
    roster    the roster and the `dsh_roster` tool
    tasks     the task records and the `dsh_tasks` tool
    core      `State`, which joins the three on one `Store`
    reports   the digest of a worker (`dsh_digest`)
    hooks     `before_call` and `after_call`

Stdlib only, like the rest of the package.
"""

from dsh_workers_mcp.state.core import State
from dsh_workers_mcp.state.history import compaction_stalled
from dsh_workers_mcp.state.hooks import after_call, before_call
from dsh_workers_mcp.state.reports import cmd_digest
from dsh_workers_mcp.state.roster import cmd_roster
from dsh_workers_mcp.state.store import DEFAULT_DIRNAME, ms_to_iso, now_iso, resolve_state_dir
from dsh_workers_mcp.state.tasks import CLOSED, TASK_STATUSES, cmd_tasks
from dsh_workers_mcp.state.usage import task_usage, usage_delta

__all__ = [
    "CLOSED", "DEFAULT_DIRNAME", "State", "TASK_STATUSES", "after_call", "before_call",
    "cmd_digest", "cmd_roster", "cmd_tasks", "compaction_stalled", "ms_to_iso", "now_iso",
    "resolve_state_dir", "task_usage", "usage_delta",
]
