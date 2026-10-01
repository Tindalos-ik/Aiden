"""用户售后与工单进度，以及员工处理 API。"""

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import current_actor, current_staff
from app.persistence.mysql.database import get_session_factory
from app.persistence.mysql.models import AfterSaleRequest, Order, OrderItem, Policy, Ticket
from app.persistence.mysql.queries import list_user_orders
from app.persistence.mysql.service_workflow import (
    change_request, change_ticket, preview_request, request_data, requests_data,
    submit_request, ticket_data, tickets_data,
)

user_router = APIRouter(prefix="/api/service", tags=["service"])
staff_router = APIRouter(prefix="/api/staff/service", tags=["staff-service"])


class SubmitRequest(BaseModel):
    orderNo: str = Field(min_length=1, max_length=64)
    orderItemId: str = Field(min_length=1, max_length=36)
    requestType: Literal["refund", "return", "exchange"]
    reason: str = Field(min_length=1, max_length=2000)
    policyReference: str = Field(min_length=1, max_length=255)
    policySnapshot: str = Field(pattern=r"^[0-9a-f]{64}$")
    submissionKey: str = Field(min_length=1, max_length=100)
    confirmed: bool
    sourceTicketNo: str | None = Field(default=None, max_length=64)


class Transition(BaseModel):
    status: str = Field(min_length=1, max_length=24)
    note: str = Field(default="", max_length=4000)
    afterSaleRequestId: str | None = Field(default=None, max_length=36)


def _error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=404 if isinstance(exc, LookupError) else 409, detail=str(exc))


@user_router.get("/orders")
def my_orders(actor: dict = Depends(current_actor)) -> list[dict[str, Any]]:
    with get_session_factory()() as session:
        return [{"orderNo": row.order_no, "status": row.status,
                 "items": [{"id": item.id, "name": item.product_name_snapshot,
                            "quantity": item.quantity} for item in row.items]}
                for row in list_user_orders(session, actor["id"], limit=100)]


@user_router.get("/orders/{order_no}/after-sale-preview")
def preview(order_no: str, requestType: Literal["refund", "return", "exchange"],
            actor: dict = Depends(current_actor)) -> dict[str, Any]:
    try:
        with get_session_factory()() as session:
            return preview_request(session, actor["id"], order_no, requestType)
    except (LookupError, ValueError) as exc:
        raise _error(exc) from exc


@user_router.post("/after-sales", status_code=201)
def submit(body: SubmitRequest, actor: dict = Depends(current_actor)) -> dict[str, Any]:
    try:
        with get_session_factory().begin() as session:
            row, repeated = submit_request(
                session, user_id=actor["id"], order_no=body.orderNo,
                order_item_id=body.orderItemId, request_type=body.requestType,
                reason=body.reason, policy_reference=body.policyReference,
                policy_snapshot_value=body.policySnapshot,
                submission_key=body.submissionKey, confirmed=body.confirmed,
                source_ticket_no=body.sourceTicketNo,
            )
            result = request_data(session, row)
            result["alreadyExists"] = repeated
            return result
    except (LookupError, ValueError) as exc:
        raise _error(exc) from exc


@user_router.get("/after-sales")
def my_requests(actor: dict = Depends(current_actor)) -> list[dict[str, Any]]:
    with get_session_factory()() as session:
        rows = session.execute(select(AfterSaleRequest, Order.order_no).join(
            Order, Order.id == AfterSaleRequest.order_id).where(
            AfterSaleRequest.user_id == actor["id"], Order.user_id == actor["id"]
        ).order_by(AfterSaleRequest.created_at.desc()).limit(100)).all()
        return requests_data(session, [row for row, _order_no in rows])


@user_router.get("/tickets")
def my_tickets(actor: dict = Depends(current_actor)) -> list[dict[str, Any]]:
    with get_session_factory()() as session:
        rows = list(session.scalars(select(Ticket).where(Ticket.user_id == actor["id"])
                                    .order_by(Ticket.created_at.desc()).limit(100)))
        return tickets_data(session, rows)


@user_router.post("/after-sales/{request_id}/cancel")
def cancel(request_id: str, actor: dict = Depends(current_actor)) -> dict[str, Any]:
    try:
        with get_session_factory().begin() as session:
            return request_data(session, change_request(
                session, request_id, actor["id"], "cancelled", user_cancel=True))
    except (LookupError, ValueError) as exc:
        raise _error(exc) from exc


@staff_router.get("/after-sales")
def staff_requests(_actor: dict = Depends(current_staff)) -> list[dict[str, Any]]:
    with get_session_factory()() as session:
        rows = session.execute(select(AfterSaleRequest, Order.order_no,
                                      OrderItem.product_name_snapshot, Policy).join(
            Order, Order.id == AfterSaleRequest.order_id).outerjoin(
            OrderItem, OrderItem.id == AfterSaleRequest.order_item_id).outerjoin(
            Policy, Policy.id == AfterSaleRequest.policy_reference)
        .order_by(AfterSaleRequest.created_at.desc()).limit(200)).all()
        result = []
        enriched = requests_data(session, [row for row, _order_no, _item_name, _policy in rows])
        for data, (_row, _order_no, item_name, policy) in zip(enriched, rows):
            data["itemName"] = item_name
            data["policyName"] = policy.name if policy else None
            data["policyEvidence"] = policy.content if policy else None
            result.append(data)
        return result


@staff_router.post("/after-sales/{request_id}/transition")
def staff_request_transition(request_id: str, body: Transition,
                             actor: dict = Depends(current_staff)) -> dict[str, Any]:
    try:
        with get_session_factory().begin() as session:
            return request_data(session, change_request(
                session, request_id, actor["id"], body.status, body.note))
    except (LookupError, ValueError) as exc:
        raise _error(exc) from exc


@staff_router.get("/tickets")
def staff_tickets(_actor: dict = Depends(current_staff)) -> list[dict[str, Any]]:
    with get_session_factory()() as session:
        rows = list(session.scalars(select(Ticket).order_by(Ticket.created_at.desc()).limit(200)))
        return tickets_data(session, rows)


@staff_router.post("/tickets/{ticket_id}/transition")
def staff_ticket_transition(ticket_id: str, body: Transition,
                            actor: dict = Depends(current_staff)) -> dict[str, Any]:
    try:
        with get_session_factory().begin() as session:
            return ticket_data(session, change_ticket(
                session, ticket_id, actor["id"], body.status, body.note, body.afterSaleRequestId))
    except (LookupError, ValueError) as exc:
        raise _error(exc) from exc
