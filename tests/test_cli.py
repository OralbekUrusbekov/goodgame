import json
import os
from pathlib import Path
import subprocess
import sys


def test_end_to_end_cli_with_mock(tmp_path: Path) -> None:
    source = tmp_path / "requirements.md"
    report = tmp_path / "out" / "report.md"
    source.write_text("# Сервис\n\n## Функции\n* Сервис сохраняет настройку пользователя.\n", encoding="utf-8")
    root = Path(__file__).parents[1]
    env = os.environ.copy()
    env["LLM_PROVIDER"] = "mock"

    completed = subprocess.run(
        [sys.executable, "main.py", str(source), str(report)], cwd=root, env=env,
        capture_output=True, text=True, timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert report.is_file()
    json_path = report.with_suffix(".json")
    assert json_path.is_file()
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["metadata"]["provider"] == "mock"
    assert payload["requirements"][0]["quote_verified"] is True
