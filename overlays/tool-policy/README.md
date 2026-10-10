# tool-policy: supervision-level presets for dsh workers

A dsh permission preset has two knobs, a sandbox mode and an approval policy.
On its own that only governs sandbox escalation: no stock tool asks for approval
otherwise. This overlay adds the missing layer. It defines supervision-level
presets and, per preset, allow / deny / ask rules on tool calls. A lead picks a
level when it hires a worker (`dsh_create permission=…`), so how far a worker is
trusted is decided once, up front, not argued turn by turn.

| Preset | Sandbox | Approval | Behaviour |
|---|---|---|---|
| `read-only` | read-only | ask | `write`/`edit` denied. For reviewers and verifiers |
| `supervised` | workspace-write | ask | `web_fetch`, `web_search` and `git push` **ask an approver** (the lead, through `dsh_pending`) |
| `autonomous` | workspace-write | never | the same actions are **rejected outright**, so it never blocks. For hands-off work in a worktree |
| `danger-full-access` | danger-full-access | never | unconfined. Human use only |

`supervised` replaces the stock `workspace-write` preset: both would be the same
bundle, and dsh resolves a shared bundle to whichever is declared first.

## Scope, not security

Presets control what a worker is *supposed* to do. They are not a boundary.

- The sandbox modes confine file **writes** only. Reads (your home directory,
  including any credentials in it) and the network stay open, and `web_*`, MCP
  and model calls run outside the sandbox entirely.
- Rules on `bash` match the command text. `sh -c`, base64 or a script file
  evades them, and `curl` from bash reaches the network whatever the rules say.
- Confining a hijacked or misled agent needs an OS sandbox around the whole
  dsh process (for example bubblewrap, or a container).

## Use

```sh
dshctl policy-patch --out ~/.dsh/tool-policy.patch.yml     # fills in this install's path
dsh --patch ~/.dsh/tool-policy.patch.yml --dump-config --profile <profile>   # check it composes
dsh --patch ~/.dsh/tool-policy.patch.yml --profile <profile>                 # use it
```

`dsh` loads a plugin by absolute path, which is why the patch is rendered, not
used from the package as it is. Put `--patch` before `--profile` (an alias such
as `web` has its own parser and rejects it after). To make it permanent, copy
the rendered entries into the profile's `cordis.patch.yml`.

Switch a worker with `/permission <preset>`. From an orchestrator that is
`dsh_permission` (it goes through `POST /api/commands/execute`), never through
a prompt, which would hand the line to the model as ordinary text.

## How it decides

On `tools/pre-execute` the plugin reads the calling session's effective preset
and applies that preset's first matching rule, else the preset's `default`.

- `deny`: the model gets an error carrying the rule's reason. Nobody is asked.
  The error ends with `[tool-policy: preset <name>]`, which `dsh_digest` counts as
  a refusal.
- `ask`: dsh's approval service decides. Under approval `ask` the request
  reaches an answerer (the UI, or an orchestrator via `dsh_pending` and
  `dsh_approve`). Under `never` the service rejects it itself. So one rule
  means "ask" in `supervised` and "reject" in `autonomous`.

**Fail-closed.** A session whose knobs match no preset (dsh reports `custom`), an
unknown preset name, or a deployment without the preset service gets `fallback`:
no `write`, `edit`, web access or `git push`.

**Subagents.** dsh copies the parent's sandbox override to a child and pins the
child's approval policy to `never`. A child of `supervised` therefore resolves
to `autonomous`, and a child of `read-only` resolves to `custom` and gets
`fallback`. A child can never be asked about anything, so it gets the
reject-outright rules.

## Make it yours

The rules are data in `policy.patch.yml`. Each is `tool`, optionally `argument`
and a JavaScript regex `pattern` (tested against that argument; `command` for
`bash`, `url` for `web_fetch`), a `decision`, and the `reason` the model or the
approver sees. The first match wins. Start from the shipped presets and add
rules for what your project cannot afford. Some that fit most projects:

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

(These three are exercised in `test.ts`.) A bad regex or a malformed rule fails
the boot, not the first tool call, because the plugin validates its config at
load. Add a case to `test.ts` for each rule you add; it needs only `node`:
`node test.ts`.

Note that a non-insert patch replaces an entry's whole config, which is why the
patch restates the full preset table.

## Status

- `test.ts` (rule matching, the `git push` regex including `git -C x push` and
  `git --no-pager push` but not `git log --grep push`, the example rules above,
  and config validation) runs in this repository's tests.
- The rendered patch composes with `dsh --dump-config`. That composes; it does
  not boot.
- In the author's testing the same plugin was booted live (dsh `b150a55`,
  2026-09-26): `autonomous` denied `git push` and `web_search` with the rule's
  reason and no approval; `supervised` raised an approval carrying the rule's
  reason, and `rejected` and `allowed-once` behaved as described; `read-only`
  denied `write`; a subagent of a `read-only` worker resolved to `custom` and was
  denied by `fallback`. Re-check after a dsh upgrade.

## Implementation notes

- Runtime-dependency-free on purpose: every import is type-only, and the config
  validator is a hand-written Standard Schema object. The plugin loads by
  absolute path from outside the dsh workspace, where `@deepseek-ai/*` runtime
  packages would not resolve.
- `package.json` with `"type": "module"` is load-bearing. Without it Node
  resolves this out-of-tree `.ts` file as CommonJS and the dsh loader fails with
  `ERR_REQUIRE_CYCLE_MODULE`.
- `--dump-config` prints regex patterns with doubled backslashes. The value
  actually loaded is single-escaped.
