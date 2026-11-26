from __future__ import annotations

import argparse
import sys
import traceback

from validator.config import load_config
from validator.llm import mask_secrets
from validator.pipeline import run_pipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Статическая ИИ-проверка документа требований")
    parser.add_argument("source", help="Путь к .md, .txt, .pdf или .docx")
    parser.add_argument("report", help="Путь для Markdown-отчёта")
    parser.add_argument("--model", help="Переопределить модель")
    parser.add_argument("--provider", choices=("gemini", "openai", "mock"), help="LLM-провайдер")
    parser.add_argument("--batch-size", type=int, help="Число требований в одном запросе проверки")
    parser.add_argument("--context", nargs="+", help="Файлы или каталог контекста проекта")
    parser.add_argument(
        "--domain-check", action=argparse.BooleanOptionalAction, default=True,
        help="Доменная проверка рисков (по умолчанию включена)",
    )
    parser.add_argument("--tests", action="store_true", help="Сгенерировать черновики тест-кейсов")
    parser.add_argument("--previous", help="Предыдущий report.json для сравнения")
    parser.add_argument("--pdf", action="store_true", help="Дополнительно создать PDF, если доступно")
    parser.add_argument("--no-cache", action="store_true", help="Не использовать кэш LLM")
    parser.add_argument("--verbose", action="store_true", help="Подробный прогресс")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = None
    try:
        settings = load_config(
            args.provider, args.model, args.batch_size,
            False if args.no_cache else None,
        )
        result = run_pipeline(
            args.source, args.report, settings, args.verbose,
            context_paths=args.context, domain_check=args.domain_check,
            generate_tests=args.tests, previous_report=args.previous, also_pdf=args.pdf,
        )
        print(f"Отчёт: {result.report_path}")
        print(f"JSON: {result.json_path}")
        print(f"Требований: {result.requirements_count}; нарушений: {result.findings_count}")
        return 0
    except Exception as error:
        if args.verbose:
            traceback.print_exc()
        else:
            secrets = tuple(filter(None, (
                settings.gemini_api_key if settings else None,
                settings.openai_api_key if settings else None,
            )))
            message = mask_secrets(" ".join(str(error).split()), secrets) or type(error).__name__
            if len(message) > 400:
                message = message[:397] + "..."
            print(f"Ошибка: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
