#!/usr/bin/env python3
"""Assert the Python test-interpreter contract.

Two independent properties:

``guard``
    Configuring with an interpreter that lacks the test dependencies must fail
    at configure time with a message that names the interpreter, the missing
    module, the required modules and the command that fixes it.  The negative
    control is a throw-away virtualenv built from the configured interpreter
    itself, so nothing here depends on a machine-specific install path.

``conda-base``
    The documented recovery works without an activated conda: conda is not on
    PATH and ``CONDA_PREFIX`` is unset in this repository's default shell, yet
    the conda base interpreter must provide the test dependencies, both through
    ``<prefix>/bin/conda run -n base`` and through ``<prefix>/bin/python``.  The
    prefix is derived from the configured interpreter (or ``--conda-prefix``),
    never hard-coded.

Usage: check_python_env.py <repository-root> [--python INTERPRETER]
                           [--conda-prefix PREFIX] [--only guard|conda-base]
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TEST_MODULES = "import flask, requests, jinja2"
GUARD_MESSAGES = (
    "The Python interpreter used by the test suite is missing dependencies",
    "interpreter:",
    "missing module:",
    "required modules: flask, requests, jinja2",
    "fix: re-run cmake",
)
ENV_DROP_KEYS = (
    "PY",
    "PYTHONPATH",
    "PYTHONHOME",
    "VIRTUAL_ENV",
    "CONDA_PREFIX",
    "CONDA_DEFAULT_ENV",
    "CONDA_SHLVL",
    "CONDA_PROMPT_MODIFIER",
    "CONDA_EXE",
)


def sanitized_env(*, path: str | None = None) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if key not in ENV_DROP_KEYS}
    if path is not None:
        env["PATH"] = path
    return env


def run(command: list[str], *, env: dict[str, str], cwd: Path | None = None):
    return subprocess.run(command, cwd=cwd, env=env, text=True,
                          capture_output=True)


def check_guard(root: Path, python: str, work: Path) -> list[str]:
    failures: list[str] = []
    cmake = shutil.which("cmake")
    if cmake is None:
        return ["cmake is not on PATH; the configure-time guard cannot be driven"]
    project = root / "proxy"
    if not (project / "CMakeLists.txt").is_file():
        return [f"{project} does not look like the proxy source tree"]

    venv = work / "dep-less"
    created = run([python, "-m", "venv", "--without-pip", str(venv)],
                  env=sanitized_env())
    if created.returncode != 0:
        return [f"could not build the negative-control virtualenv: "
                f"{created.stderr.strip()}"]
    venv_python = venv / "bin" / "python"
    probe = run([str(venv_python), "-c", "import flask"], env=sanitized_env())
    if probe.returncode == 0:
        return [f"{venv_python} already imports flask; the negative control "
                "would not prove anything"]

    env = sanitized_env()
    rejected = run([cmake, "-S", str(project), "-B", str(work / "bad-build"),
                    "-DBUILD_TESTING=ON",
                    f"-DPython3_EXECUTABLE={venv_python}"], env=env)
    output = rejected.stdout + rejected.stderr
    if rejected.returncode == 0:
        failures.append("configure accepted an interpreter without the test "
                        "dependencies")
    for expected in GUARD_MESSAGES:
        if expected not in output:
            failures.append(f"configure error does not contain {expected!r}")
    if str(venv_python) not in output:
        failures.append("configure error does not name the rejected interpreter")

    accepted = run([cmake, "-S", str(project), "-B", str(work / "good-build"),
                    "-DBUILD_TESTING=ON",
                    f"-DPython3_EXECUTABLE={python}"], env=env)
    if accepted.returncode != 0:
        failures.append("configure rejected the configured interpreter: " +
                        (accepted.stdout + accepted.stderr).strip())
    if failures:
        return failures
    print(f"[TB-PYTHON-ENV] guard ok: {venv_python} rejected at configure time, "
          f"{python} accepted")
    return []


def conda_prefix_for(python: str, explicit: str | None) -> Path | None:
    if explicit:
        candidate = Path(explicit).resolve()
        return candidate if (candidate / "bin" / "conda").is_file() else None
    candidate = Path(python).resolve().parent.parent
    if (candidate / "bin" / "conda").is_file() and (candidate / "conda-meta").is_dir():
        return candidate
    return None


def check_conda_base(python: str, explicit: str | None) -> list[str]:
    prefix = conda_prefix_for(python, explicit)
    if prefix is None:
        return [f"the configured interpreter {python} is not a conda prefix and "
                "--conda-prefix was not given; the documented conda base "
                "recovery cannot be checked"]
    conda = prefix / "bin" / "conda"
    base_python = prefix / "bin" / "python"
    # The repository's default shell has conda off PATH and CONDA_PREFIX unset;
    # reproduce that instead of trusting whatever this process inherited.
    env = sanitized_env(path="/usr/bin:/bin")
    failures: list[str] = []
    version = run([str(base_python), "-c",
                   "import sys; print(sys.version.split()[0])"], env=env)
    if version.returncode != 0:
        return [f"cannot run {base_python}: {version.stderr.strip()}"]

    run_env = sanitized_env(path="/usr/bin:/bin")
    documented = run([str(conda), "run", "-n", "base", "python", "-c",
                      TEST_MODULES], env=run_env)
    if documented.returncode != 0:
        failures.append(
            f"{conda} run -n base does not provide the test dependencies: "
            f"{(documented.stdout + documented.stderr).strip()}")
    direct = run([str(base_python), "-c", TEST_MODULES], env=env)
    if direct.returncode != 0:
        failures.append(f"{base_python} does not import flask, requests, jinja2")
    if shutil.which("conda") is not None:
        print(f"[TB-PYTHON-ENV] note: conda is on this shell's PATH at "
              f"{shutil.which('conda')}; the check does not rely on it")
    if failures:
        return failures
    print(f"[TB-PYTHON-ENV] conda-base ok: {base_python} "
          f"({version.stdout.strip()}) and "
          f"{conda} run -n base provide the test dependencies without activation")
    return []


def main() -> None:
    args = list(sys.argv[1:])
    root = Path(args.pop(0) if args and not args[0].startswith("-") else ".").resolve()
    python = sys.executable
    explicit_prefix: str | None = None
    only = "both"
    while args:
        flag = args.pop(0)
        if flag == "--python":
            python = args.pop(0)
        elif flag == "--conda-prefix":
            explicit_prefix = args.pop(0)
        elif flag == "--only":
            only = args.pop(0)
        else:
            raise SystemExit(f"unknown argument: {flag}")
    if only not in ("guard", "conda-base", "both"):
        raise SystemExit(f"--only must be guard, conda-base or both: {only}")

    failures: list[str] = []
    work = Path(tempfile.mkdtemp(prefix="tb-python-env-"))
    try:
        if only in ("guard", "both"):
            failures.extend(check_guard(root, python, work))
        if only in ("conda-base", "both"):
            failures.extend(check_conda_base(python, explicit_prefix))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if failures:
        raise SystemExit("python test-environment contract failed:\n" +
                         "\n".join(failures))
    print("[TB-PYTHON-ENV] python test-environment contract passed")


if __name__ == "__main__":
    main()
