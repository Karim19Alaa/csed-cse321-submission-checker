"""OPTIONAL. Delete this file if plain 'stdin -> stdout, compare with .out' is enough.

Add check(ctx) when a test needs more than that: file arguments, several runs, a property instead of one
fixed answer, any-valid-answer validation, partial credit ...  It must return
    (status, message)            or      (status, message, fraction)
with status "AC", "WA", "PE", or the verdict of a failed run (see ctx.problem).

ctx gives you:
    ctx.run(*args, stdin=None, time_limit=None, memory_mb=None, cwd=None) -> RunResult
        runs `java -jar <student>.jar *args` under the limits; RunResult has .status (None = ok, or
        TLE/MLE/OLE/RE), .stdout (bytes), .stderr, .returncode, .elapsed
    ctx.problem(run_result, "phase")   None if the run was fine, else a verdict tuple you can `return`
    ctx.input_path, ctx.expected_path (None if there is no .out), ctx.workdir (private temp folder)
    ctx.params / ctx.override          [params] and [test_overrides.<name>] from config.toml
    ctx.metrics                        dict shown in reports, e.g. ctx.metrics["ratio"] = 0.6
See assignments/_examples/ for a complete example.  Never import student code; only run the jar.
"""


def check(ctx):
    r = ctx.run(ctx.input_path)
    if (bad := ctx.problem(r, "run")):
        return bad
    return ("AC", "")
