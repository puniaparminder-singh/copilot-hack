"""Offline tests: Availity is replaced by an httpx.MockTransport.

The coverage payloads are illustrative, shaped after Availity's Coverages
JSON; they are not captured payer responses.
"""

import csv
import json
from pathlib import Path

import httpx
import pytest

from eligibility_agent.availity import AvailityClient, AvailityError, CoverageRequest
from eligibility_agent.agent import build_tools
from eligibility_agent.benefits import summarize
from eligibility_agent.config import AvailitySettings, PracticeSettings
from eligibility_agent.ecw import ScheduleCsvSource, normalize_date
from eligibility_agent.verifier import EligibilityVerifier, write_worklist

SAMPLE_CSV = Path(__file__).parent.parent / "sample_data" / "schedule.csv"
BASE = "https://api.availity.com/availity/v1"

ACTIVE_COVERAGE = {
    "id": "cov-1",
    "status": "Complete",
    "statusCode": "4",
    "payer": {"name": "AETNA", "payerId": "AETNA-TEST"},
    "patient": {"firstName": "TEST", "lastName": "PATIENTONE"},
    "plans": [{
        "status": "Active Coverage",
        "statusCode": "1",
        "groupNumber": "G100",
        "groupName": "ACME CORP",
        "coverageStartDate": "2026-01-01",
        "benefits": [{
            "name": "Health Benefit Plan Coverage",
            "amounts": {
                "coPayment": {"inNetwork": [{"amount": "25.00", "level": "Individual"}]},
                "coInsurance": {"inNetwork": [{"percent": "20", "level": "Individual"}]},
                "deductibles": {"inNetwork": [
                    {"amount": "6000.00", "remaining": "4000.00", "level": "Family"},
                    {"amount": "1500.00", "remaining": "250.00", "level": "Individual"},
                ]},
                "outOfPocket": {"inNetwork": [{"amount": "5000.00", "remaining": "4100.00", "level": "Individual"}]},
            },
        }],
    }],
}

INACTIVE_COVERAGE = {
    "id": "cov-2", "status": "Complete", "statusCode": "4",
    "payer": {"name": "CIGNA"},
    "patient": {"lastName": "PATIENTTWO"},
    "plans": [{"status": "Inactive", "statusCode": "6", "coverageEndDate": "2026-06-30"}],
}


def settings(**overrides):
    return AvailitySettings(client_id="id", client_secret="secret", poll_interval_seconds=0, **overrides)


class FakeAvaility:
    """Mock transport: token endpoint plus async coverage polling."""

    def __init__(self, final_by_member: dict[str, dict], polls_before_complete: int = 1):
        self.final_by_member = final_by_member
        self.polls_before_complete = polls_before_complete
        self.token_calls = 0
        self.coverage_posts: list[dict] = []
        self.poll_counts: dict[str, int] = {}
        self.pending: dict[str, dict] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/token"):
            self.token_calls += 1
            form = dict(httpx.QueryParams(request.content.decode()))
            assert form["grant_type"] == "client_credentials"
            assert form["scope"] == "hipaa"
            return httpx.Response(200, json={"access_token": f"tok{self.token_calls}", "token_type": "bearer",
                                             "expires_in": 300, "scope": "hipaa"})
        assert request.headers["authorization"].startswith("Bearer tok")
        if path.endswith("/coverages") and request.method == "POST":
            form = dict(httpx.QueryParams(request.content.decode()))
            self.coverage_posts.append(form)
            final = self.final_by_member[form["memberId"]]
            self.pending[final["id"]] = final
            self.poll_counts[final["id"]] = 0
            return httpx.Response(202, json={"id": final["id"], "status": "In Progress", "statusCode": "0"},
                                  headers={"location": f"{BASE}/coverages/{final['id']}"})
        if "/coverages/" in path and request.method == "GET":
            cov_id = path.rsplit("/", 1)[-1]
            self.poll_counts[cov_id] += 1
            if self.poll_counts[cov_id] < self.polls_before_complete:
                return httpx.Response(200, json={"id": cov_id, "status": "In Progress", "statusCode": "0"})
            return httpx.Response(200, json=self.pending[cov_id])
        return httpx.Response(404)

    def client(self, **overrides) -> AvailityClient:
        return AvailityClient(settings(**overrides), http=httpx.Client(transport=httpx.MockTransport(self.handler)),
                              sleep=lambda _: None)


def practice():
    return PracticeSettings(provider_npi="1234567893", payer_map={"aetna": "AETNA-TEST", "cigna": "CIGNA-TEST"})


def test_normalize_date():
    assert normalize_date("01/15/1980") == "1980-01-15"
    assert normalize_date("1980-01-15") == "1980-01-15"
    assert normalize_date("") == ""


def test_csv_source_filters_by_date():
    patients = ScheduleCsvSource(SAMPLE_CSV).list_patients("2026-10-22")
    assert [p.patient_id for p in patients] == ["10001", "10002", "10003"]
    assert patients[0].birth_date == "1980-01-15"


def test_check_eligibility_polls_until_complete_and_reuses_token():
    fake = FakeAvaility({"W1": ACTIVE_COVERAGE, "W2": INACTIVE_COVERAGE}, polls_before_complete=3)
    client = fake.client()
    for member in ("W1", "W2"):
        cov = client.check_eligibility(CoverageRequest(payer_id="P", provider_npi="1", member_id=member,
                                                       patient_birth_date="1980-01-15"))
        assert cov["statusCode"] == "4"
    assert fake.poll_counts == {"cov-1": 3, "cov-2": 3}
    assert fake.token_calls == 1
    assert fake.coverage_posts[0]["serviceType"] == "30"


def test_check_eligibility_raises_when_never_completes():
    fake = FakeAvaility({"W1": ACTIVE_COVERAGE}, polls_before_complete=99)
    with pytest.raises(AvailityError, match="still in progress"):
        fake.client(max_polls=3).check_eligibility(
            CoverageRequest(payer_id="P", provider_npi="1", member_id="W1", patient_birth_date="1980-01-15"))


def test_check_eligibility_error_status():
    failed = {**ACTIVE_COVERAGE, "status": "Request Error", "statusCode": "7"}
    fake = FakeAvaility({"W1": failed})
    with pytest.raises(AvailityError, match="Request Error"):
        fake.client().check_eligibility(
            CoverageRequest(payer_id="P", provider_npi="1", member_id="W1", patient_birth_date="1980-01-15"))


def test_summarize_active_prefers_individual_amounts():
    s = summarize("10001", ACTIVE_COVERAGE, expected_name=("Test", "Patientone"))
    assert s.eligible is True
    assert (s.copay, s.coinsurance, s.deductible, s.deductible_remaining) == ("25.00", "20%", "1500.00", "250.00")
    assert s.out_of_pocket_remaining == "4100.00"
    assert s.flags == ["DEDUCTIBLE_NOT_MET"]


def test_summarize_inactive_and_name_mismatch():
    s = summarize("10002", INACTIVE_COVERAGE, expected_name=("Test", "Someoneelse"))
    assert s.eligible is False
    assert "INACTIVE_COVERAGE" in s.flags and "NAME_MISMATCH" in s.flags
    assert s.coverage_end == "2026-06-30"


def test_verifier_end_to_end(tmp_path):
    fake = FakeAvaility({"W123456789": ACTIVE_COVERAGE, "U987654321": INACTIVE_COVERAGE})
    verifier = EligibilityVerifier(ScheduleCsvSource(SAMPLE_CSV), fake.client(), practice())
    results = {r.patient_id: r for r in verifier.verify_all("2026-10-22")}
    assert results["10001"].eligible is True
    assert results["10002"].eligible is False
    assert "PAYER_NOT_MAPPED" in results["10003"].flags
    assert len(fake.coverage_posts) == 2  # unmapped payer is never sent to Availity
    assert fake.coverage_posts[0]["asOfDate"] == "2026-10-22"

    paths = write_worklist(list(results.values()), tmp_path, {"10002": "Ask for new card"})
    rows = list(csv.DictReader(open(paths["csv"])))
    assert rows[1]["staff_note"] == "Ask for new card"
    assert rows[1]["flags"] == "INACTIVE_COVERAGE"


def test_missing_member_id_is_flagged():
    verifier = EligibilityVerifier(ScheduleCsvSource(SAMPLE_CSV), FakeAvaility({}).client(), practice())
    verifier.load_patients("2026-10-23")
    assert "MISSING_MEMBER_ID" in verifier.verify("10004").flags


def test_agent_tools_do_not_expose_identifiers(tmp_path):
    fake = FakeAvaility({"W123456789": ACTIVE_COVERAGE, "U987654321": INACTIVE_COVERAGE})
    verifier = EligibilityVerifier(ScheduleCsvSource(SAMPLE_CSV), fake.client(), practice())
    list_tool, check_tool, note_tool, save_tool = build_tools(verifier, str(tmp_path))

    listed = list_tool.call({"appointment_date": "2026-10-22"})
    for secret in ("Patientone", "1980-01-15", "W123456789"):
        assert secret not in listed
    checked = check_tool.call({"patient_id": "10001"})
    assert "W123456789" not in checked and json.loads(checked)["eligible"] is True
    note_tool.call({"patient_id": "10001", "note": "Collect deductible estimate"})
    paths = json.loads(save_tool.call({}))
    assert "Collect deductible estimate" in Path(paths["csv"]).read_text()
