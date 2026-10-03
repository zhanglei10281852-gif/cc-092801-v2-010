from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class ResourceCreate(BaseModel):
    resource_type: Literal["staff", "vehicle", "supplier"]
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    name: str = Field(min_length=1, max_length=120)
    daily_capacity: int = Field(default=1, ge=1, le=100000)
    change_fee_cents: int = Field(default=0, ge=0, le=10**9)


class BookingCreate(BaseModel):
    task_id: int = Field(ge=1)
    resource_code: str = Field(min_length=2, max_length=64)
    booking_date: date | None = None
    units: int = Field(default=1, ge=1, le=10000)
    commitment: Literal["tentative", "confirmed"] = "confirmed"
    fee_cents: int = Field(default=0, ge=0, le=10**12)


class PlanCreate(BaseModel):
    source_date: date
    candidate_start: date
    candidate_end: date
    reason: str = Field(min_length=2, max_length=500)
    report_ttl_minutes: int = Field(default=30, ge=1, le=1440)
    idempotency_key: str = Field(min_length=6, max_length=160)


class ConfirmRequest(BaseModel):
    domain: Literal["operations", "finance"]
    report_version: int = Field(ge=1)
    note: str = Field(default="", max_length=500)


class CancelPlanRequest(BaseModel):
    reason: str = Field(min_length=2, max_length=500)
