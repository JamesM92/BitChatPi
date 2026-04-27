"""
Local identity: static Curve25519 keypair (for Noise XX) + Ed25519 signing keypair.

peerID = first 8 bytes of SHA-256(ed25519_sign_public_key).
This makes the peer ID stable across restarts even though the Noise keypair
is regenerated on every startup.

Regenerating the Noise keypair each restart causes phones to see a changed
noise_pub for the same peer_id and automatically re-initiate the Noise XX
handshake, recovering from Pi restarts without any manual phone reset.

Persisted to ~/.config/bitchatd/identity.json (signing keypair only).
"""
from __future__ import annotations
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding, PublicFormat, PrivateFormat, NoEncryption,
)
from dissononce.dh.x25519.x25519 import X25519DH
from dissononce.dh.x25519.keypair import KeyPair as X25519KeyPair

_DH = X25519DH()

_DEFAULT_PATH = Path.home() / ".config" / "bitchatd" / "identity.json"


@dataclass
class Identity:
    noise_keypair: X25519KeyPair   # Curve25519 static key for Noise XX — fresh each run
    sign_private: Ed25519PrivateKey
    sign_public: bytes             # 32-byte Ed25519 public key (raw)
    peer_id: bytes                 # 8-byte peer identifier — derived from sign_public


def _peer_id_from_sign_pub(sign_pub: bytes) -> bytes:
    return hashlib.sha256(sign_pub).digest()[:8]


def generate() -> Identity:
    noise_kp = _DH.generate_keypair()
    sign_key = Ed25519PrivateKey.generate()
    sign_pub = sign_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    peer_id = _peer_id_from_sign_pub(sign_pub)
    return Identity(
        noise_keypair=noise_kp,
        sign_private=sign_key,
        sign_public=sign_pub,
        peer_id=peer_id,
    )


def load_or_create(path: Path = _DEFAULT_PATH) -> Identity:
    path = Path(path)
    if path.exists():
        try:
            return _load(path)
        except Exception:
            pass
    identity = generate()
    _save(identity, path)
    return identity


def _save(identity: Identity, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    priv_bytes = identity.sign_private.private_bytes(
        Encoding.Raw, PrivateFormat.Raw, NoEncryption()
    )
    data = {
        "sign_private": priv_bytes.hex(),
        "sign_public":  identity.sign_public.hex(),
        "peer_id":      identity.peer_id.hex(),
    }
    path.write_text(json.dumps(data, indent=2))


def _load(path: Path) -> Identity:
    data = json.loads(path.read_text())

    sign_priv_bytes = bytes.fromhex(data["sign_private"])
    sign_key = Ed25519PrivateKey.from_private_bytes(sign_priv_bytes)
    sign_pub = sign_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    # Noise keypair is always fresh — never persisted
    noise_kp = _DH.generate_keypair()

    # peer_id is stable: SHA-256(sign_public)[:8]
    peer_id = _peer_id_from_sign_pub(sign_pub)

    return Identity(
        noise_keypair=noise_kp,
        sign_private=sign_key,
        sign_public=sign_pub,
        peer_id=peer_id,
    )
