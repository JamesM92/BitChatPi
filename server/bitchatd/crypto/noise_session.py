"""
Per-peer Noise XX session state machine.

Protocol: Noise_XX_25519_ChaChaPoly_SHA256

XX pattern (3 messages):
  -> e                          (32 bytes)
  <- e, ee, s, es               (96 bytes)
  -> s, se                      (48 bytes)

Transport nonce format matches Android/iOS implementation:
  Each NOISE_ENCRYPTED payload = <4-byte-nonce-BE><ciphertext+16-byte-MAC>
  The nonce is transmitted explicitly so out-of-order delivery is possible.
"""
from __future__ import annotations
import struct
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

from dissononce.processing.impl.handshakestate import HandshakeState
from dissononce.processing.impl.symmetricstate import SymmetricState
from dissononce.processing.impl.cipherstate import CipherState
from dissononce.processing.handshakepatterns.interactive.XX import XXHandshakePattern
from dissononce.dh.x25519.x25519 import X25519DH
from dissononce.dh.x25519.keypair import KeyPair as X25519KeyPair
from dissononce.cipher.chachapoly import ChaChaPolyCipher
from dissononce.hash.sha256 import SHA256Hash
from dissononce.exceptions.decrypt import DecryptFailedException

from bitchatd.protocol.constants import NOISE_REKEY_TIME_MS, NOISE_REKEY_MSG_LIMIT_SESSION

_DH = X25519DH()

_NONCE_SIZE = 4           # 4-byte big-endian nonce prefix on every transport message
_NONCE_MAX  = 0xFFFF_FFFF # uint32 max — must rekey before reaching this


class SessionState(Enum):
    HANDSHAKE = auto()   # exchange in progress
    TRANSPORT = auto()   # session established
    FAILED    = auto()   # unrecoverable error


@dataclass
class NoiseSession:
    """
    Noise XX session for one remote peer.

    Handshake usage (Pi as responder — phone is BLE central/initiator):
        sess = NoiseSession.create_responder(local_kp)
        msg2 = sess.read_handshake_message(msg1_from_phone)
        # send msg2 back
        sess.read_handshake_message(msg3_from_phone)
        # session established

    Transport:
        wire_bytes = sess.encrypt(plaintext)   # <4B-nonce><ciphertext+MAC>
        plaintext  = sess.decrypt(wire_bytes)  # extracts nonce, decrypts
    """
    _hs:            HandshakeState
    _initiator:     bool
    _state:         SessionState = field(default=SessionState.HANDSHAKE)
    _cs_send:       Optional[CipherState] = field(default=None)
    _cs_recv:       Optional[CipherState] = field(default=None)
    _remote_static: Optional[bytes] = field(default=None)
    _send_nonce:    int = field(default=0)
    _session_start: float = field(default_factory=time.monotonic)
    _msg_count:     int = field(default=0)

    @classmethod
    def create_initiator(cls, local_keypair: X25519KeyPair) -> "NoiseSession":
        hs = _make_handshake_state()
        hs.initialize(XXHandshakePattern(), initiator=True, prologue=b'', s=local_keypair)
        return cls(_hs=hs, _initiator=True)

    @classmethod
    def create_responder(cls, local_keypair: X25519KeyPair) -> "NoiseSession":
        hs = _make_handshake_state()
        hs.initialize(XXHandshakePattern(), initiator=False, prologue=b'', s=local_keypair)
        return cls(_hs=hs, _initiator=False)

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def is_established(self) -> bool:
        return self._state == SessionState.TRANSPORT

    @property
    def remote_static_public(self) -> Optional[bytes]:
        return self._remote_static

    def write_handshake_message(self, payload: bytes = b'') -> Optional[bytes]:
        """Write next handshake message. Returns None if session already failed."""
        if self._state == SessionState.FAILED:
            return None
        try:
            buf = bytearray()
            result = self._hs.write_message(payload, buf)
            if result is not None:
                self._cs_send, self._cs_recv = result
                self._remote_static = self._hs.rs.data if self._hs.rs else None
                self._state = SessionState.TRANSPORT
                self._session_start = time.monotonic()
            return bytes(buf)
        except Exception:
            self._state = SessionState.FAILED
            return None

    def read_handshake_message(self, data: bytes) -> Optional[bytes]:
        """
        Read next handshake message. Returns the response message to send back,
        or b'' when this is the final message (session now established).
        Returns None on failure.
        """
        if self._state == SessionState.FAILED:
            return None
        try:
            buf = bytearray()
            result = self._hs.read_message(data, buf)
            if result is not None:
                # Final read (msg3 for responder, msg2 for initiator) — split
                self._cs_recv, self._cs_send = result
                self._remote_static = self._hs.rs.data if self._hs.rs else None
                self._state = SessionState.TRANSPORT
                self._session_start = time.monotonic()
                return b''   # no response to send — session is established
            # Not final — caller must send the next write_handshake_message()
            return bytes(buf)
        except Exception:
            self._state = SessionState.FAILED
            return None

    def encrypt(self, plaintext: bytes) -> Optional[bytes]:
        """
        Encrypt plaintext for transport.
        Returns <4-byte-nonce-BE><ciphertext+16-byte-MAC> matching Android.
        Returns None if session not established or nonce exhausted.
        """
        if self._state != SessionState.TRANSPORT or self._cs_send is None:
            return None
        if self._send_nonce > _NONCE_MAX:
            return None
        self._maybe_rekey()
        nonce = self._send_nonce
        self._cs_send.set_nonce(nonce)
        ct = self._cs_send.encrypt_with_ad(b'', plaintext)
        self._send_nonce += 1
        self._msg_count += 1
        return struct.pack(">I", nonce) + ct

    def decrypt(self, wire: bytes) -> Optional[bytes]:
        """
        Decrypt a transport message.
        Expects <4-byte-nonce-BE><ciphertext+16-byte-MAC>.
        Returns None on failure.
        """
        if self._state != SessionState.TRANSPORT or self._cs_recv is None:
            return None
        if len(wire) < _NONCE_SIZE + 16:   # at minimum: nonce + empty + MAC
            return None
        try:
            nonce = struct.unpack_from(">I", wire, 0)[0]
            ct    = wire[_NONCE_SIZE:]
            self._cs_recv.set_nonce(nonce)
            return self._cs_recv.decrypt_with_ad(b'', ct)
        except (DecryptFailedException, Exception):
            return None

    def _maybe_rekey(self) -> None:
        elapsed_ms = (time.monotonic() - self._session_start) * 1000
        if (elapsed_ms >= NOISE_REKEY_TIME_MS or
                self._msg_count >= NOISE_REKEY_MSG_LIMIT_SESSION):
            if self._cs_send:
                self._cs_send.rekey()
            if self._cs_recv:
                self._cs_recv.rekey()
            self._send_nonce = 0
            self._msg_count = 0
            self._session_start = time.monotonic()


def _make_handshake_state() -> HandshakeState:
    ss = SymmetricState(CipherState(ChaChaPolyCipher()), SHA256Hash())
    return HandshakeState(ss, _DH)
