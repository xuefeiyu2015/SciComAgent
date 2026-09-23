"""Tests for api.assets — the image storage/manifest module.

`tests/conftest._isolate_job_mirrors` already points `api.assets._IMAGES_DIR`
at a throwaway `tmp_path` for every test in the suite (mirroring how it
isolates `api.jobs`), so nothing here writes into the repo's own outputs/.
"""

from __future__ import annotations

import json

import pytest

from api import assets
from api.schema import ImageAsset, ImageKind


# --- image_dir ----------------------------------------------------------

def test_image_dir_creates_and_returns_session_directory():
    path = assets.image_dir("sess1")
    assert path.is_dir()
    assert path.name == "sess1"


def test_image_dir_is_idempotent():
    first = assets.image_dir("sess1")
    second = assets.image_dir("sess1")
    assert first == second
    assert first.is_dir()


@pytest.mark.parametrize("bad", ["../escape", "a/b", "a\\b", "..", ""])
def test_image_dir_rejects_unsafe_session_id(bad):
    with pytest.raises(ValueError):
        assets.image_dir(bad)


# --- image_path -----------------------------------------------------------

def test_image_path_cover_is_cover_png():
    path = assets.image_path("sess1", ImageKind.cover)
    assert path.name == "cover.png"
    assert path.parent == assets.image_dir("sess1")


def test_image_path_explainer_uses_claim_id_verbatim():
    """c17 -> c17.png, never cc17.png (the ledger id already has its 'c')."""
    path = assets.image_path("sess1", ImageKind.explainer, claim_id="c17")
    assert path.name == "c17.png"


@pytest.mark.parametrize("bad", ["../escape", "a/b", "a\\b", ".."])
def test_image_path_rejects_unsafe_session_id(bad):
    with pytest.raises(ValueError):
        assets.image_path(bad, ImageKind.cover)


@pytest.mark.parametrize("bad", ["../c1", "c1/../c2", "c\\1", ".."])
def test_image_path_rejects_unsafe_claim_id(bad):
    with pytest.raises(ValueError):
        assets.image_path("sess1", ImageKind.explainer, claim_id=bad)


def test_image_path_cover_with_empty_claim_id_is_fine():
    """Cover callers normally pass no claim_id at all; that must still work."""
    path = assets.image_path("sess1", ImageKind.cover, claim_id="")
    assert path.name == "cover.png"


@pytest.mark.parametrize("bad", ["../../etc/passwd", "../escape", "a/b", "a\\b", ".."])
def test_image_path_rejects_unsafe_claim_id_even_for_cover(bad):
    """claim_id is unused by the cover filename, but an attacker-shaped value
    passed alongside kind=cover must still raise, not be silently ignored.
    """
    with pytest.raises(ValueError):
        assets.image_path("sess1", ImageKind.cover, claim_id=bad)


# --- repo_relative ----------------------------------------------------------
#
# #53: assert what the real path-construction code PRODUCES, not what a test
# hands write_manifest/read_manifest as a literal string (that only proves
# the round-trip doesn't mangle a string — see
# test_manifest_path_recorded_repo_relative_posix_round_trips_unchanged
# below, and the issue's own account of why that test didn't catch the bug).

def test_repo_relative_of_a_real_cover_path_is_repo_relative_posix():
    path = assets.image_path("sess1", ImageKind.cover)
    result = assets.repo_relative(path)

    assert result == "outputs/images/sess1/cover.png"
    assert not result.startswith("/")
    assert "\\" not in result
    assert str(assets._REPO_ROOT) not in result


def test_repo_relative_of_a_real_explainer_path_is_repo_relative_posix():
    path = assets.image_path("sess1", ImageKind.explainer, claim_id="c17")
    result = assets.repo_relative(path)

    assert result == "outputs/images/sess1/c17.png"
    assert not result.startswith("/")
    assert "\\" not in result
    assert str(assets._REPO_ROOT) not in result


def test_repo_relative_of_a_real_image_dir_path_is_repo_relative_posix():
    """`image_dir`'s return value must relativize the same way `image_path`'s
    does — both are `Path`s built from the same `_IMAGES_DIR`."""
    path = assets.image_dir("sess1") / "cover.png"
    assert assets.repo_relative(path) == "outputs/images/sess1/cover.png"


# --- write_manifest / read_manifest ----------------------------------------

def _full_asset(**overrides) -> ImageAsset:
    fields = dict(
        kind=ImageKind.explainer,
        claim_id="c17",
        path="outputs/images/sess1/c17.png",
        alt="A bar chart of reaction times.",
        generated=True,
        prompt="a friendly illustration of reaction time data",
        model="image-gen-role",
        source_hash="deadbeef",
    )
    fields.update(overrides)
    return ImageAsset(**fields)


def test_manifest_round_trips_fully_populated_asset():
    asset = _full_asset()
    assets.write_manifest("sess1", [asset])

    result = assets.read_manifest("sess1")

    assert result == [asset]
    assert isinstance(result[0].kind, ImageKind)  # not a bare string
    assert result[0].kind is ImageKind.explainer
    assert result[0].generated is True


def test_manifest_round_trips_multiple_assets_in_order():
    cover = ImageAsset(kind=ImageKind.cover, path="outputs/images/sess1/cover.png")
    explainer = _full_asset()
    assets.write_manifest("sess1", [cover, explainer])

    result = assets.read_manifest("sess1")

    assert result == [cover, explainer]


def test_write_manifest_overwrites_previous_contents():
    assets.write_manifest("sess1", [_full_asset()])
    assets.write_manifest("sess1", [])

    assert assets.read_manifest("sess1") == []


def test_read_manifest_missing_file_returns_empty_list():
    assert assets.read_manifest("never_written") == []


def test_read_manifest_corrupt_json_returns_empty_list():
    path = assets.image_dir("sess1") / "images.json"
    path.write_text("{not valid json", encoding="utf-8")

    assert assets.read_manifest("sess1") == []


def test_read_manifest_non_list_json_returns_empty_list():
    path = assets.image_dir("sess1") / "images.json"
    path.write_text(json.dumps({"not": "a list"}), encoding="utf-8")

    assert assets.read_manifest("sess1") == []


def test_read_manifest_schema_mismatched_entry_returns_empty_list():
    path = assets.image_dir("sess1") / "images.json"
    path.write_text(json.dumps([{"kind": "not-a-real-kind"}]), encoding="utf-8")

    assert assets.read_manifest("sess1") == []


@pytest.mark.parametrize("bad", ["../escape", "a/b", "a\\b", ".."])
def test_write_manifest_rejects_unsafe_session_id(bad):
    with pytest.raises(ValueError):
        assets.write_manifest(bad, [])


@pytest.mark.parametrize("bad", ["../escape", "a/b", "a\\b", ".."])
def test_read_manifest_rejects_unsafe_session_id(bad):
    with pytest.raises(ValueError):
        assets.read_manifest(bad)


def test_manifest_path_recorded_repo_relative_posix_round_trips_unchanged():
    """`ImageAsset.path` is just a string field: assets.py must not mangle it,
    so a repo-relative, POSIX-separated path put in comes back identical.
    """
    asset = _full_asset(path="outputs/images/sess1/c17.png")
    assets.write_manifest("sess1", [asset])

    result = assets.read_manifest("sess1")[0]

    assert result.path == "outputs/images/sess1/c17.png"
    assert not result.path.startswith("/")
    assert "\\" not in result.path
