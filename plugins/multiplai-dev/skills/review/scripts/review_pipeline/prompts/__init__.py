"""Prompt text, one module per stage. Shared blocks live here."""

from __future__ import annotations

from ..models import TargetInfo

# Diffs larger than this are cut in the prompt; the agent is told where the
# whole diff is.
DIFF_PROMPT_CHARS = 120_000


def workspace_block(target: TargetInfo) -> str:
    return (
        f"Your working directory is a read-only snapshot of the repository `{target.label}` at commit "
        f"{target.head_sha}. Use paths relative to it. Line numbers are 1-based, exactly as the Read "
        f"tool shows them. You have Read, Grep and Glob; you cannot run commands."
    )


CITATION_RULES = """\
Citations. Every citation is {"path", "line_start", "line_end", "quote"}. `quote` is text copied
exactly from those lines of the file at this commit (one line, or a short run of lines). A program
re-reads every cited range from git and discards anything whose quote is not there. It does not ask
you again. Do not paraphrase a quote, do not quote the diff's +/- markers, and read the file before
you cite it."""

JSON_ONLY = "Answer with ONE JSON object and nothing before or after it: no heading, no prose, no table."


def diff_block(target: TargetInfo, diff: str) -> str:
    if len(diff) <= DIFF_PROMPT_CHARS:
        body = diff
    else:
        body = diff[:DIFF_PROMPT_CHARS] + (
            f"\n[... diff cut at {DIFF_PROMPT_CHARS} characters; the whole diff is at {target.diff_path} ...]"
        )
    return f"The change under review is {target.base_sha[:12]}..{target.head_sha[:12]}:\n\n```diff\n{body}\n```"


def commits_block(target: TargetInfo) -> str:
    lines = "\n".join(f"- {sha[:10]} {subject}" for sha, subject in target.commits[:200])
    return f"Commits in the change:\n{lines or '(none listed)'}"


def files_block(target: TargetInfo) -> str:
    return "Files changed:\n" + "\n".join(f"- {f}" for f in target.files)
