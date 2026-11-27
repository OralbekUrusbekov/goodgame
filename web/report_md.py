"""Запасной рендер отчёта Markdown по контракту результата.

Используется демо-конвейером и только если настоящий конвейер не вернул report_markdown.
Основной отчёт формирует validator/ — так консоль и веб выдают одинаковый файл.
"""
from __future__ import annotations

SEV = {"critical": "критичный", "major": "существенный", "minor": "мелкий"}
STATUS = {"not_ready": "Не готово к разработке", "ready_with_notes": "Готово с замечаниями", "ready": "Готово к разработке"}


def _cell(s) -> str:
    return str(s if s is not None else "—").replace("|", "\\|").replace("\n", " ")


def render(result: dict) -> str:
    p = result.get("passport", {})
    reqs = result.get("requirements", [])
    finds = result.get("findings", [])
    unverified = result.get("unverified_findings", [])
    summ = result.get("summary", {})
    verdict = result.get("verdict", {})
    by_id = {f["id"]: f for f in finds}
    out: list[str] = []
    w = out.append

    w(f"# Отчёт о проверке требований: {p.get('document_title', '')}\n")
    if result.get("partial"):
        w("> **Отчёт неполный** — прогон был прерван, выводы по документу целиком делать рано.\n")

    w("## 1. Паспорт прогона\n")
    w("| Поле | Значение |\n| :--- | :--- |")
    gl = p.get("gitlab")
    for k, v in [
        ("Документ", p.get("document_title")),
        ("Файл", p.get("file_name")),
        ("Дата", p.get("date")),
        ("Модель", p.get("model")),
        ("Версия агента", p.get("agent_version")),
        ("Критерии", ", ".join(p.get("criteria", []))),
        ("Проект GitLab", f"{gl['project']} @ {gl['ref']}" if gl else "не подключался"),
        ("Отброшено находок без подтверждённой цитаты", p.get("dropped_findings", 0)),
    ]:
        w(f"| {k} | {_cell(v)} |")
    w("")

    w("## 2. Нарезка требований\n")
    w("| ID в отчёте | ID в документе | Краткая формулировка |\n| :--- | :--- | :--- |")
    for r in reqs:
        w(f"| {r['id']} | {_cell(r.get('doc_id') or '—')} | {_cell(r.get('summary'))} |")
    w("")

    w("## 3. Проверка каждого требования\n")
    for r in reqs:
        rf = [f for f in finds if f.get("req_id") == r["id"]]
        w(f"### {r['id']} ({r.get('doc_id') or 'без номера'})\n")
        w(f"> {r.get('quote', '').strip()}\n")
        if not rf:
            w("Нарушений не найдено.\n")
            continue
        for f in rf:
            w(_finding(f))
    w("## 4. Проверка всего набора требований\n")
    sf = [f for f in finds if f.get("scope") in ("set", "code") or not f.get("req_id")]
    if not sf:
        w("Нарушений не найдено.\n")
    for f in sf:
        w(_finding(f))

    if result.get("code_checks"):
        w("### Сверка с кодом GitLab\n")
        w("| Что | Статус | Где в коде | Комментарий |\n| :--- | :--- | :--- | :--- |")
        st = {"match": "совпадает", "mismatch": "расходится", "missing": "не найдено"}
        for c in result["code_checks"]:
            ref = c.get("code_ref") or {}
            loc = f"{ref.get('path')}:{ref.get('line')}" if ref.get("path") else "—"
            w(f"| `{_cell(c['item'])}` | {st.get(c['status'], c['status'])} | {_cell(loc)} | {_cell(c.get('note'))} |")
        w("")

    w("## 5. Сводка\n")
    bs = summ.get("by_severity", {})
    w(f"- Требований: **{summ.get('requirements_total', len(reqs))}**")
    w(f"- Нарушений: **{summ.get('findings_total', len(finds))}** "
      f"(критичных — {bs.get('critical', 0)}, существенных — {bs.get('major', 0)}, мелких — {bs.get('minor', 0)})")
    crit = [by_id[i] for i in summ.get("critical_ids", []) if i in by_id]
    if crit:
        w("\nКритичные дефекты:\n")
        for f in crit:
            w(f"- **{f['id']}** {f.get('title', '')} — «{f.get('quote', '')}» ({f.get('location', '')})")
    w("")

    w("### Неподтверждённые выводы\n")
    w("Эти выводы показываются отдельно и не влияют на вердикт.\n")
    if not unverified:
        w("Нет.\n")
    for finding in unverified:
        w(_finding(finding))

    w("## 6. Итог\n")
    w(f"**{STATUS.get(verdict.get('status'), verdict.get('status', ''))}.** {verdict.get('text', '')}\n")
    if verdict.get("fix_first"):
        w("Исправить в первую очередь:\n")
        for i, fid in enumerate(verdict["fix_first"], 1):
            f = by_id.get(fid)
            if f:
                w(f"{i}. {f['id']} — {f.get('fix') or f.get('title')}")
        w("")

    w("## 7. Уточняющие вопросы к автору требований\n")
    qs = result.get("questions", [])
    if not qs:
        w("Вопросов нет.\n")
    for i, q in enumerate(qs, 1):
        refs = ", ".join(q.get("finding_ids", []))
        w(f"{i}. {q['text']}" + (f" _(к {refs})_" if refs else ""))
    w("")
    return "\n".join(out)


def _finding(f: dict) -> str:
    lines = [
        f"- **{f['id']} · {SEV.get(f.get('severity'), f.get('severity'))} · {f.get('criterion_label') or f.get('criterion')}**"
        f" ({f.get('source', '')}) — {f.get('title', '')}",
        f"  - Где: {f.get('location', '—')}",
        f"  - Цитата: «{f.get('quote', '')}»",
    ]
    for q in f.get("quotes") or []:
        lines.append(f"  - Цитата: «{q}»")
    if f.get("defect"):
        lines.append(f"  - Дефект: {f['defect']}")
    if f.get("fix"):
        lines.append(f"  - Как исправить: {f['fix']}")
    if f.get("question"):
        lines.append(f"  - Вопрос автору: {f['question']}")
    if f.get("code_ref"):
        lines.append(f"  - Код: `{f['code_ref'].get('path')}`")
    return "\n".join(lines) + "\n"
