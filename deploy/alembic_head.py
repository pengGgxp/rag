"""Print the packaged Alembic head without third-party dependencies."""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REVISION_RE = re.compile(
    r"^revision(?:\s*:\s*[^=]+)?\s*=\s*['\"]([^'\"]+)['\"]",
    re.MULTILINE,
)
DOWN_REVISION_RE = re.compile(
    r"^down_revision(?:\s*:\s*[^=]+)?\s*=\s*(.+)$",
    re.MULTILINE,
)


def main(path: str) -> int:
    directory = Path(path)
    versions = directory / "alembic" / "versions"
    if not (directory / "alembic.ini").is_file() or not versions.is_dir():
        print("Alembic configuration is incomplete", file=sys.stderr)
        return 1
    revisions: set[str] = set()
    parents: set[str] = set()
    for source in versions.glob("*.py"):
        text = source.read_text(encoding="utf-8")
        match = REVISION_RE.search(text)
        if not match:
            print(f"Alembic revision is missing in {source.name}", file=sys.stderr)
            return 1
        revisions.add(match.group(1))
        down = DOWN_REVISION_RE.search(text)
        if down:
            try:
                value = ast.literal_eval(down.group(1).strip())
            except (SyntaxError, ValueError):
                print(f"Invalid down_revision in {source.name}", file=sys.stderr)
                return 1
            if isinstance(value, str):
                parents.add(value)
            elif isinstance(value, (tuple, list)):
                parents.update(item for item in value if isinstance(item, str))
    heads = sorted(revisions - parents)
    if len(heads) != 1:
        print(f"expected one Alembic head, found {len(heads)}", file=sys.stderr)
        return 1
    print(heads[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
