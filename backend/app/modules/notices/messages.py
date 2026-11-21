"""The text of each notice. Plain text, fixed subjects: names chosen by users (workspace, projects) appear
only in the body, stripped of control characters, so they cannot shape headers."""

from __future__ import annotations

import re
from dataclasses import dataclass

_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")

SUBJECTS = {
    "site_suspended": "A site in your workspace stopped serving",
    "billing_past_due": "A payment for your workspace is past due",
    "dispute_opened": "A payment for your workspace is disputed",
    "dispute_lost": "A payment dispute for your workspace was lost",
}

_REASONS = {
    "connection_inactive": (
        "The GitHub connection that reads its repository is no longer active. Reconnect it on the"
        " Connections page, then publish the site again."
    ),
    "repository_revoked": (
        "The GitHub App no longer has access to its repository. Grant access again, then publish the"
        " site again."
    ),
    "operator": (
        "The service operator took it down. Contact support if you think this is a mistake; workspace"
        " admins cannot lift a takedown."
    ),
    "billing": (
        "The workspace's payment has been past due for too long, or its plan ended. The site serves"
        " again once billing is in order on the Plan and usage page."
    ),
    "license": (
        "Its repository's license does not permit a public site, or no license was recorded. Publish the"
        " site again to check the license, or publish it privately."
    ),
}


def subject(kind: str, operator: str) -> str:
    return f"[{clean(operator, 60)}] {SUBJECTS[kind]}"


def clean(value: str, limit: int = 120) -> str:
    return _CONTROL.sub(" ", value).strip()[:limit]


@dataclass(frozen=True)
class Context:
    origin: str  # PUBLIC_ORIGIN
    workspace_id: str
    workspace_name: str
    projects: list[tuple[str, str]]  # (project id, name) of the projects a notice names that still exist
    support: str | None


def body(kind: str, details: dict[str, object], ctx: Context) -> str:
    workspace = clean(ctx.workspace_name)
    usage = f"{ctx.origin}/w/{ctx.workspace_id}/usage"
    lines: list[str]
    if kind == "site_suspended":
        reason = str(details.get("reason") or "")
        count = len(ctx.projects)
        lines = [
            f"{'These sites' if count > 1 else 'A site'} in the workspace “{workspace}” stopped serving:",
            "",
            *(f"- {clean(name)}: {ctx.origin}/p/{pid}" for pid, name in ctx.projects),
            "",
            _REASONS.get(reason, "Its publication was suspended."),
        ]
    elif kind == "billing_past_due":
        lines = [
            f"A payment for the workspace “{workspace}” failed and is now past due.",
            "",
            "New builds are paused until it is paid. If it stays unpaid, the workspace's sites stop serving.",
            "",
            f"Update the payment method on the Plan and usage page: {usage}",
        ]
    elif kind == "dispute_opened":
        lines = [
            f"A payment for the workspace “{workspace}” is disputed with the card issuer.",
            "",
            "New builds are paused while the dispute is open. Published sites keep serving.",
            "",
            f"Plan and usage: {usage}",
        ]
    else:  # dispute_lost
        lines = [
            f"A payment dispute for the workspace “{workspace}” closed in the cardholder's favour.",
            "",
            "New builds stay paused until support reviews the workspace.",
        ]
    if ctx.support:
        lines += ["", f"Support: {ctx.support}"]
    lines += [
        "",
        "You receive this because you are an owner or admin of this workspace.",
    ]
    return "\n".join(lines) + "\n"
