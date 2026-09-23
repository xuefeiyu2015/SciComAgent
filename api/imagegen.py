"""Image backend adapter — turns a prompt into PNG bytes through one call.

One narrow interface (`generate_image`) so a second backend (#40) can be
added as a new function later without any caller of this one changing. The
implementation today talks to Google through `google-genai`, which exposes
TWO different image surfaces, and this module speaks both:

- `Client.models.generate_images` — the Imagen `:predict` surface.
- `Client.models.generate_content` — the chat surface, which the
  `gemini-*-image` models use to return an image as an inline data part.

WHICH SURFACE, AND WHY THAT WAY (#62). The surface is inferred from the
model id, with no second config key and no probe call: a bare model id that
starts with `imagen` (`imagen-4.0-generate-001`, `models/imagen-...`) is an
Imagen model and is sent to `generate_images`; anything else
(`gemini-2.5-flash-image`, `gemini-3-pro-image`) is sent to
`generate_content`. The id is already in hand before any request, so this
costs no round trip, cannot fail on its own, and keeps the one-attempt
contract below. The alternatives were rejected deliberately: asking the API
which actions a model supports adds a second network call and a second
failure mode to every cover; calling one surface and falling back to the
other on a 404 makes two billable calls on the common path and turns a
provider outage into a confusing double error. Google's naming is the thing
that actually distinguishes the two families, so this reads it directly.
A standard Gemini API key serves NO `predict` model at all (#62), which is
why the `generate_content` branch is the one that works there — but an
Imagen-capable key keeps its own branch, unchanged.

Three facts about those SDK surfaces make this module more than a thin
wrapper:

1. A provider safety/content refusal comes back as a NORMAL RESPONSE FIELD,
   not a raised exception — `GeneratedImage.rai_filtered_reason` on the
   Imagen surface; `prompt_feedback.block_reason` or a refusing
   `candidate.finish_reason` (`IMAGE_SAFETY`, `PROHIBITED_CONTENT`,
   `NO_IMAGE`, ...) on the chat surface. A caller that only wraps the SDK
   call in `try/except` would treat a refused image as success and hand
   back nothing useful. This module inspects the response and raises
   `ImageGenRefusedError`, which stays distinguishable from the
   `ImageGenProviderError` a network failure raises.
2. On the chat surface a refusal is also expressed in WORDS: the model
   answers with a text part and no image and still finishes with `STOP`.
   Since this module only ever asks for a picture, that is a refusal too,
   and the model's own sentence becomes the `.reason`.
3. The declared mime type is the provider's OWN claim about what it sent
   back; `api/assets.py` (#24) writes these bytes straight to a
   `<claim_id>.png` / `cover.png` path, so this module verifies the PNG
   signature on the actual bytes rather than trusting that claim. (It also
   cannot ask the Gemini Developer API for PNG: `ImageConfig.output_mime_type`
   is rejected there as Agent-Platform-only, so the check is the only
   guarantee.)

Model name and any tuning are read from config via
`api.config_loader.resolve_setting` — never hardcoded (CLAUDE.md). The key is
read from `GOOGLE_API_KEY` and never returned, appears in no exception
message, log line or return value (matching `api/providers.py`'s `_scrub`
precedent; reimplemented here rather than importing that private helper).

Deciding WHEN to call this, and whether to retry a failure, is #30's job, not
this module's: `generate_image` makes exactly one attempt and raises.
"""

from __future__ import annotations

import os

from api.config_loader import resolve_setting

# google-genai is already a dependency (pulled in by langchain-google-genai).
from google.genai import Client
from google.genai.types import GenerateContentConfig, GenerateImagesConfig, HttpOptions

_MODEL_SETTING_PATH = ("images", "model")
_MODEL_ENV_VAR = "IMAGE_MODEL"
_API_KEY_ENV_VAR = "GOOGLE_API_KEY"

# PNG requires a real generation call — a listing call, not a generation
# call, is what api/providers.py's 5s _TIMEOUT bounds. Image generation
# routinely takes tens of seconds; this is sized for that, not copied from
# there.
_TIMEOUT_S = 60.0

# Below this length a value cannot be a real credential, and scrubbing it
# would corrupt ordinary words in an error message instead of protecting
# anything. Mirrors api/providers.py's _MIN_SECRET_LEN.
_MIN_SECRET_LEN = 8

# The first 8 bytes of every PNG file, always. Checking these (not the
# provider's declared mime_type) is what #28 requires: api/assets.py writes
# these bytes straight to a `<claim_id>.png` / `cover.png` path.
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# A bare model id starting with this is an Imagen model and is served by the
# `:predict` surface (`generate_images`); everything else goes to the chat
# surface (`generate_content`). See the module docstring for why the id, and
# not a config key or a probe call, decides this (#62).
_IMAGEN_MODEL_PREFIX = "imagen"

# Chat-surface finish reasons that mean "the provider declined", as opposed to
# "the call failed". Compared by NAME so a plain string works as well as the
# SDK enum. Anything else with no image (MAX_TOKENS, OTHER, ...) stays an
# ImageGenProviderError: a refusal has to stay distinguishable from a failure.
_REFUSAL_FINISH_REASONS = frozenset(
    {
        "SAFETY",
        "IMAGE_SAFETY",
        "PROHIBITED_CONTENT",
        "IMAGE_PROHIBITED_CONTENT",
        "RECITATION",
        "IMAGE_RECITATION",
        "BLOCKLIST",
        "SPII",
        "NO_IMAGE",
        "LANGUAGE",
    }
)


class ImageGenError(Exception):
    """Base for every failure this adapter raises. Catch this for "any of them"."""


class ImageGenConfigError(ImageGenError):
    """Setup is wrong: no model configured, or no API key — never sent to the provider."""


class ImageGenRefusedError(ImageGenError):
    """The provider declined to draw this (safety/content filtering).

    Raised when `GeneratedImage.rai_filtered_reason` is set — a normal SDK
    response field, not an exception the SDK raises on its own. `.reason`
    carries the provider's own explanation, when it gave one.
    """

    def __init__(self, reason: str = "") -> None:
        self.reason = reason
        message = f"image generation refused by the provider: {reason}" if reason else (
            "image generation refused by the provider"
        )
        super().__init__(message)


class ImageGenProviderError(ImageGenError):
    """The call itself failed: network error, API error, bad response shape."""


class ImageGenFormatError(ImageGenError):
    """The provider returned bytes that are not a PNG (checked by signature)."""


def _scrub(text: str) -> str:
    """Remove the API key from `text` before it can be raised, logged or returned.

    Same intent as `api/providers.py`'s private `_scrub` / `_MIN_SECRET_LEN`;
    reimplemented here rather than importing that module's private helper
    across module boundaries.
    """
    value = os.environ.get(_API_KEY_ENV_VAR, "")
    if value and len(value) >= _MIN_SECRET_LEN:
        text = text.replace(value, "***")
    return text


def _resolve_model() -> str:
    model = resolve_setting(_MODEL_SETTING_PATH, _MODEL_ENV_VAR, default="")
    if not model:
        raise ImageGenConfigError(
            "no image model configured: set images.model in config/config.yaml "
            f"or the {_MODEL_ENV_VAR} environment variable"
        )
    return model


def _resolve_api_key() -> str:
    key = os.environ.get(_API_KEY_ENV_VAR, "")
    if not key:
        raise ImageGenConfigError(
            f"no Google API key configured: set the {_API_KEY_ENV_VAR} environment variable"
        )
    return key


def _uses_imagen_surface(model: str) -> bool:
    """True when `model` is an Imagen model and must use the `:predict` surface.

    Reads the bare id, so `imagen-4.0-generate-001` and
    `models/imagen-4.0-generate-001` decide the same way, and a
    `gemini-*-image` model does not accidentally match on the word "image".
    """
    bare = model.strip().lower().rsplit("/", 1)[-1]
    return bare.startswith(_IMAGEN_MODEL_PREFIX)


def _enum_name(value: object) -> str:
    """The NAME of an SDK enum member, or the plain string it already is."""
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name.upper()
    return str(value or "").rsplit(".", 1)[-1].upper()


def _generate_via_imagen(client, model: str, prompt: str) -> bytes:
    """The Imagen `:predict` surface — `generated_images[].image.image_bytes`."""
    response = client.models.generate_images(
        model=model,
        prompt=prompt,
        config=GenerateImagesConfig(
            number_of_images=1,
            output_mime_type="image/png",
            http_options=HttpOptions(timeout=int(_TIMEOUT_S * 1000)),
        ),
    )

    generated = list(getattr(response, "generated_images", None) or [])
    if not generated:
        raise ImageGenProviderError("provider returned no generated images")

    first = generated[0]

    refusal = getattr(first, "rai_filtered_reason", None)
    if refusal:
        raise ImageGenRefusedError(_scrub(str(refusal)))

    image = getattr(first, "image", None)
    image_bytes = getattr(image, "image_bytes", None) if image is not None else None
    if not image_bytes:
        raise ImageGenProviderError("provider response carried no image bytes")

    return image_bytes


def _generate_via_generate_content(client, model: str, prompt: str) -> bytes:
    """The chat surface — the image arrives as a `Part.inline_data` blob.

    Nothing but the timeout is configured: `response_modalities` and
    `ImageConfig` are not portable across the `gemini-*-image` family on a
    Developer API key (`output_mime_type` is rejected outright there), and
    every one of those models returns its picture by default. The PNG check
    in `generate_image` is what makes that safe.
    """
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=GenerateContentConfig(
            http_options=HttpOptions(timeout=int(_TIMEOUT_S * 1000)),
        ),
    )

    # A blocked PROMPT never reaches a candidate: it comes back here.
    feedback = getattr(response, "prompt_feedback", None)
    block_reason = getattr(feedback, "block_reason", None) if feedback is not None else None
    if block_reason:
        detail = getattr(feedback, "block_reason_message", None) or _enum_name(block_reason)
        raise ImageGenRefusedError(_scrub(str(detail)))

    candidates = list(getattr(response, "candidates", None) or [])
    if not candidates:
        raise ImageGenProviderError("provider returned no candidates")

    candidate = candidates[0]
    content = getattr(candidate, "content", None)
    parts = list(getattr(content, "parts", None) or []) if content is not None else []

    texts: list[str] = []
    for part in parts:
        inline = getattr(part, "inline_data", None)
        data = getattr(inline, "data", None) if inline is not None else None
        if data:
            return data
        text = getattr(part, "text", None)
        if text:
            texts.append(str(text))

    # No picture. Either the provider said why in a finish reason, or it
    # answered in words — both are the provider declining, not a failed call.
    finish_reason = _enum_name(getattr(candidate, "finish_reason", None))
    if finish_reason in _REFUSAL_FINISH_REASONS:
        detail = getattr(candidate, "finish_message", None) or " ".join(texts) or finish_reason
        raise ImageGenRefusedError(_scrub(str(detail)))
    if texts:
        raise ImageGenRefusedError(_scrub(" ".join(texts).strip()))

    raise ImageGenProviderError("provider response carried no image bytes")


def generate_image(prompt: str) -> bytes:
    """Turn `prompt` into PNG image bytes through the configured backend.

    Args:
        prompt: the text prompt to generate an image from.

    Returns:
        Raw PNG bytes (verified against the PNG signature).

    Raises:
        ImageGenConfigError: no model configured (`images.model` /
            `IMAGE_MODEL` resolves to `""`), or no `GOOGLE_API_KEY` set.
            Raised before any call reaches the provider.
        ImageGenRefusedError: the provider declined to draw this rather than
            returning image bytes — `rai_filtered_reason` on the Imagen
            surface, a prompt `block_reason`, a refusing `finish_reason` or a
            worded answer with no picture on the chat surface. `.reason`
            carries the provider's text, when it gave one.
        ImageGenProviderError: the SDK call itself failed (network error,
            API error) or the response did not contain a generated image.
        ImageGenFormatError: the provider returned bytes that are not a PNG,
            checked by signature rather than the declared mime_type.

    The surface is chosen from the model id (see the module docstring): an
    `imagen-*` model goes to `generate_images`, anything else to
    `generate_content`.

    One attempt only — this function does not retry. Whether a caller
    retries is #30's decision, not this module's.
    """
    model = _resolve_model()
    api_key = _resolve_api_key()

    try:
        client = Client(api_key=api_key)
        if _uses_imagen_surface(model):
            image_bytes = _generate_via_imagen(client, model, prompt)
        else:
            image_bytes = _generate_via_generate_content(client, model, prompt)
    except ImageGenError:
        raise
    except Exception as err:  # network error, API error, SDK-internal failure
        raise ImageGenProviderError(_scrub(str(err))) from err

    if not image_bytes.startswith(_PNG_SIGNATURE):
        raise ImageGenFormatError(
            "provider returned bytes that are not a PNG (signature mismatch)"
        )

    return image_bytes


__all__ = [
    "ImageGenError",
    "ImageGenConfigError",
    "ImageGenRefusedError",
    "ImageGenProviderError",
    "ImageGenFormatError",
    "generate_image",
]
