"""Visuals orchestrator — composes the image feature's modules into one call.

`illustrate` is the single entry point that turns a finished run's
`AgentOutput` into that run's image assets: the model-generated cover
(`api.imagegen` + `api.imageprompt`) and, in `ImageMode.all`, one
deterministically rendered explainer card per selected claim
(`api.claimcard`). It holds no layout, prompt or provider logic of its own —
it only composes #23-#29's modules and decides storage (`api.assets`) and
selection (`api.explainer`).

Composition order per asset:
    cover:      api.imageprompt.build_cover_prompt -> api.imagegen.generate_image
                -> api.assets.image_path (write bytes)
    explainer:  api.explainer.select_claim_ids -> api.claimcard.render_claim_card
                -> api.assets.image_path (write bytes)
    storage:    api.assets.read_manifest (once, at the start, for skip-checks)
                api.assets.write_manifest (once, at the end, with exactly the
                images this call returns)

ACYCLIC BY CONSTRUCTION — this module imports NOTHING from `api.jobs`, ever,
not even inside a function. #31 makes `api/jobs.py` import THIS module (to
call `illustrate` from `jobs.start`'s closure); an import the other way would
make `jobs -> visuals -> jobs`, a circular import at module load time. That is
why `build_cover_prompt`'s `card`/`language`/`liveliness` arrive as plain
PARAMETERS here rather than being resolved from `session_id` via
`api.jobs.read_card`/`read_request` — turning a bare `session_id` into those
values is real work and it belongs to #31, in `api/jobs.py`, which already
owns that machinery. `out.style_profile` is passed straight through instead,
since it needs no lookup at all.

Never raises. A caller gets a finished `AgentOutput` back for any input,
including one with no drafts, no ledger, or a `failed` status — see
`illustrate`'s docstring. Failures are isolated PER ASSET, not per stage: one
claim's card refusing to render, or the cover backend refusing the request,
must not lose any other asset. Each distinct failure is a `Notice` with
`code=NoticeCode.image_error`; there is only the one code, so the MESSAGE is
what tells a font refusal apart from a provider refusal, a config error, a
format error, or a layout refusal (see `_generate_cover`/`_generate_card`).

Never mutates the `AgentOutput` it is given: `illustrate` always returns a
new object built with `model_copy`, and every field it changes (`notices`,
`images`) is a freshly built list — the input's own `notices`/`images` lists
are read, never appended to in place.
"""

from __future__ import annotations

import hashlib
import logging

from api.assets import image_path, read_manifest, write_manifest
from api.claimcard import CardLayoutRefusedError, FontRefusedError, render_claim_card
from api.config_loader import resolve_setting
from api.explainer import select_claim_ids
from api.imagegen import (
    ImageGenConfigError,
    ImageGenError,
    ImageGenFormatError,
    ImageGenProviderError,
    ImageGenRefusedError,
    generate_image,
)
from api.imageprompt import build_cover_prompt
from api.schema import (
    AgentOutput,
    Claim,
    ImageAsset,
    ImageKind,
    ImageMode,
    Language,
    Notice,
    NoticeCode,
)

_log = logging.getLogger(__name__)

# Same env-indirection every other setting in this codebase uses
# (api.config_loader.resolve_setting). Read here, not re-derived, per the
# issue's constraint: "Reads api.config_loader.resolve_setting for
# images.cap and images.model."
_CAP_SETTING_PATH = ("images", "cap")
_CAP_ENV_VAR = "IMAGE_CAP"
_DEFAULT_CAP = "3"

_MODEL_SETTING_PATH = ("images", "model")
_MODEL_ENV_VAR = "IMAGE_MODEL"


def _resolve_cap() -> int:
    """How many explainer cards a run may render, parsed defensively.

    A garbage `images.cap` (unset, non-numeric, negative) must never raise —
    it falls back to `_DEFAULT_CAP`, mirroring `api.pipeline._draft_workers`'s
    same defend-against-malformed-config shape. Negative/zero resolves to 0
    (api.explainer.select_claim_ids already clamps this too; clamped again
    here so a caller reading `_resolve_cap()` never sees a negative number).
    """
    raw = resolve_setting(_CAP_SETTING_PATH, _CAP_ENV_VAR, _DEFAULT_CAP)
    try:
        return max(int(raw), 0)
    except (TypeError, ValueError):
        return int(_DEFAULT_CAP)


def _resolve_image_model_name() -> str:
    """The configured image model name, for `ImageAsset.model` bookkeeping.

    Best-effort only: `api.imagegen.generate_image` does not hand its caller
    back the model name it used, so this re-reads the same setting it reads
    from, to record on the asset. An unresolved setting yields `""` rather
    than raising — `api.imagegen._resolve_model` is what actually enforces
    that a model is configured before a real call is made.
    """
    return resolve_setting(_MODEL_SETTING_PATH, _MODEL_ENV_VAR, default="")


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _card_source_hash(claim: Claim) -> str:
    """Hash covering exactly `claim.claim`/`claim.qualifier`/`claim.id`.

    A unit-separator join, not string concatenation, so two claims whose
    fields differ only in where a boundary falls (e.g. `claim="ab", id="c"`
    vs. `claim="a", id="bc"`) never collide onto the same hash.
    """
    return _hash("\x1f".join([claim.claim, claim.qualifier, claim.id]))


def _reuse(
    existing_by_key: dict[tuple[ImageKind, str], ImageAsset],
    key: tuple[ImageKind, str],
    source_hash: str,
    force: bool,
) -> ImageAsset | None:
    """The manifest's existing asset for `key`, if `force` is False and its
    recorded `source_hash` matches — the idempotence check. `None` otherwise,
    meaning the caller must (re)generate."""
    if force:
        return None
    existing = existing_by_key.get(key)
    if existing is None or not existing.source_hash:
        return None
    if existing.source_hash != source_hash:
        return None
    return existing


def _cover_alt(card: dict) -> str:
    title = str(card.get("title") or "").strip()
    return f"Cover illustration for: {title}" if title else "Cover illustration"


def _generate_cover(
    *,
    out: AgentOutput,
    session_id: str,
    card: dict,
    language: Language,
    liveliness: int,
    force: bool,
    existing_by_key: dict[tuple[ImageKind, str], ImageAsset],
    notices: list[Notice],
    images: list[ImageAsset],
) -> None:
    """Produce the run's cover asset, or append one distinguishable Notice.

    Isolated from every other asset: any failure here returns without
    raising and without adding to `images` — it never touches `notices`
    for any OTHER asset, and never stops explainer cards from being tried.
    """
    try:
        prompt = build_cover_prompt(card, language, liveliness, out.style_profile)
    except Exception as err:  # build_cover_prompt is documented pure/non-raising,
        # but a caller-supplied `card`/`style` shaped unexpectedly must still
        # not sink the whole run.
        notices.append(
            Notice(
                code=NoticeCode.image_error,
                message=f"cover image skipped — could not build the prompt: {err}",
            )
        )
        return

    source_hash = _hash(prompt)
    reused = _reuse(existing_by_key, (ImageKind.cover, ""), source_hash, force)
    if reused is not None:
        images.append(reused)
        return

    try:
        image_bytes = generate_image(prompt)
    except ImageGenConfigError as err:
        notices.append(
            Notice(
                code=NoticeCode.image_error,
                message=f"cover image skipped — image backend not configured: {err}",
            )
        )
        return
    except ImageGenRefusedError as err:
        notices.append(
            Notice(
                code=NoticeCode.image_error,
                message=f"cover image skipped — provider refused the request: {err}",
            )
        )
        return
    except ImageGenProviderError as err:
        notices.append(
            Notice(
                code=NoticeCode.image_error,
                message=f"cover image skipped — provider call failed: {err}",
            )
        )
        return
    except ImageGenFormatError as err:
        notices.append(
            Notice(
                code=NoticeCode.image_error,
                message=f"cover image skipped — provider returned invalid image data: {err}",
            )
        )
        return
    except ImageGenError as err:  # any other/future ImageGenError subclass
        notices.append(
            Notice(
                code=NoticeCode.image_error,
                message=f"cover image skipped — image generation failed: {err}",
            )
        )
        return
    except Exception as err:  # truly unexpected
        notices.append(
            Notice(
                code=NoticeCode.image_error,
                message=f"cover image skipped — unexpected error: {err}",
            )
        )
        return

    try:
        path = image_path(session_id, ImageKind.cover)
        path.write_bytes(image_bytes)
    except Exception as err:
        notices.append(
            Notice(
                code=NoticeCode.image_error,
                message=f"cover image skipped — could not save the file: {err}",
            )
        )
        return

    images.append(
        ImageAsset(
            kind=ImageKind.cover,
            claim_id="",
            path=str(path),
            alt=_cover_alt(card),
            generated=True,
            prompt=prompt,
            model=_resolve_image_model_name(),
            source_hash=source_hash,
        )
    )


def _generate_card(
    *,
    claim: Claim,
    session_id: str,
    force: bool,
    existing_by_key: dict[tuple[ImageKind, str], ImageAsset],
    notices: list[Notice],
    images: list[ImageAsset],
) -> None:
    """Produce one explainer card asset, or append one distinguishable Notice.

    Isolated from every other asset, including the cover and every other
    card: a font refusal or layout refusal on THIS claim never stops another
    claim's card, and never touches any other asset's Notice.
    """
    source_hash = _card_source_hash(claim)
    reused = _reuse(existing_by_key, (ImageKind.explainer, claim.id), source_hash, force)
    if reused is not None:
        images.append(reused)
        return

    try:
        image_bytes = render_claim_card(claim)
    except FontRefusedError as err:
        notices.append(
            Notice(
                code=NoticeCode.image_error,
                message=f"explainer card for {claim.id} skipped — font refused: {err}",
            )
        )
        return
    except CardLayoutRefusedError as err:
        notices.append(
            Notice(
                code=NoticeCode.image_error,
                message=f"explainer card for {claim.id} skipped — layout refused: {err}",
            )
        )
        return
    except Exception as err:  # truly unexpected
        notices.append(
            Notice(
                code=NoticeCode.image_error,
                message=f"explainer card for {claim.id} skipped — unexpected error: {err}",
            )
        )
        return

    try:
        path = image_path(session_id, ImageKind.explainer, claim.id)
        path.write_bytes(image_bytes)
    except Exception as err:
        notices.append(
            Notice(
                code=NoticeCode.image_error,
                message=f"explainer card for {claim.id} skipped — could not save the file: {err}",
            )
        )
        return

    images.append(
        ImageAsset(
            kind=ImageKind.explainer,
            claim_id=claim.id,
            path=str(path),
            alt=claim.claim,
            generated=False,
            prompt="",
            model="",
            source_hash=source_hash,
        )
    )


def _illustrate(
    out: AgentOutput,
    session_id: str,
    mode: ImageMode,
    card: dict,
    language: Language,
    liveliness: int,
    force: bool,
) -> AgentOutput:
    """The real work, wrapped by `illustrate`'s last-resort safety net."""
    notices = list(out.notices)

    if mode is ImageMode.off:
        # Zero backend calls, no manifest read or write — nothing to skip-check
        # and nothing to record. `notices`/`images` are untouched, so the
        # unchanged `out.images` reference is kept rather than rebuilt.
        return out.model_copy(update={"notices": notices})

    try:
        existing = read_manifest(session_id)
    except Exception as err:  # unsafe session_id, unreadable mirror, ...
        _log.debug("session %s: could not read image manifest (%s)", session_id, err)
        existing = []
    existing_by_key: dict[tuple[ImageKind, str], ImageAsset] = {
        (asset.kind, asset.claim_id): asset for asset in existing
    }

    images: list[ImageAsset] = []

    _generate_cover(
        out=out,
        session_id=session_id,
        card=card,
        language=language,
        liveliness=liveliness,
        force=force,
        existing_by_key=existing_by_key,
        notices=notices,
        images=images,
    )

    if mode is ImageMode.all:
        try:
            claim_ids = select_claim_ids(out, _resolve_cap())
        except Exception as err:  # select_claim_ids is documented pure, but a
            # malformed AgentOutput must still not sink the run.
            notices.append(
                Notice(
                    code=NoticeCode.image_error,
                    message=f"explainer card selection skipped — unexpected error: {err}",
                )
            )
            claim_ids = []

        ledger_by_id = {claim.id: claim for claim in out.claim_ledger}
        for claim_id in claim_ids:
            claim = ledger_by_id.get(claim_id)
            if claim is None:  # not expected (select_claim_ids already filters
                continue        # to ledger ids) — skip rather than crash.
            _generate_card(
                claim=claim,
                session_id=session_id,
                force=force,
                existing_by_key=existing_by_key,
                notices=notices,
                images=images,
            )

    try:
        write_manifest(session_id, images)
    except Exception as err:
        notices.append(
            Notice(
                code=NoticeCode.image_error,
                message=f"image manifest write failed — {err}",
            )
        )

    return out.model_copy(update={"notices": notices, "images": images})


def illustrate(
    out: AgentOutput,
    session_id: str,
    mode: ImageMode,
    card: dict,
    language: Language,
    liveliness: int,
    force: bool = False,
) -> AgentOutput:
    """Produce every image asset for one finished run, in one call.

    Composes, in order per asset: `api.claimcard.render_claim_card` (cards),
    `api.imageprompt.build_cover_prompt` + `api.imagegen.generate_image`
    (cover), `api.assets.image_path`/`write_manifest`/`read_manifest`
    (storage), and `api.explainer.select_claim_ids` (which cards, in
    `ImageMode.all`). Holds no layout, prompt or provider logic of its own.

    Args:
        out: a finished run's result. An output with no drafts, no ledger, or
            `status=failed` is handled the same as any other — never raises,
            and simply produces whatever assets its inputs support (a cover
            only needs `card`/`language`/`liveliness`/`out.style_profile`; an
            empty `claim_ledger` yields no explainer cards).
        session_id: whose `outputs/images/<session_id>/` directory and
            manifest this call reads and writes (`api.assets`).
        mode: `off` makes zero backend calls and writes nothing (not even an
            empty manifest). `cover` produces the cover only. `all` also
            renders one explainer card per id `api.explainer.select_claim_ids`
            selects, capped by `resolve_setting(("images","cap"), "IMAGE_CAP",
            "3")`.
        card: a plain dict shaped like `api.extract.CARD_FIELDS`, passed
            straight to `build_cover_prompt`. A sparse or empty `{}` card is
            normal (a thinner cover, no Notice) — see `api.imageprompt`.
        language: the audience language, passed straight to
            `build_cover_prompt`. Pass `AgentInput`'s own default
            (`Language.zh`) when there is no request sidecar to read.
        liveliness: the 1-5 mood dial, passed straight to
            `build_cover_prompt`. Pass `AgentInput`'s own default (`3`) when
            there is no request sidecar to read.
        force: when True, regenerates every asset this call would otherwise
            skip via the `source_hash` idempotence check (see below).

    Returns:
        A NEW `AgentOutput` (never the same object as `out`, never a mutation
        of it) with `images` set to exactly the assets this call produced —
        freshly generated ones, plus any reused-by-hash ones — and `notices`
        set to `out.notices` PLUS one `Notice(code=NoticeCode.image_error, ...)`
        per asset that failed, its message distinguishing a font refusal from
        a layout refusal, a provider refusal, a provider-config error, or a
        provider format error. `platform_outputs` and every other field carry
        over unchanged. Idempotent: calling this again with the same
        `session_id`/`card`/`language`/`liveliness`/`out` and `force=False`
        reuses every asset whose recorded `source_hash` (the cover prompt
        string; for a card, `claim.claim`/`claim.qualifier`/`claim.id`) still
        matches, making no backend call and writing no new file for it;
        `force=True` regenerates everything regardless.

    Never raises, for any input — an internal failure that is not one of the
    per-asset cases above is caught and reported as one generic
    `image_error` Notice instead.
    """
    try:
        return _illustrate(out, session_id, mode, card, language, liveliness, force)
    except Exception as err:  # last-resort net: illustrate must NEVER raise
        _log.exception(
            "session %s: illustrate crashed unexpectedly; returning input unchanged", session_id
        )
        notices = list(out.notices)
        notices.append(
            Notice(
                code=NoticeCode.image_error,
                message=f"image generation skipped — unexpected error: {err}",
            )
        )
        return out.model_copy(update={"notices": notices})


__all__ = ["illustrate"]
