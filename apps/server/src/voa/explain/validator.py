"""Explanation validator: evidence and claim checks (SPEC 6, EXP-01, T-R08).

Changing the term lists requires changing the tests with them.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from voa.explain.repository import ObservationRepository

ViolationCode = Literal[
    "unknown_evidence",
    "foreign_session",
    "stale_evidence",
    "numeric_claim",
    "identity_claim",
    "action_claim",
]
VIOLATION_ORDER: tuple[ViolationCode, ...] = (
    "unknown_evidence",
    "foreign_session",
    "stale_evidence",
    "numeric_claim",
    "identity_claim",
    "action_claim",
)

NUMERIC_CLAIM = re.compile(
    r"\d+(?:[.,]\d+)?\s*(?:m|cm|mm|km|km/h|㎞|미터|센티|킬로|%|퍼센트|초|분)", re.IGNORECASE
)
IDENTITY_TERMS = ("신원", "이름은", "사번", "직원번호", "얼굴 인식", "누구인지")
ACTION_TERMS = ("신고했", "신고 완료", "정지시켰", "멈췄", "차단했", "전송했", "호출했")


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    violations: tuple[ViolationCode, ...]  # unique, in VIOLATION_ORDER


class ExplanationValidator:
    def __init__(self, repository: ObservationRepository) -> None:
        self._repository = repository

    def validate(
        self, text: str, evidence_ids: Sequence[str], session_id: str, now_mono_ns: int
    ) -> ValidationResult:
        """An explanation without any evidence counts as unknown_evidence."""
        found: set[ViolationCode] = set()
        if not evidence_ids:
            found.add("unknown_evidence")
        for evidence_id in evidence_ids:
            observation = self._repository.get(evidence_id)
            if observation is None:
                found.add("unknown_evidence")
            elif observation.session_id != session_id:
                found.add("foreign_session")
            elif not self._repository.is_fresh(evidence_id, now_mono_ns):
                found.add("stale_evidence")
        if NUMERIC_CLAIM.search(text):
            found.add("numeric_claim")
        if any(term in text for term in IDENTITY_TERMS):
            found.add("identity_claim")
        if any(term in text for term in ACTION_TERMS):
            found.add("action_claim")
        violations = tuple(code for code in VIOLATION_ORDER if code in found)
        return ValidationResult(ok=not violations, violations=violations)
