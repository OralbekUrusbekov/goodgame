# Матрица соответствия ТЗ

Аудит выполнен по `docs/TASK.md`, §2–§7. Статус «соответствует» означает, что пункт имеет реализацию и автоматическую офлайн-проверку с `LLM_PROVIDER=mock`, `LLM_CACHE=0`.

| Пункт ТЗ | Где реализовано | Какой тест проверяет | Статус |
|---|---|---|---|
| §2: репозиторий с агентом, исходным кодом, инструкцией запуска и зависимостями | `validator/pipeline.py:49`, `main.py:12`, `requirements.txt:1`, `README.md:5` | `tests/test_task_compliance.py::test_one_command_cli_produces_complete_russian_report` | Соответствует |
| §2: отчёт должен быть сформирован агентом по выданному документу | `validator/pipeline.py:145`, `validator/formats.py:42`, `main.py:40` | `tests/test_task_compliance.py::test_one_command_cli_produces_complete_russian_report` | Соответствует |
| §3.1: получить документ требований на вход | `main.py:14`, `validator/loader.py:187` | `tests/test_task_compliance.py::test_one_command_cli_produces_complete_russian_report` | Соответствует |
| §3.2: передать PDF, при поддержке — другие форматы | `validator/loader.py:187`, `README.md:35` | `tests/test_task_compliance.py::test_one_command_cli_produces_complete_russian_report` (MD/PDF/DOCX) | Соответствует |
| §3.3: выделить и проверить отдельные требования | `validator/pipeline.py:68`, `validator/pipeline.py:83` | `tests/test_segmentation_coverage.py`, `tests/test_checker.py`, `tests/test_task_compliance.py:79` | Соответствует |
| §3.4: проверить набор требований целиком | `validator/pipeline.py:86`, `validator/set_checker.py:38` | `tests/test_segmentation_coverage.py::test_mock_exercises_two_set_checks`, `tests/test_task_compliance.py:94` | Соответствует |
| §3.5: записать отчёт в файл | `validator/pipeline.py:150`, `validator/formats.py:42` | `tests/test_cli.py`, `tests/test_task_compliance.py:87` | Соответствует |
| §4: подключение к LLM и указание выбранной модели | `validator/config.py:56`, `validator/llm.py:542`, `README.md:16` | `tests/test_llm.py`, `tests/test_upgrade.py::test_fallback_switches_on_http_429`, `tests/test_task_compliance.py:101` | Соответствует |
| §5: критерии ISO/IEC/IEEE 29148 §5.2.5/5.2.6 и IEEE 830 §4.3 | `validator/criteria.py:4`, `validator/criteria.py:11`, `validator/prompts.py:156` | `tests/test_contract.py::test_payload_is_mapped_to_complete_web_contract`, `tests/test_web_agent.py::test_meta_reports_agent_and_validator_catalog` | Соответствует |
| §6: отчёт на русском языке, файл | `validator/reporter.py:62`, `validator/formats.py:42` | `tests/test_task_compliance.py:87`, `tests/test_task_compliance.py:93` | Соответствует |
| §6: паспорт — название, дата, модель, версия агента (коммит/тег) | `validator/reporter.py:68`, `validator/reporter.py:30`, `validator/pipeline.py:120` | `tests/test_task_compliance.py:99` | Соответствует |
| §6: таблица нарезки «ID отчёта — ID документа — формулировка»; свои ID и цитата для ненумерованных | `validator/segmenter.py:124`, `validator/reporter.py:81` | `tests/test_task_compliance.py:103`, `tests/test_task_compliance.py:122` | Соответствует |
| §6: проверка каждого требования | `validator/reporter.py:89`, `validator/checker.py:46` | `tests/test_reporter.py`, `tests/test_task_compliance.py:94` | Соответствует |
| §6: проверка всего набора | `validator/reporter.py:105`, `validator/set_checker.py:38` | `tests/test_reporter.py`, `tests/test_task_compliance.py:94` | Соответствует |
| §6: сводка — число требований, число нарушений, критичные непроверяемые/противоречивые/неполные дефекты | `validator/reporter.py:122`, `validator/reporter.py:189` | `tests/test_task_compliance.py:104`, `tests/test_reporter.py` | Соответствует |
| §6: итог о готовности к разработке и тестированию и первоочередные исправления | `validator/reporter.py:139`, `validator/reporter.py:224` | `tests/test_task_compliance.py:106` | Соответствует |
| §6: каждое нарушение привязано к цитате и месту документа | `validator/reporter.py:51`, `validator/verifier.py:34`, `validator/verifier.py:59` | `tests/test_task_compliance.py:110`, `tests/test_contract.py::test_fuzzy_table_quote_is_replaced_with_exact_document_fragment`, `tests/test_verifier.py` | Соответствует |
| §7: код агента и файл зависимостей | `validator/pipeline.py:1`, `requirements.txt:1` | Полный импорт и прогон: `.venv/bin/python -m pytest -q` | Соответствует |
| §7: README — ключ, размещение, команда и путь отчёта | `README.md:5`, `README.md:16`, `README.md:24`, `README.md:30` | Ручная структурная сверка аудита; команда проверена `tests/test_task_compliance.py:79` | Соответствует |
| §7: одна команда принимает путь входа и путь отчёта | `main.py:12`, `main.py:40`, `README.md:26` | `tests/test_task_compliance.py::test_one_command_cli_produces_complete_russian_report` | Соответствует |
| §7: секреты не попадают в репозиторий; корневой `.env.example` без ключа | `.gitignore:1`, `.env.example:1`, `validator/llm.py:135` | `tests/test_task_compliance.py::test_example_config_and_repository_contain_no_secrets` | Соответствует |
| §7: обязательны чтение документа, обращение к модели, запись отчёта | `validator/pipeline.py:64`, `validator/pipeline.py:65`, `validator/pipeline.py:150` | `tests/test_cli.py`, `tests/test_task_compliance.py::test_one_command_cli_produces_complete_russian_report` | Соответствует |

## Исправленные несоответствия

1. Нечётко совпавшая цитата считалась подтверждённой, но оставалась пересказом LLM. Веб не находил раздел и не подсвечивал таблицу. Добавлено восстановление буквальной подстроки источника и повторное вычисление места (`validator/verifier.py:34`, `validator/contract.py:100`).
2. CLI передавал `cache_enabled=True` при обычном запуске и тем самым игнорировал `LLM_CACHE=0`. Теперь явное переопределение происходит только для `--no-cache`, а значение окружения сохраняется (`main.py:36`).
3. Не было корневого README, требуемого §7. Добавлен `README.md` с настройкой Gemini, моделями, запуском CLI/web, форматами, этапами, критериями, mock-режимом и тестами.
4. Fallback-модель была задана только локальной конфигурацией, но не являлась безопасным значением по умолчанию и HTTP 429 не всегда переключал маршрут. Добавлены default `gemini-2.5-flash`, пример настройки и обработка 429 (`validator/config.py:75`, `validator/llm.py:486`, `.env.example:22`).
5. Не было комплексных проверок хакатонного ТЗ, веб-контракта и веб-агента. Добавлены `tests/test_task_compliance.py`, `tests/test_contract.py`, `tests/test_web_agent.py`; общий `tests/conftest.py` принудительно блокирует реальные LLM-вызовы.
