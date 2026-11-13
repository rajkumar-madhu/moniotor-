#!/usr/bin/env python
import os
import sys
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Load repo .env into os.environ without overriding values already set (shell / CI win)."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ[key] = value


if __name__ == "__main__":
    _load_dotenv(Path(__file__).resolve().parent / ".env")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "linkedeye.settings")
    from django.core.management import execute_from_command_line
    execute_from_command_line(sys.argv)
