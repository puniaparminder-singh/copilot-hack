"""Settings loaded from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    return value if value not in ("", None) else default


@dataclass(frozen=True)
class AvailitySettings:
    client_id: str
    client_secret: str
    base_url: str = "https://api.availity.com/availity/v1"
    scope: str = "hipaa"
    # Availity's API guide shows GET /coverages; some integrators report the
    # endpoint moved to POST. Confirm against the Coverages 1.0.0 reference
    # for your subscription and set AVAILITY_COVERAGES_METHOD accordingly.
    coverages_method: str = "POST"
    poll_interval_seconds: float = 2.0
    max_polls: int = 15
    # Demo plan only: canned scenario returned instead of live payer data.
    mock_scenario_id: str | None = None

    @classmethod
    def from_env(cls) -> "AvailitySettings":
        client_id = _env("AVAILITY_CLIENT_ID")
        client_secret = _env("AVAILITY_CLIENT_SECRET")
        if not client_id or not client_secret:
            raise RuntimeError("AVAILITY_CLIENT_ID and AVAILITY_CLIENT_SECRET must be set")
        return cls(
            client_id=client_id,
            client_secret=client_secret,
            base_url=_env("AVAILITY_BASE_URL", cls.base_url).rstrip("/"),
            scope=_env("AVAILITY_SCOPE", cls.scope),
            coverages_method=_env("AVAILITY_COVERAGES_METHOD", cls.coverages_method).upper(),
            poll_interval_seconds=float(_env("AVAILITY_POLL_INTERVAL", "2")),
            max_polls=int(_env("AVAILITY_MAX_POLLS", "15")),
            mock_scenario_id=_env("AVAILITY_MOCK_SCENARIO_ID"),
        )


@dataclass(frozen=True)
class PracticeSettings:
    provider_npi: str
    provider_last_name: str | None = None  # or organization name
    provider_tax_id: str | None = None
    # payer name as it appears in eCW -> Availity payer ID
    payer_map: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "PracticeSettings":
        npi = _env("PROVIDER_NPI")
        if not npi:
            raise RuntimeError("PROVIDER_NPI must be set")
        payer_map: dict[str, str] = {}
        for pair in (_env("PAYER_MAP", "") or "").split(";"):
            if "=" in pair:
                name, payer_id = pair.split("=", 1)
                payer_map[name.strip().lower()] = payer_id.strip()
        return cls(
            provider_npi=npi,
            provider_last_name=_env("PROVIDER_LAST_NAME"),
            provider_tax_id=_env("PROVIDER_TAX_ID"),
            payer_map=payer_map,
        )
