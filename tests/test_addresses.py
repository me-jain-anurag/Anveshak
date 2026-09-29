import pytest

from anveshak.addresses import AddressError, encode_segwit, evm_checksum, is_valid, normalize, tron_from_hex, tron_to_hex
from anveshak.chain import Chain


# Real pairs observed in live TronGrid responses (2026-09-30): event-log hex vs base58 form.
@pytest.mark.parametrize(
    "hex_form, base58",
    [
        ("41b28f509a1861a9689b8e9b6675b390680adee624", "TSFLxtstFSkzQD6MM1vdBKrKstfFgAMgEm"),
        ("0x63348c5a9ecdc090e0ccf7ad7b5fa36ce3df3ba4", "TK1krAecBzu9cxT6Vd5GcivSASZWs856pN"),
        ("41a614f803b6fd780986a42c78ec9c7f77e6ded13c", "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"),  # USDT contract
    ],
)
def test_tron_hex_base58_roundtrip(hex_form, base58):
    assert tron_from_hex(hex_form) == base58
    assert tron_to_hex(base58)[2:] == hex_form.lower().removeprefix("0x").removeprefix("41")
    assert normalize(Chain.TRON, base58) == base58


def test_tron_rejects_bad_checksum_and_evm_form():
    assert not is_valid(Chain.TRON, "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6u")
    assert not is_valid(Chain.TRON, "0xa614f803b6fd780986a42c78ec9c7f77e6ded13c")


# BIP-173 / BIP-350 test vectors.
def test_bitcoin_segwit_vectors():
    assert normalize(Chain.BITCOIN, "BC1QW508D6QEJXTDG4Y5R3ZARVARY0C5XW7KV8F3T4") == "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
    assert is_valid(Chain.BITCOIN, "bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0")
    assert not is_valid(Chain.BITCOIN, "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t5")  # checksum
    assert not is_valid(Chain.BITCOIN, "bc1QW508D6QEJXTDG4Y5R3ZARVARY0C5XW7KV8F3T4")  # mixed case
    assert not is_valid(Chain.BITCOIN, "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx")  # testnet


def test_bitcoin_base58():
    assert is_valid(Chain.BITCOIN, "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa")
    assert is_valid(Chain.BITCOIN, "34xp4vRoCGJym3xR7yCVPFHoCNxv4Twseo")
    assert not is_valid(Chain.BITCOIN, "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNb")


def test_encode_segwit_matches_bip173_vector():
    program = bytes.fromhex("751e76e8199196d454941c45d1b3a323f1433bd6")
    assert encode_segwit(0, program) == "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"


# EIP-55 test vectors.
@pytest.mark.parametrize("addr", ["0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed", "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359", "0xdbF03B407c01E7cD3CBea99509d93f8DDDC8C6FB"])
def test_eip55_valid(addr):
    assert evm_checksum(addr.lower()) == addr
    assert normalize(Chain.ETHEREUM, addr) == addr.lower()


def test_eip55_wrong_checksum_rejected_but_single_case_accepted():
    with pytest.raises(AddressError):
        normalize(Chain.ETHEREUM, "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAeD")
    assert normalize(Chain.POLYGON, "0x5AAEB6053F3E94C9B9A09F33669435E7EF1BEAED") == "0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed"


@pytest.mark.parametrize("bad", ["", "   ", "0x123", "not-an-address", "0x" + "g" * 40])
def test_garbage_rejected(bad):
    for chain in Chain:
        assert not is_valid(chain, bad)
