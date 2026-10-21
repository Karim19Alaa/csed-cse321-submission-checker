#!/usr/bin/env python3
"""
Student-facing submission check, shared by all assignments.

    python check_submission.py --assignment triangle --submissions submissions

Per-assignment settings live in assignments/<name>/ :
    config.toml     limits, Java version, output format, custom advice messages
    tests_public/   NAME.in (+ NAME.out for answer-checked tests; without .out it is a smoke/performance test)
    smoke.py        optional hook: expected_outputs(input_text) -> int | None
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import re
import sys
from pathlib import Path

import grader

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")      # also blocks path traversal; '_template' is not selectable
ADVICE = {
    "TLE": "Your program is too slow.",
    "MLE": "Your program used too much memory.",
    "RE": "Your program crashed. See the message above; try running it locally on the same input.",
    "OLE": "Your program printed far too much output.",
    "WA": "The output differs from the expected answer.",
    "PE": "The output format is wrong.",
    "IE": "The checker itself failed - this is not your fault; please tell the instructor.",
}


def die(msg: str) -> "None":
    print(f"::error title=Checker setup::{msg}" if os.environ.get("GITHUB_ACTIONS") == "true" else f"error: {msg}")
    sys.exit(2)


def load_assignment(name: str, adir: Path):
    if not NAME_RE.match(name):
        die(f"invalid assignment name '{name}' (lowercase letters, digits, - and _)")
    d = adir / name
    if not (d / "config.toml").is_file():
        avail = sorted(p.name for p in adir.iterdir() if NAME_RE.match(p.name) and (p / "config.toml").is_file())
        die(f"unknown assignment '{name}'. Available: {', '.join(avail) or 'none'}")
    cfg = grader.load_config(d / "config.toml")
    smoke = None
    if (d / "smoke.py").is_file():
        spec = importlib.util.spec_from_file_location("smoke", d / "smoke.py")
        smoke = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(smoke)
    return d, cfg, smoke


def limits_for(cfg: dict, name: str) -> tuple[float, int]:
    ov = cfg["test_overrides"].get(name, {})
    return (float(ov.get("time_limit_s", cfg["limits"]["time_limit_s"])),
            int(ov.get("memory_limit_mb", cfg["limits"]["memory_limit_mb"])))


def main() -> int:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser()
    ap.add_argument("--assignment", required=True)
    ap.add_argument("--assignments-dir", type=Path, default=here.parent / "assignments")
    ap.add_argument("--submissions", type=Path, default=Path("submissions"))
    ap.add_argument("--jar", type=Path)
    ap.add_argument("--print-java-version", action="store_true", help="print java_version=<v> and exit")
    args = ap.parse_args()

    adir, cfg, smoke = load_assignment(args.assignment, args.assignments_dir)
    if args.print_java_version:
        print(f"java_version={cfg['assignment']['java_version']}")
        return 0
    gh = os.environ.get("GITHUB_ACTIONS") == "true"
    title = cfg["assignment"]["title"] or args.assignment
    rows: list[tuple[str, str, str]] = []
    failed = False

    def fail(check: str, status: str, detail: str, file: str | None = None) -> None:
        nonlocal failed
        failed = True
        rows.append((check, status, detail))
        if gh:
            print(f"::error {'file=' + file + ',' if file else ''}title={check}::{detail}")

    jar = args.jar
    if jar is not None:                              # explicit path (a workflow input): must be a file inside the repo
        root = Path.cwd().resolve()
        try:
            resolved = jar.resolve(strict=True)
        except (FileNotFoundError, OSError):
            fail("Jar file", "FAIL", f"'{jar}' does not exist - check the path (relative to the repository root)")
            return finish(title, rows, failed)
        if not resolved.is_file() or not resolved.is_relative_to(root):
            fail("Jar file", "FAIL", f"'{jar}' must be a file inside the repository")
            return finish(title, rows, failed)
        jar = resolved
    if jar is None:
        jars = sorted(args.submissions.glob("*.jar"))
        if len(jars) != 1:
            fail("Jar file", "FAIL", f"put exactly one <student_id>.jar in '{args.submissions}/' "
                 f"(found {len(jars)}: {', '.join(j.name for j in jars) or 'none'})")
            return finish(title, rows, failed)
        jar = jars[0]
    rows.append(("Jar file", "OK", jar.name))

    if not re.match(cfg["submission"]["id_pattern"], jar.stem):
        fail("File name", "FAIL", f"'{jar.name}' must be named <student_id>.jar (letters, digits, - and _ only)", str(jar))
    else:
        rows.append(("File name", "OK", f"student id = {jar.stem}"))
    ok, msg = grader.validate_jar(jar, cfg)
    if not ok:
        fail("Runnable jar", "FAIL", msg, str(jar))
        return finish(title, rows, failed)
    rows.append(("Runnable jar", "OK", msg))
    ok, msg, warns = grader.check_sources(jar, cfg)
    if not ok:
        fail("Source code", "FAIL", msg, str(jar))
        return finish(title, rows, failed)
    if cfg["submission"].get("require_sources"):
        rows.append(("Source code", "WARN" if warns else "OK", msg + ("; " + "; ".join(warns) if warns else "")))
        if warns and gh:
            print(f"::warning title=Source code::{'; '.join(warns)}")

    hook = grader.load_hook(adir / "checker.py")
    tests = grader.discover_tests(adir / "tests_public", cfg)
    for name, inp, exp in tests:
        tl, ml = limits_for(cfg, name)
        o = grader.evaluate_test(jar, name, inp, exp, cfg, hook, smoke, tl, ml)
        detail = f"{o.elapsed:.2f}s (limit {tl:g}s per run), peak {o.peak:.0f} MB"
        if o.metrics:
            detail += ", " + ", ".join(f"{k}={v}" for k, v in o.metrics.items())
        if o.message:
            detail += f" - {o.message}"
        label = f"Test '{name}'" + (" (performance)" if hook is None and exp is None else "")
        if o.status == "AC":
            slow = o.elapsed > 0.7 * tl * (2 if hook else 1)
            rows.append((label, "WARN" if slow else "OK",
                         detail + (" - close to the limit; the grading machine may be slower" if slow else "")))
            if slow and gh:
                print(f"::warning title={label}::{detail}")
        else:
            advice = cfg["messages"].get(o.status.lower()) or ADVICE.get(o.status, "")
            fail(label, o.status, f"{detail}. {advice}")
    return finish(title, rows, failed)


def finish(title: str, rows, failed) -> int:
    width = max(len(r[0]) for r in rows)
    print(f"\n{title}")
    for check, status, detail in rows:
        print(f"  [{status:<4}] {check:<{width}}  {detail}")
    verdict = ("FAILED - fix the problems above and run the check again." if failed else
               "PASSED - your jar is accepted by the grader's format and limits checks.")
    print(f"\n{verdict}\nNote: grading also uses hidden tests, so a pass here does not guarantee full marks.")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        icon = {"OK": "✅", "WARN": "⚠️"}
        with open(summary, "a") as f:
            f.write(f"## {title}: {'❌ failed' if failed else '✅ passed'}\n\n| Check | Result | Details |\n|---|---|---|\n")
            for check, status, detail in rows:
                f.write(f"| {check} | {icon.get(status, '❌')} {status} | {detail} |\n")
            f.write("\n_Grading also uses hidden tests, so passing here does not guarantee full marks._\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
