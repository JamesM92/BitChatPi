"""
Phase 2 crypto tests: identity persistence, Ed25519 signing, Noise XX sessions.
"""
import json
import tempfile
from pathlib import Path

import pytest

from bitchatd.crypto.identity import generate, load_or_create, _peer_id_from_sign_pub
from bitchatd.crypto.signing import sign, verify
from bitchatd.crypto.noise_session import NoiseSession, SessionState


# ── Identity ───────────────────────────────────────────────────────────────────

def test_generate_produces_8_byte_peer_id():
    ident = generate()
    assert len(ident.peer_id) == 8


def test_peer_id_derived_from_sign_public():
    ident = generate()
    expected = _peer_id_from_sign_pub(ident.sign_public)
    assert ident.peer_id == expected


def test_save_and_load_round_trip():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "identity.json"
        ident = load_or_create(path)
        loaded = load_or_create(path)
        # peer_id and signing key are stable; noise keypair is regenerated each load
        assert ident.peer_id == loaded.peer_id
        assert ident.sign_public == loaded.sign_public
        assert ident.noise_keypair.public.data != loaded.noise_keypair.public.data


def test_load_or_create_is_idempotent():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "sub" / "identity.json"
        a = load_or_create(path)
        b = load_or_create(path)
        assert a.peer_id == b.peer_id


def test_identity_json_has_expected_keys():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "identity.json"
        load_or_create(path)
        data = json.loads(path.read_text())
        # noise keypair is never persisted — regenerated fresh each run
        for key in ("sign_private", "sign_public", "peer_id"):
            assert key in data
        assert "noise_private" not in data
        assert "noise_public" not in data


# ── Ed25519 signing ────────────────────────────────────────────────────────────

def test_sign_produces_64_bytes():
    ident = generate()
    sig = sign(ident.sign_private, b"test message")
    assert len(sig) == 64


def test_verify_valid_signature():
    ident = generate()
    msg = b"announce payload"
    sig = sign(ident.sign_private, msg)
    assert verify(ident.sign_public, msg, sig)


def test_verify_wrong_message():
    ident = generate()
    sig = sign(ident.sign_private, b"original")
    assert not verify(ident.sign_public, b"tampered", sig)


def test_verify_wrong_key():
    a = generate()
    b = generate()
    sig = sign(a.sign_private, b"data")
    assert not verify(b.sign_public, b"data", sig)


def test_verify_truncated_signature():
    ident = generate()
    sig = sign(ident.sign_private, b"data")
    assert not verify(ident.sign_public, b"data", sig[:32])


# ── Noise XX session ───────────────────────────────────────────────────────────

def _complete_xx_handshake():
    """Return (initiator_session, responder_session) both in TRANSPORT state."""
    from dissononce.dh.x25519.x25519 import X25519DH
    dh = X25519DH()
    kp_i = dh.generate_keypair()
    kp_r = dh.generate_keypair()

    sess_i = NoiseSession.create_initiator(kp_i)
    sess_r = NoiseSession.create_responder(kp_r)

    # msg1: -> e
    msg1 = sess_i.write_handshake_message()
    assert msg1 is not None
    sess_r.read_handshake_message(msg1)

    # msg2: <- e, ee, s, es
    msg2 = sess_r.write_handshake_message()
    assert msg2 is not None
    sess_i.read_handshake_message(msg2)

    # msg3: -> s, se  (final — both sides transition to TRANSPORT)
    msg3 = sess_i.write_handshake_message()
    assert msg3 is not None
    sess_r.read_handshake_message(msg3)

    return sess_i, sess_r


def test_noise_xx_handshake_completes():
    sess_i, sess_r = _complete_xx_handshake()
    assert sess_i.is_established
    assert sess_r.is_established


def test_noise_encrypt_decrypt_round_trip():
    sess_i, sess_r = _complete_xx_handshake()
    plaintext = b"hello noise world"
    ct = sess_i.encrypt(plaintext)
    assert ct is not None
    assert ct != plaintext
    pt = sess_r.decrypt(ct)
    assert pt == plaintext


def test_noise_multiple_messages():
    sess_i, sess_r = _complete_xx_handshake()
    for i in range(10):
        msg = f"message {i}".encode()
        ct = sess_i.encrypt(msg)
        pt = sess_r.decrypt(ct)
        assert pt == msg


def test_noise_bidirectional():
    sess_i, sess_r = _complete_xx_handshake()
    # initiator → responder
    ct1 = sess_i.encrypt(b"ping")
    assert sess_r.decrypt(ct1) == b"ping"
    # responder → initiator
    ct2 = sess_r.encrypt(b"pong")
    assert sess_i.decrypt(ct2) == b"pong"


def test_noise_decrypt_wrong_ciphertext():
    sess_i, sess_r = _complete_xx_handshake()
    ct = sess_i.encrypt(b"data")
    tampered = bytes([ct[0] ^ 0xFF]) + ct[1:]
    assert sess_r.decrypt(tampered) is None


def test_noise_remote_static_key_known_after_handshake():
    from dissononce.dh.x25519.x25519 import X25519DH
    dh = X25519DH()
    kp_i = dh.generate_keypair()
    kp_r = dh.generate_keypair()

    sess_i = NoiseSession.create_initiator(kp_i)
    sess_r = NoiseSession.create_responder(kp_r)

    msg1 = sess_i.write_handshake_message()
    sess_r.read_handshake_message(msg1)
    msg2 = sess_r.write_handshake_message()
    sess_i.read_handshake_message(msg2)
    msg3 = sess_i.write_handshake_message()
    sess_r.read_handshake_message(msg3)

    # Each side should know the other's static public key
    assert sess_i.remote_static_public == kp_r.public.data
    assert sess_r.remote_static_public == kp_i.public.data


def test_noise_encrypt_before_handshake_returns_none():
    from dissononce.dh.x25519.x25519 import X25519DH
    kp = X25519DH().generate_keypair()
    sess = NoiseSession.create_initiator(kp)
    assert sess.encrypt(b"data") is None


def test_noise_wire_format_has_4_byte_nonce():
    """Transport payload = <4B nonce BE><ciphertext+MAC> — matching Android."""
    sess_i, sess_r = _complete_xx_handshake()
    wire = sess_i.encrypt(b"test")
    assert wire is not None
    assert len(wire) >= 4 + len(b"test") + 16   # nonce + ciphertext + MAC
    # Nonce starts at 0
    import struct
    nonce = struct.unpack_from(">I", wire, 0)[0]
    assert nonce == 0


def test_noise_explicit_nonce_increments():
    sess_i, sess_r = _complete_xx_handshake()
    import struct
    for expected_nonce in range(5):
        wire = sess_i.encrypt(b"x")
        nonce = struct.unpack_from(">I", wire, 0)[0]
        assert nonce == expected_nonce


def test_noise_state_transitions():
    from dissononce.dh.x25519.x25519 import X25519DH
    dh = X25519DH()
    kp_i = dh.generate_keypair()
    kp_r = dh.generate_keypair()

    sess_i = NoiseSession.create_initiator(kp_i)
    assert sess_i.state == SessionState.HANDSHAKE

    sess_r = NoiseSession.create_responder(kp_r)
    msg1 = sess_i.write_handshake_message()
    assert sess_i.state == SessionState.HANDSHAKE

    sess_r.read_handshake_message(msg1)
    msg2 = sess_r.write_handshake_message()
    assert sess_r.state == SessionState.HANDSHAKE

    sess_i.read_handshake_message(msg2)
    msg3 = sess_i.write_handshake_message()
    assert sess_i.state == SessionState.TRANSPORT

    sess_r.read_handshake_message(msg3)
    assert sess_r.state == SessionState.TRANSPORT
