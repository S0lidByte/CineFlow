"""Export OpenAPI schema definition from FastAPI application directly without running a server."""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Ensure src is on Python path
ROOT_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from main import app


def export_openapi(output_path: Path | None = None) -> Path:
    """Generate and write the OpenAPI JSON schema within the backend directory."""
    target_path = (output_path or (ROOT_DIR / "openapi.json")).resolve()
    try:
        target_path.relative_to(ROOT_DIR)
    except ValueError as error:
        raise ValueError(f"OpenAPI output must be inside {ROOT_DIR}") from error

    openapi_schema = app.openapi()
    with target_path.open("w", encoding="utf-8") as schema_file:
        json.dump(openapi_schema, schema_file, indent=2, sort_keys=True)
        schema_file.write("\n")

    print(f"OpenAPI schema successfully exported to: {target_path}")
    return target_path


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    export_openapi(out)
