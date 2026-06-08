import logging
from typing import Any, Dict
from models.schemas import ResourceCard
from services.dry_run import DryRunPlan

logger = logging.getLogger(__name__)


class ExecutorError(Exception):
    pass


def _exec_vcd(plan: DryRunPlan, vcd_client=None) -> Dict[str, Any]:
    from connectors.vcd_client import vcd_client as _default, VCDClientError
    vcd_client = vcd_client or _default

    r = plan.resolved
    op = plan.operation

    try:
        if op == "create_network":
            vdc_id = r.get("vdc_id") or vcd_client.get_vdc_id(r["vdc"])
            raw_type = r.get("network_type", "routed").upper()
            network_type = "NAT_ROUTED" if raw_type in ("ROUTED", "NAT_ROUTED") else raw_type
            payload = {
                "name": r.get("name"),
                "networkType": network_type,
                "ownerRef": {"id": vdc_id},
                "subnets": {
                    "values": [{
                        "gateway": r.get("gateway", "192.168.1.1"),
                        "prefixLength": int(r.get("prefix_length", 24)),
                        "dnsSuffix": r.get("dns_suffix", ""),
                        "dnsServer1": r.get("dns1", ""),
                        "dnsServer2": r.get("dns2", ""),
                        "ipRanges": {"values": []},
                    }]
                },
            }
            resp = vcd_client._post(vcd_client._cloudapi("orgVdcNetworks"), payload)
            return {"vcd_id": resp.get("id"), "name": resp.get("name")}

        elif op == "create_ip_set":
            vdc_group_id = r.get("vdc_group_id")
            if not vdc_group_id:
                raise ExecutorError("vdc_group_id is required for create_ip_set")
            payload = {
                "name": r.get("name"),
                "ipAddresses": r.get("ip_addresses", []),
            }
            resp = vcd_client._post(
                vcd_client._cloudapi(f"vdcGroups/{vdc_group_id}/ipSets"), payload
            )
            return {"vcd_id": resp.get("id")}

        elif op == "create_security_group":
            vdc_group_id = r.get("vdc_group_id")
            if not vdc_group_id:
                raise ExecutorError("vdc_group_id is required for create_security_group")
            payload = {"name": r.get("name"), "members": []}
            resp = vcd_client._post(
                vcd_client._cloudapi(f"vdcGroups/{vdc_group_id}/securityGroups"), payload
            )
            return {"vcd_id": resp.get("id")}

        elif op == "create_nat_rule":
            edge_gateway_id = r.get("edge_gateway_id")
            if not edge_gateway_id:
                raise ExecutorError("edge_gateway_id is required for create_nat_rule")
            payload = {
                "name": r.get("rule_name") or r.get("name"),
                "type": r.get("type", "DNAT").upper(),
                "externalAddresses": r.get("external_ip", ""),
                "internalAddresses": r.get("internal_ip", ""),
                "enabled": True,
                "logging": False,
            }
            resp = vcd_client._post(
                vcd_client._cloudapi(f"edgeGateways/{edge_gateway_id}/nat/rules"), payload
            )
            return {"vcd_id": resp.get("id")}

        elif op == "edit_vm":
            vdc_id = r.get("vdc_id") or vcd_client.get_vdc_id(r["vdc"])
            vm_name = r.get("name") or r.get("vm_name")
            if not vm_name:
                raise ExecutorError("VM name is required for edit_vm")
            cpu = r.get("cpu")
            memory_mb = r.get("memory_mb")
            if cpu is None and memory_mb is None:
                raise ExecutorError("At least one of cpu or memory_mb must be specified for edit_vm")
            vm_id = vcd_client.get_vm_id(vdc_id, vm_name)
            vcd_client.update_vm_compute(
                vm_id=vm_id,
                cpu=int(cpu) if cpu is not None else None,
                memory_mb=int(memory_mb) if memory_mb is not None else None,
            )
            return {"vm_id": vm_id, "vm_name": vm_name, "status": "UPDATED"}

        elif op == "create_edge_fw_rule":
            edge_gateway_id = r.get("edge_gateway_id")
            if not edge_gateway_id:
                raise ExecutorError("edge_gateway_id is required for create_edge_fw_rule")
            payload = {
                "userDefinedRules": [{
                    "name": r.get("rule_name") or r.get("name"),
                    "action": {"type": r.get("action", "ALLOW").upper()},
                    "enabled": True,
                    "logging": False,
                    "sourceFirewallGroups": [],
                    "destinationFirewallGroups": [],
                    "applicationPortProfiles": [],
                }]
            }
            vcd_client._post(
                vcd_client._cloudapi(f"edgeGateways/{edge_gateway_id}/firewall/rules"), payload
            )
            return {"result": "created"}

        else:
            raise ExecutorError(f"VCD operation '{op}' executor not yet implemented.")

    except VCDClientError as exc:
        raise ExecutorError(str(exc))


def execute(plan: DryRunPlan, vcd_client=None) -> ResourceCard:
    logger.info("Executing plan %s: %s/%s", plan.plan_id, plan.platform, plan.operation)
    try:
        if plan.platform == "VCD":
            result = _exec_vcd(plan, vcd_client=vcd_client)
        else:
            raise ExecutorError(f"Unknown platform: {plan.platform}")

        display_params = dict(plan.resolved)
        display_params.update(result)

        return ResourceCard(
            platform=plan.platform,
            resource_type=plan.resource_type,
            name=plan.name,
            parameters=display_params,
            status="COMPLETE",
            plan_id=plan.plan_id,
        )

    except ExecutorError:
        raise
    except Exception as exc:
        raise ExecutorError(f"Unexpected error during execution: {exc}")
