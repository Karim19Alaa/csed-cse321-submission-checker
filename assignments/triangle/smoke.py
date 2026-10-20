"""Optional hook. Return how many output values an input file requires (None = don't check the count).
Input format: repeated blocks of  n  followed by n lines 'x y'."""
def expected_outputs(text: str):
    tok, i, c = text.split(), 0, 0
    while i < len(tok):
        i += 1 + 2 * int(tok[i])
        c += 1
    return c
