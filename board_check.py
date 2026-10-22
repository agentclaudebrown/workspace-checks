"""
Check a repository's board and roadmap against the rules a script can decide.

    python board_check.py --repo <owner/name> --root <checkout>

WHAT IT CHECKS

- **Every open issue carries exactly one state label** from the issue-state table,
  and **at least one `block:` label**.
- **Every issue closed in the last 35 days has a closing comment**, and any reason
  label it carries is from the closure table. At most one reason label is allowed,
  since one closure has one reason.
- **`ROADMAP.md` carries no issue numbers and no state label names**: state lives
  on the board only, and a copy in the roadmap is a second home that goes stale.
- **The keys sweep**, when its script is present beside this one (see `KEYS_SWEEP`).

Checks that need judgement - whether a waiting condition names a checkable moment,
whether a document is past its declared rhythm, whether closures share a rationale -
are not made here.

REPORTING

Each finding names the issue or file. Any finding fails the run. The output ends
with a summary line and then a verdict line, `PRECOMMIT-VERDICT board PASS` or
`PRECOMMIT-VERDICT board REFUSE`, so a run that stopped before its end is visible
by the absence of both.

FAULTS FAIL CLOSED

The check runs on the code host, where a fault must not read as a clean board. If
the issues cannot be listed, it prints the fault, no verdict, and exits 2.

It uses the Python standard library and the `gh` command-line tool.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

NAME = "board"
VERDICT = "PRECOMMIT-VERDICT"

# The issue-state table. An open issue carries exactly one of these.
STATE_LABELS = (
    "waiting-on-data",
    "waiting-on-owner",
    "awaiting-deploy",
    "blocked",
    "needs-decision",
    "milestone-gated",
    "sequenced",
    "untested",
)

# The closure table. No label means done; these are the other reasons.
REASON_LABELS = (
    "closed-by-measurement",
    "superseded",
    "declined",
    "blocked",
)

# The code host's stock resolution labels. On a closed issue each states a reason
# for closing that is not in the closure table.
OFF_TABLE_REASONS = (
    "duplicate",
    "invalid",
    "wontfix",
)

BLOCK_PREFIX = "block:"

# How far back closed issues are checked. Longer than a monthly audit's interval,
# so no closure falls between two weekly runs or two monthly audits.
CLOSED_WINDOW_DAYS = 35

# A comment counts as the closing comment if it was posted no earlier than this
# before the issue closed. Closing with a comment posts the comment first, seconds
# before the close; the margin allows the comment to be written and then the
# issue closed by hand.
CLOSING_COMMENT_MARGIN = dt.timedelta(hours=1)

# ----------------------------------------------------------------------------
# HOOK POINT - KEYS SWEEP.
# The keys-versus-machines sweep is built separately. When a script of this name
# exists beside this file, it is run as
#     python <KEYS_SWEEP> --root <checkout>
# and judged by its verdict line, `PRECOMMIT-VERDICT keys PASS|REFUSE`. A
# non-zero exit with no verdict is a fault in the sweep, reported as a finding,
# because on the code host a fault must not pass. While the script is absent,
# the run says so in its summary and does not fail for it.
KEYS_SWEEP = "keys_sweep.py"
# ----------------------------------------------------------------------------

ISSUE_NUMBER = re.compile(r"(?<![\w&/])#\d+(?![\w-])|\bissues/\d+\b")

# A state label whose name is a hyphenated term is found anywhere in the roadmap.
# One whose name is an ordinary word (`blocked`, `sequenced`, `untested`) is found
# only where it is used as a label: as the whole content of an inline code span or
# of a table cell. Prose such as "what is blocked on the owner" is not a label.
_HYPHENATED = [s for s in STATE_LABELS if "-" in s]
_WORDS = [s for s in STATE_LABELS if "-" not in s]
STATE_ANYWHERE = re.compile(r"(?<![\w-])(" + "|".join(map(re.escape, _HYPHENATED))
                            + r")(?![\w-])")
STATE_AS_LABEL = re.compile(r"`(" + "|".join(map(re.escape, _WORDS)) + r")`"
                            r"|(?:^|\|)\s*(" + "|".join(map(re.escape, _WORDS))
                            + r")\s*(?=\|)")


class Fault(Exception):
    """The check could not reach a verdict."""


def _labels(issue: dict) -> list[str]:
    return [label["name"] if isinstance(label, dict) else str(label)
            for label in issue.get("labels") or []]


def _when(stamp: str) -> dt.datetime:
    return dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def _ref(issue: dict) -> str:
    return f"#{issue['number']} {issue.get('title', '')}".rstrip()


# --- the checks: pure functions over the code host's JSON -------------------

def check_open(issues: list[dict]) -> list[str]:
    findings = []
    for issue in issues:
        labels = _labels(issue)
        states = [name for name in labels if name in STATE_LABELS]
        if not states:
            findings.append(f"ISSUE  {_ref(issue)}: no state label")
        elif len(states) > 1:
            findings.append(f"ISSUE  {_ref(issue)}: {len(states)} state labels "
                            f"({', '.join(states)}); exactly one is allowed")
        if not any(name.startswith(BLOCK_PREFIX) for name in labels):
            findings.append(f"ISSUE  {_ref(issue)}: no {BLOCK_PREFIX} label")
    return findings


def check_closed(issues: list[dict], now: dt.datetime) -> list[str]:
    findings = []
    since = now - dt.timedelta(days=CLOSED_WINDOW_DAYS)
    for issue in issues:
        closed = issue.get("closedAt")
        if not closed or _when(closed) < since:
            continue
        closed_at = _when(closed)
        comments = issue.get("comments") or []
        if not any(_when(c["createdAt"]) >= closed_at - CLOSING_COMMENT_MARGIN
                   for c in comments if c.get("createdAt")):
            findings.append(f"ISSUE  {_ref(issue)}: closed with no closing comment")
        labels = _labels(issue)
        reasons = [name for name in labels if name in REASON_LABELS]
        off_table = [name for name in labels if name in OFF_TABLE_REASONS]
        for name in off_table:
            findings.append(f"ISSUE  {_ref(issue)}: closure reason '{name}' is "
                            f"not in the closure table")
        if len(reasons) > 1:
            findings.append(f"ISSUE  {_ref(issue)}: {len(reasons)} closure "
                            f"reasons ({', '.join(reasons)}); one closure has one")
    return findings


def check_roadmap(root: Path) -> list[str]:
    path = root / "ROADMAP.md"
    if not path.is_file():
        return ["FILE   ROADMAP.md: missing, in a repository that carries a board"]
    findings = []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [f"FILE   ROADMAP.md: cannot be read ({exc})"]
    for lineno, line in enumerate(text.splitlines(), start=1):
        for match in ISSUE_NUMBER.finditer(line):
            findings.append(f"FILE   ROADMAP.md:{lineno}: issue number "
                            f"'{match.group(0)}'")
        for match in STATE_ANYWHERE.finditer(line):
            findings.append(f"FILE   ROADMAP.md:{lineno}: state label "
                            f"'{match.group(1)}'")
        for match in STATE_AS_LABEL.finditer(line):
            findings.append(f"FILE   ROADMAP.md:{lineno}: state label "
                            f"'{match.group(1) or match.group(2)}'")
    return findings


def run_keys_sweep(root: Path, here: Path | None = None) -> tuple[list[str], str]:
    """The hook point for the keys sweep. Returns (findings, summary note)."""
    script = (here or Path(__file__).resolve().parent) / KEYS_SWEEP
    if not script.is_file():
        return [], "keys sweep not present"
    proc = subprocess.run([sys.executable, str(script), "--root", str(root)],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    out = proc.stdout.strip().splitlines()
    last = out[-1].strip() if out else ""
    if last == f"{VERDICT} keys PASS":
        return [], "keys sweep passed"
    detail = [f"KEYS   {line}" for line in out[:-1] + proc.stderr.splitlines()
              if line.strip()]
    if last == f"{VERDICT} keys REFUSE":
        return detail or ["KEYS   the keys sweep refused"], "keys sweep refused"
    return (detail + [f"KEYS   the keys sweep ended without a verdict "
                      f"(exit {proc.returncode}); a fault, not a pass"],
            "keys sweep faulted")


# --- the code host ------------------------------------------------------------

def gh_json(args: list[str]) -> list[dict]:
    try:
        proc = subprocess.run(["gh", *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    except OSError as exc:
        raise Fault(f"gh could not be run: {exc}") from exc
    if proc.returncode != 0:
        raise Fault(f"gh {' '.join(args[:3])} ... exited {proc.returncode}: "
                    f"{proc.stderr.strip()}")
    try:
        data = json.loads(proc.stdout or "null")
    except json.JSONDecodeError as exc:
        raise Fault(f"gh returned output that is not JSON: {exc}") from exc
    if not isinstance(data, list):
        raise Fault("gh returned something other than a list of issues")
    return data


LIMIT = "5000"


def fetch_open(repo: str) -> list[dict]:
    return gh_json(["issue", "list", "-R", repo, "--state", "open",
                    "--limit", LIMIT, "--json", "number,title,labels"])


def fetch_closed(repo: str, now: dt.datetime) -> list[dict]:
    since = (now - dt.timedelta(days=CLOSED_WINDOW_DAYS + 1)).date().isoformat()
    return gh_json(["issue", "list", "-R", repo, "--state", "closed",
                    "--search", f"closed:>={since}", "--limit", LIMIT,
                    "--json", "number,title,labels,closedAt,comments"])


# --- the run --------------------------------------------------------------------

def main(argv: list[str] | None = None, now: dt.datetime | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError, TypeError):
            pass

    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--repo", required=True, help="owner/name on the code host")
    ap.add_argument("--root", required=True, type=Path,
                    help="a checkout of the repository")
    args = ap.parse_args(argv)
    now = now or dt.datetime.now(dt.timezone.utc)

    try:
        open_issues = fetch_open(args.repo)
        closed_issues = fetch_closed(args.repo, now)
    except Fault as exc:
        print(f"FAULT  {exc}", file=sys.stderr)
        print(f"{NAME}: no verdict - the board could not be read", flush=True)
        return 2

    findings = check_open(open_issues)
    findings += check_closed(closed_issues, now)
    findings += check_roadmap(args.root)
    keys_findings, keys_note = run_keys_sweep(args.root)
    findings += keys_findings

    recent = sum(1 for i in closed_issues if i.get("closedAt") and
                 _when(i["closedAt"]) >= now - dt.timedelta(days=CLOSED_WINDOW_DAYS))
    for line in findings:
        print(line)
    print(f"{NAME}: {args.repo} - {len(open_issues)} open issue(s), {recent} "
          f"closed in the last {CLOSED_WINDOW_DAYS} days, ROADMAP.md checked, "
          f"{keys_note}; {len(findings)} finding(s)")
    print(f"{VERDICT} {NAME} {'REFUSE' if findings else 'PASS'}", flush=True)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
