#!/usr/bin/env bash
# size-gate.sh: refuse a commit when the work in a worktree passes the size set for its task.
#
# This is an optional recipe. It is not on by default. It makes an overrun
# visible: the worker cannot commit past the limit without the lead raising it.
# It is a speed bump, not a lock. A worker can still run `git commit --no-verify`
# or edit the limit file. The lead's check of the diff (PLAYBOOKS §6) stays.
#
# Usage (run inside the worktree or the repository):
#   size-gate.sh install                                  once per repository: add the pre-commit hook
#   size-gate.sh set --lines N [--files M] [--base BR]    set the limit for this worktree
#   size-gate.sh show                                     print the limit and the current size
#   size-gate.sh clear                                    remove the limit
#   size-gate.sh check                                    what the hook runs
#
# The size is the lines added plus the lines deleted, and the number of files,
# in the staged diff against the merge-base with BR (default: main). Staged
# changes count, so the check sees the whole task and not only the next commit.
# The limit lives in the git directory of the worktree, so git does not track it
# and it does not affect other worktrees. A worktree with no limit is not checked.
set -euo pipefail

die() { echo "size-gate: $*" >&2; exit 1; }

limit_file() { git rev-parse --git-path task-size; }

read_limit() {
  # Sets LINES, FILES and BASE from the limit file. Exits 1 if the file is wrong.
  local f; f=$(limit_file)
  LINES=""; FILES=""; BASE="main"
  local key val
  while IFS='=' read -r key val; do
    case "$key" in
      lines) LINES=$val ;;
      files) FILES=$val ;;
      base)  BASE=$val ;;
      ''|'#'*) ;;
      *) die "unknown key '$key' in $f" ;;
    esac
  done < "$f"
  [[ "$LINES" =~ ^[0-9]+$ ]] || die "'lines' in $f must be a number"
  [[ -z "$FILES" || "$FILES" =~ ^[0-9]+$ ]] || die "'files' in $f must be a number"
}

measure() {
  # Prints "<lines> <files>" for the staged diff against the merge-base with $BASE.
  local mb; mb=$(git merge-base HEAD "$BASE" 2>/dev/null) \
    || die "cannot find a merge-base of HEAD and '$BASE'. Tell the lead; do not use --no-verify."
  git diff --cached --numstat "$mb" | awk '
    { if ($1 != "-") n += $1; if ($2 != "-") n += $2; f++ }
    END { printf "%d %d\n", n + 0, f + 0 }'
}

cmd_check() {
  [[ -f "$(limit_file)" ]] || exit 0
  read_limit
  local size lines files; size=$(measure); lines=${size% *}; files=${size#* }
  if (( lines > LINES )) || { [[ -n "$FILES" ]] && (( files > FILES )); }; then
    {
      echo "size-gate: this commit would bring the task to $lines changed lines in $files files."
      echo "The limit for this task is $LINES lines${FILES:+ and $FILES files} against $BASE."
      echo "Do not commit. Report the total and the reason to the lead (what grew, what is left), then wait."
      echo "The lead can raise the limit with: size-gate.sh set --lines N"
    } >&2
    exit 1
  fi
}

cmd_set() {
  local lines="" files="" base="main"
  while (( $# )); do
    case "$1" in
      --lines) lines=${2:-}; shift 2 ;;
      --files) files=${2:-}; shift 2 ;;
      --base)  base=${2:-}; shift 2 ;;
      *) die "unknown option '$1'" ;;
    esac
  done
  [[ "$lines" =~ ^[0-9]+$ ]] || die "--lines N is required"
  [[ -z "$files" || "$files" =~ ^[0-9]+$ ]] || die "--files must be a number"
  {
    echo "lines=$lines"
    [[ -n "$files" ]] && echo "files=$files"
    echo "base=$base"
  } > "$(limit_file)"
  echo "size-gate: limit set in $(limit_file)"
}

cmd_show() {
  [[ -f "$(limit_file)" ]] || { echo "size-gate: no limit set for this worktree"; return; }
  read_limit
  local size; size=$(measure)
  echo "limit: $LINES lines${FILES:+, $FILES files} against $BASE"
  echo "now:   ${size% *} lines, ${size#* } files (staged diff)"
}

cmd_clear() { rm -f "$(limit_file)"; echo "size-gate: limit removed"; }

cmd_install() {
  local hook self; hook=$(git rev-parse --git-path hooks/pre-commit)
  self=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")
  if [[ -e "$hook" ]] && ! grep -q 'size-gate' "$hook"; then
    die "$hook already exists and is not ours. Add 'exec \"$self\" check' to it by hand."
  fi
  mkdir -p "$(dirname "$hook")"
  printf '#!/bin/sh\n# installed by size-gate.sh\nexec "%s" check\n' "$self" > "$hook"
  chmod +x "$hook"
  echo "size-gate: hook installed at $hook (it applies to every worktree of this repository)"
}

case "${1:-}" in
  install) cmd_install ;;
  set)     shift; cmd_set "$@" ;;
  show)    cmd_show ;;
  clear)   cmd_clear ;;
  check)   cmd_check ;;
  -h|--help|'') sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//' ;;
  *) die "unknown command '$1' (try --help)" ;;
esac
