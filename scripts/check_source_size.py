#!/usr/bin/env python3
"""Fail CI when production source files exceed the modularity budget."""

from __future__ import annotations

import sys
import re
from pathlib import Path


LIMITS = {".py": 500, ".cpp": 500, ".h": 300}
FORBIDDEN_CPP_INCLUDES = re.compile(r'^\s*#\s*include\s+["<][^">]+\.cpp[">]')
FORBIDDEN_FPRINTF = re.compile(r'\bfprintf\s*\(\s*stderr\s*,')
SILENT_EXCEPT = re.compile(r'^\s*except(?:\s+[^:]+)?:\s*(?:#.*)?$')
FORBIDDEN_V0_SQL = re.compile(
    r'\b(?:FROM|JOIN|INTO|UPDATE|DELETE\s+FROM)\s+'
    r'(?:upstream_accounts|local_keys|upstream_keys|plan_price_history|'
    r'token_usage|request_usage|cost)\b', re.IGNORECASE)


def main() -> None:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    search_roots = (root / "app", root / "proxy/src",
                    root / "schema/transitions")
    failures: list[str] = []
    checked = 0
    for search_root in search_roots:
        for path in search_root.rglob("*"):
            limit = LIMITS.get(path.suffix)
            if limit is None or "third_party" in path.parts or "tests" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            prefixes = ("#",) if path.suffix == ".py" else ("#", "//", "/*", "*", "*/")
            lines = sum(
                1 for line in text.splitlines()
                if line.strip() and not line.lstrip().startswith(prefixes)
            )
            checked += 1
            if lines > limit:
                failures.append(
                    f"{path.relative_to(root)}: {lines} logical lines > {limit}")
            if (path.parts and path.parts[0] == "app" and
                    "schema_upgrade" not in path.parts and
                    FORBIDDEN_V0_SQL.search(text)):
                failures.append(
                    f"{path.relative_to(root)}: production code queries a V0 table")
            if path.suffix in {".cpp", ".h"}:
                for lineno, line in enumerate(text.splitlines(), 1):
                    if FORBIDDEN_CPP_INCLUDES.match(line):
                        failures.append(
                            f"{path.relative_to(root)}:{lineno}: includes a .cpp file")
                if path.name != "logging.h" and FORBIDDEN_FPRINTF.search(text):
                    failures.append(
                        f"{path.relative_to(root)}: direct fprintf(stderr, is forbidden; use TB_LOG_*")
            if path.suffix == ".py":
                lines_text = text.splitlines()
                for index, line in enumerate(lines_text):
                    if not SILENT_EXCEPT.match(line):
                        continue
                    indent = len(line) - len(line.lstrip())
                    for following in lines_text[index + 1:index + 4]:
                        if not following.strip() or following.lstrip().startswith("#"):
                            continue
                        following_indent = len(following) - len(following.lstrip())
                        if following_indent > indent and following.strip() == "pass":
                            failures.append(
                                f"{path.relative_to(root)}:{index + 1}: silent except/pass")
                        break
    if failures:
        raise SystemExit("production source size limit exceeded:\n" +
                         "\n".join(failures))
    print(f"source size check passed ({checked} files)")


if __name__ == "__main__":
    main()
