#!/usr/bin/env python3
"""
Auto grader for Java jar submissions.

Submissions are named  <student_id>.jar  and are run as:
    java -Xmx<mem>m -Xss<stack>m ... -jar <student_id>.jar  < test.in  > captured stdout

Each test file (which may hold many test cases) is run once and compared with
the matching .out file.  Time and memory limits are configurable (config.toml
or command line).

Usage:
    python grader.py --submissions subs/            # grade every *.jar in a folder
    python grader.py --jar subs/20240001.jar        # grade a single jar
    python grader.py --submissions subs/ --time-limit 3 --memory-limit 128
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

try:  # optional, only used when /proc is unavailable (macOS, Windows)
    import psutil  # type: ignore
except ImportError:  # pragma: no cover
    psutil = None

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "java": {
        "command": "java",
        "stack_mb": 16,                 # -Xss
        "extra_args": ["-XX:+UseSerialGC", "-XX:TieredStopAtLevel=4"],
        "command_prefix": [],           # e.g. ["firejail", "--net=none", "--quiet"]
    },
    "limits": {
        "time_limit_s": 5.0,            # wall clock per test file (includes JVM start-up)
        "memory_limit_mb": 256,         # JVM heap (-Xmx)
        "jvm_overhead_mb": 128,         # extra RSS allowed on top of the heap (metaspace, JIT, stacks)
        "output_limit_mb": 16,          # max bytes the program may write to stdout
        "file_limit_mb": 64,            # max size of any single file the program may create
        "max_jar_mb": 50,
        "warmup": True,                 # one untimed run on the smallest test before the timed ones
        "warmup_time_limit_s": 10.0,    # generous limit for that warm-up run (its result is ignored)
    },
    "submission": {
        "id_pattern": r"^[A-Za-z0-9_\-]+$",
        "require_sources": False,       # true = the jar must contain the student's source files
        "source_extensions": [".java"],
        "min_source_files": 1,
    },
    "tests": {
        "input_glob": "*.in",           # which files in the tests folder are test inputs ("*" = every file except .out)
    },
    "params": {},                       # free-form parameters for an assignment's checker.py
    "checker": {
        "mode": "numeric",              # "numeric" or "exact"
        "tolerance": 1e-3,
        "format_regex": r"^-?\d+\.\d{3}$",   # set to "" to disable the format check
    },
    "scoring": {
        "total_points": 100,
    },
    "assignment": {
        "title": "",
        "java_version": "21",
    },
    "messages": {},                     # optional per-assignment advice, keys: tle, mle, re, wa, pe, ole
    "paths": {
        "tests_dir": "tests",
        "results_dir": "results",
    },
    # per-test overrides, e.g.  [test_overrides.big1]  time_limit_s = 10  weight = 3
    "test_overrides": {},
}


def deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(*paths: Path | None) -> dict:
    """Merge DEFAULT_CONFIG with each given TOML file in order (later files win)."""
    cfg = DEFAULT_CONFIG
    for path in paths:
        if path is None:
            continue
        with open(path, "rb") as f:
            cfg = deep_merge(cfg, tomllib.load(f))
    return cfg


# ----------------------------------------------------------------------------
# Data classes
# ----------------------------------------------------------------------------
@dataclass
class TestResult:
    name: str
    status: str            # AC, WA, TLE, MLE, RE, OLE, PE (format)
    time_s: float = 0.0
    peak_rss_mb: float = 0.0
    points: float = 0.0
    max_points: float = 0.0
    message: str = ""
    metrics: dict = field(default_factory=dict)


@dataclass
class SubmissionResult:
    student_id: str
    jar: str
    status: str = "OK"     # OK, BAD_NAME, BAD_JAR, NO_SOURCE
    message: str = ""
    score: float = 0.0
    tests: list = field(default_factory=list)


# ----------------------------------------------------------------------------
# Jar validation (re-usable by the student-facing submission checker)
# ----------------------------------------------------------------------------
def validate_jar(jar: Path, cfg: dict) -> tuple[bool, str]:
    """Static checks that don't need to run anything."""
    if not jar.is_file():
        return False, "file not found"
    if jar.suffix.lower() != ".jar":
        return False, "submission must be a .jar file"
    if jar.stat().st_size > cfg["limits"]["max_jar_mb"] * 1024 * 1024:
        return False, f"jar larger than {cfg['limits']['max_jar_mb']} MB"
    if not zipfile.is_zipfile(jar):
        return False, "file is not a valid jar/zip archive"
    try:
        with zipfile.ZipFile(jar) as z:
            names = z.namelist()
            mf = next((n for n in names if n.upper() == "META-INF/MANIFEST.MF"), None)
            if mf is None:
                return False, "no META-INF/MANIFEST.MF in the jar"
            text = z.read(mf).decode("utf-8", "replace")
    except zipfile.BadZipFile:
        return False, "corrupt jar archive"
    text = text.replace("\r\n ", "").replace("\n ", "")   # manifest line continuations
    m = re.search(r"^Main-Class:\s*(\S+)", text, re.MULTILINE)
    if not m:
        return False, "manifest has no Main-Class entry (java -jar would fail)"
    main_cls = m.group(1).replace(".", "/") + ".class"
    if main_cls not in names:
        return False, f"Main-Class '{m.group(1)}' not found inside the jar"
    return True, f"Main-Class = {m.group(1)}"


def check_sources(jar: Path, cfg: dict) -> tuple[bool, str, list[str]]:
    """Is the student's source code inside the jar? Returns (ok, message, warnings)."""
    sub = cfg["submission"]
    if not sub.get("require_sources"):
        return True, "not required", []
    exts = tuple(e.lower() for e in sub.get("source_extensions", [".java"]))
    need = int(sub.get("min_source_files", 1))
    try:
        with zipfile.ZipFile(jar) as z:
            srcs = [i for i in z.infolist() if not i.is_dir() and i.filename.lower().endswith(exts)]
            mf = next((n for n in z.namelist() if n.upper() == "META-INF/MANIFEST.MF"), None)
            manifest = z.read(mf).decode("utf-8", "replace").replace("\r\n ", "").replace("\n ", "") if mf else ""
    except zipfile.BadZipFile:
        return False, "corrupt jar archive", []
    nonempty = [i for i in srcs if i.file_size > 0]
    if len(nonempty) < need:
        what = "empty source files" if srcs else f"no source files ({', '.join(exts)})"
        return False, (f"{what} found in the jar - include your source code in the jar, "
                       f"e.g. `jar cfe <id>.jar Main Main.class Main.java`"), []
    warns = []
    m = re.search(r"^Main-Class:\s*(\S+)", manifest, re.MULTILINE)
    if m:
        simple = m.group(1).split(".")[-1]
        if not any(Path(i.filename).stem == simple for i in nonempty):
            warns.append(f"no source file named {simple}{exts[0]} (the Main-Class) was found")
    return True, f"{len(nonempty)} source file(s): " + ", ".join(sorted(i.filename for i in nonempty)[:6]), warns


def warm_up(jar: Path, cfg: dict, tests, hook, smoke=None) -> None:
    """One UNTIMED run on the smallest test, result ignored. A cold machine (first JVM start, jar and
    JDK files not yet in the OS cache) makes the first run much slower than later ones; this absorbs that."""
    lim = cfg["limits"]
    if not lim.get("warmup", True) or not tests:
        return
    name, inp, exp = min(tests, key=lambda t: t[1].stat().st_size)
    evaluate_test(jar, name, inp, exp, cfg, hook, smoke, float(lim["warmup_time_limit_s"]), int(lim["memory_limit_mb"]))


# ----------------------------------------------------------------------------
# Process execution with limits
# ----------------------------------------------------------------------------
@dataclass
class RunResult:
    status: str | None        # None = exited normally with code 0; else TLE / MLE / OLE / RE
    returncode: int | None
    stdout: bytes
    stderr: str
    elapsed: float
    peak_rss_mb: float


def rss_mb(pid: int) -> float:
    """Resident memory of a process (and its children, when psutil is available)."""
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
        return 0.0
    except OSError:
        pass
    if psutil is not None:
        try:
            p = psutil.Process(pid)
            total = p.memory_info().rss
            for c in p.children(recursive=True):
                total += c.memory_info().rss
            return total / (1024.0 * 1024.0)
        except Exception:
            return 0.0
    return 0.0


def kill_tree(p: subprocess.Popen) -> None:
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, AttributeError):
        try:
            p.kill()
        except Exception:
            pass


def build_command(jar: Path, cfg: dict, mem_mb: int, args=()) -> list[str]:
    j = cfg["java"]
    return [
        *j["command_prefix"],
        j["command"],
        f"-Xmx{mem_mb}m",
        f"-Xss{j['stack_mb']}m",
        *j["extra_args"],
        "-jar",
        str(Path(jar).resolve()),
        *[str(a) for a in args],
    ]


def run_jar_ex(jar: Path, args, cfg: dict, time_limit: float, mem_mb: int,
               stdin_path: Path | None = None, workdir: Path | None = None) -> RunResult:
    """Run `java -jar jar *args` once under the time / memory / output limits."""
    lim = cfg["limits"]
    rss_cap = mem_mb + lim["jvm_overhead_mb"]
    out_cap = lim["output_limit_mb"] * 1024 * 1024
    file_cap = int(lim["file_limit_mb"]) * 1024 * 1024
    cmd = build_command(jar, cfg, mem_mb, args)

    own_dir = workdir is None
    wd = tempfile.mkdtemp(prefix="grader_") if own_dir else str(workdir)
    out_f, err_f = tempfile.TemporaryFile(), tempfile.TemporaryFile()
    fin = open(stdin_path, "rb") if stdin_path else None

    def limit_files():                      # a program can't fill the disk with one huge file
        import resource
        resource.setrlimit(resource.RLIMIT_FSIZE, (file_cap, file_cap))

    status, peak = None, 0.0
    try:
        t0 = time.perf_counter()
        p = subprocess.Popen(cmd, stdin=fin if fin else subprocess.DEVNULL, stdout=out_f, stderr=err_f,
                             cwd=wd, start_new_session=True, preexec_fn=limit_files)
        while True:
            if p.poll() is not None:
                break
            if time.perf_counter() - t0 > time_limit:
                status = "TLE"; kill_tree(p); break
            r = rss_mb(p.pid)
            peak = max(peak, r)
            if r > rss_cap:
                status = "MLE"; kill_tree(p); break
            if os.fstat(out_f.fileno()).st_size > out_cap:
                status = "OLE"; kill_tree(p); break
            time.sleep(0.005)
        p.wait()
        elapsed = time.perf_counter() - t0
        out_f.seek(0); stdout = out_f.read(out_cap + 1)
        err_f.seek(0); stderr = err_f.read(4000).decode("utf-8", "replace")
        if status is None and p.returncode != 0:
            if "OutOfMemoryError" in stderr:
                status = "MLE"
            elif "File too large" in stderr:
                status = "OLE"
            else:
                status = "RE"
        return RunResult(status, p.returncode, stdout, stderr, elapsed, peak)
    finally:
        if fin:
            fin.close()
        out_f.close(); err_f.close()
        if own_dir:
            shutil.rmtree(wd, ignore_errors=True)


def run_jar(jar: Path, in_path: Path, cfg: dict, time_limit: float, mem_mb: int):
    """Plain stdin/stdout run (kept for simple callers)."""
    r = run_jar_ex(jar, [], cfg, time_limit, mem_mb, stdin_path=in_path)
    return r.status, r.stdout, r.stderr, r.elapsed, r.peak_rss_mb


def failure_message(status: str, stderr: str, tl: float, ml: int) -> str:
    if status == "TLE":
        return f"exceeded {tl:g}s"
    if status == "MLE":
        return f"exceeded memory limit ({ml} MB heap)"
    if status == "OLE":
        return "wrote too much output"
    lines = [l.strip() for l in stderr.splitlines() if l.strip()]
    hit = next((l for l in lines if re.search(r"(Exception|Error)\b", l)), None)
    return (hit or (lines[-1] if lines else "non-zero exit code"))[:200]


# ----------------------------------------------------------------------------
# Custom checkers (assignments/<id>/checker.py)
# ----------------------------------------------------------------------------
class Ctx:
    """Handed to an assignment's check(ctx). Lets the checker run the student's jar any number of
    times with any arguments (each run under the limits) and inspect the files it produces."""

    def __init__(self, jar, cfg, name, input_path, expected_path, workdir, time_limit, mem_mb):
        self.jar, self.cfg, self.name = jar, cfg, name
        self.input_path, self.expected_path = input_path, expected_path
        self.workdir = Path(workdir)                    # private scratch folder, deleted afterwards
        self.time_limit, self.mem_mb = time_limit, mem_mb   # limits PER RUN
        self.params = cfg.get("params", {})
        self.override = cfg.get("test_overrides", {}).get(name, {})
        self.metrics: dict = {}                         # shown in reports, e.g. ctx.metrics["ratio"] = 0.61
        self.elapsed, self.peak = 0.0, 0.0

    def run(self, *args, stdin=None, time_limit=None, memory_mb=None, cwd=None) -> RunResult:
        r = run_jar_ex(self.jar, args, self.cfg, time_limit or self.time_limit, memory_mb or self.mem_mb,
                       stdin_path=stdin, workdir=cwd or self.workdir)
        self.elapsed += r.elapsed
        self.peak = max(self.peak, r.peak_rss_mb)
        return r

    def problem(self, r: RunResult, phase: str):
        """None if the run was fine; otherwise a ready-made verdict tuple for the checker to return."""
        if r.status is None:
            return None
        return (r.status, f"{phase}: {failure_message(r.status, r.stderr, self.time_limit, self.mem_mb)}")


def load_hook(path: Path | None):
    if path is None or not Path(path).is_file():
        return None
    spec = importlib.util.spec_from_file_location("assignment_checker", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "check"):
        raise SystemExit(f"{path} must define check(ctx)")
    return mod


# ----------------------------------------------------------------------------
# Output checking (built-in stdin/stdout mode)
# ----------------------------------------------------------------------------
def check_output(expected: str, actual: str, chk: dict) -> tuple[str, str]:
    exp, act = expected.split(), actual.split()
    if len(act) != len(exp):
        return "WA", f"expected {len(exp)} values, got {len(act)}"
    fmt = re.compile(chk["format_regex"]) if chk.get("format_regex") else None
    tol = float(chk["tolerance"])
    for i, (e, a) in enumerate(zip(exp, act), 1):
        if chk["mode"] == "exact":
            if e != a:
                return "WA", f"value {i}: expected {e}, got {a}"
            continue
        if fmt and not fmt.match(a):
            return "PE", f"value {i}: '{a}' does not match required format"
        try:
            if abs(float(e) - float(a)) > tol + 1e-12:
                return "WA", f"value {i}: expected {e}, got {a}"
        except ValueError:
            return "PE", f"value {i}: '{a}' is not a number"
    return "AC", ""


@dataclass
class Outcome:
    status: str
    message: str = ""
    elapsed: float = 0.0
    peak: float = 0.0
    fraction: float = 1.0       # share of the test's points awarded when status == AC
    metrics: dict = field(default_factory=dict)


def evaluate_test(jar: Path, name: str, inp: Path, expected: Path | None, cfg: dict,
                  hook, smoke, tl: float, ml: int) -> Outcome:
    """Run ONE test. Uses the assignment's checker.py if there is one, else stdin/stdout comparison."""
    if hook is not None:
        wd = Path(tempfile.mkdtemp(prefix="grader_"))
        ctx = Ctx(jar, cfg, name, inp, expected, wd, tl, ml)
        try:
            res = hook.check(ctx)
            status, msg = res[0], res[1]
            frac = res[2] if len(res) > 2 and res[2] is not None else 1.0
        except Exception as e:            # a bug in the checker is not the student's fault
            status, msg, frac = "IE", f"internal checker error: {type(e).__name__}: {e}", 0.0
        finally:
            shutil.rmtree(wd, ignore_errors=True)
        return Outcome(status, msg, ctx.elapsed, ctx.peak, frac if status == "AC" else 0.0, ctx.metrics)

    r = run_jar_ex(jar, [], cfg, tl, ml, stdin_path=inp)
    if r.status is not None:
        return Outcome(r.status, failure_message(r.status, r.stderr, tl, ml), r.elapsed, r.peak_rss_mb)
    text = r.stdout.decode("utf-8", "replace")
    if expected is not None:
        status, msg = check_output(expected.read_text(), text, cfg["checker"])
    else:                                  # no expected answer (public performance test): count + format only
        toks = text.split()
        n_exp = smoke.expected_outputs(inp.read_text()) if smoke else None
        status, msg = "AC", ""
        if n_exp is not None and len(toks) != n_exp:
            status, msg = "WA", f"expected {n_exp} value(s), got {len(toks)}"
        elif not toks:
            status, msg = "WA", "no output"
        elif cfg["checker"].get("format_regex"):
            fmt = re.compile(cfg["checker"]["format_regex"])
            bad = next((t for t in toks if not fmt.match(t)), None)
            if bad:
                status, msg = "PE", f"'{bad}' does not match the required format"
    return Outcome(status, msg, r.elapsed, r.peak_rss_mb)


# ----------------------------------------------------------------------------
# Grading
# ----------------------------------------------------------------------------
def discover_tests(tests_dir: Path, cfg: dict) -> list[tuple[str, Path, Path | None]]:
    """(name, input, expected-or-None). Expected = same stem with .out, when it exists."""
    inputs = [p for p in sorted(tests_dir.glob(cfg["tests"]["input_glob"]))
              if p.is_file() and p.suffix != ".out" and not p.name.startswith(".")]
    stems = [p.stem for p in inputs]
    out = []
    for p in inputs:
        name = p.name if stems.count(p.stem) > 1 else p.stem
        exp = p.with_suffix(".out")
        out.append((name, p, exp if exp.exists() else None))
    return out


def grade_submission(jar: Path, cfg: dict, tests, hook, limits_override: dict) -> SubmissionResult:
    sid = jar.stem
    res = SubmissionResult(student_id=sid, jar=jar.name)

    if not re.match(cfg["submission"]["id_pattern"], sid):
        res.status, res.message = "BAD_NAME", f"file name '{jar.name}' is not <student_id>.jar"
        return res
    ok, msg = validate_jar(jar, cfg)
    if not ok:
        res.status, res.message = "BAD_JAR", msg
        return res
    ok, msg, warns = check_sources(jar, cfg)
    if not ok:
        res.status, res.message = "NO_SOURCE", msg
        return res
    if warns:
        res.message = "; ".join(warns)
    warm_up(jar, cfg, tests, hook)

    overrides = cfg["test_overrides"]
    weights = {name: float(overrides.get(name, {}).get("weight", 1)) for name, _, _ in tests}
    total_w = sum(weights.values()) or 1.0
    total_pts = float(cfg["scoring"]["total_points"])

    for name, inp, exp in tests:
        ov = overrides.get(name, {})
        tl = float(limits_override.get("time_limit_s") or ov.get("time_limit_s") or cfg["limits"]["time_limit_s"])
        ml = int(limits_override.get("memory_limit_mb") or ov.get("memory_limit_mb") or cfg["limits"]["memory_limit_mb"])
        max_pts = total_pts * weights[name] / total_w
        o = evaluate_test(jar, name, inp, exp, cfg, hook, None, tl, ml)
        pts = max_pts * o.fraction if o.status == "AC" else 0.0
        res.tests.append(TestResult(name, o.status, round(o.elapsed, 3), round(o.peak, 1),
                                    round(pts, 2), round(max_pts, 2), o.message, o.metrics))

    res.score = round(sum(t.points for t in res.tests), 2)
    return res


# ----------------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------------
def print_report(results: list[SubmissionResult]) -> None:
    for r in results:
        print(f"\n== {r.student_id}  ({r.jar})  score: {r.score}")
        if r.status != "OK":
            print(f"   {r.status}: {r.message}")
            continue
        if r.message:
            print(f"   note: {r.message}")
        for t in r.tests:
            extra = ("  " + " ".join(f"{k}={v}" for k, v in t.metrics.items())) if t.metrics else ""
            extra += f"  {t.message}" if t.message else ""
            print(f"   {t.name:<16}{t.status:<5}{t.time_s:>7.2f}s{t.peak_rss_mb:>8.0f}MB  "
                  f"{t.points:>6.2f}/{t.max_points:<6.2f}{extra}")


def write_reports(results: list[SubmissionResult], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "results.json", "w") as f:
        json.dump([asdict(r) for r in results], f, indent=2)
    test_names = sorted({t.name for r in results for t in r.tests})
    with open(out_dir / "summary.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["student_id", "status", "score", *[f"{n}" for n in test_names], "message"])
        for r in results:
            by = {t.name: t.status for t in r.tests}
            w.writerow([r.student_id, r.status, r.score, *[by.get(n, "") for n in test_names], r.message])


# ----------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Grade <student_id>.jar submissions")
    ap.add_argument("--config", type=Path, required=True, help="assignment config.toml")
    ap.add_argument("--base-config", type=Path, help="config merged UNDER --config (e.g. the public one)")
    ap.add_argument("--checker", type=Path, help="checker.py (default: checker.py next to --config, if present)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--submissions", type=Path, help="folder containing <id>.jar files")
    g.add_argument("--jar", type=Path, help="a single jar")
    ap.add_argument("--tests", type=Path, help="tests folder (default from config)")
    ap.add_argument("--results", type=Path, help="results folder (default from config)")
    ap.add_argument("--time-limit", type=float, help="seconds per run (overrides config)")
    ap.add_argument("--memory-limit", type=int, help="JVM heap in MB (overrides config)")
    args = ap.parse_args()

    cfg = load_config(args.base_config, args.config)
    base = args.config.parent
    tests_dir = args.tests or (base / cfg["paths"]["tests_dir"])
    results_dir = args.results or (base / cfg["paths"]["results_dir"])
    if args.checker is not None and not args.checker.is_file():
        print(f"checker not found: {args.checker}", file=sys.stderr)
        return 2
    hook = load_hook(args.checker or (base / "checker.py"))
    tests = discover_tests(tests_dir, cfg)
    if hook is None:                                    # stdin/stdout mode needs an expected answer
        for name, _, exp in tests:
            if exp is None:
                print(f"[warn] test '{name}' has no .out file, skipped", file=sys.stderr)
        tests = [t for t in tests if t[2] is not None]
    if not tests:
        print(f"no tests found in {tests_dir}", file=sys.stderr)
        return 2

    jars = [args.jar] if args.jar else sorted(args.submissions.glob("*.jar"))
    if args.submissions:
        for p in args.submissions.iterdir():
            if p.is_file() and p.suffix.lower() != ".jar":
                print(f"[warn] ignoring non-jar file: {p.name}", file=sys.stderr)
    limits_override = {"time_limit_s": args.time_limit, "memory_limit_mb": args.memory_limit}

    results = [grade_submission(j, cfg, tests, hook, limits_override) for j in jars]
    print_report(results)
    write_reports(results, results_dir)
    print(f"\nWrote {results_dir}/results.json and summary.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
