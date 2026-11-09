"""Compatibility fixes for Pycord's experimental DAVE voice receiver.

Pycord's receive implementation is not part of a stable release yet.  The
reviewed commit used by this project has two fail-open cases which are unsafe
for archival recording:

* DAVE exists but is not ready: encrypted Opus is sent to libopus.
* Discord has not populated the SSRC -> user mapping yet: every packet is
  discarded even though the DAVE session already knows the participants.

Keep the workaround small and explicit so it can be deleted when the upstream
receive tracker is resolved and a stable release contains equivalent logic.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("yagapon.pycord_voice_receive")

_INSTALLED = False


def _is_passthrough_transition(error: ValueError) -> bool:
    """Return whether DAVE says this transport-decrypted frame is plaintext."""

    return "UnencryptedWhenPassthroughDisabled" in str(error)


def decrypt_rtp_safely(decryptor: Any, packet: Any) -> bytes:
    """Transport-decrypt and then DAVE-decrypt one RTP payload.

    Invalid or not-yet-decryptable frames are dropped before Opus.  Returning
    encrypted bytes would make libopus produce ``corrupted stream`` and can
    flood the event loop with tracebacks while a recording is active.
    """

    # Import lazily.  Unit tests can exercise this module without importing the
    # optional voice stack during collection.
    import davey

    state = decryptor.client._connection
    dave = getattr(state, "dave_session", None)
    raw_payload = decryptor._decryptor_rtp(packet)

    if dave is None:
        packet.decrypted_data = raw_payload
        return raw_payload

    # A normal voice channel may receive RTP before the MLS/DAVE handshake is
    # ready.  Those bytes are still end-to-end encrypted; never pass them to
    # Opus as the old receiver did.
    if not getattr(dave, "ready", False):
        packet.decrypted_data = b""
        return b""

    user_id = getattr(state, "ssrc_user_map", {}).get(packet.ssrc)
    if user_id is not None:
        try:
            decrypted = dave.decrypt(user_id, davey.MediaType.audio, raw_payload)
        except ValueError as exc:
            # During an epoch transition Discord can briefly send an already
            # plaintext Opus frame while DAVE passthrough has just changed.
            # Only that specific error is safe to pass through.
            decrypted = raw_payload if _is_passthrough_transition(exc) else b""
        packet.decrypted_data = decrypted
        return decrypted

    # member_connect and RTP are delivered on separate paths.  Infer a missing
    # mapping once from the authenticated DAVE participants and cache it.
    for candidate in getattr(dave, "get_user_ids", lambda: ())():
        try:
            candidate_id = int(candidate)
            decrypted = dave.decrypt(
                candidate_id,
                davey.MediaType.audio,
                raw_payload,
            )
        except (TypeError, ValueError):
            continue
        add_ssrc = getattr(decryptor.client, "_add_ssrc", None)
        if callable(add_ssrc):
            add_ssrc(candidate_id, packet.ssrc)
        else:
            state.ssrc_user_map[packet.ssrc] = candidate_id
        packet.decrypted_data = decrypted
        log.info(
            "Recovered missing DAVE SSRC mapping ssrc=%s user=%s",
            packet.ssrc,
            candidate_id,
        )
        return decrypted

    packet.decrypted_data = b""
    return b""


def install_pycord_voice_receive_fix() -> bool:
    """Install the bounded compatibility hook exactly once.

    Returns ``True`` when Pycord's experimental receive class was found.  A
    missing class is fatal to recording startup and is therefore logged loudly.
    """

    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        from discord.voice.receive.reader import PacketDecryptor
    except (ImportError, AttributeError):
        log.exception("Pycord DAVE receive implementation is unavailable")
        return False
    PacketDecryptor.decrypt_rtp = decrypt_rtp_safely
    _INSTALLED = True
    log.info("Installed Pycord DAVE receive compatibility fix")
    return True

