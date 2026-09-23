"""Tests for the image types added to api/schema.py (types only, no behaviour).

Pins the settled shape #23 exists to fix: the `images` dial on AgentInput, the
`images` list on AgentOutput, and the ImageAsset/ImageMode/ImageKind types the
other image-feature tasks (#24, #30, #32, #33, #35) are written against.
"""

from __future__ import annotations

from api.schema import (
    AgentInput,
    AgentOutput,
    ImageAsset,
    ImageKind,
    ImageMode,
    NoticeCode,
    REDRAFTABLE_DIALS,
    merge_dials,
)


def test_image_mode_has_exactly_off_cover_all():
    assert {m.value for m in ImageMode} == {"off", "cover", "all"}


def test_image_kind_has_exactly_cover_explainer():
    assert {k.value for k in ImageKind} == {"cover", "explainer"}


def test_image_asset_has_the_required_fields():
    asset = ImageAsset(kind=ImageKind.explainer)

    assert asset.kind == ImageKind.explainer
    assert asset.claim_id == ""
    assert asset.path == ""
    assert asset.alt == ""
    assert asset.generated is False
    assert asset.prompt == ""
    assert asset.model == ""
    assert asset.source_hash == ""


def test_image_asset_generated_field_describes_true_and_false():
    description = ImageAsset.model_fields["generated"].description

    assert "model-generated" in description
    assert "deterministically rendered" in description
    assert "Claim" in description


def test_every_new_image_field_carries_a_description():
    for name in ("kind", "claim_id", "path", "alt", "generated", "prompt", "model", "source_hash"):
        assert ImageAsset.model_fields[name].description, f"{name} has no description"


def test_notice_code_has_image_error():
    assert NoticeCode.image_error == "image_error"


def test_agent_input_images_dial_defaults_to_off_and_sits_beside_background():
    field_names = list(AgentInput.model_fields.keys())

    assert AgentInput.model_fields["images"].default == ImageMode.off
    assert field_names.index("images") == field_names.index("background") + 1


def test_agent_output_images_defaults_to_empty_list():
    assert AgentOutput().images == []


def test_agent_output_still_constructs_with_no_new_arguments():
    output = AgentOutput()

    assert output.images == []
    assert output.status is not None


def test_agent_input_still_constructs_with_no_new_arguments():
    agent_input = AgentInput(source="https://example.com/paper", source_type="url")

    assert agent_input.images == ImageMode.off


def test_images_is_in_redraftable_dials():
    """#33: without this, `merge_dials` silently drops an `images` change —
    a redraft's `images` argument would be accepted and do nothing."""
    assert "images" in REDRAFTABLE_DIALS


def test_merge_dials_actually_applies_an_images_change():
    before = AgentInput(source="https://example.com/paper", source_type="url")
    assert before.images == ImageMode.off

    after = merge_dials(before, {"images": ImageMode.cover})

    assert after.images == ImageMode.cover
    # everything else carried over unchanged
    assert after.source == before.source
    assert after.language == before.language
