# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins NIP-65 in MyEditor: the rules, the lookups, the directory, the writer.

The invariants, each pinned below:

  I1  A replaceable write needs a fresh FOUND base, or ABSENT when creating,
      or a key minted here. UNKNOWN always refuses; nothing is signed.
  I2  Only validly signed events of the right kind and author count; newest
      wins, a tie goes to the lowest id.
  I3  A FOUND list is never downgraded by a timeout or an empty answer.
  I4  A replacement is timed after what it replaces.
  I5  Private records are read where they are written.
  I6  The author's write relays come first and are never pushed out.
  I7  What the signer returns must match what was asked.
  I8  One normalizer; I9 curated relays live in outbox/defaults.py only.
"""

import ast
import json
import os
import pathlib
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from nostr import bunker, crypto, events  # noqa: E402
from nostr.outbox import defaults, policy, writer  # noqa: E402
from nostr.outbox.directory import RelayDirectory  # noqa: E402
from nostr.outbox.lookup import classify  # noqa: E402
from nostr.outbox.policy import LookupState, RelayList  # noqa: E402
from tests.outbox_fakes import (  # noqa: E402
    ABSENT, NOW, OTHER_PK, OTHER_SK, PK, UNKNOWN, FakeClient, FakePool, FakeQuery, HandPool,
    FakeSessionPool, Profile, found, one_by_one, settle, signed,
)


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def rl(write=(), read=(), state=LookupState.FOUND):
    return RelayList(write=list(write), read=list(read), state=state)


# -- the defaults (I9) -------------------------------------------------------------------

# The relay names allowed outside defaults.py, each for a reason given
# there: NIP-46 transport, the members' relay, NostrHub's own relays.
NAMED_ELSEWHERE = {
    ("nostr", "bunker.py"): {"NIP46_RELAYS"},
    ("nostr", "einundzwanzig.py"): {"MEMBER_RELAY"},
    ("nostr", "imports", "constants.py"): {"NOSTRHUB_RELAYS"},
}
_RELAY_NAME = re.compile(r"wss?://([a-z0-9-]+(?:\.[a-z0-9-]+)+)", re.IGNORECASE)


def _relays_named_in(path, allowed_constants):
    """Relay hosts written in a module's code (not its docstrings or
    comments), outside the constants it may define."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skipped = set()
    for node in tree.body:
        targets = (node.targets if isinstance(node, ast.Assign)
                   else [node.target] if isinstance(node, ast.AnnAssign) else [])
        if any(isinstance(t, ast.Name) and t.id in allowed_constants for t in targets):
            skipped.update(id(n) for n in ast.walk(node))
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            skipped.add(id(node.value))               # a docstring or bare string
    found = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in skipped):
            found += [h for h in _RELAY_NAME.findall(node.value)
                      if not h.lower().endswith(".example")]
    return found


def test_relays_are_named_only_in_the_defaults():
    root = pathlib.Path(__file__).resolve().parent.parent
    files = sorted((root / "nostr").rglob("*.py")) + sorted(root.glob("*.py"))
    offenders = {}
    for path in files:
        parts = path.relative_to(root).parts
        if parts == ("nostr", "outbox", "defaults.py"):
            continue
        named = _relays_named_in(path, NAMED_ELSEWHERE.get(parts, set()))
        if named:
            offenders["/".join(parts)] = named
    assert offenders == {}


def test_the_curated_lists_are_clean():
    for group in (defaults.FALLBACK_RELAYS, defaults.INDEXER_RELAYS,
                  defaults.SEARCH_RELAYS, [u for u, _m in defaults.STARTER_LIST]):
        assert len(set(group)) == len(group)
        assert all(policy.normalize_relay_url(u) == u for u in group)
    assert {u for u, _m in defaults.STARTER_LIST} <= set(defaults.FALLBACK_RELAYS)
    assert 2 <= len(defaults.STARTER_LIST) <= 4          # NIP-65 asks for a small list
    # Found unreachable or paid when last qualified.
    for gone in ("nostr.oxtr.dev", "relay.nostr.band", "theforest.nostr1.com"):
        assert all(gone not in u for u in defaults.FALLBACK_RELAYS + defaults.INDEXER_RELAYS
                   + defaults.SEARCH_RELAYS)


def test_people_are_searched_on_relays_that_offer_search():
    from nostr.search import Nip50SearchClient

    pool = FakePool()
    client = Nip50SearchClient(pool, people=None)
    client.search("satoshi")
    relays, filters = pool.subscriptions[0]
    assert relays == list(defaults.SEARCH_RELAYS)
    assert filters[0]["search"] == "satoshi"
    client.cancel()


def test_pairing_relays_are_separate_from_home_relays():
    assert "wss://nostr.oxtr.dev" not in bunker.NIP46_RELAYS
    assert "sign_event:0" in bunker.DEFAULT_PERMS and "sign_event:10002" in bunker.DEFAULT_PERMS


# -- normalizing (I8) --------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("wss://Relay.Example.COM/", "wss://relay.example.com"),
    ("  wss://relay.example.com/path/ ", "wss://relay.example.com/path"),
    ("ws://localhost:7777", "ws://localhost:7777"),
    ("https://relay.example.com", None),
    ("wss://", None),
    ("wss://user@host", None),
    ("not a url", None),
    (None, None),
])
def test_relay_urls_are_normalized_one_way(raw, expected):
    assert policy.normalize_relay_url(raw) == expected


def test_dedupe_keeps_first_and_caps():
    out = policy.dedupe_relays(["wss://a.com/", "wss://B.com"], ["wss://a.com", "bad"], cap=5)
    assert out == ["wss://a.com", "wss://b.com"]


# -- reading lists (I2) ------------------------------------------------------------------

def test_markers_split_read_and_write():
    event = signed(10002, [["r", "wss://both.com"], ["r", "wss://w.com", "write"],
                           ["r", "wss://r.com", "read"], ["r", "https://bad"]])
    parsed = policy.parse_relay_list(event)
    assert parsed.write == ["wss://both.com", "wss://w.com"]
    assert parsed.read == ["wss://both.com", "wss://r.com"]
    assert parsed.found and parsed.event is event


def test_a_forged_newer_event_cannot_hide_the_real_one():
    real = signed(10002, [["r", "wss://real.com"]], created_at=NOW - 500)
    forged = dict(signed(10002, [["r", "wss://evil.com"]], created_at=NOW), sig="00" * 64)
    other = signed(10002, [["r", "wss://other.com"]], sk=OTHER_SK, created_at=NOW)
    assert policy.newest_valid([forged, other, real], kind=10002, author=PK) is real


def test_a_tie_goes_to_the_lowest_id():
    a = signed(10002, [["r", "wss://a.com"]], created_at=NOW)
    b = signed(10002, [["r", "wss://b.com"]], created_at=NOW)
    low = min(a, b, key=lambda e: e["id"])
    assert policy.newest_valid([a, b], kind=10002, author=PK) is low
    assert policy.newest_valid([b, a], kind=10002, author=PK) is low


def test_absent_needs_a_quorum_including_an_indexer():
    assert classify(None, ["wss://purplepag.es", "wss://nos.lol"]) is LookupState.ABSENT
    assert classify(None, ["wss://nos.lol", "wss://relay.damus.io"]) is LookupState.UNKNOWN
    assert classify(None, ["wss://purplepag.es"]) is LookupState.UNKNOWN
    assert classify({"id": "x"}, []) is LookupState.FOUND


# -- changing lists and profiles (I4) ----------------------------------------------------

def test_adding_keeps_everything_and_refuses_from_nothing():
    base = signed(10002, [["r", "wss://a.com"], ["r", "wss://b.com", "read"], ["x", "keep"]])
    tags = policy.relay_list_tags_adding(base, "wss://NEW.com/")
    assert tags == [["r", "wss://a.com"], ["r", "wss://b.com", "read"], ["x", "keep"],
                    ["r", "wss://new.com", "write"]]
    assert policy.relay_list_tags_adding(None, "wss://new.com") is None
    assert policy.relay_list_tags_adding(base, "wss://b.com/") is None   # read-only stays


def test_the_starter_list_has_no_markers_and_no_repeats():
    tags = policy.starter_relay_list_tags(extra_write=["wss://nos.lol", "wss://m.com"])
    assert tags[:3] == [["r", u] for u, _m in defaults.STARTER_LIST]
    assert tags[3:] == [["r", "wss://m.com", "write"]]


def test_a_profile_change_keeps_fields_it_does_not_touch():
    base = json.dumps({"name": "old", "lud16": "me@wallet.com", "website": "https://x"})
    merged = json.loads(policy.merge_profile_content(base, {"name": "new", "website": ""}))
    assert merged == {"name": "new", "lud16": "me@wallet.com"}
    with pytest.raises(ValueError):
        policy.merge_profile_content("[1, 2]", {"name": "x"})


def test_a_replacement_is_always_after_the_original():
    assert policy.replacement_created_at({"created_at": 500}, now=100) == 501
    assert policy.replacement_created_at(None, now=100) == 100


# -- routing (I5, I6) ----------------------------------------------------------------------

def test_the_authors_write_relays_lead_and_are_never_pushed_out():
    author = rl(write=[f"wss://w{i}.com" for i in range(6)])
    plan = policy.plan_publish(author, entitled=["wss://nostr.einundzwanzig.space"])
    assert list(plan.author[:6]) == [f"wss://w{i}.com" for i in range(6)]
    assert plan.author[6] == "wss://nostr.einundzwanzig.space"


def test_too_few_write_relays_are_topped_up():
    plan = policy.plan_publish(rl(write=["wss://only.com"]))
    assert plan.author[0] == "wss://only.com" and len(plan.author) >= 2
    unknown = policy.plan_publish(RelayList())
    assert list(unknown.author) == list(defaults.FALLBACK_RELAYS[:3])


def test_mentions_reach_their_inboxes():
    alice, bob, carol = "a" * 64, "b" * 64, "c" * 64
    plan = policy.plan_publish(
        rl(write=["wss://me.com", "wss://me2.com"]),
        mentioned={alice: rl(read=["wss://alice1.com", "wss://alice2.com", "wss://alice3.com"]),
                   bob: RelayList(),
                   carol: rl(read=["wss://me.com"])},
        hints={bob: "wss://bob-hint.com"})
    assert list(plan.inbox) == ["wss://alice1.com", "wss://bob-hint.com", "wss://alice2.com"]
    assert plan.targets[:2] == ["wss://me.com", "wss://me2.com"]


def test_inbox_slots_go_round_so_every_mention_gets_one():
    people = [f"{i:x}" * 64 for i in range(8)]
    mentioned = {p: rl(read=[f"wss://{p[:4]}-1.com", f"wss://{p[:4]}-2.com"]) for p in people}
    plan = policy.plan_publish(rl(write=["wss://me.com", "wss://me2.com"]), mentioned=mentioned)
    assert len(plan.inbox) == defaults.INBOX_TOTAL_CAP
    firsts = [f"wss://{p[:4]}-1.com" for p in people]
    assert list(plan.inbox[:8]) == firsts                   # everyone's first relay
    assert list(plan.inbox[8:]) == [f"wss://{p[:4]}-2.com" for p in people[:2]]


def test_private_records_are_read_where_they_are_written():
    author = rl(write=["wss://w.com"], read=["wss://r.com"])
    relays = policy.private_relays(author, entitled=["wss://e.com"], legacy=["wss://old.com"])
    assert relays == ["wss://w.com", "wss://r.com", "wss://e.com", "wss://old.com"]
    assert policy.private_relays(RelayList())[:5] == list(defaults.FALLBACK_RELAYS)


def test_reading_someone_uses_their_outbox():
    assert policy.outbox_relays(rl(write=["wss://theirs.com"]), hints=["wss://h.com"]) == \
        ["wss://h.com", "wss://theirs.com"]
    assert policy.outbox_relays(RelayList()) == list(defaults.FALLBACK_RELAYS)


def test_a_new_relay_list_also_replaces_the_old_copies():
    old = rl(write=["wss://old.com"])
    new = signed(10002, [["r", "wss://new.com"]])
    targets = policy.relay_list_targets(new, old)
    assert targets[:2] == ["wss://new.com", "wss://old.com"]
    assert set(defaults.INDEXER_RELAYS) <= set(targets)


# -- the directory (I3) ----------------------------------------------------------------

class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def directory(query, *, tmp_path=None, own=(), clock=None):
    many = query.many if isinstance(query, FakeQuery) else one_by_one(query)
    return RelayDirectory(FakePool(), query=query, query_many=many,
                          own_pubkeys=lambda: own,
                          store_path=(tmp_path / "lists.json") if tmp_path else None,
                          clock=clock or Clock())


def test_a_lookup_is_cached_and_coalesced():
    event = signed(10002, [["r", "wss://mine.com"]])
    query = FakeQuery({10002: found(event)})
    d = directory(query)
    got = []
    d.lookup(PK, got.append)
    d.lookup(PK, got.append)
    settle()
    assert len(query.calls) == 1 and [g.write for g in got] == [["wss://mine.com"]] * 2


def test_a_known_list_is_never_downgraded_by_a_timeout():
    clock = Clock()
    event = signed(10002, [["r", "wss://mine.com"]])
    query = FakeQuery({10002: found(event)})
    d = directory(query, clock=clock)
    d.lookup(PK, lambda _r: None)
    settle()
    query.answers[10002] = UNKNOWN
    clock.now += defaults.TTL_FOUND_S + 1
    got = []
    d.lookup(PK, got.append)                 # stale: answered at once, refreshed behind
    settle()
    assert got[0].found and d.cached(PK).found
    assert d.cached(PK).write == ["wss://mine.com"]


def test_remember_takes_only_newer_valid_lists():
    d = directory(FakeQuery())
    old = signed(10002, [["r", "wss://old.com"]], created_at=NOW - 50)
    new = signed(10002, [["r", "wss://new.com"]], created_at=NOW)
    changed = []
    d.changed.connect(changed.append)
    assert d.remember(new)
    assert not d.remember(old)
    assert not d.remember(dict(new, sig="00" * 64, created_at=NOW + 9))
    assert d.cached(PK).write == ["wss://new.com"] and changed == [PK]


def test_own_lists_survive_a_restart_and_tampering_is_dropped(tmp_path):
    d = directory(FakeQuery(), tmp_path=tmp_path, own=(PK,))
    d.remember(signed(10002, [["r", "wss://mine.com"]]))
    again = directory(FakeQuery(), tmp_path=tmp_path, own=(PK,))
    assert again.cached(PK).write == ["wss://mine.com"]
    data = json.loads((tmp_path / "lists.json").read_text())
    data["lists"][PK]["tags"] = [["r", "wss://evil.com"]]
    (tmp_path / "lists.json").write_text(json.dumps(data))
    assert not directory(FakeQuery(), tmp_path=tmp_path, own=(PK,)).cached(PK).found


def test_other_peoples_lists_are_not_written_to_disk(tmp_path):
    d = directory(FakeQuery(), tmp_path=tmp_path, own=(PK,))
    d.remember(signed(10002, [["r", "wss://theirs.com"]], sk=OTHER_SK))
    assert not (tmp_path / "lists.json").exists()


def test_a_publish_plan_combines_author_and_mentions():
    mine = signed(10002, [["r", "wss://me.com"]])
    theirs = signed(10002, [["r", "wss://their-inbox.com", "read"]], sk=OTHER_SK)

    def query(pool, relays, *, kind, author, on_done, timeout_ms=0, parent=None):
        on_done(found(mine if author == PK else theirs))

    d = directory(query)
    plans = []
    d.publish_plan(PK, plans.append, mentioned=[(OTHER_PK, "")])
    settle()
    assert plans[0].targets[0] == "wss://me.com"
    assert "wss://their-inbox.com" in plans[0].inbox


# -- the writer (I1, I7) ----------------------------------------------------------------

def deps(query, *, client=None, pool=None, d=None):
    pool = pool or FakePool()
    return {"pool": pool, "directory": d or directory(FakeQuery()),
            "session_pool": FakeSessionPool(client or FakeClient()),
            "profile": Profile(), "query": query, "clock": lambda: NOW}


def run(writer_obj):
    outcomes = []
    writer_obj.finished.connect(outcomes.append)
    writer_obj.start()
    settle(10)
    return outcomes[0] if outcomes else None


def test_an_unreadable_list_is_never_replaced():
    client = FakeClient()
    outcome = run(writer.add_relay(url="wss://m.com", **deps(FakeQuery({10002: UNKNOWN}),
                                                             client=client)))
    assert outcome.status == writer.UNKNOWN_BASE and client.requests == []


def test_adding_to_no_list_is_refused():
    client = FakeClient()
    outcome = run(writer.add_relay(url="wss://m.com", **deps(FakeQuery({10002: ABSENT}),
                                                             client=client)))
    assert outcome.status == writer.REFUSED and client.requests == []


def test_adding_publishes_the_old_list_plus_one():
    base = signed(10002, [["r", "wss://a.com"]], created_at=NOW + 10)
    pool = FakePool()
    d = directory(FakeQuery())
    outcome = run(writer.add_relay(url="wss://m.com",
                                   **deps(FakeQuery({10002: found(base)}), pool=pool, d=d)))
    assert outcome.status == writer.WRITTEN
    assert pool.subscriptions == []                     # done once accepted: no read-back wait
    event = outcome.event
    assert event["tags"] == [["r", "wss://a.com"], ["r", "wss://m.com", "write"]]
    assert event["created_at"] == NOW + 11              # after the base, despite the clock
    targets, _published = pool.published[0]
    assert {"wss://a.com", "wss://m.com", *defaults.INDEXER_RELAYS} <= set(targets)
    assert d.cached(PK).write == ["wss://a.com", "wss://m.com"]   # seeded, no refetch


def test_creating_when_one_exists_sends_nothing():
    client = FakeClient()
    outcome = run(writer.create_relay_list(
        **deps(FakeQuery({10002: found(signed(10002, [["r", "wss://a.com"]]))}), client=client)))
    assert outcome.status == writer.EXISTS and client.requests == []


def test_a_signer_returning_something_else_is_refused():
    def tamper(event):
        return signed(10002, [["r", "wss://evil.com"]], created_at=event["created_at"])
    base = signed(10002, [["r", "wss://a.com"]])
    pool = FakePool()
    outcome = run(writer.add_relay(url="wss://m.com", **deps(
        FakeQuery({10002: found(base)}), client=FakeClient(tamper=tamper), pool=pool)))
    assert outcome.status == writer.FAILED and pool.published == []


def test_one_accepting_relay_is_not_enough_and_the_refusals_are_named():
    base = signed(10002, [["r", "wss://a.com"]])
    everyone_but_a = set(defaults.INDEXER_RELAYS) | {"wss://m.com"}
    outcome = run(writer.add_relay(url="wss://m.com", **deps(
        FakeQuery({10002: found(base)}), pool=FakePool(refuse=everyone_but_a))))
    assert outcome.status == writer.FAILED
    assert outcome.accepted == ("wss://a.com",)
    assert ("wss://m.com", "blocked") in outcome.refused
    assert "wss://m.com: blocked" in outcome.reason


def test_a_write_is_done_once_enough_relays_accept_it():
    # An acknowledgement is all a write waits for; whether the default
    # relays keep what they accept is checked when they are chosen.
    base = signed(10002, [["r", "wss://a.com"]])
    pool = FakePool(keep=False, refuse={"wss://m.com"})
    outcome = run(writer.add_relay(url="wss://m.com", **deps(
        FakeQuery({10002: found(base)}), pool=pool)))
    assert outcome.status == writer.WRITTEN and pool.subscriptions == []
    assert outcome.refused == (("wss://m.com", "blocked"),)


def test_a_base_dated_far_ahead_is_refused_before_signing():
    client = FakeClient()
    base = signed(10002, [["r", "wss://a.com"]],
                  created_at=NOW + defaults.MAX_BASE_AHEAD_S + 3 * 86_400)
    outcome = run(writer.add_relay(url="wss://m.com", **deps(
        FakeQuery({10002: found(base)}), client=client)))
    assert outcome.status == writer.FAILED and client.requests == []
    assert "clock" in outcome.reason


def test_a_base_a_little_ahead_is_followed():
    base = signed(10002, [["r", "wss://a.com"]], created_at=NOW + 3_600)
    outcome = run(writer.add_relay(url="wss://m.com", **deps(FakeQuery({10002: found(base)}))))
    assert outcome.status == writer.WRITTEN
    assert outcome.event["created_at"] == NOW + 3_601


def test_creating_sends_nothing_when_a_known_list_has_not_reached_the_relays_yet():
    client = FakeClient()
    d = directory(FakeQuery())
    d.remember(signed(10002, [["r", "wss://mine.com"]]))
    outcome = run(writer.create_relay_list(**deps(FakeQuery({10002: ABSENT}), client=client,
                                                  d=d)))
    assert outcome.status == writer.EXISTS and client.requests == []
    assert outcome.event["tags"] == [["r", "wss://mine.com"]]


def test_adding_to_a_list_the_relays_say_is_absent_is_still_refused():
    # Only creating may lean on what this app remembers; a change is
    # always made to what the relays hand back.
    client = FakeClient()
    d = directory(FakeQuery())
    d.remember(signed(10002, [["r", "wss://mine.com"]]))
    outcome = run(writer.add_relay(url="wss://m.com", **deps(FakeQuery({10002: ABSENT}),
                                                             client=client, d=d)))
    assert outcome.status == writer.REFUSED and client.requests == []


def test_a_profile_update_keeps_unknown_fields():
    base = signed(0, content=json.dumps({"name": "old", "lud16": "me@w.com"}))
    outcome = run(writer.update_profile(changes={"name": "new"},
                                        **deps(FakeQuery({0: found(base)}))))
    assert json.loads(outcome.event["content"]) == {"name": "new", "lud16": "me@w.com"}


def test_an_unchanged_profile_is_not_republished():
    base = signed(0, content=json.dumps({"name": "same"}))
    client = FakeClient()
    outcome = run(writer.update_profile(changes={"name": "same"},
                                        **deps(FakeQuery({0: found(base)}), client=client)))
    assert outcome.status == writer.UNCHANGED and client.requests == []


# -- a new account ---------------------------------------------------------------------------

def test_a_new_account_publishes_its_relay_list_then_its_profile():
    pool = FakePool()
    query = FakeQuery()
    setup = writer.AccountSetup(name="Satoshi", deps={
        "pool": pool, "directory": directory(FakeQuery()),
        "session_pool": FakeSessionPool(FakeClient()), "profile": Profile(),
        "query": query, "clock": lambda: NOW})
    steps, results = [], []
    setup.step.connect(lambda k, s, d: steps.append((k, s)))
    setup.finished.connect(lambda ok, msg: results.append(ok))
    setup.start()
    settle(20)
    assert results == [True]
    assert query.calls == []                     # a key minted here has nothing to read
    kinds = [event["kind"] for _targets, event in pool.published]
    assert kinds == [10002, 0]
    assert json.loads(pool.published[1][1]["content"]) == {"name": "Satoshi",
                                                           "display_name": "Satoshi"}
    assert ("relays", "done") in steps and ("profile", "done") in steps


class ProfileShyClient(FakeClient):
    """Signs the relay list, refuses the profile the first time."""

    def __init__(self):
        super().__init__()
        self.refused_profile = False

    def sign_event(self, unsigned, on_success, on_failure, **kw):
        if unsigned["kind"] == 0 and not self.refused_profile:
            self.refused_profile = True
            self.requests.append(dict(unsigned))
            on_failure("not now")
            return
        super().sign_event(unsigned, on_success, on_failure, **kw)


def test_a_retry_skips_the_steps_already_done():
    pool = FakePool()
    client = ProfileShyClient()
    setup = writer.AccountSetup(name="Satoshi", deps={
        "pool": pool, "directory": directory(FakeQuery()),
        "session_pool": FakeSessionPool(client), "profile": Profile(),
        "query": FakeQuery(), "clock": lambda: NOW})
    results, steps = [], []
    setup.finished.connect(lambda ok, msg: results.append(ok))
    setup.step.connect(lambda k, s, d: steps.append((k, s)))
    setup.start()
    settle(20)
    assert results == [False] and [e["kind"] for _t, e in pool.published] == [10002]
    steps.clear()
    setup.start()
    settle(20)
    assert results == [False, True]
    assert [e["kind"] for _t, e in pool.published] == [10002, 0]   # the list went out once
    assert ("relays", "active") not in steps and ("relays", "done") in steps
    setup.start()                                   # everything done: nothing more is sent
    settle(20)
    assert results == [False, True, True] and len(pool.published) == 2


def test_a_retry_resends_the_same_signed_events():
    pool = FakePool(refuse=set(policy.dedupe_relays(
        [u for u, _m in defaults.STARTER_LIST], defaults.INDEXER_RELAYS)))
    client = FakeClient()
    setup = writer.AccountSetup(name="", deps={
        "pool": pool, "directory": directory(FakeQuery()),
        "session_pool": FakeSessionPool(client), "profile": Profile(),
        "query": FakeQuery(), "clock": lambda: NOW})
    results = []
    setup.finished.connect(lambda ok, msg: results.append(ok))
    setup.start()
    settle(20)
    assert results == [False]
    pool.refuse = set()
    setup.start()
    settle(20)
    assert results == [False, True]
    assert len(client.requests) == 1             # signed once, sent twice
    assert pool.published[0][1]["id"] == pool.published[1][1]["id"]


def finish_later(query, client, pool=None):
    setup = writer.AccountSetup(name="Satoshi", fresh=False, deps={
        "pool": pool or FakePool(), "directory": directory(FakeQuery()),
        "session_pool": FakeSessionPool(client), "profile": Profile(),
        "query": query, "clock": lambda: NOW})
    results = []
    setup.finished.connect(lambda ok, msg: results.append((ok, msg)))
    setup.start()
    settle(20)
    return results


def test_finishing_a_setup_later_creates_only_what_is_missing():
    existing = signed(10002, [["r", "wss://a.com"]])
    pool, client = FakePool(), FakeClient()
    results = finish_later(FakeQuery({10002: found(existing), 0: ABSENT}), client, pool)
    assert [ok for ok, _msg in results] == [True]
    assert [request["kind"] for request in client.requests] == [0]   # the list stays
    assert json.loads(pool.published[0][1]["content"])["name"] == "Satoshi"


def test_finishing_later_never_replaces_a_profile_published_since():
    theirs = signed(0, content=json.dumps({"name": "Chosen elsewhere"}))
    client = FakeClient()
    results = finish_later(FakeQuery({10002: ABSENT, 0: found(theirs)}), client)
    assert [ok for ok, _msg in results] == [True]
    assert [request["kind"] for request in client.requests] == [10002]


def test_finishing_later_waits_when_the_network_cant_be_read():
    client = FakeClient()
    results = finish_later(FakeQuery({10002: UNKNOWN}), client)
    assert results[0][0] is False and client.requests == []
    # The key may be in a signer app: the message doesn't say where it is.
    assert "on this computer" not in results[0][1]


# -- one bad answer never jams the directory (I2, I3) ---------------------------------------

class ParkedQuery:
    """A lookup that answers only when the test says so."""

    def __init__(self):
        self.pending = []

    def __call__(self, pool, relays, *, kind, author, on_done, timeout_ms=0, parent=None):
        self.pending.append(on_done)


def test_a_copy_with_a_string_timestamp_cannot_jam_the_directory():
    real = signed(10002, [["r", "wss://mine.com"]])
    stringy = dict(real, created_at=str(real["created_at"]))
    assert events.verify_event(stringy)            # the signature still checks out
    assert policy.newest_valid([stringy], kind=10002, author=PK) is None
    assert policy.newest_valid([stringy, real], kind=10002, author=PK) is real
    clock = Clock()
    query = FakeQuery({10002: found(real)})
    d = directory(query, clock=clock)
    d.lookup(PK, lambda _r: None)
    settle()
    query.answers[10002] = found(stringy)          # as a careless query might pass it on
    got = []
    d.lookup(PK, got.append, fresh=True)
    d.lookup(PK, got.append, fresh=True)           # not stuck behind the first
    settle()
    assert [g.write for g in got] == [["wss://mine.com"]] * 2
    assert len(query.calls) == 3


def test_remember_breaks_a_tie_by_the_lowest_id():
    a = signed(10002, [["r", "wss://a.com"]], created_at=NOW)
    b = signed(10002, [["r", "wss://b.com"]], created_at=NOW)
    low, high = sorted((a, b), key=lambda e: e["id"])
    d = directory(FakeQuery())
    assert d.remember(high) and d.remember(low)
    assert not d.remember(high)
    assert d.cached(PK).event["id"] == low["id"]
    assert not d.remember(dict(low, created_at=str(NOW)))


def test_one_failing_caller_is_logged_and_the_others_still_answered(caplog):
    query = ParkedQuery()
    d = directory(query)
    got = []

    def broken(_relay_list):
        raise RuntimeError("caller bug")

    d.lookup(PK, broken)
    d.lookup(PK, got.append)
    assert len(query.pending) == 1                 # coalesced
    query.pending[0](found(signed(10002, [["r", "wss://mine.com"]])))
    assert [g.write for g in got] == [["wss://mine.com"]]
    assert "caller bug" in caplog.text


# -- many people at once -------------------------------------------------------------------

THIRD_SK = bytes.fromhex("4c" * 32)
THIRD_PK = crypto.get_public_key(THIRD_SK).hex()


def live_directory(own=()):
    """The real lookups, against relays the test answers by hand."""
    pool = HandPool()
    return pool, RelayDirectory(pool, own_pubkeys=lambda: own, store_path=None,
                                clock=Clock())


def test_everyone_a_note_mentions_is_looked_up_in_one_request():
    pool, d = live_directory()
    got = []
    d.lookup_many([OTHER_PK, THIRD_PK, PK], got.append)
    assert len(pool.subs) == 1
    sub = pool.subs[0]
    assert sub.filters[0]["kinds"] == [10002]
    assert sorted(sub.filters[0]["authors"]) == sorted([OTHER_PK, THIRD_PK, PK])
    theirs = signed(10002, [["r", "wss://their-inbox.com", "read"]], sk=OTHER_SK)
    mine = signed(10002, [["r", "wss://mine.com"]])
    sub.answer(sub.urls[0], theirs, mine)
    for url in sub.urls[1:]:
        sub.answer(url)
    settle()
    lists = got[0]
    assert lists[OTHER_PK].read == ["wss://their-inbox.com"]
    assert lists[PK].write == ["wss://mine.com"]
    assert lists[THIRD_PK].state is LookupState.ABSENT    # every relay, an indexer among them, said so


def test_a_mentions_relay_hint_is_asked_too():
    pool, d = live_directory()
    d.publish_plan(PK, lambda _plan: None,
                   mentioned=[(OTHER_PK, "wss://hint.example"), (THIRD_PK, "")])
    batch = [s for s in pool.subs if len(s.filters[0]["authors"]) == 2]
    assert len(pool.subs) == 2 and len(batch) == 1          # the author, and one for both mentions
    assert batch[0].urls[0] == "wss://hint.example"


def test_a_later_caller_with_a_new_hint_widens_the_running_lookup():
    pool, d = live_directory()
    got = []
    d.lookup(OTHER_PK, got.append)
    d.lookup(OTHER_PK, got.append, hints=["wss://where-they-are.example"])
    assert len(pool.subs) == 2
    assert pool.subs[1].urls == ["wss://where-they-are.example"]
    for url in pool.subs[0].urls:
        pool.subs[0].answer(url)
    assert got == []                                       # still waiting for the hint
    theirs = signed(10002, [["r", "wss://theirs.com"]], sk=OTHER_SK)
    pool.subs[1].answer("wss://where-they-are.example", theirs)
    settle()
    assert [g.write for g in got] == [["wss://theirs.com"]] * 2


def test_the_same_caller_again_joins_without_a_second_request():
    pool, d = live_directory()
    d.lookup(OTHER_PK, lambda _r: None, hints=["wss://h.example"])
    d.lookup(OTHER_PK, lambda _r: None, hints=["wss://h.example"])
    assert len(pool.subs) == 1


def test_mentioning_yourself_reaches_your_own_inbox_in_one_lookup():
    pool, d = live_directory(own=(PK,))
    plans = []
    d.publish_plan(PK, plans.append, mentioned=[(PK, "")])
    assert len(pool.subs) == 1                              # the mention joined the author's lookup
    mine = signed(10002, [["r", "wss://out.com", "write"], ["r", "wss://in.com", "read"]])
    sub = pool.subs[0]
    for url in sub.urls:
        sub.answer(url, mine)
    settle()
    assert plans[0].author[0] == "wss://out.com"
    assert "wss://in.com" in plans[0].inbox


# -- relays named by other people --------------------------------------------------------

@pytest.mark.parametrize("url,public", [
    ("wss://relay.damus.io", True),
    ("wss://relay.example.com:7447/path", True),
    ("wss://8.8.8.8", True),
    ("ws://relay.damus.io", False),               # plain text, rewritable on the way
    ("wss://localhost:7777", False),
    ("wss://127.0.0.1", False),
    ("wss://10.0.0.5:7777", False),
    ("wss://192.168.1.1", False),
    ("wss://169.254.169.254", False),
    ("wss://100.64.0.1", False),
    ("wss://[::1]", False),
    ("wss://[fe80::1]", False),
    ("wss://[::ffff:127.0.0.1]", False),
    ("wss://224.0.0.1", False),
    ("wss://printer.local", False),
    ("wss://router.lan", False),
    ("wss://abcdefghijklmnop.onion", False),
    ("wss://nas", False),                          # a single label: someone's own network
    ("wss://127.1", False),                        # an address in disguise
    ("wss://0x7f.1", False),
    ("https://relay.damus.io", False),
])
def test_only_public_relays_named_by_others_are_used(url, public):
    assert policy.is_public_relay(url) is public


def test_a_mentions_private_relays_and_hints_are_not_used():
    alice, bob = "a" * 64, "b" * 64
    plan = policy.plan_publish(
        rl(write=["wss://me.com", "wss://me2.com"]),
        mentioned={alice: rl(read=["wss://192.168.1.10", "ws://alice.com", "wss://alice.com"]),
                   bob: RelayList()},
        hints={bob: "wss://localhost:4848"})
    assert list(plan.inbox) == ["wss://alice.com"]


def test_your_own_relays_are_yours_to_choose():
    me = "c" * 64
    mine = rl(write=["ws://127.0.0.1:7777"], read=["ws://127.0.0.1:7777"])
    plan = policy.plan_publish(rl(write=["wss://me.com", "wss://me2.com"]),
                               mentioned={me: mine}, own=[me])
    assert list(plan.inbox) == ["ws://127.0.0.1:7777"]
    assert policy.outbox_relays(mine, own=True) == ["ws://127.0.0.1:7777"]
    assert policy.lookup_relays(known=mine, own=True)[0] == "ws://127.0.0.1:7777"
    assert policy.private_relays(mine)[0] == "ws://127.0.0.1:7777"


def test_someone_elses_private_relays_are_not_read_from():
    theirs = rl(write=["wss://10.0.0.2", "wss://router.lan"])
    assert policy.outbox_relays(theirs, hints=["ws://hint.com", "wss://hint.com"]) == \
        ["wss://hint.com", *defaults.FALLBACK_RELAYS]
    assert not set(policy.lookup_relays(known=theirs)) & {"wss://10.0.0.2", "wss://router.lan"}


def test_a_lookup_caps_hints_and_write_relays_and_always_asks_an_indexer():
    hints = [f"wss://hint{i}.com" for i in range(5)]
    known = rl(write=[f"wss://w{i}.com" for i in range(9)])
    relays = policy.lookup_relays(hints=hints, known=known)
    assert len(relays) == defaults.LOOKUP_CAP
    assert relays[:defaults.HINT_CAP] == hints[:defaults.HINT_CAP]
    assert sum(r.startswith("wss://w") for r in relays) <= defaults.WRITE_CAP
    assert relays[-1] == defaults.INDEXER_RELAYS[0]


def test_default_ports_are_dropped_and_a_host_is_required():
    from nostr import relay
    assert policy.normalize_relay_url("wss://Relay.Example.com:443/") == "wss://relay.example.com"
    assert policy.normalize_relay_url("ws://relay.example.com:80") == "ws://relay.example.com"
    assert policy.normalize_relay_url("wss://relay.example.com:80") == "wss://relay.example.com:80"
    assert policy.normalize_relay_url("WSS://[::1]:443/x") == "wss://[::1]/x"
    for bad in ("wss://:443", "wss://host:abc", "wss://host?x=1", "wss://[::1"):
        assert policy.normalize_relay_url(bad) is None
    # The connection a relay gets is spelt as the routing spells it.
    assert relay._normalize("wss://Relay.Example.com:443/") == "wss://relay.example.com"


def test_a_mention_with_a_private_relay_list_in_the_directory():
    pool, d = live_directory()
    plans = []
    d.publish_plan(PK, plans.append, mentioned=[(OTHER_PK, "ws://192.168.0.2")])
    batch = [s for s in pool.subs if OTHER_PK in s.filters[0]["authors"]][0]
    assert "ws://192.168.0.2" not in batch.urls


# -- what the directory hands out and keeps ------------------------------------------------

def test_a_caller_changing_its_answer_changes_nothing_here():
    d = directory(FakeQuery())
    d.remember(signed(10002, [["r", "wss://mine.com"]]))
    handed = d.cached(PK)
    handed.write.append("wss://evil.com")
    handed.event["tags"].append(["r", "wss://evil.com"])
    got = []
    d.lookup(PK, got.append)
    settle()
    got[0].write.clear()
    assert d.cached(PK).write == ["wss://mine.com"]
    assert d.cached(PK).event["tags"] == [["r", "wss://mine.com"]]


def test_other_peoples_lists_are_capped_least_recently_used_first(monkeypatch):
    monkeypatch.setattr(defaults, "DIRECTORY_CAP", 3)
    keys = [bytes([i]) * 32 for i in range(10, 15)]
    d = directory(FakeQuery(), own=(PK,))
    d.remember(signed(10002, [["r", "wss://mine.com"]]))
    pubkeys = []
    for sk in keys:
        event = signed(10002, [["r", "wss://theirs.com"]], sk=sk)
        pubkeys.append(event["pubkey"])
        d.remember(event)
        if len(pubkeys) == 3:
            d.lookup(pubkeys[0], lambda _r: None)        # used again: kept longer
    known = [p for p in pubkeys if d.cached(p).found]
    assert known == [pubkeys[0], *pubkeys[3:]]
    assert d.cached(PK).found                              # the user's own are never dropped


def test_ages_are_measured_on_a_monotonic_clock(tmp_path):
    import time
    assert RelayDirectory(FakePool(), store_path=None)._clock is time.monotonic
    saved = directory(FakeQuery(), tmp_path=tmp_path, own=(PK,))
    saved.remember(signed(10002, [["r", "wss://mine.com"]]))
    # Just after a boot the monotonic clock is small; a list loaded from
    # disk is still due for a refresh, never mistaken for a fresh one.
    query = FakeQuery()
    again = directory(query, tmp_path=tmp_path, own=(PK,), clock=lambda: 5.0)
    got = []
    again.lookup(PK, got.append)
    settle()
    assert got[0].write == ["wss://mine.com"] and len(query.calls) == 1


def test_a_failed_save_leaves_no_temporary_file(tmp_path, monkeypatch, caplog):
    import os as os_module
    d = directory(FakeQuery(), tmp_path=tmp_path, own=(PK,))

    def refuse(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(os_module, "replace", refuse)
    d.remember(signed(10002, [["r", "wss://mine.com"]]))
    assert list(tmp_path.iterdir()) == []
    assert "disk full" in caplog.text
    assert d.cached(PK).found                              # still known for this session


def test_the_rules_load_without_qt():
    import subprocess
    code = ("import sys; import nostr.outbox.policy, nostr.outbox.defaults; "
            "from nostr.outbox import plan_publish, private_relays; "
            "sys.exit(any(m.startswith('PySide6') for m in sys.modules))")
    root = pathlib.Path(__file__).resolve().parent.parent
    assert subprocess.run([sys.executable, "-c", code], cwd=root).returncode == 0


def test_a_relay_list_is_shared_with_a_relay_once():
    pool = FakePool(refuse={"wss://refusing.com"})
    d = RelayDirectory(pool, store_path=None, clock=Clock())
    d.remember(signed(10002, [["r", "wss://mine.com"]]))
    d.share_relay_list(PK, ["wss://inbox.com", "wss://refusing.com", "wss://mine.com"])
    settle()
    d.share_relay_list(PK, ["wss://inbox.com", "wss://refusing.com"])
    settle()
    assert [targets for targets, _event in pool.published] == [
        ["wss://inbox.com", "wss://refusing.com"],
        ["wss://refusing.com"]]                            # the refusal is tried again
    d.remember(signed(10002, [["r", "wss://mine.com"], ["r", "wss://more.com"]],
                      created_at=NOW))
    d.share_relay_list(PK, ["wss://inbox.com"])            # a newer list goes out again
    assert pool.published[-1][0] == ["wss://inbox.com"]


# -- the fake directory the other tests route through -----------------------------------

def test_the_fake_directory_takes_what_the_real_one_takes():
    import inspect

    from tests.outbox_fakes import FakeRelayDirectory

    def public(cls):
        return {name: inspect.signature(member) for name, member in vars(cls).items()
                if inspect.isfunction(member) and not name.startswith("_")}

    real, fake = public(RelayDirectory), public(FakeRelayDirectory)
    assert set(real) <= set(fake)
    for name, signature in real.items():
        assert [(p.name, p.kind) for p in signature.parameters.values()] == \
            [(p.name, p.kind) for p in fake[name].parameters.values()], name


def test_the_fake_directory_answers_on_the_next_turn_as_the_real_one_does():
    from tests.outbox_fakes import FakeRelayDirectory

    real = directory(FakeQuery())
    real.remember(signed(10002, [["r", "wss://mine.com"]]))      # known: no relay asked
    for d in (FakeRelayDirectory({PK: ["wss://mine.com"]}), real):
        got = []
        d.lookup(PK, got.append)
        d.outbox_of(PK, got.append)
        assert got == []
        settle()
        assert len(got) == 2


def test_at_most_mention_lookup_cap_people_are_looked_up():
    from tests.outbox_fakes import FakeRelayDirectory

    people = [f"{i:064x}" for i in range(defaults.MENTION_LOOKUP_CAP + 5)]
    pool, d = live_directory()
    d.lookup_many(people, lambda _lists: None)
    assert len(pool.subs[0].filters[0]["authors"]) == defaults.MENTION_LOOKUP_CAP
    fake = FakeRelayDirectory()
    fake.lookup_many(people, lambda _lists: None)
    assert len(fake.asked("lookup_many")[0][1]) == defaults.MENTION_LOOKUP_CAP
