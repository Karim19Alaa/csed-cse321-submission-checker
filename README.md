# submission-checker (central repo; students can read it)

A composite GitHub Action that checks jar submissions for **any number of assignments**.
Students call it from a manually triggered workflow in their own repo:

    - uses: actions/checkout@v4
    - uses: YOUR-ORG/submission-checker@v1
      with:
        assignment: triangle

## Layout
    action.yml                       the action (reads the assignment's Java version, then runs the check)
    core/                            grader.py (shared engine), check_submission.py
    assignments/<id>/config.toml     title, java_version, limits, output format, advice messages
    assignments/<id>/tests_public/   NAME.in (+ NAME.out). No .out = performance/format smoke test
    assignments/<id>/smoke.py        optional: expected_outputs(input_text) -> int | None
    assignments/<id>/checker.py      optional: custom way to run and judge a test (see below)
    assignments/_template/           copy this to start a new assignment
    assignments/_examples/           complete examples (not selectable by students)
    scripts/lint_assignments.py      run by CI on every push

## Add an assignment
1. `cp -r assignments/_template assignments/hw2` (id: lowercase letters, digits, `-`, `_`).
2. Edit `config.toml` (limits, Java version, `format_regex`, messages); add public tests.
   Tests with a `.out` are answer-checked; tests without one only check time, memory, count and format.
3. If the input format needs it, implement `smoke.py`.
4. Create the matching private folder in the instructor repo (`assignments/hw2/` with the SAME limits
   plus the hidden `tests/`).
5. Push, let the lint job pass, then publish (see below). Student template for hw2 = same workflow with `assignment: hw2`.

## Assignments that aren't "stdin -> stdout, compare with an expected file"
Add `checker.py` with `check(ctx)`. It decides *how* a test is run and judged, so it can pass file paths,
run the jar several times (e.g. compress, then decompress), validate any-valid-answer output, check a property,
or give partial credit. `ctx.run(*args)` runs the student's jar under the time/memory limits; `ctx.problem(...)`
turns a failed run into a verdict. `assignments/_template/checker.py` documents the whole interface and
`assignments/_examples/roundtrip/` is a worked example (compress -> decompress -> compare, ratio-based partial credit).
Set `[tests] input_glob = "*"` when test inputs are not `*.in` files. Limits apply to **each** run.
Only the student's *jar* is ever run; the checker is trusted code. A checker in this repo is readable by students,
so put thresholds you want secret in the instructor repo's overlay `config.toml` (`[params]`, `[test_overrides.*]`).
