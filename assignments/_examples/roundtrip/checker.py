"""EXAMPLE checker: compress -> decompress -> compare. Copy and adapt for similar assignments.

Contract: check(ctx) returns (status, message) or (status, message, fraction).
  status   "AC" | "WA" | "PE" | or the status of a failed run (TLE / MLE / RE / OLE) via ctx.problem()
  fraction optional, 0..1: share of the test's points when status == "AC" (partial credit)
"""
import shutil


def first_diff(a, b):
    """Index of the first differing byte (or the shorter length), or None when the files are identical."""
    with open(a, "rb") as fa, open(b, "rb") as fb:
        pos = 0
        while True:
            ca, cb = fa.read(65536), fb.read(65536)
            if ca != cb:
                for i, (x, y) in enumerate(zip(ca, cb)):
                    if x != y:
                        return pos + i
                return pos + min(len(ca), len(cb))
            if not ca:
                return None
            pos += len(ca)


def check(ctx):
    wd = ctx.workdir
    for d in ("in", "packed", "out"):
        (wd / d).mkdir()
    src, packed, restored = wd / "in" / "input.dat", wd / "packed" / "result.bin", wd / "out" / "restored.dat"
    shutil.copyfile(ctx.input_path, src)                  # the student never touches the original test file

    r = ctx.run("compress", src, packed)
    if (bad := ctx.problem(r, "compress")):
        return bad
    if not packed.is_file():
        return ("WA", "compress did not create the output file")

    src.unlink()    # decompression must work from the compressed file alone (blocks 'store the path' tricks)
    r = ctx.run("decompress", packed, restored, cwd=wd / "out")
    if (bad := ctx.problem(r, "decompress")):
        return bad
    if not restored.is_file():
        return ("WA", "decompress did not create the output file")

    pos = first_diff(ctx.input_path, restored)
    if pos is not None:
        return ("WA", f"restored file differs from the original (first difference at byte {pos})")

    size_in, size_out = ctx.input_path.stat().st_size, packed.stat().st_size
    if size_in:
        ctx.metrics["ratio"] = round(size_out / size_in, 3)
    target = ctx.override.get("target_ratio", ctx.params.get("target_ratio", 0))
    if target and size_in and size_out / size_in > target:
        return ("AC", f"correct, but ratio {size_out / size_in:.3f} is above the target {target}",
                ctx.params.get("partial_credit", 0.5))
    return ("AC", "")
