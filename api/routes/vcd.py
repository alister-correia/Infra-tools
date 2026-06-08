import logging
import os
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Header, Depends
from pydantic import BaseModel

from connectors.vcd_client import vcd_client as _singleton, make_vcd_client, VCDClient, VCDClientError

logger = logging.getLogger(__name__)
_DEBUG = os.getenv("DEBUG", "false").lower() == "true"

router = APIRouter(prefix="/api/vcd", tags=["vcd"])


class OrgSelect(BaseModel):
    org: str

class EnvSelect(BaseModel):
    name: str


def _vcd_error(exc: VCDClientError) -> HTTPException:
    return HTTPException(status_code=503, detail=str(exc))


def _client_from_headers(
    x_vcd_username: Annotated[str, Header()] = "",
    x_vcd_password: Annotated[str, Header()] = "",
    x_vcd_org: Annotated[str, Header()] = "",
) -> VCDClient:
    if x_vcd_username and x_vcd_password:
        return make_vcd_client(x_vcd_username, x_vcd_password, org=x_vcd_org)
    return _singleton


# ── Environment endpoints (no user creds needed — server config) ──────────

@router.get("/environments")
async def list_environments() -> list:
    return _singleton.list_environments()


@router.post("/environments/select")
async def select_environment(body: EnvSelect) -> dict:
    try:
        _singleton.set_environment(body.name)
        return {"ok": True, "active_environment": body.name, "host": _singleton._host}
    except VCDClientError as exc:
        raise _vcd_error(exc)


# ── Debug endpoints (only available when DEBUG=true) ─────────────────────

@router.get("/debug-orgs")
async def debug_orgs(client: VCDClient = Depends(_client_from_headers)) -> dict:
    if not _DEBUG:
        raise HTTPException(status_code=404, detail="Not found")
    try:
        configured = client._configured()
        host = client._host
        username = client._username
        has_password = bool(client._password)
        active_org = client._active_org
        token_valid_system = client._token_valid("System")

        try:
            client.authenticate("System")
            auth_ok = True
            auth_error = None
        except Exception as e:
            auth_ok = False
            auth_error = str(e)

        try:
            client._active_org = "System"
            data = client._get(client._cloudapi("orgs"), params={"page": 1, "pageSize": 10})
            orgs_sample = data.get("values", []) if isinstance(data, dict) else []
            result_total = data.get("resultTotal", "?")
            orgs_ok = True
            orgs_error = None
        except Exception as e:
            orgs_sample = []
            result_total = 0
            orgs_ok = False
            orgs_error = str(e)

        return {
            "configured": configured,
            "host": host,
            "username": username,
            "has_password": has_password,
            "active_org": active_org,
            "token_valid_system": token_valid_system,
            "auth_ok": auth_ok,
            "auth_error": auth_error,
            "orgs_ok": orgs_ok,
            "orgs_error": orgs_error,
            "result_total": result_total,
            "sample_orgs": [o.get("name") for o in orgs_sample[:5]],
        }
    except Exception as e:
        return {"fatal_error": str(e)}


@router.get("/debug-auth")
async def debug_auth(client: VCDClient = Depends(_client_from_headers)) -> dict:
    if not _DEBUG:
        raise HTTPException(status_code=404, detail="Not found")
    import base64, re
    import requests as _req
    ver_resp = _req.get(f"{client._host}/api/versions", headers={"Accept": "application/*+xml"}, timeout=5)
    all_versions = re.findall(r"<Version>([\d.]+)</Version>", ver_resp.text)

    client._api_version = None
    negotiated = client.API_VERSION
    results = {"all_supported_versions": all_versions, "negotiated_api_version": negotiated}

    s = _req.Session()
    for org_name in ["System", "system", None]:
        cred_str = f"{client._username}:{client._password}" if org_name is None else f"{client._username}@{org_name}:{client._password}"
        creds = base64.b64encode(cred_str.encode()).decode()
        label_key = org_name or "no-org (provider)"
        try:
            accept = f"application/json;version={negotiated}"
            attempts = []
            for label, url in [
                ("provider-endpoint", f"{client._host}/cloudapi/1.0.0/sessions/provider"),
                ("tenant-endpoint",   f"{client._host}/cloudapi/1.0.0/sessions"),
            ]:
                r = s.post(url, headers={"Authorization": f"Basic {creds}", "Accept": accept, "Content-Type": "application/json"}, timeout=10)
                attempts.append({
                    "label": label,
                    "status": r.status_code,
                    "has_token": bool(r.headers.get("X-VMWARE-VCLOUD-ACCESS-TOKEN") or r.headers.get("x-vcloud-authorization")),
                    "body": r.json() if "json" in r.headers.get("content-type", "") else r.text[:150],
                })
                if r.status_code in (200, 201):
                    break
            results[label_key] = attempts
        except Exception as exc:
            results[label_key] = {"error": str(exc)}
    return results


# ── Authenticated endpoints ───────────────────────────────────────────────

@router.get("/verify")
async def verify_vcd(client: VCDClient = Depends(_client_from_headers)) -> dict:
    return client.verify()


@router.get("/orgs")
async def list_orgs(client: VCDClient = Depends(_client_from_headers)) -> list:
    try:
        return client.list_orgs()
    except VCDClientError as exc:
        raise _vcd_error(exc)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/orgs/select")
async def select_org(body: OrgSelect) -> dict:
    """Acknowledge org selection — org is tracked client-side in sessionStorage."""
    if not body.org:
        raise HTTPException(status_code=400, detail="org is required")
    return {"ok": True, "active_org": body.org}


@router.get("/vdcs")
async def list_vdcs(client: VCDClient = Depends(_client_from_headers)) -> list[dict]:
    try:
        return client.list_vdcs()
    except VCDClientError as exc:
        raise _vcd_error(exc)


@router.get("/vapps")
async def list_vapps(
    vdc_id: str = Query(...),
    client: VCDClient = Depends(_client_from_headers),
) -> list[dict]:
    try:
        return client.list_vapps(vdc_id)
    except VCDClientError as exc:
        raise _vcd_error(exc)


@router.get("/vms")
async def list_vms(
    vdc_id: str = Query(...),
    client: VCDClient = Depends(_client_from_headers),
) -> list[dict]:
    try:
        return client.list_vms(vdc_id)
    except VCDClientError as exc:
        raise _vcd_error(exc)


@router.get("/networks")
async def list_networks(
    vdc_id: str = Query(None),
    client: VCDClient = Depends(_client_from_headers),
) -> list[dict]:
    try:
        return client.list_networks(vdc_id)
    except VCDClientError as exc:
        raise _vcd_error(exc)


@router.get("/edge-gateways")
async def list_edge_gateways(
    vdc_id: str = Query(None),
    client: VCDClient = Depends(_client_from_headers),
) -> list[dict]:
    try:
        return client.list_edge_gateways(vdc_id)
    except VCDClientError as exc:
        raise _vcd_error(exc)


@router.get("/dfw-policies")
async def list_dfw_policies(client: VCDClient = Depends(_client_from_headers)) -> list[dict]:
    try:
        return client.list_dfw_policies()
    except VCDClientError as exc:
        raise _vcd_error(exc)


@router.get("/security-groups")
async def list_security_groups(
    vdc_group_id: str = Query(...),
    client: VCDClient = Depends(_client_from_headers),
) -> list[dict]:
    try:
        return client.list_security_groups(vdc_group_id)
    except VCDClientError as exc:
        raise _vcd_error(exc)


@router.get("/ip-sets")
async def list_ip_sets(
    vdc_group_id: str = Query(...),
    client: VCDClient = Depends(_client_from_headers),
) -> list[dict]:
    try:
        return client.list_ip_sets(vdc_group_id)
    except VCDClientError as exc:
        raise _vcd_error(exc)


@router.get("/firewall-rules")
async def list_firewall_rules(
    edge_gateway_id: str = Query(...),
    client: VCDClient = Depends(_client_from_headers),
) -> list[dict]:
    try:
        return client.list_firewall_rules(edge_gateway_id)
    except VCDClientError as exc:
        raise _vcd_error(exc)


@router.get("/nat-rules")
async def list_nat_rules(
    edge_gateway_id: str = Query(...),
    client: VCDClient = Depends(_client_from_headers),
) -> list[dict]:
    try:
        return client.list_nat_rules(edge_gateway_id)
    except VCDClientError as exc:
        raise _vcd_error(exc)
