"""
Orchestrator-side control of long-lived dsh worker sessions, exposed over MCP.

Two entry points:
  * ``dshctl``           -- the CLI / library (``dsh_workers_mcp.dshctl``)
  * ``dsh-workers-mcp``  -- the MCP stdio server (``dsh_workers_mcp.server``)

Runtime is deliberately stdlib-only: this drives an agent harness, so it must
not drag its own dependency tree into whatever environment it runs beside.
"""

__version__ = "0.1.0"
