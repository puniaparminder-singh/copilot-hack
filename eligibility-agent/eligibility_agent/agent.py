"""Claude agent that runs eligibility checks and triages the results for staff.

Claude never sees names, dates of birth or member IDs: the tools only return
the eCW account number, payer and the benefit summary. Even so, the summaries
are PHI, so use the Claude API only under a BAA with Anthropic.
"""

from __future__ import annotations

import json

import anthropic
from anthropic import beta_tool

from .verifier import EligibilityVerifier, write_worklist

MODEL = "claude-opus-5-5"

SYSTEM_PROMPT = """You are an eligibility and benefits verification assistant for a medical practice \
that uses eClinicalWorks (eCW). Results come from Availity's real-time eligibility (270/271) service.

Your job for each run:
1. Load the patients scheduled for the requested date.
2. Run an eligibility check for every patient. If a check fails with RATE_LIMITED, retry it once later in the run.
3. Write a short, specific staff note for each patient who needs follow-up, for example: \
inactive coverage (ask for new insurance before the visit), payer not mapped (add the Availity payer ID), \
missing member ID, name mismatch (verify the card spelling in eCW), multiple plans (confirm \
coordination of benefits), deductible not met (collect an estimate at check-in), or unclear status \
(call the payer or check Availity Essentials). Patients with active coverage and no flags need no note.
4. Save the worklist, then reply with a brief summary: how many patients were checked, how many are \
clear, and the follow-ups grouped by type.

Only report what the tools return. Never guess at benefits, copays or coverage dates. If a field is \
missing, say it was not returned by the payer."""


def build_tools(verifier: EligibilityVerifier, out_dir: str):
    notes: dict[str, str] = {}

    @beta_tool
    def list_scheduled_patients(appointment_date: str) -> str:
        """Load the patients scheduled in eCW for a date.

        Args:
            appointment_date: Appointment date in YYYY-MM-DD format.
        """
        patients = verifier.load_patients(appointment_date)
        return json.dumps([
            {
                "patient_id": p.patient_id,
                "payer": p.payer_name,
                "payer_mapped": verifier.payer_id_for(p) is not None,
                "has_member_id": bool(p.member_id),
            }
            for p in patients
        ])

    @beta_tool
    def check_eligibility(patient_id: str, service_type: str = "30") -> str:
        """Run a real-time Availity eligibility and benefits check for one patient.

        Args:
            patient_id: eCW account number from list_scheduled_patients.
            service_type: X12 service type code. Use 30 (Health Benefit Plan Coverage) unless the visit type needs a specific one.
        """
        try:
            return json.dumps(verifier.verify(patient_id, service_type).to_dict())
        except KeyError as exc:
            return json.dumps({"error": str(exc)})

    @beta_tool
    def add_staff_note(patient_id: str, note: str) -> str:
        """Attach a follow-up note for front-desk or billing staff to a patient's worklist row.

        Args:
            patient_id: eCW account number.
            note: One or two sentences saying what staff should do before the visit.
        """
        notes[patient_id] = note
        return "saved"

    @beta_tool
    def save_worklist() -> str:
        """Write the worklist CSV and JSON for all patients checked so far. Call once at the end."""
        paths = write_worklist(list(verifier.results.values()), out_dir, notes)
        return json.dumps(paths)

    return [list_scheduled_patients, check_eligibility, add_staff_note, save_worklist]


def run_agent(verifier: EligibilityVerifier, appointment_date: str, out_dir: str,
              client: anthropic.Anthropic | None = None) -> str:
    client = client or anthropic.Anthropic()
    runner = client.beta.messages.tool_runner(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        tools=build_tools(verifier, out_dir),
        output_config={"effort": "medium"},
        # Re-run on Anthropic's recommended model if a safety classifier declines.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{
            "role": "user",
            "content": f"Verify eligibility and benefits for everyone scheduled on {appointment_date}.",
        }],
    )
    final = None
    for message in runner:
        final = message
    if final is None:
        return ""
    if final.stop_reason == "refusal":
        return "The model declined this request; the worklist may be incomplete. Run the batch mode instead."
    return "".join(block.text for block in final.content if block.type == "text")
