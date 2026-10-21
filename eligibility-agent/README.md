# eCW + Availity Eligibility & Benefits Agent

This agent checks insurance eligibility and benefits for patients scheduled in eClinicalWorks (eCW). The checks run through Availity's real-time 270/271 Coverages API. It writes a worklist that front-desk and billing staff can act on before the visit.

```
eCW schedule ──► EligibilityVerifier ──► Availity Coverages API ──► benefit summary + flags
(CSV export       (payer mapping,          (OAuth token, submit,        │
 or FHIR R4)       missing-data checks)      poll until complete)        ▼
                                                              worklist.csv / results.json
                                                                        │
                                           optional: Claude agent writes staff follow-up notes
```

## Two modes

| Mode | What it does | Needs Claude? |
|---|---|---|
| `batch` | Checks every scheduled patient and writes `worklist.csv` and `results.json` | No |
| `agent` | Does the same checks, then Claude retries rate-limited calls, writes a follow-up note per flagged patient and summarizes the day | Yes |

## Setup

```bash
cd eligibility-agent
pip install -r requirements.txt
cp .env.example .env   # fill in, then: set -a; source .env; set +a
python -m pytest -q    # offline tests, no credentials needed

python -m eligibility_agent batch --csv sample_data/schedule.csv --date 2026-10-22
python -m eligibility_agent agent --csv sample_data/schedule.csv --date 2026-10-22
```

1. **Availity:** Create an account at developer.availity.com, register an app, and subscribe it to the Coverages API. Start on the Demo plan, which returns canned responses. Your practice also needs an Availity provider relationship before you can move to the Standard plan for live payer data.
2. **Payer IDs:** Map each insurance name in eCW to its Availity payer ID in `PAYER_MAP`. Take the IDs from Availity's payer list and don't assume your clearinghouse's EDI IDs match. Unmapped payers are flagged `PAYER_NOT_MAPPED` and are never sent to Availity.
3. **eCW patients:** Choose one of two sources.
   - **CSV (works today):** Export the day's appointments with insurance from eCW, for example as a schedule or eBO report. If your column names differ, change `DEFAULT_COLUMNS` in `eligibility_agent/ecw.py`.
   - **FHIR R4:** Register a backend-services app on eCW's developer portal. The practice then enables FHIR in eCW admin and approves the app. Set the `ECW_FHIR_*` variables and leave out `--csv`.

## Flags in the worklist

| Flag | Meaning / suggested action |
|---|---|
| `INACTIVE_COVERAGE` | Payer says the plan is inactive. Get new insurance before the visit. |
| `STATUS_UNCLEAR` | Payer returned no clear active or inactive status. Check Availity Essentials or call the payer. |
| `DEDUCTIBLE_NOT_MET` | Remaining in-network individual deductible is above 0. Give the patient a cost estimate. |
| `MULTIPLE_PLANS` | More than one plan was returned. Confirm coordination of benefits. |
| `NAME_MISMATCH` | Last name on the payer's file differs from eCW. Check the card spelling. |
| `PAYER_MESSAGES` | Payer sent validation messages. Review them in `results.json`. |
| `PAYER_NOT_MAPPED` / `MISSING_MEMBER_ID` | Data is missing in the config or in eCW. |
| `CHECK_FAILED` / `RATE_LIMITED` | Availity error or 429. Retry. |

## What was verified, and what you still need to confirm

**Confirmed from Availity's official API guide ([developer.availity.com](https://developer.availity.com/blog/2025/10/31/availity-api-guide)):**
- Tokens come from `POST https://api.availity.com/availity/v1/token` with `grant_type=client_credentials` and `scope=hipaa`. A token is valid for 5 minutes (`expires_in: 300`).
- Calls use `Authorization: Bearer <token>`.
- A Coverages request returns `statusCode "0"` ("In Progress") and an `id`. Poll `GET /coverages/{id}` about every 2 seconds until `statusCode "4"` ("Complete"). Any other code is an error.
- Every poll counts toward your limits. The Demo plan allows 5 calls per second and 500 per day. The Standard plan allows 100 per second and 100,000 per day.
- On the Demo plan, the `X-Api-Mock-Scenario-ID` header selects a canned response.

**Not confirmed. Check these in the Coverages 1.0.0 reference, which needs a free developer login:**
- **GET vs POST for submitting a check.** The guide shows `GET /coverages`, but an integrator ([Candid Health](https://docs.joincandidhealth.com/api-reference/eligibility/v-2/submit-eligibility-check-availity-post)) says Availity moved it to POST. The code defaults to POST with a form-encoded body. Set `AVAILITY_COVERAGES_METHOD=GET` if needed.
- **Request parameter names.** `payerId` and `providerNpi` appear in the guide. The other names (`memberId`, `patientBirthDate`, `patientLastName`, `serviceType`, `asOfDate`, and so on) are in `CoverageRequest.to_params()` and should be checked.
- **Response field names** for plans, benefits and amounts. They are isolated in `eligibility_agent/benefits.py`. The test fixtures are illustrative, not real payer responses.
- **Which eCW FHIR resources and searches your eCW version exposes**, such as `Appointment?date=` and `Coverage?patient=`. eCW's free FHIR APIs do not cover financial or operational data. eCW has no public API for writing eligibility results back to the chart, so results go to the worklist instead.

## Compliance notes

- Eligibility responses are PHI. Only run `agent` mode if your organization has a BAA with Anthropic. The agent's tools never send names, dates of birth or member IDs to Claude, only the eCW account number, payer and benefit summary. The test `test_agent_tools_do_not_expose_identifiers` enforces this.
- Keep `.env`, private keys and `output/` out of git. `.gitignore` already covers them.
- Real-time eligibility is a snapshot. It is not a guarantee of payment, so staff should still review flagged patients.
