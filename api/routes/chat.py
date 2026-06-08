from typing import Annotated, Optional

from fastapi import APIRouter, HTTPException, Header

from models.schemas import ChatRequest, ChatResponse, ConfirmRequest, ResourceCard
from services.intent_parser import make_intent_parser, IntentParserError
from services.dry_run import build_plan, get_plan, DryRunError
from services.executor import execute, ExecutorError
from services.query_executor import execute_query, execute_disambiguation, QueryError

router = APIRouter(prefix="/api/chat", tags=["chat"])

# Ops where the only missing param is VDC — default_vdc_id resolves it
_VDC_SCOPED_OPS = {"list_vms", "list_vapps", "list_networks", "list_edge_gateways", "list_vdcs"}


def _plan_to_card(plan) -> ResourceCard:
    display_params = dict(plan.resolved)
    if plan.warnings:
        display_params["_warnings"] = plan.warnings
    return ResourceCard(
        platform=plan.platform,
        resource_type=plan.resource_type,
        name=plan.name,
        parameters=display_params,
        status="DRY_RUN",
        plan_id=plan.plan_id,
    )


def _require_vcd_client(username: str, password: str, org: str = ""):
    if not username or not password:
        return None
    from connectors.vcd_client import make_vcd_client
    return make_vcd_client(username, password, org=org)


@router.post("", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    x_anthropic_key: Annotated[str, Header()] = "",
    x_vcd_username: Annotated[str, Header()] = "",
    x_vcd_password: Annotated[str, Header()] = "",
    x_vcd_org: Annotated[str, Header()] = "",
    x_vcd_vdc_id: Annotated[str, Header()] = "",
) -> ChatResponse:
    if not x_anthropic_key:
        raise HTTPException(status_code=401, detail="Anthropic API key required (X-Anthropic-Key header)")

    parser = make_intent_parser(x_anthropic_key)

    try:
        intent = parser.parse(request.message, request.history)
    except IntentParserError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    vcd_client = _require_vcd_client(x_vcd_username, x_vcd_password, org=x_vcd_org)

    if intent.requires_clarification:
        # If it's a VDC-scoped query and we have a default VDC, proceed directly
        if (intent.is_query and intent.platform == "VCD"
                and x_vcd_vdc_id and intent.operation in _VDC_SCOPED_OPS):
            pass  # fall through to execute_query below
        elif intent.platform == "VCD" and vcd_client:
            try:
                result = execute_disambiguation(intent, vcd_client=vcd_client, default_vdc_id=x_vcd_vdc_id)
                return ChatResponse(message=result, card=None)
            except QueryError as exc:
                return ChatResponse(message=str(exc), card=None)
        else:
            return ChatResponse(
                message=intent.clarification_message or intent.display_message,
                card=None,
            )

    if intent.operation == "chat":
        return ChatResponse(message=intent.display_message, card=None)

    if intent.is_query:
        try:
            result = execute_query(intent, vcd_client=vcd_client, default_vdc_id=x_vcd_vdc_id)
        except QueryError as exc:
            return ChatResponse(message=str(exc), card=None)
        return ChatResponse(message=result, card=None)

    if intent.platform not in ("OCI", "VCD"):
        return ChatResponse(message=intent.display_message, card=None)

    try:
        plan = build_plan(intent, vcd_client=vcd_client)
    except DryRunError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    message = intent.display_message
    if plan.warnings:
        warning_text = "; ".join(plan.warnings)
        message = f"{message}\n\n⚠️ Warnings: {warning_text}"

    return ChatResponse(message=message, card=_plan_to_card(plan))


@router.post("/confirm", response_model=ResourceCard)
async def confirm(
    request: ConfirmRequest,
    x_vcd_username: Annotated[str, Header()] = "",
    x_vcd_password: Annotated[str, Header()] = "",
    x_vcd_org: Annotated[str, Header()] = "",
) -> ResourceCard:
    plan = get_plan(request.plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail=f"Plan '{request.plan_id}' not found")

    vcd_client = _require_vcd_client(x_vcd_username, x_vcd_password, org=x_vcd_org)

    try:
        return execute(plan, vcd_client=vcd_client)
    except ExecutorError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
