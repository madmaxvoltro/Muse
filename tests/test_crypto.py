import pytest

from muse_schema.crypto import (
    DecryptionError,
    NullVault,
    Vault,
    VaultUnavailable,
    add_key,
    generate_key_file,
    is_blind,
    is_token,
)


def test_seal_open_roundtrip_and_purpose_binding():
    v = Vault(generate_key_file())
    tok = v.seal({"query": "how to get over a breakup", "n": 3}, "event.metadata")
    assert is_token(tok) and "breakup" not in tok
    assert v.open(tok, "event.metadata") == {"query": "how to get over a breakup", "n": 3}
    with pytest.raises(DecryptionError):
        v.open(tok, "taste_profile.profile")  # moved to another column: refuses
    assert v.seal("x", "p") != v.seal("x", "p")  # random nonce


def test_tampering_and_wrong_key_fail():
    v = Vault(generate_key_file())
    tok = v.seal("secret", "p")
    bad = tok[:-2] + ("A" if tok[-2] != "A" else "B") + tok[-1]
    with pytest.raises(DecryptionError):
        v.open(bad, "p")
    other = Vault(generate_key_file())
    with pytest.raises(DecryptionError):
        other.open(tok, "p")


def test_blind_index_is_deterministic_keyed_and_namespaced():
    text = generate_key_file()
    v1, v2 = Vault(text), Vault(generate_key_file())
    a = v1.bid("tmdb:movie:603", "canon.key")
    assert is_blind(a) and a == Vault(text).bid("tmdb:movie:603", "canon.key")
    assert a != v2.bid("tmdb:movie:603", "canon.key")  # different key -> unlinkable
    assert a != v1.bid("tmdb:movie:603", "event.source_item_id")  # different column -> unlinkable
    assert v1.bid(a, "canon.key") == a  # idempotent


def test_rotation_keeps_old_data_readable():
    text = generate_key_file()
    old = Vault(text)
    tok = old.seal({"a": 1}, "p")
    rotated_text, kid = add_key(text)
    new = Vault(rotated_text)
    assert new.active_kid == kid == "k2"
    assert new.open(tok, "p") == {"a": 1} and new.needs_reseal(tok)
    assert not new.needs_reseal(new.seal({"a": 1}, "p"))
    assert new.bid("x", "p") == old.bid("x", "p")  # index key never rotates


def test_json_helpers_and_plaintext_passthrough():
    v = Vault(generate_key_file())
    sealed = v.seal_json({"title": "Dark"}, "event.metadata")
    assert set(sealed) == {"_enc"} and v.open_json(sealed, "event.metadata") == {"title": "Dark"}
    assert v.open_json({"title": "legacy row"}, "event.metadata") == {"title": "legacy row"}
    assert NullVault().seal_json({"a": 1}, "p") == {"a": 1}


def test_bad_key_files():
    with pytest.raises(VaultUnavailable):
        Vault("k1:" + "QUFB")  # too short, no idx
