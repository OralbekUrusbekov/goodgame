from __future__ import annotations

import argparse
import json
from pathlib import Path

from .matcher import evaluate, load_cases, render_results


def main() -> int:
    parser = argparse.ArgumentParser(description="Офлайн eval отчёта")
    parser.add_argument("report")
    parser.add_argument("cases")
    args = parser.parse_args()
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    result = evaluate(report, load_cases(args.cases))
    rendered = render_results(result)
    print(rendered, end="")
    output = Path("eval/results.md")
    output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
