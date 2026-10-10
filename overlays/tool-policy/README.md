# tool-policy: supervision-level presets for dsh workers

A dsh permission preset has two knobs: a sandbox mode and an approval policy. On
its own, a preset governs only sandbox escalation. No stock tool asks for
approval otherwise. This overlay adds the missing layer. It defines
supervision-level presets and, for each preset, allow, deny and ask rules on
tool calls. A lead picks a level when it hires a worker (`dsh_create
permission=…`). So the lead decides once, up front, how far a worker is trusted.
Nobody argues it turn by turn.

| Preset | Sandbox | Approval | Behaviour |
|---|---|---|---|
| `read-only` | read-only | ask | `write` and `edit` are denied. For reviewers and verifiers |
| `supervised` | workspace-write | ask | `web_fetch`, `web_search` and `git push` **ask an approver** (the lead, through `dsh_pending`) |
| `autonomous` | workspace-write | never | the same actions are **rejected outright**, so the worker never blocks. For hands-off work in a worktree |
| `danger-full-access` | danger-full-access | never | unconfined. For human use only |

`supervised` replaces the stock `workspace-write` preset. Both would be the same
bundle, and dsh resolves a shared bundle to whichever one is declared first.

## Scope, not security

Presets control what a worker is *supposed* to do. They are not a boundary.

- The sandbox modes confine file **writes** only. Reads stay open (your home
  directory, including any credentials in it), and so does the network. The
  `web_*`, MCP and model calls run outside the sandbox entirely.
- Rules on `bash` match the command text. `sh -c`, base64 or a script file
  evades them. `curl` from bash reaches the network, whatever the rules say.
- To confine a hijacked or misled agent, you need an OS sandbox around the whole
  dsh process (for example bubblewrap, or a container).

## Use

```sh
dshctl policy-patch --out ~/.dsh/tool-policy.patch.yml     # fills in the path of this install
dsh --patch ~/.dsh/tool-policy.patch.yml --dump-config --profile <profile>   # check that it composes
dsh --patch ~/.dsh/tool-policy.patch.yml --profile <profile>                 # use it
```

`dsh` loads a plugin by absolute path. For this reason the patch is rendered and
not used from the package as it is. Put `--patch` before `--profile`. (An alias
such as `web` has its own parser and rejects it after.) To make the patch
permanent, copy the rendered entries into the `cordis.patch.yml` of the profile.

Switch a worker with `/permission <preset>`. From an orchestrator, use
`dsh_permission`. It goes through `POST /api/commands/execute`. Never send the
line in a prompt, because that gives the line to the model as ordinary text.

## How it decides

On `tools/pre-execute`, the plugin reads the effective preset of the calling
session. It applies the first matching rule of that preset. If no rule matches,
it applies the `default` of the preset.

- `deny`: the model gets an error that carries the reason of the rule. Nobody is
  asked. The error ends with `[tool-policy: preset <name>]`, and `dsh_digest`
  counts it as a refusal.
- `ask`: the approval service of dsh decides. Under approval `ask`, the request
  reaches an answerer (the UI, or an orchestrator through `dsh_pending` and
  `dsh_approve`). Under `never`, the service rejects the request itself. So one
  rule means "ask" in `supervised` and "reject" in `autonomous`.

**Fail-closed.** Three cases get the `fallback` rule set: a session whose knobs
match no preset (dsh reports `custom`), an unknown preset name, and a deployment
without the preset service. The fallback allows no `write`, no `edit`, no web
access and no `git push`.

**Subagents.** dsh copies the sandbox override of the parent to a child, and it
pins the approval policy of the child to `never`. So a child of `supervised`
resolves to `autonomous`. A child of `read-only` resolves to `custom` and gets
`fallback`. Nobody can ask a child about anything, so it gets the
reject-outright rules.

## Make it yours

The rules are data in `policy.patch.yml`. A rule has these parts:
- `tool`
- optionally `argument` and a JavaScript regex `pattern`, which the plugin tests
  against that argument (`command` for `bash`, `url` for `web_fetch`)
- a `decision`
- the `reason` that the model or the approver sees

The first match wins. Start from the shipped presets, and add rules for what your
project cannot afford. These fit most projects:

```yaml
- tool: bash
  argument: command
  pattern: '\brm\s+(-\w*r\w*f|-\w*f\w*r)\b'
  decision: deny
  reason: recursive forced deletes are not permitted; remove specific files
- tool: bash
  argument: command
  pattern: '\b(npm|pnpm|yarn|cargo)\s+publish\b|\btwine\s+upload\b'
  decision: ask
  reason: publishing releases a package beyond the worktree
- tool: bash
  argument: command
  pattern: '\b(curl|wget)\b[^|]*\|\s*(ba|z)?sh\b'
  decision: deny
  reason: do not pipe a download into a shell
```

`test.ts` covers these three rules. [`docs/SANDBOX.md`](../../docs/SANDBOX.md) has
a fourth rule, for the dsh settings directory, with its cases in `test.ts`.
[`RECIPES.md`](RECIPES.md) has more optional rules: no writes in the main
checkout, no commands outside the worktrees, a refusal for heredocs that tells
the worker what to do instead, and no goals that a worker creates itself.

A bad regex or a malformed rule fails the boot, not the first tool call, because
the plugin validates its config at load. Add a case to `test.ts` for each rule
that you add. It needs only `node`: `node test.ts`.

A non-insert patch replaces the whole config of an entry. For this reason the
patch restates the full preset table.

## Status

- `test.ts` runs in the tests of this repository. It covers rule matching, the
  `git push` regex (it matches `git -C x push` and `git --no-pager push`, and
  does not match `git log --grep push`), the example rules above, and config
  validation.
- The rendered patch composes with `dsh --dump-config`. That checks the
  composition. It does not boot dsh.
- In testing, we booted the same plugin live (dsh `b150a55`, 2026-09-26):
  - `autonomous` denied `git push` and `web_search` with the reason of the rule
    and no approval.
  - `supervised` raised an approval that carried the reason of the rule.
    `rejected` and `allowed-once` behaved as described.
  - `read-only` denied `write`.
  - A subagent of a `read-only` worker resolved to `custom`, and `fallback`
    denied it.

  Check these again after a dsh upgrade.

## Implementation notes

- The plugin has no runtime dependencies, on purpose. Every import is type-only,
  and the config validator is a hand-written Standard Schema object. The plugin
  loads by absolute path from outside the dsh workspace, where the
  `@deepseek-ai/*` runtime packages would not resolve.
- `package.json` with `"type": "module"` is load-bearing. Without it, Node
  resolves this out-of-tree `.ts` file as CommonJS, and the dsh loader fails with
  `ERR_REQUIRE_CYCLE_MODULE`.
- `--dump-config` prints regex patterns with doubled backslashes. The value that
  dsh loads has single backslashes.
