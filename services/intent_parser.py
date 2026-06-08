import json
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import anthropic
from anthropic import APIStatusError, APIConnectionError

MODEL = "claude-sonnet-4-6"

# Stable system prompt — cached on every request
_SYSTEM_PROMPT = """You are an AI assistant for an enterprise infrastructure operations platform.
Your sole job is to interpret natural-language commands and queries about cloud infrastructure
and extract them into structured operations by calling the parse_intent tool.

## Supported operations

### VCD (VMware vCloud Director)
Write operations:
- edit_vm              — Edit an existing VM (resize CPU/memory)
  params: vdc (required), name (required), cpu, memory_mb
- create_network       — Create an Org VDC network (routed or isolated)
  params: vdc (required), name, network_type (routed/isolated), gateway, prefix_length
- create_edge_fw_rule  — Create an Edge Gateway firewall rule
  params: edge_gateway (required), rule_name, source, destination, service, action
- create_nat_rule      — Create an Edge Gateway NAT rule (DNAT or SNAT)
  params: edge_gateway (required), rule_name, type (DNAT/SNAT), external_ip, internal_ip
- create_security_group — Create an NSX security group
  params: vdc_group_id (required), name
- create_ip_set        — Create an IP set
  params: vdc_group_id (required), name, ip_addresses (list)

Read/query operations:
- list_vdcs          — List all VDCs in the org (no params required)
- list_vms, list_vapps, list_networks, list_edge_gateways, list_firewall_rules, list_nat_rules, list_security_groups, list_ip_sets
  params: vdc or edge_gateway_id (required for most)
- get_vm            — Get compute details (CPU, RAM, disks) for a specific VM by name
  params: name (required), vdc (optional)
- get_network       — Get subnet, DHCP, and DNS details for a specific network by name
  params: name (required), vdc (optional)

## Rules
1. ALWAYS call parse_intent — never respond with plain text.
2. Set requires_clarification=true if a required parameter is missing and you cannot infer it.
3. is_query=true for list/get/report operations; false for create/deploy/modify operations.
4. In VCD context, "VDC" or "environment" means the org VDC (Virtual Data Center).
5. If the user says "dry run", "preview", or "what would happen if" — still parse the operation normally; the dry-run flag is always set before execution.
7. For deployment counts ("deploy 3 VMs"), use count parameter.
8. Use display_message to give a clear human-readable summary of what you parsed.
9. If the user says "this VDC", "current VDC", "selected VDC", "this org", "this org VDC", or omits the VDC entirely for a list/query operation, do NOT set requires_clarification=true. Leave vdc empty in parameters — the UI will supply the currently selected VDC automatically.
10. If the previous assistant message listed numbered options (e.g. "1. EdgeGW-A\n2. EdgeGW-B\nReply with a number to select.") and the user replies with just a number like "1" or "2", re-issue the original operation with the corresponding resource name filled into the appropriate parameter (edge_gateway → edge_gateway param; VDC name → vdc param; VM name → name param; VDC Group name → vdc_group_id param). Do NOT set requires_clarification=true.
"""

_PARSE_INTENT_TOOL = {
    "name": "parse_intent",
    "description": "Parse the user's infrastructure request into a structured operation.",
    "input_schema": {
        "type": "object",
        "properties": {
            "platform": {
                "type": "string",
                "enum": ["VCD", "GENERAL"],
                "description": "Target platform. GENERAL for questions not tied to a specific operation.",
            },
            "operation": {
                "type": "string",
                "description": "The specific operation name (e.g. deploy_instance, create_vcn, list_vms). Use 'chat' for conversational messages with no infrastructure action.",
            },
            "parameters": {
                "type": "object",
                "description": "Operation parameters extracted from the user message.",
                "additionalProperties": True,
            },
            "is_query": {
                "type": "boolean",
                "description": "True for read/list/report operations. False for create/deploy/modify operations.",
            },
            "requires_clarification": {
                "type": "boolean",
                "description": "True if required parameters are missing and cannot be inferred.",
            },
            "clarification_message": {
                "type": "string",
                "description": "Question to ask the user if requires_clarification is true.",
            },
            "display_message": {
                "type": "string",
                "description": "Clear, concise human-readable description of what was parsed or what clarification is needed.",
            },
        },
        "required": [
            "platform",
            "operation",
            "parameters",
            "is_query",
            "requires_clarification",
            "display_message",
        ],
        "additionalProperties": False,
    },
}


@dataclass
class ParsedIntent:
    platform: str
    operation: str
    parameters: Dict[str, Any]
    is_query: bool
    requires_clarification: bool
    display_message: str
    clarification_message: Optional[str] = None


class IntentParserError(Exception):
    pass


class IntentParser:
    def parse(self, message: str, history: list = None) -> ParsedIntent:
        messages = []

        # Cap history to keep token usage bounded — keep the most recent 20 turns
        for turn in (history or [])[-20:]:
            messages.append({"role": turn.role, "content": turn.content})

        messages.append({"role": "user", "content": message})

        try:
            response = self._client.messages.create(
                model=MODEL,
                max_tokens=1024,
                system=[
                    {
                        "type": "text",
                        "text": _SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tools=[_PARSE_INTENT_TOOL],
                tool_choice={"type": "tool", "name": "parse_intent"},
                messages=messages,
            )
        except APIConnectionError as exc:
            raise IntentParserError(f"Cannot reach Anthropic API: {exc}")
        except APIStatusError as exc:
            if exc.status_code == 401:
                raise IntentParserError(
                    "Invalid Anthropic API key. Check the key you entered at login."
                )
            raise IntentParserError(f"Anthropic API error {exc.status_code}: {exc}")

        # Extract the tool_use block
        tool_block = next(
            (b for b in response.content if b.type == "tool_use"),
            None,
        )
        if not tool_block:
            raise IntentParserError("Model did not call parse_intent — unexpected response.")

        data = tool_block.input
        return ParsedIntent(
            platform=data.get("platform", "GENERAL"),
            operation=data.get("operation", "chat"),
            parameters=data.get("parameters", {}),
            is_query=data.get("is_query", True),
            requires_clarification=data.get("requires_clarification", False),
            display_message=data.get("display_message", ""),
            clarification_message=data.get("clarification_message"),
        )


def make_intent_parser(api_key: str) -> IntentParser:
    """Return a per-request IntentParser using the caller's Anthropic API key."""
    parser = IntentParser.__new__(IntentParser)
    parser._client = anthropic.Anthropic(api_key=api_key)
    return parser
