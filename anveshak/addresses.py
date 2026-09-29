"""Address validation and canonicalisation.

Every address that enters the system goes through `normalize()`. Malformed input is
rejected with `AddressError` — never "corrected" or guessed. A mistyped address that
happens to be valid-looking would send an investigation after the wrong person, so all
checksums (Base58Check, Bech32/Bech32m, EIP-55) are enforced.

Canonical forms:
  bitcoin   base58 as given (case-sensitive), bech32/bech32m lower-case
  evm       0x + 40 lower-case hex
  tron      Base58Check "T..." form
  solana    base58 of a 32-byte public key, as given (Solana addresses carry NO checksum:
            a typo that still decodes to 32 bytes cannot be detected — confirm by hand)
"""

from __future__ import annotations

import hashlib
import re

from Crypto.Hash import keccak

from .chain import Chain, ChainFamily


class AddressError(ValueError):
    pass


# --------------------------------------------------------------------------- base58

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _dsha256(data: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def b58decode(text: str) -> bytes:
    n = 0
    for ch in text:
        idx = _B58.find(ch)
        if idx < 0:
            raise AddressError(f"invalid base58 character {ch!r}")
        n = n * 58 + idx
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    leading_zeros = len(text) - len(text.lstrip("1"))
    return b"\x00" * leading_zeros + body


def b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = []
    while n:
        n, rem = divmod(n, 58)
        out.append(_B58[rem])
    leading_zeros = len(data) - len(data.lstrip(b"\x00"))
    return "1" * leading_zeros + "".join(reversed(out))


def b58check_decode(text: str) -> bytes:
    raw = b58decode(text)
    if len(raw) < 5:
        raise AddressError("base58check payload too short")
    payload, checksum = raw[:-4], raw[-4:]
    if _dsha256(payload)[:4] != checksum:
        raise AddressError("base58check checksum mismatch")
    return payload


def b58check_encode(payload: bytes) -> str:
    return b58encode(payload + _dsha256(payload)[:4])


# --------------------------------------------------------------------------- bech32 (BIP-173 / BIP-350)

_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_CONST = 1
_BECH32M_CONST = 0x2BC830A3


def _bech32_polymod(values: list[int]) -> int:
    gen = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for v in values:
        top = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ v
        for i in range(5):
            if (top >> i) & 1:
                chk ^= gen[i]
    return chk


def _bech32_hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _bech32_decode(text: str) -> tuple[str, list[int], int]:
    if any(ord(c) < 33 or ord(c) > 126 for c in text):
        raise AddressError("bech32 contains invalid characters")
    if text.lower() != text and text.upper() != text:
        raise AddressError("bech32 must not mix upper and lower case")
    text = text.lower()
    pos = text.rfind("1")
    if pos < 1 or pos + 7 > len(text) or len(text) > 90:
        raise AddressError("bech32 has invalid separator position or length")
    if any(c not in _BECH32_CHARSET for c in text[pos + 1 :]):
        raise AddressError("bech32 data part contains invalid characters")
    hrp = text[:pos]
    data = [_BECH32_CHARSET.find(c) for c in text[pos + 1 :]]
    const = _bech32_polymod(_bech32_hrp_expand(hrp) + data)
    if const not in (_BECH32_CONST, _BECH32M_CONST):
        raise AddressError("bech32 checksum mismatch")
    return hrp, data[:-6], const


def _convertbits(data: list[int], frombits: int, tobits: int, pad: bool = False) -> list[int]:
    acc = bits = 0
    out = []
    maxv = (1 << tobits) - 1
    max_acc = (1 << (frombits + tobits - 1)) - 1
    for value in data:
        if value < 0 or value >> frombits:
            raise AddressError("bech32 value out of range")
        acc = ((acc << frombits) | value) & max_acc
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            out.append((acc >> bits) & maxv)
    if pad:
        if bits:
            out.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        raise AddressError("bech32 has invalid padding")
    return out


def encode_segwit(version: int, program: bytes, hrp: str = "bc") -> str:
    """Encode a witness program as a bech32 (v0) / bech32m (v1+) address."""
    data = [version] + _convertbits(list(program), 8, 5, pad=True)
    const = _BECH32_CONST if version == 0 else _BECH32M_CONST
    polymod = _bech32_polymod(_bech32_hrp_expand(hrp) + data + [0] * 6) ^ const
    checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_BECH32_CHARSET[d] for d in data + checksum)


def _decode_segwit(address: str) -> tuple[int, bytes]:
    hrp, data, const = _bech32_decode(address)
    if hrp != "bc":
        raise AddressError(f"unsupported bech32 prefix {hrp!r} (mainnet 'bc' only)")
    if not data:
        raise AddressError("empty segwit data")
    version = data[0]
    program = bytes(_convertbits(data[1:], 5, 8))
    if version > 16 or not 2 <= len(program) <= 40:
        raise AddressError("invalid segwit version or program length")
    if version == 0 and len(program) not in (20, 32):
        raise AddressError("invalid v0 witness program length")
    expected = _BECH32_CONST if version == 0 else _BECH32M_CONST
    if const != expected:
        raise AddressError("wrong bech32 variant for witness version")
    return version, program


# --------------------------------------------------------------------------- EIP-55

_EVM_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_HEX40_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def keccak256(data: bytes) -> bytes:
    return keccak.new(digest_bits=256, data=data).digest()


def evm_checksum(address: str) -> str:
    """EIP-55 mixed-case checksum form of a 0x address."""
    body = address[2:].lower()
    digest = keccak256(body.encode("ascii")).hex()
    return "0x" + "".join(c.upper() if int(digest[i], 16) >= 8 else c for i, c in enumerate(body))


def _normalize_evm(address: str) -> str:
    if not _EVM_RE.match(address):
        raise AddressError("EVM address must be 0x followed by 40 hex characters")
    body = address[2:]
    if body != body.lower() and body != body.upper() and evm_checksum(address) != address:
        raise AddressError("EIP-55 checksum mismatch (mixed-case address with wrong checksum)")
    return address.lower()


# --------------------------------------------------------------------------- Tron

def tron_from_hex(hex_address: str) -> str:
    """Convert '41'+40hex, or bare 40 hex / 0x+40hex (event-log form), to base58 'T...'."""
    h = hex_address.lower()
    if h.startswith("0x"):
        h = h[2:]
    if len(h) == 40 and _HEX40_RE.match(h):
        h = "41" + h
    if len(h) != 42 or not h.startswith("41") or not _HEX40_RE.match(h[2:]):
        raise AddressError(f"not a Tron hex address: {hex_address!r}")
    return b58check_encode(bytes.fromhex(h))


def tron_to_hex(address: str) -> str:
    """Base58 'T...' to '41'+40 hex."""
    payload = b58check_decode(address)
    if len(payload) != 21 or payload[0] != 0x41:
        raise AddressError("Tron address must decode to 21 bytes starting with 0x41")
    return payload.hex()


def _normalize_tron(address: str) -> str:
    if address.startswith("41") and len(address) == 42:
        return tron_from_hex(address)
    if not address.startswith("T") or len(address) != 34:
        raise AddressError("Tron address must be 34-character base58 starting with 'T'")
    tron_to_hex(address)  # validates checksum and prefix
    return address


# --------------------------------------------------------------------------- Bitcoin

def _normalize_bitcoin(address: str) -> str:
    if address[:3].lower() == "bc1":
        _decode_segwit(address)
        return address.lower()
    if address[:1] in ("1", "3"):
        payload = b58check_decode(address)
        if len(payload) != 21 or payload[0] not in (0x00, 0x05):
            raise AddressError("base58 Bitcoin address must be mainnet P2PKH (0x00) or P2SH (0x05)")
        return address
    raise AddressError("unsupported Bitcoin address format (mainnet P2PKH, P2SH, bech32, bech32m only)")


# --------------------------------------------------------------------------- Solana

def _normalize_solana(address: str) -> str:
    if not 32 <= len(address) <= 44:
        raise AddressError("Solana address must be 32-44 base58 characters")
    if len(b58decode(address)) != 32:
        raise AddressError("Solana address must decode to 32 bytes")
    return address


_ED25519_P = 2**255 - 19
_ED25519_D = (-121665 * pow(121666, _ED25519_P - 2, _ED25519_P)) % _ED25519_P
_SQRT_M1 = pow(2, (_ED25519_P - 1) // 4, _ED25519_P)


def ed25519_on_curve(key: bytes) -> bool:
    """True if the 32 bytes decompress to a point on the ed25519 curve (RFC 8032 section 5.1.3).

    Solana program-derived addresses (token accounts, pool vaults, program authorities) are
    deliberately *off* the curve, so no private key exists for them: they are controlled by
    programs, not people. This is how the tracer recognises them without any API call."""
    if len(key) != 32:
        return False
    y = int.from_bytes(key, "little") & ((1 << 255) - 1)
    sign = key[31] >> 7
    if y >= _ED25519_P:
        return False
    u = (y * y - 1) % _ED25519_P
    v = (_ED25519_D * y * y + 1) % _ED25519_P
    x2 = u * pow(v, _ED25519_P - 2, _ED25519_P) % _ED25519_P
    x = pow(x2, (_ED25519_P + 3) // 8, _ED25519_P)
    if (x * x - x2) % _ED25519_P != 0:
        x = x * _SQRT_M1 % _ED25519_P
    if (x * x - x2) % _ED25519_P != 0:
        return False
    return not (x == 0 and sign == 1)


def solana_is_program_derived(address: str) -> bool:
    return not ed25519_on_curve(b58decode(address))


def solana_find_program_address(seeds: list[bytes], program_id: str) -> tuple[str, int]:
    """Solana `find_program_address`: first bump (255 down to 0) whose hash is off the curve."""
    program = b58decode(program_id)
    for bump in range(255, -1, -1):
        digest = hashlib.sha256(b"".join(seeds) + bytes([bump]) + program + b"ProgramDerivedAddress").digest()
        if not ed25519_on_curve(digest):
            return b58encode(digest), bump
    raise AddressError("no viable program address")


SPL_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ASSOCIATED_TOKEN_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"


def solana_associated_token_account(owner: str, mint: str, token_program: str = SPL_TOKEN_PROGRAM) -> str:
    return solana_find_program_address([b58decode(owner), b58decode(token_program), b58decode(mint)], ASSOCIATED_TOKEN_PROGRAM)[0]


# --------------------------------------------------------------------------- entry point

def normalize(chain: Chain, address: str) -> str:
    """Validate `address` for `chain` and return its canonical form, or raise AddressError."""
    if not isinstance(address, str):
        raise AddressError("address must be a string")
    address = address.strip()
    if not address:
        raise AddressError("empty address")
    family = chain.family
    if family is ChainFamily.EVM:
        return _normalize_evm(address)
    if family is ChainFamily.TRON:
        return _normalize_tron(address)
    if family is ChainFamily.SOLANA:
        return _normalize_solana(address)
    return _normalize_bitcoin(address)


def is_valid(chain: Chain, address: str) -> bool:
    try:
        normalize(chain, address)
        return True
    except AddressError:
        return False


def detect_chains(address: str) -> list[Chain]:
    """Chains on which `address` is valid, decided by format and checksum alone.

    An EVM address is the same key on every EVM chain, so all EVM chains are returned; the
    caller decides which to trace (e.g. only those with activity). The formats cannot
    collide: Bitcoin and Tron base58 decode to 25 bytes (with checksums), Solana to 32."""
    return [chain for chain in Chain if is_valid(chain, address.strip())]
