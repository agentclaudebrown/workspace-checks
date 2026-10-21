# workspace-checks

A pre-commit and CI check that refuses text-encoding damage before it enters a
repository's history.

It refuses a commit that introduces:

- **a byte-order mark**, at the start of a file or anywhere in it;
- **a mojibake sequence** - UTF-8 that was read as cp1252 and written back as UTF-8 -
  recognised by its shape rather than from a list, and recovered to the original text;
- **a raw control byte** in a text file;
- **a text file that is not UTF-8**.

It runs on the Python standard library alone.

## Use

```
python encoding.py                     # the working tree
python encoding.py --staged --hook     # the index, as a pre-commit hook
python encoding.py --message FILE      # a commit message, from a commit-msg hook
python encoding.py --commits RANGE     # every commit in a range, for CI
python encoding.py --fix               # repair byte-order marks and mojibake in place
```

**In hook mode the last line on stdout is a verdict**: `PRECOMMIT-VERDICT encoding PASS`
or `PRECOMMIT-VERDICT encoding REFUSE`. A caller treats a non-zero exit **without** a
verdict as a fault in the check rather than a refusal, because an interpreter that fails
to start exits non-zero too.

**`--commits` exits 2 when the range cannot be listed.** That is a fault, never a pass.

**`--fix` repairs only byte-order marks and mojibake.** Control bytes and non-UTF-8 files
are reported and left for a person, because the original text cannot be known.

## Quotations

A sequence that is the whole content of an inline code span - or, in source files, of a
string literal - is a quotation and is not reported. Anywhere else it is a defect. There
is no exclusion list.

## Session records

Files matching `sessions/<date>-<id>.md` are treated as immutable records: checked like
any other file when staged, listed as historical rather than failed in a working-tree
scan, and never repaired by `--fix`.

## Pinning

Pin this repository by commit wherever it is used - as a submodule, or by checking it out
at a fixed commit in CI. Update the pin deliberately, and update every pin together.

## Tests

```
python -m pytest tests
```
