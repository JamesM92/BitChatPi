"""
Local identity: static Curve25519 keypair (for Noise XX) + Ed25519 signing keypair.

peerID = first 8 bytes of SHA-256(ed25519_sign_public_key).

Both keypairs are persisted to ~/.config/bitchatd/identity.json so they survive
daemon restarts.  A stable Noise static key is required for Noise XX to work
reliably with iOS/Android: after a restart the remote peer verifies that the
static key received in msg2 matches the noise_pub it last received in an ANNOUNCE.
If the key changes (e.g. because we regenerate each restart), msg2 verification
fails and the phone never sends msg3 — so the handshake loops forever.

Delete identity.json to force generation of a completely new identity (new peer
ID and new Noise keypair).  The daemon will create a fresh file on next start.
"""
from __future__ import annotations
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding, PublicFormat, PrivateFormat, NoEncryption,
)
from dissononce.dh.x25519.x25519 import X25519DH
from dissononce.dh.x25519.keypair import KeyPair as X25519KeyPair
from dissononce.dh.x25519.private import PrivateKey as X25519PrivateKey

_DH = X25519DH()

_DEFAULT_PATH = Path.home() / ".config" / "bitchatd" / "identity.json"


@dataclass
class Identity:
    noise_keypair: X25519KeyPair   # Curve25519 static key for Noise XX — persisted
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
    sign_priv_bytes = identity.sign_private.private_bytes(
        Encoding.Raw, PrivateFormat.Raw, NoEncryption()
    )
    data = {
        "sign_private":  sign_priv_bytes.hex(),
        "sign_public":   identity.sign_public.hex(),
        "peer_id":       identity.peer_id.hex(),
        "noise_private": identity.noise_keypair.private.data.hex(),
        "noise_public":  identity.noise_keypair.public.data.hex(),
    }
    path.write_text(json.dumps(data, indent=2))


def _load(path: Path) -> Identity:
    data = json.loads(path.read_text())

    sign_priv_bytes = bytes.fromhex(data["sign_private"])
    sign_key = Ed25519PrivateKey.from_private_bytes(sign_priv_bytes)
    sign_pub = sign_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    # Load persisted Noise keypair, or generate and save a new one if absent
    # (upgrades identity.json files created before this field was added).
    if "noise_private" in data:
        noise_priv = X25519PrivateKey(bytes.fromhex(data["noise_private"]))
        noise_kp = _DH.generate_keypair(privatekey=noise_priv)
    else:
        noise_kp = _DH.generate_keypair()
        # Persist the new keypair so it survives the next restart.
        peer_id = _peer_id_from_sign_pub(sign_pub)
        _save(Identity(noise_kp, sign_key, sign_pub, peer_id), path)

    peer_id = _peer_id_from_sign_pub(sign_pub)

    return Identity(
        noise_keypair=noise_kp,
        sign_private=sign_key,
        sign_public=sign_pub,
        peer_id=peer_id,
    )
