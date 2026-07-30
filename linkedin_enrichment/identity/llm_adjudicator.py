"""Optional LLM adjudication of borderline candidates.

Off by default. The deterministic scorer must stand on its own — a rules engine
is auditable, reproducible and free, and those properties matter more than yield
for a CRM that will be acted on commercially.

When enabled, this stage sees only candidates inside a narrow band below the
threshold, and it can only ever do one of two things: confirm a candidate that
already passed every hard gate, or leave it blank. **It cannot resurrect a
candidate that a reject rule discarded, and it cannot invent a URL** — it is
handed a fixed list of candidates and must pick one or abstain.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You verify whether a LinkedIn search result belongs to a specific person from an \
Indian company registry (MCA/ROC) record.

Rules you must follow:
- The registry name is a legal long-form. The same person may appear on LinkedIn \
with dropped village/patronymic prefixes, initials, or a different token order.
- The registry "Designation" is a board appointment, NOT a job title. A different \
LinkedIn headline is NORMAL and is not evidence against a match.
- The company must corroborate the match. If no candidate mentions the company, \
there is no match.
- Common Indian names are shared by thousands of people. If two candidates are \
equally plausible, there is no match.
- You may only choose from the candidates given. Never construct a URL.
- When in doubt, abstain. A blank is always better than a wrong answer.

Reply with JSON only: {"index": <0-based candidate index or null>, \
"confidence": <0.0-1.0>, "reason": "<one sentence>"}"""


@dataclass
class Adjudication:
    index: int | None = None
    confidence: float = 0.0
    reason: str = ""

    @property
    def abstained(self) -> bool:
        return self.index is None


class LLMAdjudicator:
    """Wraps the Anthropic API. Fails soft: any error means "abstain"."""

    def __init__(self, config) -> None:
        self.config = config
        self._client = None
        self.calls = 0
        self.confirmations = 0
        self.abstentions = 0

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled and self.config.api_key)

    def _get_client(self):
        if self._client is None:
            try:
                from anthropic import AsyncAnthropic
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise RuntimeError(
                    "LLM adjudication enabled but the `anthropic` package is not installed"
                ) from exc
            self._client = AsyncAnthropic(api_key=self.config.api_key)
        return self._client

    def in_band(self, confidence: float) -> bool:
        return self.config.band_low <= confidence < self.config.band_high

    async def adjudicate(self, subject, candidates) -> Adjudication:
        """Ask the model to confirm one candidate or abstain."""
        if not self.enabled or not candidates:
            return Adjudication()

        prompt = _build_prompt(subject, candidates)
        try:
            client = self._get_client()
            response = await client.messages.create(
                model=self.config.model,
                max_tokens=self.config.max_tokens,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(
                block.text for block in response.content if getattr(block, "type", "") == "text"
            )
        except Exception as exc:  # noqa: BLE001 - never fail a row over this
            logger.warning("LLM adjudication failed, abstaining: %s", exc)
            return Adjudication()

        self.calls += 1
        verdict = _parse(text, len(candidates))
        if verdict.abstained:
            self.abstentions += 1
        else:
            self.confirmations += 1
        return verdict

    def stats(self) -> dict[str, int]:
        return {
            "llm_calls": self.calls,
            "llm_confirmations": self.confirmations,
            "llm_abstentions": self.abstentions,
        }


def _build_prompt(subject, candidates) -> str:
    lines = [
        "REGISTRY RECORD",
        f"  Name       : {subject.name.raw}",
        f"  Company    : {subject.company.raw}",
        f"  City       : {subject.location}",
        f"  Designation: {subject.designation}  (board role, not a job title)",
        f"  Industry   : {subject.industry}",
        "",
        "CANDIDATES",
    ]
    for index, candidate in enumerate(candidates):
        lines += [
            f"  [{index}] {candidate.result.title}",
            f"      url    : {candidate.url}",
            f"      snippet: {candidate.result.snippet or '(none)'}",
            f"      scores : {candidate.evidence()}",
        ]
    lines += ["", "Which candidate, if any, is this person? JSON only."]
    return "\n".join(lines)


def _parse(text: str, candidate_count: int) -> Adjudication:
    """Parse the model's JSON reply defensively; anything unexpected is an abstention."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return Adjudication()
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return Adjudication()

    index = data.get("index")
    if index is None or not isinstance(index, int) or not 0 <= index < candidate_count:
        return Adjudication(reason=str(data.get("reason", ""))[:200])

    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0

    return Adjudication(
        index=index,
        confidence=max(0.0, min(1.0, confidence)),
        reason=str(data.get("reason", ""))[:200],
    )
