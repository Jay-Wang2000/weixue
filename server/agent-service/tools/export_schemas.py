"""Write the API JSON Schemas to server/agent-service/schemas/ (a test fails when they drift).

    python -m tools.export_schemas
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

from weixue_agent.schemas import all_schemas  # noqa: E402

OUT = ROOT / "schemas"


def render(schema) -> str:
    return json.dumps(schema, indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def main() -> int:
    OUT.mkdir(exist_ok=True)
    for old in OUT.glob("*.schema.json"):
        old.unlink()
    for name, schema in all_schemas().items():
        (OUT / name).write_text(render(schema))
    print(f"wrote {len(all_schemas())} schemas to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
