from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import current_principal
from app.core.security import Principal
from app.reschedule.schemas import BookingCreate, CancelPlanRequest, ConfirmRequest, PlanCreate, ResourceCreate
from app.reschedule.service import RescheduleService

router = APIRouter(prefix="/api/reschedule", tags=["仪式订单批量改期"])


def service() -> RescheduleService:
    return RescheduleService()


def _payload_dates(payload: dict) -> dict:
    return {key: (value.isoformat() if hasattr(value, "isoformat") else value) for key, value in payload.items()}


@router.post("/resources", status_code=201)
def create_resource(payload: ResourceCreate, principal: Principal = Depends(current_principal)):
    principal.require("reschedule.write")
    return service().create_resource(payload.model_dump(), principal.username)


@router.get("/resources")
def list_resources(principal: Principal = Depends(current_principal)):
    principal.require("reschedule.read")
    return {"items": service().list_resources()}


@router.post("/bookings", status_code=201)
def create_booking(payload: BookingCreate, principal: Principal = Depends(current_principal)):
    principal.require("reschedule.write")
    return service().create_booking(_payload_dates(payload.model_dump()), principal.username)


@router.get("/bookings")
def list_bookings(booking_date: str | None = None, task_id: int | None = None, principal: Principal = Depends(current_principal)):
    principal.require("reschedule.read")
    return {"items": service().list_bookings(booking_date=booking_date, task_id=task_id)}


@router.post("/plans", status_code=201)
def create_plan(payload: PlanCreate, principal: Principal = Depends(current_principal)):
    principal.require("reschedule.write")
    return service().create_plan(_payload_dates(payload.model_dump()), principal.username)


@router.get("/plans")
def list_plans(status: str | None = None, limit: int = Query(default=100, ge=1, le=500), principal: Principal = Depends(current_principal)):
    principal.require("reschedule.read")
    return {"items": service().list_plans(status=status, limit=limit)}


@router.get("/plans/{plan_id}")
def get_plan(plan_id: int, principal: Principal = Depends(current_principal)):
    principal.require("reschedule.read")
    return service().get_plan(plan_id)


@router.get("/plans/{plan_id}/report")
def get_report(plan_id: int, full: bool = Query(default=False), principal: Principal = Depends(current_principal)):
    principal.require("reschedule.read")
    return service().get_report(plan_id, full=full)


@router.post("/plans/{plan_id}/rehearse")
def rehearse(plan_id: int, principal: Principal = Depends(current_principal)):
    principal.require("reschedule.write")
    return service().rehearse(plan_id, principal.username)


@router.post("/plans/{plan_id}/confirm", status_code=201)
def confirm(plan_id: int, payload: ConfirmRequest, principal: Principal = Depends(current_principal)):
    return service().confirm(
        plan_id,
        domain=payload.domain,
        actor=principal.username,
        report_version=payload.report_version,
        note=payload.note,
        permissions=principal.permissions,
    )


@router.get("/plans/{plan_id}/confirmations")
def list_confirmations(plan_id: int, principal: Principal = Depends(current_principal)):
    principal.require("reschedule.read")
    return {"items": service().list_confirmations(plan_id)}


@router.post("/plans/{plan_id}/apply")
def apply(plan_id: int, principal: Principal = Depends(current_principal)):
    principal.require("reschedule.write")
    return service().apply(plan_id, principal.username)


@router.get("/plans/{plan_id}/final")
def final_version(plan_id: int, principal: Principal = Depends(current_principal)):
    principal.require("reschedule.read")
    return service().final_version(plan_id)


@router.post("/plans/{plan_id}/cancel")
def cancel(plan_id: int, payload: CancelPlanRequest, principal: Principal = Depends(current_principal)):
    principal.require("reschedule.write")
    return service().cancel(plan_id, principal.username, payload.reason)
