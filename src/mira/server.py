"""Local administration API and independent Mira service entry point."""

import argparse
import asyncio
import io
import json
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path

import qrcode
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .config import load_character, load_settings
from .memory_storage import MemoryRepository
from .models import MiraError
from .standalone import MiraRuntime, USER_ID
from .storage import ConversationRepository


class PasswordInput(BaseModel):
    password: str


class BotInput(BaseModel):
    app_id: str
    secret: str | None = None
    allowed_user_id: str | None = None
    replace: bool = False


class OnboardStartInput(BaseModel):
    replace: bool = False


class EnabledInput(BaseModel):
    enabled: bool


class BindInput(BaseModel):
    user_id: str


class CharacterInput(BaseModel):
    character_id: str
    display_name: str
    personality: str
    speaking_style: str
    background: str
    boundaries: str


class ModelInput(BaseModel):
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    temperature: str = ""
    max_output_tokens: str = ""


class CorrectionInput(BaseModel):
    content: str = Field(min_length=1, max_length=300)


class OrganizeMemoryInput(BaseModel):
    category: str = Field(max_length=40)
    kind: str
    confirm: bool = False


class MergeMemoryInput(BaseModel):
    source_id: str


def create_app(root=None, runtime=None):
    """Create a single-user dashboard; network binding is handled by main()."""
    root = Path(root or Path.cwd()).resolve()
    runtime = runtime or MiraRuntime(root)
    sessions = {}

    @asynccontextmanager
    async def lifespan(app):
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(title="Mira", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.state.runtime = runtime
    web = Path(__file__).parent / "web"

    @app.middleware("http")
    async def protect(request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
                return JSONResponse({"error": "invalid_origin"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'"
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(MiraError)
    async def mira_error(request, exc):
        return JSONResponse({"error": exc.code}, status_code=400)

    def session(request: Request):
        token = request.cookies.get("mira_session", "")
        value = sessions.get(token)
        if not value or value["expires"] < time.time():
            raise HTTPException(status_code=401, detail="authentication_required")
        return value

    def mutation(request: Request):
        value = session(request)
        if not secrets.compare_digest(
            request.headers.get("X-Mira-CSRF", ""), value["csrf"]
        ):
            raise HTTPException(status_code=403, detail="invalid_csrf")
        return value

    def issue_session(response):
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        sessions[token] = {"csrf": csrf, "expires": time.time() + 12 * 3600}
        response.set_cookie("mira_session", token, httponly=True,
                            samesite="strict", max_age=12 * 3600, path="/")
        return {"authenticated": True, "csrf": csrf}

    @app.get("/")
    def index():
        return FileResponse(web / "index.html")

    @app.get("/assets/{name}")
    def asset(name: str):
        if name not in {"app.js", "styles.css"}:
            raise HTTPException(status_code=404)
        return FileResponse(web / name)

    @app.get("/api/session")
    def get_session(request: Request):
        try:
            value = session(request)
        except HTTPException:
            return {"authenticated": False,
                    "setup_required": not runtime.config.has_password()}
        return {"authenticated": True, "csrf": value["csrf"]}

    @app.post("/api/setup")
    def setup(body: PasswordInput, response: Response):
        if runtime.config.has_password():
            raise HTTPException(status_code=409, detail="already_configured")
        runtime.config.set_password(body.password)
        runtime.events.add("admin", "管理密码已设置")
        return issue_session(response)

    @app.post("/api/login")
    def login(body: PasswordInput, response: Response):
        if not runtime.config.verify_password(body.password):
            raise HTTPException(status_code=401, detail="invalid_credentials")
        return issue_session(response)

    @app.post("/api/logout")
    def logout(request: Request, response: Response, _=Depends(mutation)):
        sessions.pop(request.cookies.get("mira_session", ""), None)
        response.delete_cookie("mira_session", path="/")
        return {"authenticated": False}

    def repository():
        settings = load_settings(root, require_model=False)
        return ConversationRepository(settings.database_path)

    def character_or_none():
        try:
            settings = load_settings(root, require_model=False)
            return load_character(settings.character_path)
        except MiraError:
            return None

    @app.get("/api/state")
    def state(_=Depends(session)):
        settings = load_settings(root, require_model=False)
        character = character_or_none()
        bot = runtime.config.bot()
        counts = {"turns": 0, "memories": 0, "pending_jobs": 0,
                  "failed_jobs": 0, "model_calls": 0}
        repo = repository()
        try:
            for name, query in {
                "turns": "SELECT COUNT(*) FROM turns WHERE status='completed' AND source<>'admin'",
                "memories": "SELECT COUNT(*) FROM memory_items WHERE status IN ('active','open','resolved')",
                "pending_jobs": "SELECT COUNT(*) FROM memory_jobs WHERE status='pending'",
                "failed_jobs": "SELECT COUNT(*) FROM memory_jobs WHERE error_code IS NOT NULL",
                "model_calls": "SELECT COUNT(*) FROM model_calls",
            }.items():
                counts[name] = repo.connection.execute(query).fetchone()[0]
        finally:
            repo.close()
        return {
            "bot": {"app_id": bot.get("app_id", ""),
                    "has_secret": bool(bot.get("secret")),
                    "allowed_user_id": bot.get("allowed_user_id", ""),
                    "enabled": bool(bot.get("enabled")),
                    "status": runtime.qq.status,
                    "last_error": runtime.qq.last_error,
                    "pending_user": runtime.qq.pending_user},
            "character": character,
            "model": {"base_url": settings.base_url, "model": settings.model,
                      "temperature": settings.temperature,
                      "max_output_tokens": settings.max_output_tokens,
                      "has_api_key": bool(settings.api_key)},
            "counts": counts,
        }

    @app.post("/api/bot")
    def save_bot(body: BotInput, _=Depends(mutation)):
        if runtime.qq.websocket is not None:
            raise MiraError("disconnect_bot_before_edit")
        current = runtime.config.bot()
        secret = body.secret or None
        if not secret and (not current.get("secret") or
                           current.get("app_id") != body.app_id):
            raise MiraError("invalid_bot_secret")
        runtime.config.save_bot(body.app_id, secret, body.allowed_user_id,
                                enabled=False, replace=body.replace)
        runtime.events.add("qq", "机器人接入配置已保存")
        return {"saved": True}

    @app.post("/api/bot/enabled")
    async def set_bot_enabled(body: EnabledInput, _=Depends(mutation)):
        if body.enabled:
            await runtime.qq.connect()
            runtime.config.set_bot_enabled(True)
        else:
            runtime.config.set_bot_enabled(False)
            await runtime.qq.disconnect()
        return {"enabled": body.enabled, "status": runtime.qq.status}

    @app.post("/api/bot/bind")
    def bind_bot(body: BindInput, _=Depends(mutation)):
        bot = runtime.config.bot()
        if not bot.get("app_id") or not body.user_id.strip():
            raise MiraError("invalid_user_id")
        runtime.config.save_bot(bot["app_id"], allowed_user_id=body.user_id.strip())
        runtime.qq.pending_user = None
        runtime.events.add("qq", "私聊账号已绑定")
        return {"bound": True}

    @app.post("/api/bot/test")
    async def test_bot(_=Depends(mutation)):
        await runtime.qq.send_proactive("【Mira 连接测试】这是一条管理端发送的测试消息。")
        return {"sent": True}

    @app.post("/api/onboard/start")
    def start_onboard(body: OnboardStartInput | None = None, _=Depends(mutation)):
        if runtime.qq.websocket is not None:
            raise MiraError("disconnect_bot_before_edit")
        runtime.start_onboarding(replace=body.replace if body else False)
        return {"started": True}

    @app.get("/api/onboard/status")
    def onboard_status(_=Depends(session)):
        return runtime.onboard

    @app.get("/api/onboard/qr")
    def onboard_qr(_=Depends(session)):
        url = runtime.qr_url()
        if not url:
            raise HTTPException(status_code=404)
        output = io.BytesIO()
        qrcode.make(url).save(output, format="PNG")
        output.seek(0)
        return StreamingResponse(output, media_type="image/png")

    @app.post("/api/character")
    def save_character(body: CharacterInput, _=Depends(mutation)):
        data = body.model_dump()
        if any(not value.strip() or len(value) > 2000 for value in data.values()):
            raise MiraError("invalid_character")
        settings = load_settings(root, require_model=False)
        path = Path(settings.character_path)
        if path.exists() and load_character(path)["character_id"] != body.character_id:
            raise MiraError("character_id_mismatch")
        repo = repository()
        try:
            repo.bind_character(body.character_id, path)
        finally:
            repo.close()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        temporary.replace(path)
        runtime.events.add("character", "角色设定已保存")
        return {"saved": True}

    @app.post("/api/model")
    def save_model(body: ModelInput, _=Depends(mutation)):
        data = {key: value.strip() for key, value in body.model_dump().items()
                if value.strip()}
        if ("base_url" in data and not data["base_url"].startswith(("https://", "http://"))):
            raise MiraError("invalid_model_settings")
        try:
            if "temperature" in data and not 0 <= float(data["temperature"]) <= 2:
                raise ValueError
            if "max_output_tokens" in data and not 1 <= int(data["max_output_tokens"]) <= 8192:
                raise ValueError
        except ValueError as exc:
            raise MiraError("invalid_model_settings") from exc
        runtime.config.save_model(data)
        runtime.events.add("model", "模型配置已保存")
        return {"saved": True}

    @app.get("/api/conversations")
    def conversations(response: Response, before: int = Query(None, ge=1), _=Depends(session)):
        response.headers["Cache-Control"] = "no-store"
        character = character_or_none()
        if not character:
            return {"items": [], "next_before": None}
        repo = repository()
        try:
            rows = repo.connection.execute(
                "SELECT t.sequence, t.status, u.occurred_at, u.text AS user_text, "
                "a.text AS reply, j.status AS job_status, j.error_code AS job_error, "
                "s.turn_id IS NOT NULL AS suppressed FROM turns t "
                "JOIN messages u ON u.turn_id=t.id AND u.role='user' "
                "LEFT JOIN messages a ON a.turn_id=t.id AND a.role='assistant' "
                "LEFT JOIN memory_jobs j ON j.turn_id=t.id "
                "LEFT JOIN memory_suppressions s ON s.turn_id=t.id "
                "WHERE t.user_id=? AND t.character_id=? AND t.source<>'admin' "
                "AND (? IS NULL OR t.sequence<?) ORDER BY t.sequence DESC LIMIT 21",
                (USER_ID, character["character_id"], before, before),
            ).fetchall()
            return {"items": [dict(row) for row in rows[:20]],
                    "next_before": rows[19]["sequence"] if len(rows) > 20 else None}
        finally:
            repo.close()

    @app.get("/api/memories")
    def list_memories(_=Depends(session)):
        character = character_or_none()
        if not character:
            return {"items": []}
        repo = repository()
        try:
            rows = MemoryRepository(repo).list_for_admin(USER_ID, character["character_id"])
            return {"items": [{
                "id": row["id"], "kind": row["kind"], "status": row["status"],
                "content": row["content"], "importance": row["importance"],
                "occurred_at": row["occurred_at"],
                "sensitivity": row["sensitivity"], "claim_mode": row["claim_mode"],
                "category": row["category"],
            } for row in rows]}
        finally:
            repo.close()

    @app.get("/api/memories/{memory_id}/sources")
    def memory_sources(memory_id: str, _=Depends(session)):
        character = character_or_none()
        if not character:
            raise HTTPException(status_code=404)
        repo = repository()
        try:
            memory = MemoryRepository(repo)
            item = memory.get_item(USER_ID, character["character_id"], memory_id)
            if not item or item["status"] == "forgotten":
                raise HTTPException(status_code=404)
            return {"sources": [dict(row) for row in memory.source_texts(
                USER_ID, character["character_id"], memory_id
            )]}
        finally:
            repo.close()

    @app.post("/api/memories/{memory_id}/forget")
    def forget_memory(memory_id: str, _=Depends(mutation)):
        character = character_or_none()
        if not character:
            raise HTTPException(status_code=404)
        repo = repository()
        try:
            MemoryRepository(repo).forget(USER_ID, character["character_id"], memory_id)
        finally:
            repo.close()
        runtime.events.add("memory", "管理端已停止使用一条记忆")
        return {"forgotten": True}

    @app.post("/api/memories/{memory_id}/correct")
    def correct_memory(memory_id: str, body: CorrectionInput, _=Depends(mutation)):
        character = character_or_none()
        if not character:
            raise HTTPException(status_code=404)
        repo = repository()
        try:
            new_id = MemoryRepository(repo).correct_from_admin(
                USER_ID, character["character_id"], memory_id, body.content.strip()
            )
        finally:
            repo.close()
        runtime.events.add("memory", "管理端已更正一条记忆")
        return {"corrected": True, "id": new_id}

    @app.post("/api/memories/{memory_id}/organize")
    def organize_memory(memory_id: str, body: OrganizeMemoryInput, _=Depends(mutation)):
        character = character_or_none()
        if not character:
            raise HTTPException(status_code=404)
        repo = repository()
        try:
            MemoryRepository(repo).organize(
                USER_ID, character["character_id"], memory_id,
                body.category, body.kind, body.confirm,
            )
        finally:
            repo.close()
        runtime.events.add("memory", "管理端已整理一条记忆")
        return {"saved": True}

    @app.post("/api/memories/{memory_id}/merge")
    def merge_memories(memory_id: str, body: MergeMemoryInput, _=Depends(mutation)):
        character = character_or_none()
        if not character:
            raise HTTPException(status_code=404)
        repo = repository()
        try:
            MemoryRepository(repo).merge(
                USER_ID, character["character_id"], memory_id, body.source_id
            )
        finally:
            repo.close()
        runtime.events.add("memory", "管理端已合并重复记忆")
        return {"merged": True}

    @app.get("/api/logs")
    def logs(_=Depends(session)):
        return {"events": runtime.events.recent()}

    @app.get("/api/runtime-logs")
    def runtime_logs(limit: int = Query(default=500, ge=1, le=1000),
                     _=Depends(session)):
        return {"lines": runtime.logs.tail(limit),
                "file": str(runtime.logs.path)}

    @app.post("/api/worker/check")
    async def check_worker(_=Depends(mutation)):
        await asyncio.to_thread(runtime.run_due)
        return {"checked": True}

    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description="Mira 独立服务与管理端")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[2]),
                        help="Mira 项目配置目录")
    parser.add_argument("--port", type=int, default=6186)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("端口范围为 1 到 65535")
    app = create_app(args.root)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
