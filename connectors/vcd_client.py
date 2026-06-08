import http.client
import json
import logging
import time
import base64
import requests
from typing import Dict, Optional
from config.settings import settings

logger = logging.getLogger(__name__)

# VCD returns many Set-Cookie headers — raise Python's default limit of 100
http.client._MAXHEADERS = 500


class VCDClientError(Exception):
    pass


class VCDClient:
    """
    VMware VCD REST API wrapper.
    Supports dynamic org switching — tokens are cached per org so switching
    doesn't re-authenticate unnecessarily.
    """

    # Versions to try in order — newest first
    API_VERSIONS = ["39.1", "39.0", "38.1", "38.0", "37.3", "37.2", "37.1", "37.0",
                    "36.3", "36.2", "36.1", "35.2", "34.0", "33.0"]
    TOKEN_TTL = 1800  # VCD tokens expire after 30 min inactivity
    REQUEST_TIMEOUT = 30  # seconds

    def __init__(self):
        self._username: str = settings.vcd_username
        self._password: str = settings.vcd_password

        # Parse environments from settings
        self._environments: Dict[str, str] = self._parse_environments()

        # Active environment — defaults to first in list
        if self._environments:
            first_name = next(iter(self._environments))
            self._active_env: str = first_name
            self._host: str = self._environments[first_name]
        else:
            self._active_env = ""
            self._host = ""

        # Negotiated API version — set on first successful auth
        self._api_version: Optional[str] = None

        # Active org — can be changed at runtime via set_org()
        self._active_org: str = settings.vcd_org  # may be empty

        # Token cache keyed by (host, org)
        self._tokens: Dict[str, str] = {}
        self._token_types: Dict[str, str] = {}  # "bearer" or "legacy"
        self._token_times: Dict[str, float] = {}

        self._session = requests.Session()

    def _parse_environments(self) -> Dict[str, str]:
        """Parse VCD_ENVIRONMENTS = 'Name1:https://host1,Name2:https://host2'"""
        raw = settings.vcd_environments.strip()
        if not raw:
            return {}
        envs = {}
        for entry in raw.split(","):
            entry = entry.strip()
            if ":" not in entry:
                continue
            # Split on first colon only, but host has colons too (https://)
            # Format: Name:https://host → split on first colon
            name, rest = entry.split(":", 1)
            envs[name.strip()] = rest.strip().rstrip("/")
        return envs

    def _token_key(self, host: str, org: str) -> str:
        return f"{host}||{org}"

    def _negotiate_api_version(self) -> str:
        """
        Fetch supported API versions from VCD and return the highest we support.
        VCD returns this list as XML regardless of Accept header.
        """
        try:
            r = self._session.get(
                f"{self._host}/api/versions",
                headers={"Accept": "application/*+xml"},
                timeout=5,
            )
            if r.status_code == 200:
                # Parse version numbers from XML: <Version>36.2</Version>
                import re
                found = re.findall(r"<Version>([\d.]+)</Version>", r.text)
                if found:
                    supported = set(found)
                    for v in self.API_VERSIONS:
                        if v in supported:
                            return v
        except Exception:
            pass
        return self.API_VERSIONS[-1]

    # ------------------------------------------------------------------
    # Environment + org management
    # ------------------------------------------------------------------

    def list_environments(self) -> list:
        return [{"name": name, "host": host} for name, host in self._environments.items()]

    def set_environment(self, name: str) -> None:
        if name not in self._environments:
            raise VCDClientError(
                f"Environment '{name}' not found. Available: {list(self._environments.keys())}"
            )
        self._active_env = name
        self._host = self._environments[name]
        self._api_version = None  # re-negotiate for new host

    def get_active_environment(self) -> str:
        return self._active_env

    def set_org(self, org: str) -> None:
        """Switch the active org context. Uses System token + tenant context header."""
        self._active_org = org

    def get_active_org(self) -> str:
        return self._active_org

    def _org_context_header(self) -> dict:
        """Returns the tenant context header for org-scoped operations."""
        if self._active_org and self._active_org != "System":
            return {"X-VMWARE-VCLOUD-TENANT-CONTEXT": self._active_org}
        return {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _configured(self) -> bool:
        return all([self._host, self._username, self._password])

    def _token_valid(self, org: str) -> bool:
        key = self._token_key(self._host, org)
        token = self._tokens.get(key)
        acquired = self._token_times.get(key, 0)
        return bool(token) and (time.time() - acquired) < self.TOKEN_TTL

    @property
    def API_VERSION(self) -> str:
        if not self._api_version:
            self._api_version = self._negotiate_api_version()
        return self._api_version

    def _base_headers(self, with_org_context: bool = True) -> dict:
        # Always use the System token — org context is set via tenant context header
        key = self._token_key(self._host, "System")
        token = self._tokens.get(key, "")
        token_type = self._token_types.get(key, "bearer")
        accept = f"application/json;version={self.API_VERSION}"
        if token_type == "legacy":
            headers = {"Accept": accept, "x-vcloud-authorization": token}
        else:
            headers = {"Accept": accept, "Authorization": f"Bearer {token}"}
        if with_org_context:
            headers.update(self._org_context_header())
        return headers

    def _api(self, path: str) -> str:
        return f"{self._host}/api/{path.lstrip('/')}"

    def _cloudapi(self, path: str) -> str:
        return f"{self._host}/cloudapi/1.0.0/{path.lstrip('/')}"

    @staticmethod
    def _to_uuid(urn_or_id: str) -> str:
        """Extract UUID from URN (urn:vcloud:type:uuid → uuid) or return as-is."""
        return urn_or_id.split(":")[-1] if urn_or_id.startswith("urn:") else urn_or_id

    def _get_legacy(self, url: str, params: Optional[dict] = None) -> dict:
        """GET against the legacy /api endpoint with the correct Accept header."""
        self._ensure_auth()
        key = self._token_key(self._host, "System")
        token = self._tokens.get(key, "")
        token_type = self._token_types.get(key, "bearer")
        accept = f"application/*+json;version={self.API_VERSION}"
        if token_type == "legacy":
            headers = {"Accept": accept, "x-vcloud-authorization": token}
        else:
            headers = {"Accept": accept, "Authorization": f"Bearer {token}"}
        headers.update(self._org_context_header())
        resp = self._session.get(url, headers=headers, params=params, timeout=self.REQUEST_TIMEOUT)
        if resp.status_code == 401:
            self.authenticate(self._active_org)
            resp = self._session.get(url, headers=headers, params=params, timeout=self.REQUEST_TIMEOUT)
        return self._handle_response(resp, url)

    def _handle_response(self, resp: requests.Response, context: str) -> dict:
        if resp.status_code in (200, 201, 202):
            try:
                return resp.json()
            except Exception:
                return {}
        if resp.status_code == 401:
            raise VCDClientError(f"VCD auth failed during {context} — token may have expired")
        if resp.status_code == 403:
            try:
                body = resp.json()
                detail = body.get("message") or body.get("minorErrorCode") or resp.text[:300]
            except Exception:
                detail = resp.text[:300]
            logger.info("403 detail for %s: %s", context, detail)
            raise VCDClientError(f"VCD permission denied for {context}: {detail}")
        if resp.status_code == 404:
            raise VCDClientError(f"VCD resource not found: {context}")
        try:
            detail = resp.json().get("message", resp.text[:200])
        except Exception:
            detail = resp.text[:200]
        raise VCDClientError(f"VCD API error {resp.status_code} in {context}: {detail}")

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def authenticate(self, org: Optional[str] = None) -> None:
        """Authenticate for a specific org and cache the token."""
        if not self._configured():
            raise VCDClientError(
                "VCD credentials not configured. "
                "Set VCD_HOST, VCD_USERNAME, VCD_PASSWORD in .env"
            )
        target_org = org or self._active_org
        if not target_org:
            raise VCDClientError(
                "No org selected. Choose an org from the selector in the UI."
            )

        credentials = base64.b64encode(
            f"{self._username}@{target_org}:{self._password}".encode()
        ).decode()
        accept = f"application/json;version={self.API_VERSION}"
        headers = {"Authorization": f"Basic {credentials}", "Accept": accept, "Content-Type": "application/json"}

        # VCD 10.4+ provider/system admin uses /cloudapi/1.0.0/sessions/provider
        # Tenant orgs use /cloudapi/1.0.0/sessions
        # Older VCD uses /api/sessions
        resp = self._session.post(self._cloudapi("sessions/provider"), headers=headers)
        if resp.status_code in (404, 405):
            resp = self._session.post(self._cloudapi("sessions"), headers=headers)
        if resp.status_code in (404, 405):
            legacy_headers = {"Authorization": f"Basic {credentials}", "Accept": f"application/*+json;version={self.API_VERSION}"}
            resp = self._session.post(self._api("sessions"), headers=legacy_headers)

        if resp.status_code not in (200, 201):
            raise VCDClientError(
                f"VCD authentication failed for org '{target_org}' (HTTP {resp.status_code}). "
                "Check credentials in .env"
            )
        bearer_token = resp.headers.get("X-VMWARE-VCLOUD-ACCESS-TOKEN")
        legacy_token = resp.headers.get("x-vcloud-authorization")
        token = bearer_token or legacy_token
        if not token:
            raise VCDClientError("VCD returned no auth token — check API version compatibility")

        key = self._token_key(self._host, target_org)
        self._tokens[key] = token
        self._token_types[key] = "bearer" if bearer_token else "legacy"
        self._token_times[key] = time.time()
        self._active_org = target_org
        # Clear accumulated cookies — we manage auth via headers, not cookies
        self._session.cookies.clear()

    def _ensure_auth(self) -> None:
        # Always authenticate as System — org context is handled via header
        if not self._token_valid("System"):
            self.authenticate("System")

    def _get_all_pages(self, url: str, page_size: int = 100, params: Optional[dict] = None) -> list:
        """Fetch all pages from a paginated cloudapi endpoint."""
        results = []
        page = 1
        base = params or {}
        while True:
            data = self._get(url, params={**base, "page": page, "pageSize": page_size})
            values = data.get("values", []) if isinstance(data, dict) else []
            results.extend(values)
            result_total = data.get("resultTotal", 0) if isinstance(data, dict) else 0
            if len(results) >= result_total or not values:
                break
            page += 1
        return results

    def _get(self, url: str, params: Optional[dict] = None) -> dict:
        self._ensure_auth()
        resp = self._session.get(url, headers=self._base_headers(), params=params, timeout=self.REQUEST_TIMEOUT)
        if resp.status_code == 401:
            self.authenticate(self._active_org)
            resp = self._session.get(url, headers=self._base_headers(), params=params, timeout=self.REQUEST_TIMEOUT)
        return self._handle_response(resp, url)

    def _get_versioned(self, url: str, version: str, params: Optional[dict] = None) -> dict:
        """GET with an explicit API version override (bypasses the negotiated version)."""
        self._ensure_auth()
        key = self._token_key(self._host, "System")
        token = self._tokens.get(key, "")
        token_type = self._token_types.get(key, "bearer")
        if token_type == "legacy":
            headers = {"Accept": f"application/json;version={version}", "x-vcloud-authorization": token}
        else:
            headers = {"Accept": f"application/json;version={version}", "Authorization": f"Bearer {token}"}
        headers.update(self._org_context_header())
        resp = self._session.get(url, headers=headers, params=params, timeout=self.REQUEST_TIMEOUT)
        if resp.status_code == 401:
            self.authenticate(self._active_org)
            resp = self._session.get(url, headers=headers, params=params, timeout=self.REQUEST_TIMEOUT)
        return self._handle_response(resp, url)

    def _get_gw(self, url: str, params: Optional[dict] = None) -> dict:
        """
        GET for edge gateway sub-resources (firewall, NAT).
        Tries with the tenant org context first; if VCD returns 403 (e.g. the gateway
        belongs to a VDC group or a different org scope) retries as pure System admin.
        """
        self._ensure_auth()
        resp = self._session.get(url, headers=self._base_headers(with_org_context=True),
                                 params=params, timeout=self.REQUEST_TIMEOUT)
        if resp.status_code == 403:
            logger.info("GW 403 with org context — retrying as System (url=%s, org=%s)", url, self._active_org)
            resp = self._session.get(url, headers=self._base_headers(with_org_context=False),
                                     params=params, timeout=self.REQUEST_TIMEOUT)
        if resp.status_code == 401:
            self.authenticate(self._active_org)
            resp = self._session.get(url, headers=self._base_headers(with_org_context=False),
                                     params=params, timeout=self.REQUEST_TIMEOUT)
        return self._handle_response(resp, url)

    def _put_gw(self, url: str, payload: dict) -> dict:
        """PUT for edge gateway sub-resources with same 403-retry logic as _get_gw."""
        self._ensure_auth()
        body = json.dumps(payload)

        headers_ctx = {**self._base_headers(with_org_context=True), "Content-Type": "application/json"}
        resp = self._session.put(url, headers=headers_ctx, data=body, timeout=self.REQUEST_TIMEOUT)
        if resp.status_code == 403:
            logger.info("GW PUT 403 with org context — retrying as System (url=%s)", url)
            headers_sys = {**self._base_headers(with_org_context=False), "Content-Type": "application/json"}
            resp = self._session.put(url, headers=headers_sys, data=body, timeout=self.REQUEST_TIMEOUT)
        if resp.status_code == 401:
            self.authenticate(self._active_org)
            headers_sys = {**self._base_headers(with_org_context=False), "Content-Type": "application/json"}
            resp = self._session.put(url, headers=headers_sys, data=body, timeout=self.REQUEST_TIMEOUT)
        return self._handle_response(resp, url)

    def _post(self, url: str, payload: dict, content_type: str = "application/json") -> dict:
        self._ensure_auth()
        headers = {**self._base_headers(), "Content-Type": content_type}
        body = json.dumps(payload)
        resp = self._session.post(url, headers=headers, data=body, timeout=self.REQUEST_TIMEOUT)
        if resp.status_code == 401:
            self.authenticate(self._active_org)
            resp = self._session.post(url, headers=headers, data=body, timeout=self.REQUEST_TIMEOUT)
        return self._handle_response(resp, url)

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    def verify(self) -> dict:
        if not self._configured():
            return {
                "ok": False,
                "error": "VCD credentials not configured — set VCD_HOST, VCD_USERNAME, VCD_PASSWORD in .env",
            }
        try:
            orgs = self.list_orgs()
            return {
                "ok": True,
                "host": self._host,
                "active_org": self._active_org or None,
                "available_orgs": [o["name"] for o in orgs],
                "total_orgs": len(orgs),
            }
        except VCDClientError as exc:
            return {"ok": False, "error": str(exc)}

    # ------------------------------------------------------------------
    # Org listing (no org required — uses System org)
    # ------------------------------------------------------------------

    def list_orgs(self) -> list:
        """
        List all orgs with pagination.
        Authenticates as System if not already authenticated.
        """
        prev_org = self._active_org
        try:
            self.authenticate("System")
            values = self._get_all_pages(self._cloudapi("orgs"))
            return [{"id": o.get("id", ""), "name": o.get("name", "")} for o in values]
        except VCDClientError:
            raise
        except Exception as exc:
            raise VCDClientError(f"Failed to list orgs: {exc}")
        finally:
            self._active_org = prev_org

    # ------------------------------------------------------------------
    # VDCs
    # ------------------------------------------------------------------

    def list_vdcs(self) -> list:
        # Keep _active_org set so the X-VMWARE-VCLOUD-TENANT-CONTEXT header is
        # included — without it VCD returns VDCs across all orgs for users with
        # elevated permissions, leaking other tenants' resources.
        data = self._get_all_pages(self._cloudapi("vdcs"))
        result = []
        for v in data:
            org_ref = v.get("org") or v.get("ownerRef") or v.get("orgRef") or {}
            if not isinstance(org_ref, dict):
                org_ref = {}
            result.append({
                "id": v.get("id"),
                "name": v.get("name") or v.get("displayName"),
                "org": org_ref.get("name") or org_ref.get("displayName"),
                "org_id": org_ref.get("id"),
            })
        # Extra safety: if we know the active org, discard VDCs from other orgs.
        if self._active_org and self._active_org != "System":
            result = [
                v for v in result
                if not v.get("org") or v["org"].lower() == self._active_org.lower()
            ]
        return result

    def get_vdc_id(self, name: str) -> str:
        vdcs = self.list_vdcs()
        for v in vdcs:
            if v["name"] and v["name"].lower() == name.lower():
                return v["id"]
        raise VCDClientError(
            f"VDC '{name}' not found. Available: {[v['name'] for v in vdcs]}"
        )

    # ------------------------------------------------------------------
    # VMs / vApps
    # ------------------------------------------------------------------

    def _put(self, url: str, payload: dict) -> dict:
        self._ensure_auth()
        headers = {**self._base_headers(), "Content-Type": "application/json"}
        body = json.dumps(payload)
        resp = self._session.put(url, headers=headers, data=body, timeout=self.REQUEST_TIMEOUT)
        if resp.status_code == 401:
            self.authenticate(self._active_org)
            resp = self._session.put(url, headers=headers, data=body, timeout=self.REQUEST_TIMEOUT)
        return self._handle_response(resp, url)

    def _post_xml(self, url: str, xml_body: str, content_type: str) -> dict:
        import re
        self._ensure_auth()
        headers = {**self._base_headers(), "Content-Type": content_type}
        headers["Accept"] = f"application/*+xml;version={self.API_VERSION}"
        encoded = xml_body.encode("utf-8")
        resp = self._session.post(url, headers=headers, data=encoded, timeout=self.REQUEST_TIMEOUT)
        if resp.status_code == 401:
            self.authenticate(self._active_org)
            resp = self._session.post(url, headers=headers, data=encoded, timeout=self.REQUEST_TIMEOUT)
        if resp.status_code in (200, 201, 202):
            vapp_id = None
            m = re.search(r'id="(urn:vcloud:vapp:[^"]+)"', resp.text)
            if not m:
                m = re.search(r'href="[^"]+/vApp/(vapp-[^"/]+)"', resp.text)
            if m:
                vapp_id = m.group(1)
            return {"vapp_id": vapp_id, "status": "INSTANTIATING"}
        raise VCDClientError(f"VCD API error {resp.status_code}: {resp.text[:300]}")

    def get_vdc_href(self, vdc_name: str) -> str:
        """Return the legacy API href for a VDC — needed for XML-based API calls."""
        params = {"type": "orgVdc", "format": "records", "filter": f"name=={vdc_name}"}
        data = self._get(self._api("query"), params=params)
        records = data.get("record", []) if isinstance(data, dict) else []
        if not records:
            raise VCDClientError(f"VDC '{vdc_name}' not found via query API.")
        return records[0].get("href", "")

    def find_template_href(self, template_name: str) -> str:
        """Find a vApp template by name across all catalogs and return its href."""
        params = {"type": "vAppTemplate", "format": "records", "filter": f"name=={template_name}"}
        data = self._get(self._api("query"), params=params)
        records = data.get("record", []) if isinstance(data, dict) else []
        if not records:
            raise VCDClientError(
                f"Template '{template_name}' not found in any accessible catalog."
            )
        return records[0].get("href", "")

    def instantiate_vapp(
        self,
        vdc_href: str,
        vapp_name: str,
        template_href: str,
        network_name: str = "",
    ) -> dict:
        """Instantiate a vApp from a catalog template into the given VDC."""
        network_section = ""
        if network_name:
            network_section = f"""
            <NetworkConfig networkName="{network_name}">
                <Configuration>
                    <FenceMode>bridged</FenceMode>
                </Configuration>
            </NetworkConfig>"""

        xml_body = f"""<?xml version="1.0" encoding="UTF-8"?>
<InstantiateVAppTemplateParams
    xmlns="http://www.vmware.com/vcloud/v1.5"
    xmlns:ovf="http://schemas.dmtf.org/ovf/envelope/1"
    name="{vapp_name}"
    deploy="true"
    powerOn="true">
    <InstantiationParams>
        <NetworkConfigSection>
            <ovf:Info>Network config</ovf:Info>
            {network_section}
        </NetworkConfigSection>
    </InstantiationParams>
    <Source href="{template_href}"/>
    <AllEULAsAccepted>true</AllEULAsAccepted>
</InstantiateVAppTemplateParams>"""

        url = f"{vdc_href}/action/instantiateVAppTemplate"
        return self._post_xml(
            url, xml_body,
            "application/vnd.vmware.vcloud.instantiateVAppTemplateParams+xml",
        )

    def get_vm_id(self, vdc_id: str, vm_name: str, vdc_name: str = "") -> str:
        """Find a VM by name in a VDC and return its cloudapi ID."""
        vms = self.list_vms(vdc_id, vdc_name)
        for vm in vms:
            if vm.get("name", "").lower() == vm_name.lower():
                return vm["id"]
        raise VCDClientError(
            f"VM '{vm_name}' not found. Available: {[v['name'] for v in vms]}"
        )

    @staticmethod
    def _set_rasd_quantity(data: dict, value: int) -> None:
        """Set virtualQuantity in a RASD JSON object.
        VCD returns virtualQuantity as {'value': N, 'otherAttributes': {}} — Jackson
        rejects a plain int replacement, so we must update the nested value field."""
        vq = data.get("virtualQuantity")
        if isinstance(vq, dict):
            data["virtualQuantity"]["value"] = value
        elif vq is not None:
            data["virtualQuantity"] = value
        elif "rasd:VirtualQuantity" in data:
            data["rasd:VirtualQuantity"] = value

    @staticmethod
    def _strip_links_from_rasd(data: dict) -> dict:
        """Remove LinkType navigation entries from 'any' — they cause Jackson deserialization
        errors when included in PUT body."""
        if "any" in data:
            data["any"] = [x for x in data["any"] if x.get("_type") not in ("LinkType",)]
        return data

    def _wait_for_task(self, resp, timeout: int = 60) -> None:
        """Poll a VCD task until it completes (success/error/canceled).

        Accepts either a requests.Response from a 202 reply, or a dict already
        parsed from one. Extracts the task href from the body and polls
        GET /api/task/{id} until the status is terminal or timeout is reached.
        """
        import time as _t
        try:
            if hasattr(resp, "status_code"):
                if resp.status_code != 202:
                    return
                try:
                    body = resp.json()
                except Exception:
                    return
            else:
                body = resp  # already a dict

            task_href = body.get("href", "") if isinstance(body, dict) else ""
            if not task_href or "/task/" not in task_href:
                # try operationKey / tasks link
                return

            logger.info("_wait_for_task: polling %s", task_href)
            deadline = _t.time() + timeout
            while _t.time() < deadline:
                _t.sleep(3)
                try:
                    headers = self._auth_headers()
                    headers["Accept"] = f"application/*+json;version={self.API_VERSION}"
                    tr = self._session.get(task_href, headers=headers, timeout=self.REQUEST_TIMEOUT)
                    if tr.status_code != 200:
                        logger.warning("_wait_for_task: GET returned %d", tr.status_code)
                        return
                    td = tr.json()
                    status = (td.get("status") or "").lower()
                    logger.info("_wait_for_task: status=%s", status)
                    if status in ("success", "error", "canceled", "aborted"):
                        if status != "success":
                            logger.warning("_wait_for_task: task ended with status=%s", status)
                        return
                except Exception as exc:
                    logger.warning("_wait_for_task: poll exception: %s", exc)
                    return
            logger.warning("_wait_for_task: timed out after %ds", timeout)
        except Exception as exc:
            logger.warning("_wait_for_task: unexpected error: %s", exc)

    def update_vm_compute(self, vm_id: str, cpu: int = None, memory_mb: int = None) -> dict:
        """Update CPU and/or memory via legacy virtualHardwareSection sub-resources."""
        vm_uuid = self._to_uuid(vm_id)
        ct = f"application/vnd.vmware.vcloud.rasdItem+json;version={self.API_VERSION}"

        if cpu is not None:
            url = self._api(f"vApp/vm-{vm_uuid}/virtualHardwareSection/cpu")
            data = self._get_legacy(url)
            self._set_rasd_quantity(data, cpu)
            self._strip_links_from_rasd(data)
            headers = self._auth_headers()
            headers["Content-Type"] = ct
            resp = self._session.put(url, headers=headers, data=json.dumps(data), timeout=self.REQUEST_TIMEOUT)
            logger.info("update_vm_compute cpu: PUT -> HTTP %d: %s", resp.status_code, resp.text[:150])
            if resp.status_code not in (200, 201, 202):
                raise VCDClientError(f"CPU update failed ({resp.status_code}): {resp.text[:200]}")
            self._wait_for_task(resp)

        if memory_mb is not None:
            url = self._api(f"vApp/vm-{vm_uuid}/virtualHardwareSection/memory")
            data = self._get_legacy(url)
            self._set_rasd_quantity(data, memory_mb)
            self._strip_links_from_rasd(data)
            headers = self._auth_headers()
            headers["Content-Type"] = ct
            resp = self._session.put(url, headers=headers, data=json.dumps(data), timeout=self.REQUEST_TIMEOUT)
            logger.info("update_vm_compute mem: PUT -> HTTP %d: %s", resp.status_code, resp.text[:150])
            if resp.status_code not in (200, 201, 202):
                raise VCDClientError(f"Memory update failed ({resp.status_code}): {resp.text[:200]}")
            self._wait_for_task(resp)

        return {"ok": True}

    def list_vapps(self, vdc_id: str, vdc_name: str = "") -> list:
        params = {"type": "adminVApp", "format": "records", "pageSize": "128"}
        if vdc_name:
            params["filter"] = f"vdcName=={vdc_name}"
        data = self._get_legacy(self._api("query"), params=params)
        records = data.get("record", []) if isinstance(data, dict) else []
        return [
            {"id": r.get("href", "").split("/")[-1], "name": r.get("name"), "status": r.get("status")}
            for r in records
        ]

    _VM_STATUS_MAP = {
        0: "UNRESOLVED", 1: "RESOLVED", 3: "SUSPENDED",
        4: "POWERED_ON", 5: "WAITING_FOR_INPUT", 6: "UNKNOWN_ERROR",
        7: "UNRECOGNIZED", 8: "POWERED_OFF", 9: "INCONSISTENT_STATE",
    }

    def _vm_status_str(self, raw) -> str:
        if isinstance(raw, int):
            return self._VM_STATUS_MAP.get(raw, str(raw))
        if isinstance(raw, str) and raw.isdigit():
            return self._VM_STATUS_MAP.get(int(raw), raw)
        return str(raw) if raw is not None else "UNKNOWN"

    def _get_vm_compute(self, vm_uuid: str) -> tuple:
        """Return (cpu, memory_mb, disks) for a VM by parsing its virtualHardwareSection."""
        try:
            data = self._get_legacy(self._api(f"vApp/vm-{vm_uuid}"))
            cpu = mem = None
            disks = []
            sections = data.get("section") or []
            if isinstance(sections, dict):
                sections = [sections]
            for section in sections:
                stype = str(section.get("_type", ""))
                if "hardwaresection" not in stype.lower():
                    continue
                items = section.get("item") or []
                if isinstance(items, dict):
                    items = [items]
                disk_index = 0
                for item in items:
                    rt = item.get("resourceType", {})
                    rt_val = rt.get("value") if isinstance(rt, dict) else rt
                    vq = item.get("virtualQuantity", {})
                    qty = vq.get("value") if isinstance(vq, dict) else vq
                    try:
                        rt_int = int(rt_val)
                    except (TypeError, ValueError):
                        continue
                    if rt_int == 3:
                        cpu = qty
                    elif rt_int == 4:
                        mem = qty
                    elif rt_int == 17:
                        disks.append(self._parse_hw_item(item, disk_index))
                        disk_index += 1
            return cpu, mem, disks
        except Exception as exc:
            logger.warning("_get_vm_compute %s failed: %s", vm_uuid[:8], exc)
            return None, None, []

    def list_vms(self, vdc_id: str, vdc_name: str = "") -> list:
        """List deployed VMs by iterating vApps and parsing their child VM XML.

        The 'vm' query type for tenant users only returns catalog template VMs,
        not deployed VMs. The only reliable path is vApp XML children.
        """
        import re as _re

        vapps = self.list_vapps(vdc_id, vdc_name)
        logger.info("list_vms: found %d vApps, scanning for child VMs", len(vapps))

        result = []
        xml_headers = self._auth_headers()
        xml_headers["Accept"] = f"application/*+xml;version={self.API_VERSION}"

        for vapp in vapps:
            vapp_id = vapp.get("id", "")
            vapp_name = vapp.get("name", "")
            if not vapp_id:
                continue
            vapp_href = self._api(f"vApp/{vapp_id}")
            try:
                resp = self._session.get(vapp_href, headers=xml_headers, timeout=self.REQUEST_TIMEOUT)
                if resp.status_code != 200:
                    continue
                for m in _re.finditer(r'<(?:\w+:)?Vm\b([^>]+?)(?:/>|>)', resp.text):
                    attrs = m.group(1)
                    href_m   = _re.search(r'\bhref="([^"]+/vApp/vm-[^"]+)"', attrs)
                    name_m   = _re.search(r'\bname="([^"]*)"', attrs)
                    status_m = _re.search(r'\bstatus="([^"]*)"', attrs)
                    if not href_m:
                        continue
                    seg  = href_m.group(1).rsplit("/", 1)[-1]
                    uuid = seg[3:] if seg.startswith("vm-") else seg
                    cpu, mem, disks = self._get_vm_compute(uuid)
                    result.append({
                        "id": f"urn:vcloud:vm:{uuid}",
                        "name": name_m.group(1) if name_m else uuid,
                        "status": self._vm_status_str(status_m.group(1) if status_m else None),
                        "cpu": cpu,
                        "memory_mb": mem,
                        "disks": disks,
                        "guest_os": None,
                        "detected_os": None,
                        "ip_address": None,
                        "network_name": None,
                        "storage_profile": None,
                        "vapp_name": vapp_name,
                        "tools_status": None,
                    })
            except Exception as exc:
                logger.warning("list_vms: failed to scan vApp %s: %s", vapp_id, exc)

        logger.info("list_vms: total %d deployed VMs found", len(result))
        return result

    # ------------------------------------------------------------------
    # Networks
    # ------------------------------------------------------------------

    def list_networks(self, vdc_id: Optional[str] = None) -> list:
        params = {"filter": f"ownerRef.id=={vdc_id}"} if vdc_id else {}
        data = self._get(self._cloudapi("orgVdcNetworks"), params=params)
        values = data.get("values", []) if isinstance(data, dict) else []
        return [
            {
                "id": n.get("id"),
                "name": n.get("name"),
                "type": n.get("networkType"),
                "gateway": n.get("subnets", {}).get("values", [{}])[0].get("gateway"),
                "prefix_length": n.get("subnets", {}).get("values", [{}])[0].get("prefixLength"),
            }
            for n in values
        ]

    # ------------------------------------------------------------------
    # Edge Gateways
    # ------------------------------------------------------------------

    def list_edge_gateways(self, vdc_id: Optional[str] = None) -> list:
        params = {"filter": f"ownerRef.id=={vdc_id}"} if vdc_id else {}
        data = self._get(self._cloudapi("edgeGateways"), params=params)
        values = data.get("values", []) if isinstance(data, dict) else []
        result = []
        for e in values:
            # Detect NSX-V vs NSX-T from the backing type field
            backing = e.get("gatewayBacking") or e.get("backingType") or ""
            if isinstance(backing, dict):
                backing = backing.get("backingType") or backing.get("gatewayType") or ""
            is_nsxv = "NSX_V" in str(backing).upper() or "NSXV" in str(backing).upper()
            result.append({
                "id": e.get("id"),
                "name": e.get("name"),
                "status": e.get("status"),
                "backing_type": "NSX-V" if is_nsxv else "NSX-T",
            })
        return result

    # ------------------------------------------------------------------
    # Edge Gateway firewall rules
    # ------------------------------------------------------------------

    @staticmethod
    def _gw_urn(gw_id: str) -> str:
        """Ensure edge gateway ID is a full URN — CloudAPI paths require urn:vcloud:gateway:uuid."""
        return gw_id if gw_id.startswith("urn:") else f"urn:vcloud:gateway:{gw_id}"

    def _get_nsxv_edge_backing_id(self, edge_gateway_id: str) -> str:
        """Return the NSX-V edge ID (e.g. 'edge-123') for a CloudAPI edge gateway."""
        gw_urn = self._gw_urn(edge_gateway_id)
        data = self._get(self._cloudapi(f"edgeGateways/{gw_urn}"))
        backing = data.get("gatewayBacking") or {}
        if isinstance(backing, dict):
            bid = backing.get("backingId") or backing.get("nsxId") or ""
            if bid:
                return bid
        raise VCDClientError(f"No NSX-V backing ID found for edge gateway {edge_gateway_id}")

    def _nsxv_headers(self) -> dict:
        """Auth headers for the VCD NSX-V proxy (/network/edges/...) — no tenant context."""
        key = self._token_key(self._host, "System")
        token = self._tokens.get(key, "")
        token_type = self._token_types.get(key, "bearer")
        if token_type == "legacy":
            return {"Accept": "application/json", "x-vcloud-authorization": token}
        return {"Accept": "application/json", "Authorization": f"Bearer {token}"}

    def _list_firewall_rules_nsxv(self, nsxv_edge_id: str) -> list:
        """Fetch firewall rules via the VCD NSX-V proxy and normalise to the NSX-T shape."""
        url = f"{self._host}/network/edges/{nsxv_edge_id}/firewall/config"
        resp = self._session.get(url, headers=self._nsxv_headers(), timeout=self.REQUEST_TIMEOUT)
        logger.info("NSX-V FW %s → HTTP %d", url, resp.status_code)
        if resp.status_code not in (200, 201):
            raise VCDClientError(f"NSX-V firewall fetch failed (HTTP {resp.status_code}): {resp.text[:200]}")
        data = resp.json()
        logger.info("NSX-V FW top-level keys: %s", list(data.keys()) if isinstance(data, dict) else type(data).__name__)

        # Response is at top level (no "firewall" wrapper in this VCD version)
        fw = data.get("firewall") if isinstance(data, dict) and "firewall" in data else data

        rules_raw = fw.get("firewallRules") if isinstance(fw, dict) else None
        logger.info("NSX-V FW firewallRules type=%s value=%s",
                    type(rules_raw).__name__, str(rules_raw)[:200] if rules_raw else "None")

        if isinstance(rules_raw, dict):
            rules = rules_raw.get("firewallRule") or []
        elif isinstance(rules_raw, list):
            rules = rules_raw
        else:
            rules = []
        if isinstance(rules, dict):
            rules = [rules]

        logger.info("NSX-V FW found %d user rules", len(rules))
        result = []
        for r in rules:
            if not isinstance(r, dict):
                continue
            src = r.get("source") or {}
            dst = r.get("destination") or {}
            src_ips = src.get("ipAddress") or []
            dst_ips = dst.get("ipAddress") or []
            if isinstance(src_ips, str):
                src_ips = [src_ips]
            if isinstance(dst_ips, str):
                dst_ips = [dst_ips]
            action = (r.get("action") or "").lower()
            # Extract services: application.service[{protocol, port}]
            app = r.get("application") or {}
            services_raw = app.get("service") or []
            if isinstance(services_raw, dict):
                services_raw = [services_raw]
            app_ports = []
            for svc in services_raw:
                proto = svc.get("protocol", "")
                ports = svc.get("port") or svc.get("destinationPort") or []
                if isinstance(ports, str):
                    ports = [ports]
                if proto and ports:
                    app_ports.append(f"{proto}/{','.join(str(p) for p in ports)}")
                elif proto:
                    app_ports.append(proto)
            result.append({
                "id": str(r.get("ruleId") or r.get("id") or ""),
                "name": r.get("name") or r.get("ruleTag") or "",
                "action": "ALLOW" if action == "accept" else "DROP",
                "enabled": r.get("enabled", True),
                "source_groups": src_ips,
                "dest_groups": dst_ips,
                "app_ports": app_ports,
            })

        # Always append the default policy so the user knows the catch-all behaviour
        default = fw.get("defaultPolicy") if isinstance(fw, dict) else None
        if isinstance(default, dict):
            dp_action = (default.get("action") or "").lower()
            result.append({
                "id": "default",
                "name": "Default Policy",
                "action": "ALLOW" if dp_action == "accept" else "DROP",
                "enabled": True,
                "source_groups": ["Any"],
                "dest_groups": ["Any"],
                "app_ports": [],
            })

        return result

    def _list_nat_rules_nsxv(self, nsxv_edge_id: str) -> list:
        """Fetch NAT rules via the VCD NSX-V proxy and normalise to the NSX-T shape."""
        url = f"{self._host}/network/edges/{nsxv_edge_id}/nat/config"
        resp = self._session.get(url, headers=self._nsxv_headers(), timeout=self.REQUEST_TIMEOUT)
        logger.info("NSX-V NAT %s → HTTP %d", url, resp.status_code)
        if resp.status_code not in (200, 201):
            raise VCDClientError(f"NSX-V NAT fetch failed (HTTP {resp.status_code}): {resp.text[:200]}")
        data = resp.json()
        # Structure: {"nat": {"natRules": {"natRule": [...]}}}
        nat = data.get("nat") or data
        rules_wrap = nat.get("natRules") or nat
        rules = rules_wrap.get("natRule") or []
        if isinstance(rules, dict):
            rules = [rules]
        result = []
        for r in rules:
            rule_type = (r.get("ruleType") or r.get("action") or "").upper()
            result.append({
                "id": str(r.get("ruleId") or r.get("id") or ""),
                "name": r.get("description") or r.get("ruleTag") or "",
                "type": rule_type,
                "external_ip": r.get("originalAddress") or r.get("originalIp") or "",
                "internal_ip": r.get("translatedAddress") or r.get("translatedIp") or "",
                "enabled": r.get("enabled", True),
            })
        return result

    def _norm_nsxt_rule(self, r: dict, rule_type: str) -> dict:
        src_raw = r.get("sourceFirewallGroups") or []
        dst_raw = r.get("destinationFirewallGroups") or []
        src_ips = r.get("sourceFirewallIpAddresses") or []
        dst_ips = r.get("destinationFirewallIpAddresses") or []
        return {
            "id": r.get("id"),
            "name": r.get("name"),
            "action": r.get("action"),
            "enabled": r.get("enabled"),
            "rule_type": rule_type,
            "source_groups": [s.get("name") for s in src_raw] + src_ips,
            "dest_groups":   [d.get("name") for d in dst_raw] + dst_ips,
            "app_ports":     [p.get("name") for p in (r.get("applicationPortProfiles") or [])],
            # Kept for enrichment — stripped before returning to callers
            "_src_fw_groups": src_raw,
            "_dst_fw_groups": dst_raw,
            "_src_inline_ips": src_ips,
            "_dst_inline_ips": dst_ips,
        }

    def _enrich_fw_rule_ports(self, rules: list, edge_gateway_id: str) -> None:
        """Best-effort: replace bare profile names in app_ports with 'Name {proto/ports}'."""
        try:
            gw_urn = self._gw_urn(edge_gateway_id)
            try:
                items = self._get_all_pages(
                    self._cloudapi("applicationPortProfiles"),
                    params={"contextEntityId": gw_urn},
                )
            except VCDClientError:
                items = self._get_all_pages(self._cloudapi("applicationPortProfiles"))

            lookup: dict = {}
            for item in items:
                name = item.get("name", "")
                if not name:
                    continue
                parts = []
                for ap in (item.get("applicationPorts") or []):
                    proto = ap.get("protocol", "")
                    ports = ap.get("destinationPorts") or []
                    if ports:
                        parts.append(f"{proto}/{','.join(str(p) for p in ports)}")
                    elif proto:
                        parts.append(proto)
                lookup[name] = f"{name} {{{', '.join(parts)}}}" if parts else name

            for rule in rules:
                rule["app_ports"] = [lookup.get(p, p) for p in (rule.get("app_ports") or [])]
        except Exception as exc:
            logger.debug("FW rule port enrichment failed (non-fatal): %s", exc)

    def _enrich_fw_rule_groups(self, rules: list) -> None:
        """Best-effort: for named groups that are IP sets, append their IPs to the display name."""
        # Collect unique group IDs across all rules
        all_groups: dict = {}  # urn -> {name, id}
        for rule in rules:
            for g in rule.get("_src_fw_groups", []) + rule.get("_dst_fw_groups", []):
                gid = g.get("id", "")
                if gid and gid not in all_groups:
                    all_groups[gid] = g.get("name", "")

        # Fetch each group individually — GET /firewallGroups/{uuid} is often allowed
        # even when the collection GET returns 405
        ip_lookup: dict = {}  # group name -> [ips]
        for gid, gname in list(all_groups.items())[:30]:  # cap at 30 to avoid slow responses
            try:
                uuid = self._to_uuid(gid)
                data = self._get(self._cloudapi(f"firewallGroups/{uuid}"))
                if data.get("type") == "IP_SET":
                    ips = data.get("ipAddresses") or []
                    if ips:
                        ip_lookup[gname] = ips
            except Exception as exc:
                logger.debug("Could not fetch firewallGroup %s (%s): %s", gname, gid, exc)

        # Rebuild source_groups / dest_groups with enriched names, then strip temp fields
        for rule in rules:
            src_fgs = rule.pop("_src_fw_groups", [])
            dst_fgs = rule.pop("_dst_fw_groups", [])
            src_inline = rule.pop("_src_inline_ips", [])
            dst_inline = rule.pop("_dst_inline_ips", [])

            def _display(g: dict) -> str:
                name = g.get("name", "")
                ips = ip_lookup.get(name)
                return f"{name} ({', '.join(ips)})" if ips else name

            rule["source_groups"] = [_display(g) for g in src_fgs] + src_inline
            rule["dest_groups"]   = [_display(g) for g in dst_fgs] + dst_inline

    def list_firewall_rules(self, edge_gateway_id: str) -> list:
        try:
            data = self._get_gw(self._cloudapi(f"edgeGateways/{self._gw_urn(edge_gateway_id)}/firewall/rules"))
            if not isinstance(data, dict):
                return []
            result = [self._norm_nsxt_rule(r, "user")
                      for r in (data.get("userDefinedRules") or [])]
            result += [self._norm_nsxt_rule(r, "system")
                       for r in (data.get("systemRules") or [])]
            self._enrich_fw_rule_groups(result)
            self._enrich_fw_rule_ports(result, edge_gateway_id)
            return result
        except VCDClientError as exc:
            msg = str(exc)
            if "NSX_T" in msg or "GATEWAY_VIEW_FIREWALL_NSX_T" in msg:
                logger.info("NSX-T FW endpoint rejected — trying NSX-V proxy for %s", edge_gateway_id)
                return self._list_firewall_rules_nsxv(self._to_uuid(edge_gateway_id))
            raise

    # ------------------------------------------------------------------
    # Edge Gateway NAT rules
    # ------------------------------------------------------------------

    def list_nat_rules(self, edge_gateway_id: str) -> list:
        try:
            data = self._get_gw(self._cloudapi(f"edgeGateways/{self._gw_urn(edge_gateway_id)}/nat/rules"))
            values = data.get("values", []) if isinstance(data, dict) else []
            return [
                {
                    "id": r.get("id"),
                    "name": r.get("name"),
                    "type": r.get("type"),
                    "external_ip": r.get("externalAddresses"),
                    "internal_ip": r.get("internalAddresses"),
                    "enabled": r.get("enabled"),
                }
                for r in values
            ]
        except VCDClientError as exc:
            msg = str(exc)
            if "NSX_T" in msg or "GATEWAY_NAT_NSX_T" in msg:
                logger.info("NSX-T NAT endpoint rejected — trying NSX-V proxy for %s", edge_gateway_id)
                return self._list_nat_rules_nsxv(self._to_uuid(edge_gateway_id))
            raise

    def list_app_port_profiles(self, edge_gateway_id: str) -> list:
        """Return predefined and custom application port profiles available for a gateway."""
        gw_urn = self._gw_urn(edge_gateway_id)
        try:
            items = self._get_all_pages(
                self._cloudapi("applicationPortProfiles"),
                params={"contextEntityId": gw_urn},
            )
        except VCDClientError:
            items = self._get_all_pages(self._cloudapi("applicationPortProfiles"))
        return [
            {"id": i.get("id"), "name": i.get("name"), "scope": i.get("scope", "")}
            for i in items
            if i.get("name")
        ]

    def _create_app_port_profile(self, rule_name: str, protocol: str, dest_ports: list) -> dict:
        """Create an ApplicationPortProfile and return {id, name}."""
        payload = {
            "name": f"{rule_name}-svc",
            "applicationPorts": [{"protocol": protocol.upper(), "destinationPorts": dest_ports}],
            "scope": "TENANT",
        }
        result = self._post(self._cloudapi("applicationPortProfiles"), payload)
        return {"id": result.get("id"), "name": result.get("name", f"{rule_name}-svc")}

    def _parse_ports(self, ports_str: str) -> list:
        """Split comma-separated port/range string into a list (e.g. '80, 443, 8080-8090')."""
        return [p.strip() for p in ports_str.split(",") if p.strip()]

    def _create_ip_set(self, vdc_group_id: str, name: str, ip_addresses: list) -> dict:
        """Create an IP set in a VDC group and return {id, name}."""
        payload = {"name": name, "ipAddresses": ip_addresses}
        result = self._post(self._cloudapi(f"vdcGroups/{vdc_group_id}/ipSets"), payload)
        return {"id": result.get("id"), "name": result.get("name", name)}

    def _create_ip_set_for_gateway(self, gw_urn: str, name: str, ip_addresses: list) -> dict:
        """Create a firewall group (IP_SET) scoped directly to an edge gateway."""
        payload = {
            "name": name,
            "type": "IP_SET",
            "ipAddresses": ip_addresses,
            "ownerRef": {"id": gw_urn},
        }
        result = self._post(self._cloudapi("firewallGroups"), payload)
        return {"id": result.get("id"), "name": result.get("name", name)}

    def _parse_ips(self, ips_str: str) -> list:
        """Split comma-separated IP/CIDR string into a list, stripping whitespace."""
        return [ip.strip() for ip in ips_str.split(",") if ip.strip()]

    def create_firewall_rule(
        self,
        edge_gateway_id: str,
        rule_name: str,
        action: str,
        source_ips: str = "",
        dest_ips: str = "",
        protocol: str = "",
        dest_ports: str = "",
        enabled: bool = True,
        app_port_profile_id: str = "",
        position: int = 0,
    ) -> dict:
        gw_urn = self._gw_urn(edge_gateway_id)

        app_port_profiles = []
        if app_port_profile_id:
            app_port_profiles = [{"id": app_port_profile_id}]
        else:
            proto_upper = (protocol or "").upper()
            if proto_upper in ("TCP", "UDP") and dest_ports:
                profile = self._create_app_port_profile(rule_name, proto_upper, self._parse_ports(dest_ports))
                if profile.get("id"):
                    app_port_profiles = [profile]

        fw_url = self._cloudapi(f"edgeGateways/{gw_urn}/firewall/rules")
        data = self._get_gw(fw_url)
        existing = data.get("userDefinedRules", []) if isinstance(data, dict) else []

        new_rule = {
            "name": rule_name,
            "action": action.upper(),
            "enabled": enabled,
            "ipProtocol": "IPV4_IPV6",
            "logging": False,
            "sourceFirewallGroups": [],
            "sourceFirewallIpAddresses": self._parse_ips(source_ips) if source_ips else None,
            "destinationFirewallGroups": [],
            "destinationFirewallIpAddresses": self._parse_ips(dest_ips) if dest_ips else None,
            "applicationPortProfiles": app_port_profiles,
        }
        if position and 1 <= position <= len(existing) + 1:
            existing.insert(position - 1, new_rule)
        else:
            existing.append(new_rule)

        payload = {"userDefinedRules": existing}
        try:
            return self._put_gw(fw_url, payload)
        except VCDClientError as exc:
            import re
            m = re.search(
                r"Application Port Profile ([\w-]+) does not exist or is invalid"
                r".*?Firewall Rules? ([^\.,\]]+)",
                str(exc),
            )
            if not m:
                raise
            bad_uuid = m.group(1)
            bad_rule = m.group(2).strip()
            raise VCDClientError(
                f"Cannot add rule — existing rule '{bad_rule}' references a deleted or "
                f"invalid port profile ({bad_uuid}). Please edit or delete that rule in the "
                f"VCD UI to remove the stale service reference, then try again."
            ) from None

    def create_nat_rule(self, edge_gateway_id: str, rule_name: str, rule_type: str,
                        external_ip: str, internal_ip: str, enabled: bool = True) -> dict:
        gw_urn = self._gw_urn(edge_gateway_id)
        payload = {
            "name": rule_name,
            "type": rule_type.upper(),
            "externalAddresses": external_ip,
            "internalAddresses": internal_ip,
            "enabled": enabled,
            "logging": False,
            "priority": 0,
            "snatDestinationAddresses": "",
            "dnatExternalPort": "",
            "internalPort": "",
            "applicationPortProfile": None,
        }
        nat_url = self._cloudapi(f"edgeGateways/{gw_urn}/nat/rules")
        self._ensure_auth()
        body = json.dumps(payload)
        headers_ctx = {**self._base_headers(with_org_context=True), "Content-Type": "application/json"}
        resp = self._session.post(nat_url, headers=headers_ctx, data=body, timeout=self.REQUEST_TIMEOUT)
        if resp.status_code == 403:
            logger.info("NAT POST 403 with org context — retrying as System")
            headers_sys = {**self._base_headers(with_org_context=False), "Content-Type": "application/json"}
            resp = self._session.post(nat_url, headers=headers_sys, data=body, timeout=self.REQUEST_TIMEOUT)
        return self._handle_response(resp, nat_url)

    # ------------------------------------------------------------------
    # NSX DFW
    # ------------------------------------------------------------------

    def list_dfw_policies(self) -> list:
        data = self._get(self._cloudapi("securityPolicies"))
        values = data.get("values", []) if isinstance(data, dict) else []
        return [
            {"id": p.get("id"), "name": p.get("name"), "type": p.get("type")}
            for p in values
        ]

    def list_vdc_groups(self, org_id: str = "") -> list:
        # Fetch all groups; filter client-side by org if given (the ownerRef filter
        # isn't reliable across VCD versions and the list is typically small).
        data = self._get_all_pages(self._cloudapi("vdcGroups"))
        groups = []
        for g in data:
            if org_id:
                owner = g.get("ownerRef") or g.get("orgRef") or {}
                gid = owner.get("id", "") if isinstance(owner, dict) else ""
                if org_id not in gid and org_id != gid:
                    continue
            groups.append({"id": g.get("id"), "name": g.get("name"), "status": g.get("status")})
        return groups

    def get_vdc_group_id(self, name_or_id: str) -> str:
        """Resolve a VDC group name OR URN to its ID."""
        if ":" in name_or_id:
            return name_or_id
        groups = self.list_vdc_groups()
        g = next((x for x in groups if x["name"].lower() == name_or_id.lower()), None)
        if not g:
            raise VCDClientError(
                f"VDC Group '{name_or_id}' not found. Available: {[x['name'] for x in groups]}"
            )
        return g["id"]

    @staticmethod
    def _parse_hw_item(item: dict, disk_index: int) -> dict:
        """Extract disk info from one OVF VirtualHardwareSection item (resourceType 17)."""
        hr = (item.get("hostResource") or item.get("HostResource")
              or item.get("rasd:HostResource") or item.get("ovf:HostResource") or {})
        if isinstance(hr, list):
            hr = hr[0] if hr else {}

        def _hr(key):
            return (hr.get(key) or hr.get(f"ovf:{key}") or hr.get(f"vcloud:{key}")
                    or hr.get(f"rasd:{key}"))

        cap_raw = _hr("capacity")
        try:
            cap_mb = int(float(cap_raw)) if cap_raw is not None else None
        except (ValueError, TypeError):
            cap_mb = None

        return {
            "name": (item.get("elementName") or item.get("ElementName")
                     or item.get("rasd:ElementName") or item.get("description")
                     or f"Disk {disk_index + 1}"),
            "size_mb": cap_mb,
            "bus_type": _hr("busSubType") or _hr("busType"),
            "unit": _hr("diskId"),
            "storage_profile": None,
        }

    def _extract_hw_items(self, container: dict, nics: list, disks: list) -> None:
        """Scan one section/container dict for NIC and disk entries, appending in-place."""
        # NICs
        if "networkConnection" in container:
            ncs = container["networkConnection"]
            if isinstance(ncs, dict):
                ncs = [ncs]
            primary_idx = container.get("primaryNetworkConnectionIndex", 0)
            for i, n in enumerate(ncs):
                idx = n.get("networkConnectionIndex", i)
                nics.append({
                    "index": idx,
                    "network": n.get("network"),
                    "ip": n.get("ipAddress"),
                    "mac": n.get("macAddress"),
                    "mode": n.get("ipAddressAllocationMode"),
                    "adapter": n.get("networkAdapterType"),
                    "connected": n.get("isConnected"),
                    "primary": idx == primary_idx,
                })

        # Disks — try every key name VCD/OVF versions use for hardware item lists
        for key in ("item", "Item", "items", "ovf:Item", "rasdItem", "RasdItem"):
            raw_items = container.get(key)
            if raw_items is None:
                continue
            if isinstance(raw_items, dict):
                raw_items = [raw_items]
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                rt = (item.get("resourceType") or item.get("ResourceType")
                      or item.get("rasd:ResourceType") or item.get("ovf:ResourceType") or "")
                if str(rt) == "17":
                    disks.append(self._parse_hw_item(item, len(disks)))

    @staticmethod
    def _parse_disk_settings(disk_sec) -> list:
        """
        Parse disks from VmSpecSection.diskSection (newer VCD format).
        diskSection can be a dict with a diskSettings/diskSetting list, or a list directly.
        """
        disks = []
        if isinstance(disk_sec, dict):
            settings = (disk_sec.get("diskSettings") or disk_sec.get("diskSetting")
                        or disk_sec.get("disk") or [])
        elif isinstance(disk_sec, list):
            settings = disk_sec
        else:
            return disks
        if isinstance(settings, dict):
            settings = [settings]
        for ds in settings:
            if not isinstance(ds, dict):
                continue
            size_raw = (ds.get("sizeMb") or ds.get("sizeInMb") or ds.get("size")
                        or ds.get("capacityMb"))
            try:
                size_mb = int(float(size_raw)) if size_raw is not None else None
            except (ValueError, TypeError):
                size_mb = None
            # adapterType / storageProfile may be a {name, id} reference object
            def _str_ref(v):
                if isinstance(v, dict):
                    return v.get("name") or v.get("id") or ""
                return v or ""
            sp = ds.get("storageProfile") or {}
            bus_type = _str_ref(ds.get("adapterType") or ds.get("busSubType")
                                 or ds.get("busType") or ds.get("diskType"))
            disks.append({
                "name": ds.get("name") or f"Disk {len(disks) + 1}",
                "size_mb": size_mb,
                "bus_type": bus_type or None,
                "unit": ds.get("unitNumber") if ds.get("unitNumber") is not None else ds.get("diskId"),
                "storage_profile": _str_ref(sp) or None,
            })
        return disks

    def _get_vm_hardware_legacy(self, vm_uuid: str) -> tuple:
        """Return (nics, disks) from the legacy /api/vApp/vm-{uuid} endpoint."""
        nics, disks = [], []
        try:
            vm_data = self._get_legacy(self._api(f"vApp/vm-{vm_uuid}"))
            if not isinstance(vm_data, dict):
                return nics, disks

            sections = vm_data.get("section", [])
            if isinstance(sections, dict):
                sections = [sections]

            for section in (sections or []):
                if not isinstance(section, dict):
                    continue

                # VmSpecSection (newer VCD): disks are under diskSection.diskSettings
                disk_sec = section.get("diskSection")
                if disk_sec:
                    disks.extend(self._parse_disk_settings(disk_sec))

                # NetworkConnectionSection / VirtualHardwareSection items
                self._extract_hw_items(section, nics, disks)

                nested = section.get("virtualHardwareSection") or section.get("VirtualHardwareSection")
                if isinstance(nested, dict):
                    self._extract_hw_items(nested, nics, disks)

            # Fallbacks for older VCD layouts
            vhs = vm_data.get("virtualHardwareSection") or vm_data.get("VirtualHardwareSection")
            if isinstance(vhs, dict):
                self._extract_hw_items(vhs, nics, disks)

            if not disks:
                self._extract_hw_items(vm_data, nics, disks)

            logger.info("hw parse result: %d nics, %d disks", len(nics), len(disks))

        except Exception as e:
            logger.info("legacy VM hardware fetch failed: %s", e)
        return nics, disks

    def _get_vm_disks_subreq(self, vm_uuid: str) -> list:
        """
        Fetch disks via /api/vApp/vm-{uuid}/virtualHardwareSection/disks — the dedicated
        sub-resource that returns only ResourceType-17 items, avoiding full VM parse complexity.
        """
        try:
            data = self._get_legacy(self._api(f"vApp/vm-{vm_uuid}/virtualHardwareSection/disks"))
            if isinstance(data, dict):
                logger.info("disk subreq keys: %s", sorted(data.keys()))
            else:
                logger.info("disk subreq data type: %s", type(data).__name__)
            items = []
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                for k in ("item", "Item", "items", "ovf:Item", "rasdItem"):
                    v = data.get(k)
                    if v is not None:
                        items = [v] if isinstance(v, dict) else (v if isinstance(v, list) else [])
                        if items:
                            break
            if not items and isinstance(data, dict):
                logger.info("disk subreq full data sample: %s", str(data)[:400])
            disks = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                rt = (item.get("resourceType") or item.get("ResourceType")
                      or item.get("rasd:ResourceType") or "")
                if str(rt) not in ("17", ""):
                    continue
                disks.append(self._parse_hw_item(item, len(disks)))
            logger.info("disk subreq found %d disks", len(disks))
            return disks
        except Exception as e:
            logger.info("disk subreq failed: %s", e)
            return []

    def get_vm_details(self, vdc_id: str, vm_name: str, vdc_name: str = "") -> dict:
        """Return full VM details: compute, OS, disks, NICs."""
        vms = self.list_vms(vdc_id, vdc_name)
        vm = next((v for v in vms if v.get("name", "").lower() == vm_name.lower()), None)
        if not vm:
            raise VCDClientError(
                f"VM '{vm_name}' not found. Available: {[v['name'] for v in vms]}"
            )
        uuid = self._to_uuid(vm["id"])

        # NICs + disks from the full legacy VM object
        nics, disks = self._get_vm_hardware_legacy(uuid)
        logger.info("legacy hardware: %d nics, %d disks", len(nics), len(disks))

        # Ensure status is always a string
        vm["status"] = self._vm_status_str(vm.get("status"))

        # Fill cpu/memory if list_vms didn't have them
        if vm.get("cpu") is None or vm.get("memory_mb") is None:
            cpu, mem = self._get_vm_compute(uuid)
            if vm.get("cpu") is None:
                vm["cpu"] = cpu
            if vm.get("memory_mb") is None:
                vm["memory_mb"] = mem

        vm["nics"] = nics if nics else (
            [{"index": 0, "network": vm.get("network_name"), "ip": vm.get("ip_address"),
              "mac": None, "mode": None, "adapter": None, "connected": None, "primary": True}]
            if (vm.get("ip_address") or vm.get("network_name")) else []
        )

        if disks:
            vm["disks"] = disks
        else:
            # Fallback 1: dedicated sub-endpoint (simpler parse, different code path)
            vm["disks"] = self._get_vm_disks_subreq(uuid)

        if not vm["disks"]:
            # Fallback 2: CloudAPI disk endpoint
            try:
                disk_data = self._get(self._cloudapi(f"vms/{uuid}/disks"))
                raw = disk_data if isinstance(disk_data, list) else disk_data.get("values", [])
                logger.info("cloudapi disk fallback: %d records", len(raw))
                vm["disks"] = [
                    {
                        "name": d.get("name"),
                        "size_mb": d.get("sizeInMb") or d.get("sizeMb"),
                        "bus_type": d.get("busSubType") or d.get("busType"),
                        "unit": d.get("unitNumber"),
                        "storage_profile": (d.get("storageProfile") or {}).get("name"),
                    }
                    for d in raw
                ]
            except VCDClientError as e:
                logger.info("cloudapi disk fallback failed: %s", e)
                vm["disks"] = []
        return vm

    def get_network_details(self, network_name: str, vdc_id: Optional[str] = None) -> dict:
        """Return full subnet/DHCP/DNS details for a named network."""
        networks = self.list_networks(vdc_id)
        net = next((n for n in networks if n["name"].lower() == network_name.lower()), None)
        if not net:
            raise VCDClientError(
                f"Network '{network_name}' not found. Available: {[n['name'] for n in networks]}"
            )
        try:
            data = self._get(self._cloudapi(f"orgVdcNetworks/{net['id']}"))
            subnet_values = data.get("subnets", {}).get("values", [])
            net["subnets_detail"] = subnet_values
            connection = data.get("connection")
            if connection:
                router_ref = connection.get("routerRef", {})
                net["connected_edge_id"] = router_ref.get("id")
                net["connected_edge_name"] = router_ref.get("displayName") or router_ref.get("name")
            else:
                net["connected_edge_id"] = None
                net["connected_edge_name"] = None
        except VCDClientError:
            net["subnets_detail"] = []
            net["connected_edge_id"] = None
            net["connected_edge_name"] = None
        return net

    def edit_vm(self, vdc_id: str, vm_name: str, cpu: int = None, memory_mb: int = None, vdc_name: str = "") -> dict:
        vm_id = self.get_vm_id(vdc_id, vm_name, vdc_name)
        return self.update_vm_compute(vm_id, cpu=cpu, memory_mb=memory_mb)

    def _auth_headers(self) -> dict:
        """Return auth + org-context headers for legacy API calls."""
        self._ensure_auth()
        key = self._token_key(self._host, "System")
        token = self._tokens.get(key, "")
        token_type = self._token_types.get(key, "bearer")
        accept = f"application/*+json;version={self.API_VERSION}"
        if token_type == "legacy":
            h = {"x-vcloud-authorization": token, "Accept": accept}
        else:
            h = {"Authorization": f"Bearer {token}", "Accept": accept}
        h.update(self._org_context_header())
        return h

    def _resize_disk_by_uuid(self, vm_uuid: str, disk_index: int, new_size_gb: int) -> dict:
        """Resize a VM disk by 1-based index using VM UUID directly."""
        new_size_mb = new_size_gb * 1024
        headers = self._auth_headers()
        url = self._api(f"vApp/vm-{vm_uuid}/virtualHardwareSection/disks")
        resp = self._session.get(url, headers=headers, timeout=self.REQUEST_TIMEOUT)
        data = self._handle_response(resp, url)

        items = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            for k in ("item", "Item", "items", "ovf:Item", "rasdItem"):
                v = data.get(k)
                if v is not None:
                    items = [v] if isinstance(v, dict) else (v if isinstance(v, list) else [])
                    if items:
                        break

        disk_items = [i for i in items if isinstance(i, dict)
                      and str(i.get("rasd:ResourceType") or i.get("ResourceType") or "") == "17"]

        if not disk_items:
            raise VCDClientError("No disks found on this VM.")
        if disk_index < 1 or disk_index > len(disk_items):
            raise VCDClientError(f"Disk {disk_index} not found — VM has {len(disk_items)} disk(s).")

        target = disk_items[disk_index - 1]
        hr = (target.get("rasd:HostResource") or target.get("HostResource") or
              target.get("ovf:HostResource") or {})
        if isinstance(hr, list):
            hr = hr[0] if hr else {}

        for cap_key in ("vcloud:capacity", "capacity", "ovf:capacity"):
            if cap_key in hr:
                try:
                    current_mb = int(float(hr[cap_key]))
                    if new_size_mb < current_mb:
                        raise VCDClientError(
                            f"Cannot shrink disk from {current_mb // 1024} GB to {new_size_gb} GB."
                        )
                    hr[cap_key] = str(new_size_mb)
                except (ValueError, TypeError):
                    pass
                break

        headers["Content-Type"] = "application/vnd.vmware.vcloud.rasdItemsList+json"
        put_resp = self._session.put(url, headers=headers,
                                     data=json.dumps(data), timeout=self.REQUEST_TIMEOUT)
        self._handle_response(put_resp, url)
        return {"ok": True, "disk": disk_index, "new_size_gb": new_size_gb}

    def _configure_vm_nics_by_uuid(self, vm_uuid: str, nic_networks: dict,
                                    ip_allocation_mode: str = "POOL",
                                    ip_address: str = "") -> None:
        """Set NIC network, IP allocation mode, and optional static IP."""
        headers = self._auth_headers()
        url = self._api(f"vApp/vm-{vm_uuid}/networkConnectionSection")
        resp = self._session.get(url, headers=headers, timeout=self.REQUEST_TIMEOUT)
        data = self._handle_response(resp, url)

        ncs = data.get("networkConnection") or []
        if isinstance(ncs, dict):
            ncs = [ncs]

        # If VCD returned no NICs yet, seed one entry per requested network
        if not ncs:
            for idx in sorted(nic_networks.keys()):
                ncs.append({"networkConnectionIndex": idx, "isConnected": True})

        for nc in ncs:
            idx = int(nc.get("networkConnectionIndex", 0))
            if idx in nic_networks:
                nc["network"] = nic_networks[idx]
                nc["ipAllocationMode"] = ip_allocation_mode
                nc["isConnected"] = True
                if ip_allocation_mode == "MANUAL" and ip_address:
                    nc["ipAddress"] = ip_address

        data["networkConnection"] = ncs
        headers["Content-Type"] = "application/vnd.vmware.vcloud.networkConnectionSection+json"
        put_resp = self._session.put(url, headers=headers,
                                     data=json.dumps(data), timeout=self.REQUEST_TIMEOUT)
        self._handle_response(put_resp, url)
        self._wait_for_task(put_resp)

    def resize_vm_disk(self, vdc_id: str, vm_name: str, disk_index: int, new_size_gb: int, vdc_name: str = "") -> dict:
        """Resize a VM disk by 1-based index. new_size_gb must be >= current size."""
        vm_id = self.get_vm_id(vdc_id, vm_name, vdc_name)
        vm_uuid = self._to_uuid(vm_id)
        return self._resize_disk_by_uuid(vm_uuid, disk_index, new_size_gb)

    def create_network(self, vdc_id: str, name: str, network_type: str = "ISOLATED",
                       gateway: str = "", prefix_length: int = 24,
                       dns1: str = "", dns2: str = "",
                       dhcp_start: str = "", dhcp_end: str = "",
                       edge_gateway_id: str = "") -> dict:
        subnet: dict = {
            "gateway": gateway,
            "prefixLength": int(prefix_length),
            "dnsServer1": dns1,
            "dnsServer2": dns2,
            "ipRanges": {"values": [{"startAddress": dhcp_start, "endAddress": dhcp_end}]} if dhcp_start and dhcp_end else {"values": []},
        }
        payload: dict = {
            "name": name,
            "networkType": network_type.upper(),
            "ownerRef": {"id": vdc_id},
            "subnets": {"values": [subnet]},
        }
        if edge_gateway_id and network_type.upper() == "NAT_ROUTED":
            payload["connection"] = {
                "routerRef": {"id": edge_gateway_id},
                "connectionType": "INTERNAL",
            }
        return self._post(self._cloudapi("orgVdcNetworks"), payload)

    def edit_network(self, network_name: str, vdc_id: Optional[str] = None,
                     gateway: str = None, prefix_length: int = None,
                     dns1: str = None, dns2: str = None,
                     disconnect_edge: bool = False,
                     connect_edge_id: str = None) -> dict:
        networks = self.list_networks(vdc_id)
        net = next((n for n in networks if n["name"].lower() == network_name.lower()), None)
        if not net:
            raise VCDClientError(f"Network '{network_name}' not found. Available: {[n['name'] for n in networks]}")
        data = self._get(self._cloudapi(f"orgVdcNetworks/{net['id']}"))
        subnets = data.get("subnets", {}).get("values", [])
        subnet = (subnets[0] if subnets else {}).copy()
        # Some VCD versions return empty gateway in the full GET — restore from list endpoint
        if not subnet.get("gateway") and net.get("gateway"):
            subnet["gateway"] = net["gateway"]
        if not subnet.get("prefixLength") and net.get("prefix_length"):
            subnet["prefixLength"] = net["prefix_length"]
        if gateway is not None:
            subnet["gateway"] = gateway
        if prefix_length is not None:
            subnet["prefixLength"] = int(prefix_length)
        if dns1 is not None:
            subnet["dnsServer1"] = dns1
        if dns2 is not None:
            subnet["dnsServer2"] = dns2
        if not subnet.get("gateway"):
            raise VCDClientError(
                "This network has no gateway configured. "
                "Please provide a 'New Gateway IP' to set one."
            )
        data["subnets"] = {"values": [subnet]}
        if connect_edge_id:
            gw_urn = self._gw_urn(connect_edge_id)
            data["connection"] = {"routerRef": {"id": gw_urn}, "connectionType": "INTERNAL"}
            data["networkType"] = "NAT_ROUTED"
        elif disconnect_edge:
            data.pop("connection", None)
            data["networkType"] = "ISOLATED"
        return self._put(self._cloudapi(f"orgVdcNetworks/{net['id']}"), data)

    def list_vm_snapshots(self, vdc_id: str, vm_name: str, vdc_name: str = "") -> list:
        vm_id = self.get_vm_id(vdc_id, vm_name, vdc_name)
        vm_uuid = self._to_uuid(vm_id)
        data = self._get_legacy(self._api(f"vApp/vm-{vm_uuid}/snapshotSection"))
        snapshots = data.get("snapshot") or []
        if isinstance(snapshots, dict):
            snapshots = [snapshots]
        return [
            {
                "name": s.get("name") or "Snapshot",
                "created": s.get("created") or "",
                "memory": s.get("memory", False),
                "size_mb": round(int(s.get("size", 0)) / (1024 * 1024), 1) if s.get("size") else None,
            }
            for s in snapshots
        ]

    def create_vm_snapshot(self, vdc_id: str, vm_name: str, snapshot_name: str = "", vdc_name: str = "") -> dict:
        vm_id = self.get_vm_id(vdc_id, vm_name, vdc_name)
        vm_uuid = self._to_uuid(vm_id)
        name = snapshot_name or f"snap-{int(time.time())}"
        xml_body = (
            f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<CreateSnapshotParams name="{name}" memory="false" quiesce="false"'
            f' xmlns="http://www.vmware.com/vcloud/v1.5">'
            f'<Description>Created via Infra Assistant</Description>'
            f'</CreateSnapshotParams>'
        )
        self._ensure_auth()
        key = self._token_key(self._host, "System")
        token = self._tokens.get(key, "")
        token_type = self._token_types.get(key, "bearer")
        accept = f"application/*+json;version={self.API_VERSION}"
        if token_type == "legacy":
            headers = {"x-vcloud-authorization": token, "Accept": accept}
        else:
            headers = {"Authorization": f"Bearer {token}", "Accept": accept}
        headers["Content-Type"] = "application/vnd.vmware.vcloud.createSnapshotParams+xml"
        headers.update(self._org_context_header())
        url = self._api(f"vApp/vm-{vm_uuid}/action/createSnapshot")
        resp = self._session.post(url, headers=headers, data=xml_body.encode(), timeout=self.REQUEST_TIMEOUT)
        if resp.status_code in (200, 201, 202):
            return {"ok": True, "name": name}
        try:
            detail = resp.json().get("message", resp.text[:200])
        except Exception:
            detail = resp.text[:200]
        raise VCDClientError(f"Create snapshot failed (HTTP {resp.status_code}): {detail}")

    def revert_vm_snapshot(self, vdc_id: str, vm_name: str, vdc_name: str = "") -> dict:
        vm_id = self.get_vm_id(vdc_id, vm_name, vdc_name)
        vm_uuid = self._to_uuid(vm_id)
        return self._post_action(self._api(f"vApp/vm-{vm_uuid}/action/revertToCurrentSnapshot"))

    def _get_child_vm_hrefs_from_template(self, template_href: str) -> list:
        """Return the href for each child VM inside a vApp template.

        VCD returns XML (not JSON) for vAppTemplate GETs, so _get_legacy silently
        returns {} here.  We issue a raw XML-accept GET and regex-parse the Vm hrefs.
        """
        import re as _re
        headers = self._auth_headers()
        headers["Accept"] = f"application/*+xml;version={self.API_VERSION}"
        try:
            resp = self._session.get(template_href, headers=headers, timeout=self.REQUEST_TIMEOUT)
            if resp.status_code == 200:
                # Match <Vm ... href="..."> inside the Children block
                hrefs = _re.findall(r'<(?:\w+:)?Vm\b[^>]*\bhref="([^"]+)"', resp.text)
                if hrefs:
                    return hrefs
        except Exception:
            pass

        # Fallback: query API (works when the direct GET returns no children)
        try:
            params = {
                "type": "vm",
                "format": "records",
                "pageSize": "128",
                "filter": f"isVAppTemplate==true;container=={template_href}",
            }
            data = self._get_legacy(self._api("query"), params=params)
            records = data.get("record", []) if isinstance(data, dict) else []
            if isinstance(records, dict):
                records = [records]
            hrefs = [r.get("href", "") for r in records if r.get("href")]
            if hrefs:
                return hrefs
        except VCDClientError:
            pass

        return []

    def _find_vapp_href(self, vapp_name: str, vdc_name: str = "", require_ready: bool = False) -> str:
        """Return the full API href for a vApp by name.

        If require_ready=True, only returns once the vApp status is not UNRESOLVED/PENDING
        (i.e. VCD has finished provisioning it and it accepts further operations).
        """
        NOT_READY = {"UNRESOLVED", "PENDING", "UNKNOWN", "INCONSISTENT_STATE", ""}
        f = f"name=={vapp_name}"
        if vdc_name:
            f += f";vdcName=={vdc_name}"
        try:
            data = self._get_legacy(self._api("query"), params={"type": "adminVApp", "format": "records", "pageSize": "128", "filter": f})
            records = data.get("record", []) if isinstance(data, dict) else []
            if isinstance(records, dict):
                records = [records]
            for r in records:
                href = r.get("href", "")
                status = (r.get("status") or "").upper()
                if href:
                    if require_ready and status in NOT_READY:
                        return ""   # found but not ready yet — caller should retry
                    return href
        except VCDClientError:
            pass
        return ""

    def _get_vm_ids_by_vapp_href(self, vapp_href: str) -> list:
        """Return VM URNs for all live VMs in a vApp by parsing the vApp XML directly."""
        import re as _re
        try:
            headers = self._auth_headers()
            headers["Accept"] = f"application/*+xml;version={self.API_VERSION}"
            resp = self._session.get(vapp_href, headers=headers, timeout=self.REQUEST_TIMEOUT)
            if resp.status_code != 200:
                logger.warning("_get_vm_ids_by_vapp_href: GET %s returned %d", vapp_href, resp.status_code)
                return []
            hrefs = _re.findall(r'<(?:\w+:)?Vm\b[^>]*\bhref="([^"]+/vApp/vm-[^"]+)"', resp.text)
            ids = []
            for h in hrefs:
                seg = h.rsplit("/", 1)[-1]
                uuid = seg[3:] if seg.startswith("vm-") else seg
                if uuid:
                    ids.append(f"urn:vcloud:vm:{uuid}")
            logger.info("_get_vm_ids_by_vapp_href: %s found %d VMs: %s", vapp_href, len(ids), ids)
            return ids
        except Exception as exc:
            logger.warning("_get_vm_ids_by_vapp_href: failed for %s: %s", vapp_href, exc)
            return []

    def _rename_vm(self, vm_id: str, new_name: str) -> None:
        """Rename a VM via the legacy /api/vApp/vm-{uuid} endpoint."""
        import time as _t
        vm_uuid = self._to_uuid(vm_id)
        url = self._api(f"vApp/vm-{vm_uuid}")
        last_exc = None
        for attempt in range(2):
            try:
                vm = self._get_legacy(url)
                if not vm:
                    raise VCDClientError("empty VM response")
                if vm.get("name") == new_name:
                    return
                vm["name"] = new_name
                headers = self._auth_headers()
                headers["Content-Type"] = f"application/vnd.vmware.vcloud.vm+json;version={self.API_VERSION}"
                put_resp = self._session.put(url, headers=headers, data=json.dumps(vm), timeout=self.REQUEST_TIMEOUT)
                logger.info("_rename_vm: PUT %s -> HTTP %d", url, put_resp.status_code)
                if put_resp.status_code not in (200, 201, 202):
                    raise VCDClientError(f"PUT returned {put_resp.status_code}: {put_resp.text[:200]}")
                self._wait_for_task(put_resp)
                return
            except Exception as exc:
                last_exc = exc
                logger.warning("VM rename attempt %d for '%s' failed: %s", attempt + 1, new_name, exc)
                if attempt < 1:
                    _t.sleep(5)
        raise VCDClientError(f"VM rename to '{new_name}' failed: {last_exc}")

    @staticmethod
    def _xml_escape(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    def _build_sourced_items(self, vm_hrefs: list, vm_names: list) -> str:
        """Build <SourcedItem> XML fragments pairing child-VM hrefs with requested names."""
        items = []
        for i, name in enumerate(vm_names):
            if not name:
                continue
            href = vm_hrefs[i] if i < len(vm_hrefs) else (vm_hrefs[0] if vm_hrefs else "")
            if href:
                safe_name = self._xml_escape(name)
                items.append(
                    f'<SourcedItem>'
                    f'<Source href="{href}" name="{safe_name}"/>'
                    f'<VmGeneralParams>'
                    f'<Name>{safe_name}</Name>'
                    f'<NeedsCustomization>false</NeedsCustomization>'
                    f'</VmGeneralParams>'
                    f'</SourcedItem>'
                )
        return "".join(items)

    def _xml_post(self, url: str, xml_body: str, content_type: str, timeout: int = 60):
        headers = self._auth_headers()
        headers["Content-Type"] = content_type
        resp = self._session.post(url, headers=headers, data=xml_body.encode(), timeout=timeout)
        return resp

    def deploy_vm_from_template(self, vdc_id: str, vapp_name: str, template_name: str,
                                catalog_name: str = "", vm_names: list = None,
                                existing_vapp: bool = False,
                                cpu: int = None, memory_mb: int = None,
                                network_name: str = "", network2_name: str = "",
                                ip_allocation_mode: str = "POOL", ip_addresses: list = None,
                                disk_sizes: list = None, vdc_name: str = "",
                                per_vm_configs: list = None) -> dict:
        # Resolve template href
        q_params = {"type": "vAppTemplate", "format": "records", "pageSize": "128",
                    "filter": f"name=={template_name}" + (f";catalogName=={catalog_name}" if catalog_name else "")}
        q_data = self._get_legacy(self._api("query"), params=q_params)
        records = q_data.get("record", []) if isinstance(q_data, dict) else []
        if not records:
            raise VCDClientError(f"Template '{template_name}' not found" + (f" in catalog '{catalog_name}'" if catalog_name else ""))
        template_href = records[0].get("href", "")
        if not template_href:
            raise VCDClientError(f"Template href missing for '{template_name}'")

        names = vm_names or []
        result: dict = {"ok": True, "vapp_name": vapp_name, "vm_count": len(names) or 1}

        if existing_vapp:
            # Add VMs to an existing vApp via recomposeVApp
            vapps = self.list_vapps(vdc_id, vdc_name)
            vapp = next((v for v in vapps if v["name"].lower() == vapp_name.lower()), None)
            if not vapp:
                raise VCDClientError(f"vApp '{vapp_name}' not found.")
            vapp_uuid = self._to_uuid(vapp["id"])
            child_vm_hrefs = self._get_child_vm_hrefs_from_template(template_href)
            sourced = self._build_sourced_items(child_vm_hrefs, names) if names else \
                      (f'<SourcedItem><Source href="{child_vm_hrefs[0]}"/></SourcedItem>' if child_vm_hrefs else
                       f'<SourcedItem><Source href="{template_href}"/></SourcedItem>')
            xml_body = (
                f'<?xml version="1.0" encoding="UTF-8"?>'
                f'<RecomposeVAppParams xmlns="http://www.vmware.com/vcloud/v1.5">'
                f'{sourced}'
                f'</RecomposeVAppParams>'
            )
            resp = self._xml_post(
                self._api(f"vApp/vapp-{vapp_uuid}/action/recomposeVApp"),
                xml_body,
                "application/vnd.vmware.vcloud.recomposeVAppParams+xml",
            )
            if resp.status_code not in (200, 201, 202):
                try:
                    detail = resp.json().get("message", resp.text[:300])
                except Exception:
                    detail = resp.text[:300]
                raise VCDClientError(f"Recompose vApp failed (HTTP {resp.status_code}): {detail}")
            result["existing_vapp"] = True
        else:
            # Create new vApp from template
            import time as _time
            vdc_uuid = self._to_uuid(vdc_id)
            child_vm_hrefs = self._get_child_vm_hrefs_from_template(template_href)

            # SourcedItem clones the same source VM multiple times in one transaction,
            # causing a vapp_scoped_vm_id uniqueness violation in VCD's DB.
            # Safe path: always instantiate with <Source> only (one transaction = no clash),
            # then add extra VMs via sequential recomposeVApp calls.
            if names and len(child_vm_hrefs) >= len(names):
                # Template has enough distinct VM slots — SourcedItem is safe here
                sourced = self._build_sourced_items(child_vm_hrefs, names)
                xml_body = (
                    f'<?xml version="1.0" encoding="UTF-8"?>'
                    f'<InstantiateVAppTemplateParams name="{vapp_name}" deploy="false" powerOn="false"'
                    f' xmlns="http://www.vmware.com/vcloud/v1.5"'
                    f' xmlns:ovf="http://schemas.dmtf.org/ovf/envelope/1">'
                    f'<Source href="{template_href}"/>'
                    f'{sourced}'
                    f'</InstantiateVAppTemplateParams>'
                )
            else:
                xml_body = (
                    f'<?xml version="1.0" encoding="UTF-8"?>'
                    f'<InstantiateVAppTemplateParams name="{vapp_name}" deploy="false" powerOn="false"'
                    f' xmlns="http://www.vmware.com/vcloud/v1.5"'
                    f' xmlns:ovf="http://schemas.dmtf.org/ovf/envelope/1">'
                    f'<Source href="{template_href}"/>'
                    f'</InstantiateVAppTemplateParams>'
                )
            resp = self._xml_post(
                self._api(f"vdc/{vdc_uuid}/action/instantiateVAppTemplate"),
                xml_body,
                "application/vnd.vmware.vcloud.instantiateVAppTemplateParams+xml",
                timeout=60,
            )
            if resp.status_code not in (200, 201, 202):
                try:
                    detail = resp.json().get("message", resp.text[:300])
                except Exception:
                    detail = resp.text[:300]
                raise VCDClientError(f"Deploy failed (HTTP {resp.status_code}): {detail}")

            # Extract the vApp href directly from the response XML — this identifies the
            # exact vApp we just created, avoiding collisions with older vApps of the same name.
            import re as _re
            _NOT_READY = {"UNRESOLVED", "PENDING", "UNKNOWN", "INCONSISTENT_STATE", ""}
            _id_m = _re.search(
                r'/(vapp-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})',
                resp.text, _re.I,
            )
            initial_vapp_href = self._api(f"vApp/{_id_m.group(1)}") if _id_m else ""
            logger.info("deploy: initial_vapp_href=%r (from response regex)", initial_vapp_href)

            # Wait for the vApp to leave UNRESOLVED state; recompose is rejected otherwise.
            vapp_href = ""
            for wait in (6, 8, 10, 12, 15, 20):
                _time.sleep(wait)
                if initial_vapp_href:
                    try:
                        _qd = self._get_legacy(self._api("query"), params={
                            "type": "adminVApp", "format": "records", "pageSize": "5",
                            "filter": f"href=={initial_vapp_href}",
                        })
                        _recs = _qd.get("record", []) if isinstance(_qd, dict) else []
                        if isinstance(_recs, dict):
                            _recs = [_recs]
                        logger.info("deploy: RESOLVED poll (href query) -> %d records", len(_recs))
                        for _r in _recs:
                            _st = (_r.get("status") or "").upper()
                            if _r.get("href") and _st not in _NOT_READY:
                                vapp_href = _r["href"]
                                break
                    except Exception as _e:
                        logger.warning("deploy: RESOLVED poll (href query) exception: %s", _e)
                if not vapp_href:
                    vapp_href = self._find_vapp_href(vapp_name, vdc_name, require_ready=True)
                logger.info("deploy: after wait=%ds vapp_href=%r", wait, vapp_href)
                if vapp_href:
                    break

            # Wait for the initial VM(s) to appear in the query results before recomposing.
            # VCD can report RESOLVED but still hold a lock (BusyEntityException) until
            # background provisioning fully commits the VMs to the database.
            if vapp_href:
                for _vm_wait in (6, 8, 12, 15, 20):
                    _time.sleep(_vm_wait)
                    if self._get_vm_ids_by_vapp_href(vapp_href):
                        break

            # If the template has fewer VMs than requested, add extras via sequential recompose.
            num_template_vms = len(child_vm_hrefs) if child_vm_hrefs else 1
            extra_names = names[num_template_vms:] if names and len(names) > num_template_vms else []
            recompose_failures = []
            if extra_names and child_vm_hrefs and vapp_href:
                seg = vapp_href.rsplit("/", 1)[-1]          # "vapp-{uuid}"
                vapp_uuid_r = seg[5:] if seg.startswith("vapp-") else seg
                source_href = child_vm_hrefs[0]
                for extra_name in extra_names:
                    safe = self._xml_escape(extra_name)
                    extra_xml = (
                        f'<?xml version="1.0" encoding="UTF-8"?>'
                        f'<RecomposeVAppParams xmlns="http://www.vmware.com/vcloud/v1.5">'
                        f'<SourcedItem>'
                        f'<Source href="{source_href}" name="{safe}"/>'
                        f'<VmGeneralParams>'
                        f'<Name>{safe}</Name>'
                        f'<NeedsCustomization>false</NeedsCustomization>'
                        f'</VmGeneralParams>'
                        f'</SourcedItem>'
                        f'</RecomposeVAppParams>'
                    )
                    recomposed = False
                    for _attempt in range(3):
                        try:
                            rc = self._xml_post(
                                self._api(f"vApp/vapp-{vapp_uuid_r}/action/recomposeVApp"),
                                extra_xml,
                                "application/vnd.vmware.vcloud.recomposeVAppParams+xml",
                            )
                            if rc.status_code in (200, 201, 202):
                                _time.sleep(12)  # recompose is async; give VCD time to finish
                                recomposed = True
                                break
                            if "busy" in rc.text.lower() or "BusyEntityException" in rc.text:
                                logger.warning("vApp busy, retrying recompose for '%s' (attempt %d)", extra_name, _attempt + 1)
                                _time.sleep(10)
                            else:
                                logger.warning("Recompose for extra VM '%s' failed: %s", extra_name, rc.text[:200])
                                break
                        except Exception as exc:
                            logger.warning("Recompose for '%s' exception: %s", extra_name, exc)
                            break
                    if not recomposed:
                        recompose_failures.append(extra_name)
            elif extra_names:
                recompose_failures.extend(extra_names)

            # Adjust reported VM count to reflect what actually got created
            actual_vm_count = (len(names) if names else 1) - len(recompose_failures)
            result["vm_count"] = actual_vm_count
            if recompose_failures:
                result["recompose_warning"] = f"These VMs could not be added: {', '.join(recompose_failures)}"

        # Post-deploy configuration (CPU, memory, disks, NICs) — new vApp only
        nic_networks = {}
        if network_name:
            nic_networks[0] = network_name
        if network2_name:
            nic_networks[1] = network2_name

        needs_post = (names or cpu or memory_mb or disk_sizes or nic_networks or per_vm_configs) and not existing_vapp
        if needs_post:
            import time
            try:
                # vapp_href was found above; if it's still empty the deploy itself had issues
                if not vapp_href:
                    vapp_href = self._find_vapp_href(vapp_name, vdc_name)
                if vapp_href:
                    expected = (len(names) if names else 1) - len(recompose_failures)
                    vm_ids = []
                    for wait in (6, 8, 10, 12, 15):
                        time.sleep(wait)
                        vm_ids = self._get_vm_ids_by_vapp_href(vapp_href)
                        if len(vm_ids) >= expected:
                            break
                    # Extra wait so recompose tasks fully commit before any configure calls.
                    # VCD holds a lock on all VMs in the vApp until the task finishes (~25-40s).
                    if extra_names:
                        time.sleep(20)
                    if vm_ids:
                        post_warnings = []
                        for i, vm_id in enumerate(vm_ids):
                            vm_uuid = self._to_uuid(vm_id)
                            if names and i < len(names) and names[i]:
                                try:
                                    self._rename_vm(vm_id, names[i])
                                except Exception as e:
                                    post_warnings.append(f"VM {i+1} rename: {e}")
                            if per_vm_configs and i < len(per_vm_configs):
                                pvc = per_vm_configs[i]
                                vm_cpu = pvc.get("cpu") or cpu
                                vm_mem = pvc.get("memory_mb") or memory_mb
                                vm_disks = pvc.get("disk_sizes") if pvc.get("disk_sizes") is not None else disk_sizes
                            else:
                                vm_cpu, vm_mem, vm_disks = cpu, memory_mb, disk_sizes
                            if vm_cpu or vm_mem:
                                try:
                                    self.update_vm_compute(vm_id, cpu=vm_cpu, memory_mb=vm_mem)
                                except Exception as e:
                                    post_warnings.append(f"VM {i+1} compute: {e}")
                            for idx, size_gb in (vm_disks or []):
                                try:
                                    self._resize_disk_by_uuid(vm_uuid, idx, size_gb)
                                except VCDClientError as e:
                                    post_warnings.append(f"VM {i+1} disk {idx}: {e}")
                            if nic_networks:
                                try:
                                    ip_list = ip_addresses or []
                                    vm_ip = ip_list[i] if i < len(ip_list) else (ip_list[0] if ip_list else "")
                                    self._configure_vm_nics_by_uuid(vm_uuid, nic_networks, ip_allocation_mode, vm_ip)
                                except VCDClientError as e:
                                    post_warnings.append(f"VM {i+1} NIC: {e}")
                        result["post_configured"] = not post_warnings
                        if post_warnings:
                            result["configure_warning"] = "Some post-deploy steps failed: " + "; ".join(post_warnings)
                    else:
                        result["configure_warning"] = "vApp deployed but VMs were not yet visible after waiting — CPU/disk/NIC/rename were not applied."
                else:
                    result["configure_warning"] = "Could not locate the new vApp after deploy — post-deploy configuration was skipped."
            except Exception as exc:
                logger.warning("Post-deploy configure failed (non-fatal): %s", exc)
                result["configure_warning"] = f"Post-deploy configuration failed: {exc}"

        return result

    def _get_vm_ids_from_vapp(self, vapp_id: str) -> list:
        """Return urn:vcloud:vm IDs for all VMs inside a vApp."""
        vapp_uuid = self._to_uuid(vapp_id)

        # Primary: parse children from the vApp GET
        try:
            data = self._get_legacy(self._api(f"vApp/{vapp_uuid}"))
            children = data.get("children") or data.get("Children") or {}
            raw_vms = children.get("vm") or children.get("Vm") or []
            if isinstance(raw_vms, dict):
                raw_vms = [raw_vms]
            ids = []
            for vm in raw_vms:
                # Prefer the id URN directly (newer VCD JSON responses)
                vm_id = vm.get("id", "")
                if vm_id.startswith("urn:vcloud:vm:"):
                    ids.append(vm_id)
                    continue
                href = vm.get("href", "")
                segment = href.rsplit("/", 1)[-1]
                uuid = segment[3:] if segment.startswith("vm-") else segment
                if uuid:
                    ids.append(f"urn:vcloud:vm:{uuid}")
            if ids:
                return ids
        except VCDClientError:
            pass

        # Fallback: parse VM hrefs from the vApp XML directly (container== filter
        # is not supported for tenant users with the 'vm' query type)
        vapp_href = self._api(f"vApp/vapp-{vapp_uuid}")
        return self._get_vm_ids_by_vapp_href(vapp_href)

    def _post_action(self, url: str) -> dict:
        """POST to a VCD power/action endpoint (no request body)."""
        self._ensure_auth()
        key = self._token_key(self._host, "System")
        token = self._tokens.get(key, "")
        token_type = self._token_types.get(key, "bearer")
        accept = f"application/*+json;version={self.API_VERSION}"
        if token_type == "legacy":
            headers = {"Accept": accept, "x-vcloud-authorization": token}
        else:
            headers = {"Accept": accept, "Authorization": f"Bearer {token}"}
        headers.update(self._org_context_header())
        resp = self._session.post(url, headers=headers, timeout=self.REQUEST_TIMEOUT)
        if resp.status_code == 401:
            self.authenticate(self._active_org)
            resp = self._session.post(url, headers=headers, timeout=self.REQUEST_TIMEOUT)
        if resp.status_code in (200, 201, 202):
            return {"ok": True}
        try:
            detail = resp.json().get("message", resp.text[:200])
        except Exception:
            detail = resp.text[:200]
        raise VCDClientError(f"Action failed (HTTP {resp.status_code}): {detail}")

    def power_vm(self, vm_id: str, action: str) -> dict:
        """action: poweron, poweroff, reset"""
        vm_uuid = self._to_uuid(vm_id)
        return self._post_action(self._api(f"vApp/vm-{vm_uuid}/power/action/{action}"))

    def power_vapp(self, vapp_id: str, action: str) -> dict:
        """action: poweron, poweroff. vapp_id is like 'vapp-uuid' from list_vapps."""
        if not vapp_id.startswith("vapp-"):
            vapp_id = f"vapp-{self._to_uuid(vapp_id)}"
        return self._post_action(self._api(f"vApp/{vapp_id}/power/action/{action}"))

    def get_vapp_details(self, vdc_id: str, vapp_name: str, vdc_name: str = "") -> dict:
        """Return vApp info with child VMs and connected networks."""
        vapps = self.list_vapps(vdc_id, vdc_name)
        vapp = next((v for v in vapps if v["name"].lower() == vapp_name.lower()), None)
        if not vapp:
            available = [v["name"] for v in vapps]
            raise VCDClientError(f"vApp '{vapp_name}' not found. Available: {available}")

        vapp_href = f"{self._host}/api/vApp/{vapp['id']}"

        # Child VMs — use vApp XML GET (container== filter unsupported for tenant vm query type)
        try:
            vm_ids = self._get_vm_ids_by_vapp_href(vapp_href)
            vms_out = []
            for vm_id in vm_ids:
                vm_uuid = self._to_uuid(vm_id)
                try:
                    vm_data = self._get_legacy(self._api(f"vApp/vm-{vm_uuid}"))
                    cpu, mem = self._get_vm_compute(vm_uuid)
                    vms_out.append({
                        "name": vm_data.get("name"),
                        "status": self._vm_status_str(vm_data.get("status")),
                        "cpu": cpu,
                        "memory_mb": mem,
                    })
                except Exception as ve:
                    logger.warning("get_vapp_details: VM %s detail failed: %s", vm_id, ve)
                    vms_out.append({"name": vm_id, "status": None, "cpu": None, "memory_mb": None})
            vapp["vms"] = vms_out
        except Exception as e:
            logger.info("vapp VMs fetch failed: %s", e)
            vapp["vms"] = []

        # Connected networks — parse NetworkConfigSection from the vApp XML
        try:
            import xml.etree.ElementTree as ET
            self._ensure_auth()
            key = self._token_key(self._host, "System")
            token = self._tokens.get(key, "")
            token_type = self._token_types.get(key, "bearer")
            headers = {
                "Accept": f"application/*+xml;version={self.API_VERSION}",
                "x-vcloud-authorization" if token_type == "legacy" else "Authorization":
                    token if token_type == "legacy" else f"Bearer {token}",
            }
            resp = self._session.get(
                f"{vapp_href}/networkConfigSection",
                headers=headers,
                timeout=self.REQUEST_TIMEOUT,
            )
            logger.info("vapp networkConfigSection HTTP %d", resp.status_code)
            networks = []
            if resp.status_code == 200:
                ns = {
                    "vcd": "http://www.vmware.com/vcloud/v1.5",
                    "ovf": "http://schemas.dmtf.org/ovf/envelope/1",
                }
                root = ET.fromstring(resp.text)
                for nc in root.findall(".//vcd:NetworkConfig", ns):
                    net_name = nc.get("networkName", "")
                    cfg = nc.find("vcd:Configuration", ns)
                    fence = cfg.findtext("vcd:FenceMode", namespaces=ns) if cfg else None
                    ip_scope = cfg.find(".//vcd:IpScope", ns) if cfg else None
                    gateway = ip_scope.findtext("vcd:Gateway", namespaces=ns) if ip_scope is not None else None
                    netmask = ip_scope.findtext("vcd:Netmask", namespaces=ns) if ip_scope is not None else None
                    dns1    = ip_scope.findtext("vcd:Dns1",    namespaces=ns) if ip_scope is not None else None
                    dns2    = ip_scope.findtext("vcd:Dns2",    namespaces=ns) if ip_scope is not None else None
                    networks.append({
                        "name": net_name,
                        "type": fence,
                        "gateway": gateway,
                        "netmask": netmask,
                        "dns1": dns1,
                        "dns2": dns2,
                    })
            logger.info("vapp networks parsed: %d", len(networks))
            vapp["networks"] = networks
        except Exception as e:
            logger.info("vapp networks fetch failed: %s", e)
            vapp["networks"] = []

        return vapp

    def list_ip_sets_for_gateway(self, edge_gateway_id: str) -> list:
        """
        List IP sets scoped to an edge gateway.
        Tries the firewallGroups collection filtered by ownerRef, then falls back
        to VDC-group-level IP sets for gateways owned by a VDC group.
        """
        gw_urn = self._gw_urn(edge_gateway_id)

        # Try edge-gateway-scoped firewall groups endpoint
        for url, p in [
            (self._cloudapi(f"edgeGateways/{gw_urn}/firewall/groups"), {"filter": "type==IP_SET"}),
            (self._cloudapi(f"edgeGateways/{gw_urn}/firewall/groups"), {}),
        ]:
            try:
                data = self._get_gw(url, params=p)
                items = data.get("values", []) if isinstance(data, dict) else []
                logger.info("Gateway firewall groups GET succeeded url=%s count=%d", url, len(items))
                return [
                    {"id": i.get("id"), "name": i.get("name"), "ip_addresses": i.get("ipAddresses", [])}
                    for i in items
                ]
            except VCDClientError as exc:
                logger.info("Gateway firewall groups GET failed url=%s error=%s", url, exc)

        # Fallback: VDC-group-owned gateways
        try:
            gw = self._get(self._cloudapi(f"edgeGateways/{gw_urn}"))
        except VCDClientError:
            raise VCDClientError("Could not retrieve edge gateway details to locate IP sets.")

        owner = gw.get("ownerRef") or {}
        owner_id = owner.get("id", "") if isinstance(owner, dict) else ""
        owner_uuid = self._to_uuid(owner_id) if owner_id else ""

        if "vdcGroup" in owner_id.lower() and owner_uuid:
            return self.list_ip_sets(owner_uuid)

        org_ref = gw.get("orgRef") or {}
        gw_org_id = org_ref.get("id", "") if isinstance(org_ref, dict) else ""
        for grp in self.list_vdc_groups(gw_org_id):
            try:
                grp_uuid = self._to_uuid(grp["id"])
                detail = self._get_gw(self._cloudapi(f"vdcGroups/{grp_uuid}"))
                members = detail.get("participatingOrgVdcs") or []
                if any(self._to_uuid(m.get("id", "")) == owner_uuid
                       for m in members if isinstance(m, dict)):
                    return self.list_ip_sets(grp_uuid)
            except VCDClientError:
                continue

        logger.info("No accessible IP sets endpoint found for gateway %s — returning empty list", gw_urn)
        return []

    def list_security_groups(self, vdc_group_id: str) -> list:
        gid = self._to_uuid(vdc_group_id) if ":" in vdc_group_id else vdc_group_id
        data = self._get(self._cloudapi(f"vdcGroups/{gid}/securityGroups"))
        values = data.get("values", []) if isinstance(data, dict) else []
        return [{"id": g.get("id"), "name": g.get("name")} for g in values]

    def list_ip_sets(self, vdc_group_id: str) -> list:
        gid = self._to_uuid(vdc_group_id) if ":" in vdc_group_id else vdc_group_id
        data = self._get(self._cloudapi(f"vdcGroups/{gid}/ipSets"))
        values = data.get("values", []) if isinstance(data, dict) else []
        return [
            {
                "id": i.get("id"),
                "name": i.get("name"),
                "ip_addresses": i.get("ipAddresses", []),
            }
            for i in values
        ]

    def list_dfw_rules(self, vdc_group_id: str) -> list:
        """Return all DFW rules across all policies for a VDC group, tagged with policy name."""
        gid = self._to_uuid(vdc_group_id) if ":" in vdc_group_id else vdc_group_id
        try:
            policies_data = self._get(self._cloudapi(f"vdcGroups/{gid}/dfwPolicies"))
        except VCDClientError:
            # Older VCD / group has no DFW — return empty
            return []
        policies = policies_data.get("values", []) if isinstance(policies_data, dict) else []
        result = []
        for pol in policies:
            pol_id = self._to_uuid(pol.get("id", ""))
            pol_name = pol.get("name", "")
            try:
                rules_data = self._get(self._cloudapi(f"vdcGroups/{gid}/dfwPolicies/{pol_id}/rules"))
                rules = rules_data.get("values", []) if isinstance(rules_data, dict) else []
                for r in rules:
                    src_groups = [s.get("name") or s.get("id", "") for s in (r.get("sourceFirewallGroups") or [])]
                    dst_groups = [d.get("name") or d.get("id", "") for d in (r.get("destinationFirewallGroups") or [])]
                    result.append({
                        "policy": pol_name,
                        "id": r.get("id"),
                        "name": r.get("name"),
                        "action": r.get("action"),
                        "enabled": r.get("enabled"),
                        "source_groups": src_groups,
                        "dest_groups": dst_groups,
                        "app_ports": [p.get("name", "") for p in (r.get("applicationPortProfiles") or [])],
                    })
            except VCDClientError:
                continue
        return result


# Module-level singleton — holds environment config and negotiated API version.
# Never mutate its credentials; use make_vcd_client() for per-request instances.
vcd_client = VCDClient()


def make_vcd_client(username: str, password: str, org: str = "") -> VCDClient:
    """
    Return a per-request VCDClient using the caller's credentials.
    Inherits environment config and negotiated API version from the singleton.
    Org is caller-supplied — never inherited from shared singleton state.
    """
    client = VCDClient.__new__(VCDClient)
    client._username = username
    client._password = password
    client._environments = vcd_client._environments
    client._active_env = vcd_client._active_env
    client._host = vcd_client._host
    client._api_version = vcd_client._api_version  # reuse negotiated version; None is safe
    client._active_org = org or ""
    client._tokens = {}
    client._token_types = {}
    client._token_times = {}
    client._session = requests.Session()
    return client
