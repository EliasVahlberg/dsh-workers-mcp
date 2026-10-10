# The tools

The server has 21 tools. Four of them (`dsh_roster`, `dsh_tasks`, `dsh_digest` and `dsh_brief`) need a state directory and are hidden with `--no-state`. The table comes from the tool registry in `src/dsh_workers_mcp/tools/`. Do not edit it by hand: run `uv run python scripts/gen-tool-tables.py`.

<!-- tools:start -->
| Tool | Does | Blocks? |
|---|---|---|
| `dsh_list` | the roster: who exists, who is busy, context pressure | no |
| `dsh_status` | deep insight into one worker, including `lastTurnEnd` | no |
| `dsh_grep` | regex over the own history of one worker, paged backwards | no |
| `dsh_search` | full-text search across every session, to find who discussed a topic | no |
| `dsh_task` | dispatch real work. The call returns at once | no |
| `dsh_ask` | ask a quick question and wait for the reply | yes, until the turn ends |
| `dsh_wait` | wait for a turn to end, or return early when the worker blocks. `sessions=[…]` returns for the first of several workers | yes, and it returns early if the worker blocks |
| `dsh_models` | model and reasoning-effort levels. `reasoning_effort=` sets the effort of a worker, only while it is idle | no |
| `dsh_create` | create a worker, optionally at a permission preset | no |
| `dsh_pending` | **what a worker is blocked on** | no |
| `dsh_answer` | **unblock it** (a question) | no |
| `dsh_fork` | fork a session at a turn boundary (recovers a poisoned history) | no |
| `dsh_goal` | read or drive a goal, which is an autonomous loop (`set` needs `start=true`) | no |
| `dsh_cancel` | interrupt a turn, and confirm that the abort landed | briefly, to confirm the abort |
| `dsh_name` | pin a role name on a worker, so that the roster is readable | no |
| `dsh_permission` | show or set the supervision level of a worker | no |
| `dsh_approve` | **unblock it** (a tool approval), after you read the real arguments | no |
| `dsh_roster` | managed workers: facts that the server fills in, plus your role and notes | no |
| `dsh_tasks` | task records: dispatch and return as the server tracks them, plus your verdicts | no |
| `dsh_digest` | what a worker did in its last turns, computed mechanically (no model) | no |
| `dsh_brief` | the start-of-session sweep: what needs you, then the roster and the open tasks | no |
<!-- tools:end -->

Three groups carry the weight: **delegate** (`dsh_task` and `dsh_ask`), **report** (`dsh_status` and `dsh_grep`), and **unblock** (`dsh_pending` and `dsh_answer`). The other tools keep a long-lived roster readable.

Read-only tools are safe to auto-approve. These tools change worker state: `dsh_task`, `dsh_ask`, `dsh_answer`, `dsh_goal`, `dsh_name`, `dsh_create`, `dsh_cancel`, `dsh_approve` and `dsh_permission`. An orchestrator that must keep workers moving without the user in the loop needs to auto-approve them. The server limits what that allows. It refuses `danger-full-access`, and it refuses the slash commands for permission, sandbox and approval that arrive as prompts, unless it started with `--allow-full-access`. So the widest permission that a lead can give a worker is `autonomous` or `supervised`, whatever the lead is talked into. `dsh_roster` and `dsh_tasks` write only the state directory. Gate them with care, because a worker acts on an answer or an approval with full tool access.

The supervision-level presets (`read-only`, `supervised`, `autonomous`) come from the dsh overlay in [`overlays/tool-policy/`](../overlays/tool-policy/). See [Permissions](PERMISSIONS.md). They control **scope, not security**.
