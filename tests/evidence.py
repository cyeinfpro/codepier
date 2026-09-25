"""Small evidence helpers; no environment-wide browser state."""
from pathlib import Path


def evidence_path(path: str | Path) -> str:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)
