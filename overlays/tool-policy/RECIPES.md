# Optional rules

These rules are recipes. None is on by default. Copy the ones that your project
needs into the `rules:` list of a preset in `policy.patch.yml`. Put them before
the other rules, because the first matching rule wins. Each rule has test cases
in `test.ts`.

All of them are scope controls. They match text, so `sh -c`, a script file or an
encoded string gets past them. They stop a worker that makes a mistake. They do
not stop a worker that tries to get around them. For the limits, see
[Scope, not security](README.md#scope-not-security).

## How a rule learns the worktree

It does not. A preset applies to every session that uses it, and the plugin sees
only the tool name, the arguments and the preset. So these rules use fixed
absolute paths that you write into the pattern. The examples use `/work/main` for
the main checkout, `/work/trees` for the directory that holds the worktrees, and
`/work/sibling` for another repository.

Two conditions make this work:
- Worktrees live outside the checkout that you protect. A worktree inside
  `/work/main` would match the first rule.
- You escape regex characters in the paths. A dot in a path is written `\.`.

A rule cannot say "this worker may write only in its own worktree", because the
policy has no per-session path. That needs a file or a field that maps each
session to its worktree. It is not built. See the issue linked from the README.
Until then, the digest reports `editedOutsideWorktree` after the fact.

## 1. Do not write in the main checkout

A worker once edited a file in the main checkout and not in its worktree. The
presets allow writes in the whole workspace, so they did not stop it.

```yaml
- tool: write
  argument: file_path
  pattern: '^/work/main(/|$)'
  decision: deny
  reason: the main checkout is not yours; write in your worktree under /work/trees
- tool: edit
  argument: file_path
  pattern: '^/work/main(/|$)'
  decision: deny
  reason: the main checkout is not yours; edit in your worktree under /work/trees
```

This covers the `write` and `edit` tools. It does not cover a shell command that
writes into the checkout. The next recipe covers some of those.

## 2. Do not run commands outside the worktrees

A worker once ran a build in a sibling repository to debug a problem. The build
changed files that every other worktree's staleness check reads.

Deny a shell command whose `workdir` is not under the worktree directory:

```yaml
- tool: bash
  argument: workdir
  pattern: '^(?!/work/trees/)'
  decision: deny
  reason: run commands in your worktree under /work/trees
```

A command with no `workdir` does not match this rule, because the argument is
missing. It runs in the session directory.

Deny a build that starts with `cd` into another repository:

```yaml
- tool: bash
  argument: command
  pattern: '\bcd\s+["'']?/work/sibling(/|\s|$|["'';&|])[^\n]*\b(make|gradlew?|npm\s+run\s+build|cargo\s+build)\b'
  decision: deny
  reason: do not build in another repository; report what you need instead
```

The pattern matches `cd /work/sibling && make`. It does not match
`cd /work/siblings-other && make`, `cd /work/trees/t1 && make` or
`cd /work/sibling && ls`. It does not match a build that reaches the directory in
another way, for example `make -C /work/sibling`.

## 3. Refuse a heredoc, and say what to do instead

Some model providers filter or break long shell commands. A heredoc (`<<EOF`)
carries a whole file inside the command, so it triggers the problem. A worker
that sent an empty heredoc as a way to pause was refused many times.

```yaml
- tool: bash
  argument: command
  pattern: '(?<!<)<<-?\s*["'']?[A-Za-z_]\w*'
  decision: deny
  reason: write files with the write or edit tool. An empty heredoc is still a heredoc. If you meant to pause, send no command.
```

The pattern matches `cat <<EOF`, `cat <<'EOF'` and `python3 - <<-PY`. It does not
match a here-string (`<<<`) or an arithmetic shift written with a number
(`1 << 3`). It refuses a shift by a variable name written without a dollar sign
(`1<<n`). That is a false positive. Rewrite the expression.

Enable this rule only if your provider needs it. It is the reason for a rule in
one project, not a default.
