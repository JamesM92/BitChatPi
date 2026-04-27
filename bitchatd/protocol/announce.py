# ─── PROTOCOL CONTRACT ────────────────────────────────────────────────────────
# Derived from: app/src/main/java/com/bitchat/android/model/IdentityAnnouncement.kt
# Last verified against upstream commit: c5a3368 (2026-04-25)
# ──────────────────────────────────────────────────────────────────────────────
"""
ANNOUNCE packet payload: TLV encoding matching Android IdentityAnnouncement.kt.

TLV tags:
  0x01  nickname       UTF-8, max 15 bytes (MAX_NICKNAME_LENGTH)
  0x02  noise_pub      32-byte X25519 static public key (for Noise XX)
  0x03  sign_pub       32-byte Ed25519 public key (for signature verification)

Unknown tags are silently skipped on decode (forward-compatible).
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional

_TAG_NICKNAME  = 0x01
_TAG_NOISE_PUB = 0x02
_TAG_SIGN_PUB  = 0x03


@dataclass
class AnnouncePayload:
    nickname:  str
    noise_pub: bytes   # 32 bytes, X25519
    sign_pub:  bytes   # 32 bytes, Ed25519


def encode_announce(nickname: str, noise_pub: bytes, sign_pub: bytes) -> bytes:
    nick = nickname.encode("utf-8")[:15]
    out = bytearray()
    for tag, val in (
        (_TAG_NICKNAME,  nick),
        (_TAG_NOISE_PUB, noise_pub),
        (_TAG_SIGN_PUB,  sign_pub),
    ):
        out += bytes([tag, len(val)]) + val
    return bytes(out)


def decode_announce(data: bytes) -> Optional[AnnouncePayload]:
    nickname: Optional[str]   = None
    noise_pub: Optional[bytes] = None
    sign_pub: Optional[bytes]  = None

    i = 0
    while i + 2 <= len(data):
        tag = data[i];     i += 1
        ln  = data[i];     i += 1
        if i + ln > len(data):
            return None
        val = data[i:i + ln]; i += ln
        if tag == _TAG_NICKNAME:
            nickname = val.decode("utf-8", errors="replace")
        elif tag == _TAG_NOISE_PUB:
            noise_pub = bytes(val)
        elif tag == _TAG_SIGN_PUB:
            sign_pub = bytes(val)
        # unknown tags: skip (forward-compatible)

    if nickname is None or noise_pub is None or sign_pub is None:
        return None
    return AnnouncePayload(nickname=nickname, noise_pub=noise_pub, sign_pub=sign_pub)
