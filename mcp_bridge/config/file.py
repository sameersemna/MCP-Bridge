import json
from pathlib import Path
from typing import Any


def load_config(file: str) -> dict[str, Any]:
    candidate_path = Path(file).expanduser()
    if not candidate_path.is_absolute():
        candidate_path = (Path.cwd() / candidate_path).resolve()
    else:
        candidate_path = candidate_path.resolve()

    workspace_root = Path.cwd().resolve()
    if workspace_root not in candidate_path.parents and candidate_path != workspace_root:
        raise ValueError(f'config path "{file}" resolves outside the workspace root')

    if not candidate_path.exists():
        raise FileNotFoundError(f'the "{file}" file was not found')

    if not candidate_path.is_file():
        raise ValueError(f'config path "{file}" must point to a file')

    try:
        with candidate_path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError as exc:
        raise ValueError(f'failed to parse json from "{file}"') from exc
    except OSError as exc:
        raise OSError(f'there was an error reading the "{file}" file') from exc
