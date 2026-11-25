#!/usr/bin/env bash
# Deploy on the box (AGENTS.md rule 14): pull, wait until the dashboard runs no compute job, restart the service.
# A Google sampling or compute job runs in a thread of the dashboard process, so a restart mid-run kills it (and any
# Route Matrix request in flight, which Google may bill while we never log or cache it).
#   scripts/deploy.sh [--timeout SECONDS] [--no-pull]
# Run from the repo on the box. Exit 2: a job was still running at the timeout (nothing restarted; the pull is kept,
# it takes effect at the next restart). Exit 3: the status check answered something other than 200.
set -euo pipefail

main() {
  local timeout=1800 pull=1 port="${APTF_PORT:-8765}" base="" code state waited=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --timeout) timeout="$2"; shift 2 ;;
      --no-pull) pull=0; shift ;;
      *) echo "unknown argument $1" >&2; exit 64 ;;
    esac
  done
  cd "$(dirname "$0")/.."
  # the dashboard may be served under a secret base path (APTF_BASE_PATH in .env): read it, never print it
  if [ -f .env ]; then
    base="$(sed -n 's/^APTF_BASE_PATH=//p' .env | tail -n 1 | tr -d '"'"'"' ')"
  fi
  base="${base#/}"; base="${base%/}"
  local url="http://127.0.0.1:${port}${base:+/$base}/api/compute/status"

  if [ "$pull" = 1 ]; then
    if command -v git >/dev/null; then git pull --ff-only; else nix shell nixpkgs#git -c git pull --ff-only; fi
  fi

  while :; do
    code="$(curl -s -o /tmp/aptf-deploy-status.$$ -w '%{http_code}' --max-time 10 "$url" || true)"
    if [ "$code" = "000" ]; then
      echo "dashboard not answering: nothing to wait for"; break
    fi
    if [ "$code" != "200" ]; then
      echo "status check answered HTTP $code: not restarting" >&2; rm -f /tmp/aptf-deploy-status.$$; exit 3
    fi
    state="$(sed -n 's/.*"state": *"\([a-z_]*\)".*/\1/p' /tmp/aptf-deploy-status.$$)"
    if [ "$state" != "running" ]; then
      echo "no compute job running (state: ${state:-unknown})"; break
    fi
    if [ "$waited" -ge "$timeout" ]; then
      echo "a compute job is still running after ${timeout}s: not restarting. Rerun scripts/deploy.sh --no-pull" \
           "once it ends (Pipeline panel, or GET /api/compute/status)." >&2
      rm -f /tmp/aptf-deploy-status.$$; exit 2
    fi
    [ $((waited % 60)) -eq 0 ] && echo "a compute job is running; waiting (${waited}s of ${timeout}s)…"
    sleep 5; waited=$((waited + 5))
  done
  rm -f /tmp/aptf-deploy-status.$$

  systemctl --user restart aptfinder.service
  for _ in $(seq 1 60); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$url" || true)"
    if [ "$code" = "200" ]; then echo "restarted; dashboard answering"; return 0; fi
    sleep 2
  done
  echo "restarted, but the dashboard did not answer within 120 s (journalctl --user -u aptfinder.service)" >&2
  exit 1
}

main "$@"
