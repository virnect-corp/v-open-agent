"""Explanation with approved-template fallback (SPEC 6, EXP-01).

A candidate text (for example from a VLM in Stage E) is used only if it passes
the validator against the decision's own evidence. Otherwise, or when there is
no candidate, the scenario's approved warning_text is returned.
"""

from dataclasses import dataclass

from voa.contracts.decision import RiskDecision
from voa.contracts.enums import TextKind
from voa.engine.profile import PolicyProfile
from voa.explain.validator import ExplanationValidator, ViolationCode


@dataclass(frozen=True)
class Explanation:
    text: str
    text_kind: TextKind
    violations: tuple[ViolationCode, ...]


class TemplateExplanationProvider:
    def __init__(self, profile: PolicyProfile, validator: ExplanationValidator) -> None:
        self._templates = {scenario.id: scenario.warning_text for scenario in profile.scenarios}
        self._validator = validator

    def explain(
        self, decision: RiskDecision, candidate: str | None, now_mono_ns: int
    ) -> Explanation:
        template = self._templates.get(decision.scenario_id)
        if template is None:
            raise ValueError("decision scenario is not in the profile")
        if candidate is None or not candidate.strip():
            return Explanation(template, "approved_template", ())
        result = self._validator.validate(
            candidate, decision.evidence_ids, decision.session_id, now_mono_ns
        )
        if result.ok:
            return Explanation(candidate, "validated_explanation", ())
        return Explanation(template, "approved_template", result.violations)
