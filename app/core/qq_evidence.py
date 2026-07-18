from __future__ import annotations

import hashlib
import struct
import time
import urllib.parse
import urllib.request
from typing import TypedDict

from app.config import settings
from app.core.provider_policy import is_qq_email
from app.core.smtp_limiter import SMTPDeliveryLimiter


# These are the provider's 40px default avatars observed from known default
# profiles. Unknown 40px images remain inconclusive instead of being treated
# as account evidence.
DEFAULT_AVATAR_HASHES = frozenset(
    {
        "f4da77884bee3c5c",
        "d3b86c828178ce7a",
    }
)


class QQAvatarEvidence(TypedDict):
    source: str
    width: int
    height: int
    fingerprint: str


def _image_dimensions(data: bytes) -> tuple[int, int] | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return struct.unpack(">II", data[16:24])

    if data.startswith(b"\xff\xd8"):
        position = 2
        while position + 9 < len(data):
            if data[position] != 0xFF:
                position += 1
                continue
            marker = data[position + 1]
            position += 2
            while marker == 0xFF and position < len(data):
                marker = data[position]
                position += 1
            if marker in {0xD8, 0xD9}:
                continue
            if position + 2 > len(data):
                return None
            segment_length = struct.unpack(">H", data[position:position + 2])[0]
            if segment_length < 2 or position + segment_length > len(data):
                return None
            if marker in {
                0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
            } and segment_length >= 7:
                height, width = struct.unpack(">HH", data[position + 3:position + 7])
                return width, height
            position += segment_length
    return None


def qq_avatar_evidence(email: str) -> QQAvatarEvidence | None:
    """Return only a conservative positive QQ account signal.

    The public avatar response is never used to reject an address. It is only
    useful when it is clearly a non-default, full-size profile image.
    """
    local_part = email.rsplit("@", 1)[0] if "@" in email else ""
    if not is_qq_email(email) or not local_part.isdecimal():
        return None

    limiter = SMTPDeliveryLimiter()
    with limiter.permit(
        "q1.qlogo.cn",
        capacity=1,
        wait_seconds=settings.qq_avatar_wait_seconds,
    ) as acquired:
        if not acquired:
            return None
        time.sleep(settings.qq_avatar_min_interval_seconds)
        query = urllib.parse.urlencode({"b": "qq", "nk": local_part, "s": "640"})
        request = urllib.request.Request(
            f"https://q1.qlogo.cn/g?{query}",
            headers={"User-Agent": "Verigo QQ evidence/1.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=settings.qq_avatar_timeout_seconds) as response:
                image = response.read(2 * 1024 * 1024)
        except Exception:
            return None

    fingerprint = hashlib.sha256(image).hexdigest()[:16]
    dimensions = _image_dimensions(image)
    if not dimensions or fingerprint in DEFAULT_AVATAR_HASHES:
        return None
    width, height = dimensions
    if width <= 40 or height <= 40:
        return None
    return {
        "source": "qq_avatar",
        "width": width,
        "height": height,
        "fingerprint": fingerprint,
    }
