from __future__ import annotations

import asyncio
import hmac
import json
import logging
from contextlib import asynccontextmanager
from urllib.parse import quote_plus

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.db import Database
from app.logging_utils import configure_logging
from app.schemas import HealthResponse, ReorderRequest, TaskCreate, TaskDetail, TaskLogsResponse, TaskSummary, normalize_patterns
from app.security import TokenCipher, build_auth_token, mask_secret, verify_auth_token
from app.services.runner import RuntimeInspector, TaskRunner

logger = logging.getLogger(__name__)


def task_to_summary(task: dict) -> TaskSummary:
    return TaskSummary.model_validate(task)


def task_to_detail(task: dict) -> TaskDetail:
    payload = dict(task)
    payload["hf_token_masked"] = mask_secret(payload.get("hf_token_plaintext"))
    return TaskDetail.model_validate(payload)


def build_task_payload(data: TaskCreate) -> dict:
    payload = data.model_dump()
    payload["batch_size_limit_bytes"] = payload.pop("batch_size_limit_gb") * 1024 * 1024 * 1024
    return payload


def build_local_task_dir(queue_position: int, repo_id: str) -> str:
    safe_repo_id = repo_id.replace("/", "--")
    return str(settings.download_root / f"task-{queue_position}-{safe_repo_id}")


def build_health_payload(request: Request) -> HealthResponse:
    inspector_result = request.app.state.inspector.inspect()
    worker_alive = request.app.state.runner.is_alive()
    status = "ok" if worker_alive and all(inspector_result.values()) else "degraded"
    return HealthResponse(
        status=status,
        worker_alive=worker_alive,
        **inspector_result,
    )


def create_app() -> FastAPI:
    configure_logging(settings.log_dir, settings.log_level)
    database = Database(settings.db_path)
    database.init()
    cipher = TokenCipher(settings.app_secret)
    runner = TaskRunner(database, settings, cipher)
    inspector = RuntimeInspector(settings)
    templates = Jinja2Templates(directory="templates")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        interrupted = database.mark_running_tasks_interrupted()
        for task_id in interrupted:
            database.append_log(task_id, "WARNING", "interrupted", "Task was interrupted because the service restarted")
        inspection = inspector.inspect()
        if not inspection["tosutil_available"]:
            logger.warning("tosutil binary not found at %s", settings.tosutil_path)
        if not inspection["tosutil_config_exists"]:
            logger.warning("tosutil config file not found at %s", settings.tosutil_config)
        runner.start()
        app.state.db = database
        app.state.runner = runner
        app.state.cipher = cipher
        app.state.inspector = inspector
        app.state.templates = templates
        try:
            yield
        finally:
            runner.stop()

    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory="static"), name="static")

    def get_db(request: Request) -> Database:
        return request.app.state.db

    def get_runner(request: Request) -> TaskRunner:
        return request.app.state.runner

    def is_public_path(path: str) -> bool:
        return path == "/login" or path == "/healthz" or path == "/favicon.ico" or path.startswith("/static/")

    def safe_next_url(value: str | None) -> str:
        if not value or not value.startswith("/") or value.startswith("//"):
            return "/tasks"
        return value

    @app.middleware("http")
    async def require_password_auth(request: Request, call_next):
        if is_public_path(request.url.path) or verify_auth_token(
            request.cookies.get(settings.auth_cookie_name),
            settings.auth_password,
            settings.app_secret,
        ):
            return await call_next(request)

        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Authentication required"}, status_code=401)

        next_url = request.url.path
        if request.url.query:
            next_url = f"{next_url}?{request.url.query}"
        return RedirectResponse(url=f"/login?next={quote_plus(next_url)}", status_code=303)

    def render_task(task: dict, request: Request) -> TaskDetail:
        payload = dict(task)
        token = request.app.state.cipher.decrypt(payload["hf_token_encrypted"])
        payload["hf_token_plaintext"] = token
        return task_to_detail(payload)

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request) -> HTMLResponse:
        return RedirectResponse(url="/tasks", status_code=303)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"request": request, "error": None, "next_url": safe_next_url(request.query_params.get("next"))},
        )

    @app.post("/login", response_class=HTMLResponse)
    async def login(request: Request, password: str = Form(...), next_url: str = Form("/tasks")):
        if not hmac.compare_digest(password, settings.auth_password):
            return templates.TemplateResponse(
                request,
                "login.html",
                {"request": request, "error": "密码错误", "next_url": safe_next_url(next_url)},
                status_code=401,
            )
        response = RedirectResponse(url=safe_next_url(next_url), status_code=303)
        response.set_cookie(
            settings.auth_cookie_name,
            build_auth_token(settings.auth_password, settings.app_secret),
            httponly=True,
            samesite="lax",
            max_age=7 * 24 * 60 * 60,
        )
        return response

    @app.get("/tasks", response_class=HTMLResponse)
    async def tasks_page(request: Request) -> HTMLResponse:
        db = get_db(request)
        tasks = [task_to_summary(task) for task in db.list_tasks()]
        form_error = request.query_params.get("error")
        health = build_health_payload(request)
        return templates.TemplateResponse(
            request,
            "tasks.html",
            {
                "request": request,
                "tasks": tasks,
                "health": health,
                "form_error": form_error,
                "defaults": {
                    "batch_size_limit_gb": settings.default_batch_size_gb,
                    "max_retries": settings.default_max_retries,
                },
            },
        )

    @app.get("/tasks/{task_id}", response_class=HTMLResponse)
    async def task_detail_page(task_id: int, request: Request) -> HTMLResponse:
        db = get_db(request)
        task = db.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        logs = db.list_recent_logs(task_id, limit=200)
        files = db.get_task_files(task_id)
        return templates.TemplateResponse(
            request,
            "task_detail.html",
            {
                "request": request,
                "task": render_task(task, request),
                "logs": logs,
                "files": files[:200],
                "file_count": len(files),
            },
        )

    @app.post("/tasks", response_class=HTMLResponse)
    async def create_task_from_form(
        request: Request,
        repo_id: str = Form(...),
        repo_type: str = Form(...),
        hf_token: str = Form(...),
        tos_target: str = Form(...),
        revision: str = Form(""),
        allow_patterns: str = Form(""),
        ignore_patterns: str = Form(""),
        batch_size_limit_gb: int = Form(settings.default_batch_size_gb),
        max_retries: int = Form(settings.default_max_retries),
        cleanup_local_files: bool = Form(False),
    ):
        db = get_db(request)
        runner = get_runner(request)
        try:
            task_create = TaskCreate(
                repo_id=repo_id,
                repo_type=repo_type,
                hf_token=hf_token,
                tos_target=tos_target,
                revision=revision or None,
                allow_patterns=normalize_patterns(allow_patterns),
                ignore_patterns=normalize_patterns(ignore_patterns),
                batch_size_limit_gb=batch_size_limit_gb,
                max_retries=max_retries,
                cleanup_local_files=cleanup_local_files,
            )
        except Exception as exc:  # noqa: BLE001
            return RedirectResponse(url=f"/tasks?error={quote_plus(str(exc))}", status_code=303)

        payload = build_task_payload(task_create)
        encrypted = request.app.state.cipher.encrypt(task_create.hf_token)
        queue_position = db.next_queue_position()
        task = db.create_task(
            payload,
            encrypted,
            build_local_task_dir(queue_position, task_create.repo_id),
            queue_position=queue_position,
        )
        db.append_log(task["id"], "INFO", "queued", "Task created from web form")
        runner.wake()
        return RedirectResponse(url=f"/tasks/{task['id']}", status_code=303)

    @app.post("/tasks/{task_id}/cancel")
    async def cancel_task_form(task_id: int, request: Request) -> RedirectResponse:
        db = get_db(request)
        runner = get_runner(request)
        task = db.request_cancel(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        db.append_log(task_id, "WARNING", "cancelled", "Cancellation requested by user")
        runner.wake()
        referer = request.headers.get("referer") or "/tasks"
        return RedirectResponse(url=referer, status_code=303)

    @app.post("/tasks/{task_id}/retry")
    async def retry_task_form(task_id: int, request: Request) -> RedirectResponse:
        db = get_db(request)
        runner = get_runner(request)
        try:
            task = db.retry_task(task_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        db.append_log(task_id, "INFO", "queued", "Task queued for retry")
        runner.wake()
        return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)

    @app.post("/tasks/{task_id}/move/{direction}")
    async def move_task(task_id: int, direction: str, request: Request) -> RedirectResponse:
        db = get_db(request)
        if direction not in {"up", "down"}:
            raise HTTPException(status_code=400, detail="Invalid direction")
        try:
            db.move_queued_task(task_id, direction)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(url="/tasks", status_code=303)

    @app.post("/tasks/{task_id}/delete")
    async def delete_task(task_id: int, request: Request) -> RedirectResponse:
        db = get_db(request)
        try:
            deleted = db.delete_finished_task(task_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail="Task not found")
        return RedirectResponse(url="/tasks", status_code=303)

    @app.get("/healthz", response_model=HealthResponse)
    async def healthcheck(request: Request) -> HealthResponse:
        return build_health_payload(request)

    @app.get("/api/tasks", response_model=list[TaskSummary])
    async def list_tasks_api(request: Request) -> list[TaskSummary]:
        return [task_to_summary(task) for task in get_db(request).list_tasks()]

    @app.post("/api/tasks", response_model=TaskDetail, status_code=201)
    async def create_task_api(task_create: TaskCreate, request: Request) -> TaskDetail:
        db = get_db(request)
        payload = build_task_payload(task_create)
        encrypted = request.app.state.cipher.encrypt(task_create.hf_token)
        queue_position = db.next_queue_position()
        task = db.create_task(
            payload,
            encrypted,
            build_local_task_dir(queue_position, task_create.repo_id),
            queue_position=queue_position,
        )
        db.append_log(task["id"], "INFO", "queued", "Task created through API")
        get_runner(request).wake()
        return render_task(task, request)

    @app.get("/api/tasks/{task_id}", response_model=TaskDetail)
    async def get_task_api(task_id: int, request: Request) -> TaskDetail:
        task = get_db(request).get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        return render_task(task, request)

    @app.post("/api/tasks/{task_id}/cancel", response_model=TaskSummary)
    async def cancel_task_api(task_id: int, request: Request) -> TaskSummary:
        task = get_db(request).request_cancel(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        get_db(request).append_log(task_id, "WARNING", "cancelled", "Cancellation requested through API")
        get_runner(request).wake()
        return task_to_summary(task)

    @app.post("/api/tasks/{task_id}/retry", response_model=TaskSummary)
    async def retry_task_api(task_id: int, request: Request) -> TaskSummary:
        try:
            task = get_db(request).retry_task(task_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        get_db(request).append_log(task_id, "INFO", "queued", "Task queued for retry through API")
        get_runner(request).wake()
        return task_to_summary(task)

    @app.post("/api/tasks/reorder", response_model=list[TaskSummary])
    async def reorder_tasks_api(payload: ReorderRequest, request: Request) -> list[TaskSummary]:
        tasks = get_db(request).reorder_queued_tasks(payload.task_ids)
        return [task_to_summary(task) for task in tasks]

    @app.get("/api/tasks/{task_id}/logs", response_model=TaskLogsResponse)
    async def task_logs_api(task_id: int, request: Request, after_id: int = 0, limit: int = 200) -> TaskLogsResponse:
        db = get_db(request)
        if db.get_task(task_id) is None:
            raise HTTPException(status_code=404, detail="Task not found")
        return TaskLogsResponse(task_id=task_id, items=db.list_logs(task_id, after_id=after_id, limit=limit))

    @app.get("/api/tasks/{task_id}/stream")
    async def task_logs_stream(task_id: int, request: Request):
        db = get_db(request)
        if db.get_task(task_id) is None:
            raise HTTPException(status_code=404, detail="Task not found")

        async def event_generator():
            last_id = 0
            while True:
                if await request.is_disconnected():
                    break
                items = db.list_logs(task_id, after_id=last_id, limit=100)
                if items:
                    last_id = items[-1]["id"]
                    yield f"data: {json.dumps(items, ensure_ascii=False)}\n\n"
                else:
                    yield ": keep-alive\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    return app


app = create_app()
