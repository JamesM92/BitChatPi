#!/usr/bin/env python3
"""
check-compat.py — verify that Pi protocol constants match the upstream Android source.

The Android repo is the authority. This script fails (exit 1) if any critical
constant in bitchatd/protocol/constants.py diverges from the upstream snapshots
in upstream/android/AppConstants.kt and related files.

Run from the repo root:
    python3 upstream/scripts/check-compat.py
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ANDROID_DIR = REPO_ROOT / "upstream" / "android"
PI_CONSTANTS = REPO_ROOT / "bitchatd" / "protocol" / "constants.py"

errors: list[str] = []
warnings: list[str] = []


# ── helpers ──────────────────────────────────────────────────────────────────

def read(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text()

def extract_kt_uuid(text: str, name: str) -> str | None:
    """Extract UUID string from Kotlin: UUID.fromString("...") near `name`."""
    pattern = rf'{name}.*?UUID\.fromString\("([^"]+)"\)'
    m = re.search(pattern, text, re.DOTALL)
    return m.group(1).upper() if m else None

def extract_kt_int(text: str, name: str) -> int | None:
    """Extract Int/Long constant from Kotlin: const val NAME = 123"""
    m = re.search(rf'(?:const val|val)\s+{name}\s*[:=][^=].*?=\s*([0-9_]+)', text)
    if m:
        return int(m.group(1).replace("_", ""))
    return None

def extract_py_str(text: str, name: str) -> str | None:
    """Extract string constant from Python: NAME = "..." """
    m = re.search(rf'^{name}\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    return m.group(1).upper() if m else None

def extract_py_int(text: str, name: str) -> int | None:
    """Extract int constant from Python: NAME = 123"""
    m = re.search(rf'^{name}\s*=\s*([0-9_]+)', text, re.MULTILINE)
    return int(m.group(1).replace("_", "")) if m else None

def extract_py_float(text: str, name: str) -> float | None:
    m = re.search(rf'^{name}\s*=\s*([0-9.]+)', text, re.MULTILINE)
    return float(m.group(1)) if m else None

def check(label: str, android_val, pi_val, critical: bool = True):
    status = "critical" if critical else "warning"
    if android_val is None:
        warnings.append(f"[SKIP]  {label}: could not parse Android value")
        return
    if pi_val is None:
        (errors if critical else warnings).append(
            f"[{status.upper()}] {label}: not found in Pi constants.py  (Android: {android_val})"
        )
        return
    if str(android_val) != str(pi_val):
        (errors if critical else warnings).append(
            f"[{'FAIL' if critical else 'WARN'}]  {label}: Android={android_val!r}  Pi={pi_val!r}"
        )
    else:
        print(f"  [OK]    {label} = {android_val!r}")


# ── load files ───────────────────────────────────────────────────────────────

kt_constants = read(ANDROID_DIR / "AppConstants.kt")
kt_relay     = read(ANDROID_DIR / "PacketRelayManager.kt")
kt_fragment  = read(ANDROID_DIR / "FragmentPayload.kt")
pi_c         = read(PI_CONSTANTS)

if not kt_constants:
    print("ERROR: upstream/android/AppConstants.kt not found — run fetch-upstream.sh first")
    sys.exit(1)

if not pi_c:
    print("NOTE: bitchatd/protocol/constants.py does not exist yet — nothing to check")
    sys.exit(0)


# ── BLE UUIDs ────────────────────────────────────────────────────────────────

print("\n── BLE GATT UUIDs ──")
check("SERVICE_UUID",
      extract_kt_uuid(kt_constants, "SERVICE_UUID"),
      extract_py_str(pi_c, "SERVICE_UUID"))

check("CHARACTERISTIC_UUID",
      extract_kt_uuid(kt_constants, "CHARACTERISTIC_UUID"),
      extract_py_str(pi_c, "CHARACTERISTIC_UUID"))

check("DESCRIPTOR_UUID",
      extract_kt_uuid(kt_constants, "DESCRIPTOR_UUID"),
      extract_py_str(pi_c, "DESCRIPTOR_UUID"))


# ── Fragmentation ────────────────────────────────────────────────────────────

print("\n── Fragmentation ──")
check("FRAGMENT_SIZE_THRESHOLD",
      extract_kt_int(kt_constants, "FRAGMENT_SIZE_THRESHOLD"),
      extract_py_int(pi_c, "FRAGMENT_SIZE_THRESHOLD"))

check("MAX_FRAGMENT_SIZE",
      extract_kt_int(kt_constants, "MAX_FRAGMENT_SIZE"),
      extract_py_int(pi_c, "MAX_FRAGMENT_SIZE"))

check("FRAGMENT_TIMEOUT_MS",
      extract_kt_int(kt_constants, "FRAGMENT_TIMEOUT_MS"),
      extract_py_int(pi_c, "FRAGMENT_TIMEOUT_MS"))

check("MAX_FRAGMENTS_PER_ID",
      extract_kt_int(kt_constants, "MAX_FRAGMENTS_PER_ID"),
      extract_py_int(pi_c, "MAX_FRAGMENTS_PER_ID"))

check("MAX_FRAGMENT_TOTAL_BYTES",
      extract_kt_int(kt_constants, "MAX_FRAGMENT_TOTAL_BYTES"),
      extract_py_int(pi_c, "MAX_FRAGMENT_TOTAL_BYTES"))

check("MAX_ACTIVE_FRAGMENT_SETS",
      extract_kt_int(kt_constants, "MAX_ACTIVE_FRAGMENT_SETS"),
      extract_py_int(pi_c, "MAX_ACTIVE_FRAGMENT_SETS"))

check("FRAGMENT_HEADER_SIZE",
      13,  # FragmentPayload.HEADER_SIZE — hardcoded in source
      extract_py_int(pi_c, "FRAGMENT_HEADER_SIZE"))


# ── Relay logic ──────────────────────────────────────────────────────────────

print("\n── Relay probabilities ──")
# Extract probabilities from PacketRelayManager.kt comment block we added
def kt_relay_prob(size_expr: str) -> float | None:
    m = re.search(rf'networkSize\s*<=\s*{size_expr}\s*->\s*([0-9.]+)', kt_relay)
    return float(m.group(1)) if m else None

check("RELAY_PROB_LE10",  kt_relay_prob("10"),  extract_py_float(pi_c, "RELAY_PROB_LE10"),  critical=True)
check("RELAY_PROB_LE30",  kt_relay_prob("30"),  extract_py_float(pi_c, "RELAY_PROB_LE30"),  critical=True)
check("RELAY_PROB_LE50",  kt_relay_prob("50"),  extract_py_float(pi_c, "RELAY_PROB_LE50"),  critical=True)
check("RELAY_PROB_LE100", kt_relay_prob("100"), extract_py_float(pi_c, "RELAY_PROB_LE100"), critical=True)

# TTL
print("\n── TTL ──")
check("MESSAGE_TTL_HOPS",
      extract_kt_int(kt_constants, "MESSAGE_TTL_HOPS"),
      extract_py_int(pi_c, "MESSAGE_TTL_HOPS"))

check("SYNC_TTL_HOPS",
      extract_kt_int(kt_constants, "SYNC_TTL_HOPS"),
      extract_py_int(pi_c, "SYNC_TTL_HOPS"))

# Security / dedup
print("\n── Security / dedup ──")
check("MAX_PROCESSED_MESSAGES",
      extract_kt_int(kt_constants, "MAX_PROCESSED_MESSAGES"),
      extract_py_int(pi_c, "MAX_PROCESSED_MESSAGES"))

check("COMPRESSION_THRESHOLD_BYTES",
      extract_kt_int(kt_constants, "COMPRESSION_THRESHOLD_BYTES"),
      extract_py_int(pi_c, "COMPRESSION_THRESHOLD_BYTES"))

check("STALE_PEER_TIMEOUT_MS",
      extract_kt_int(kt_constants, "STALE_PEER_TIMEOUT_MS"),
      extract_py_int(pi_c, "STALE_PEER_TIMEOUT_MS"),
      critical=False)


# ── summary ──────────────────────────────────────────────────────────────────

print()
if warnings:
    print("Warnings:")
    for w in warnings:
        print(f"  {w}")

if errors:
    print("\nCompatibility FAILURES:")
    for e in errors:
        print(f"  {e}")
    print(f"\n{len(errors)} failure(s) — Pi constants do not match Android upstream.")
    sys.exit(1)
else:
    print(f"All checks passed ({len(warnings)} warning(s)).")
    sys.exit(0)
