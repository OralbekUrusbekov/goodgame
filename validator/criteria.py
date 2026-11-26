"""Критерии, которые агент реально проверяет. Веб (web.criteria.catalog) берёт этот каталог."""
from __future__ import annotations

GROUPS = [
    {"id": "iso29148", "label": "Свойства требования и набора", "source": "ISO/IEC/IEEE 29148:2018, п. 5.2.5–5.2.6"},
    {"id": "ieee830", "label": "Качество спецификации", "source": "IEEE Std 830-1998, п. 4.3"},
    {"id": "context", "label": "Сверка с контекстом проекта и кодом", "source": "паспорт проекта, системы, политики, GitLab"},
]

# (код веба, группа, название, пункт, уровень, проверочный вопрос, код агента)
_C = [
    ("29148.necessary", "iso29148", "Необходимость", "п. 5.2.5", "requirement",
     "Если убрать требование, потеряется ли нужная возможность системы?", "necessary"),
    ("29148.appropriate", "iso29148", "Уместность уровня", "п. 5.2.5", "requirement",
     "Описывает ли требование «что», а не «как», без лишних проектных решений?", "appropriate"),
    ("29148.unambiguous", "iso29148", "Однозначность", "п. 5.2.5", "requirement",
     "Можно ли прочитать формулировку только одним способом?", "unambiguous"),
    ("29148.complete", "iso29148", "Полнота", "п. 5.2.5", "requirement",
     "Хватает ли формулировки, чтобы реализовать требование без догадок?", "complete"),
    ("29148.singular", "iso29148", "Единичность", "п. 5.2.5", "requirement",
     "Описывает ли требование ровно одну возможность или свойство?", "singular"),
    ("29148.feasible", "iso29148", "Реализуемость", "п. 5.2.5", "requirement",
     "Можно ли выполнить требование при заявленных ограничениях?", "feasible"),
    ("29148.verifiable", "iso29148", "Проверяемость", "п. 5.2.5", "requirement",
     "Можно ли написать тест с однозначным результатом?", "verifiable"),
    ("29148.correct", "iso29148", "Корректность", "п. 5.2.5", "requirement",
     "Точно ли требование отражает потребность, из которой выведено?", "correct"),
    ("29148.conforming", "iso29148", "Соответствие шаблону", "п. 5.2.5", "requirement",
     "Оформлено ли требование по принятому шаблону и словарю?", "conforming"),
    ("29148.set_complete", "iso29148", "Полнота набора", "п. 5.2.6", "set",
     "Покрыты ли все случаи, ошибки, границы и нефункциональные свойства?", "complete"),
    ("29148.consistent", "iso29148", "Непротиворечивость", "п. 5.2.6", "set",
     "Нет ли требований, которые нельзя выполнить одновременно?", "consistent"),
    ("29148.set_feasible", "iso29148", "Реализуемость набора", "п. 5.2.6", "set",
     "Реализуем ли набор целиком в рамках ограничений проекта?", "feasible"),
    ("29148.comprehensible", "iso29148", "Понятность", "п. 5.2.6", "set",
     "Ясно ли из набора, что ожидается от системы?", "comprehensible"),
    ("29148.able_to_be_validated", "iso29148", "Подтверждаемость", "п. 5.2.6", "set",
     "Можно ли доказать, что набор решает бизнес-задачу?", "able_to_be_validated"),
    ("830.ranked", "ieee830", "Важность и стабильность", "п. 4.3.5", "set",
     "Указаны ли важность и стабильность требований?", "ranked_importance_stability"),
    ("830.modifiable", "ieee830", "Модифицируемость", "п. 4.3.7", "set",
     "Есть ли структура, версия, нет ли дублирования?", "modifiable"),
    ("830.traceable", "ieee830", "Трассируемость", "п. 4.3.8", "set",
     "Ясно ли происхождение каждого требования?", "traceable"),
    ("context.goal_not_covered", "context", "Цель не покрыта", "контекст", "set",
     "Покрыта ли каждая цель проекта хотя бы одним требованием?", "goal_not_covered"),
    ("context.unjustified_requirement", "context", "Требование без обоснования", "контекст", "requirement",
     "Связано ли требование с целью проекта?", "unjustified_requirement"),
    ("context.scope_conflict", "context", "Конфликт со scope", "контекст", "requirement",
     "Не выходит ли требование за границы проекта?", "scope_conflict"),
    ("context.constraint_conflict", "context", "Конфликт с ограничениями", "контекст", "requirement",
     "Не нарушает ли требование SLA, сроки, технологии?", "constraint_conflict"),
    ("context.term_conflict", "context", "Расхождение терминов", "контекст", "requirement",
     "Совпадают ли термины с глоссарием проекта?", "term_conflict"),
    ("context.system_conflict", "context", "Расхождение с системами/кодом", "контекст", "requirement",
     "Согласуется ли требование с API, схемами и кодом систем?", "system_conflict"),
    ("context.policy_conflict", "context", "Конфликт с политиками", "контекст", "requirement",
     "Не противоречит ли требование политикам банка и ИБ?", "policy_conflict"),
]

_SOURCE = {g["id"]: g["source"] for g in GROUPS}
CRITERIA = [
    {"code": code, "group": group, "label": label, "clause": clause, "scope": scope, "question": question,
     "source": (_SOURCE[group].split(",")[0] + ", " + clause) if group in ("iso29148", "ieee830") else _SOURCE[group]}
    for code, group, label, clause, scope, question, _ in _C
]
BY_CODE = {item["code"]: item for item in CRITERIA}
REQUIREMENT_CODE = {agent: code for code, _, _, _, scope, _, agent in _C if code.startswith("29148.") and scope == "requirement"}
SET_CODE = {agent: code for code, _, _, _, scope, _, agent in _C if not code.startswith("context.") and scope == "set"}
CONTEXT_CODE = {agent: code for code, *_, agent in _C if code.startswith("context.")}
