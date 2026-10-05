# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins keeping a private key on this computer and signing with it.

What must hold:

  A password-protected key (NIP-49 ncryptsec) opens in other Nostr apps
  and theirs open here: the published test vectors decode, a wrong
  password is told apart from a damaged key, weaker files from other apps
  open, and every failure is a Nip49Error in plain words. MyEditor writes
  only its own settings, and says it doesn't know the key's history.

  The key file is readable by this account only, and a key filed under
  the wrong public key is never handed out. A file that can't be read is
  never overwritten, and a damaged one is kept aside before a new one is
  written.

  The local signer answers exactly like the remote one, later rather than
  inside the call, and its signatures verify.

  A profile that signs locally gets a local signer from the session pool;
  without its key it gets a plain explanation, not a crash. When an
  account changes how it signs, the old signer is dropped (closed, its
  key overwritten, deleted), never handed out again.
"""

import os
import stat
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import shiboken6  # noqa: E402
from PySide6.QtCore import QCoreApplication, QEvent, QObject  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from nostr import bech32, bunker, crypto, events, nip49  # noqa: E402
from nostr.bunker import BunkerSessionPool  # noqa: E402
from nostr.key_vault import KeyVault, KeyVaultError  # noqa: E402
from nostr.local_signer import LocalSigner  # noqa: E402
from nostr.profiles import Profile  # noqa: E402

SPEC_NCRYPTSEC = (
    "ncryptsec1qgg9947rlpvqu76pj5ecreduf9jxhselq2nae2kghhvd5g7dgjtcxfqtd67p9m0w57lspw8gsq6"
    "yphnm8623nsl8xn9j4jdzz84zm3frztj3z7s35vpzmqf6ksu8r89qk5z2zxfmu5gv8th8wclt0h4p")
SPEC_SECRET = "3501454135014541350145413501453fefb02227e449e57cf4d3a3ce05378683"
SK = bytes.fromhex("6e" * 32)


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def settle():
    for _ in range(3):
        QApplication.processEvents()


# -- NIP-49 ---------------------------------------------------------------------------

def test_hchacha20_matches_the_xchacha_draft_vector():
    out = nip49.hchacha20(bytes(range(32)), bytes.fromhex("000000090000004a0000000031415927"))
    assert out.hex() == "82413b4227b27bfed30e42508a877d73a0f9e4d58a74a853c12ec41326d3ecdc"


def test_the_spec_vector_opens_with_its_password():
    assert nip49.decrypt(SPEC_NCRYPTSEC, "nostr").hex() == SPEC_SECRET


def test_a_wrong_password_is_told_apart_from_a_damaged_key():
    with pytest.raises(nip49.Nip49Error) as wrong:
        nip49.decrypt(SPEC_NCRYPTSEC, "not it")
    assert wrong.value.wrong_password
    with pytest.raises(nip49.Nip49Error) as damaged:
        nip49.decrypt(SPEC_NCRYPTSEC[:-5] + "qqqqq", "nostr")
    assert not damaged.value.wrong_password


def payload_of(ncryptsec: str) -> bytes:
    _hrp, data = bech32.bech32_decode(ncryptsec)
    return bytes(bech32.convertbits(data, 5, 8, False))


def crafted(log_n: int, password: str = "pw") -> str:
    """An ncryptsec as another app might write it, with any work setting."""
    salt, nonce, aad = bytes(range(16)), bytes(range(24)), bytes([nip49.KEY_UNKNOWN])
    key = nip49._derive(password, salt, log_n) if 1 <= log_n <= 18 else bytes(32)
    sealed = nip49._xchacha_seal(key, nonce, SK, aad)
    payload = bytes([nip49.VERSION, log_n]) + salt + nonce + aad + sealed
    return bech32.bech32_encode(nip49.HRP, bech32.convertbits(list(payload), 8, 5))


def test_a_key_survives_the_round_trip_and_says_its_history_is_unknown():
    sealed = nip49.encrypt(SK, "correct horse")
    assert sealed.startswith("ncryptsec1")
    assert nip49.decrypt(sealed, "correct horse") == SK
    # MyEditor doesn't follow where a key has been (copied, pasted), so it
    # never claims the key was handled securely.
    assert payload_of(sealed)[42] == nip49.KEY_UNKNOWN


def test_passwords_are_normalized_so_the_same_word_opens_everywhere():
    sealed = nip49.encrypt(SK, "Å")            # precomposed A-ring
    assert nip49.decrypt(sealed, "Å") == SK   # A + combining ring


def test_weaker_files_from_other_apps_open_but_are_never_written():
    assert nip49.decrypt(crafted(10), "pw") == SK
    with pytest.raises(ValueError):
        nip49.encrypt(SK, "pw", log_n=10)
    with pytest.raises(ValueError):
        nip49.encrypt(SK, "pw", log_n=21)


@pytest.mark.parametrize("log_n", [21, 22, 255])
def test_a_file_too_costly_to_open_is_explained_in_words(log_n):
    with pytest.raises(nip49.Nip49Error) as err:
        nip49.decrypt(crafted(log_n), "pw")
    assert "memory" in str(err.value) and "maxmem" not in str(err.value)
    assert not err.value.wrong_password


def test_every_failure_is_a_nip49_error_in_plain_words(monkeypatch):
    # Damaged padding inside a valid checksum: convertbits refuses it.
    data = bech32.convertbits(list(payload_of(SPEC_NCRYPTSEC)), 8, 5) + [31]
    with pytest.raises(nip49.Nip49Error, match="complete"):
        nip49.decrypt(bech32.bech32_encode(nip49.HRP, data), "nostr")
    with pytest.raises(nip49.Nip49Error, match="damaged"):
        nip49.decrypt(crafted(0), "pw")

    def no_memory(*_args):
        raise MemoryError()
    monkeypatch.setattr(nip49, "_derive", no_memory)
    with pytest.raises(nip49.Nip49Error, match="memory"):
        nip49.decrypt(SPEC_NCRYPTSEC, "nostr")


# -- the key file ------------------------------------------------------------------------

def test_the_key_file_is_private_and_keys_come_back(tmp_path):
    vault = KeyVault(tmp_path / "cfg" / "nostr_keys.json")
    assert not vault.has(crypto.get_public_key(SK).hex())
    pubkey = vault.store(SK)
    assert pubkey == crypto.get_public_key(SK).hex()
    assert vault.load(pubkey) == SK and vault.has(pubkey)
    if os.name == "posix":
        mode = stat.S_IMODE(os.stat(tmp_path / "cfg" / "nostr_keys.json").st_mode)
        assert mode == 0o600
        assert stat.S_IMODE(os.stat(tmp_path / "cfg").st_mode) == 0o700


def test_a_key_filed_under_the_wrong_public_key_is_not_handed_out(tmp_path):
    path = tmp_path / "nostr_keys.json"
    vault = KeyVault(path)
    other = "ab" * 32
    path.write_text('{"version": 1, "keys": {"%s": "%s"}}' % (other, SK.hex()))
    assert vault.load(other) is None


def test_forgetting_a_key_removes_it(tmp_path):
    vault = KeyVault(tmp_path / "nostr_keys.json")
    pubkey = vault.store(SK)
    vault.forget(pubkey)
    assert vault.load(pubkey) is None


def test_a_missing_or_broken_file_means_no_keys(tmp_path):
    path = tmp_path / "nostr_keys.json"
    assert KeyVault(path).load("ab" * 32) is None
    path.write_text("{not json")
    assert KeyVault(path).load("ab" * 32) is None
    path.write_bytes(b"\xff\xfe not text")
    assert KeyVault(path).load("ab" * 32) is None


def test_a_damaged_key_file_is_kept_aside_before_a_new_one_is_written(tmp_path):
    path = tmp_path / "nostr_keys.json"
    path.write_text('{"version": 1, "keys": [')        # cut off mid-write
    pubkey = KeyVault(path).store(SK)
    assert KeyVault(path).load(pubkey) == SK
    aside = list(tmp_path.glob("nostr_keys.json.corrupt-*"))
    assert len(aside) == 1 and aside[0].read_text() == '{"version": 1, "keys": ['


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0,
                    reason="needs file permissions that apply to this user")
def test_a_key_file_that_cant_be_read_is_never_overwritten(tmp_path):
    path = tmp_path / "nostr_keys.json"
    other = bytes.fromhex("7a" * 32)
    vault = KeyVault(path)
    kept = vault.store(other)
    before = path.read_bytes()
    os.chmod(path, 0)
    try:
        with pytest.raises(KeyVaultError):
            vault.store(SK)
        with pytest.raises(OSError):         # what the windows catch
            vault.forget(kept)
    finally:
        os.chmod(path, 0o600)
    assert path.read_bytes() == before and vault.load(kept) == other


# -- the local signer ----------------------------------------------------------------------

def test_signing_answers_later_and_the_signature_verifies():
    signer = LocalSigner(SK)
    got = []
    signer.sign_event({"kind": 1, "content": "hi", "tags": [], "created_at": 1},
                      got.append, lambda r: got.append(("fail", r)))
    assert got == []          # never inside the call
    settle()
    assert events.verify_event(got[0])
    assert got[0]["pubkey"] == signer.user_pubkey


def test_self_encryption_round_trips():
    signer = LocalSigner(SK)
    out = {}
    signer.nip44_encrypt_self("secret draft", lambda c: out.setdefault("c", c), print)
    settle()
    signer.nip44_decrypt_self(out["c"], lambda p: out.setdefault("p", p), print)
    settle()
    assert out["p"] == "secret draft"


def test_a_closed_signer_refuses():
    signer = LocalSigner(SK)
    signer.close()
    failed = []
    signer.sign_event({"kind": 1, "content": "", "tags": [], "created_at": 1},
                      print, failed.append)
    settle()
    assert failed == ["not connected"]


# -- the session pool --------------------------------------------------------------------------

def local_profile(pubkey):
    return Profile(user_pubkey=pubkey, bunker_pubkey="", bunker_relays=["wss://nos.lol"],
                   local_secret_hex="", signer="local")


def test_a_local_profile_gets_a_local_signer(tmp_path):
    vault = KeyVault(tmp_path / "nostr_keys.json")
    pubkey = vault.store(SK)
    pool = BunkerSessionPool(pool=None, vault=vault)
    got = []
    pool.get(local_profile(pubkey), got.append, lambda r: got.append(("fail", r)))
    assert isinstance(got[0], LocalSigner) and got[0].user_pubkey == pubkey
    again = []
    pool.get(local_profile(pubkey), again.append, print)
    assert again[0] is got[0]


def test_a_local_profile_without_its_key_is_explained(tmp_path):
    pool = BunkerSessionPool(pool=None, vault=KeyVault(tmp_path / "nostr_keys.json"))
    failed = []
    pool.get(local_profile("cd" * 32), print, failed.append)
    assert failed and "not on this computer" in failed[0]


def test_old_profile_files_load_as_signer_app_accounts():
    profile = Profile(user_pubkey="a" * 64, bunker_pubkey="b" * 64,
                      bunker_relays=[], local_secret_hex="c" * 64)
    assert profile.signer == "remote" and not profile.is_local


# -- changing how an account signs ---------------------------------------------------------

class FakeRemote(QObject):
    """Stands in for BunkerClient: a reattach that answers when told to."""

    made = []

    def __init__(self, pool, parent=None):
        super().__init__(parent)
        self.is_connected = False
        self.closed = False
        self.answer = None
        FakeRemote.made.append(self)

    def reattach(self, *, on_success, on_failure, **_kw):
        def answer():
            self.is_connected = True
            on_success()
        self.answer = answer

    def close(self, reason=""):
        self.closed = True
        self.is_connected = False


def remote_profile(pubkey):
    return Profile(user_pubkey=pubkey, bunker_pubkey="b" * 64, bunker_relays=["wss://nos.lol"],
                   local_secret_hex="c" * 64)


@pytest.fixture
def fake_remote(monkeypatch):
    FakeRemote.made = []
    monkeypatch.setattr(bunker, "BunkerClient", FakeRemote)
    return FakeRemote.made


def deleted(obj) -> bool:
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    return not shiboken6.isValid(obj)


def test_restoring_the_key_of_an_app_paired_account_stops_signing_through_the_app(
        tmp_path, fake_remote):
    vault = KeyVault(tmp_path / "nostr_keys.json")
    pubkey = vault.store(SK)
    pool = BunkerSessionPool(pool=None, vault=vault)
    got = []
    pool.get(remote_profile(pubkey), got.append, print)
    fake_remote[0].answer()
    assert got == [fake_remote[0]]
    pool.get(local_profile(pubkey), got.append, print)
    assert isinstance(got[1], LocalSigner)
    assert fake_remote[0].closed and deleted(fake_remote[0])


def test_a_reattach_still_in_flight_is_given_up_for_the_local_key(tmp_path, fake_remote):
    vault = KeyVault(tmp_path / "nostr_keys.json")
    pubkey = vault.store(SK)
    pool = BunkerSessionPool(pool=None, vault=vault)
    waited, local = [], []
    pool.get(remote_profile(pubkey), waited.append, print)
    pool.get(local_profile(pubkey), local.append, print)
    assert isinstance(local[0], LocalSigner) and waited == local
    fake_remote[0].answer()                    # the signer app answers late
    assert fake_remote[0].closed
    again = []
    pool.get(local_profile(pubkey), again.append, print)
    assert again[0] is local[0]


def test_pairing_a_local_account_with_an_app_closes_the_local_signer(tmp_path, fake_remote):
    vault = KeyVault(tmp_path / "nostr_keys.json")
    pubkey = vault.store(SK)
    pool = BunkerSessionPool(pool=None, vault=vault)
    got = []
    pool.get(local_profile(pubkey), got.append, print)
    local = got[0]
    pool.get(remote_profile(pubkey), got.append, print)
    fake_remote[0].answer()
    assert got[1] is fake_remote[0]
    assert not local.is_connected and local._sk == bytearray()


def test_closing_the_local_signer_overwrites_its_key():
    signer = LocalSigner(SK)
    held = signer._sk
    signer.close()
    assert held == bytearray(32) and signer._sk == bytearray()


def test_a_dropped_signer_is_closed_and_deleted(tmp_path):
    vault = KeyVault(tmp_path / "nostr_keys.json")
    pubkey = vault.store(SK)
    pool = BunkerSessionPool(pool=None, vault=vault)
    got = []
    pool.get(local_profile(pubkey), got.append, print)
    pool.drop(pubkey)
    assert not got[0].is_connected and deleted(got[0])
    pool.get(local_profile(pubkey), got.append, print)
    pool.close_all()
    assert deleted(got[1])
