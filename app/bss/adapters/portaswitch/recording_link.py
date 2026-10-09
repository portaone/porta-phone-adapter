"""Public links to a call recording and its transcript (WT-1993).

A CRM stores the link in a ticket field, and anyone who can open the ticket can follow
it with no WebTrit session, long after the agent who made the call has signed out. So
the link itself is the credential: `link_id` carries the PortaBilling keys of the
recording, an optional expiry, and an HMAC over both, and the adapter fetches the files
with its admin token once the HMAC checks out. Nothing is stored, and no link can be
revoked on its own - deleting the recording in PortaBilling, or changing
SECRET_KEY_BASE, which voids every link at once, are the ways to take one back.

The id is kept short on purpose: CRMs such as Freshdesk cap a text field at 255
characters, and the whole URL has to fit. Packed binary rather than JSON, and the MAC
cut to 16 bytes, keep `link_id` under ~90 characters.
"""

import base64
import binascii
import hashlib
import hmac
import struct
import time
from dataclasses import dataclass
from typing import Optional

# version, expires_at (unix seconds, 0 = never), i_xdr; call_recording_id follows as UTF-8
_HEADER = struct.Struct(">BIQ")
_VERSION = 1
_MAC_SIZE = 16
_KEY_CONTEXT = "webtrit recording link"
#: Ten years: the expiry has to fit its 32-bit field.
MAX_TTL = 10 * 365 * 24 * 3600


class RecordingLinkInvalid(Exception):
    """Not a link this adapter issued, or one altered since."""


class RecordingLinkExpired(Exception):
    """A genuine link past its expiry."""


@dataclass(frozen=True)
class RecordingLink:
    i_xdr: int
    # None when the link gives no access to the transcript
    call_recording_id: Optional[str]
    expires_at: int


class RecordingLinkSigner:
    def __init__(self, secret: str, billing: str):
        # Derived rather than used as is, so the same secret keyed for anything else
        # never produces a MAC that is valid here. The billing it serves goes into the
        # key too: an i_xdr names another call on another PortaBilling, so a link
        # replayed against another tenant's adapter sharing the secret must not verify.
        context = f"{_KEY_CONTEXT}|{billing}".encode("utf-8")
        self._key = hmac.new(secret.encode("utf-8"), context, hashlib.sha256).digest()

    def sign(self, i_xdr: int, call_recording_id: Optional[str], ttl: Optional[int] = None) -> str:
        if ttl and not 0 < ttl <= MAX_TTL:
            raise ValueError(f"ttl must be between 1 and {MAX_TTL}")
        expires_at = int(time.time()) + ttl if ttl else 0
        payload = _HEADER.pack(_VERSION, expires_at, i_xdr) + (call_recording_id or "").encode("utf-8")
        return base64.urlsafe_b64encode(payload + self._mac(payload)).rstrip(b"=").decode("ascii")

    def verify(self, link_id: str) -> RecordingLink:
        try:
            raw = base64.urlsafe_b64decode(link_id + "=" * (-len(link_id) % 4))
        except (binascii.Error, ValueError):
            raise RecordingLinkInvalid()

        payload, mac = raw[:-_MAC_SIZE], raw[-_MAC_SIZE:]
        if len(payload) < _HEADER.size or not hmac.compare_digest(mac, self._mac(payload)):
            raise RecordingLinkInvalid()

        version, expires_at, i_xdr = _HEADER.unpack_from(payload)
        if version != _VERSION:
            raise RecordingLinkInvalid()
        if expires_at and expires_at < time.time():
            raise RecordingLinkExpired()

        call_recording_id = payload[_HEADER.size :].decode("utf-8") or None
        return RecordingLink(i_xdr=i_xdr, call_recording_id=call_recording_id, expires_at=expires_at)

    def _mac(self, payload: bytes) -> bytes:
        return hmac.new(self._key, payload, hashlib.sha256).digest()[:_MAC_SIZE]
