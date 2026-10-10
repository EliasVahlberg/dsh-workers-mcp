# Security policy

## Report a problem

Use GitHub's private vulnerability reporting: open the **Security** tab of this
repository and choose **Report a vulnerability**. Do not open a public issue for a
security problem.

Include the version or commit, the dsh version, the steps to reproduce, and what
you expected. You get a first answer within 14 days. This is a small project, and
no one is paid to answer faster.

## What counts

This server lets one agent (the lead) act on other agents (the workers). A report
is in scope when it shows one of these:

- A call that the policy should refuse and the server allows. Examples are applying
  `danger-full-access` without `--allow-full-access`, and a `dsh_permission` or
  `/sandbox` change that gets past `check_policy`.
- A tool approval that a worker can give itself, or a question that a worker can
  answer for itself.
- A secret that the server writes to its state directory, its logs or a tool result.
- A path in the state directory that a crafted session id, task id or role name can
  escape.
- A rule in `overlays/tool-policy/` that does not match what its tests say it matches.

## What does not count

[`docs/PERMISSIONS.md`](docs/PERMISSIONS.md) and [`docs/SANDBOX.md`](docs/SANDBOX.md) list what the permission presets
do not stop: reads of any file in the home directory, network access, and writes
into the area of another worker. The presets control scope. They are not a security
boundary. A report that repeats one of those listed limits is not a vulnerability.
If you find a new limit, report it. We will add it to the list.

## Supported versions

The project is alpha. Only the latest commit on `main` gets fixes.
