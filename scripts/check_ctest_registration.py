#!/usr/bin/env python3
"""Fail when a build tree's CTest registrations are stale.

CMake registers tests only while a build tree is configured: every add_test()
in a tests/CMakeLists.txt ends up in the generated CTestTestfile.cmake of that
build tree. Editing a tests/CMakeLists.txt afterwards leaves the generated file
untouched, so ``ctest`` in an already-configured build tree keeps running the
old test list and silently skips newly added tests. This gate compares the test
count the source tree currently declares (foreach expansion included) with the
``ctest --show-only=json-v1`` registration of a build directory, names the
missing/extra tests when the two disagree, and fails when a registered test has
no positive TIMEOUT property (a hung test would otherwise occupy CI forever).
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

SOURCE_CMAKELISTS = (
    "proxy/tests/CMakeLists.txt",
    "app/tests/CMakeLists.txt",
)

ADD_TEST_RE = re.compile(r"\badd_test\s*\(")
BLOCK_RE = re.compile(r"\b(foreach|endforeach)\s*\(")
STRING_RE = re.compile(r"\bstring\s*\(")
PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z0-9_]+)\}")


class RegistrationError(Exception):
    """The build tree or the source tree cannot be inspected."""


@dataclass
class ReplaceCommand:
    old: str
    new: str
    outvar: str
    input: str
    offset: int


@dataclass
class ForEachBlock:
    var: str | None
    items: list[str] | None
    start: int
    end: int = 0
    replacements: list[ReplaceCommand] = field(default_factory=list)


@dataclass
class SourceFile:
    path: Path
    declarations: int
    expanded: int | None
    names: list[str]
    unresolved: list[str]

    @property
    def names_complete(self) -> bool:
        return not self.unresolved


@dataclass
class BuildRegistration:
    total: int
    names: list[str]
    missing_timeout: list[str]


def strip_comments(text: str) -> str:
    """Blank out CMake comments while preserving line structure and offsets."""
    stripped: list[str] = []
    for line in text.splitlines(keepends=True):
        quote = False
        cut: int | None = None
        for index, char in enumerate(line):
            if char == '"':
                quote = not quote
            elif char == "#" and not quote:
                cut = index
                break
        stripped.append(line if cut is None else line[:cut] + "\n")
    return "".join(stripped)


def scan_args(text: str, open_index: int) -> list[str] | None:
    """Split the parenthesised argument list starting at open_index."""
    depth = 0
    quote = False
    start = open_index + 1
    for index in range(open_index, len(text)):
        char = text[index]
        if char == '"':
            quote = not quote
        elif quote:
            continue
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                try:
                    return shlex.split(text[start:index], posix=True)
                except ValueError:
                    return None
    return None


def parse_for_each_blocks(text: str) -> list[ForEachBlock]:
    blocks: list[ForEachBlock] = []
    stack: list[ForEachBlock] = []
    for match in BLOCK_RE.finditer(text):
        if match.group(1) == "foreach":
            args = scan_args(text, match.end() - 1) or []
            block = ForEachBlock(
                var=args[0] if args else None,
                items=list(args[1:]) if len(args) > 1 else None,
                start=match.end(),
                end=len(text),
            )
            blocks.append(block)
            stack.append(block)
        elif stack:
            stack.pop().end = match.start()
    for match in STRING_RE.finditer(text):
        args = scan_args(text, match.end() - 1)
        if not args or args[0].upper() != "REPLACE" or len(args) < 5:
            continue
        owner = None
        for block in blocks:
            if block.start <= match.start() <= block.end:
                if owner is None or block.start > owner.start:
                    owner = block
        if owner is None:
            continue
        owner.replacements.append(
            ReplaceCommand(
                old=args[1],
                new=args[2],
                outvar=args[3],
                input=args[4],
                offset=match.start(),
            )
        )
    return blocks


def enclosing_blocks(blocks: list[ForEachBlock], offset: int) -> list[ForEachBlock]:
    return sorted(
        (block for block in blocks if block.start <= offset <= block.end),
        key=lambda block: block.start,
    )


def expand_expression(expr: str, mapping: dict[str, list[str]]) -> list[str] | None:
    """Expand ${VAR} references; None when a variable is not known here."""
    names = list(dict.fromkeys(PLACEHOLDER_RE.findall(expr)))
    if not names:
        return [expr]
    results = [expr]
    for name in names:
        if name not in mapping:
            return None
        pattern = "${" + name + "}"
        results = [
            result.replace(pattern, value)
            for result in results
            for value in mapping[name]
        ]
    return results


def evaluate_replace(
    command: ReplaceCommand, mapping: dict[str, list[str]]
) -> list[str] | None:
    names = list(dict.fromkeys(PLACEHOLDER_RE.findall(command.input)))
    if not names:
        return [command.input.replace(command.old, command.new)]
    if len(names) > 1 or names[0] not in mapping:
        return None
    return [value.replace(command.old, command.new) for value in mapping[names[0]]]


def scope_at(
    blocks: list[ForEachBlock], offset: int
) -> tuple[dict[str, list[str]], int | None]:
    """Variables in scope at offset plus the loop multiplicity (None if unknown)."""
    mapping: dict[str, list[str]] = {}
    multiplicity = 1
    for block in enclosing_blocks(blocks, offset):
        if block.var is None or not block.items:
            return mapping, None
        values: list[str] = []
        for token in block.items:
            expanded = expand_expression(token, mapping)
            if expanded is None:
                return mapping, None
            values.extend(expanded)
        mapping[block.var] = values
        multiplicity *= len(values)
        for command in block.replacements:
            if command.offset > offset:
                continue
            derived = evaluate_replace(command, mapping)
            if derived is not None:
                mapping[command.outvar] = derived
    return mapping, multiplicity


def read_source_file(path: Path) -> SourceFile:
    text = strip_comments(path.read_text(encoding="utf-8"))
    blocks = parse_for_each_blocks(text)
    declarations = 0
    expanded: int | None = 0
    names: list[str] = []
    unresolved: list[str] = []
    for match in ADD_TEST_RE.finditer(text):
        declarations += 1
        line = text.count("\n", 0, match.start()) + 1
        args = scan_args(text, match.end() - 1)
        if args is None or "NAME" not in args:
            unresolved.append(f"add_test at line {line}")
            expanded = None
            continue
        name_index = args.index("NAME")
        name_expr = args[name_index + 1] if name_index + 1 < len(args) else None
        mapping, multiplicity = scope_at(blocks, match.start())
        if multiplicity is None or name_expr is None:
            unresolved.append(f"add_test at line {line}")
            expanded = None
            continue
        if expanded is not None:
            expanded += multiplicity
        expanded_names = expand_expression(name_expr, mapping)
        if expanded_names is None or len(expanded_names) != multiplicity:
            unresolved.append(name_expr)
            continue
        names.extend(expanded_names)
    return SourceFile(
        path=path,
        declarations=declarations,
        expanded=expanded,
        names=names,
        unresolved=unresolved,
    )


def parse_json_payload(stdout: str, command: list[str]) -> dict:
    """Parse ctest's JSON, tolerating diagnostic lines around the object."""
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        start = stdout.find("{")
        end = stdout.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(stdout[start:end + 1])
            except json.JSONDecodeError:
                pass
        raise RegistrationError(
            f"`{' '.join(command)}` printed no parsable JSON"
        ) from None


def property_value(test: dict, name: str) -> object:
    for prop in test.get("properties", []):
        if prop.get("name") == name:
            return prop.get("value")
    return None


def timeout_is_positive(value: object) -> bool:
    if value is None:
        return False
    values = value if isinstance(value, list) else [value]
    for item in values:
        try:
            if float(item) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def read_build_registration(build_dir: Path, ctest: str) -> BuildRegistration:
    command = [ctest, "--test-dir", str(build_dir), "--show-only=json-v1"]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as error:
        raise RegistrationError(f"cannot execute {ctest!r}: {error}") from error
    if completed.returncode != 0:
        raise RegistrationError(
            f"`{' '.join(command)}` exited {completed.returncode}: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    payload = parse_json_payload(completed.stdout, command)
    tests = payload.get("tests")
    if tests is None:
        raise RegistrationError(f"`{' '.join(command)}` printed no 'tests' list")
    names = [str(test.get("name", "")) for test in tests]
    missing_timeout = [
        name
        for test, name in zip(tests, names)
        if not timeout_is_positive(property_value(test, "TIMEOUT"))
    ]
    return BuildRegistration(
        total=len(tests), names=names, missing_timeout=missing_timeout
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--source-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help=(
            "repository root holding proxy/tests and app/tests "
            "(default: the repo this script lives in)"
        ),
    )
    parser.add_argument(
        "--build-dir",
        type=Path,
        default=None,
        help=(
            "build directory whose CTest registration is checked "
            "(default: <source-root>/proxy/build)"
        ),
    )
    parser.add_argument("--ctest", default="ctest", help="ctest executable")
    args = parser.parse_args(argv)

    source_root = args.source_root.resolve()
    build_dir = (args.build_dir or source_root / "proxy/build").resolve()

    missing = [name for name in SOURCE_CMAKELISTS if not (source_root / name).is_file()]
    if missing:
        print(
            f"ctest registration check error: missing {', '.join(missing)} "
            f"under {source_root}",
            file=sys.stderr,
        )
        return 2

    files = [read_source_file(source_root / name) for name in SOURCE_CMAKELISTS]
    detail = ", ".join(
        f"{source_file.path.relative_to(source_root)}: "
        f"{source_file.declarations} add_test -> {source_file.expanded}"
        for source_file in files
    )
    unknown = [source_file for source_file in files if source_file.expanded is None]
    if unknown:
        print(
            "ctest registration check error: cannot expand a foreach iteration "
            "list, so the source-side test count is unknown",
            file=sys.stderr,
        )
        for source_file in unknown:
            print(
                f"  {source_file.path.relative_to(source_root)}: "
                f"{', '.join(source_file.unresolved)}",
                file=sys.stderr,
            )
        return 2

    expected = sum(source_file.expanded for source_file in files)
    names_complete = all(source_file.names_complete for source_file in files)
    expected_names = {name for source_file in files for name in source_file.names}

    try:
        registration = read_build_registration(build_dir, args.ctest)
    except RegistrationError as error:
        print(f"ctest registration check error: {error}", file=sys.stderr)
        return 2

    actual = registration.total
    build_names = registration.names

    print(f"source root: {source_root}")
    print(f"build dir:   {build_dir}")
    print(f"source:      {expected} test(s) ({detail})")
    print(f"build tree:  {actual} test(s) registered by `ctest --show-only=json-v1`")

    failures: list[str] = []
    counts_match = expected == actual and (
        not names_complete or expected_names == set(build_names)
    )
    if not counts_match:
        failures.append("registration")
        print(
            f"ctest registration is stale: expected {expected}, actual {actual}, "
            f"difference {expected - actual}"
        )
        if not names_complete:
            print("test names: not fully resolved from source, listing counts only")
            for source_file in files:
                if source_file.unresolved:
                    print(
                        f"  {source_file.path.relative_to(source_root)}: "
                        f"{', '.join(source_file.unresolved)}"
                    )
        else:
            missing_names = sorted(expected_names - set(build_names))
            extra_names = sorted(set(build_names) - expected_names)
            print(
                f"missing ({len(missing_names)}): "
                f"{', '.join(missing_names) or 'none'}"
            )
            print(
                f"extra ({len(extra_names)}): "
                f"{', '.join(extra_names) or 'none'}"
            )

    if registration.missing_timeout:
        failures.append("timeout")
        print(
            "ctest TIMEOUT check failed: "
            f"{len(registration.missing_timeout)} test(s) have no positive "
            "TIMEOUT property:"
        )
        print("  " + ", ".join(registration.missing_timeout))

    if failures:
        print(
            "fix: re-run the cmake configure step for this build directory after "
            "editing any tests/CMakeLists.txt (for example `cmake -S proxy -B "
            f"{build_dir}`); set a positive TIMEOUT for every registered test"
        )
        return 1

    print("ctest registration is fresh and every test has a TIMEOUT")
    return 0


if __name__ == "__main__":
    sys.exit(main())
