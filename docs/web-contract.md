# Контракт «конвейер агента ↔ веб-сервис»

Документ для автора агента (`validator/`). Веб (`web/`) вызывает конвейер **тем же кодом**, что и консоль,
поэтому отчёт в браузере и `report.md` из CLI совпадают байт в байт.

Пока `validator.pipeline.run_pipeline` не существует, веб работает в **демо-режиме** (`web/mock_pipeline.py`)
и отдаёт результат ровно этой формы. Как только функция появится — веб переключится на неё сам
(принудительно включить демо: `VALIDATOR_MOCK=1`).

## 1. Точка входа

```python
# validator/pipeline.py
def run_pipeline(
    doc_path: str,                     # путь к PDF / MD / DOCX
    on_progress=None,                  # callable(stage: str, info: dict | None)
    gitlab=None,                       # web.gitlab.GitLabContext | None — см. §4
    options: dict | None = None,       # {"title", "work_type", "criteria": [...], "scopes": [...]}
) -> dict:                             # результат по схеме §3
    ...
```

Веб передаёт только те аргументы, которые есть в сигнатуре (через `inspect.signature`),
так что `gitlab` и `options` можно добавить позже.

CLI делает то же самое:

```python
result = run_pipeline(path, on_progress=print_stage)
Path(report_path).write_text(result["report_markdown"], encoding="utf-8")
```

## 2. Этапы (`on_progress`)

| stage                   | info (всё необязательно)                                   |
|-------------------------|------------------------------------------------------------|
| `reading`               | `{"message": "..."}`                                       |
| `splitting`             | `{"total": 18}`                                            |
| `checking_requirements` | `{"done": 11, "total": 18, "findings_so_far": 7}`          |
| `checking_set`          | `{"findings_so_far": 12}`                                  |
| `checking_gitlab`       | `{"skipped": true}` если проект не выбран                  |
| `verifying_quotes`      | `{"dropped": 2}` — сколько находок отброшено без цитаты    |
| `summary`               | —                                                          |
| `warning`               | `{"code": "llm_429", "message": "повтор через 30 с"}`      |

`done` и `error` веб ставит сам. Если `on_progress` бросил исключение `RunCancelled` —
пользователь нажал «Остановить», конвейер должен его не глотать.

## 3. Результат

```jsonc
{
  "contract_version": "1.0",
  "passport": {
    "document_title": "Уведомление о расходах по карте",
    "file_name": "requirements.pdf",
    "date": "2026-11-26T16:42:00+05:00",
    "model": "gemini-2.5-flash",
    "agent_version": "v1.0 (a1b2c3d)",          // git describe / rev-parse
    "criteria": ["iso29148", "ieee830", "checklist", "fintech"],
    "gitlab": {"project": "retail/card-spend-alerts", "ref": "main"},  // или null
    "dropped_findings": 2                         // отброшено верификатором цитат
  },
  "document": {
    "text": "...полный извлечённый текст...",   // ВАЖНО: цитаты ищутся именно в нём
    "format": "pdf",
    "pages": 6
  },
  "requirements": [
    {"id": "R-01", "doc_id": "Ф-1", "section": "Функциональные требования",
     "summary": "Порог от 1 000 до 1 000 000 ₸, по умолчанию выключено",
     "quote": "Держатель активной карты KZT задаёт целый порог от 1 000 до 1 000 000 ₸ включительно."}
  ],
  "findings": [
    {
      "id": "F-01",
      "req_id": "R-02",                 // null для находок по набору
      "scope": "requirement",           // requirement | set | code
      "criterion": "29148.consistent",  // код критерия
      "criterion_label": "Непротиворечивость",
      "source": "ISO/IEC/IEEE 29148:2018, п. 5.2.6",
      "verdict": "fail",                // pass | fail | n_a  (pass/n_a можно не присылать)
      "severity": "critical",           // critical | major | minor
      "title": "Месяц покупки определён двумя способами",
      "quote": "Месяц определяется по дате совершения покупки в Алматы",
      "quotes": ["Февральский счётчик вырос на 4 000 ₸"],   // доп. цитаты, необязательно
      "location": "Часть A → Функциональные требования → Ф-2",
      "defect": "Ф-2 считает месяц по дате совершения, критерий приёмки 2 — по дате проведения.",
      "fix": "Выбрать одну дату и привести к ней Ф-2 и критерий приёмки.",
      "question": "Какая дата определяет месяц расходов: совершения или проведения?",
      "quote_verified": true,
      "code_ref": null                  // для scope=code: {"path": "...", "line": 42, "url": "..."}
    }
  ],
  "code_checks": [                       // вкладка «ТЗ ↔ код», пусто без GitLab
    {"item": "/alerts/saveLimit", "kind": "endpoint", "status": "match",   // match | mismatch | missing
     "doc_quote": "/alerts/saveLimit", "note": "найден в AlertsController.java",
     "code_ref": {"path": "src/.../AlertsController.java", "line": 31, "url": "https://..."}}
  ],
  "questions": [
    {"id": "Q-01", "text": "Какая дата определяет месяц расходов?", "finding_ids": ["F-01"], "req_ids": ["R-02"]}
  ],
  "summary": {
    "requirements_total": 18,
    "findings_total": 17,
    "by_severity": {"critical": 4, "major": 9, "minor": 4},
    "critical_ids": ["F-01", "F-02", "F-03", "F-04"]
  },
  "verdict": {
    "status": "not_ready",              // not_ready | ready_with_notes | ready
    "ready": false,
    "text": "Начинать разработку нельзя: ...",
    "fix_first": ["F-01", "F-02"]
  },
  "warnings": [{"stage": "checking_gitlab", "code": "gitlab_401", "message": "..."}],
  "partial": false,                     // true, если прогон оборвался, но часть находок есть
  "report_markdown": "# Отчёт о проверке требований\n..."   // ровно то, что пишет CLI
}
```

Минимально обязательные поля: `passport`, `document.text`, `requirements`, `findings`
(с `quote`, `severity`, `criterion`, `title`/`defect`), `verdict.status`, `report_markdown`.
Остальное веб достроит сам (`summary`, `questions` — из находок).

## 4. GitLab-контекст

Если пользователь выбрал проект, в `gitlab` приходит объект `web.gitlab.GitLabContext`:

```python
ctx.project        # "retail/card-spend-alerts"
ctx.ref            # "main"
ctx.scopes         # ["api", "openapi", "db", "docs"]
ctx.files()        # [{"path": "...", "category": "api"|"openapi"|"db"|"docs"}]  — только выбранные scopes
ctx.read(path)     # str, не больше 200 КБ на файл, кэшируется
ctx.collect(max_files=60, max_bytes=600_000)   # [{"path", "category", "content"}]
ctx.web_url(path, line=None)                    # ссылка на файл в GitLab для code_ref
```

Доступ только на чтение (`read_api`, `read_repository`); токен в контекст не попадает.
