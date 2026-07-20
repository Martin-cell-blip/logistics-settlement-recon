"""Small API-key/RBAC boundary for the local review service."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Callable

from fastapi import Depends, Header, HTTPException


@dataclass(frozen=True)
class Actor:
    actor_id: str
    role: str


def auth_mode() -> str:
    return "api-key" if os.environ.get("REVIEW_API_KEYS") else "demo"


def get_actor(x_api_key: str | None = Header(default=None)) -> Actor:
    raw = os.environ.get("REVIEW_API_KEYS")
    if not raw:
        return Actor(actor_id="demo-reviewer", role="reviewer")
    try:
        key_map = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=503, detail="REVIEW_API_KEYS 配置不是有效 JSON") from exc
    record = key_map.get(x_api_key or "")
    if not record:
        raise HTTPException(status_code=401, detail="缺少或无效的 X-API-Key")
    actor = Actor(actor_id=str(record.get("actor_id", "")).strip(), role=str(record.get("role", "")))
    if not actor.actor_id or actor.role not in {"viewer", "reviewer", "admin"}:
        raise HTTPException(status_code=503, detail="API key 身份配置无效")
    return actor


def require_roles(*roles: str) -> Callable:
    def dependency(actor: Actor = Depends(get_actor)) -> Actor:
        if actor.role not in roles:
            raise HTTPException(status_code=403, detail="当前角色无权执行该操作")
        return actor

    return dependency
