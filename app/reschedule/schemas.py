from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, model_validator

MAX_WINDOW_DAYS = 14


class ResourceInput(BaseModel):
    resource_type: Literal["staff", "vehicle", "vendor"]
    resource_name: str = Field(min_length=1, max_length=120)
    commitment_fee_cents: int = Field(default=0, ge=0, le=10**12)


class OrderCreate(BaseModel):
    order_code: str = Field(min_length=2, max_length=40, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]+$")
    ceremony_type: Literal["wedding", "memorial"]
    ceremony_date: date
    venue: str = Field(min_length=1, max_length=120)
    customer_name: str = Field(min_length=1, max_length=80)
    base_fee_cents: int = Field(ge=0, le=10**12)
    resources: list[ResourceInput] = Field(default_factory=list, max_length=50)


class RehearsalCreate(BaseModel):
    source_date: date
    window_start: date
    window_end: date
    reason: str = Field(min_length=2, max_length=500)

    @model_validator(mode="after")
    def validate_window(self) -> "RehearsalCreate":
        if self.window_end < self.window_start:
            raise ValueError("候选日期范围的结束日期不能早于开始日期")
        if (self.window_end - self.window_start).days >= MAX_WINDOW_DAYS:
            raise ValueError(f"候选日期范围不能超过 {MAX_WINDOW_DAYS} 天")
        if self.window_start <= self.source_date <= self.window_end:
            raise ValueError("候选日期范围不能包含原定日期")
        return self


class ConfirmationCreate(BaseModel):
    group: Literal["operations", "finance"]
    report_version: int | None = Field(default=None, ge=1)
    note: str = Field(default="", max_length=500)
