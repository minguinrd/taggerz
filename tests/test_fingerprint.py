from pathlib import Path

import pytest

from app import fingerprint as fp
from app import beets_adapter


def test_recording_ids_happy_path(tmp_path, monkeypatch):
    monkeypatch.setattr(fp.acoustid, "fingerprint_file", lambda *a, **k: (1.5, b"abc"))
    monkeypatch.setattr(fp.acoustid, "lookup", lambda *a, **k: {
        "status": "ok", "results": [{"score": 0.9, "recordings": [
            {"id": "r1"}, {"id": "r2"}, {"id": "r3"}, {"id": "r4"},
            {"id": "r5"}, {"id": "r6"}]}]})
    assert fp.recording_ids(Path("/x.flac"), "key") == ["r1", "r2", "r3", "r4", "r5"]


def test_low_score_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(fp.acoustid, "fingerprint_file", lambda *a, **k: (1.5, b"abc"))
    monkeypatch.setattr(fp.acoustid, "lookup", lambda *a, **k: {
        "status": "ok", "results": [{"score": 0.4, "recordings": [{"id": "r1"}]}]})
    assert fp.recording_ids(Path("/x.flac"), "key") == []


def test_no_results_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(fp.acoustid, "fingerprint_file", lambda *a, **k: (1.5, b"abc"))
    monkeypatch.setattr(fp.acoustid, "lookup", lambda *a, **k: {"status": "ok"})
    assert fp.recording_ids(Path("/x.flac"), "key") == []


def test_lookup_exception_wrapped(tmp_path, monkeypatch):
    monkeypatch.setattr(fp.acoustid, "fingerprint_file", lambda *a, **k: (1.5, b"abc"))
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(fp.acoustid, "lookup", boom)
    with pytest.raises(fp.AcoustidFailure) as exc:
        fp.recording_ids(Path("/x.flac"), "key")
    assert "/x.flac" in str(exc.value)
