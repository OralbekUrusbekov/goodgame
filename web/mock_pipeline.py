"""Демо-конвейер: та же сигнатура и форма результата, что у validator.pipeline.run_pipeline.

Работает без LLM: читает документ, режет его эвристиками, ищет размытые слова, пустые разделы,
незаполненный чек-лист, смешение единиц и (для выданного ТЗ) подставляет эталонные находки,
если их цитаты действительно есть в тексте. Нужен, чтобы сайт можно было показать и проверить
до подключения настоящего агента. В интерфейсе прогон помечается как демо.
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from . import report_md
from .criteria import BY_CODE
from .reader import read_document
from .textutil import find_quote, headings, norm, plain, section_path

DELAY = float(os.getenv("MOCK_DELAY", "0.35"))
SEV_ORDER = {"critical": 0, "major": 1, "minor": 2}

REQ_SECTION = re.compile(r"требован|^цел|критери|логика|особые правила|параметры|^ответ|статус", re.I)
SKIP_SECTION = re.compile(r"ревью|согласующ|общие сведения|общие параметры|^схема|дизайн|архитектур|bpmn|sequence", re.I)
PLACEHOLDER = re.compile(r"^(заполня|tbd|todo|н/д|n/a|—|-|\.\.\.)", re.I)
MARKER = re.compile(r"^[\s*\-]*\**\s*([A-ZА-ЯЁ]{1,6}-\d{1,3}(?:\.\d+)?)\.?\**\s*")

VAGUE = [
    (r"быстр\w*", "29148.verifiable"), (r"удобн\w*", "29148.verifiable"), (r"своевременн\w*", "29148.verifiable"),
    (r"оптимальн\w*", "29148.unambiguous"), (r"по возможности", "29148.unambiguous"), (r"и т\.\s?д\.", "29148.complete"),
    (r"при необходимости", "29148.unambiguous"), (r"достаточн\w*", "29148.verifiable"), (r"максимально\b", "29148.verifiable"),
    (r"как правило", "29148.unambiguous"), (r"некотор\w+", "29148.unambiguous"), (r"надёжн\w*|надежн\w*", "29148.verifiable"),
    (r"интуитивн\w*", "29148.verifiable"), (r"в течение (?:минуты|секунды|часа|дня)", "29148.verifiable"),
    (r"и/или", "29148.unambiguous"), (r"\bTBD\b", "29148.complete"),
]

# Эталонные находки для выданного ТЗ. Подставляются ТОЛЬКО если все цитаты есть в документе.
FIXTURES = [
    ("critical", "29148.consistent", "Месяц покупки определён двумя способами",
     ["Месяц определяется по дате совершения покупки в Алматы", "Февральский счётчик вырос на 4 000 ₸"],
     "Ф-2 относит покупку к месяцу по дате совершения, а критерий приёмки 2 — покупку 31 января к февралю по дате проведения.",
     "Выбрать одну дату (совершения или проведения) и привести к ней Ф-2 и критерий приёмки.",
     "Какая дата определяет месяц расходов: совершения покупки или её проведения?"),
    ("critical", "29148.consistent", "Лимит push противоречит смене порога",
     ["На карту приходится максимум один такой push за календарный месяц",
      "следующее достижение вызывает push независимо от истории предыдущей настройки"],
     "Ф-3 разрешает один push в месяц, Ф-4 требует push после смены порога независимо от истории.",
     "Явно указать, сбрасывает ли смена порога месячный лимит push.",
     "Если push в этом месяце уже был и порог подняли — отправлять второй?"),
    ("critical", "fintech.http", "Метод сохранения описан как GET",
     ["Сохранение порога. Функциональные требования: чтение порога", "**REST type** | GET"],
     "Описание смешивает сохранение и чтение; изменение данных через GET нарушает семантику HTTP, а метода чтения нет.",
     "Разделить на метод чтения (GET) и метод сохранения (POST или PUT) с телом запроса.",
     "Каким методом приложение читает настройку и почему сохранение описано через GET?"),
    ("critical", "29148.unambiguous", "Неясно, как сообщать о неверной сумме",
     ["При отклонении суммы вернуть прежнее состояние и результат ERROR",
      "Ответ в случае, если параметры запроса заполнены неверно"],
     "Логика возвращает ERROR в ответе 200, а раздел статусов отводит неверным параметрам код 400.",
     "Выбрать один вариант и привести пример ответа для суммы вне диапазона.",
     "Неверная сумма — это ответ 200 с result=ERROR или статус 400?"),
    ("major", "fintech.money", "Сумма покупки в тиынах, порог в тенге",
     ["сумму покупки в тиынах", "порог в тенге"],
     "Не указано, где переводятся единицы и как округляются значения при сравнении с порогом.",
     "Хранить обе величины в тиынах или явно описать конвертацию и округление.",
     "Где и с каким округлением тиыны сравниваются с порогом в тенге?"),
    ("major", "29148.verifiable", "«В течение минуты» нельзя проверить",
     ["в течение минуты после получения события"],
     "Нет доли событий, нагрузки и точки отсчёта — тест не даст однозначного результата.",
     "Например: 95% событий передаются за 60 секунд при нагрузке N событий в секунду.",
     "«В течение минуты» — для какой доли событий и при какой нагрузке?"),
    ("major", "29148.feasible", "Цель «в момент покупки» недостижима",
     ["узнать о достижении личного бюджета в момент покупки", "В расходы входят проведённые покупки"],
     "Учитываются только проведённые покупки, а проведение происходит позже покупки.",
     "Уточнить цель («после проведения покупки») или учитывать авторизации.",
     "Клиент должен узнать о пороге при авторизации или после проведения покупки?"),
    ("major", "fintech.refunds", "Не описаны частичные возвраты и другие валюты",
     ["Возврат уменьшает расходы месяца исходной покупки"],
     "Не сказано про частичный возврат, возврат после push, покупки в другой валюте и прочие типы операций.",
     "Перечислить типы операций и правила для частичного возврата и валютных покупок.",
     "Как учитываются частичные возвраты и покупки в валюте, отличной от KZT?"),
    ("major", "29148.complete", "Не описан случай, когда сумма уже выше нового порога",
     ["Если она ниже нового порога"],
     "Ф-4 говорит только о сумме ниже нового порога; что делать, если накопленные расходы уже выше, — не сказано.",
     "Добавить правило для накопленной суммы, которая уже выше нового порога.",
     "Отправлять ли push сразу, если новый порог ниже уже накопленных расходов?"),
    ("major", "830.consistent", "Блок 403 описывает 404, а 201 не описан",
     ["Для чужой карты возвращается 404, как и для отсутствующей карты", "Если результат 200 или 201 — успешный"],
     "Заголовок 403 содержит правило про 404; статус 201 назван успешным, но нигде не описан.",
     "Привести список статусов к фактическим кодам и описать каждый.",
     "Возвращает ли метод 201 и 403 на самом деле?"),
    ("major", "fintech.http", "Content-Type указан при отсутствии тела",
     ["**Content-Type:** `application/json`", "Тело не используется"],
     "Для запроса без тела указан Content-Type application/json.",
     "Убрать Content-Type или перенести параметры в тело запроса.",
     ""),
    ("major", "830.traceable", "Метода чтения настроек нет в API",
     ["Приложение читает и сохраняет настройки через дочерний документ"],
     "Часть A ссылается на чтение настроек, но в части B описан только метод сохранения.",
     "Добавить в часть B метод чтения настроек.",
     "Через какой метод приложение получает текущий порог и состояние переключателя?"),
    ("major", "fintech.security", "API не проверяет активность карты и валюту KZT",
     ["Держатель активной карты KZT задаёт целый порог"],
     "Ф-1 ограничивает функцию активными картами KZT, а логика метода этого не проверяет.",
     "Добавить в логику метода проверку статуса карты и валюты и код ошибки.",
     "Какой ответ возвращать для заблокированной карты или карты в другой валюте?"),
    ("minor", "29148.set_complete", "Признак включения не входит в требования к данным",
     ["Хранить идентификатор карты, месяц, идентификатор операции"],
     "API сохраняет enabled, а требования к данным его не содержат.",
     "Добавить признак включения уведомления в требования к данным.",
     ""),
    ("minor", "checklist.versioning", "Нет версии и даты документа",
     ["Документ (версия/дата) в источнике не заполнен"],
     "Версия и дата не указаны, чек-лист ревью не заполнен.",
     "Указать версию, дату, заполнить чек-лист и блок согласующих.",
     ""),
    ("minor", "29148.comprehensible", "Один компонент — два названия",
     ["Мобильное приложение → сервис настроек", "хранилище настроек"],
     "«Сервис настроек» в части A и «хранилище настроек» в части B.",
     "Использовать одно название и добавить глоссарий.",
     ""),
    ("minor", "29148.unambiguous", "Не определено, какая сумма уходит в push",
     ["передать последние четыре цифры карты и сумму при достижении порога"],
     "Непонятно, это сумма покупки, накопленные расходы или порог.",
     "Указать, какая именно сумма передаётся в push.",
     "Какая сумма показывается клиенту в push: покупки, накопленная или порог?"),
]


def run_pipeline(doc_path: str, on_progress=None, gitlab=None, options: dict | None = None) -> dict:
    options = options or {}
    progress = on_progress or (lambda stage, info=None: None)

    def step(stage: str, **info):
        progress(stage, info)

    step("reading", message="Извлекаем текст")
    doc = read_document(Path(doc_path))
    time.sleep(DELAY)
    text = doc.text
    hs = headings(text)

    step("splitting")
    reqs = split_requirements(text)
    time.sleep(DELAY)
    step("splitting", total=len(reqs))

    findings: list[dict] = []
    step("checking_requirements", done=0, total=len(reqs))
    for i, r in enumerate(reqs):
        findings += check_requirement(r)
        step("checking_requirements", done=i + 1, total=len(reqs), findings_so_far=len(findings))
        time.sleep(DELAY / 3)

    step("checking_set", findings_so_far=len(findings))
    findings += check_set(text)
    time.sleep(DELAY)

    code_checks: list[dict] = []
    warnings: list[dict] = []
    if gitlab is not None:
        step("checking_gitlab", message=f"{gitlab.project} @ {gitlab.ref}")
        try:
            code_checks, code_findings = check_code(text, gitlab)
            findings += code_findings
        except Exception as e:  # noqa: BLE001
            msg = getattr(e, "message", str(e))
            warnings.append({"stage": "checking_gitlab", "code": "gitlab_error", "message": msg})
            step("warning", code="gitlab_error", message=msg)
    else:
        step("checking_gitlab", skipped=True)

    step("verifying_quotes")
    findings, dropped = verify_and_locate(findings, text, reqs, hs)
    time.sleep(DELAY)
    step("verifying_quotes", dropped=dropped)

    step("summary")
    result = assemble(doc, reqs, findings, code_checks, warnings, dropped, options, gitlab, Path(doc_path).name)
    result["report_markdown"] = report_md.render(result)
    return result


# ---------- нарезка

def split_requirements(text: str) -> list[dict]:
    reqs: list[dict] = []
    section = ""
    in_code = False
    table_row = 0
    pos = 0
    for line in text.splitlines(keepends=True):
        start = pos
        pos += len(line)
        s = line.strip()
        if s.startswith("```"):
            in_code = not in_code
            continue
        if in_code or not s:
            table_row = 0 if not s else table_row
            continue
        hm = re.match(r"^(#{1,6})\s+(.+)", s)
        if hm:
            section = hm.group(2).strip("* ")
            table_row = 0
            continue
        if re.fullmatch(r"[|:\-\s]+", s) or s == "---":
            continue
        is_table = s.startswith("|")
        table_row = table_row + 1 if is_table else 0

        m = MARKER.match(s)
        doc_id = m.group(1) if m else None
        in_req_section = bool(REQ_SECTION.search(section)) and not SKIP_SECTION.search(section)
        if not doc_id and not in_req_section:
            continue
        if is_table and table_row == 1:
            continue  # шапка таблицы
        body = s[m.end():] if m else re.sub(r"^[*\-\d.\s]+", "", s) if not is_table else s
        if PLACEHOLDER.match(plain(body, 400)) or len(plain(body, 400)) < 8:
            continue
        if not doc_id:
            nm = re.match(r"^(\d+)\.\s", s)
            doc_id = f"п. {nm.group(1)}" if nm else None
        quote_start = start + line.find(body.strip()[:20]) if body.strip() else start
        reqs.append({
            "doc_id": doc_id or "—",
            "section": section or "Документ",
            "summary": plain(body),
            "quote": body.strip(),
            "_start": quote_start,
            "_end": start + len(line.rstrip()),
        })
        if len(reqs) >= 80:
            break
    for i, r in enumerate(reqs, 1):
        r["id"] = f"R-{i:02d}"
    return reqs


# ---------- проверки

def _finding(severity, criterion, title, quote, defect, fix, question="", quotes=None, scope="requirement",
             req_id=None, code_ref=None) -> dict:
    c = BY_CODE.get(criterion, {})
    return {"req_id": req_id, "scope": scope, "criterion": criterion, "criterion_label": c.get("label", criterion),
            "source": c.get("source", ""), "verdict": "fail", "severity": severity, "title": title,
            "quote": quote, "quotes": quotes or [], "defect": defect, "fix": fix, "question": question,
            "code_ref": code_ref}


def check_requirement(r: dict) -> list[dict]:
    out = []
    for pattern, crit in VAGUE:
        m = re.search(pattern, r["quote"], re.I)
        if m:
            word = m.group(0)
            out.append(_finding(
                "major", crit, f"Размытая формулировка «{word}»", word,
                f"Слово «{word}» не задаёт измеримого условия — разработчик и тестировщик поймут его по-разному.",
                "Заменить на измеримое значение: число, единицу измерения, условие.",
                f"Что конкретно означает «{word}» в {r['doc_id'] if r['doc_id'] != '—' else r['id']}?",
                req_id=r["id"]))
    return out


def check_set(text: str) -> list[dict]:
    out = []
    for sev, crit, title, quotes, defect, fix, q in FIXTURES:
        if all(find_quote(text, x) >= 0 for x in quotes):
            out.append(_finding(sev, crit, title, quotes[0], defect, fix, q, quotes=quotes[1:], scope="set"))

    # пустые разделы и заглушки
    for m in re.finditer(r"(?mi)^\s*(заполняются[^\n]*|tbd[^\n]*|todo[^\n]*)$", text):
        out.append(_finding("major", "checklist.testable", "Раздел не заполнен", m.group(1).strip(),
                            "Вместо содержания раздела стоит заглушка.", "Заполнить раздел до передачи в разработку.",
                            "Кто и когда заполнит этот раздел?", scope="set"))
    # незаполненный чек-лист
    if text.count("Да / Нет") >= 3:
        out.append(_finding("minor", "checklist.review", "Чек-лист ревью не заполнен", "Да / Нет",
                            f"Варианты «Да / Нет» не выбраны ни в одном из {text.count('Да / Нет') // 2} пунктов.",
                            "Пройти чек-лист и проставить отметки.", "", scope="set"))
    # смешение единиц
    if re.search(r"тиын", text, re.I) and re.search(r"тенге|₸", text, re.I) and not any(
            f["criterion"] == "fintech.money" for f in out):
        m = re.search(r"[^.\n]*тиын[^.\n]*", text, re.I)
        out.append(_finding("major", "fintech.money", "Суммы в разных единицах", m.group(0).strip(),
                            "В документе встречаются и тиыны, и тенге без правила перевода.",
                            "Выбрать одну единицу хранения или описать перевод и округление.",
                            "В каких единицах хранятся и сравниваются суммы?", scope="set"))
    # мало критериев приёмки
    acc = re.search(r"(?is)#+\s*критерии при[её]мки\s*\n(.*?)(?=\n#+\s|\Z)", text)
    if acc:
        rows = [l for l in acc.group(1).splitlines() if l.strip().startswith("|") and not re.fullmatch(r"[|:\-\s]+", l.strip())]
        n = max(0, len(rows) - 1)
        if 0 < n < 5:
            out.append(_finding("major", "29148.able_to_be_validated", f"Всего {n} критерия приёмки",
                                acc.group(0).strip().splitlines()[0].strip("# ").strip(),
                                "Критерии приёмки не покрывают возвраты, границы диапазонов, выключенный переключатель и ошибки.",
                                "Добавить критерии для границ, ошибок и каждого функционального требования.",
                                "", scope="set"))
    return out


ENDPOINT_RE = re.compile(r"(?<![\w$])(/[a-zA-Z][\w\-]*(?:/[\w\-{}]+)+)")
FIELD_RE = re.compile(r"`([a-z][a-z0-9]*_[a-z0-9_]+)`")


def check_code(text: str, ctx) -> tuple[list[dict], list[dict]]:
    files = ctx.collect()
    checks, findings = [], []

    def search(needle: str, cats: tuple[str, ...]):
        for f in files:
            if f["category"] not in cats:
                continue
            idx = f["content"].find(needle)
            if idx >= 0:
                line = f["content"].count("\n", 0, idx) + 1
                return {"path": f["path"], "line": line, "url": ctx.web_url(f["path"], line)}
        return None

    for ep in dict.fromkeys(ENDPOINT_RE.findall(text)):
        if "." in ep.split("/")[-1]:
            continue
        ref = search(ep, ("api", "openapi")) or search(ep.rsplit("/", 1)[-1], ("api", "openapi"))
        checks.append({"item": ep, "kind": "endpoint", "status": "match" if ref else "missing", "doc_quote": ep,
                       "note": f"найден в {ref['path'].rsplit('/', 1)[-1]}" if ref else "в коде и OpenAPI не найден",
                       "code_ref": ref})
        if not ref:
            findings.append(_finding("major", "830.traceable", f"Метод {ep} не найден в коде", ep,
                                     f"ТЗ описывает {ep}, но ни контроллеры, ни OpenAPI проекта {ctx.project} его не содержат.",
                                     "Проверить имя метода или отметить, что метод новый.",
                                     f"Метод {ep} новый или уже существует под другим именем?", scope="code"))
    for fld in dict.fromkeys(FIELD_RE.findall(text)):
        ref = search(fld, ("db", "api", "openapi"))
        checks.append({"item": fld, "kind": "db_field", "status": "match" if ref else "missing", "doc_quote": f"`{fld}`",
                       "note": f"найдено в {ref['path'].rsplit('/', 1)[-1]}" if ref else "в схеме БД и DTO не найдено",
                       "code_ref": ref})
    if not files:
        checks.append({"item": ctx.project, "kind": "project", "status": "missing", "doc_quote": "",
                       "note": "в выбранных областях нет файлов для сверки", "code_ref": None})
    return checks, findings


# ---------- верификатор цитат и сборка

def verify_and_locate(findings: list[dict], text: str, reqs: list[dict], hs) -> tuple[list[dict], int]:
    kept, dropped, seen = [], 0, set()
    for f in findings:
        pos = find_quote(text, f["quote"])
        if pos < 0:
            dropped += 1
            continue
        f["quotes"] = [q for q in f.get("quotes", []) if find_quote(text, q) >= 0]
        key = (norm(f["quote"]), f["criterion"])
        if key in seen:
            continue
        seen.add(key)
        f["quote_verified"] = True
        f["_pos"] = pos
        if not f.get("req_id") and f["scope"] != "code":
            for r in reqs:
                if r["_start"] <= pos <= r["_end"]:
                    f["req_id"] = r["id"]
                    if not f.get("quotes"):
                        f["scope"] = "requirement"
                    break
        loc = section_path(text, pos, hs)
        rid = next((r["doc_id"] for r in reqs if r["id"] == f.get("req_id") and r["doc_id"] != "—"), None)
        f["location"] = loc + (f" → {rid}" if rid else "")
        kept.append(f)
    # «размытое слово» не дублирует более конкретную находку на том же месте
    spans = [(f["_pos"], f["_pos"] + len(f["quote"])) for f in kept if not f["title"].startswith("Размытая")]
    kept = [f for f in kept if not f["title"].startswith("Размытая")
            or not any(a <= f["_pos"] < b for a, b in spans)]
    kept.sort(key=lambda f: (SEV_ORDER[f["severity"]], f["_pos"]))
    for i, f in enumerate(kept, 1):
        f["id"] = f"F-{i:02d}"
        f.pop("_pos", None)
    return kept, dropped


def _agent_version() -> str:
    try:
        import subprocess
        root = Path(__file__).resolve().parent.parent
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, text=True, timeout=3)
        if sha.returncode == 0:
            return sha.stdout.strip() + " (демо-конвейер)"
    except Exception:  # noqa: BLE001
        pass
    return "dev (демо-конвейер)"


def assemble(doc, reqs, findings, code_checks, warnings, dropped, options, gitlab, file_name) -> dict:
    by = {s: sum(1 for f in findings if f["severity"] == s) for s in ("critical", "major", "minor")}
    crit_ids = [f["id"] for f in findings if f["severity"] == "critical"]
    if by["critical"]:
        status = "not_ready"
        text = (f"Начинать разработку и тестирование нельзя: {by['critical']} критичных дефекта делают требования "
                "неоднозначными или непроверяемыми. Сначала исправьте их и ответьте на вопросы ниже.")
    elif by["major"]:
        status = "ready_with_notes"
        text = "Критичных дефектов нет, разработку можно начинать, но существенные замечания лучше закрыть до оценки задачи."
    else:
        status = "ready"
        text = "Документ прошёл проверку: формулировки однозначны, противоречий не найдено."
    fix_first = crit_ids[:5] or [f["id"] for f in findings if f["severity"] == "major"][:3]

    questions, seen = [], set()
    for f in findings:
        q = (f.get("question") or "").strip()
        if q and norm(q) not in seen:
            seen.add(norm(q))
            questions.append({"id": f"Q-{len(questions) + 1:02d}", "text": q, "finding_ids": [f["id"]],
                              "req_ids": [f["req_id"]] if f.get("req_id") else []})

    tz = timezone(timedelta(hours=5))
    return {
        "contract_version": "1.0",
        "passport": {
            "document_title": options.get("title") or doc.title,
            "file_name": file_name,
            "date": datetime.now(tz).isoformat(timespec="seconds"),
            "model": "нет (демо-конвейер без LLM)",
            "agent_version": _agent_version(),
            "criteria": options.get("criteria") or ["iso29148", "ieee830", "checklist", "fintech"],
            "gitlab": {"project": gitlab.project, "ref": gitlab.ref} if gitlab is not None else None,
            "dropped_findings": dropped,
            "demo": True,
        },
        "document": {"text": doc.text, "format": doc.format, "pages": doc.pages},
        "requirements": [{k: v for k, v in r.items() if not k.startswith("_")} for r in reqs],
        "findings": findings,
        "code_checks": code_checks,
        "questions": questions,
        "summary": {"requirements_total": len(reqs), "findings_total": len(findings), "by_severity": by,
                    "critical_ids": crit_ids},
        "verdict": {"status": status, "ready": status != "not_ready", "text": text, "fix_first": fix_first},
        "warnings": warnings,
        "partial": False,
    }
