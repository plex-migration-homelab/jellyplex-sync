import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from jellyplex.sync import sync

engine = importlib.import_module("jellyplex.sync")


@pytest.fixture
def libraries(tmp_path):
    source, target = tmp_path / "plex", tmp_path / "jellyfin"
    movie = source / "Example (2020)"
    movie.mkdir(parents=True)
    target.mkdir()
    (movie / "Example (2020).mkv").write_bytes(b"video")
    return source, target, movie


def run(source, target, **kwargs):
    return sync(str(source), str(target), convert_to="jellyfin", delete=True, **kwargs)


@pytest.mark.parametrize("level", ["library", "movie", "asset"])
def test_reports_failed_removal(libraries, level, monkeypatch, caplog):
    source, target, movie = libraries
    (movie / "extras").mkdir()
    assert run(source, target) == 0
    parents = {"library": target, "movie": target / movie.name,
               "asset": target / movie.name / "extras"}
    stray = parents[level] / "stray.txt"
    stray.write_text("keep")
    original_remove = engine.utils.remove
    monkeypatch.setattr(engine.utils, "remove", lambda path: path != stray and original_remove(path))
    assert run(source, target) == 4
    assert stray.is_file()
    assert "1 removals failed" in caplog.text


def test_conflict_and_failed_removal(libraries, monkeypatch):
    source, target, movie = libraries
    (movie / "duplicate.mkv").write_bytes(b"duplicate")
    (target / "stray").touch()
    monkeypatch.setattr(engine.utils, "remove", lambda _: False)
    assert run(source, target) == 4
    assert run(source, target, partial_path=str(movie)) == 3


def test_read_only_modes_and_hidden_source(libraries):
    source, target, movie = libraries
    missing = target / "missing"
    for mode in ("dry_run", "verify_only"):
        assert run(source, missing, create=True, **{mode: True}) == 0
        assert not missing.exists()
    stray = target / "stray"
    stray.touch()
    assert run(source, target, verify_only=True, update_filenames=True) == 0
    assert stray.exists()
    hidden = movie.rename(source / ".stversions")
    assert run(source, target) == 1
    assert run(source, target, partial_path=str(hidden)) == 0
    assert stray.exists()


def test_marker_preserves_mirrors_and_nested_target(libraries):
    source, target, movie = libraries
    extras = movie / "extras"
    extras.mkdir()
    (extras / "clip.mkv").write_bytes(b"clip")
    assert run(source, target) == 0
    mirror = target / movie.name
    assert os.path.samefile(movie / "Example (2020).mkv", mirror / "Example (2020).mkv")
    stray = target / "stray" / "nested"
    stray.mkdir(parents=True)
    (stray / engine.IGNORE_MARKER).touch()
    (extras / engine.IGNORE_MARKER).touch()
    (movie / engine.IGNORE_MARKER).touch()
    assert run(source, target) == 0
    assert mirror.is_dir() and stray.is_dir()
    (movie / engine.IGNORE_MARKER).unlink()
    (extras / "clip.mkv").unlink()
    assert run(source, target, partial_path=str(movie)) == 0
    assert (mirror / "extras" / "clip.mkv").exists()
    (mirror / engine.IGNORE_MARKER).touch()
    (movie / "Example (2020).mkv").unlink()
    assert run(source, target) == 0
    assert (mirror / "Example (2020).mkv").exists()


def test_cli_respects_marker_on_pooled_view(libraries, tmp_path):
    source, target, movie = libraries
    pooled = tmp_path / "pooled"
    (pooled / movie.name).mkdir(parents=True)
    (pooled / movie.name / engine.IGNORE_MARKER).touch()
    absent = target / "absent"
    result = subprocess.run(
        [sys.executable, "-m", "jellyplex.sync", str(source), str(absent),
         "--partial", str(movie), "--convert-to", "jellyfin", "--delete",
         "--create", "--marker-source-root", str(pooled)],
        env={**os.environ, "PYTHONPATH": str(Path(engine.__file__).resolve().parents[1])},
        capture_output=True, text=True, check=True,
    )
    assert "Skipping marked folder" in result.stderr
    assert not absent.exists()


@pytest.mark.parametrize("side", ["source", "target"])
def test_marked_root_stops_cleanup(libraries, side):
    source, target, _ = libraries
    protected = source if side == "source" else target
    (protected / engine.IGNORE_MARKER).touch()
    stray = target / "stray"
    stray.touch()
    assert run(source, target) == 0
    assert stray.exists()


def test_nested_pooled_target_marker_blocks_partial_writes(libraries, tmp_path):
    source, target, movie = libraries
    extras = movie / "extras"
    extras.mkdir()
    (extras / "clip.mkv").write_bytes(b"clip")
    pooled = tmp_path / "pool"
    protected = pooled / movie.name / "extras"
    protected.mkdir(parents=True)
    (protected / engine.IGNORE_MARKER).touch()
    assert run(source, target, partial_path=str(movie), marker_target_root=str(pooled)) == 0
    assert (target / movie.name / "Example (2020).mkv").exists()
    assert not (target / movie.name / "extras").exists()
