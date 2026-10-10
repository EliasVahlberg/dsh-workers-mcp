# Running workers in a sandbox

The permission presets (`overlays/tool-policy/`) control what a worker is
*supposed* to do. This page says what they do not stop, and gives a way to put
the whole dsh server in a box. Read the "Known problem" section before relying
on it.

## What a worker can and cannot do

From testing: a scratch worker at the `autonomous` preset, in a real workspace,
with fake canary files and a harmless peer process, all removed afterwards.
The contents of real files were not read.

| Result | Probe |
|---|---|
| Enforced by code | write outside the workspace (the sandbox refuses); `git push` (the tool-policy overlay); signalling other processes (PID namespace); API keys in the environment (none passed to workers) |
| **Not prevented** | **reads** of any file in the home directory, including the dsh settings file that holds provider keys (checked with `test -r`, not read); **writes into another worker's area**, because the writable boundary is the whole workspace; **network**, open by design |

So a worker you do not trust can read your credentials and send them out. The
presets do not change that. If you run workers unattended, either accept it, or
box the server.

## Per-worker writable roots

In the dsh source, the writable set for `workspace-write` is derived in one place
(`packages/sandbox/sandbox/src/roots.ts`): the policy's `workspaceRoot`, `/tmp`
and the platform temp directory, nothing configurable per session. So:

- Workers that share a workspace share write access to all of it. A rule such as
  "write only inside your worktree" cannot come from the sandbox.
- A session whose `workspaceRoot` is its own worktree would be confined to it.
  I read that `workspaceRoot` is the policy's field, but did not confirm end to
  end that it is the session's `cwd`, and did not try creating a worker with a
  worktree as its `cwd`. The cost would be one worker per worktree, which fights
  the method's long-lived workers.

The practical answer today is the lead's own check: the digest shows edits
outside the worktree (`editedOutsideWorktree`), and `PLAYBOOKS` §6 has the lead
look.

## Boxing the whole server with bubblewrap

`scripts/dsh-sandbox.sh` runs a command with the real home hidden (an empty
tmpfs), `/` read-only, writes allowed only where you name, a cleared
environment with only the variables you name, and other processes invisible.
Network stays open unless you pass `--no-net`.

```bash
scripts/dsh-sandbox.sh --self-test        # proves each property with canary files
```

The self-test checks that a file and a directory in the real home are invisible,
that writes land only in the named workspace (and a write to `$HOME` goes to a
tmpfs that never reaches the host), that a named variable passes and an unnamed
one does not, that host processes are not visible, and that `--no-net` blocks
name lookups. It needs `bwrap` and user namespaces. `tests/test_sandbox.py` runs
it with a throwaway `HOME`; set `DSH_SANDBOX_SKIP_NET=1` to skip the (slow) DNS
checks.

To run the dsh server in it, with a dedicated home that holds no real
credentials:

```bash
mkdir -p ~/.dsh-boxed-home            # put a settings.yaml here with ONLY the provider
                                      # you need, naming its key by environment
                                      # variable (apiKeyEnv), never the key itself
scripts/dsh-sandbox.sh \
  --ro <dsh checkout or install> --ro <node installation dir> --path <node bin dir> \
  --rw ~/.dsh-boxed-home --rw <your workspace> \
  --set DSH_HOME=$HOME/.dsh-boxed-home --env PROVIDER_API_KEY \
  --cwd <dsh checkout> -- <the command that starts dsh web> --no-open
```

Start in the dsh checkout (`--cwd`): its loader resolves packages from the
working directory. The workers' own workspace is the `cwd` you give `dsh_create`,
which must be one of the `--rw` paths.

**Verified:** the self-test passes, and `dsh web` booted from a source checkout
under the wrapper (a throwaway home, node installed under `$HOME` bound
read-only), answered HTTP, and `dshctl list` reached it.

## Known problem: dsh's own sandbox inside this one

dsh confines each worker command with its own sandbox, choosing `bwrap` and then
Landlock, and failing closed when neither works (`packages/sandbox/sandbox-local`).
Inside this wrapper **a nested `bwrap` does not start**: `bwrap: No permissions to
create a new namespace`. I checked on Ubuntu 26.04 (kernel 7.0) with
unprivileged user namespaces enabled: `bwrap --ro-bind / / bwrap --ro-bind / / true`
fails on that host even with the bare minimum outer options, so it is not caused by
this script's flags.

So under the wrapper dsh would have to fall back to Landlock, and **I have not
confirmed that a worker's `bash` call runs there**. A probe of the Landlock
launcher that I ran with the dsh package's own function reported `unusable`, both
inside the wrapper and outside it; I may have called the probe wrongly, so treat
it as unknown, not as a verdict. Nothing here has run a worker turn under the
wrapper, because that needs a model.

Before you rely on the wrapper, do this once: start the boxed server as above,
create a worker at `autonomous` (`dsh_create permission=autonomous`), and ask it
to run `echo ok` and `touch somefile`. If the call is refused as a sandbox
failure, the wrapper protects the server but workers cannot run commands under
it; use a container or a VM instead, where the sandbox inside is not nested in a
user namespace of the same kind.

## A speed bump for the settings directory

A deny rule for the dsh settings and home directories is cheap and worth having,
but it matches text, so `sh -c` or a script file gets past it. Add it as a
rule in the preset you use (see the overlay README); the cases are in
`overlays/tool-policy/test.ts`:

```yaml
- tool: bash
  argument: command
  pattern: '(^|[\s=:"''])(~|\$HOME|/home/[^/\s]+)/\.dsh\b'
  decision: deny
  reason: the dsh settings hold provider keys and are not part of the task
- tool: read
  argument: file_path
  pattern: '/\.dsh/'
  decision: deny
  reason: the dsh settings hold provider keys and are not part of the task
```

`file_path` is the argument name the model sends to `read` (the rule matches the
call as sent), so check a new tool's argument name before copying a rule.
