"""Hand-validation harness: turn a model score into a measured precision.

The pipeline reports a calibrated confidence. That is a model output. Nobody
should quote "≥99% precision" on the strength of it, because the calibration
itself was never fitted to this dataset.

Measuring it requires human eyes. The arithmetic is unforgiving:

    381 rows labelled, ZERO errors -> Wilson 95% lower bound = 0.9900
    381 rows labelled, ONE error   -> Wilson 95% lower bound = 0.9860

So a single mistake in the sample means you cannot claim 99%. That is the honest
cost of the claim, and this module makes it cheap to pay: each row is presented
with its evidence and answered with one keystroke.

Labels are stored, so validation can be done in several sittings and the bound
recomputed at any point.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..identity.calibrate import wilson_lower_bound

logger = logging.getLogger(__name__)


@dataclass
class ValidationState:
    labelled: int = 0
    correct: int = 0
    incorrect: int = 0
    skipped: int = 0
    total_matches: int = 0

    @property
    def precision(self) -> float:
        return self.correct / self.labelled if self.labelled else 0.0

    @property
    def lower_bound(self) -> float:
        return wilson_lower_bound(self.correct, self.labelled)

    def bound_for(self, target: float = 0.99) -> int | None:
        """How many more labels, all correct, would reach ``target``.

        Returns None when the target is not reachable by sampling. That is the
        case whenever the *observed* precision is already below the target: the
        errors are in the population, not the sample, so labelling more rows at
        the same error rate cannot rescue it. Arithmetically one could always
        drive the ratio up with enough consecutive correct labels, but expecting
        an unbroken run of them from a population that just produced errors is
        not a plan — the matcher or the threshold has to change instead.
        """
        if self.incorrect and self.precision < target:
            return None
        n, k = self.labelled, self.correct
        for extra in range(0, 5000):
            if wilson_lower_bound(k + extra, n + extra) >= target:
                return extra
        return None


def render_row(row, index: int, total: int) -> str:
    """One match, laid out for a human to judge in a few seconds."""
    lines = [
        "=" * 78,
        f"  {index}/{total}   confidence {float(row['confidence']):.4f}",
        "=" * 78,
        f"  REGISTRY : {row['name']}",
        f"             {row['company']}",
        f"             {row['designation']} · {row['location']}",
        "",
        f"  PROPOSED : {row['linkedin_url']}",
    ]
    sources = [s.strip() for s in (row["source_urls"] or "").split("|") if s.strip()]
    for source in sources:
        if "linkedin.com" not in source:
            lines.append(f"  EVIDENCE : {source}")
    if row["notes"]:
        lines.append(f"  NOTES    : {row['notes'][:180]}")
    lines += [
        "",
        "  Is this the right person?  [y] yes  [n] no  [s] skip  [q] save and quit",
    ]
    return "\n".join(lines)


def render_summary(state: ValidationState, target: float = 0.99) -> str:
    lines = [
        "=" * 78,
        "  VALIDATION RESULT",
        "=" * 78,
        f"  Matches in deliverable : {state.total_matches:,}",
        f"  Labelled               : {state.labelled:,}"
        f"   (correct {state.correct:,}, incorrect {state.incorrect:,}, skipped {state.skipped:,})",
    ]
    if not state.labelled:
        lines += ["", "  Nothing labelled yet.", "=" * 78]
        return "\n".join(lines)

    lines += [
        f"  Observed precision     : {state.precision:.4f}",
        f"  Wilson 95% lower bound : {state.lower_bound:.4f}   <- quote THIS number",
        "",
    ]

    if state.lower_bound >= target:
        lines.append(f"  PASS — precision >= {target:.0%} is supported at n={state.labelled}.")
    else:
        needed = state.bound_for(target)
        if needed is None:
            lines += [
                f"  CANNOT REACH {target:.0%} — {state.incorrect} error(s) already found.",
                "  The threshold or the matching rules need to change, not the sample.",
            ]
        else:
            lines += [
                f"  NOT YET — need ~{needed} more labels, all correct, to reach {target:.0%}.",
                f"  (a single further error pushes this target further away)",
            ]
    lines.append("=" * 78)
    return "\n".join(lines)
