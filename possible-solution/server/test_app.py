from pathlib import Path

import pytest

import app as app_module


class StubModel:
    def predict_proba(self, features):
        assert features == [[3, 14771]]
        return [[0.75, 0.25]]


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "model", StubModel())

    csv_file = tmp_path / "airports.csv"
    csv_file.write_text(
        "OriginAirportID,OriginAirportName\n"
        "14747,Seattle/Tacoma International\n"
        "15304,Tampa International\n"
        "13931,Norfolk International\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(app_module, "BASE_DIR", tmp_path)

    app_module.app.config.update(TESTING=True)
    return app_module.app.test_client()


def test_predict_returns_certainty_and_delay(client):
    response = client.get("/predict?day_of_week=3&airport_id=14771")

    assert response.status_code == 200
    assert response.get_json() == {"certainty": 0.75, "delay": 0.25}


@pytest.mark.parametrize("query", [
    "",
    "?day_of_week=3",
    "?airport_id=14771",
    "?day_of_week=not-a-number&airport_id=14771",
    "?day_of_week=3&airport_id=not-a-number",
])
def test_predict_rejects_missing_or_malformed_parameters(client, query):
    response = client.get("/predict" + query)

    assert response.status_code == 400


def test_airports_are_sorted_by_name(client):
    response = client.get("/airports")

    assert response.status_code == 200
    assert response.get_json() == [
        {"id": 13931, "name": "Norfolk International"},
        {"id": 14747, "name": "Seattle/Tacoma International"},
        {"id": 15304, "name": "Tampa International"},
    ]


def test_api_responses_allow_cross_origin_requests(client):
    response = client.get("/predict?day_of_week=3&airport_id=14771")

    assert response.headers["Access-Control-Allow-Origin"] == "*"
    assert "Content-Type" in response.headers["Access-Control-Allow-Headers"]
