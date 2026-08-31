"""Stable, human-readable morning report rendering."""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from .scoring import Action, Provenance, ScoreResult


def _stamp(value: datetime) -> str:
    return value.isoformat(timespec="minutes")


def render_markdown(results: Iterable[ScoreResult], *, generated_at: datetime) -> str:
    """Render a deterministic report ordered by action urgency then ticker."""

    priority = {Action.EXIT: 0, Action.TRIM: 1, Action.BUY: 2, Action.WATCH: 3, Action.NO_ACTION: 4}
    ordered = sorted(results, key=lambda item: (priority[item.action], item.ticker))
    lines = [
        "# Codex Screener report",
        "",
        f"Generated: {_stamp(generated_at)}",
        "",
        "Decision support only. Market-implied probability, model estimates, and action gates are separate.",
        "",
        "| Ticker | Action | Setup | Directional estimate | Confidence | Readiness |",
        "| --- | --- | ---: | ---: | ---: | --- |",
    ]
    for item in ordered:
        gate = "PASS" if item.data_gate.passed else "BLOCKED"
        execution = "READY" if item.execution_ready else "NOT READY"
        calibration = "VALIDATED" if item.calibration_ready else "SHADOW"
        lines.append(
            f"| {item.ticker} | {item.action.value} | {item.setup_score}/100 | "
            f"{item.directional_probability:.0%} | {item.confidence}/100 | "
            f"{gate} / {execution} / {calibration} |"
        )

    for item in ordered:
        provenance = ", ".join(
            f"{kind.value} {item.provenance_summary.get(kind, 0)}" for kind in Provenance
        )
        lines.extend([
            "",
            f"## {item.ticker} — {item.action.value}",
            "",
            f"Scoring version: `{item.scoring_version}` · Snapshot: {_stamp(item.as_of)}",
            "",
            f"Evidence provenance: {provenance}.",
            "",
            "Reasons:",
            *[f"- {reason}" for reason in item.reasons],
        ])
        if not item.data_gate.passed:
            lines.extend(["", "Do not open a new position until the blocked data gates are resolved."])
        elif not item.execution_ready:
            lines.extend(["", "Execution is not ready. This snapshot may inform a watchlist but cannot authorize a new option entry."])
        elif not item.calibration_ready:
            lines.extend(["", "Calibration is not ready. Keep this result in shadow mode; it cannot authorize a new option entry."])

    return "\n".join(lines) + "\n"
