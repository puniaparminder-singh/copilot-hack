"""Command line entry point.

    python -m eligibility_agent batch --csv schedule.csv --date 2026-10-22
    python -m eligibility_agent agent --csv schedule.csv --date 2026-10-22
"""

from __future__ import annotations

import argparse
import logging
import os

from .availity import AvailityClient
from .config import AvailitySettings, PracticeSettings
from .ecw import FhirSource, ScheduleCsvSource
from .verifier import EligibilityVerifier, write_worklist


def _source(args):
    if args.csv:
        return ScheduleCsvSource(args.csv)
    with open(os.environ["ECW_FHIR_PRIVATE_KEY_PATH"], encoding="utf-8") as fh:
        key = fh.read()
    return FhirSource(
        fhir_base_url=os.environ["ECW_FHIR_BASE_URL"],
        token_url=os.environ["ECW_FHIR_TOKEN_URL"],
        client_id=os.environ["ECW_FHIR_CLIENT_ID"],
        private_key_pem=key,
        key_id=os.environ.get("ECW_FHIR_KEY_ID"),
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="eligibility_agent")
    parser.add_argument("mode", choices=["batch", "agent"],
                        help="batch: check everyone and write the worklist; agent: same, plus Claude triage notes")
    parser.add_argument("--date", required=True, help="Appointment date, YYYY-MM-DD")
    parser.add_argument("--csv", help="eCW schedule export (CSV). Omit to use the eCW FHIR API (ECW_FHIR_* env vars)")
    parser.add_argument("--out", default="output", help="Directory for worklist.csv and results.json")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    verifier = EligibilityVerifier(_source(args), AvailityClient(AvailitySettings.from_env()),
                                   PracticeSettings.from_env())
    if args.mode == "batch":
        results = verifier.verify_all(args.date)
        paths = write_worklist(results, args.out)
        clear = sum(1 for r in results if r.eligible and not r.flags)
        print(f"Checked {len(results)} patients, {clear} clear, {len(results) - clear} need follow-up.")
        print(f"Worklist: {paths['csv']}")
    else:
        from .agent import run_agent

        print(run_agent(verifier, args.date, args.out))


if __name__ == "__main__":
    main()
