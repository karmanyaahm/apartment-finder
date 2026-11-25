"""Fail if the repo would publish the live dashboard's address (AGENTS.md rule 10).

Checks, per file:
  - any hostname under the owner's personal domain (the live tunnel hostname lives there);
  - a Cloudflare tunnel token (a long base64 JSON blob, `eyJ...`);
  - any word or hostname whose SHA-256 is in scripts/secret_hashes.txt (the base path and the live hostname are
    listed there as hashes only, so this file and the list never contain them).

  python3 scripts/check_secrets.py            # the staged (index) version of every tracked file: a pre-commit hook
  python3 scripts/check_secrets.py --tree     # the working-tree version of every tracked file
  python3 scripts/check_secrets.py FILE...    # these files, e.g. a commit message (commit-msg hook)

Findings name the file, line and check, never the matched text. Exit 1 on any finding.
Standard library only, so it runs as a git hook without the project's environment.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HASHES_FILE = ROOT / "scripts" / "secret_hashes.txt"

PERSONAL_DOMAIN = re.compile(r"malhotra\.cc\b", re.IGNORECASE)
TUNNEL_TOKEN = re.compile(r"eyJ[A-Za-z0-9_-]{40,}")
WORD = re.compile(r"[A-Za-z0-9]+")
HOSTLIKE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]*")


def load_hashes(path: Path = HASHES_FILE) -> set[str]:
    out = set()
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.add(line.split()[0].lower())
    return out


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def findings(name: str, text: str, hashes: set[str]) -> list[str]:
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        if PERSONAL_DOMAIN.search(line):
            out.append(f"{name}:{n}: hostname under the personal domain")
        if TUNNEL_TOKEN.search(line):
            out.append(f"{name}:{n}: looks like a tunnel token")
        tokens = set(WORD.findall(line)) | {t.strip(".-") for t in HOSTLIKE.findall(line)}
        if any(_sha(t) in hashes or _sha(t.lower()) in hashes for t in tokens if t):
            out.append(f"{name}:{n}: a string listed in scripts/secret_hashes.txt")
    return out


def _decode(data: bytes) -> str | None:
    if b"\0" in data[:8192]:
        return None  # binary
    return data.decode("utf-8", errors="replace")


def tracked_files(root: Path = ROOT) -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True).stdout
    return [p for p in out.decode().split("\0") if p]


def scan_tree(root: Path = ROOT) -> list[str]:
    hashes, out = load_hashes(), []
    for p in tracked_files(root):
        f = root / p
        if not f.is_file():
            continue
        text = _decode(f.read_bytes())
        if text is not None:
            out += findings(p, text, hashes)
    return out


def scan_index(root: Path = ROOT) -> list[str]:
    hashes, out = load_hashes(), []
    for p in tracked_files(root):
        r = subprocess.run(["git", "show", f":{p}"], cwd=root, capture_output=True)
        text = _decode(r.stdout) if r.returncode == 0 else None
        if text is not None:
            out += findings(p, text, hashes)
    return out


def main(argv: list[str]) -> int:
    if argv == ["--tree"]:
        found = scan_tree()
    elif argv:
        hashes = load_hashes()
        found = [x for a in argv for x in findings(a, Path(a).read_text(errors="replace"), hashes)]
    else:
        found = scan_index()
    for f in found:
        print(f, file=sys.stderr)
    if found:
        print("check_secrets: the live URL, base path and tunnel hostname never go in the repo (AGENTS.md rule 10)",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
