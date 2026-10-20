#!/usr/bin/env python3
"""Sanity-check every assignment folder (run in CI on each push to the central repo)."""
import re, sys, tomllib
from pathlib import Path
root = Path(__file__).resolve().parent.parent / "assignments"
name_re = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
errors = []
for d in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("_")):
    if not name_re.match(d.name): errors.append(f"{d.name}: invalid folder name")
    cfg_path = d / "config.toml"
    if not cfg_path.is_file(): errors.append(f"{d.name}: missing config.toml"); continue
    try: cfg = tomllib.loads(cfg_path.read_text())
    except Exception as e: errors.append(f"{d.name}: config.toml does not parse ({e})"); continue
    if not re.fullmatch(r"\d+", str(cfg.get("assignment", {}).get("java_version", "21"))):
        errors.append(f"{d.name}: assignment.java_version must be a number like 21")
    tp = d / "tests_public"
    ins = {p.stem for p in tp.glob("*.in")}
    if not ins: errors.append(f"{d.name}: tests_public has no .in files")
    for o in tp.glob("*.out"):
        if o.stem not in ins: errors.append(f"{d.name}: {o.name} has no matching .in")
    if any(not (tp / f"{n}.out").exists() for n in ins) and not (d / "smoke.py").exists():
        errors.append(f"{d.name}: answer-less (performance) tests exist but there is no smoke.py")
    for p in d.rglob("*"):
        if p.is_file() and p.stat().st_size > 20 * 1024 * 1024: errors.append(f"{d.name}: {p.name} is over 20 MB")
    if any("hidden" in p.name.lower() for p in d.rglob("*")): errors.append(f"{d.name}: a file/folder named 'hidden' - hidden tests must not be in this repo")
print("\n".join(errors) or f"OK: {len([p for p in root.iterdir() if p.is_dir() and not p.name.startswith('_')])} assignment(s) pass lint")
sys.exit(1 if errors else 0)
