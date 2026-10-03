from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import current_principal
from app.core.security import Principal
from app.reschedule.schemas import ConfirmationCreate, OrderCreate, RehearsalCreate
from app.reschedule.service import RescheduleService

router = APIRouter(prefix="/api/reschedule", tags=["批量改期预演"])


def service() -> RescheduleService:
    return RescheduleService()


@router.post("/orders", status_code=201)
def create_order(payload: OrderCreate, principal: Principal = Depends(current_principal)):
    return service().create_order(payload.model_dump(), principal)


@router.get("/orders")
def list_orders(
    ceremony_date: date | None = None,
    status: str | None = None,
    principal: Principal = Depends(current_principal),
):
    principal.require("reschedule.read")
    return {"items": service().list_orders(date=ceremony_date.isoformat() if ceremony_date else None, status=status)}


@router.post("/rehearsals", status_code=201)
def create_rehearsal(payload: RehearsalCreate, principal: Principal = Depends(current_principal)):
    return service().create_rehearsal(payload.model_dump(), principal)


@router.get("/rehearsals")
def list_rehearsals(
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(current_principal),
):
    principal.require("reschedule.read")
    return {"items": service().list_rehearsals(status=status, limit=limit)}


@router.get("/rehearsals/{rehearsal_id}")
def get_rehearsal(rehearsal_id: int, principal: Principal = Depends(current_principal)):
    principal.require("reschedule.read")
    return service().get_rehearsal(rehearsal_id)


@router.get("/rehearsals/{rehearsal_id}/report")
def get_report(rehearsal_id: int, principal: Principal = Depends(current_principal)):
    principal.require("reschedule.read")
    return service().get_report(rehearsal_id)


@router.get("/rehearsals/{rehearsal_id}/confirmations")
def list_confirmations(rehearsal_id: int, principal: Principal = Depends(current_principal)):
    principal.require("reschedule.read")
    return {"items": service().list_confirmations(rehearsal_id)}


@router.post("/rehearsals/{rehearsal_id}/confirmations", status_code=201)
def confirm(rehearsal_id: int, payload: ConfirmationCreate, principal: Principal = Depends(current_principal)):
    return service().confirm(rehearsal_id, principal, payload.group, payload.report_version, payload.note)


@router.get("/rehearsals/{rehearsal_id}/events")
def list_events(rehearsal_id: int, principal: Principal = Depends(current_principal)):
    principal.require("reschedule.read")
    return {"items": service().list_events(rehearsal_id)}


@router.get("/rehearsals/{rehearsal_id}/result")
def get_result(rehearsal_id: int, principal: Principal = Depends(current_principal)):
    principal.require("reschedule.read")
    return service().get_result(rehearsal_id)
