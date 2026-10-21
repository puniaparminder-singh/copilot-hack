"""Turn a raw Availity coverage into a short, staff-readable benefits summary.

The field names below follow the shape of Availity's Coverages JSON
(coverage -> plans[] -> benefits[] -> amounts). Payers fill these in very
unevenly, so every lookup is defensive. Verify the paths against the
Coverages 1.0.0 reference on developer.availity.com (free account required)
and adjust FIELD_* constants here if your responses differ.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

FIELD_PLANS = "plans"
FIELD_BENEFITS = "benefits"
FIELD_AMOUNTS = "amounts"

# Availity amount keys -> summary keys
AMOUNT_KEYS = {
    "coPayment": "copay",
    "coInsurance": "coinsurance",
    "deductibles": "deductible",
    "outOfPocket": "out_of_pocket",
}


@dataclass
class BenefitSummary:
    patient_id: str
    payer: str | None
    eligible: bool | None
    plan_status: str | None
    plan_name: str | None = None
    group_number: str | None = None
    coverage_start: str | None = None
    coverage_end: str | None = None
    insurance_type: str | None = None
    copay: str | None = None
    coinsurance: str | None = None
    deductible: str | None = None
    deductible_remaining: str | None = None
    out_of_pocket: str | None = None
    out_of_pocket_remaining: str | None = None
    coverage_id: str | None = None
    flags: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def summarize(patient_id: str, coverage: dict[str, Any], *, expected_name: tuple[str, str] | None = None) -> BenefitSummary:
    plans = coverage.get(FIELD_PLANS) or []
    plan = plans[0] if plans else {}
    plan_status = plan.get("status") or coverage.get("status")
    summary = BenefitSummary(
        patient_id=patient_id,
        payer=(coverage.get("payer") or {}).get("name"),
        eligible=_is_active(plan),
        plan_status=plan_status,
        plan_name=plan.get("planName") or plan.get("groupName") or plan.get("description"),
        group_number=plan.get("groupNumber"),
        coverage_start=plan.get("coverageStartDate") or plan.get("eligibilityStartDate"),
        coverage_end=plan.get("coverageEndDate") or plan.get("eligibilityEndDate"),
        insurance_type=plan.get("insuranceType"),
        coverage_id=coverage.get("id"),
    )

    for benefit in plan.get(FIELD_BENEFITS) or []:
        amounts = benefit.get(FIELD_AMOUNTS) or {}
        for availity_key, summary_key in AMOUNT_KEYS.items():
            if getattr(summary, summary_key) is None and availity_key in amounts:
                total, remaining = _in_network(amounts[availity_key])
                setattr(summary, summary_key, total)
                if summary_key == "deductible":
                    summary.deductible_remaining = remaining
                if summary_key == "out_of_pocket":
                    summary.out_of_pocket_remaining = remaining

    summary.flags = _flags(summary, coverage, plans, expected_name)
    return summary


def error_summary(patient_id: str, message: str) -> BenefitSummary:
    return BenefitSummary(patient_id=patient_id, payer=None, eligible=None, plan_status=None,
                          flags=["CHECK_FAILED"], error=message)


def _is_active(plan: dict[str, Any]) -> bool | None:
    status = (plan.get("status") or "").lower()
    code = str(plan.get("statusCode") or "")
    # X12 271 EB01: 1 = Active Coverage, 6 = Inactive.
    if code == "1" or status.startswith("active"):
        return True
    if code == "6" or status.startswith("inactive"):
        return False
    return None


def _in_network(amount_block: Any) -> tuple[str | None, str | None]:
    """Return (amount, remaining) for the first in-network, individual entry."""
    entries: list[dict[str, Any]] = []
    if isinstance(amount_block, dict):
        entries = amount_block.get("inNetwork") or amount_block.get("notApplicableNetwork") or []
    elif isinstance(amount_block, list):
        entries = amount_block
    if not entries:
        return None, None
    individual = [e for e in entries if (e.get("level") or "").lower().startswith("individual")]
    entry = (individual or entries)[0]
    amount = entry.get("amount")
    if amount is None and entry.get("percent") is not None:
        amount = f"{entry['percent']}%"
    remaining = entry.get("remaining")
    return (str(amount) if amount is not None else None, str(remaining) if remaining is not None else None)


def _flags(summary: BenefitSummary, coverage: dict[str, Any], plans: list, expected_name) -> list[str]:
    flags: list[str] = []
    if summary.eligible is False:
        flags.append("INACTIVE_COVERAGE")
    elif summary.eligible is None:
        flags.append("STATUS_UNCLEAR")
    if len(plans) > 1:
        flags.append("MULTIPLE_PLANS")
    if summary.deductible_remaining not in (None, "0", "0.0", "0.00"):
        flags.append("DEDUCTIBLE_NOT_MET")
    if expected_name:
        patient = coverage.get("patient") or coverage.get("subscriber") or {}
        returned_last = (patient.get("lastName") or "").strip().lower()
        if returned_last and returned_last != expected_name[1].strip().lower():
            flags.append("NAME_MISMATCH")
    if coverage.get("validationMessages") or coverage.get("errors"):
        flags.append("PAYER_MESSAGES")
    return flags
