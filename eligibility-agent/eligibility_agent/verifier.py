"""Glue: eCW patients -> Availity coverage checks -> benefit summaries -> worklist."""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path

from .availity import AvailityClient, AvailityError, CoverageRequest, DEFAULT_SERVICE_TYPE
from .benefits import BenefitSummary, error_summary, summarize
from .config import PracticeSettings
from .ecw import Patient, PatientSource

log = logging.getLogger(__name__)


class EligibilityVerifier:
    def __init__(self, source: PatientSource, availity: AvailityClient, practice: PracticeSettings):
        self.source = source
        self.availity = availity
        self.practice = practice
        self._patients: dict[str, Patient] = {}
        self.results: dict[str, BenefitSummary] = {}

    def load_patients(self, appointment_date: str | None = None) -> list[Patient]:
        patients = self.source.list_patients(appointment_date)
        self._patients = {p.patient_id: p for p in patients}
        return patients

    def patient(self, patient_id: str) -> Patient:
        if patient_id not in self._patients:
            raise KeyError(f"Unknown patient {patient_id}; load the schedule first")
        return self._patients[patient_id]

    def payer_id_for(self, patient: Patient) -> str | None:
        return patient.payer_id or self.practice.payer_map.get(patient.payer_name.strip().lower())

    def verify(self, patient_id: str, service_type: str = DEFAULT_SERVICE_TYPE) -> BenefitSummary:
        patient = self.patient(patient_id)
        payer_id = self.payer_id_for(patient)
        if not payer_id:
            summary = error_summary(patient_id, f"No Availity payer ID mapped for payer {patient.payer_name!r}")
            summary.flags.append("PAYER_NOT_MAPPED")
        elif not patient.member_id:
            summary = error_summary(patient_id, "No member/subscriber ID on file in eCW")
            summary.flags.append("MISSING_MEMBER_ID")
        else:
            request = CoverageRequest(
                payer_id=payer_id,
                provider_npi=self.practice.provider_npi,
                member_id=patient.member_id,
                patient_birth_date=patient.birth_date,
                patient_first_name=patient.first_name,
                patient_last_name=patient.last_name,
                service_type=service_type,
                as_of_date=patient.appointment_date,
                provider_last_name=self.practice.provider_last_name,
                provider_tax_id=self.practice.provider_tax_id,
            )
            try:
                coverage = self.availity.check_eligibility(request)
                summary = summarize(patient_id, coverage, expected_name=(patient.first_name, patient.last_name))
            except AvailityError as exc:
                log.warning("Eligibility check failed for %s: %s", patient_id, exc)
                summary = error_summary(patient_id, str(exc))
                if exc.status_code == 429:
                    summary.flags.append("RATE_LIMITED")
        self.results[patient_id] = summary
        return summary

    def verify_all(self, appointment_date: str | None = None) -> list[BenefitSummary]:
        if appointment_date is not None or not self._patients:
            self.load_patients(appointment_date)
        return [self.verify(pid) for pid in self._patients]


WORKLIST_COLUMNS = [
    "patient_id", "payer", "eligible", "plan_status", "plan_name", "group_number",
    "coverage_start", "coverage_end", "copay", "coinsurance", "deductible",
    "deductible_remaining", "out_of_pocket", "out_of_pocket_remaining", "flags", "error",
    "coverage_id", "staff_note",
]


def write_worklist(results: list[BenefitSummary], out_dir: str | Path, notes: dict[str, str] | None = None) -> dict[str, str]:
    """Write worklist.csv (for staff/eCW import) and results.json (full detail)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    notes = notes or {}
    csv_path, json_path = out / "worklist.csv", out / "results.json"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=WORKLIST_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for r in results:
            row = r.to_dict()
            row["flags"] = "|".join(r.flags)
            row["staff_note"] = notes.get(r.patient_id, "")
            writer.writerow(row)
    json_path.write_text(json.dumps([{**r.to_dict(), "staff_note": notes.get(r.patient_id, "")} for r in results],
                                    indent=2), encoding="utf-8")
    return {"csv": str(csv_path), "json": str(json_path)}
