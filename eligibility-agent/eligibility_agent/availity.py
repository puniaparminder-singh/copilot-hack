"""Availity Coverages (270/271 eligibility & benefits) client.

Flow, per Availity's API guide:
  1. POST {base}/token with grant_type=client_credentials, scope=hipaa.
     Tokens expire after 5 minutes (expires_in=300), so they are cached and
     refreshed shortly before expiry.
  2. Submit a coverage request. The payer usually answers asynchronously:
     the first response has statusCode "0" ("In Progress") and an id.
  3. Poll GET {base}/coverages/{id} about every 2 seconds until statusCode
     "4" ("Complete"). Any other code is an error. Every poll counts toward
     the plan's transaction limits (Demo: 5/s, 500/day).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from .config import AvailitySettings

STATUS_IN_PROGRESS = "0"
STATUS_COMPLETE = "4"

# X12 service type code 30 = "Health Benefit Plan Coverage" (general check).
DEFAULT_SERVICE_TYPE = "30"


class AvailityError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


@dataclass
class CoverageRequest:
    payer_id: str
    provider_npi: str
    member_id: str
    patient_birth_date: str  # YYYY-MM-DD
    patient_last_name: str | None = None
    patient_first_name: str | None = None
    service_type: str = DEFAULT_SERVICE_TYPE
    as_of_date: str | None = None  # YYYY-MM-DD, defaults to payer's "today"
    provider_last_name: str | None = None
    provider_tax_id: str | None = None

    def to_params(self) -> dict[str, str]:
        params = {
            "payerId": self.payer_id,
            "providerNpi": self.provider_npi,
            "memberId": self.member_id,
            "patientBirthDate": self.patient_birth_date,
            "patientLastName": self.patient_last_name,
            "patientFirstName": self.patient_first_name,
            "serviceType": self.service_type,
            "asOfDate": self.as_of_date,
            "providerLastName": self.provider_last_name,
            "providerTaxId": self.provider_tax_id,
        }
        return {k: v for k, v in params.items() if v}


class AvailityClient:
    def __init__(
        self,
        settings: AvailitySettings,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.settings = settings
        self.http = http or httpx.Client(timeout=30.0)
        self._sleep = sleep
        self._clock = clock
        self._token: str | None = None
        self._token_expires_at = 0.0

    # -- auth ---------------------------------------------------------------

    def _access_token(self) -> str:
        # Refresh 30s early so a token never expires mid-poll.
        if self._token and self._clock() < self._token_expires_at - 30:
            return self._token
        resp = self.http.post(
            f"{self.settings.base_url}/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.settings.client_id,
                "client_secret": self.settings.client_secret,
                "scope": self.settings.scope,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code != 200:
            raise AvailityError("Availity token request failed", status_code=resp.status_code, body=resp.text)
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_expires_at = self._clock() + float(payload.get("expires_in", 300))
        return self._token

    def _headers(self) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._access_token()}", "Accept": "application/json"}
        if self.settings.mock_scenario_id:
            headers["X-Api-Mock-Scenario-ID"] = self.settings.mock_scenario_id
        return headers

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        resp = self.http.request(method, url, headers=self._headers(), **kwargs)
        if resp.status_code == 401:
            # Token revoked or expired early: refresh once and retry.
            self._token = None
            resp = self.http.request(method, url, headers=self._headers(), **kwargs)
        if resp.status_code == 429:
            raise AvailityError("Availity rate limit exceeded (429)", status_code=429, body=resp.text)
        if resp.status_code >= 400:
            raise AvailityError(f"Availity {method} {url} failed", status_code=resp.status_code, body=_safe_json(resp))
        return resp

    # -- coverages ----------------------------------------------------------

    def submit_coverage(self, request: CoverageRequest) -> dict[str, Any]:
        url = f"{self.settings.base_url}/coverages"
        params = request.to_params()
        if self.settings.coverages_method == "GET":
            resp = self._request("GET", url, params=params)
        else:
            resp = self._request("POST", url, data=params)
        body = _safe_json(resp) or {}
        # A GET search returns a collection; a POST returns the coverage itself.
        if isinstance(body, dict) and "coverages" in body and body["coverages"]:
            body = body["coverages"][0]
        if not body.get("id"):
            location = resp.headers.get("location", "")
            if location:
                body["id"] = location.rstrip("/").rsplit("/", 1)[-1]
        return body

    def get_coverage(self, coverage_id: str) -> dict[str, Any]:
        resp = self._request("GET", f"{self.settings.base_url}/coverages/{coverage_id}")
        return _safe_json(resp) or {}

    def check_eligibility(self, request: CoverageRequest) -> dict[str, Any]:
        """Submit a request and poll until the payer answers. Returns the raw coverage."""
        coverage = self.submit_coverage(request)
        polls = 0
        while str(coverage.get("statusCode")) == STATUS_IN_PROGRESS:
            if polls >= self.settings.max_polls:
                raise AvailityError(
                    f"Coverage {coverage.get('id')} still in progress after {polls} polls", body=coverage
                )
            if not coverage.get("id"):
                raise AvailityError("In-progress coverage response has no id to poll", body=coverage)
            self._sleep(self.settings.poll_interval_seconds)
            coverage = self.get_coverage(coverage["id"])
            polls += 1
        if str(coverage.get("statusCode")) != STATUS_COMPLETE:
            raise AvailityError(
                f"Coverage finished with status {coverage.get('status')!r} (code {coverage.get('statusCode')})",
                body=coverage,
            )
        return coverage


def _safe_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return resp.text
