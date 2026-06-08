import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from services.intent_parser import ParsedIntent

logger = logging.getLogger(__name__)

PLAN_TTL = 3600  # plans expire after 1 hour

VCD_RESOURCE_TYPES: Dict[str, str] = {
    "edit_vm": "Virtual Machine",
    "create_network": "Org VDC Network",
    "create_edge_fw_rule": "Edge Firewall Rule",
    "create_nat_rule": "NAT Rule",
    "create_security_group": "Security Group",
    "create_ip_set": "IP Set",
}


@dataclass
class DryRunPlan:
    plan_id: str
    platform: str
    operation: str
    resource_type: str
    name: str
    parameters: Dict[str, Any]
    resolved: Dict[str, Any]
    warnings: List[str] = field(default_factory=list)


_plan_store: Dict[str, DryRunPlan] = {}
_plan_times: Dict[str, float] = {}


def get_plan(plan_id: str) -> Optional[DryRunPlan]:
    plan = _plan_store.get(plan_id)
    if plan and (time.time() - _plan_times.get(plan_id, 0)) > PLAN_TTL:
        _plan_store.pop(plan_id, None)
        _plan_times.pop(plan_id, None)
        return None
    return plan


def _evict_expired_plans() -> None:
    now = time.time()
    expired = [pid for pid, t in _plan_times.items() if now - t > PLAN_TTL]
    for pid in expired:
        _plan_store.pop(pid, None)
        _plan_times.pop(pid, None)
    if expired:
        logger.info("Evicted %d expired plan(s)", len(expired))


def _resolve_vcd(params: Dict[str, Any], warnings: List[str], vcd_client=None) -> Dict[str, Any]:
    resolved = dict(params)
    try:
        from connectors.vcd_client import vcd_client as _default, VCDClientError
        client = vcd_client or _default

        vdc_name = params.get("vdc")
        if vdc_name:
            try:
                vdc_id = client.get_vdc_id(vdc_name)
                resolved["vdc_id"] = vdc_id
                resolved["vdc"] = vdc_name
            except VCDClientError as exc:
                warnings.append(f"Could not resolve VDC '{vdc_name}': {exc}")

    except Exception as exc:
        warnings.append(f"VCD connector not available — parameters unverified. ({exc})")

    return resolved


def _apply_vcd_defaults(operation: str, resolved: Dict[str, Any]) -> Dict[str, Any]:
    if operation == "create_network":
        resolved.setdefault("network_type", "routed")
        resolved.setdefault("prefix_length", 24)
    elif operation == "create_dfw_rule":
        resolved.setdefault("action", "allow")
        resolved.setdefault("direction", "IN_OUT")
    return resolved


class DryRunError(Exception):
    pass


def build_plan(intent: ParsedIntent, vcd_client=None) -> DryRunPlan:
    warnings: List[str] = []
    params = dict(intent.parameters)

    if intent.platform == "VCD":
        resolved = _resolve_vcd(params, warnings, vcd_client=vcd_client)
        resolved = _apply_vcd_defaults(intent.operation, resolved)
        resource_type = VCD_RESOURCE_TYPES.get(intent.operation, intent.operation)
    else:
        raise DryRunError(f"Unknown platform: {intent.platform}")

    name = (
        params.get("name")
        or params.get("rule_name")
        or f"{intent.operation}-{str(uuid.uuid4())[:8]}"
    )

    plan = DryRunPlan(
        plan_id=str(uuid.uuid4()),
        platform=intent.platform,
        operation=intent.operation,
        resource_type=resource_type,
        name=name,
        parameters=params,
        resolved=resolved,
        warnings=warnings,
    )

    _evict_expired_plans()
    _plan_store[plan.plan_id] = plan
    _plan_times[plan.plan_id] = time.time()
    logger.info("Plan created: %s %s/%s", plan.plan_id, plan.platform, plan.operation)
    return plan
