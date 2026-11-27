#!/usr/bin/env python3
"""Static assertions for the proxy dead-code and source-hygiene audit items.

The C++ suites compile the code, so they cannot see "this field has no reader",
"this include is redundant" or "this switch has no default:".  Those are
source-shape facts, and this file pins them together with the two audit rows
that live in codec warnings:

  01-P-14  codec.cpp uses std::abort() and must include <cstdlib>
  01-P-15  stream_pipeline.cpp must not keep a declared-but-unused codec ref
  01-P-06  a tool the Chat wire cannot represent is dropped *and reported*

  03-F-01  every proxy/src .cpp appears in the production source list
  03-F-02  every proxy/src header is included by something
  03-F-03  build outputs stay untracked (the `.gitignore` rule)
  03-F-04  every adapter module is registered and no KIND collides
  03-F-11  LiveRequest has no write-only field
  03-F-12  no type is declared and never used elsewhere
  03-F-13  window_pressure does not encode its invariant in the return value
  03-F-14  candidate_exhausted_reset has no redundant local condition
  03-F-15  usage_window_name enumerates every kind and keeps a fall-through
  03-F-18  no `#if 0` block anywhere in proxy/src
  03-F-24  proxy_server.cpp does not include what it never uses
  03-F-25  router.cpp does not include candidate_selection.h

Usage:
  static_source_test.py <project_root> <format_conv_test>
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys
from pathlib import Path

FAILURES: list[str] = []


def check(ok: bool, what: str) -> None:
    if ok:
        print(f"  ok:   {what}")
    else:
        print(f"  FAIL: {what}")
        FAILURES.append(what)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def occurrences(blob: str, name: str) -> int:
    return len(re.findall(rf"\b{re.escape(name)}\b", blob))


def production_sources(src: Path) -> dict[str, str]:
    return {
        str(path.relative_to(src)): read(path)
        for path in sorted(src.rglob("*"))
        if path.suffix in (".h", ".cpp") and "third_party" not in path.parts
    }


def check_source_hygiene(project: Path, sources: dict[str, str]) -> None:
    print("--- source hygiene (01-P-14/P-15, 03-F-01/02/03/18) ---")
    codec = sources["format/codec.cpp"]
    check("#include <cstdlib>" in codec,
          "P-14: codec.cpp includes <cstdlib> for std::abort")

    pipeline = sources["core/stream_pipeline.cpp"]
    uses = occurrences(pipeline, "harness_codec")
    check(uses == 0 or uses >= 2,
          f"P-15: stream_pipeline.cpp has no write-only harness_codec ({uses})")

    cmake = read(project / "proxy" / "CMakeLists.txt")
    missing = [
        name for name in sources
        if name.endswith(".cpp") and Path(name).name not in cmake
    ]
    check(not missing, f"F-01: every proxy/src .cpp is in proxy/CMakeLists.txt ({missing})")

    include_names = {
        Path(match.group(1)).name
        for text in sources.values()
        for match in re.finditer(r'#\s*include\s+"([^"]+)"', text)
    }
    orphans = [
        name for name in sources
        if name.endswith(".h") and Path(name).name not in include_names
    ]
    check(not orphans, f"F-02: every proxy/src header is included ({orphans})")

    gitignore = read(project / ".gitignore")
    check(re.search(r"^\s*build/\s*$", gitignore, re.M) is not None,
          "F-03: build outputs stay untracked via .gitignore")

    dead_branches = [
        name for name, text in sources.items()
        if re.search(r"^\s*#\s*if\s+0\b", text, re.M)
    ]
    check(not dead_branches, f"F-18: no #if 0 block in proxy/src ({dead_branches})")


def check_dead_symbols(sources: dict[str, str]) -> None:
    print("--- dead symbols removed (03-F-11/12/13/14/15/24/25) ---")
    blob = "\n".join(sources.values())

    started_at = occurrences(blob, "started_at")
    check(started_at == 0 or started_at >= 2,
          f"F-11: LiveRequest::started_at is not write-only ({started_at})")

    selection = sources["core/candidate_selection.h"]
    check("return out->remaining > 0" not in selection,
          "F-13: window_pressure keeps its invariant out of the return value")
    check("assert(out->remaining > 0)" in selection,
          "F-13: the remaining-pressure invariant is asserted")
    check("vetos" not in selection,
          "F-14: candidate_exhausted_reset has no redundant vetos local")

    window_fn = selection.split("inline const char *usage_window_name")[-1]
    window_fn = window_fn.split("\n}")[0]
    cases = re.findall(r"case UsageWindowKind::", window_fn)
    check(len(cases) == 3, f"F-15: usage_window_name enumerates every kind ({len(cases)})")
    # Strip // comments first: the fix documents itself by *mentioning* a
    # `default:` label, and a substring test would read that prose as code.
    window_code = "\n".join(
        line.split("//", 1)[0] for line in window_fn.splitlines())
    check(re.search(r"^\s*default\s*:", window_code, re.M) is None,
          "F-15: the switch keeps -Wswitch exhaustiveness (no default: label)")
    check('return ""' in window_fn,
          "F-15: the documented fall-through return is kept")

    server = sources["core/proxy_server.cpp"]
    redundant = [
        header for header in ("endpoint_policy.h", "db.h", "request_context.h",
                              "upstream_client.h", "usage_recorder.h")
        if re.search(rf'#\s*include\s+"{re.escape(header)}"', server)
    ]
    check(not redundant,
          f"F-24: proxy_server.cpp drops unused includes ({redundant})")

    router = sources["core/router.cpp"]
    router_includes = set(re.findall(r'#\s*include\s+"([^"]+)"', router))
    check(not any(pathlib.Path(name).name == "candidate_selection.h"
                  for name in router_includes),
          "F-25: router.cpp does not include candidate_selection.h")

    # 01-P-11: the group merge in the Anthropic serializer carries `content`
    # only.  Resetting the accumulator's Message-level fields is what the audit
    # flagged, and it is not observable on the wire (the merge happens on the
    # serializer's local vector), so the guard is source-level and cites the
    # implementation t3 landed (format_anthropic_request.cpp, the
    # `normalized_items.back()` branch).
    anthropic_request = sources["format/format_anthropic_request.cpp"]
    merge_start = anthropic_request.find(
        "normalized_items.back().group_id == item.group_id")
    if merge_start < 0:
        check(False, "P-11: the group-merge branch is still present")
    else:
        merge_block = anthropic_request[merge_start:merge_start + 700]
        check("merged.content.insert" in merge_block,
              "P-11: the group merge appends content")
        check("merged.item_kind" not in merge_block and
              "merged.extra" not in merge_block,
              "P-11: the group merge keeps the first item's Message-level "
              "extra/item_kind (no reset)")

    declared = set()
    for text in sources.values():
        declared |= {
            match.group(1)
            for match in re.finditer(
                r"^\s*(?:template\s*<[^>]*>\s*)?(?:struct|class|enum\s+class|enum)"
                r"\s+([A-Za-z_]\w*)\s*\{",
                text, re.M)
        }
    unused = sorted(name for name in declared if occurrences(blob, name) < 2)
    check(not unused, f"F-12: no type is declared and never used ({unused})")


def check_adapter_registry(project: Path) -> None:
    print("--- adapter registry (03-F-04) ---")
    package = project / "app" / "services" / "agent_usage"
    registry = read(package / "registry.py")
    imported = set(
        re.findall(r"from \.adapters import ([A-Za-z_, ]+)", registry)
    )
    modules = {
        name.strip()
        for group in imported
        for name in group.split(",")
        if name.strip()
    }
    tuple_block = registry.split("for module in (", 1)[1].split(")", 1)[0]
    registered = [
        name.strip().rstrip(",")
        for name in tuple_block.replace("\n", " ").split(",")
        if name.strip()
    ]
    # An adapter is a module that declares KIND; shared helpers (pi_common,
    # zstd_reader) are imported by adapters but never registered.
    adapter_files = {
        path.stem
        for path in (package / "adapters").glob("*.py")
        if path.stem != "__init__"
        and re.search(r"^KIND\s*=", read(path), re.M) is not None
    }
    check(set(registered) == adapter_files,
          f"F-04: every adapter module is registered "
          f"(unregistered={sorted(adapter_files - set(registered))})")
    check(set(registered) == modules,
          f"F-04: registry imports match the registered tuple "
          f"(diff={sorted(modules ^ set(registered))})")
    check(len(registered) == len(set(registered)),
          "F-04: no adapter module is registered twice (KIND collision)")


def run_conversion(binary: Path, args: list[str], body: str) -> tuple[int, str, str]:
    completed = subprocess.run(
        [str(binary), *args],
        input=body, capture_output=True, text=True, timeout=60,
    )
    return completed.returncode, completed.stdout, completed.stderr


def check_codec_warnings(binary: Path) -> None:
    print("--- conversion warnings (01-P-06, 01-P-03) ---")
    dropped_request = (
        '{"model":"m","stream":false,"input":['
        '{"type":"message","role":"user","content":'
        '[{"type":"input_text","text":"hi"}]}],'
        '"tools":[{"type":"web_search"}]}')
    code, out, err = run_conversion(
        binary, ["--request", "--from", "responses", "--to", "openai"],
        dropped_request)
    check(code == 0, f"P-06: the responses→openai conversion succeeds ({code})")
    check("web_search" not in out,
          "P-06: a Hosted tool is not sent to a Chat upstream")
    check("dropped tool" in err and "web_search" in err,
          "P-06: dropping a tool is reported on stderr")

    unknown_effort = (
        '{"model":"m","stream":false,"max_tokens":10,"reasoning_effort":'
        '"not-a-tier","messages":[{"role":"user","content":"hi"}]}')
    code, _out, err = run_conversion(
        binary, ["--request", "--from", "openai", "--to", "anthropic"],
        unknown_effort)
    check(code == 0, f"P-03: the openai→anthropic conversion succeeds ({code})")
    check('unknown reasoning effort' in err and "not-a-tier" in err,
          "P-03: an unknown effort tier is reported on stderr")


def main() -> None:
    project = Path(sys.argv[1]).resolve()
    format_conv_test = Path(sys.argv[2]).resolve()
    src = project / "proxy" / "src"
    sources = production_sources(src)

    check_source_hygiene(project, sources)
    check_dead_symbols(sources)
    check_adapter_registry(project)
    check_codec_warnings(format_conv_test)

    print(f"\n{'ALL PASSED' if not FAILURES else 'FAILED'} "
          f"({len(FAILURES)} failure{'s' if len(FAILURES) != 1 else ''})")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
