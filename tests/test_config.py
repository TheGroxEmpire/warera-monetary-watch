import os
import subprocess
import sys
from pathlib import Path

from warera_monetary_watch.config import normalize_base_path


def test_normalize_base_path_handles_root() -> None:
    assert normalize_base_path("/") == "/"


def test_normalize_base_path_adds_leading_slash() -> None:
    assert normalize_base_path("monetary-watch/") == "/monetary-watch"


def test_app_import_does_not_require_runtime_settings(tmp_path: Path) -> None:
    env = os.environ.copy()
    for key in ("DATABASE_URL", "WARERA_API_BASE_URL", "WARERA_API_TOKEN"):
        env.pop(key, None)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")

    subprocess.run(
        [sys.executable, "-c", "import warera_monetary_watch.api.app"],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
