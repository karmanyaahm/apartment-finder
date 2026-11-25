#!/usr/bin/env bash
# Refuse a commit that would publish the live dashboard URL, base path or tunnel hostname (AGENTS.md rule 10).
# Same checks as tests/test_no_secrets.py; see scripts/check_secrets.py.
#   As a pre-commit hook (checks the staged version of every tracked file):
#     ln -s ../../scripts/check_secrets.sh .git/hooks/pre-commit
#   As a commit-msg hook too (checks the message; git passes its file as $1):
#     ln -s ../../scripts/check_secrets.sh .git/hooks/commit-msg
#   By hand: scripts/check_secrets.sh --tree
set -euo pipefail
root="$(git rev-parse --show-toplevel)"
exec python3 "$root/scripts/check_secrets.py" "$@"
