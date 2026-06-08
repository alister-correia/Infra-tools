from typing import Optional

from connectors.vcd_client import VCDClient, VCDClientError


class QueryError(Exception):
    pass


def _numbered_list(header: str, items: list) -> str:
    lines = [header]
    for i, item in enumerate(items, 1):
        lines.append(f"{i}. {item}")
    lines.append("\nReply with a number to select.")
    return "\n".join(lines)


def execute_disambiguation(intent, vcd_client: Optional[VCDClient] = None, default_vdc_id: str = "") -> str:
    """
    Called when requires_clarification=True for a VCD query.
    Fetches available options and returns a numbered list so the user can pick.
    If only one option exists, auto-selects it and executes immediately.
    """
    if not vcd_client:
        raise QueryError("VCD credentials not provided — please sign in.")

    op = intent.operation
    params = intent.parameters

    try:
        # ── Edge gateway disambiguation ────────────────────────────────────
        if op in ("list_firewall_rules", "list_nat_rules",
                  "create_edge_fw_rule", "create_nat_rule"):
            vdc_id = default_vdc_id or None
            gateways = vcd_client.list_edge_gateways(vdc_id)
            if not gateways:
                return "No edge gateways found."
            if len(gateways) == 1:
                intent.parameters["edge_gateway"] = gateways[0]["name"]
                return execute_query(intent, vcd_client, default_vdc_id)
            return _numbered_list(
                f"Found {len(gateways)} edge gateways. Which one?",
                [f"{g['name']} — {g.get('status', '?')}" for g in gateways],
            )

        # ── VM disambiguation ──────────────────────────────────────────────
        if op in ("edit_vm", "get_vm"):
            vdc_id = default_vdc_id or None
            if not vdc_id:
                return intent.clarification_message or intent.display_message
            vms = vcd_client.list_vms(vdc_id)
            if not vms:
                return "No VMs found in the selected VDC."
            if len(vms) == 1:
                intent.parameters["name"] = vms[0]["name"]
                return execute_query(intent, vcd_client, default_vdc_id)
            return _numbered_list(
                f"Found {len(vms)} VMs. Which one?",
                [f"{v['name']} — {v.get('status', '?')}" for v in vms],
            )

        # ── VDC Group disambiguation ────────────────────────────────────────
        if op in ("list_security_groups", "list_ip_sets",
                  "create_security_group", "create_ip_set"):
            groups = vcd_client.list_vdc_groups()
            if not groups:
                return "No VDC Groups found."
            if len(groups) == 1:
                intent.parameters["vdc_group_id"] = groups[0]["id"]
                return execute_query(intent, vcd_client, default_vdc_id)
            return _numbered_list(
                f"Found {len(groups)} VDC Groups. Which one?",
                [f"{g['name']} — {g.get('status', '?')}" for g in groups],
            )

    except VCDClientError as exc:
        raise QueryError(str(exc))

    # Fallback: return the clarification message as-is
    return intent.clarification_message or intent.display_message


def execute_query(intent, vcd_client: Optional[VCDClient] = None, default_vdc_id: str = "") -> str:
    if intent.platform == "VCD":
        return _vcd_query(intent.operation, intent.parameters, vcd_client, default_vdc_id)
    return intent.display_message


# ── VCD dispatcher ─────────────────────────────────────────────────────────

def _vcd_query(op: str, params: dict, client: Optional[VCDClient], default_vdc_id: str = "") -> str:
    if not client:
        raise QueryError("VCD credentials not provided — please sign in.")
    try:
        if op == "list_vms":
            return _list_vms(client, params.get("vdc"), default_vdc_id)
        if op == "list_vapps":
            return _list_vapps(client, params.get("vdc"), default_vdc_id)
        if op == "list_networks":
            return _list_networks(client, params.get("vdc"), default_vdc_id)
        if op == "list_vdcs":
            return _list_vdcs(client)
        if op == "list_edge_gateways":
            return _list_edge_gateways(client, params.get("vdc"), default_vdc_id)
        if op == "list_firewall_rules":
            return _list_firewall_rules(client, params.get("edge_gateway"), default_vdc_id)
        if op == "list_nat_rules":
            return _list_nat_rules(client, params.get("edge_gateway"), default_vdc_id)
        if op == "list_security_groups":
            return _list_security_groups(client, params.get("vdc_group_id"))
        if op == "list_ip_sets":
            return _list_ip_sets(client, params.get("vdc_group_id"))
        if op == "get_vm":
            return _get_vm(client, params.get("name"), params.get("vdc"), default_vdc_id)
        if op == "get_network":
            return _get_network(client, params.get("name"), params.get("vdc"), default_vdc_id)
        raise QueryError(f"Query operation '{op}' is not yet implemented.")
    except VCDClientError as exc:
        raise QueryError(str(exc))


# ── Helpers ────────────────────────────────────────────────────────────────

def _fmt_vm(vm: dict) -> str:
    cpu = vm.get("cpu") or "?"
    mem = vm.get("memory_mb")
    mem_str = f"{mem // 1024} GB" if mem else "?"
    return f"  • {vm['name']} — {vm.get('status', '?')} | CPU: {cpu} | RAM: {mem_str}"


def _list_vms(client: VCDClient, vdc_name: Optional[str], default_vdc_id: str = "") -> str:
    want_all = vdc_name and vdc_name.lower() in ("all", "any")

    if not vdc_name and default_vdc_id:
        vms = client.list_vms(default_vdc_id)
        if not vms:
            return "No VMs found in the selected VDC."
        return f"Found {len(vms)} VM(s) in the selected VDC:\n" + "\n".join(_fmt_vm(v) for v in vms)

    if vdc_name and not want_all:
        vdc_id = client.get_vdc_id(vdc_name)
        vms = client.list_vms(vdc_id)
        if not vms:
            return f"No VMs found in VDC '{vdc_name}'."
        return f"Found {len(vms)} VM(s) in VDC '{vdc_name}':\n" + "\n".join(_fmt_vm(v) for v in vms)

    vdcs = client.list_vdcs()
    active_org = getattr(client, "_active_org", "") or ""
    if active_org and active_org.lower() != "system":
        vdcs = [v for v in vdcs if (v.get("org") or "").lower() == active_org.lower()]
    if not vdcs:
        scope = f" in org '{active_org}'" if active_org else ""
        return f"No VDCs found{scope}."
    lines = []
    total = 0
    for vdc in vdcs:
        try:
            vms = client.list_vms(vdc["id"])
            total += len(vms)
            lines.append(f"\nVDC: {vdc['name']} ({len(vms)} VM{'s' if len(vms) != 1 else ''})")
            lines.extend(_fmt_vm(v) for v in vms)
        except VCDClientError:
            lines.append(f"\nVDC: {vdc['name']} (access denied)")
    org_label = f" in org '{active_org}'" if active_org else ""
    return f"Found {total} VM(s) across {len(vdcs)} VDC(s){org_label}:" + "\n".join(lines)


def _list_vdcs(client: VCDClient) -> str:
    vdcs = client.list_vdcs()
    if not vdcs:
        return "No VDCs found."
    lines = [f"Found {len(vdcs)} VDC(s):"]
    for v in vdcs:
        suffix = f" (org: {v['org']})" if v.get("org") else ""
        lines.append(f"  • {v['name']}{suffix}")
    return "\n".join(lines)


def _list_vapps(client: VCDClient, vdc_name: Optional[str], default_vdc_id: str = "") -> str:
    if not vdc_name and default_vdc_id:
        vapps = client.list_vapps(default_vdc_id)
        if not vapps:
            return "No vApps found in the selected VDC."
        lines = [f"Found {len(vapps)} vApp(s) in the selected VDC:"]
        lines.extend(f"  • {v['name']} — {v.get('status', '?')}" for v in vapps)
        return "\n".join(lines)
    if not vdc_name:
        raise QueryError("Which VDC would you like to list vApps from?")
    vdc_id = client.get_vdc_id(vdc_name)
    vapps = client.list_vapps(vdc_id)
    if not vapps:
        return f"No vApps found in VDC '{vdc_name}'."
    lines = [f"Found {len(vapps)} vApp(s) in VDC '{vdc_name}':"]
    lines.extend(f"  • {v['name']} — {v.get('status', '?')}" for v in vapps)
    return "\n".join(lines)


def _list_networks(client: VCDClient, vdc_name: Optional[str], default_vdc_id: str = "") -> str:
    vdc_id = None
    if not vdc_name and default_vdc_id:
        vdc_id = default_vdc_id
    elif vdc_name and vdc_name.lower() not in ("all", "any"):
        vdc_id = client.get_vdc_id(vdc_name)
    networks = client.list_networks(vdc_id)
    scope = f"VDC '{vdc_name}'" if vdc_name and vdc_id else "all VDCs"
    if not networks:
        return f"No networks found in {scope}."
    lines = [f"Found {len(networks)} network(s) in {scope}:"]
    for n in networks:
        gw = n.get("gateway") or "?"
        prefix = n.get("prefix_length") or "?"
        lines.append(f"  • {n['name']} — {n.get('type', '?')} | {gw}/{prefix}")
    return "\n".join(lines)


def _list_edge_gateways(client: VCDClient, vdc_name: Optional[str], default_vdc_id: str = "") -> str:
    vdc_id = None
    if not vdc_name and default_vdc_id:
        vdc_id = default_vdc_id
    elif vdc_name and vdc_name.lower() not in ("all", "any"):
        vdc_id = client.get_vdc_id(vdc_name)
    gateways = client.list_edge_gateways(vdc_id)
    if not gateways:
        return "No edge gateways found."
    lines = [f"Found {len(gateways)} edge gateway(s):"]
    lines.extend(f"  • {g['name']} — {g.get('status', '?')}" for g in gateways)
    return "\n".join(lines)


def _resolve_edge_gateway(client: VCDClient, name: str, vdc_id: Optional[str] = None) -> dict:
    gateways = client.list_edge_gateways(vdc_id)
    gw = next((g for g in gateways if g["name"].lower() == name.lower()), None)
    if not gw and vdc_id:
        # Fallback: search without VDC filter
        gateways = client.list_edge_gateways()
        gw = next((g for g in gateways if g["name"].lower() == name.lower()), None)
    if not gw:
        available = [g["name"] for g in gateways]
        raise QueryError(f"Edge gateway '{name}' not found. Available: {available}")
    return gw


def _list_firewall_rules(client: VCDClient, edge_gateway_name: Optional[str], default_vdc_id: str = "") -> str:
    if not edge_gateway_name:
        raise QueryError("Which edge gateway would you like to list firewall rules for?")
    gw = _resolve_edge_gateway(client, edge_gateway_name, default_vdc_id or None)
    rules = client.list_firewall_rules(gw["id"])
    if not rules:
        return f"No firewall rules found on '{edge_gateway_name}'."
    lines = [f"Found {len(rules)} firewall rule(s) on '{edge_gateway_name}':"]
    for r in rules:
        status = "enabled" if r.get("enabled") else "disabled"
        lines.append(f"  • {r['name']} — {r.get('action', '?')} ({status})")
    return "\n".join(lines)


def _list_nat_rules(client: VCDClient, edge_gateway_name: Optional[str], default_vdc_id: str = "") -> str:
    if not edge_gateway_name:
        raise QueryError("Which edge gateway would you like to list NAT rules for?")
    gw = _resolve_edge_gateway(client, edge_gateway_name, default_vdc_id or None)
    rules = client.list_nat_rules(gw["id"])
    if not rules:
        return f"No NAT rules found on '{edge_gateway_name}'."
    lines = [f"Found {len(rules)} NAT rule(s) on '{edge_gateway_name}':"]
    for r in rules:
        ext = r.get("external_ip") or "?"
        int_ = r.get("internal_ip") or "?"
        lines.append(f"  • {r['name']} — {r.get('type', '?')} | {ext} → {int_}")
    return "\n".join(lines)


def _list_security_groups(client: VCDClient, vdc_group_id: Optional[str]) -> str:
    if not vdc_group_id:
        raise QueryError("Which VDC Group would you like to list security groups for?")
    resolved_id = client.get_vdc_group_id(vdc_group_id)
    groups = client.list_security_groups(resolved_id)
    if not groups:
        return "No security groups found."
    lines = [f"Found {len(groups)} security group(s):"]
    lines.extend(f"  • {g['name']}" for g in groups)
    return "\n".join(lines)


def _list_ip_sets(client: VCDClient, vdc_group_id: Optional[str]) -> str:
    if not vdc_group_id:
        raise QueryError("Which VDC Group would you like to list IP sets for?")
    resolved_id = client.get_vdc_group_id(vdc_group_id)
    ip_sets = client.list_ip_sets(resolved_id)
    if not ip_sets:
        return "No IP sets found."
    lines = [f"Found {len(ip_sets)} IP set(s):"]
    for i in ip_sets:
        ips = ", ".join(i.get("ip_addresses", [])) or "none"
        lines.append(f"  • {i['name']} — {ips}")
    return "\n".join(lines)


def _get_vm(client: VCDClient, vm_name: Optional[str], vdc_name: Optional[str], default_vdc_id: str = "") -> str:
    if not vm_name:
        raise QueryError("Which VM would you like details for?")
    if not vdc_name and default_vdc_id:
        vdc_id = default_vdc_id
    elif vdc_name:
        vdc_id = client.get_vdc_id(vdc_name)
    else:
        raise QueryError("Which VDC is this VM in?")
    vm = client.get_vm_details(vdc_id, vm_name)
    cpu = vm.get("cpu") or "?"
    mem = vm.get("memory_mb")
    mem_str = f"{mem // 1024} GB" if mem else "?"
    lines = [
        f"VM: {vm['name']}",
        f"  Status : {vm.get('status', '?')}",
        f"  CPU    : {cpu} vCPU(s)",
        f"  RAM    : {mem_str}",
    ]
    disks = vm.get("disks", [])
    if disks:
        lines.append(f"  Disks  : {len(disks)} disk(s)")
        for d in disks:
            sz = f"{d['size_mb'] // 1024} GB" if d.get("size_mb") else "?"
            sp = f" [{d['storage_profile']}]" if d.get("storage_profile") else ""
            lines.append(f"    • {d.get('name', '?')} — {sz}{sp}")
    return "\n".join(lines)


def _get_network(client: VCDClient, network_name: Optional[str], vdc_name: Optional[str], default_vdc_id: str = "") -> str:
    if not network_name:
        raise QueryError("Which network would you like details for?")
    vdc_id = None
    if not vdc_name and default_vdc_id:
        vdc_id = default_vdc_id
    elif vdc_name and vdc_name.lower() not in ("all", "any"):
        vdc_id = client.get_vdc_id(vdc_name)
    net = client.get_network_details(network_name, vdc_id)
    lines = [
        f"Network: {net['name']}",
        f"  Type   : {net.get('type', '?')}",
        f"  Gateway: {net.get('gateway', '?')}/{net.get('prefix_length', '?')}",
    ]
    for subnet in net.get("subnets_detail", []):
        dns1 = subnet.get("dnsServer1") or subnet.get("dns1")
        dns2 = subnet.get("dnsServer2") or subnet.get("dns2")
        if dns1:
            lines.append(f"  DNS    : {dns1}" + (f", {dns2}" if dns2 else ""))
        suffix = subnet.get("dnsSuffix")
        if suffix:
            lines.append(f"  Suffix : {suffix}")
        pools = subnet.get("ipRanges", {}).get("values", [])
        if pools:
            lines.append(f"  DHCP pools:")
            for p in pools:
                lines.append(f"    • {p.get('startAddress')} – {p.get('endAddress')}")
    return "\n".join(lines)
