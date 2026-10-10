#!/usr/bin/env bash
# dsh-sandbox.sh: run a command (typically the dsh server) with your real home
# hidden, writes confined, and only named environment variables passed through.
# Uses bubblewrap, the mechanism dsh itself uses on Linux. It protects the
# machine from what a worker could read or write; it says nothing about how
# one worker is kept out of another's files (see docs/SANDBOX.md).
#
#   dsh-sandbox.sh [options] -- COMMAND [ARGS...]
#   dsh-sandbox.sh --self-test
#
# Options:
#   --rw PATH      also writable, bound at the same path; repeatable. Give the
#                  workspace and the dsh home here.
#   --ro PATH      also visible read-only, bound at the same path; repeatable.
#                  For toolchains that live under $HOME (node, uv, a dsh checkout).
#   --env NAME     pass this variable from the caller's environment; repeatable.
#                  Its value is never printed.
#   --set N=VALUE  set a fixed, non-secret variable (for example DSH_HOME=...)
#   --path DIR     put DIR first on PATH (for a toolchain bound with --ro)
#   --cwd PATH     start in PATH (default: the current directory)
#   --no-net       no network at all (default: open, a hosted model needs it)
#   --tty          keep the controlling terminal (default detaches it)
#
# Besides what you name, the command gets only HOME, USER, LANG, TERM and PATH.
# / is read-only, the rest of $HOME is an empty tmpfs, /tmp is private, and
# other processes are not visible or signalable.
set -euo pipefail

rw=()
ro=()
envs=()
sets=()
extra_path=""
net=1
tty=0
cwd="$PWD"

die() { echo "dsh-sandbox.sh: $*" >&2; exit 2; }
command -v bwrap >/dev/null || die "bubblewrap (bwrap) is not installed"

build_args() {
  args=(--ro-bind / / --dev /dev --proc /proc --tmpfs /tmp --tmpfs "$HOME"
        --unshare-pid --unshare-ipc --unshare-uts --die-with-parent)
  [[ $tty -eq 1 ]] || args+=(--new-session)
  [[ $net -eq 1 ]] || args+=(--unshare-net)
  local p
  # Order matters: the tmpfs over $HOME comes first, the binds under it after.
  for p in ${ro[@]+"${ro[@]}"}; do
    [[ -e "$p" ]] || die "--ro path missing: $p"
    args+=(--ro-bind "$p" "$p")
  done
  for p in ${rw[@]+"${rw[@]}"}; do
    mkdir -p "$p"
    args+=(--bind "$p" "$p")
  done
  local path="/usr/local/bin:/usr/bin:/bin"
  [[ -z "$extra_path" ]] || path="$extra_path:$path"
  args+=(--clearenv --setenv HOME "$HOME" --setenv USER "${USER:-user}"
         --setenv LANG "${LANG:-C.UTF-8}" --setenv TERM "${TERM:-xterm}"
         --setenv PATH "$path")
  local n kv
  for n in ${envs[@]+"${envs[@]}"}; do
    [[ -n "${!n+x}" ]] || die "--env $n: not set in the caller's environment"
    args+=(--setenv "$n" "${!n}")
  done
  for kv in ${sets[@]+"${sets[@]}"}; do
    args+=(--setenv "${kv%%=*}" "${kv#*=}")
  done
  args+=(--chdir "$cwd")
}

# Under $HOME and /tmp only what is bound is visible; fail early, not inside bwrap.
check_cwd_visible() {
  local p
  case "$cwd/" in "$HOME"/*|/tmp/*) ;; *) return 0 ;; esac
  for p in ${ro[@]+"${ro[@]}"} ${rw[@]+"${rw[@]}"}; do
    case "$cwd/" in "$p"/*) return 0 ;; esac
  done
  die "--cwd $cwd is not visible inside the sandbox; bind it with --rw or --ro, or choose another --cwd"
}

run() { check_cwd_visible; build_args; exec bwrap "${args[@]}" "$@"; }

self_test() {
  local fails=0 canary="$HOME/.dsh-sandbox-selftest-canary" canarydir="$HOME/.dsh-sandbox-selftest-dir" w
  w="$(mktemp -d "$HOME/.dsh-sandbox-selftest.XXXXXX")"
  echo fake-secret > "$canary"; mkdir -p "$canarydir"
  export SELFTEST_KEY=fake-key-value SELFTEST_OTHER=should-not-pass
  check() { # name, expected (yes|no), command...
    local name="$1" want="$2"; shift 2
    local got=no
    if "$@" >/dev/null 2>&1; then got=yes; fi
    if [[ "$got" == "$want" ]]; then echo "PASS  $name"; else echo "FAIL  $name (wanted $want, got $got)"; fails=$((fails+1)); fi
  }
  inside() { "$0" --rw "$w" --cwd "$w" --env SELFTEST_KEY --set SELFTEST_FIXED=fixed -- "$@"; }
  check "a file in the real home is invisible"   no  inside cat "$canary"
  check "a directory in the real home is invisible" no  inside test -e "$canarydir"
  check "writing in the workspace works"         yes inside sh -c "echo ok > '$w/w.txt'"
  check "that write reached the host"            yes test -e "$w/w.txt"
  check "writing to /usr is refused"             no  inside sh -c 'echo x > /usr/sandbox-escape'
  check "writing to \$HOME lands in a tmpfs"     yes inside sh -c 'echo x > "$HOME/scratch-in-tmpfs"'
  check "that write did not reach the host"      no  test -e "$HOME/scratch-in-tmpfs"
  check "a passed variable is visible"           yes inside sh -c '[ "$SELFTEST_KEY" = fake-key-value ]'
  check "a --set variable is visible"            yes inside sh -c '[ "$SELFTEST_FIXED" = fixed ]'
  check "an unlisted variable is NOT visible"    no  inside sh -c '[ -n "${SELFTEST_OTHER:-}" ]'
  check "host processes are not visible"         yes inside sh -c '[ "$(ls /proc | grep -c "^[0-9]")" -lt 20 ]'
  if [[ -n "${DSH_SANDBOX_SKIP_NET:-}" ]]; then
    echo "SKIP  network checks (DSH_SANDBOX_SKIP_NET is set)"
  elif getent hosts example.com >/dev/null 2>&1; then
    check "outbound DNS works"                   yes inside getent hosts example.com
    check "--no-net blocks DNS"                  no  "$0" --no-net -- getent hosts example.com
  else
    echo "SKIP  network checks (this host cannot resolve names either)"
  fi
  rm -rf "$w" "$canary" "$canarydir"
  unset SELFTEST_KEY SELFTEST_OTHER
  if [[ $fails -eq 0 ]]; then echo "self-test: all passed"; else echo "self-test: $fails failed"; return 1; fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --rw) rw+=("$2"); shift 2 ;;
    --ro) ro+=("$2"); shift 2 ;;
    --env) envs+=("$2"); shift 2 ;;
    --set) [[ "$2" == *=* ]] || die "--set wants NAME=VALUE"; sets+=("$2"); shift 2 ;;
    --path) extra_path="$2"; shift 2 ;;
    --cwd) cwd="$2"; shift 2 ;;
    --no-net) net=0; shift ;;
    --tty) tty=1; shift ;;
    --self-test) self_test; exit $? ;;
    --) shift; break ;;
    -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
    *) die "unknown option $1 (use --help)" ;;
  esac
done
[[ $# -gt 0 ]] || die "no command given (use --help)"
run "$@"
