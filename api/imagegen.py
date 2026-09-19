"""Image backend adapter — turns a prompt into PNG bytes through one call.

One narrow interface (`generate_image`) so a second backend (#40) can be
added as a new function later without any caller of this one changing. The
only implementation today calls Google's Imagen surface through
`google-genai`'s `Client.models.generate_images` — the Imagen-specific call,
distinct from a chat-style `generate_content` call.

Two facts about that SDK surface make this module more than a thin wrapper:

1. A provider safety/content refusal comes back as a NORMAL RESPONSE FIELD
   (`GeneratedImage.rai_filtered_reason`), not a raised exception. A caller
   that only wraps the SDK call in `try/except` would treat a refused image
   as success and hand back nothing useful (or crash later on `image_bytes`
   being `None`). This module inspects the response and raises
   `ImageRefusedError` when that field is set.
2. `image.mime_type` is the provider's OWN claim about what it sent back;
   `api/assets.py` (#24) writes these bytes straight to a `<claim_id>.png` /
   `cover.png` path, so this module verifies the PNG signature on the actual
   bytes rather than trusting that claim.

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
from google.genai.types import GenerateImagesConfig, HttpOptions

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
        ImageGenRefusedError: the provider filtered the request
            (`rai_filtered_reason` was set on the response) rather than
            returning image bytes. `.reason` carries the provider's text,
            when given.
        ImageGenProviderError: the SDK call itself failed (network error,
            API error) or the response did not contain a generated image.
        ImageGenFormatError: the provider returned bytes that are not a PNG,
            checked by signature rather than the declared mime_type.

    One attempt only — this function does not retry. Whether a caller
    retries is #30's decision, not this module's.
    """
    model = _resolve_model()
    api_key = _resolve_api_key()

    try:
        client = Client(api_key=api_key)
        response = client.models.generate_images(
            model=model,
            prompt=prompt,
            config=GenerateImagesConfig(
                number_of_images=1,
                output_mime_type="image/png",
                http_options=HttpOptions(timeout=int(_TIMEOUT_S * 1000)),
            ),
        )
    except ImageGenError:
        raise
    except Exception as err:  # network error, API error, SDK-internal failure
        raise ImageGenProviderError(_scrub(str(err))) from err

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
