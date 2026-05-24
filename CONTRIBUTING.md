# Contributing

Thanks for taking a look at WarEra Monetary Watch.

## Development Setup

Use Python 3.12 or newer. With uv:

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
```

With pip:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
pytest
ruff check .
```

## Local Configuration

Copy `.env.example` to `.env` and set `WARERA_API_TOKEN` for collector runs.
Never commit `.env`, SQL dumps, database files, or files from `backups/`.

## Database Changes

Schema changes should include an Alembic migration under `migrations/versions/`.
Run the test suite after changing models, migrations, collectors, or analytics.

## Pull Requests

Keep changes focused, include tests for behavior changes, and describe any
operator-facing migration or configuration impact.
