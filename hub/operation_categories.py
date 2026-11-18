"""Read-only classification by admitted tool identity, never request summaries."""

from shared.agent_lifecycle import DEVICE_ACTIONS
from shared.util import DevError

PROJECT_MAP_TOOLS = ("project_knowledge_build", "project_knowledge_query")
# SQLite matches a partial index against the literal predicate in the query.
DEVICE_ACTION_PREDICATE = (
    "tool IN (" + ",".join(f"'{tool}'" for tool in sorted(DEVICE_ACTIONS)) + ")"
)


def operation_category_sql(category="all", prefix=""):
    if category not in {"all", "tools", "project_map"}:
        raise DevError("INVALID_CATEGORY", "Unknown operation category", 422)
    if category == "all":
        return "1=1", []
    operator = "IN" if category == "project_map" else "NOT IN"
    return f"{prefix}tool {operator} (?,?)", list(PROJECT_MAP_TOOLS)
