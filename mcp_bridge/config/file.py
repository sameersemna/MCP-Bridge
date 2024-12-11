import json
from typing import Any

def load_config(file: str) -> dict[str, Any]:
    with open(file, "r") as f:
        return json.load(f)
