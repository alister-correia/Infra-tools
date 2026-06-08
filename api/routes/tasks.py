"""
Direct VCD task router — no Anthropic API, just VCD API calls.
Credentials are read from request headers on every call.
"""
from typing import Annotated

from fastapi import APIRouter, HTTPException, Header, Query

from connectors.vcd_client import make_vcd_client, VCDClientError

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


# ---------------------------------------------------------------------------
# Credential helpers
# ---------------------------------------------------------------------------

def _client(
    username: str,
    password: str,
    org: str = "",
):
    if not username or not password:
        raise HTTPException(status_code=401, detail="VCD credentials required")
    return make_vcd_client(username, password, org=org)


def _require_vdc(vdc_id: str) -> None:
    if not vdc_id:
        raise HTTPException(
            status_code=400,
            detail="No VDC selected — pick one from the header selector",
        )


# ---------------------------------------------------------------------------
# VMs
# ---------------------------------------------------------------------------

@router.get("/vms")
def list_vms(
    power_state: Annotated[str, Query()] = "",
    x_vcd_username: Annotated[str, Header()] = "",
    x_vcd_password: Annotated[str, Header()] = "",
    x_vcd_org:      Annotated[str, Header()] = "",
    x_vcd_vdc_id:   Annotated[str, Header()] = "",
    x_vcd_vdc_name: Annotated[str, Header()] = "",
):
    _require_vdc(x_vcd_vdc_id)
    try:
        vms = _client(x_vcd_username, x_vcd_password, x_vcd_org).list_vms(x_vcd_vdc_id, x_vcd_vdc_name)
        if power_state:
            vms = [v for v in vms if (v.get("status") or "").lower() == power_state.lower()]
        return vms
    except VCDClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/vm")
def get_vm(
    name: Annotated[str, Query()],
    x_vcd_username: Annotated[str, Header()] = "",
    x_vcd_password: Annotated[str, Header()] = "",
    x_vcd_org:      Annotated[str, Header()] = "",
    x_vcd_vdc_id:   Annotated[str, Header()] = "",
    x_vcd_vdc_name: Annotated[str, Header()] = "",
):
    _require_vdc(x_vcd_vdc_id)
    try:
        return _client(x_vcd_username, x_vcd_password, x_vcd_org).get_vm_details(x_vcd_vdc_id, name, x_vcd_vdc_name)
    except VCDClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/vm/power")
def power_vm(
    name:   Annotated[str, Query()],
    action: Annotated[str, Query()],
    x_vcd_username: Annotated[str, Header()] = "",
    x_vcd_password: Annotated[str, Header()] = "",
    x_vcd_org:      Annotated[str, Header()] = "",
    x_vcd_vdc_id:   Annotated[str, Header()] = "",
    x_vcd_vdc_name: Annotated[str, Header()] = "",
):
    _require_vdc(x_vcd_vdc_id)
    if action not in ("poweron", "poweroff", "reset"):
        raise HTTPException(status_code=400, detail="action must be one of: poweron, poweroff, reset")
    try:
        client = _client(x_vcd_username, x_vcd_password, x_vcd_org)
        vm_id = client.get_vm_id(x_vcd_vdc_id, name, x_vcd_vdc_name)
        return client.power_vm(vm_id, action)
    except VCDClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


# ---------------------------------------------------------------------------
# vApps
# ---------------------------------------------------------------------------

@router.get("/vapps")
def list_vapps(
    x_vcd_username: Annotated[str, Header()] = "",
    x_vcd_password: Annotated[str, Header()] = "",
    x_vcd_org:      Annotated[str, Header()] = "",
    x_vcd_vdc_id:   Annotated[str, Header()] = "",
    x_vcd_vdc_name: Annotated[str, Header()] = "",
):
    _require_vdc(x_vcd_vdc_id)
    try:
        return _client(x_vcd_username, x_vcd_password, x_vcd_org).list_vapps(x_vcd_vdc_id, x_vcd_vdc_name)
    except VCDClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/vapp")
def get_vapp(
    name: Annotated[str, Query()],
    x_vcd_username: Annotated[str, Header()] = "",
    x_vcd_password: Annotated[str, Header()] = "",
    x_vcd_org:      Annotated[str, Header()] = "",
    x_vcd_vdc_id:   Annotated[str, Header()] = "",
    x_vcd_vdc_name: Annotated[str, Header()] = "",
):
    _require_vdc(x_vcd_vdc_id)
    try:
        return _client(x_vcd_username, x_vcd_password, x_vcd_org).get_vapp_details(x_vcd_vdc_id, name, x_vcd_vdc_name)
    except VCDClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/vapp/power")
def power_vapp(
    name:   Annotated[str, Query()],
    action: Annotated[str, Query()],
    x_vcd_username: Annotated[str, Header()] = "",
    x_vcd_password: Annotated[str, Header()] = "",
    x_vcd_org:      Annotated[str, Header()] = "",
    x_vcd_vdc_id:   Annotated[str, Header()] = "",
    x_vcd_vdc_name: Annotated[str, Header()] = "",
):
    _require_vdc(x_vcd_vdc_id)
    if action not in ("poweron", "poweroff"):
        raise HTTPException(status_code=400, detail="action must be one of: poweron, poweroff")
    try:
        client = _client(x_vcd_username, x_vcd_password, x_vcd_org)
        vapps = client.list_vapps(x_vcd_vdc_id, x_vcd_vdc_name)
        vapp = next((v for v in vapps if v["name"].lower() == name.lower()), None)
        if not vapp:
            available = [v["name"] for v in vapps]
            raise VCDClientError(f"vApp '{name}' not found. Available: {available}")
        return client.power_vapp(vapp["id"], action)
    except VCDClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


# ---------------------------------------------------------------------------
# Networks
# ---------------------------------------------------------------------------

@router.get("/networks")
def list_networks(
    x_vcd_username: Annotated[str, Header()] = "",
    x_vcd_password: Annotated[str, Header()] = "",
    x_vcd_org:      Annotated[str, Header()] = "",
    x_vcd_vdc_id:   Annotated[str, Header()] = "",
):
    _require_vdc(x_vcd_vdc_id)
    try:
        return _client(x_vcd_username, x_vcd_password, x_vcd_org).list_networks(x_vcd_vdc_id)
    except VCDClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/network")
def get_network(
    name: Annotated[str, Query()],
    x_vcd_username: Annotated[str, Header()] = "",
    x_vcd_password: Annotated[str, Header()] = "",
    x_vcd_org:      Annotated[str, Header()] = "",
    x_vcd_vdc_id:   Annotated[str, Header()] = "",
):
    _require_vdc(x_vcd_vdc_id)
    try:
        return _client(x_vcd_username, x_vcd_password, x_vcd_org).get_network_details(name, x_vcd_vdc_id)
    except VCDClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------

@router.get("/edge-gateways")
def list_edge_gateways(
    x_vcd_username: Annotated[str, Header()] = "",
    x_vcd_password: Annotated[str, Header()] = "",
    x_vcd_org:      Annotated[str, Header()] = "",
    x_vcd_vdc_id:   Annotated[str, Header()] = "",
):
    try:
        return _client(x_vcd_username, x_vcd_password, x_vcd_org).list_edge_gateways(
            x_vcd_vdc_id or None
        )
    except VCDClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/app-port-profiles")
def list_app_port_profiles(
    gateway_id: Annotated[str, Query()],
    x_vcd_username: Annotated[str, Header()] = "",
    x_vcd_password: Annotated[str, Header()] = "",
    x_vcd_org:      Annotated[str, Header()] = "",
):
    try:
        return _client(x_vcd_username, x_vcd_password, x_vcd_org).list_app_port_profiles(gateway_id)
    except VCDClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/firewall-rules")
def list_firewall_rules(
    gateway_id: Annotated[str, Query()],
    x_vcd_username: Annotated[str, Header()] = "",
    x_vcd_password: Annotated[str, Header()] = "",
    x_vcd_org:      Annotated[str, Header()] = "",
    x_vcd_vdc_id:   Annotated[str, Header()] = "",
):
    try:
        return _client(x_vcd_username, x_vcd_password, x_vcd_org).list_firewall_rules(gateway_id)
    except VCDClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/nat-rules")
def list_nat_rules(
    gateway_id: Annotated[str, Query()],
    x_vcd_username: Annotated[str, Header()] = "",
    x_vcd_password: Annotated[str, Header()] = "",
    x_vcd_org:      Annotated[str, Header()] = "",
    x_vcd_vdc_id:   Annotated[str, Header()] = "",
):
    try:
        return _client(x_vcd_username, x_vcd_password, x_vcd_org).list_nat_rules(gateway_id)
    except VCDClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/firewall-rules")
def create_firewall_rule(
    gateway_id:           Annotated[str, Query()],
    rule_name:            Annotated[str, Query()],
    action:               Annotated[str, Query()],
    source_ips:           Annotated[str, Query()] = "",
    dest_ips:             Annotated[str, Query()] = "",
    protocol:             Annotated[str, Query()] = "",
    dest_ports:           Annotated[str, Query()] = "",
    app_port_profile_id:  Annotated[str, Query()] = "",
    enabled:              Annotated[bool, Query()] = True,
    position:             Annotated[int, Query()] = 0,
    x_vcd_username: Annotated[str, Header()] = "",
    x_vcd_password: Annotated[str, Header()] = "",
    x_vcd_org:      Annotated[str, Header()] = "",
):
    if action.upper() not in ("ALLOW", "DROP"):
        raise HTTPException(status_code=400, detail="action must be ALLOW or DROP")
    def _clean(v: str) -> str:
        v = v.strip()
        return "" if v.upper() in ("ANY", "UNDEFINED", "") else v
    src = _clean(source_ips)
    dst = _clean(dest_ips)
    try:
        clean_profile = "" if app_port_profile_id.strip().lower() in ("", "undefined", "none") else app_port_profile_id.strip()
        return _client(x_vcd_username, x_vcd_password, x_vcd_org).create_firewall_rule(
            gateway_id, rule_name, action, src, dst, protocol, dest_ports, enabled, clean_profile, position
        )
    except VCDClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc))



@router.post("/nat-rules")
def create_nat_rule(
    gateway_id:  Annotated[str, Query()],
    rule_name:   Annotated[str, Query()],
    rule_type:   Annotated[str, Query()],
    external_ip: Annotated[str, Query()],
    internal_ip: Annotated[str, Query()],
    enabled:     Annotated[bool, Query()] = True,
    x_vcd_username: Annotated[str, Header()] = "",
    x_vcd_password: Annotated[str, Header()] = "",
    x_vcd_org:      Annotated[str, Header()] = "",
):
    if rule_type.upper() not in ("DNAT", "SNAT"):
        raise HTTPException(status_code=400, detail="rule_type must be DNAT or SNAT")
    try:
        return _client(x_vcd_username, x_vcd_password, x_vcd_org).create_nat_rule(
            gateway_id, rule_name, rule_type, external_ip, internal_ip, enabled
        )
    except VCDClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------

@router.get("/vdc-groups")
def list_vdc_groups(
    x_vcd_username: Annotated[str, Header()] = "",
    x_vcd_password: Annotated[str, Header()] = "",
    x_vcd_org:      Annotated[str, Header()] = "",
    x_vcd_org_id:   Annotated[str, Header()] = "",
    x_vcd_vdc_id:   Annotated[str, Header()] = "",
):
    try:
        return _client(x_vcd_username, x_vcd_password, x_vcd_org).list_vdc_groups(x_vcd_org_id)
    except VCDClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/ip-sets")
def list_ip_sets(
    group_id: Annotated[str, Query()],
    x_vcd_username: Annotated[str, Header()] = "",
    x_vcd_password: Annotated[str, Header()] = "",
    x_vcd_org:      Annotated[str, Header()] = "",
    x_vcd_vdc_id:   Annotated[str, Header()] = "",
):
    try:
        return _client(x_vcd_username, x_vcd_password, x_vcd_org).list_ip_sets(group_id)
    except VCDClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/security-groups")
def list_security_groups(
    group_id: Annotated[str, Query()],
    x_vcd_username: Annotated[str, Header()] = "",
    x_vcd_password: Annotated[str, Header()] = "",
    x_vcd_org:      Annotated[str, Header()] = "",
    x_vcd_vdc_id:   Annotated[str, Header()] = "",
):
    try:
        return _client(x_vcd_username, x_vcd_password, x_vcd_org).list_security_groups(group_id)
    except VCDClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/gateway-ip-sets")
def list_gateway_ip_sets(
    gateway_id: Annotated[str, Query()],
    x_vcd_username: Annotated[str, Header()] = "",
    x_vcd_password: Annotated[str, Header()] = "",
    x_vcd_org:      Annotated[str, Header()] = "",
    x_vcd_vdc_id:   Annotated[str, Header()] = "",
):
    try:
        return _client(x_vcd_username, x_vcd_password, x_vcd_org).list_ip_sets_for_gateway(gateway_id)
    except VCDClientError as exc:
        msg = str(exc)
        status = 502
        raise HTTPException(status_code=status, detail=msg)


@router.get("/dfw-rules")
def list_dfw_rules(
    group_id: Annotated[str, Query()],
    x_vcd_username: Annotated[str, Header()] = "",
    x_vcd_password: Annotated[str, Header()] = "",
    x_vcd_org:      Annotated[str, Header()] = "",
    x_vcd_vdc_id:   Annotated[str, Header()] = "",
):
    try:
        return _client(x_vcd_username, x_vcd_password, x_vcd_org).list_dfw_rules(group_id)
    except VCDClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


# ---------------------------------------------------------------------------
# VM compute / snapshots / deploy
# ---------------------------------------------------------------------------

@router.post("/vm/compute")
def edit_vm_compute(
    vm_name: Annotated[str, Query()],
    cpu: Annotated[int, Query()] = 0,
    memory_gb: Annotated[int, Query()] = 0,
    x_vcd_username: Annotated[str, Header()] = "",
    x_vcd_password: Annotated[str, Header()] = "",
    x_vcd_org: Annotated[str, Header()] = "",
    x_vcd_vdc_id: Annotated[str, Header()] = "",
    x_vcd_vdc_name: Annotated[str, Header()] = "",
):
    _require_vdc(x_vcd_vdc_id)
    if not cpu and not memory_gb:
        raise HTTPException(status_code=400, detail="Provide cpu and/or memory_gb")
    try:
        return _client(x_vcd_username, x_vcd_password, x_vcd_org).edit_vm(
            x_vcd_vdc_id, vm_name,
            cpu=cpu if cpu else None,
            memory_mb=memory_gb * 1024 if memory_gb else None,
            vdc_name=x_vcd_vdc_name,
        )
    except VCDClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/vm/disks")
def list_vm_disks(
    vm_name: Annotated[str, Query()],
    x_vcd_username: Annotated[str, Header()] = "",
    x_vcd_password: Annotated[str, Header()] = "",
    x_vcd_org: Annotated[str, Header()] = "",
    x_vcd_vdc_id: Annotated[str, Header()] = "",
    x_vcd_vdc_name: Annotated[str, Header()] = "",
):
    _require_vdc(x_vcd_vdc_id)
    try:
        details = _client(x_vcd_username, x_vcd_password, x_vcd_org).get_vm_details(
            x_vcd_vdc_id, vm_name, x_vcd_vdc_name
        )
        return details.get("disks", [])
    except VCDClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.put("/vm/disk")
def resize_vm_disk(
    vm_name:    Annotated[str, Query()],
    disk_index: Annotated[int, Query()] = 1,
    new_size_gb:Annotated[int, Query()] = 0,
    x_vcd_username: Annotated[str, Header()] = "",
    x_vcd_password: Annotated[str, Header()] = "",
    x_vcd_org:      Annotated[str, Header()] = "",
    x_vcd_vdc_id:   Annotated[str, Header()] = "",
    x_vcd_vdc_name: Annotated[str, Header()] = "",
):
    _require_vdc(x_vcd_vdc_id)
    if not new_size_gb:
        raise HTTPException(status_code=400, detail="new_size_gb is required")
    try:
        return _client(x_vcd_username, x_vcd_password, x_vcd_org).resize_vm_disk(
            x_vcd_vdc_id, vm_name, disk_index, new_size_gb, x_vcd_vdc_name
        )
    except VCDClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/networks")
def create_network(
    name: Annotated[str, Query()],
    network_type: Annotated[str, Query()] = "ISOLATED",
    gateway: Annotated[str, Query()] = "",
    prefix_length: Annotated[int, Query()] = 24,
    dns1: Annotated[str, Query()] = "",
    dns2: Annotated[str, Query()] = "",
    dhcp_start: Annotated[str, Query()] = "",
    dhcp_end: Annotated[str, Query()] = "",
    edge_gateway_id: Annotated[str, Query()] = "",
    x_vcd_username: Annotated[str, Header()] = "",
    x_vcd_password: Annotated[str, Header()] = "",
    x_vcd_org: Annotated[str, Header()] = "",
    x_vcd_vdc_id: Annotated[str, Header()] = "",
):
    _require_vdc(x_vcd_vdc_id)
    if network_type.upper() not in ("ISOLATED", "NAT_ROUTED"):
        raise HTTPException(status_code=400, detail="network_type must be ISOLATED or NAT_ROUTED")
    try:
        return _client(x_vcd_username, x_vcd_password, x_vcd_org).create_network(
            x_vcd_vdc_id, name, network_type, gateway, prefix_length,
            dns1, dns2, dhcp_start, dhcp_end, edge_gateway_id,
        )
    except VCDClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.put("/network")
def edit_network(
    name: Annotated[str, Query()],
    gateway: Annotated[str, Query()] = "",
    prefix_length: Annotated[int, Query()] = 0,
    dns1: Annotated[str, Query()] = "",
    dns2: Annotated[str, Query()] = "",
    disconnect_edge: Annotated[bool, Query()] = False,
    connect_edge_id: Annotated[str, Query()] = "",
    x_vcd_username: Annotated[str, Header()] = "",
    x_vcd_password: Annotated[str, Header()] = "",
    x_vcd_org: Annotated[str, Header()] = "",
    x_vcd_vdc_id: Annotated[str, Header()] = "",
):
    _require_vdc(x_vcd_vdc_id)
    try:
        return _client(x_vcd_username, x_vcd_password, x_vcd_org).edit_network(
            name, x_vcd_vdc_id,
            gateway=gateway or None,
            prefix_length=prefix_length or None,
            dns1=dns1 or None,
            dns2=dns2 or None,
            disconnect_edge=disconnect_edge,
            connect_edge_id=connect_edge_id or None,
        )
    except VCDClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/vm/snapshots")
def list_vm_snapshots(
    vm_name: Annotated[str, Query()],
    x_vcd_username: Annotated[str, Header()] = "",
    x_vcd_password: Annotated[str, Header()] = "",
    x_vcd_org: Annotated[str, Header()] = "",
    x_vcd_vdc_id: Annotated[str, Header()] = "",
    x_vcd_vdc_name: Annotated[str, Header()] = "",
):
    _require_vdc(x_vcd_vdc_id)
    try:
        return _client(x_vcd_username, x_vcd_password, x_vcd_org).list_vm_snapshots(
            x_vcd_vdc_id, vm_name, x_vcd_vdc_name
        )
    except VCDClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/vm/snapshot")
def create_vm_snapshot(
    vm_name: Annotated[str, Query()],
    snapshot_name: Annotated[str, Query()] = "",
    x_vcd_username: Annotated[str, Header()] = "",
    x_vcd_password: Annotated[str, Header()] = "",
    x_vcd_org: Annotated[str, Header()] = "",
    x_vcd_vdc_id: Annotated[str, Header()] = "",
    x_vcd_vdc_name: Annotated[str, Header()] = "",
):
    _require_vdc(x_vcd_vdc_id)
    try:
        return _client(x_vcd_username, x_vcd_password, x_vcd_org).create_vm_snapshot(
            x_vcd_vdc_id, vm_name, snapshot_name, x_vcd_vdc_name
        )
    except VCDClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/vm/snapshot/revert")
def revert_vm_snapshot(
    vm_name: Annotated[str, Query()],
    x_vcd_username: Annotated[str, Header()] = "",
    x_vcd_password: Annotated[str, Header()] = "",
    x_vcd_org: Annotated[str, Header()] = "",
    x_vcd_vdc_id: Annotated[str, Header()] = "",
    x_vcd_vdc_name: Annotated[str, Header()] = "",
):
    _require_vdc(x_vcd_vdc_id)
    try:
        return _client(x_vcd_username, x_vcd_password, x_vcd_org).revert_vm_snapshot(
            x_vcd_vdc_id, vm_name, x_vcd_vdc_name
        )
    except VCDClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/catalogs")
def list_catalogs(
    x_vcd_username: Annotated[str, Header()] = "",
    x_vcd_password: Annotated[str, Header()] = "",
    x_vcd_org: Annotated[str, Header()] = "",
):
    try:
        client = _client(x_vcd_username, x_vcd_password, x_vcd_org)
        params = {"type": "adminCatalog", "format": "records", "pageSize": "128"}
        data = client._get_legacy(client._api("query"), params=params)
        records = data.get("record", []) if isinstance(data, dict) else []
        return [{"name": r.get("name"), "id": r.get("href", "").split("/")[-1]} for r in records if r.get("name")]
    except VCDClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/catalog-templates")
def list_catalog_templates(
    catalog_name: Annotated[str, Query()],
    x_vcd_username: Annotated[str, Header()] = "",
    x_vcd_password: Annotated[str, Header()] = "",
    x_vcd_org:      Annotated[str, Header()] = "",
):
    try:
        client = _client(x_vcd_username, x_vcd_password, x_vcd_org)
        params = {
            "type": "vAppTemplate", "format": "records", "pageSize": "128",
            "filter": f"catalogName=={catalog_name}",
        }
        data = client._get_legacy(client._api("query"), params=params)
        records = data.get("record", []) if isinstance(data, dict) else []
        return [{"name": r.get("name"), "href": r.get("href", ""),
                 "cpu": int(r.get("numCpus") or 0) or None,
                 "memory_mb": int(r.get("memoryMB") or 0) or None}
                for r in records if r.get("name")]
    except VCDClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
