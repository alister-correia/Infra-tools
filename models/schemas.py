from pydantic import BaseModel
from typing import Any, Dict, Literal, Optional


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    message: str
    history: list[ChatMessage] = []


class ResourceCard(BaseModel):
    platform: Literal["VCD"]
    resource_type: str
    name: str
    parameters: Dict[str, Any]
    status: Literal["DRY_RUN", "CONFIRMED", "PROVISIONING", "COMPLETE", "FAILED"] = "DRY_RUN"
    plan_id: Optional[str] = None


class ChatResponse(BaseModel):
    message: str
    card: Optional[ResourceCard] = None


class ConfirmRequest(BaseModel):
    plan_id: str


class HealthResponse(BaseModel):
    status: str
    version: str
