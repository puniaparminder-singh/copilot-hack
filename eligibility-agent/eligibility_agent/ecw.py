"""Patient sources for eClinicalWorks.

eCW does not offer a public API for writing eligibility results back into the
chart, so this agent *reads* patients from eCW and writes its results to a
worklist (CSV/JSON) that staff, or an eCW interface your practice already has,
can post back.

Two ways to read patients:

* ScheduleCsvSource - an appointment/insurance report exported from eCW
  (e.g. a Resource Schedule or custom eBO report) as CSV. Works today with
  no vendor enablement.
* FhirSource - eCW's FHIR R4 API using SMART Backend Services. The practice
  must enable FHIR in eCW admin settings and approve your backend app
  registered on eCW's developer portal. Which resources and searches are
  exposed (e.g. Coverage, Appointment) depends on the practice's eCW version,
  so confirm with your eCW setup before relying on it.
"""

from __future__ import annotations

import csv
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol

import httpx


@dataclass
class Patient:
    patient_id: str  # eCW account / MRN
    first_name: str
    last_name: str
    birth_date: str  # YYYY-MM-DD
    payer_name: str
    member_id: str
    appointment_date: str | None = None
    payer_id: str | None = None  # Availity payer ID, if already known


class PatientSource(Protocol):
    def list_patients(self, appointment_date: str | None = None) -> list[Patient]: ...


# Default column names; override with column_map to match your eCW report.
DEFAULT_COLUMNS = {
    "patient_id": "Account No",
    "first_name": "Patient First Name",
    "last_name": "Patient Last Name",
    "birth_date": "DOB",
    "payer_name": "Primary Insurance",
    "member_id": "Primary Subscriber No",
    "appointment_date": "Appointment Date",
    "payer_id": "Payer ID",
}


class ScheduleCsvSource:
    def __init__(self, path: str | Path, column_map: dict[str, str] | None = None):
        self.path = Path(path)
        self.columns = {**DEFAULT_COLUMNS, **(column_map or {})}

    def list_patients(self, appointment_date: str | None = None) -> list[Patient]:
        with self.path.open(newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        patients = [self._to_patient(row) for row in rows]
        if appointment_date:
            patients = [p for p in patients if p.appointment_date == appointment_date]
        return patients

    def _to_patient(self, row: dict[str, str]) -> Patient:
        get = lambda key: (row.get(self.columns[key]) or "").strip()  # noqa: E731
        return Patient(
            patient_id=get("patient_id"),
            first_name=get("first_name"),
            last_name=get("last_name"),
            birth_date=normalize_date(get("birth_date")),
            payer_name=get("payer_name"),
            member_id=get("member_id"),
            appointment_date=normalize_date(get("appointment_date")) or None,
            payer_id=get("payer_id") or None,
        )


def normalize_date(value: str) -> str:
    """Accept eCW's MM/DD/YYYY or ISO dates; return YYYY-MM-DD."""
    value = value.strip()
    if not value:
        return ""
    if "/" in value:
        month, day, year = value.split("/")[:3]
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    return value[:10]


class FhirSource:
    """Read scheduled patients and their coverage from eCW's FHIR R4 API."""

    def __init__(self, fhir_base_url: str, token_url: str, client_id: str, private_key_pem: str,
                 key_id: str | None = None, scope: str = "system/Patient.read system/Coverage.read system/Appointment.read",
                 http: httpx.Client | None = None):
        self.base = fhir_base_url.rstrip("/")
        self.token_url = token_url
        self.client_id = client_id
        self.private_key_pem = private_key_pem
        self.key_id = key_id
        self.scope = scope
        self.http = http or httpx.Client(timeout=30.0)
        self._token: str | None = None
        self._expires_at = 0.0

    def _access_token(self) -> str:
        if self._token and time.time() < self._expires_at - 30:
            return self._token
        import jwt  # PyJWT; only needed for the FHIR source

        now = int(time.time())
        assertion = jwt.encode(
            {"iss": self.client_id, "sub": self.client_id, "aud": self.token_url,
             "jti": str(uuid.uuid4()), "iat": now, "exp": now + 300},
            self.private_key_pem, algorithm="RS384",
            headers={"kid": self.key_id} if self.key_id else None,
        )
        resp = self.http.post(self.token_url, data={
            "grant_type": "client_credentials",
            "scope": self.scope,
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": assertion,
        })
        resp.raise_for_status()
        payload = resp.json()
        self._token = payload["access_token"]
        self._expires_at = time.time() + float(payload.get("expires_in", 300))
        return self._token

    def _get(self, path: str, params: dict[str, str] | None = None) -> dict:
        resp = self.http.get(f"{self.base}/{path}", params=params,
                             headers={"Authorization": f"Bearer {self._access_token()}",
                                      "Accept": "application/fhir+json"})
        resp.raise_for_status()
        return resp.json()

    def _entries(self, bundle: dict) -> Iterable[dict]:
        for entry in bundle.get("entry") or []:
            yield entry.get("resource") or {}

    def list_patients(self, appointment_date: str | None = None) -> list[Patient]:
        if not appointment_date:
            raise ValueError("FhirSource needs an appointment_date (YYYY-MM-DD)")
        bundle = self._get("Appointment", {"date": appointment_date})
        patient_refs: list[str] = []
        for appt in self._entries(bundle):
            for participant in appt.get("participant") or []:
                ref = (participant.get("actor") or {}).get("reference", "")
                if ref.startswith("Patient/") and ref not in patient_refs:
                    patient_refs.append(ref)
        return [p for ref in patient_refs if (p := self._patient(ref.split("/", 1)[1], appointment_date))]

    def _patient(self, fhir_id: str, appointment_date: str) -> Patient | None:
        pat = self._get(f"Patient/{fhir_id}")
        name = (pat.get("name") or [{}])[0]
        coverages = list(self._entries(self._get("Coverage", {"patient": fhir_id, "status": "active"})))
        if not coverages:
            return None
        cov = coverages[0]
        payer = ((cov.get("payor") or [{}])[0]).get("display", "")
        return Patient(
            patient_id=fhir_id,
            first_name=" ".join(name.get("given") or []),
            last_name=name.get("family", ""),
            birth_date=pat.get("birthDate", ""),
            payer_name=payer,
            member_id=cov.get("subscriberId") or "",
            appointment_date=appointment_date,
        )
