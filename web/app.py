"""FastAPI: тонкая обёртка над конвейером агента + подключение GitLab + раздача фронтенда."""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from urllib.parse import quote

try:
    from dotenv import load_dotenv

    if os.getenv("LLM_PROVIDER") is None:
        load_dotenv()
except ImportError:  # pragma: no cover
    pass

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__
from .criteria import catalog
from .gitlab import SCOPES, GitLabError
from .gitlab import store as gl
from .reader import MAX_BYTES, DocumentError, detect_format, read_document
from .runs import pipeline_mode
from .runs import store as runs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("web")

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="bcc spec — проверка ТЗ", version=__version__, docs_url="/api/docs", openapi_url="/api/openapi.json")


# Необязательная защита паролем для публичного стенда: APP_BASIC_AUTH="user:password".
_BASIC_AUTH = os.getenv("APP_BASIC_AUTH", "")


@app.middleware("http")
async def _basic_auth(request, call_next):
    if not _BASIC_AUTH or request.url.path == "/healthz":
        return await call_next(request)
    import base64
    import secrets
    header = request.headers.get("authorization", "")
    if header.lower().startswith("basic "):
        try:
            given = base64.b64decode(header[6:]).decode("utf-8")
        except Exception:  # noqa: BLE001
            given = ""
        if secrets.compare_digest(given.encode(), _BASIC_AUTH.encode()):
            return await call_next(request)
    return Response("Требуется вход", status_code=401, headers={"WWW-Authenticate": 'Basic realm="bcc spec"'})


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}


def err(status: int, code: str, message: str, hint: str = "") -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": code, "message": message, "hint": hint})


@app.exception_handler(GitLabError)
async def _gitlab_error(_, e: GitLabError):
    status = {"unauthorized": 401, "forbidden": 403, "not_found": 404, "write_scope": 400, "bad_url": 400,
              "no_token": 400}.get(e.code, 502)
    return err(status, f"gitlab_{e.code}", e.message)


# ---------------------------------------------------------------- meta

@app.get("/api/meta")
def meta():
    mode = pipeline_mode()
    model = os.getenv("GEMINI_MODEL") or os.getenv("LLM_MODEL") or ""
    try:
        from validator import __version__ as agent_version  # type: ignore
    except Exception:  # noqa: BLE001
        agent_version = None
    return {"web_version": __version__, "mode": mode, "model": model if mode == "agent" else "демо без LLM",
            "agent_version": agent_version, "max_upload_mb": MAX_BYTES // (1024 * 1024),
            "formats": ["pdf", "docx", "md", "txt"], "scopes": SCOPES, **catalog()}


@app.get("/api/criteria")
def criteria():
    return catalog()


# ---------------------------------------------------------------- документы

@app.post("/api/documents")
async def upload_document(file: UploadFile = File(...)):
    """Принять файл, прочитать без LLM и вернуть предпросмотр. Ошибки — понятные коды для экрана загрузки."""
    name = Path(file.filename or "document").name
    if not detect_format(name):
        return err(415, "format", "Этот формат агент не читает",
                   "Поддерживаются PDF с текстовым слоем, DOCX и Markdown.")
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        return err(413, "size", "Файл больше 20 МБ", "Сохраните документ без изображений или разбейте его на части.")
    tmp = runs.temp_path(name, data)
    try:
        doc = read_document(tmp)
    except DocumentError as e:
        return err(422, e.code, e.message, e.hint)
    finally:
        runs.drop_temp(tmp)
    preview = {
        "title": doc.title, "format": doc.format, "pages": doc.pages, "tables": doc.tables, "chars": len(doc.text),
        "req_markers": doc.req_markers[:40], "req_markers_total": len(doc.req_markers),
        "looks_like_requirements": doc.looks_like_requirements,
        "excerpt": doc.text[:1200],
        "sections": [m.group(2).strip("* ") for m in re.finditer(r"(?m)^(#{1,3})\s+(.+)$", doc.text)][:30],
    }
    up = runs.save_upload(name, data, preview)
    return {"upload_id": up.id, "file_name": up.file_name, "size": up.size, "preview": preview}


# ---------------------------------------------------------------- проверки

class RunIn(BaseModel):
    upload_id: str
    title: str = ""
    work_type: str = "Новый функционал"
    criteria: list[str] = []
    gitlab_project_id: str | None = None
    ref: str | None = None
    scopes: list[str] = []


def _create_run(body: RunIn):
    gp, factory = None, None
    if body.gitlab_project_id:
        try:
            proj = gl.get_project(body.gitlab_project_id)
        except GitLabError:
            raise HTTPException(404, "Проект GitLab не подключён")
        ref = body.ref or proj.default_branch
        scopes = body.scopes or [s["id"] for s in SCOPES]
        gp = {"id": proj.id, "path": proj.path, "ref": ref, "scopes": scopes}
        factory = lambda: gl.context(proj.id, ref, scopes)  # noqa: E731
    options = {"work_type": body.work_type, "criteria": body.criteria, "scopes": body.scopes}
    try:
        run = runs.create(body.upload_id, body.title.strip(), options, gp, factory)
    except KeyError:
        raise HTTPException(404, "Файл не найден — загрузите документ заново")
    return run


@app.post("/api/runs", status_code=201)
def create_run(body: RunIn):
    return {"run_id": _create_run(body).id}


@app.post("/api/runs/upload", status_code=201)
async def create_run_multipart(file: UploadFile = File(...), title: str = Form(""), work_type: str = Form("Новый функционал"),
                               criteria: str = Form(""), gitlab_project_id: str = Form(""), ref: str = Form(""),
                               scopes: str = Form("")):
    """Вариант «одним запросом»: файл + параметры (для curl/интеграций)."""
    up = await upload_document(file)
    if isinstance(up, JSONResponse):
        return up
    body = RunIn(upload_id=up["upload_id"], title=title, work_type=work_type,
                 criteria=[c for c in criteria.split(",") if c], gitlab_project_id=gitlab_project_id or None,
                 ref=ref or None, scopes=[s for s in scopes.split(",") if s])
    return {"run_id": _create_run(body).id}


@app.get("/api/runs")
def list_runs():
    return [r.public() for r in runs.list()]


def _run(run_id: str):
    run = runs.get(run_id)
    if not run:
        raise HTTPException(404, "Проверка не найдена")
    return run


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    return _run(run_id).public()


@app.get("/api/runs/{run_id}/result")
def get_result(run_id: str):
    run = _run(run_id)
    if run.status != "done" or not run.result:
        raise HTTPException(409, "Проверка ещё не завершена")
    data = {k: v for k, v in run.result.items() if k != "report_markdown"}
    return {**data, "run": run.public()}


@app.get("/api/runs/{run_id}/report.md")
def get_report(run_id: str, download: int = 0):
    run = _run(run_id)
    if run.status != "done" or not run.report_md:
        raise HTTPException(409, "Отчёт ещё не готов")
    headers = {}
    if download:
        stem = re.sub(r"[^\w\-]+", "_", Path(run.file_name).stem)[:60] or "report"
        headers["Content-Disposition"] = f"attachment; filename=\"report.md\"; filename*=UTF-8''{quote('report_' + stem + '.md')}"
    return PlainTextResponse(run.report_md, media_type="text/markdown; charset=utf-8", headers=headers)


@app.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: str):
    return runs.cancel(run_id).public() if _run(run_id) else None


@app.post("/api/runs/{run_id}/retry", status_code=201)
def retry_run(run_id: str):
    old = _run(run_id)
    body = RunIn(upload_id=old.upload_id, title=old.title, work_type=old.options.get("work_type", ""),
                 criteria=old.options.get("criteria", []),
                 gitlab_project_id=(old.gitlab_project or {}).get("id"), ref=(old.gitlab_project or {}).get("ref"),
                 scopes=(old.gitlab_project or {}).get("scopes", []))
    return {"run_id": _create_run(body).id}


@app.delete("/api/runs/{run_id}", status_code=204)
def delete_run(run_id: str):
    _run(run_id)
    runs.delete(run_id)
    return Response(status_code=204)


# ---------------------------------------------------------------- GitLab (только чтение)

class ConnIn(BaseModel):
    url: str
    token: str


@app.get("/api/gitlab/connections")
def gl_connections():
    return [c.public() for c in gl.connections.values()]


@app.post("/api/gitlab/connections", status_code=201)
def gl_connect(body: ConnIn):
    return gl.connect(body.url, body.token).public()


@app.delete("/api/gitlab/connections/{conn_id}", status_code=204)
def gl_disconnect(conn_id: str):
    gl.disconnect(conn_id)
    return Response(status_code=204)


@app.get("/api/gitlab/connections/{conn_id}/projects")
def gl_available_projects(conn_id: str, search: str = ""):
    items = gl.get_conn(conn_id).client.projects(search)
    return [{"gitlab_id": p["id"], "path": p.get("path_with_namespace"), "name": p.get("name"),
             "default_branch": p.get("default_branch"), "web_url": p.get("web_url"),
             "last_activity_at": p.get("last_activity_at")} for p in items]


class ProjIn(BaseModel):
    connection_id: str
    gitlab_id: int | str


@app.get("/api/gitlab/projects")
def gl_projects():
    return [p.public() for p in gl.projects.values()]


@app.post("/api/gitlab/projects", status_code=201)
def gl_add_project(body: ProjIn):
    return gl.add_project(body.connection_id, body.gitlab_id).public()


@app.post("/api/gitlab/projects/{project_id}/sync")
def gl_sync(project_id: str):
    gl.resync(project_id)
    return gl.get_project(project_id).public()


@app.delete("/api/gitlab/projects/{project_id}", status_code=204)
def gl_remove(project_id: str):
    gl.remove_project(project_id)
    return Response(status_code=204)


@app.get("/api/gitlab/projects/{project_id}/branches")
def gl_branches(project_id: str):
    proj = gl.get_project(project_id)
    items = gl.get_conn(proj.connection_id).client.branches(proj.gitlab_id)
    names = [b["name"] for b in items]
    names.sort(key=lambda n: (n != proj.default_branch, n))
    return names


@app.on_event("startup")
def _preconnect():
    """GITLAB_URL + GITLAB_TOKEN (+ GITLAB_PROJECTS через запятую) в .env — подключить при старте."""
    url, token = os.getenv("GITLAB_URL"), os.getenv("GITLAB_TOKEN")
    if not (url and token):
        return
    try:
        conn = gl.connect(url, token)
        for p in filter(None, (os.getenv("GITLAB_PROJECTS") or "").split(",")):
            gl.add_project(conn.id, p.strip())
        log.info("GitLab подключён из .env: %s", conn.url)
    except GitLabError as e:
        log.warning("GitLab из .env не подключён: %s", e.message)


# ---------------------------------------------------------------- фронтенд

app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
def spa(path: str):
    if path.startswith("api/"):
        raise HTTPException(404, "Нет такого метода API")
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})
