"""Project-wide research freeze. Missing/invalid policy fails closed."""
import json
from pathlib import Path

POLICY_FILE = Path(__file__).resolve().parents[1] / "config/research_policy.json"


class ResearchOnlyError(PermissionError):
    pass


def require(capability: str) -> None:
    try:
        policy = json.loads(POLICY_FILE.read_text(encoding="utf-8"))
        allowed = policy["capabilities"].get(capability) is True
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        allowed = False
    if not allowed:
        raise ResearchOnlyError(
            f"{capability} frozen by PROJECT_REVIEW_2026-10-22; research summaries only")
