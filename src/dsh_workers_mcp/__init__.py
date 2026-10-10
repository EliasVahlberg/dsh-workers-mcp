"""
Orchestrator-side control of long-lived dsh worker sessions, exposed over MCP.

Entry points:
  * ``dsh-workers-mcp``  -- the MCP stdio server (``dsh_workers_mcp.server``)
  * ``dshctl``           -- the same operations as a CLI (``dsh_workers_mcp.dshctl``)
  * ``dsh-wake``         -- an optional loop that wakes the lead (``dsh_workers_mcp.wake``)

Runtime is deliberately stdlib-only: this drives an agent harness, so it must
not drag its own dependency tree into whatever environment it runs beside.

Imports run in one direction. A module imports only from a lower layer:

  0  transport    the wire: HTTP and WebSocket to dsh, the ``DshClient`` interface
     digest       a mechanical digest of a worker's events (no dsh needed)
  1  sessions     find a session, read its projections and events
     shape        check that dsh responses have the fields that the code reads
     state.store  the files of a state directory
  2  pending      pending questions and approvals, and answer checks
     permissions  permission presets and the tool-policy patch
     models       model selection
     state.usage  what a task cost
  3  waiting       prompts and waiting for turn ends
     state.*      history, roster and tasks (see ``state/__init__.py``)
  4  cmd_sessions, cmd_work   the commands, one function per ``dshctl`` command
  5  dshctl       the command line
  8  brief        the start-of-session sweep
  9  server       the MCP server
     wake         the wake loop

``tests/test_imports.py`` holds the exact layers and fails on an upward import.
To share code between two modules, move it down to a lower layer.
"""

__version__ = "0.1.0"
