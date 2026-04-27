"""
Ed25519 packet signing and verification for ANNOUNCE packets.

Android signs the payload bytes with the sender's Ed25519 private key and
appends the 64-byte signature as the last field in the BLE packet.
"""
from __future__ import annotations
from typing import Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from cryptography.exceptions import InvalidSignature


def sign(private_key: Ed25519PrivateKey, data: bytes) -> bytes:
    """Return a 64-byte Ed25519 signature over data."""
    return private_key.sign(data)


def verify(public_key_bytes: bytes, data: bytes, signature: bytes) -> bool:
    """Return True iff signature is a valid Ed25519 signature over data."""
    if len(signature) != 64 or len(public_key_bytes) != 32:
        return False
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        pub = Ed25519PublicKey.from_public_bytes(public_key_bytes)
        pub.verify(signature, data)
        return True
    except (InvalidSignature, Exception):
        return False
