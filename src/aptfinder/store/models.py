"""Shapes of log rows and of observations.data."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

PriceBasis = Literal["month", "night", "week", "stay"]
Verdict = Literal["correct", "wrong", "cannot_tell"]


class ObservationData(BaseModel):
    """Everything parsed from one listing in one run. All fields optional: unknown stays None."""

    model_config = ConfigDict(extra="forbid")

    address: str | None = None
    unit: str | None = None
    city: str | None = None
    zip: str | None = None
    lat: float | None = None
    lon: float | None = None
    beds: float | None = None
    baths: float | None = None
    sqft: float | None = None
    kind: Literal["apartment", "house", "townhome", "room", "entire_place", "site_info"] | None = None
    term: Literal["long", "short"] | None = None
    furnished: bool | None = None
    private_bath: bool | None = None
    available_from: str | None = None
    available_to: str | None = None
    amenities: list[str] | None = None
    floor_plan: str | None = None
    property_name: str | None = None
    operator: str | None = None
    title: str | None = None
    description: str | None = None
    media: list[str] | None = None
    extra: dict[str, Any] | None = None


# Fields that a check can verify and override, and the SQL expression reading each from latest_obs.
CHECKABLE_FIELDS = [
    "price", "beds", "baths", "sqft", "available_from", "available_to", "address", "unit",
    "lat", "lon", "furnished", "private_bath", "term", "kind",
]
