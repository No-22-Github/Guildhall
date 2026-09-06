"""Redacted, revision-checked settings. Saved configuration applies after restart."""
from __future__ import annotations

import hashlib
import os
import tempfile
from urllib.parse import urlsplit

import tomlkit
from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from . import config, layout


class RoleSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(default="", max_length=200)
    base_url: str = Field(default="", max_length=2048)
    auth_token: str | None = Field(default=None, max_length=8192)
    clear_token: bool = False

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value):
        value = value.strip()
        if value:
            parsed = urlsplit(value)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("API 地址必须为不含凭据、查询参数的 HTTP(S) 地址")
        return value


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str
    roles: dict[str, RoleSettings]


def _document():
    path = layout.GUILDHALL_DIR / "config.toml"
    text = path.read_text() if path.exists() else config.DEFAULT_CONFIG
    return path, text, tomlkit.parse(text)


def _revision(text):
    return hashlib.sha256(text.encode()).hexdigest()


def snapshot(active):
    _, text, doc = _document()
    agent = doc.get("agent", {})
    roles = {}
    pending = False
    for role in config.ROLES:
        section = agent.get(role, {})
        saved = config.AgentRoleConfig(**{k: section.get(k) or None for k in ("model", "base_url", "auth_token")})
        pending |= saved != active.role_config(role)
        roles[role] = {"model": saved.model or "", "base_url": saved.base_url or "", "has_token": bool(saved.auth_token)}
    return {"revision": _revision(text), "roles": roles, "restart_required": pending,
            "agent_name": active.agent_name, "agent_mode": active.agent_mode}


def install(app, context):
    def guard(request):
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).hostname not in ("localhost", "127.0.0.1", "::1"):
            raise HTTPException(403, "设置仅允许本机页面访问")

    @app.get("/api/settings")
    async def get_settings(request: Request):
        guard(request)
        return snapshot(context.config)

    @app.put("/api/settings")
    async def save_settings(request: Request):
        guard(request)
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise HTTPException(415, "需要 JSON 请求")
        raw = await request.body()
        if len(raw) > 40000:
            raise HTTPException(413, "配置过大")
        try:
            body = SettingsUpdate.model_validate_json(raw)
        except ValidationError:
            # Never echo validation input: it may contain credentials.
            raise HTTPException(422, "配置格式无效；请检查模型和 HTTP(S) API 地址") from None
        if set(body.roles) != set(config.ROLES):
            raise HTTPException(422, "请提供全部三个角色的配置")
        path, text, doc = _document()
        if body.revision != _revision(text):
            raise HTTPException(409, "配置已被其他页面或文件修改，请重新载入后保存")
        agent = doc.setdefault("agent", tomlkit.table())
        for role, values in body.roles.items():
            section = agent.setdefault(role, tomlkit.table())
            for key in ("model", "base_url"):
                value = getattr(values, key).strip()
                if value:
                    section[key] = value
                else:
                    section.pop(key, None)
            if values.clear_token:
                section.pop("auth_token", None)
            elif values.auth_token:
                section["auth_token"] = values.auth_token
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".config-", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(tomlkit.dumps(doc))
                f.flush()
                os.fsync(f.fileno())
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        return snapshot(context.config)
