"""Domain constants and data shapes for the proposal queue."""

from dataclasses import dataclass

# Rescan stickiness depends on these exact strings (global constraint).
STATUS_PENDING = "pending"
STATUS_AWAITING_REVIEW = "awaiting-review"
STATUS_DONE = "done"
STATUS_SKIPPED = "skipped"
STATUS_ERROR = "error"
STATUSES = (
    STATUS_PENDING, STATUS_AWAITING_REVIEW, STATUS_DONE,
    STATUS_SKIPPED, STATUS_ERROR,
)

KIND_NEEDS_TAGS = "needs-tags"
KIND_ORGANIZE_ONLY = "organize-only"
KIND_UNIDENTIFIED = "unidentified"
KIND_MANUAL = "manual"
KINDS = (
    KIND_NEEDS_TAGS, KIND_ORGANIZE_ONLY, KIND_UNIDENTIFIED, KIND_MANUAL,
)


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
