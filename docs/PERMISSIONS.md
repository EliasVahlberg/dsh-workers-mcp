# Permissions: presets, and what a worker can and cannot do

## Permission presets

The method hires workers at a supervision level (`dsh_create permission=…`). Stock `dsh` does not have those levels. So [`overlays/tool-policy/`](../overlays/tool-policy/) provides them as a dsh patch that you can read and edit:

| Preset | Behaviour |
|---|---|
| `read-only` | `write` and `edit` are denied. For reviewers and verifiers |
| `supervised` | web access and `git push` ask the lead (`dsh_pending`) |
| `autonomous` | the same actions are rejected outright, so the worker never blocks |

```bash
dshctl policy-patch --out ~/.dsh/tool-policy.patch.yml        # fills in the path of this install
dsh --patch ~/.dsh/tool-policy.patch.yml --profile <profile> --dump-config   # does it compose?
dsh --patch ~/.dsh/tool-policy.patch.yml --profile <profile>
```

The rules are data: a `tool`, an argument regex, `allow`, `deny` or `ask`, and a reason. So you can add your own. The README of the overlay has examples, and [`RECIPES.md`](../overlays/tool-policy/RECIPES.md) has optional rules. Presets control **scope, not security**. Reads and the network stay open, and `bash` rules match command text, which is easy to evade. Status: the patch composes with `--dump-config`. The live-boot checks (a denied `git push`, an approval raised) come from earlier testing of the same plugin. They do not come from this release.

## What a worker can and cannot do

Presets and the sandbox stop some things and not others. This is from testing a scratch worker at `autonomous`:

| Result | What |
|---|---|
| Enforced by code | writes outside the workspace, `git push`, signals to other processes, API keys in the environment (the server passes none to workers) |
| **Not prevented** | **reads** of any file in the home directory, including the dsh settings file that holds provider keys. **Writes into the area of another worker** (the writable boundary is the whole workspace). **Network access** (open by design) |

If you run workers unattended, you can accept this, or you can box the dsh server. [`SANDBOX.md`](SANDBOX.md) has the full table and a bubblewrap wrapper (`scripts/dsh-sandbox.sh`, with a self-test). It also describes a **known problem**: the per-command sandbox of dsh itself may not start inside the box. Read that before you rely on it.
