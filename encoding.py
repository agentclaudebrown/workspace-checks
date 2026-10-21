"""
Refuse a commit that introduces a byte-order mark, a mojibake sequence, a raw control byte, or a file that is not UTF-8.

    python encoding.py [--staged] [--hook] [--fix] [--message FILE] [--commits RANGE]

WHAT IT GUARDS AGAINST

An editor or shell that reads UTF-8 as cp1252 and writes it back as UTF-8 turns
every non-ASCII character into a run of two or more characters. It may also add a
byte-order mark. The damage is invisible in most diffs, and files read by programs
rather than people - configuration, scripts, source - can stop working because of
it. Repairing damage once does not prevent it recurring, so the check runs on every
commit.

HOW A SEQUENCE IS RECOGNISED

By its shape, not from a list. UTF-8 bytes read as cp1252 and written back as UTF-8
leave a run of characters that all lie in cp1252's 0x80-0xFF range and that, encoded
back to cp1252, form valid UTF-8 for a shorter string. The test is that round trip:
take a maximal run of cp1252-representable characters, encode it to cp1252, and see
whether the bytes are valid UTF-8 that contracts. This recognises sequences nobody
has catalogued, and recovers the original text, which is what `--fix` writes back.

The round trip keeps false alarms low on real prose. A single accented character
cannot be mojibake, because UTF-8 needs at least two bytes for one, and most
adjacent pairs of accented letters do not encode to valid UTF-8. Validity does the
filtering, not a heuristic.

QUOTATIONS ARE NOT DEFECTS

Text that explains this defect has to show a mangled sequence. The rule that
separates the two is structural rather than a list of exempt files: **a sequence
that is the whole content of an inline code span, or of a string literal in source,
is a quotation. Anywhere else it is a defect.** String literals count only in source
files, because prose contains apostrophes that would otherwise pair up and mask
everything between them.

FOUR SHAPES, EACH CHECKED

- **A byte-order mark**, at the start of a file or anywhere after it.
- **A mojibake run**, as above.
- **A raw control byte** - a C0 control other than tab, newline and carriage return,
  or DEL - in a text file. Shells that use a control character as an escape can
  produce these from ordinary text. Reported and refused, never repaired: the
  character it replaced cannot be known. NUL is excluded because a file containing
  one is treated as binary.
- **A text file that is not UTF-8.** Reported as UNSCANNABLE and refused, never
  repaired: the code page it was written in is a guess.

cp1252 leaves five byte values undefined (0x81, 0x8D, 0x8F, 0x90, 0x9D). Windows
maps each to the C1 control character of the same value, so those code points are
part of the table and map back to their own byte. Without them, sequences whose
UTF-8 bytes include one of those values would be invisible.

SESSION RECORDS ARE REPORTED, NEVER FAILED AND NEVER FIXED

A dated session record (`sessions/<date>-<id>.md`) is finished when written and
never edited. So:

- **The staged check has no exemption.** The commit that creates a record is the
  only moment it can still be clean, and staging an edit to an existing record is
  itself a violation.
- **The working-tree check lists record defects as HISTORICAL** and does not fail on
  them. Failing would be permanently red over something nobody may repair; hiding
  them would report clean without having said what it saw.
- **`--fix` skips records and says so.**

`sessions/INDEX.md` is a living file and is not a record.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

BOM = b"\xef\xbb\xbf"
# Escaped rather than literal: a literal mark is invisible in every editor.
BOM_CHAR = "\ufeff"


# Every byte above ASCII, keyed by the character an editor reading cp1252 turns
# it into. The five slots cp1252 leaves undefined come through as the C1 control
# of the same value, which is what Windows does.
def _cp1252_table() -> dict[str, int]:
    out: dict[str, int] = {}
    for byte in range(0x80, 0x100):
        try:
            out[bytes([byte]).decode("cp1252")] = byte
        except UnicodeDecodeError:
            out[chr(byte)] = byte
    return out


CP1252_BYTE = _cp1252_table()
CP1252 = frozenset(CP1252_BYTE)

# A dated session record: finished when written, never edited. See the module
# docstring for why these are reported but neither failed nor fixed.
_RECORD = re.compile(r"sessions/\d{4}-\d{2}-\d{2}-[^/]+\.md")


def is_record(path: str) -> bool:
    return _RECORD.fullmatch(path.replace("\\", "/")) is not None

# Files whose string literals are code rather than prose.
SOURCE_SUFFIXES = frozenset({
    ".py", ".js", ".mjs", ".ts", ".html", ".css", ".json", ".ps1", ".psm1",
    ".bat", ".cmd", ".sh", ".sql", ".yml", ".yaml", ".toml", ".ini",
})

# Anything that is not text at all. Checked by extension first and by a NUL byte
# second, so a binary type missing from this list is still skipped.
BINARY_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".pdf", ".zip", ".gz",
    ".db", ".sqlite", ".pyc", ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".xlsx", ".docx", ".pptx",
})

# C0 controls, less the three that are legitimate whitespace, plus DEL. NUL is
# absent on purpose: `is_text` treats a file carrying one as binary.
CONTROL_BYTES = re.compile(r"[\x01-\x08\x0b\x0c\x0e-\x1f\x7f]")

_INLINE_CODE = re.compile(r"`[^`\n]*`")
_STRING_LITERAL = re.compile(r'"[^"\n]*"' r"|'[^'\n]*'")


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    column: int
    mangled: str
    recovered: str
    quoted: bool

    def render(self) -> str:
        where = f"{self.path}:{self.line}:{self.column}"
        if self.mangled == BOM_CHAR:
            return f"{where}  stray byte-order mark (U+FEFF) should be removed"
        return f"{where}  {self.mangled!r} should be {self.recovered!r}"


class Control(NamedTuple):
    """
    A raw C0 control byte in a text file.

    Its own type rather than a `Finding`, because a `Finding` carries a
    `recovered` string and `--fix` writes it back. There is no honest recovery
    for a control byte, so it is reported and refused and never repaired.
    """

    path: str
    line: int
    column: int
    byte: int

    def render(self) -> str:
        return (f"{self.path}:{self.line}:{self.column}  control byte "
                f"0x{self.byte:02X} in a text file - cannot be repaired "
                f"automatically")


class Unscannable(ValueError):
    """
    A text file that is not UTF-8. Raised rather than returned, so a caller that
    forgets to handle it crashes instead of passing.
    """

    def __init__(self, path: str, line: int, column: int, byte: int):
        super().__init__(path, line, column, byte)
        self.path, self.line, self.column, self.byte = path, line, column, byte

    def render(self) -> str:
        return (f"{self.path}:{self.line}:{self.column}  not UTF-8 "
                f"(byte 0x{self.byte:02X}) - cannot be scanned")


class Checked(NamedTuple):
    boms: list[str]
    findings: list[Finding]
    unscannable: list[Unscannable]
    # Working tree only: rendered defects in immutable session records.
    historical: list[str]
    # Raw C0 control bytes. Last and required rather than defaulted: a
    # NamedTuple default is one shared mutable list across every instance.
    controls: list[Control]

    @property
    def refused(self) -> bool:
        return bool(self.boms or self.findings or self.unscannable
                    or self.controls)


def _decode(path: str, data: bytes) -> tuple[bool, str]:
    """(had a leading byte-order mark, text), or raise Unscannable."""
    bom = data.startswith(BOM)
    body = data[len(BOM):] if bom else data
    try:
        return bom, body.decode("utf-8")
    except UnicodeDecodeError as exc:
        at = exc.start
        line = body.count(b"\n", 0, at) + 1
        column = at - (body.rfind(b"\n", 0, at) + 1) + 1
        raise Unscannable(path, line, column, body[at]) from None


def recover(text: str) -> str | None:
    """
    The original of a mojibake run, or None if this is not one.

    Applied repeatedly, because the damage compounds: text mangled twice needs two
    round trips to recover. Bounded rather than looped to a fixed point, so a
    pathological input cannot spin.
    """
    if len(text) < 2 or any(ch not in CP1252 for ch in text):
        return None
    try:
        out = bytes(CP1252_BYTE[ch] for ch in text).decode("utf-8")
    except UnicodeDecodeError:
        return None
    if len(out) >= len(text) or out.isascii():
        # No contraction means no multibyte sequence was reassembled, and an
        # all-ASCII result means the run was not carrying a real character.
        return None
    for _ in range(4):
        nxt = recover(out)
        if nxt is None:
            return out
        out = nxt
    return out


def _runs(text: str) -> list[tuple[int, str, str]]:
    """
    Every mojibake run in a string, longest match first at each position.

    Longest-first matters for compounded damage: a twice-mangled character is one
    run, and stopping at the first segment that round-trips would report half of
    it and leave the rest as a second finding for the same defect.
    """
    out: list[tuple[int, str, str]] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == BOM_CHAR:
            # A mark past byte 0. Its own run, recovered to nothing, so the
            # quotation rule and `--fix` treat it like any other sequence.
            out.append((i, BOM_CHAR, ""))
            i += 1
            continue
        if text[i] not in CP1252:
            i += 1
            continue
        best_end = None
        best_recovered = None
        j = i + 1
        while j <= n and text[j - 1] in CP1252:
            got = recover(text[i:j])
            if got is not None:
                best_end, best_recovered = j, got
            j += 1
        if best_end is None:
            i += 1
            continue
        out.append((i, text[i:best_end], best_recovered))
        i = best_end
    return out


def _quotation_spans(line: str, source: bool) -> list[tuple[int, int]]:
    """
    The code spans and string literals that are QUOTING a sequence, not
    carrying one.

    Being inside a delimiter is not enough: a reference that happens to contain a
    damaged character is damaged wherever it appears. What separates a quotation
    is that the sequence is the point of the span. So the test is: **remove the
    mojibake from the span, and if nothing with a letter or a digit in it is
    left, the span was quoting.** Punctuation may survive, so a quotation with a
    stray quote mark in it is still a quotation.
    """
    spans = [m.span() for m in _INLINE_CODE.finditer(line)]
    if source:
        spans += [m.span() for m in _STRING_LITERAL.finditer(line)]
    out = []
    for start, end in spans:
        inner = line[start + 1:end - 1]
        remainder = inner
        for at, mangled, _ in reversed(_runs(inner)):
            remainder = remainder[:at] + remainder[at + len(mangled):]
        if not any(ch.isalnum() for ch in remainder):
            out.append((start, end))
    return out


def scan_line(line: str, source: bool) -> list[tuple[int, str, str, bool]]:
    spans = _quotation_spans(line, source)
    return [(at, mangled, recovered,
             any(a <= at < b for a, b in spans))
            for at, mangled, recovered in _runs(line)]


def scan_text(path: str, text: str) -> list[Finding]:
    source = Path(path).suffix.lower() in SOURCE_SUFFIXES
    found: list[Finding] = []
    for number, line in enumerate(text.splitlines(), 1):
        for column, mangled, recovered, quoted in scan_line(line, source):
            found.append(Finding(path, number, column + 1, mangled,
                                 recovered, quoted))
    return found


def scan_controls(path: str, text: str) -> list[Control]:
    """Raw C0 control bytes, with line and column.

    **No quotation rule.** Prose that mentions a control byte writes it as an
    escape such as `\\x07`; a raw control byte inside backticks is still a byte no
    editor renders, which is the defect.

    **Split on the newline alone, never with `splitlines()`.** Python's
    `splitlines()` also splits on vertical tab, form feed and the file, group and
    record separators, so those bytes would be consumed as line endings and never
    reported.

    Carriage return is not in `CONTROL_BYTES`, so a CRLF file produces nothing.
    """
    out: list[Control] = []
    for number, line in enumerate(text.split("\n"), 1):
        for m in CONTROL_BYTES.finditer(line):
            out.append(Control(path, number, m.start() + 1, ord(m.group())))
    return out


def is_text(path: str, data: bytes) -> bool:
    if Path(path).suffix.lower() in BINARY_SUFFIXES:
        return False
    return b"\x00" not in data[:8192]


def scan_bytes(path: str, data: bytes) -> tuple[bool, list[Finding],
                                               list[Control]]:
    """
    (leading byte-order mark, findings, control bytes). Binary files skipped.

    **A text file that is not UTF-8 raises `Unscannable`**, because returning
    nothing would look exactly like a clean file.
    """
    if not is_text(path, data):
        return False, [], []
    bom, text = _decode(path, data)
    return bom, scan_text(path, text), scan_controls(path, text)


def _staged_paths() -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM", "-z"],
        capture_output=True, text=True, check=True).stdout
    return [p for p in out.split("\0") if p]


def _staged_blob(path: str) -> bytes | None:
    got = subprocess.run(["git", "show", f":{path}"], capture_output=True)
    return got.stdout if got.returncode == 0 else None


def _tracked_paths() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"],
                         capture_output=True, text=True, check=True).stdout
    return [p for p in out.split("\0") if p]


# ------------------------------------------------------- commits, for CI
#
# **A commit, not the working tree.** The hook's subject is what one commit adds,
# with no exemption for session records. A CI job scanning the tree would pass a
# record committed damaged, reported only as historical, and would disagree with
# the hook. Checking each commit against its first parent asks the hook's own
# question after the fact.
#
# **Every commit in the range, not the range's net diff.** A defect added in one
# commit and removed in the next is invisible in the net diff, but the history
# still carries it and cannot be repaired. The hook would have refused the first
# commit, so this does too.

def _commit_paths(sha: str) -> list[str]:
    parents = subprocess.run(["git", "rev-list", "--parents", "-n", "1", sha],
                             capture_output=True, text=True,
                             check=True).stdout.split()[1:]
    if parents:
        args = ["git", "diff", "--name-only", "--diff-filter=ACM", "-z",
                parents[0], sha]
    else:
        args = ["git", "diff-tree", "-r", "--root", "--no-commit-id",
                "--name-only", "--diff-filter=ACM", "-z", sha]
    out = subprocess.run(args, capture_output=True, text=True, check=True).stdout
    return [p for p in out.split("\0") if p]


def _commit_blob(sha: str, path: str) -> bytes | None:
    got = subprocess.run(["git", "show", f"{sha}:{path}"], capture_output=True)
    return got.stdout if got.returncode == 0 else None


def check_commits(spec: str) -> int:
    """
    Every commit `git rev-list` yields for `spec`, oldest first. 0 clean, 1 a
    commit carries a defect, 2 the range could not be read.

    **A range that cannot be listed is a FAULT and exits 2, never 0.** A job that
    passes when it could not look is indistinguishable from one that looked and
    found nothing - a force-push whose previous head no longer exists would
    otherwise read as clean. The count is printed on every run, because a check
    that passes on an empty set looks exactly like one that passes on clean
    commits.
    """
    got = subprocess.run(["git", "rev-list", "--reverse", spec],
                         capture_output=True, text=True)
    if got.returncode != 0:
        print(f"  encoding: FAULT - cannot list the commits in {spec!r}: "
              f"{got.stderr.strip()}\n  Nothing was checked. This is not a "
              f"pass.", file=sys.stderr)
        return 2
    shas = got.stdout.split()
    carrying = 0
    for sha in shas:
        result = check(False, commit=sha)
        if not result.refused:
            continue
        carrying += 1
        title = subprocess.run(["git", "log", "-1", "--format=%h %s", sha],
                               capture_output=True, text=True).stdout.strip()
        print(f"\n  COMMIT {title}")
        _report(result, hook=False)
    print(f"  {len(shas)} commit(s) checked in {spec}; "
          f"{carrying} carry a defect.")
    if carrying:
        print("  A pushed commit cannot be repaired without rewriting history. "
              "Repair the file\n  in a new commit (`encoding.py "
              "--fix`), and find out why the\n  pre-commit hook did not refuse "
              "it - that is the defect this job exists to expose.")
    return 1 if carrying else 0


def check(staged: bool, commit: str | None = None) -> Checked:
    """
    Byte-order marks, bare sequences, control bytes and undecodable files, in the
    index or in the working tree.

    The index is the right subject for a hook: a commit is made of what is staged,
    and checking the file on disk would both miss a staged defect that has since
    been edited and refuse a commit for a defect that is not in it.

    In the working tree a session record's defects go to `historical`; staged, a
    record is checked like anything else.

    `commit` asks the staged question of one commit already made: what it added,
    against its first parent, records included. That is `--commits`.
    """
    result = Checked([], [], [], [], [])
    if commit is not None:
        paths, load = _commit_paths(commit), (lambda p: _commit_blob(commit, p))
    elif staged:
        paths, load = _staged_paths(), _staged_blob
    else:
        paths, load = _tracked_paths(), _read
    for path in paths:
        data = load(path)
        if data is None:
            continue
        record = commit is None and not staged and is_record(path)
        try:
            bom, found, controls = scan_bytes(path, data)
        except Unscannable as exc:
            if record:
                result.historical.append(exc.render())
            else:
                result.unscannable.append(exc)
            continue
        bare = [f for f in found if not f.quoted]
        if record:
            if bom:
                result.historical.append(f"{path}  leading byte-order mark")
            result.historical.extend(f.render() for f in bare)
            result.historical.extend(c.render() for c in controls)
            continue
        if bom:
            result.boms.append(path)
        result.findings.extend(bare)
        result.controls.extend(controls)
    return result


def _read(path: str) -> bytes | None:
    try:
        return Path(path).read_bytes()
    except OSError:
        return None


def check_message(path: str) -> tuple[list[str], list[Finding]]:
    """
    The same defects in a COMMIT MESSAGE rather than in a file.

    The staged-file check cannot see the message, because it is not in the index,
    and a defect in a message cannot be repaired without rewriting history. This
    runs from the `commit-msg` hook.

    The mark is read from byte zero of the raw file, where git will keep it.
    Comment lines are dropped first, because git drops them before storing the
    message.

    **Scanned as prose, never as source.** Only backticks quote here, which is the
    rule an author can apply while typing a message.
    """
    data = _read(path)
    if data is None:
        return Checked([], [], [], [], [])
    boms = ["<commit message>"] if data.startswith(BOM) else []
    try:
        _bom, text = _decode("<commit message>", data)
    except Unscannable as exc:
        return Checked(boms, [], [exc], [], [])
    kept = "\n".join(line for line in text.splitlines()
                     if not line.startswith("#"))
    return Checked(boms, [f for f in scan_text("<commit message>", kept)
                          if not f.quoted], [], [],
                   scan_controls("<commit message>", kept))


def fix_file(path: str) -> tuple[bool, int]:
    """
    Repair one file in place. Returns (removed a BOM, sequences repaired).

    **Per-sequence, never a whole-file re-encode.** A blanket conversion would also
    rewrite every line that was already correct and make the diff unreadable. Only
    the runs this module identified are touched, and quoted ones are left alone.

    A text file that is not UTF-8 raises `Unscannable` and is not touched: the code
    page it was written in is a guess, and a guessed repair is a second defect.
    """
    data = _read(path)
    if data is None or not is_text(path, data):
        return False, 0
    bom, text = _decode(path, data)
    found = scan_text(path, text)
    bare = [f for f in found if not f.quoted]
    if not bom and not bare:
        return False, 0
    lines = text.splitlines(keepends=True)
    by_line: dict[int, list[Finding]] = {}
    for f in bare:
        by_line.setdefault(f.line, []).append(f)
    for number, group in by_line.items():
        line = lines[number - 1]
        # Right to left, so an earlier replacement cannot move a later column.
        for f in sorted(group, key=lambda x: -x.column):
            start = f.column - 1
            line = line[:start] + f.recovered + line[start + len(f.mangled):]
        lines[number - 1] = line
    Path(path).write_bytes("".join(lines).encode("utf-8"))
    return bom, len(bare)


# The verdict line a pre-commit check prints so the caller can tell a REFUSAL
# from a CRASH.
#
# **The exit code cannot carry this.** An interpreter that dies on import exits 1,
# the same code as a refusal, and a process that cannot import cannot choose its
# exit code. A printed line can only appear if the module loaded and reached the
# end of its own logic, so its ABSENCE is the signal: the caller treats exit 1
# with no verdict as a fault.
VERDICT = "PRECOMMIT-VERDICT"


def verdict(name: str, refused: bool) -> None:
    """On stdout, while the human-readable detail goes to stderr."""
    print(f"{VERDICT} {name} {'REFUSE' if refused else 'PASS'}", flush=True)


def _historical(result: Checked, out) -> None:
    """Reported, never failed: see the module docstring on session records."""
    if not result.historical:
        return
    print(f"\n  {len(result.historical)} defect(s) in immutable session "
          f"records - reported, not failed, not repaired:\n", file=out)
    for line in result.historical:
        print(f"  HIST  {line}", file=out)
    print("", file=out)


def _report(result: Checked, *, hook: bool,
            subject: str = "is staged", name: str = "encoding") -> int:
    boms, findings, unscannable = (result.boms, result.findings,
                                   result.unscannable)
    controls = result.controls
    out = sys.stderr if hook else sys.stdout
    if not result.refused:
        if hook:
            verdict(name, False)
        _historical(result, out)
        return 0
    if hook:
        verdict(name, True)
    if hook:
        print(f"\n  COMMIT REJECTED - a byte-order mark, a mojibake sequence, "
              f"a raw control byte\n  or a file that is not UTF-8 {subject}\n",
              file=out)
    for path in boms:
        print(f"  BOM   {path}", file=out)
    for f in findings[:40]:
        print(f"  TEXT  {f.render()}", file=out)
    if len(findings) > 40:
        print(f"  ... and {len(findings) - 40} more", file=out)
    for c in controls:
        print(f"  CTRL  {c.render()}", file=out)
    for exc in unscannable:
        print(f"  UNSCANNABLE  {exc.render()}", file=out)
    print(f"\n  {len(boms)} byte-order mark(s), {len(findings)} "
          f"sequence(s), {len(controls)} control byte(s), "
          f"{len(unscannable)} unscannable.\n", file=out)
    _historical(result, out)
    if controls and hook:
        print("  A control byte cannot be repaired by --fix: the text it "
              "replaced is a\n  guess. A common cause is a shell whose escape "
              "character turns ordinary\n  text into control bytes - PowerShell's "
              "backtick, for one. Retype the line,\n  and write the file with a "
              "text-safe editor rather than through a shell.\n", file=out)
    if unscannable and hook:
        print("  A file that is not UTF-8 cannot be repaired by --fix: the "
              "code page it was\n  written in is a guess. Re-save it as UTF-8, "
              "and give its producer an\n  explicit encoding.\n", file=out)
    if hook and name == "commit-message":
        print("  A commit message cannot be repaired after the fact without "
              "rewriting\n  history, which is why this is refused now. Retype "
              "the message.\n", file=out)
        print("  The usual cause is an editor or a shell writing the message "
              "file with a\n  byte-order mark; `git commit -m` with the text "
              "inline does not.\n", file=out)
    elif hook:
        print("  Repair, then stage the repair:\n", file=out)
        print(f'      python "{Path(__file__).resolve().as_posix()}" --fix\n',
              file=out)
        print("  A sequence quoted inside `backticks`, or inside a string "
              "literal in\n  source, is a quotation and is not reported. If "
              "one of these is\n  deliberate prose, put it in backticks - that "
              "is the rule, and there is\n  no exclusion list to add a file "
              "to.\n", file=out)
        print("  To commit anyway:  git commit --no-verify\n", file=out)
    return 1


def main(argv: list[str] | None = None) -> int:
    # Every sequence this reports is outside ASCII, and a console defaulting to a
    # legacy code page would raise partway through printing its own findings.
    # Reconfigured here rather than at module level, so importing the module for
    # its scanners does not change anyone's streams.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError, TypeError):
            pass

    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--staged", action="store_true",
                    help="check the index rather than the working tree")
    ap.add_argument("--hook", action="store_true",
                    help="report as a rejected commit, on stderr")
    ap.add_argument("--fix", action="store_true",
                    help="repair the working tree in place")
    ap.add_argument("--message", default=None, metavar="FILE",
                    help="check a commit message file rather than the index")
    ap.add_argument("--commits", default=None, metavar="RANGE",
                    help="check every commit in a git revision range, as the "
                         "hook would have (for CI)")
    args = ap.parse_args(argv)

    if args.commits:
        return check_commits(args.commits)

    if args.message:
        return _report(check_message(args.message), hook=args.hook,
                       subject="is in the commit message",
                       name="commit-message")

    if args.fix:
        boms = 0
        seqs = 0
        touched = []
        skipped = []
        unscannable = []
        for path in _tracked_paths():
            if is_record(path):
                # Repairing a record edits it. Named rather than passed over in
                # silence, but only when it has something that would be fixed.
                data = _read(path)
                try:
                    # Controls are discarded on purpose: this list is "a record
                    # that has something --fix would repair", and a control byte
                    # is never repaired.
                    bom, found, _controls = scan_bytes(path, data or b"")
                    if bom or any(not f.quoted for f in found):
                        skipped.append(path)
                except Unscannable:
                    skipped.append(path)
                continue
            try:
                bom, n = fix_file(path)
            except Unscannable as exc:
                unscannable.append(exc)
                continue
            if bom or n:
                touched.append((path, bom, n))
                boms += int(bom)
                seqs += n
        for path, bom, n in touched:
            print(f"  {path}: {'BOM removed, ' if bom else ''}{n} repaired")
        for path in skipped:
            print(f"  {path}: SKIPPED - immutable session record")
        for exc in unscannable:
            print(f"  UNSCANNABLE  {exc.render()} - not repaired")
        print(f"\n  {len(touched)} file(s), {boms} byte-order mark(s), "
              f"{seqs} sequence(s); {len(skipped)} record(s) skipped, "
              f"{len(unscannable)} unscannable.")
        return 1 if unscannable else 0

    return _report(check(args.staged), hook=args.hook)


if __name__ == "__main__":
    raise SystemExit(main())
