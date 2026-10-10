# Running workers in a sandbox

The permission presets (`overlays/tool-policy/`) control what a worker is
*supposed* to do. This page says what the presets do not stop. It also gives a
way to put the whole dsh server in a box. Read the "Known problem" section
before you rely on the box.

**Status of this page.**
- Verified: the table below, from a scratch worker. The wrapper passes its
  self-test. `dsh web` boots under it and answers HTTP.
- Not verified: that a worker can run a command under the wrapper (see "Known
  problem"). Nobody has run a worker turn there. Issue #13 tracks the check.
- Not answered: whether dsh can give each worker its own writable root. A worker
  can write into the area of another worker today.

## What a worker can and cannot do

This is from testing. We used a scratch worker at the `autonomous` preset, in a
real workspace, with fake canary files and a harmless peer process. We removed
all of them afterwards. We did not read the contents of real files.

| Result | Probe |
|---|---|
| Enforced by code | a write outside the workspace (the sandbox refuses it), `git push` (the tool-policy overlay), signals to other processes (PID namespace), API keys in the environment (the server passes none to workers) |
| **Not prevented** | **reads** of any file in the home directory, including the dsh settings file that holds provider keys (we checked with `test -r` and did not read it). **Writes into the area of another worker**, because the writable boundary is the whole workspace. **Network access**, which is open by design |

So a worker that you do not trust can read your credentials and send them out.
The presets do not change that. If you run workers unattended, you can accept
this, or you can box the server.

## Per-worker writable roots

In the dsh source, one place derives the writable set for `workspace-write`
(`packages/sandbox/sandbox/src/roots.ts`). It has the `workspaceRoot` of the
policy, `/tmp` and the temp directory of the platform. Nothing is configurable
for each session. So:

- Workers that share a workspace share write access to all of it. The sandbox
  cannot give a rule such as "write only inside your worktree".
- A session whose `workspaceRoot` is its own worktree would be confined to it.
  We read that `workspaceRoot` is a field of the policy. We did not confirm
  end to end that it is the `cwd` of the session. We did not try to create a
  worker with a worktree as its `cwd`. The cost would be one worker for each
  worktree, which conflicts with the long-lived workers of the method.

The practical answer today is the check of the lead. The digest shows edits
outside the worktree (`editedOutsideWorktree`), and `PLAYBOOKS` §6 tells the
lead to look. The optional rules in `overlays/tool-policy/RECIPES.md` stop some
of these edits before they happen.

## Boxing the whole server with bubblewrap

`scripts/dsh-sandbox.sh` runs a command with these properties:
- The real home is hidden (an empty tmpfs).
- `/` is read-only.
- Writes are allowed only where you name.
- The environment is cleared. Only the variables that you name pass.
- Other processes are invisible.

The network stays open unless you pass `--no-net`.

```bash
scripts/dsh-sandbox.sh --self-test        # proves each property with canary files
```

The self-test checks these points:
- A file and a directory in the real home are invisible.
- Writes land only in the named workspace. A write to `$HOME` goes to a tmpfs
  that never reaches the host.
- A named variable passes, and an unnamed one does not.
- Host processes are not visible.
- `--no-net` blocks name lookups.

It needs `bwrap` and user namespaces. `tests/test_sandbox.py` runs it with a
throwaway `HOME`. Set `DSH_SANDBOX_SKIP_NET=1` to skip the DNS checks, which are
slow.

To run the dsh server in the box, use a dedicated home that holds no real
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

Start in the dsh checkout (`--cwd`). Its loader resolves packages from the
working directory. The workspace of the workers is the `cwd` that you give
`dsh_create`. It must be one of the `--rw` paths.

**Verified:** the self-test passes. `dsh web` booted from a source checkout under
the wrapper (a throwaway home, with node installed under `$HOME` and bound
read-only). It answered HTTP, and `dshctl list` reached it.

## Known problem: dsh's own sandbox inside this one

dsh confines each worker command with its own sandbox. It tries `bwrap` and then
Landlock, and it fails closed when neither works
(`packages/sandbox/sandbox-local`). Inside this wrapper **a nested `bwrap` does
not start**: `bwrap: No permissions to create a new namespace`. We checked on
Ubuntu 26.04 (kernel 7.0) with unprivileged user namespaces enabled. The command
`bwrap --ro-bind / / bwrap --ro-bind / / true` fails on that host, even with the
bare minimum of outer options. So the flags of this script do not cause it.

So under the wrapper, dsh would have to fall back to Landlock. **We have not
confirmed that a `bash` call of a worker runs there.** We probed the Landlock
launcher with a function from the dsh package. The probe reported `unusable`,
both inside the wrapper and outside it. We may have called the probe wrongly, so
treat the result as unknown. Do not treat it as a verdict. Nothing here has run
a worker turn under the wrapper, because that needs a model.

Do this once before you rely on the wrapper:
1. Start the boxed server as above.
2. Create a worker at `autonomous` (`dsh_create permission=autonomous`).
3. Ask it to run `echo ok` and `touch somefile`.

If dsh refuses the call as a sandbox failure, the wrapper protects the server but
workers cannot run commands under it. Use a container or a VM instead. There, the
sandbox inside is not nested in a user namespace of the same kind.

## A speed bump for the settings directory

A deny rule for the dsh settings and home directories is cheap and worth having.
It matches text, so `sh -c` or a script file gets past it. Add it as a rule in the
preset that you use (see the README of the overlay). The cases are in
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

`file_path` is the argument name that the model sends to `read`. The rule matches
the call as sent. Check the argument name of a new tool before you copy a rule.
