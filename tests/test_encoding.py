"""
Tests for `encoding.py`: byte-order marks, mojibake, raw control bytes, and files
that are not UTF-8.

Each check is driven to a **refusal**, not only to a pass: a guard whose firing
path is never exercised could be inert without anyone knowing.
"""
from __future__ import annotations

import contextlib
import io
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import encoding  # noqa: E402


def mangle(text: str, times: int = 1) -> str:
    """
    Do to a string what a misconfigured editor does: write UTF-8, read cp1252.

    **The damaged sequences are derived here, never written as literals.** A
    literal is easy to type wrongly, and a test file carrying the damage as text
    is itself something an editor could silently rewrite.
    """
    for _ in range(times):
        text = text.encode("utf-8").decode("cp1252")
    return text


# Characters common in technical prose whose damaged forms are easy to miss.
DAMAGE = ["\u2014", "\u00a7", "\u221a", "\u25bc", "\u00a3", "\u2013",
          "\u00d7", "\u2212", "\u25b2"]


@pytest.mark.parametrize("original", DAMAGE)
def test_a_mangled_sequence_is_recognised_and_recovered(original):
    assert encoding.recover(mangle(original)) == original


def test_double_encoding_is_unwound_all_the_way():
    """Reporting the half-recovered form as the fix would leave a defect behind."""
    assert encoding.recover(mangle("\u00a3", times=2)) == "\u00a3"


@pytest.mark.parametrize("text", [
    "R\u00faben",              # one accented character cannot be mojibake
    "Gu\u00e9hi",
    "\u00dc\u00d6",            # a pair that is not valid UTF-8 read as cp1252
    "plain ascii only",
    "\u00e9\u00e8\u00ea",      # three accents, still not valid UTF-8
])
def test_real_text_is_not_reported(text):
    assert encoding.recover(text) is None
    assert encoding.scan_text("x.md", text) == []


# ------------------------------------------- the five undefined cp1252 slots

UNDEFINED = (0x81, 0x8D, 0x8F, 0x90, 0x9D)


def mangle_windows(text: str) -> str:
    """
    `mangle`, but as Windows does it: an undefined cp1252 byte becomes the C1
    control of the same value, where Python's decoder raises.

    Derived byte by byte and independently of the module's own table, so the test
    is not the module agreeing with itself.
    """
    out = []
    for byte in text.encode("utf-8"):
        try:
            out.append(bytes([byte]).decode("cp1252"))
        except UnicodeDecodeError:
            out.append(chr(byte))
    return "".join(out)


PROPORTIONAL_TO = chr(0x221D)


def test_an_undefined_slot_survives_as_a_c1_control():
    """The undefined byte is kept as a C1 control, which is what makes recovery
    possible at all."""
    assert mangle_windows(PROPORTIONAL_TO) == chr(0xE2) + chr(0x2C6) + chr(0x9D)


@pytest.mark.parametrize("original", [
    PROPORTIONAL_TO, chr(0xC1), chr(0xCD), chr(0xCF), chr(0xD0), chr(0xDD)])
def test_a_sequence_through_an_undefined_slot_is_recognised_and_recovered(original):
    assert set(original.encode("utf-8")) & set(UNDEFINED), "fixture misses the slots"
    assert encoding.recover(mangle_windows(original)) == original


def test_an_undefined_slot_sequence_is_a_defect_in_a_scanned_line():
    line = "a rate `P(k) " + mangle_windows(PROPORTIONAL_TO) + " theta^k` here"
    found = encoding.scan_text("notes.md", line)
    assert [(f.recovered, f.quoted) for f in found] == [(PROPORTIONAL_TO, False)]


def test_a_lone_c1_control_beside_an_accent_is_not_reported():
    """Widening the table must not widen the false alarms: E9 9D is not UTF-8."""
    text = "caf" + chr(0xE9) + chr(0x9D)
    assert encoding.recover(text) is None
    assert encoding.scan_text("x.md", text) == []


def test_a_run_next_to_a_legitimate_accent_is_still_found():
    """A maximal run containing one real accented character does not round-trip as
    a whole; a detector that gave up there would miss the damage beside it."""
    line = "caf\u00e9 " + mangle("\u00a7") + " here"
    found = encoding.scan_text("x.md", line)
    assert [(f.mangled, f.recovered) for f in found] == [
        (mangle("\u00a7"), "\u00a7")]


# ---------------------------------------------------- quotations are a rule

def test_a_sequence_quoted_in_backticks_is_a_quotation():
    line = "the section sign is stored as `" + mangle("\u00a7") + "` by the editor"
    assert [f.quoted for f in encoding.scan_text("notes.md", line)] == [True]


def test_a_sequence_mangled_INSIDE_backticks_is_still_a_defect():
    """A span about something else that happens to contain damage is damaged,
    not quoting."""
    for line in ("see `ARCHIVE " + mangle("\u2014") + " migrated from NOTES.md`",
                 "the objective is `points " + mangle("\u00d7") + " (1 - EO)`"):
        found = encoding.scan_text("notes.md", line)
        assert found, line
        assert [f.quoted for f in found] == [False], line


def test_a_string_literal_in_source_is_a_quotation():
    line = '    assert b"\\xc2\\xa3".decode("cp1252") == "' + mangle("\u00a3") + '"'
    assert [f.quoted for f in encoding.scan_text("tests/t.py", line)] == [True]


def test_but_a_string_literal_in_MARKDOWN_is_not():
    """Two apostrophes in prose would otherwise form a span masking everything
    between them."""
    line = "the owner's " + mangle("\u00a7") + " and the session's note"
    assert [f.quoted for f in encoding.scan_text("notes.md", line)] == [False]


# ------------------------------------------------------------- byte-order marks

MARK = chr(0xFEFF)


def test_a_byte_order_mark_is_reported():
    bom, _found, _controls = encoding.scan_bytes(
        "x.md", encoding.BOM + b"# heading\n")
    assert bom is True


def test_a_bom_does_not_hide_the_text_behind_it():
    data = encoding.BOM + ("one " + mangle("\u00a7") + " two").encode("utf-8")
    bom, found, _controls = encoding.scan_bytes("x.md", data)
    assert bom is True
    assert [f.recovered for f in found] == ["\u00a7"]


def test_binary_content_is_skipped_rather_than_guessed_at():
    assert encoding.scan_bytes("x.bin", b"\x00\xc3\x82\xc2\xa3\x00") == (False, [], [])


def test_a_byte_order_mark_mid_file_is_reported():
    found = encoding.scan_text("notes.md", "line one\n" + MARK + "# heading\n")
    assert [(f.line, f.column, f.recovered, f.quoted) for f in found] == [
        (2, 1, "", False)]
    assert "stray byte-order mark" in found[0].render()


def test_a_leading_and_a_mid_file_mark_are_two_defects():
    data = encoding.BOM + ("one\n" + MARK + "two\n").encode("utf-8")
    bom, found, _controls = encoding.scan_bytes("x.md", data)
    assert bom is True
    assert [f.line for f in found] == [2]


def test_a_mark_held_as_a_string_constant_in_source_is_a_quotation():
    line = 'BOM = "' + MARK + '"'
    assert [f.quoted for f in encoding.scan_text("console.py", line)] == [True]
    assert [f.quoted for f in encoding.scan_text("notes.md", "BOM is " + MARK)] == [
        False]


def test_the_repair_removes_a_stray_mark(tmp_path):
    path = tmp_path / "doc.md"
    path.write_bytes(("keep\n" + MARK + "# heading\n").encode("utf-8"))
    assert encoding.fix_file(str(path)) == (False, 1)
    assert path.read_bytes() == b"keep\n# heading\n"


# ------------------------------------------------------------- not UTF-8 at all

def test_a_file_that_is_not_utf8_is_UNSCANNABLE_not_clean():
    with pytest.raises(encoding.Unscannable) as got:
        encoding.scan_bytes("scratch/x.log", b"\r\nsome test \x97 1,982\r\n")
    # Line 2 is `some test ` (ten characters) then the byte: column 11.
    assert (got.value.line, got.value.column, got.value.byte) == (2, 11, 0x97)
    assert "not UTF-8" in got.value.render()


def test_a_leading_mark_does_not_shift_the_reported_position():
    with pytest.raises(encoding.Unscannable) as got:
        encoding.scan_bytes("x.md", encoding.BOM + b"ok\n\x97")
    assert (got.value.line, got.value.column) == (2, 1)


def test_an_unscannable_file_is_refused_by_the_repair_and_left_alone(tmp_path):
    path = tmp_path / "legacy.txt"
    path.write_bytes(b"an em dash \x97 in cp1252\n")
    with pytest.raises(encoding.Unscannable):
        encoding.fix_file(str(path))
    assert path.read_bytes() == b"an em dash \x97 in cp1252\n"


def test_an_unscannable_commit_message_is_refused(tmp_path):
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_bytes(b"docs: a subject \x97 in cp1252\n")
    result = encoding.check_message(str(msg))
    assert result.refused
    assert [e.byte for e in result.unscannable] == [0x97]


# ------------------------------------ session records: reported, never fixed

@pytest.mark.parametrize("path,record", [
    ("sessions/2026-01-01-a.md", True),
    ("sessions\\2026-01-02-b.md", True),
    ("sessions/INDEX.md", False),            # a living file, not a record
    ("docs/sessions/2026-01-01-x.md", False),
    ("sessions/2026-01-01-x.txt", False),
])
def test_what_counts_as_an_immutable_record(path, record):
    assert encoding.is_record(path) is record


def _a_tree_with_one_record(tmp_path, monkeypatch, body: bytes):
    record = "sessions/2026-01-01-probe.md"
    (tmp_path / "sessions").mkdir()
    (tmp_path / record).write_bytes(body)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(encoding, "_tracked_paths", lambda: [record])
    return tmp_path / record


def test_a_record_is_HISTORICAL_in_the_working_tree_and_does_not_fail(
        tmp_path, monkeypatch, capsys):
    path = _a_tree_with_one_record(tmp_path, monkeypatch,
                                   ("x\n" + MARK + "y\n").encode("utf-8"))
    result = encoding.check(staged=False)
    assert not result.refused
    assert len(result.historical) == 1 and "2026-01-01-probe.md:2:1" in result.historical[0]
    assert encoding.main([]) == 0
    assert "HIST" in capsys.readouterr().out
    assert path.exists()


def test_fix_skips_a_record_and_says_so(tmp_path, monkeypatch, capsys):
    body = ("x\n" + MARK + "y " + mangle("\u00a7") + "\n").encode("utf-8")
    path = _a_tree_with_one_record(tmp_path, monkeypatch, body)
    assert encoding.main(["--fix"]) == 0
    assert path.read_bytes() == body, "repairing a record edits a record"
    assert "SKIPPED - immutable session record" in capsys.readouterr().out


# ---------------------------------------------------------------------- repair

def test_the_repair_touches_only_the_damage(tmp_path):
    """Per-sequence, never a whole-file re-encode, and quotations left alone."""
    path = tmp_path / "doc.md"
    original = ("keep this line exactly\n"
                "a " + mangle("\u00a7") + " and a " + mangle("\u2014") + " here\n"
                "quoting `" + mangle("\u00a3") + "` on purpose\n")
    path.write_bytes(encoding.BOM + original.encode("utf-8"))

    bom, repaired = encoding.fix_file(str(path))

    assert (bom, repaired) == (True, 2)
    assert path.read_bytes() == (
        "keep this line exactly\n"
        "a \u00a7 and a \u2014 here\n"
        "quoting `" + mangle("\u00a3") + "` on purpose\n").encode("utf-8")


def test_a_clean_file_is_not_rewritten(tmp_path):
    path = tmp_path / "clean.md"
    path.write_bytes("nothing wrong \u2014 here\n".encode("utf-8"))
    before = path.stat().st_mtime_ns
    assert encoding.fix_file(str(path)) == (False, 0)
    assert path.stat().st_mtime_ns == before


# --------------------------------------------------------- raw control bytes

def test_a_shell_escape_turned_into_a_control_byte_is_found():
    """A backtick followed by `a` is BEL in PowerShell, so a code span written
    through it loses its opening backtick and gains a control byte."""
    line = "# Scratch for \x07ws ecs run-task --overrides. Inline JSON"
    got = encoding.scan_controls("notes.txt", line)
    assert [(c.line, c.column, c.byte) for c in got] == [(1, 15, 0x07)]
    assert "0x07" in got[0].render()


def test_none_of_the_other_scanners_can_see_a_control_byte():
    """BEL is below cp1252's high range, is not U+FEFF, and decodes as UTF-8, so
    it needs its own scanner."""
    bom, found, controls = encoding.scan_bytes("x.md", b"a \x07 bell\n")
    assert (bom, found) == (False, [])
    assert [c.byte for c in controls] == [0x07]


@pytest.mark.parametrize("byte", list(range(1, 9)) + [0x0b, 0x0c]
                         + list(range(0x0e, 0x20)) + [0x7f])
def test_every_control_the_rule_names_is_actually_seen(byte):
    """One case per byte, so any narrowing names exactly which byte it lost -
    including the separators `str.splitlines()` would treat as line endings."""
    got = encoding.scan_controls("x.md", f"before{chr(byte)}after")
    assert [c.byte for c in got] == [byte], f"0x{byte:02X} was not seen"


@pytest.mark.parametrize("line", ["tab\there", "two\nlines", "crlf\r\nlines"])
def test_the_three_legitimate_whitespace_controls_are_not_findings(line):
    assert encoding.scan_controls("x.md", line) == []


def test_nul_is_left_to_the_binary_check():
    assert encoding.scan_controls("x.md", "a\x00b") == []
    assert encoding.is_text("x.md", b"a\x00b") is False


def test_backticks_are_not_a_quotation_for_a_control_byte():
    assert len(encoding.scan_controls("x.md", "see `\x07` here")) == 1


def test_a_control_byte_refuses_a_commit_and_is_never_repaired(tmp_path):
    path = tmp_path / "notes.md"
    path.write_bytes(b"a line with a \x07 bell\n")
    _bom, found, controls = encoding.scan_bytes(str(path), path.read_bytes())
    assert encoding.Checked([], found, [], [], controls).refused
    assert encoding.fix_file(str(path)) == (False, 0)
    assert path.read_bytes() == b"a line with a \x07 bell\n"


def test_a_control_byte_in_a_commit_message_is_refused(tmp_path):
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_bytes(b"docs: a subject with \x07 in it\n")
    result = encoding.check_message(str(msg))
    assert result.refused
    assert [c.byte for c in result.controls] == [0x07]


def test_a_commit_message_comment_line_is_dropped_before_the_control_scan(
        tmp_path):
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_bytes(b"docs: clean subject\n# a comment with \x07 in it\n")
    assert encoding.check_message(str(msg)).refused is False


def test_this_repository_is_clean():
    """The checker's own repository passes its own check."""
    root = Path(__file__).resolve().parent.parent
    with contextlib.chdir(root):
        result = encoding.check(staged=False)
    assert result.boms == [], f"byte-order marks: {result.boms}"
    assert result.findings == [], "\n".join(
        f.render() for f in result.findings[:10])
    assert result.unscannable == [], "\n".join(
        e.render() for e in result.unscannable)
    assert result.controls == [], "\n".join(
        c.render() for c in result.controls)


# --------------------------------------------------------- the commit message

def test_a_byte_order_mark_in_a_commit_message_is_refused(tmp_path):
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_bytes(b"\xef\xbb\xbfdocs: a subject with a mark\n")
    result = encoding.check_message(str(msg))
    assert result.boms == ["<commit message>"]
    assert result.findings == []
    assert result.refused


def test_a_mangled_sequence_in_a_commit_message_is_refused(tmp_path):
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text(f"docs: the {mangle(chr(0xa7))}7 heading\n", encoding="utf-8")
    findings = encoding.check_message(str(msg)).findings
    assert findings and findings[0].recovered == "\u00a7"


def test_a_clean_commit_message_passes(tmp_path):
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text("feat: a perfectly ordinary subject \u2014 with an em dash\n",
                   encoding="utf-8")
    assert encoding.check_message(str(msg)) == encoding.Checked([], [], [], [], [])


def test_a_comment_line_in_a_commit_message_is_not_a_defect(tmp_path):
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text(f"docs: a clean subject\n#\n# beware of {mangle(chr(0xa7))}\n",
                   encoding="utf-8")
    assert encoding.check_message(str(msg)) == encoding.Checked([], [], [], [], [])


# ------------------------------------------------------- the verdict contract

def test_a_passing_check_prints_a_verdict():
    """The exit code cannot distinguish a refusal from a crash, so the printed
    verdict is the contract."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        encoding.verdict("encoding", False)
    assert out.getvalue().strip() == "PRECOMMIT-VERDICT encoding PASS"


def test_a_refusing_check_prints_a_verdict_too():
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        encoding.verdict("encoding", True)
    assert out.getvalue().strip() == "PRECOMMIT-VERDICT encoding REFUSE"


# --------------------------------------------------------- commit ranges, for CI

def _history(tmp_path):
    """A repository where a mark is added in one commit and stripped in the next,
    so the NET diff is clean and only per-commit checking sees it."""
    def git(*args):
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
            cwd=tmp_path, check=True, capture_output=True, text=True).stdout

    git("init", "-q")
    (tmp_path / "NOTES.md").write_bytes(b"# notes\n")
    git("add", "NOTES.md")
    git("commit", "-q", "-m", "base")
    base = git("rev-parse", "HEAD").strip()
    (tmp_path / "NOTES.md").write_bytes(encoding.BOM + b"# notes\n")
    git("commit", "-q", "-am", "docs: adds a mark")
    (tmp_path / "NOTES.md").write_bytes(b"# notes\n")
    git("commit", "-q", "-am", "fix: strips it")
    return git, base


def test_a_commit_carrying_a_mark_is_refused_even_when_the_range_nets_clean(
        tmp_path, monkeypatch, capsys):
    git, base = _history(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert encoding.check_commits(f"{base}..HEAD") == 1
    out = capsys.readouterr().out
    assert "adds a mark" in out and "BOM   NOTES.md" in out
    assert "strips it" not in out.split("commit(s) checked")[0]
    assert "2 commit(s) checked" in out


def test_a_clean_range_passes_and_says_how_many_it_looked_at(tmp_path,
                                                              monkeypatch,
                                                              capsys):
    _history(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert encoding.check_commits("HEAD^!") == 0
    assert "1 commit(s) checked" in capsys.readouterr().out


def test_a_session_record_committed_damaged_is_refused_not_historical(
        tmp_path, monkeypatch):
    """A tree scan would list this as historical forever; per-commit checking
    refuses it, as the hook would have."""
    git, _base = _history(tmp_path)
    (tmp_path / "sessions").mkdir()
    (tmp_path / "sessions" / "2026-01-01-x.md").write_bytes(
        encoding.BOM + b"# record\n")
    git("add", "sessions/2026-01-01-x.md")
    git("commit", "-q", "-m", "a damaged record")
    monkeypatch.chdir(tmp_path)
    assert encoding.check_commits("HEAD^!") == 1


def test_a_range_that_cannot_be_listed_is_a_fault_not_a_pass(tmp_path,
                                                             monkeypatch,
                                                             capsys):
    _history(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert encoding.check_commits("0123456789abcdef0123456789abcdef01234567"
                                  "..HEAD") == 2
    assert "not a pass" in capsys.readouterr().err
