"""Write or verify the generated OpenAPI contract (06: "FastAPI emits authoritative OpenAPI 3.1").

python -m app.openapi_export          # write ../contracts/openapi.json
python -m app.openapi_export --check  # exit 1 if the committed file is stale (CI drift check)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pydantic import SecretStr

from app.core.settings import Settings
from app.main import create_app

TARGET = Path(__file__).resolve().parents[2] / "contracts" / "openapi.json"


def render() -> str:
    # Placeholder settings: building the app never connects anywhere, and the schema has no secrets.
    settings = Settings(
        database_url=SecretStr("postgresql+psycopg://openapi@localhost/openapi"),
        session_secret=SecretStr("openapi-export"),
        app_encryption_key=SecretStr("openapi-export-key"),
    )
    schema = create_app(settings).openapi()
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def main(argv: list[str]) -> int:
    content = render()
    if "--check" in argv:
        if not TARGET.exists() or TARGET.read_text() != content:
            print(f"{TARGET} is stale; run: python -m app.openapi_export", file=sys.stderr)
            return 1
        print("openapi.json up to date")
        return 0
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(content)
    print(f"wrote {TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
