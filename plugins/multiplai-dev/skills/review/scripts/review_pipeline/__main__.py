"""CLI for the review pipeline.

  review  — one target (--branch, --pr or --range)
  batch   — a YAML list of targets, then rollups
  rollup  — regenerate HIGH/MEDIUM/LOW-only.md from existing findings.json files
  resume  — continue a review from its review-state.json
  post    — one PR comment with the HIGH and MEDIUM findings

stdout contract: progress summary lines, a `summary: <path>` line per finished
target, then for review/batch/resume a final `findings: <path>[ <path>...]`
line listing every findings.json written.

Exit codes: 0 done; 1 a target failed; 2 bad input, unresolvable target, or
post refused; 3 repository not trusted; 4 the budget circuit breaker stopped
the run (resume with a higher --max-cost-usd to continue).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from .budget import DEFAULT_MAX_USD

log = logging.getLogger("review_pipeline")

SUBCOMMANDS = ("review", "batch", "rollup", "resume", "post")


def default_out() -> Path:
    """`<workspace>/INBOX/reviews` when the workspace has an INBOX, else
    `~/.multiplai/reviews`.

    Never `./reviews`: the directory the user runs from is usually the
    repository under review, and the output would land in its working tree.
    review-viewer's `default_reviews_dir()` makes the same choice; they must
    agree, because the viewer finds a review by looking here."""
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        marker = Path(config_dir) / ".workspace"
        try:
            workspace = Path(marker.read_text(encoding="utf-8").strip()).expanduser()
        except OSError:
            workspace = None
        if workspace and (workspace / "INBOX").is_dir():
            return workspace / "INBOX" / "reviews"
    return Path.home() / ".multiplai" / "reviews"


def _trust_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument("--trust-repo", action="store_true",
                   help="Confirm you trust the repository: its contents are sent to a model, which "
                        "reads the tree (equivalent to REVIEW_TRUST_REPO=1).")


def _budget_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument("--max-cost-usd", type=float, default=DEFAULT_MAX_USD,
                   help=f"Circuit breaker per target, in USD (default {DEFAULT_MAX_USD:g}; 0 = no ceiling).")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review_pipeline",
        description="Code review with stages and gates enforced in code; writes findings.json (review-viewer v1).",
    )
    parser.add_argument("--session-id", default="", help="Claude Code session id, for log correlation")
    parser.add_argument("--out", default=None,
                        help="Output directory (default: <workspace>/INBOX/reviews if it exists, "
                             "else ~/.multiplai/reviews)")
    # The same two flags are accepted after the subcommand too
    # (`review --out DIR`). SUPPRESS keeps an absent one from overwriting the
    # value given before the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--session-id", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("--out", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True, metavar="{" + ",".join(SUBCOMMANDS) + "}")

    rv = sub.add_parser("review", parents=[common], help="Review one branch, PR or commit range")
    rv.add_argument("--repo", required=True, help="Path to the repository")
    which = rv.add_mutually_exclusive_group(required=True)
    which.add_argument("--branch", help="Review origin/<branch> against its merge-base with the default branch")
    which.add_argument("--pr", type=int, help="Review a GitHub PR (needs gh)")
    which.add_argument("--range", dest="range_", metavar="BASE..HEAD", help="Review a commit range")
    rv.add_argument("--base-branch", help="Default branch for --branch (default: origin/HEAD)")
    rv.add_argument("--fetch", action="store_true", help="git fetch origin first (never done otherwise)")
    rv.add_argument("--ticket", action="append", default=[], help="Ticket id for the header (repeatable)")
    rv.add_argument("--deployed-in", help="Branch to report whether head is deployed in (origin/<name>)")
    _trust_flag(rv)
    _budget_flag(rv)

    bt = sub.add_parser("batch", parents=[common], help="Review a YAML list of targets, then write rollups")
    bt.add_argument("file", help="YAML list of {repo, branch|pr|range, tickets, deployed_in}")
    bt.add_argument("--parallel", type=int, default=2, help="Targets reviewed at once (default 2)")
    _trust_flag(bt)
    _budget_flag(bt)

    ru = sub.add_parser("rollup", parents=[common], help="Regenerate HIGH/MEDIUM/LOW-only.md from findings.json files")
    ru.add_argument("paths", nargs="*", help="findings.json files in order (default: <out>/*/findings.json)")

    rs = sub.add_parser("resume", parents=[common], help="Continue a review from <out>/<slug>/review-state.json")
    rs.add_argument("target_dir", help="The review's directory, <out>/<slug>")
    _trust_flag(rs)
    _budget_flag(rs)

    po = sub.add_parser("post", parents=[common], help="Post the HIGH and MEDIUM findings as one PR comment")
    po.add_argument("target_dir", help="The review's directory, <out>/<slug>")
    po.add_argument("--decisions", help="The viewer's decisions.json; only accepted findings are posted")
    return parser


TRUST_MESSAGE = (
    "Not run: the review skill sends this repository's contents to a model and lets it read the "
    "tree. If you trust the repository, re-run with --trust-repo (or set REVIEW_TRUST_REPO=1)."
)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    from multiplai_core.env import load_env
    from multiplai_core.log_utils import setup_logging

    load_env()
    setup_logging("review-pipeline", session_id=args.session_id,
                  propagate_loggers=("review_pipeline", "multiplai_core"))

    if getattr(args, "trust_repo", False):
        os.environ["REVIEW_TRUST_REPO"] = "1"

    from . import orchestrator, render, sdk
    from .config import load_config
    from .post import PostError, post

    out = Path(args.out).expanduser().resolve() if args.out else default_out()

    if args.command == "rollup":
        paths = [Path(p).resolve() for p in args.paths] or None
        written = render.write_rollups(out, paths)
        print("rollups: " + " ".join(str(p) for p in written))
        return 0

    if args.command == "post":
        try:
            print(post(Path(args.target_dir), Path(args.decisions) if args.decisions else None,
                       session_id=args.session_id))
        except PostError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        return 0

    if not sdk.repo_is_trusted():
        print(TRUST_MESSAGE, file=sys.stderr)
        return 3

    max_cost = args.max_cost_usd or None
    try:
        if args.command == "review":
            out.mkdir(parents=True, exist_ok=True)
            print(f"out: {out}", flush=True)
            spec = orchestrator.TargetSpec(repo=args.repo, branch=args.branch, pr=args.pr, range=args.range_,
                                           tickets=args.ticket, deployed_in=args.deployed_in,
                                           base_branch=args.base_branch, fetch=args.fetch)
            path = asyncio.run(orchestrator.review(spec, out, load_config(out, max_cost_usd=max_cost),
                                                   session_id=args.session_id))
            render.write_rollups(out)
            print(f"findings: {path}")
            return 0

        if args.command == "resume":
            target_dir = Path(args.target_dir).expanduser().resolve()
            path = asyncio.run(orchestrator.resume(target_dir, load_config(target_dir.parent, max_cost_usd=max_cost),
                                                   session_id=args.session_id))
            render.write_rollups(target_dir.parent)
            print(f"findings: {path}")
            return 0

        if args.command == "batch":
            specs = orchestrator.load_batch(Path(args.file))
            out.mkdir(parents=True, exist_ok=True)
            print(f"out: {out}", flush=True)
            written, failures = asyncio.run(orchestrator.batch(
                specs, out, load_config(out, max_cost_usd=max_cost), parallel=args.parallel,
                session_id=args.session_id))
            print("findings: " + " ".join(str(p) for p in written))
            return 1 if failures else 0
    except orchestrator.ReviewError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except sdk.RepoTrustError:
        print(TRUST_MESSAGE, file=sys.stderr)
        return 3
    except orchestrator.budget.BudgetExceededError as e:
        print(f"STOPPED: {e}\n{e.diagnosis}\nResume with: python -m review_pipeline resume <out>/<slug> "
              f"--max-cost-usd <higher>", file=sys.stderr)
        return 4
    except sdk.AgentCallError as e:
        log.error("review failed: %s", e, exc_info=True)
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
