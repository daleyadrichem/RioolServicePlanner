# tests/test_geocoding.py

from types import SimpleNamespace

import pytest

import riool_service.geocode_service as geocoding


def test_coordinates_from_address_resolved(monkeypatch):
    def fake_geocode(address):
        assert address == "Main Street 123, Amsterdam, Netherlands"
        return SimpleNamespace(latitude="52.3676", longitude="4.9041")

    monkeypatch.setattr(geocoding, "_geocode", fake_geocode)

    result = geocoding.coordinates_from_address(
        street=" Main Street ",
        house_number=" 123 ",
        city=" Amsterdam ",
        country=" Netherlands ",
    )

    assert result == geocoding.AddressCoordinates(
        street="Main Street",
        house_number="123",
        city="Amsterdam",
        country="Netherlands",
        latitude=52.3676,
        longitude=4.9041,
        status="resolved",
    )


def test_coordinates_from_address_not_found(monkeypatch):
    def fake_geocode(address):
        assert address == "Unknown Street 999, Nowhere, Neverland"
        return None

    monkeypatch.setattr(geocoding, "_geocode", fake_geocode)

    result = geocoding.coordinates_from_address(
        street="Unknown Street",
        house_number="999",
        city="Nowhere",
        country="Neverland",
    )

    assert result == geocoding.AddressCoordinates(
        street="Unknown Street",
        house_number="999",
        city="Nowhere",
        country="Neverland",
        latitude=None,
        longitude=None,
        status="not_found",
    )


def test_address_from_coordinates_resolved_with_city(monkeypatch):
    def fake_reverse_geocode(**kwargs):
        assert kwargs == {
            "query": (52.3676, 4.9041),
            "exactly_one": True,
            "language": "en",
            "addressdetails": True,
        }

        return SimpleNamespace(
            raw={
                "address": {
                    "road": "Main Street",
                    "house_number": "123",
                    "city": "Amsterdam",
                    "country": "Netherlands",
                }
            }
        )

    monkeypatch.setattr(geocoding, "_reverse_geocode", fake_reverse_geocode)

    result = geocoding.address_from_coordinates(52.3676, 4.9041)

    assert result == geocoding.CoordinatesAddress(
        latitude=52.3676,
        longitude=4.9041,
        street="Main Street",
        house_number="123",
        city="Amsterdam",
        country="Netherlands",
        status="resolved",
    )


def test_address_from_coordinates_not_found(monkeypatch):
    def fake_reverse_geocode(**kwargs):
        assert kwargs["query"] == (0.0, 0.0)
        return None

    monkeypatch.setattr(geocoding, "_reverse_geocode", fake_reverse_geocode)

    result = geocoding.address_from_coordinates(0.0, 0.0)

    assert result == geocoding.CoordinatesAddress(
        latitude=0.0,
        longitude=0.0,
        street=None,
        house_number=None,
        city=None,
        country=None,
        status="not_found",
    )


@pytest.mark.parametrize(
    ("address", "expected_city"),
    [
        ({"city": "Amsterdam"}, "Amsterdam"),
        ({"town": "Hilversum"}, "Hilversum"),
        ({"village": "Giethoorn"}, "Giethoorn"),
        ({"municipality": "Rotterdam"}, "Rotterdam"),
        ({}, None),
    ],
)
def test_get_city_fallbacks(address, expected_city):
    assert geocoding._get_city(address) == expected_city


def test_get_city_priority():
    address = {
        "city": "Amsterdam",
        "town": "Hilversum",
        "village": "Giethoorn",
        "municipality": "Rotterdam",
    }

    assert geocoding._get_city(address) == "Amsterdam"


def test_format_address():
    result = geocoding._format_address(
        street="Main Street",
        house_number="123",
        city="Amsterdam",
        country="Netherlands",
    )

    assert result == "Main Street 123, Amsterdam, Netherlands"


def test_address_from_coordinates_handles_missing_address_fields(monkeypatch):
    def fake_reverse_geocode(**kwargs):
        return SimpleNamespace(raw={"address": {}})

    monkeypatch.setattr(geocoding, "_reverse_geocode", fake_reverse_geocode)

    result = geocoding.address_from_coordinates(52.3676, 4.9041)

    assert result == geocoding.CoordinatesAddress(
        latitude=52.3676,
        longitude=4.9041,
        street=None,
        house_number=None,
        city=None,
        country=None,
        status="resolved",
    )