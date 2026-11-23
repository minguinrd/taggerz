"""AcoustID fingerprinting. Imports only ``acoustid`` (pyacoustid).

No beets imports here; all beets interaction stays in ``app/beets_adapter.py``.
"""

from pathlib import Path

import acoustid


class AcoustidFailure(Exception):
    """Raised when fingerprinting or AcoustID lookup fails."""


def recording_ids(path: Path, app_key: str) -> list[str]:
    """Return up to 5 AcoustID recording IDs for ``path``.

    Mirrors beets' chroma plugin rules: fingerprint with fpcalc, look up
    AcoustID, require ``status == "ok"`` and a top result score >= 0.5,
    then take up to 5 recording IDs from that result. Any exception is
    wrapped in :class:`AcoustidFailure` with the file path in the message.
    """
    try:
        duration, fp_bytes = acoustid.fingerprint_file(
            str(path), force_fpcalc=True
        )
        res = acoustid.lookup(
            app_key, fp_bytes.decode(), duration,
            meta="recordings releases", timeout=10,
        )
    except Exception as exc:  # noqa: BLE001 - wrap any failure
        raise AcoustidFailure(f"{path}: {exc}") from exc

    if not isinstance(res, dict) or res.get("status") != "ok":
        return []
    results = res.get("results") or []
    if not results:
        return []
    top = results[0]
    if top.get("score", 0) < 0.5:
        return []
    recordings = top.get("recordings") or []
    ids: list[str] = []
    for rec in recordings[:5]:
        rid = rec.get("id") if isinstance(rec, dict) else None
        if rid is not None:
            ids.append(rid)
    return ids[:5]
