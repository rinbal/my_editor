# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Live check against ndak: accounts and signers.

Not run by pytest (no ``test_`` prefix). Run with the ndak relays up:

    (cd ~/projects/ndak && docker compose up -d)
    .venv/bin/python tests/smoke_ndak_accounts.py

Covers Create Account (key on this computer, NIP-49 backup file, AccountSetup
publishing the relay list then the profile), Restore Account on a second
device, Back Up Account, local signing and NIP-44 against nak, switching
accounts, Finish Later, signing out, and pairing a NIP-46 remote signer
(`nak bunker`) by bunker:// and by nostrconnect://. The windows are built
offscreen and driven through their buttons; alerts are answered by the
harness (tests/smoke_ndak_common.py).
"""

from __future__ import annotations

import json
import os
import stat
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import smoke_ndak_common as nd  # noqa: E402

nd.bootstrap("accounts")
app = nd.qt_app()

from PySide6.QtWidgets import QWidget  # noqa: E402

from nostr import bech32, crypto, nip49  # noqa: E402
from nostr.account_backup import find_key  # noqa: E402
from nostr.account_controller import AccountController  # noqa: E402
from nostr.outbox import policy  # noqa: E402
from nostr.publisher import PublishJob, build_note  # noqa: E402
from nostr.ui.connect_dialog import ConnectDialog  # noqa: E402

RNOSTR, NRS, STRFRY, OBELISK, KHATRU = nd.RNOSTR, nd.NRS, nd.STRFRY, nd.OBELISK, nd.KHATRU
PASSWORD = "correct horse battery"

rep = nd.Report("accounts")

# Every NIP-46 request the app sends (kind 24133 is ephemeral, so relays do
# not keep it for nak to read back): recorded on the way out for the review.
import nostr.relay as _relay  # noqa: E402
SENT_24133 = []
_real_send = _relay.Relay.send


def _recording_send(self, message):
    if (isinstance(message, list) and len(message) == 2 and message[0] == "EVENT"
            and isinstance(message[1], dict) and message[1].get("kind") == 24133):
        SENT_24133.append((self.url, message[1]))
    return _real_send(self, message)


_relay.Relay.send = _recording_send
stack = nd.Stack()
host = QWidget()
controller = AccountController(relay_pool=stack.pool, directory=stack.directory,
                               vault=stack.vault, session_pool=stack.sessions,
                               store=stack.store, window=host, is_dark=lambda: False)
activated = nd.Catch(controller.activate)
statuses = nd.Catch(controller.status)
# What MainWindow._on_nostr_profile_connected does with ``activate``.
controller.activate.connect(lambda p: stack.store.set_default(p.user_pubkey))


def present_on(event_id: str) -> set:
    return {relay for relay, ok in nd.presence(event_id).items() if ok}


def names(relays) -> str:
    return ", ".join(sorted(nd.RELAY_NAMES.get(r, r) for r in relays)) or "none"


def latest(relays, kind, author):
    found = nd.nak_req(relays, kinds=[kind], authors=[author])
    return max(found, key=lambda e: e["created_at"]) if found else None


# --------------------------------------------------------------------------- #
rep.section("Create Account: name, NIP-49 backup file, key on this computer, setup")

backup_path = nd.HOME / "Documents" / "alice-backup.txt"
backup_path.parent.mkdir(parents=True, exist_ok=True)
win = controller.create_account()
win._name_edit.setText("Alice Created")
win.buttons["continue"].click()
rep.check("the name page leads to the backup page with a fresh key",
          win.page == "backup" and len(win.pubkey) == 64, win.page)
rep.check("Continue is disabled until a backup was saved",
          not win.buttons["continue"].isEnabled())
form = win.backup_form
form._choose_path = lambda _w, _pk: str(backup_path)
form.password.setText("short")
form.confirm.setText("short")
rep.check("a password under 8 characters is refused", not form.save_button.isEnabled())
form.password.setText(PASSWORD)
form.confirm.setText(PASSWORD)
backed = nd.Catch(form.backed_up)
form.save_button.click()
rep.check("Save Backup File writes the file", backed.wait(30_000) and backup_path.exists(),
          backed.last[0] if backed.fired else "")
alice_pk = win.pubkey
text = backup_path.read_text(encoding="utf-8") if backup_path.exists() else ""
mode = stat.S_IMODE(backup_path.stat().st_mode) if backup_path.exists() else 0
rep.check("the backup file is readable by its owner only (0600)", mode == 0o600, oct(mode))
rep.check("the backup names the npub and holds an ncryptsec, never an nsec",
          bech32.encode_npub(alice_pk) in text and "ncryptsec1" in text and "nsec1" not in text,
          text.splitlines()[0] if text else "")
ncryptsec = next((w for w in text.split() if w.startswith("ncryptsec1")), "")
nak_sk = nd.nak("key", "decrypt", ncryptsec, PASSWORD).stdout.strip()
nak_sk_hex = bech32.decode_nsec(nak_sk) if nak_sk.startswith("nsec1") else nak_sk
rep.check("nak opens the app's ncryptsec with the password (NIP-49 interop)",
          len(nak_sk_hex) == 64 and crypto.get_public_key(bytes.fromhex(nak_sk_hex)).hex() == alice_pk,
          nak_sk[:12] + "…")
win.buttons["continue"].click()
win.buttons["continue"].click()        # "Keep the key on this computer" is the default choice
nd.wait_until(lambda: win.page == "done", 40_000)
rep.check("setup finishes: the window shows the done page", win.page == "done", win.page)
alice = stack.store.get(alice_pk)
rep.check("the account is saved, local, active, and no longer pending",
          alice is not None and alice.is_local and not alice.setup_pending
          and stack.store.default().user_pubkey == alice_pk
          and alice.bunker_relays == [RNOSTR, NRS], str(alice))
rep.check("the key is in the key vault (0600), not in the profiles file",
          stack.vault.has(alice_pk)
          and stat.S_IMODE((nd.HOME / ".config/my_editor/nostr_keys.json").stat().st_mode) == 0o600
          and nak_sk_hex not in (nd.HOME / ".config/my_editor/nostr_profiles.json").read_text(
              encoding="utf-8"))

nd.spin(500)
relay_list = latest([RNOSTR, NRS, STRFRY], 10002, alice_pk)
profile_ev = latest([RNOSTR, NRS, STRFRY], 0, alice_pk)
if rep.check("the relay list (kind 10002) was published", relay_list is not None):
    nd.review_event(rep, "new account relay list", relay_list, kind=10002)
    rep.check("NIP-65: the starter relays, no marker (read and write), empty content",
              relay_list["tags"] == [["r", RNOSTR], ["r", NRS]] and relay_list["content"] == "",
              json.dumps(relay_list["tags"]))
    where = present_on(relay_list["id"])
    rep.check("the relay list went to the relays it names and to the indexer",
              where == {RNOSTR, NRS, STRFRY}, f"on {names(where)}")
    rep.event("relay list of a created account", relay_list, expected=[RNOSTR, NRS, STRFRY],
              present=nd.presence(relay_list["id"]))
if rep.check("the profile (kind 0) was published after it", profile_ev is not None):
    nd.review_event(rep, "new account profile", profile_ev, kind=0)
    content = json.loads(profile_ev["content"])
    rep.check("kind 0 content is a JSON object with name and display_name",
              content == {"name": "Alice Created", "display_name": "Alice Created"},
              profile_ev["content"])
    rep.check("the profile is dated after the relay list",
              relay_list is not None and profile_ev["created_at"] >= relay_list["created_at"])
    where = present_on(profile_ev["id"])
    rep.check("the profile went to the account's outbox and the indexer",
              where == {RNOSTR, NRS, STRFRY}, f"on {names(where)}")
    rep.event("profile of a created account", profile_ev, expected=[RNOSTR, NRS, STRFRY],
              present=nd.presence(profile_ev["id"]))

# --------------------------------------------------------------------------- #
rep.section("Local signing and NIP-44, checked against nak")

signer = stack.signer_for(alice)
unsigned = build_note("signed locally", alice_pk)
signed = stack.sign(alice, unsigned)
rep.check("the local signer signs as the account; nak verifies it",
          signed["pubkey"] == alice_pk and nd.nak_verify(signed))
box = {}
signer.nip44_encrypt_self("secret draft ✓", lambda c: box.update(ct=c), lambda e: box.update(err=e))
nd.wait_until(lambda: box)
ct = box.get("ct", "")
nak_pt = nd.nak("decrypt", "--sec", nak_sk_hex, "-p", alice_pk, ct).stdout.strip()
rep.check("nak decrypts what the app NIP-44 encrypted to itself", nak_pt == "secret draft ✓", nak_pt)
nak_ct = nd.nak("encrypt", "--sec", nak_sk_hex, "-p", alice_pk, "from nak").stdout.strip()
box = {}
signer.nip44_decrypt_self(nak_ct, lambda p: box.update(pt=p), lambda e: box.update(err=e))
nd.wait_until(lambda: box)
rep.check("the app decrypts what nak NIP-44 encrypted", box.get("pt") == "from nak", str(box))
enc_by_nak = nd.nak("key", "encrypt", nak_sk_hex, "another password").stdout.strip()
try:
    rep.check("the app opens an ncryptsec made by nak (NIP-49 interop)",
              nip49.decrypt(enc_by_nak, "another password").hex() == nak_sk_hex)
except Exception as exc:  # noqa: BLE001
    rep.check("the app opens an ncryptsec made by nak (NIP-49 interop)", False, repr(exc))

# --------------------------------------------------------------------------- #
rep.section("Back Up Account (a second file, without a password this time)")

plain_path = nd.HOME / "Documents" / "alice-plain.txt"
bwin = controller.backup_account()
rep.check("Back Up Account opens for an account kept here", bwin is not None)
if bwin is not None:
    bwin.backup_form._choose_path = lambda _w, _pk: str(plain_path)
    nd.ALERT_POLICY = lambda alert: True if "without a password" in alert["title"] \
        else nd.default_alert_policy(alert)
    backed = nd.Catch(bwin.backup_form.backed_up)
    bwin.backup_form.save_plain()
    backed.wait(15_000)
    nd.ALERT_POLICY = nd.default_alert_policy
    plain = plain_path.read_text(encoding="utf-8") if plain_path.exists() else ""
    rep.check("the unprotected backup asks first, then holds the nsec",
              any("without a password" in a["title"] for a in nd.ALERTS_SEEN)
              and bech32.encode_nsec(nak_sk_hex) in plain)
    found = find_key(plain)
    rep.check("find_key reads it back as an unprotected key",
              not found.protected and found.secret.hex() == nak_sk_hex)

# --------------------------------------------------------------------------- #
rep.section("Restore Account on a second device (reads, never writes)")

before = {k: (latest([RNOSTR, NRS, STRFRY], k, alice_pk) or {}).get("id") for k in (0, 10002)}
dev_b = nd.Stack("b")
ctrl_b = AccountController(relay_pool=dev_b.pool, directory=dev_b.directory, vault=dev_b.vault,
                           session_pool=dev_b.sessions, store=dev_b.store, window=host,
                           is_dark=lambda: False)
ctrl_b.activate.connect(lambda p: dev_b.store.set_default(p.user_pubkey))
changed_b = nd.Catch(ctrl_b.profile_changed)
rwin = ctrl_b.restore_account()
rwin._open_file = lambda: text
rwin.buttons["choose"].click()
rep.check("opening the backup file asks for its password", rwin.page == "password", rwin.page)
rwin._backup_password.setText("wrong password")
rwin.buttons["restore"].click()
nd.wait_until(lambda: rwin._password_error.isVisible() or rwin.page != "password", 30_000)
rep.check("a wrong password is refused and says so",
          rwin.page == "password" and rwin._password_error.text() != "",
          rwin._password_error.text())
rwin._backup_password.setText(PASSWORD)
rwin.buttons["restore"].click()
nd.wait_until(lambda: "done" in rwin.buttons, 40_000)
restored = dev_b.store.get(alice_pk)
rep.check("the account is restored on the second device with its key",
          restored is not None and restored.is_local and dev_b.vault.has(alice_pk))
nd.wait_until(lambda: changed_b.fired, 10_000)
rep.check("its name is read from the network (kind 0)",
          restored is not None and dev_b.store.get(alice_pk).display_name == "Alice Created",
          dev_b.store.get(alice_pk).display_name if restored else "")
after = {k: (latest([RNOSTR, NRS, STRFRY], k, alice_pk) or {}).get("id") for k in (0, 10002)}
rep.check("restoring published nothing: relay list and profile are the same events",
          before == after, f"{before} -> {after}")
rl_b = dev_b.directory.cached(alice_pk)
rep.check("the second device routes with the account's own relay list",
          rl_b.found and set(rl_b.write) == {RNOSTR, NRS}, str(rl_b.write))

# --------------------------------------------------------------------------- #
rep.section("Two accounts: switching, Finish Later, signing out")

real_targets = policy.relay_list_targets
policy.relay_list_targets = lambda new_event, old: ["ws://localhost:7798"]
bea, bea_sk, outcome = nd.local_account(stack, "Bea Later")
policy.relay_list_targets = real_targets
rep.check("an account whose setup could not finish stays marked setup_pending",
          outcome.get("ok") is False and stack.store.get(bea.user_pubkey).setup_pending)
stack.store.set_default(alice_pk)
job_box = {}
job = PublishJob(relay_pool=stack.pool, relay_directory=stack.directory,
                 session_pool=stack.sessions, profile=stack.store.default(),
                 unsigned_event=build_note("Switch test: Alice", alice_pk))
job.completed.connect(lambda r: job_box.update(r=r))
job.signed.connect(lambda i: job_box.update(id=i))
job.start()
nd.wait_until(lambda: "r" in job_box, 20_000)
ev = nd.nak_req([RNOSTR], ids=[job_box.get("id", "")])
rep.check("with Alice active, a note is signed by Alice",
          bool(ev) and ev[0]["pubkey"] == alice_pk)
stack.store.set_default(bea.user_pubkey)
controller.profile_activated(stack.store.default())      # switching to it, as the chip does
nd.wait_until(lambda: not stack.store.get(bea.user_pubkey).setup_pending, 30_000)
rep.check("switching to the pending account finishes its setup without asking (Finish Later)",
          not stack.store.get(bea.user_pubkey).setup_pending,
          statuses.last[0] if statuses.fired else "")
bea_rl = latest([RNOSTR, NRS, STRFRY], 10002, bea.user_pubkey)
bea_p = latest([RNOSTR, NRS, STRFRY], 0, bea.user_pubkey)
rep.check("Finish Later published the relay list and the profile",
          bea_rl is not None and bea_p is not None
          and json.loads(bea_p["content"]).get("name") == "Bea Later")
if bea_rl and bea_p:
    nd.review_event(rep, "finished-later relay list", bea_rl, kind=10002)
    nd.review_event(rep, "finished-later profile", bea_p, kind=0)
    rep.event("relay list, finished later", bea_rl, present=nd.presence(bea_rl["id"]),
              expected=[RNOSTR, NRS, STRFRY])
job_box = {}
job = PublishJob(relay_pool=stack.pool, relay_directory=stack.directory,
                 session_pool=stack.sessions, profile=stack.store.default(),
                 unsigned_event=build_note("Switch test: Bea", bea.user_pubkey))
job.completed.connect(lambda r: job_box.update(r=r))
job.signed.connect(lambda i: job_box.update(id=i))
job.start()
nd.wait_until(lambda: "r" in job_box, 20_000)
ev = nd.nak_req([RNOSTR], ids=[job_box.get("id", "")])
rep.check("after switching, a note is signed by Bea", bool(ev) and ev[0]["pubkey"] == bea.user_pubkey)

rep.check("sign-out defaults to Cancel: nothing is removed",
          controller.sign_out(stack.store.get(bea.user_pubkey)) is False
          and stack.vault.has(bea.user_pubkey))
nd.ALERT_POLICY = lambda alert: True if alert["title"].startswith("Sign out") \
    else nd.default_alert_policy(alert)
rep.check("Sign Out and Delete Key forgets the key, the signer and the profile",
          controller.sign_out(stack.store.get(bea.user_pubkey)) is True
          and not stack.vault.has(bea.user_pubkey) and stack.store.get(bea.user_pubkey) is None)
nd.ALERT_POLICY = nd.default_alert_policy
rep.check("the other account stays and becomes the default",
          stack.store.default() is not None and stack.store.default().user_pubkey == alice_pk)
box = {}
stack.sessions.get(bea, lambda c: box.update(ok=c), lambda e: box.update(err=e))
nd.wait_until(lambda: box)
rep.check("a signed-out local account can no longer sign", "err" in box, str(box.get("err")))

# --------------------------------------------------------------------------- #
rep.section("NIP-46 remote signer: nak bunker, paired by pasting its bunker:// URI")

dana_sk, dana_hex, dana_pk = nd.new_identity()
pair_secret = uuid.uuid4().hex[:12]
bunker = nd.NakBunker(dana_hex, [NRS], secret=pair_secret)
try:
    uri = bunker.uri()
    rep.info("nak bunker URI", uri[:80] + "…")
    dialog = ConnectDialog(stack.pool, stack.store, parent=host, is_dark=False)
    connected = nd.Catch(dialog.profile_connected)
    dialog.details_toggle.setChecked(True)          # the connection address is in Details
    dialog._paste_edit.setText(uri.replace(uri.split("secret=")[-1], pair_secret)
                               if "secret=" in uri else uri + f"&secret={pair_secret}")
    dialog._paste_btn.click()
    connected.wait(60_000)
    dana = connected.last[0] if connected.fired else None
    rep.check("pasting the bunker:// URI pairs: the dialog emits the account",
              dana is not None and dana.user_pubkey == dana_pk, dialog._status.text())
    if dana is not None:
        rep.check("the paired account is saved as a remote signer with its channel key, "
                  "and no private key of the account",
                  stack.store.get(dana_pk) is not None and not dana.is_local
                  and len(dana.local_secret_hex) == 64 and dana.bunker_relays == [NRS]
                  and not stack.vault.has(dana_pk))
        stack.store.set_default(dana_pk)
        nd.ALERT_POLICY = nd.default_alert_policy      # "Publish Relay List" is the default
        controller.signer_paired(dana)
        nd.wait_until(lambda: latest([RNOSTR, NRS, STRFRY], 10002, dana_pk) is not None, 40_000)
        dana_rl = latest([RNOSTR, NRS, STRFRY], 10002, dana_pk)
        rep.check("a paired account with no relay list is offered one, and it is "
                  "signed by the remote signer",
                  any(a["title"].startswith("Tell Nostr apps where your posts are")
                      for a in nd.ALERTS_SEEN)
                  and dana_rl is not None, str([a["title"] for a in nd.ALERTS_SEEN][-3:]))
        if dana_rl:
            nd.review_event(rep, "relay list signed through NIP-46", dana_rl, kind=10002)
            rep.event("relay list via nak bunker", dana_rl, present=nd.presence(dana_rl["id"]),
                      expected=[RNOSTR, NRS, STRFRY])
        job_box = {}
        job = PublishJob(relay_pool=stack.pool, relay_directory=stack.directory,
                         session_pool=stack.sessions, profile=dana,
                         unsigned_event=build_note("Signed by nak bunker over NIP-46", dana_pk))
        job.completed.connect(lambda r: job_box.update(r=r))
        job.signed.connect(lambda i: job_box.update(id=i))
        job.failed.connect(lambda r: job_box.update(failed=r))
        job.start()
        nd.wait_until(lambda: "r" in job_box or "failed" in job_box, 60_000)
        ev = nd.nak_req([RNOSTR, NRS], ids=[job_box.get("id", "")])
        rep.check("a note signed by the remote signer is published",
                  bool(ev) and ev[0]["pubkey"] == dana_pk, str(job_box.get("failed", "")))
        if ev:
            nd.review_event(rep, "note signed over NIP-46", ev[0], kind=1)
            rep.event("note via nak bunker", ev[0], present=nd.presence(ev[0]["id"]))
        remote = stack.signer_for(dana)
        box = {}
        remote.nip44_encrypt_self("bunker roundtrip", lambda c: box.update(ct=c),
                                  lambda e: box.update(err=e))
        nd.wait_until(lambda: box, 40_000)
        box2 = {}
        if "ct" in box:
            remote.nip44_decrypt_self(box["ct"], lambda p: box2.update(pt=p),
                                      lambda e: box2.update(err=e))
            nd.wait_until(lambda: box2, 40_000)
        rep.check("NIP-44 encrypt and decrypt to self through the remote signer",
                  box2.get("pt") == "bunker roundtrip", f"{box} {box2}")
        if "ct" in box:
            check_pt = nd.nak("decrypt", "--sec", dana_hex, "-p", dana_pk, box["ct"]).stdout.strip()
            rep.check("nak decrypts the remote signer's self-encryption", check_pt == "bunker roundtrip")
        # A second device reattaches with the saved profile (ping, no new pairing).
        dev_c = nd.Stack("c")
        dev_c.store.upsert(dana)
        try:
            signed_c = dev_c.sign(dana, build_note("reattached", dana_pk))
            rep.check("a saved remote profile reattaches (ping) and signs again",
                      signed_c["pubkey"] == dana_pk and nd.nak_verify(signed_c))
        except RuntimeError as exc:
            rep.check("a saved remote profile reattaches (ping) and signs again", False, str(exc))
        dev_c.close()
finally:
    tail = "\n".join(bunker.lines[-8:])
    bunker.stop()
rep.note("nak bunker output (tail): " + tail.replace("\n", " | ")[:600])

# --------------------------------------------------------------------------- #
rep.section("NIP-46 remote signer: nostrconnect:// (the QR code) answered by nak bunker connect")

erin_sk, erin_hex, erin_pk = nd.new_identity()
bunker2 = nd.NakBunker(erin_hex, [NRS, RNOSTR], profile="ndak")
try:
    bunker2.uri()
    qr = ConnectDialog(stack.pool, stack.store, parent=host, is_dark=False)
    qr_connected = nd.Catch(qr.profile_connected)
    nd.wait_until(lambda: qr._qr_uri.startswith("nostrconnect://"), 5_000)
    nc_uri = qr._qr_uri
    from urllib.parse import parse_qs, urlsplit
    query = parse_qs(urlsplit(nc_uri).query)
    rep.check("the nostrconnect URI names the meeting relays, a secret, perms and the app",
              query.get("relay") == [NRS, RNOSTR] and len(query.get("secret", [""])[0]) >= 16
              and "sign_event:1" in query.get("perms", [""])[0] and query.get("name") == ["MyEditor"],
              nc_uri[:120] + "…")
    perms = query.get("perms", [""])[0].split(",")
    # Kinds the app asks a signer to sign elsewhere: 24242 (Blossom auth,
    # nostr/blossom/auth.py) and 30078 (feed list, nostr/imports/subscriptions.py).
    missing = [p for p in ("sign_event:24242", "sign_event:30078") if p not in perms]
    rep.check("the pairing asks for every kind the app signs (else strict signers prompt "
              "or refuse later)", not missing,
              ",".join(perms) + (f"; not requested: {', '.join(missing)}" if missing else ""))
    result = bunker2.connect(nc_uri)
    qr_connected.wait(60_000)
    rep.check("nak bunker connect answers the QR; the dialog pairs",
              qr_connected.fired and qr_connected.last[0].user_pubkey == erin_pk,
              (result.stdout + result.stderr).strip()[-300:] + " | " + qr._status.text())
    if qr_connected.fired:
        erin = qr_connected.last[0]
        try:
            signed_e = stack.sign(erin, build_note("Signed after a nostrconnect pairing", erin_pk))
            rep.check("the nostrconnect-paired signer signs", nd.nak_verify(signed_e)
                      and signed_e["pubkey"] == erin_pk)
        except RuntimeError as exc:
            rep.check("the nostrconnect-paired signer signs", False, str(exc))
finally:
    tail2 = "\n".join(bunker2.lines[-8:])
    bunker2.stop()
rep.note("nak bunker (nostrconnect) output (tail): " + tail2.replace("\n", " | ")[:600])

# --------------------------------------------------------------------------- #
rep.section("NIP-46 requests on the wire (kind 24133)")
if dana is not None and SENT_24133:
    bunker_pk = dana.bunker_pubkey
    ours = [(url, ev) for url, ev in SENT_24133 if nd.tag_values(ev, "p") == [bunker_pk]]
    methods = []
    for url, ev in ours:
        out = nd.nak("decrypt", "--sec", dana.local_secret_hex, "-p", bunker_pk, ev["content"])
        try:
            methods.append(json.loads(out.stdout).get("method"))
        except ValueError:
            methods.append(None)
    rep.check("every request is a kind 24133 with exactly one p tag (the signer) and NIP-44 "
              "content nak opens with the channel key",
              ours and all(len(ev["tags"]) == 1 and ev["tags"][0][0] == "p" for _, ev in ours)
              and None not in methods, f"{len(ours)} requests, methods {sorted(set(map(str, methods)))}")
    sample = next(ev for _, ev in ours)
    nd.review_event(rep, "NIP-46 request", sample, kind=24133)
    out = nd.nak("decrypt", "--sec", dana.local_secret_hex, "-p", bunker_pk, sample["content"])
    rpc = json.loads(out.stdout) if out.returncode == 0 else {}
    rep.check("the request is JSON-RPC: id, method, params as strings",
              isinstance(rpc.get("id"), str) and isinstance(rpc.get("method"), str)
              and all(isinstance(p, str) for p in rpc.get("params", [])), json.dumps(rpc)[:200])
    connect = next((json.loads(nd.nak("decrypt", "--sec", dana.local_secret_hex, "-p", bunker_pk,
                                      ev["content"]).stdout) for _, ev in ours), {})
    rep.info("first request", json.dumps(connect)[:300])
    rep.check("the channel key is not the account's key (the account key never leaves the signer)",
              crypto.get_public_key(bytes.fromhex(dana.local_secret_hex)).hex() != dana_pk
              and sample["pubkey"] != dana_pk)
    rep.event("NIP-46 request (24133)", sample)

# --------------------------------------------------------------------------- #
rep.section("Connect Signer App, a connection address made of a key, a relay and a secret")
gus_sk, gus_hex, gus_pk = nd.new_identity()
manual_secret = uuid.uuid4().hex[:10]
bunker3 = nd.NakBunker(gus_hex, [RNOSTR], secret=manual_secret)
try:
    signer_pk = bunker3.uri().split("bunker://", 1)[1].split("?", 1)[0]
    # The one-page dialog (P1) has no form for the parts: a signer app that
    # shows them shows them as one connection address, so the parts are
    # put together here as such an address, on a relay of its own.
    manual = ConnectDialog(stack.pool, stack.store, parent=host, is_dark=False)
    got = nd.Catch(manual.profile_connected)
    manual.details_toggle.setChecked(True)
    manual._paste_edit.setText(f"bunker://{signer_pk}?relay={RNOSTR}&secret={manual_secret}")
    rep.check("Connect enables for a connection address", manual._paste_btn.isEnabled())
    manual._paste_btn.click()
    got.wait(60_000)
    rep.check("the connection address pairs", got.fired and got.last[0].user_pubkey == gus_pk,
              manual._status.text())
finally:
    bunker3.stop()

# --------------------------------------------------------------------------- #
rep.section("Create Account with the key in a signer app (nak bunker playing Amber)")
pairings = []


def pair_with_bunker(on_profile, parent=None):
    """What AccountController._pair_signer does, without the modal exec():
    the QR dialog, answered by a nak bunker that holds the given key."""
    dialog = ConnectDialog(stack.pool, stack.store, parent=parent or host, is_dark=False,
                           persist=False, offer_alternatives=False)
    dialog.profile_connected.connect(on_profile)
    nd.wait_until(lambda: dialog._qr_uri.startswith("nostrconnect://"), 5_000)
    key = pairings.pop(0)
    signer = nd.NakBunker(key, [NRS, RNOSTR], profile=f"amber{len(pairings)}")
    try:
        signer.uri()
        signer.connect(dialog._qr_uri)
        done = nd.Catch(dialog.profile_connected, dialog.finished)
        done.wait(60_000)
        nd.spin(500)
    finally:
        holder.append(signer)


holder = []
controller._pair_signer = pair_with_bunker
nd.ALERT_POLICY = lambda alert: True if alert["title"].startswith("Continue without a backup") \
    else nd.default_alert_policy(alert)
amber_win = controller.create_account()
amber_win._name_edit.setText("Hana Amber")
amber_win.buttons["continue"].click()
amber_win.buttons["skip"].click()
rep.check("skipping the backup asks first, then moves on", amber_win.page == "choose", amber_win.page)
amber_win._choice_amber.setChecked(True)
amber_win.buttons["continue"].click()
amber_win.buttons["continue"].click()
amber_win.buttons["continue"].click()
rep.check("the Amber pages lead to the connect page", amber_win.page == "amber_connect", amber_win.page)
_, other_hex, _ = nd.new_identity()
pairings.append(other_hex)
amber_win.buttons["connect"].click()
rep.check("a signer holding a different key is refused, nothing saved",
          amber_win._connect_error.isVisible() and "different account" in amber_win._connect_error.text()
          and stack.store.get(amber_win.pubkey) is None, amber_win._connect_error.text())
for signer in holder:
    signer.stop()
holder.clear()
pairings.append(amber_win._secret.hex())
amber_win.buttons["connect"].click()
hana_pk = amber_win.pubkey
nd.wait_until(lambda: amber_win.page == "done", 90_000)
hana = stack.store.get(hana_pk)
rep.check("the signer holding the new key pairs, the account is saved as remote, no key kept here",
          hana is not None and not hana.is_local and not stack.vault.has(hana_pk),
          f"page {amber_win.page}")
rl = latest([RNOSTR, NRS, STRFRY], 10002, hana_pk)
pf = latest([RNOSTR, NRS, STRFRY], 0, hana_pk)
rep.check("its relay list and profile are published, signed by the signer app",
          rl is not None and pf is not None and json.loads(pf["content"]).get("name") == "Hana Amber",
          f"10002 {bool(rl)}, kind 0 {pf['content'] if pf else None}")
if rl and pf:
    nd.review_event(rep, "relay list of an Amber-path account", rl, kind=10002)
    nd.review_event(rep, "profile of an Amber-path account", pf, kind=0)
for signer in holder:
    signer.stop()
nd.ALERT_POLICY = nd.default_alert_policy

dev_b.close()
stack.close()
sys.exit(rep.finish())
