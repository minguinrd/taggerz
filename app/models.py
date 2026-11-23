"""Domain constants and data shapes for the proposal queue."""

from dataclasses import dataclass

# Rescan stickiness depends on these exact strings (global constraint).
STATUSES = ("pending", "awaiting-review", "done", "skipped", "error")
KINDS = ("needs-tags", "organize-only", "unidentified", "manual")


@dataclass
class Proposal:
    id: int
    path: str
    mtime: float
    size: int
    kind: str
    status: str
    current_tags: dict
    candidates: list[dict]
    error: str | None
    beets_item_id: int | None
