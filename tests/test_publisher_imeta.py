# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""NIP-92 ``imeta`` emission: what gets described, and what never does.

Two rules decide every case here. NIP-92 says each tag MUST carry a
``url`` plus at least one other field and SHOULD match a URL in the
event content. The architecture adds the harder half: only media this
app itself resolved is described, and a field nobody measured is left
out rather than guessed, because a wrong ``x`` tells every other client
to reject the blob it just downloaded.

Pure and offline: builders only, no network, relay or signer.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from nostr import CLIENT_NAME
from nostr.drafts import build_draft_wrap, build_inner_event, serialize_inner_event
from nostr.publisher import (
    PublishedMedia,
    build_article,
    build_imeta_tags,
    build_note,
)


PK = "79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"

SHA = "b1674191a88ec5cdd733e4240a81803105dc412d6c6708d53ab94fc248f4f553"
OTHER_SHA = "c" * 64

OURS = "https://cdn.example.com"
MIRROR = "https://mirror.example.com"

URL = f"{OURS}/{SHA}.png"


def full_record(**overrides) -> PublishedMedia:
    fields = dict(
        url=URL,
        sha256=SHA,
        mime="image/png",
        width=800,
        height=600,
        alt="A kitten",
        size=12345,
        servers=(OURS, MIRROR),
    )
    fields.update(overrides)
    return PublishedMedia(**fields)


def body_with(url: str) -> str:
    return f"Look at this ![A kitten]({url}) and then read on."


def imeta_of(event: dict) -> list:
    return [t for t in event["tags"] if t and t[0] == "imeta"]


# --------------------------------------------------------------------------- #
# The full record                                                             #
# --------------------------------------------------------------------------- #

def test_a_complete_record_emits_every_field_in_a_fixed_order():
    # Exact equality on purpose: the field order decides the event id, so
    # the same document must produce the same event twice running.
    assert build_imeta_tags([full_record()], body_with(URL)) == [[
        "imeta",
        f"url {URL}",
        "m image/png",
        f"x {SHA}",
        "dim 800x600",
        "alt A kitten",
        "size 12345",
        f"fallback {MIRROR}/{SHA}.png",
    ]]


def test_the_primary_origin_is_not_repeated_as_a_fallback():
    tags = build_imeta_tags([full_record()], body_with(URL))
    assert not any(entry.startswith(f"fallback {OURS}/") for entry in tags[0])


@pytest.mark.parametrize("url, mime, fallback", [
    (f"{OURS}/{SHA}.png", "image/png", f"{MIRROR}/{SHA}.png"),       # as the primary has it
    (f"{OURS}/{SHA}.JPG", "image/png", f"{MIRROR}/{SHA}.JPG"),       # its own spelling
    (f"{OURS}/{SHA}", "audio/mpeg", f"{MIRROR}/{SHA}.mp3"),          # from the type
    (f"{OURS}/{SHA}", "application/octet-stream", f"{MIRROR}/{SHA}"),  # none known
    (f"{OURS}/{SHA}.png?x=1", "image/png", f"{MIRROR}/{SHA}.png"),   # not the query
])
def test_a_fallback_keeps_the_blobs_file_extension(url, mime, fallback):
    """BUD-01: a server MUST accept /<sha256> with an optional file
    extension; the extension is what tells a reader a picture from a
    sound before it loads, so a fallback keeps the primary's."""
    tags = build_imeta_tags([full_record(url=url, mime=mime)], body_with(url))
    assert f"fallback {fallback}" in tags[0]


@pytest.mark.parametrize("content", [
    f"See https://evil.example/?u={URL} for it.",                    # inside another address
    f"See {URL}.bak, a backup.",                                     # a longer address
    f"See {URL}x and {OURS}/{SHA}.pngs.",                           # one that goes on
    f"See https://other.example/{URL}",                              # in another's path
])
def test_only_the_address_itself_in_the_text_is_described(content):
    """NIP-92: an imeta tag SHOULD match a URL in the event content; one
    that only holds this address inside a longer one is another URL."""
    assert build_imeta_tags([full_record()], content) == []


@pytest.mark.parametrize("content", [
    f"![A kitten]({URL})",
    f'![A kitten]({URL} "A title")',
    f"<{URL}>",
    f"Look: {URL}.",
    f"{URL}\nNext line",
    f"Look ({URL}), then on.",
])
def test_the_address_is_found_however_the_text_writes_it(content):
    assert len(build_imeta_tags([full_record()], content)) == 1


def test_unknown_values_are_omitted_rather_than_defaulted():
    record = PublishedMedia(url=URL, sha256=SHA)
    assert build_imeta_tags([record], body_with(URL)) == [
        ["imeta", f"url {URL}", f"x {SHA}"]
    ]


def test_a_half_measured_size_is_not_reported_as_a_dimension():
    record = full_record(width=800, height=0)
    tags = build_imeta_tags([record], body_with(URL))
    assert not any(e.startswith("dim ") for e in tags[0])


def test_an_unsniffable_mime_is_left_out_instead_of_claimed():
    # application/octet-stream is what the asset layer records when the
    # bytes did not sniff to a known image. Publishing it would claim a
    # measurement nobody made, and it would stop a reader from sniffing.
    record = full_record(mime="application/octet-stream")
    tags = build_imeta_tags([record], body_with(URL))
    assert not any(e.startswith("m ") for e in tags[0])
    assert f"x {SHA}" in tags[0]


# --------------------------------------------------------------------------- #
# Never fabricate (AD-4, I3)                                                  #
# --------------------------------------------------------------------------- #

def test_a_foreign_url_is_never_described_at_all():
    # A third-party image passes through the content untouched and gets
    # no tag, not even a url-only one. Nobody here fetched it, so every
    # way of making the tag valid would be a fabrication, and NIP-92
    # requires url plus at least one other field.
    foreign = "https://someone-else.example/photo.jpg"
    content = body_with(foreign)
    assert build_imeta_tags([PublishedMedia(url=foreign)], content) == []
    assert build_note(content, PK, media=[PublishedMedia(url=foreign)])["tags"] == [
        ["client", CLIENT_NAME]
    ]


def test_a_configured_but_unconfirmed_server_is_never_a_fallback():
    # servers holds only origins that answered with this blob. A server
    # the user configured but that never confirmed it would send readers
    # to a 404 under our signature.
    record = full_record(servers=(OURS,))
    tags = build_imeta_tags([record], body_with(URL))
    assert not any(e.startswith("fallback ") for e in tags[0])


def test_every_address_in_an_imeta_tag_keeps_its_scheme():
    # AD-11 guard from the imeta side. Two different tags are both named
    # "server": the BUD-11 kind 24242 authorization tag carries a bare
    # lowercase domain, and the BUD-03 kind 10063 list carries the full
    # URL. PublishedMedia.servers holds the BUD-03 form, and imeta
    # addresses are built from it. A future pass that "harmonizes" the
    # two would produce fallbacks like "cdn.example.com/<hash>", which no
    # reader can fetch. Making the two agree is a defect, not a cleanup.
    tags = build_imeta_tags([full_record()], body_with(URL))
    addresses = [e.split(" ", 1)[1] for e in tags[0]
                 if e.startswith(("url ", "fallback "))]
    assert addresses
    assert all(a.startswith("https://") for a in addresses)


def test_a_bare_domain_can_never_become_a_fallback():
    record = full_record(servers=("mirror.example.com",))
    tags = build_imeta_tags([record], body_with(URL))
    assert not any(e.startswith("fallback ") for e in tags[0])


def test_a_fallback_needs_a_hash_to_be_constructible():
    record = full_record(sha256="", servers=(OURS, MIRROR))
    tags = build_imeta_tags([record], body_with(URL))
    assert not any(e.startswith("fallback ") for e in tags[0])


# --------------------------------------------------------------------------- #
# Guards                                                                      #
# --------------------------------------------------------------------------- #

def test_a_url_absent_from_the_content_is_not_described():
    # The user can still delete an image in the publish dialog before
    # signing. NIP-92 tags describe URLs that are actually there.
    assert build_imeta_tags([full_record()], "no pictures here") == []


def test_one_tag_per_url_even_when_the_image_appears_twice():
    content = f"{body_with(URL)} and again ![A kitten]({URL})"
    tags = build_imeta_tags([full_record(), full_record(alt="second")], content)
    assert len(tags) == 1
    assert "alt A kitten" in tags[0]


@pytest.mark.parametrize("url", [
    "http://cdn.example.com/blob.png",          # plain http off loopback
    "file:///etc/passwd",
    "data:image/png;base64,AAAA",
    "javascript:alert(1)",
    "https://user:pw@cdn.example.com/blob.png",
    "",
])
def test_a_url_the_media_policy_refuses_is_dropped_whole(url):
    record = full_record(url=url)
    assert build_imeta_tags([record], f"body {url} body") == []


@pytest.mark.parametrize("hostile", [
    f"{OURS}/{SHA}.png\nfallback https://evil.example/x",
    f"{OURS}/{SHA}.png url https://evil.example/x",
    f"{OURS}/{SHA}.png\r\nx {OTHER_SHA}",
])
def test_a_url_carrying_whitespace_cannot_forge_an_entry(hostile):
    # imeta entries are space delimited inside a variadic tag, so a space
    # or a newline in a value would forge a field, an entry or a tag
    # inside an event the user is about to sign.
    assert build_imeta_tags([full_record(url=hostile)], f"x {hostile} y") == []


def test_a_newline_in_alt_text_is_flattened_not_signed():
    record = full_record(alt="line one\nfallback https://evil.example/x")
    tags = build_imeta_tags([record], body_with(URL))
    alt_entries = [e for e in tags[0] if e.startswith("alt ")]
    assert alt_entries == ["alt line one fallback https://evil.example/x"]
    assert not any("\n" in entry for entry in tags[0])


def test_control_characters_in_alt_become_spaces():
    tags = build_imeta_tags([full_record(alt="a\x00b\tc")], body_with(URL))
    assert "alt a b c" in tags[0]


def test_alt_text_is_clipped():
    tags = build_imeta_tags([full_record(alt="x" * 5000)], body_with(URL))
    alt = next(e for e in tags[0] if e.startswith("alt "))
    assert len(alt) == len("alt ") + 1000


def test_at_most_one_hundred_tags_are_emitted():
    records = []
    parts = []
    for i in range(120):
        url = f"{OURS}/{i:064x}.png"
        records.append(PublishedMedia(url=url, sha256=f"{i:064x}"))
        parts.append(f"![p]({url})")
    tags = build_imeta_tags(records, " ".join(parts))
    assert len(tags) == 100
    # The images themselves are untouched by the cap.
    assert all(r.url in " ".join(parts) for r in records)


def test_no_media_means_no_tags():
    assert build_imeta_tags((), "body") == []
    assert build_imeta_tags(None, "body") == []


# --------------------------------------------------------------------------- #
# Placement inside the builders                                               #
# --------------------------------------------------------------------------- #

def test_a_note_carries_imeta_after_the_client_tag_and_before_extras():
    event = build_note(
        body_with(URL), PK,
        media=[full_record()],
        extra_tags=[["t", "cats"]],
    )
    kinds = [t[0] for t in event["tags"]]
    assert kinds == ["client", "imeta", "t"]


def test_an_article_carries_imeta_after_its_metadata_and_before_extras():
    event = build_article(
        body_with(URL), PK, "my-slug",
        title="Cats",
        image="https://blog.example/cover.png",
        hashtags=["cats"],
        media=[full_record()],
        extra_tags=[["source", "https://blog.example/feed"]],
    )
    kinds = [t[0] for t in event["tags"]]
    assert kinds == ["d", "client", "title", "image", "t", "imeta", "source"]


def test_media_defaults_to_nothing_so_no_caller_changes():
    assert imeta_of(build_note(body_with(URL), PK)) == []
    assert imeta_of(build_article(body_with(URL), PK, "slug")) == []


# --------------------------------------------------------------------------- #
# NIP-37 drafts                                                               #
# --------------------------------------------------------------------------- #

def test_a_draft_keeps_imeta_on_the_inner_event_and_none_on_the_wrap():
    # Drafts start carrying imeta, which is a wire-format change for
    # anything already reading them, so the round trip is pinned.
    tags = build_imeta_tags([full_record()], body_with(URL))
    inner = build_inner_event(
        kind=1, content=body_with(URL), pubkey_hex=PK, tags=tags,
    )
    restored = json.loads(serialize_inner_event(inner))
    assert [list(t) for t in restored["tags"]] == tags

    wrap = build_draft_wrap(
        identifier="d1",
        inner_kind=1,
        encrypted_content="ciphertext",
        pubkey_hex=PK,
        client_name=CLIENT_NAME,
    )
    assert imeta_of(wrap) == []
    assert SHA not in json.dumps(wrap)


@pytest.mark.parametrize("content", [
    f"*{URL}*", f"**{URL}**", f"`{URL}`", f"| {URL} |", f"|{URL}|", f"{URL}#frag",
    f"Look at {URL}…", f"Look: {URL}", f"　{URL}　", f"_{URL}_",
])
def test_an_address_in_emphasis_code_a_table_or_after_any_space_is_found(content):
    """Review low 6: written in emphasis, in code, in a table, with a
    fragment, before an ellipsis or after a no-break or ideographic space,
    the address is still the address in the text (NIP-92)."""
    assert len(build_imeta_tags([full_record()], content)) == 1


@pytest.mark.parametrize("content", [f"{URL}_v2", f"foo_{URL}", f"see {URL}_v2 now"])
def test_an_address_joined_to_a_word_by_an_underscore_is_another_address(content):
    """Review round 2: "…a.png_v2" and "foo_…a.png" counted as a.png."""
    assert build_imeta_tags([full_record()], content) == []


def test_underscores_around_an_address_are_still_emphasis():
    assert len(build_imeta_tags([full_record()], f"see _{URL}_ now")) == 1


def test_an_article_published_again_has_one_imeta_per_picture_its_own():
    # NIP audit F12: reopened, an article carries its old imeta, and the
    # same picture comes in again as this app's own record.
    from nostr.article_details import ArticleDetails

    published = [["d", "hafen"], ["title", "Hafen"],
                 ["imeta", f"url {URL}", "m image/png", f"x {SHA}", "dim 600x800", "alt Old"],
                 ["imeta", f"url {OURS}/{OTHER_SHA}.png", "m image/png", "alt Kept"]]
    body = f"![A kitten]({URL})\n\n![Kept]({OURS}/{OTHER_SHA}.png)\n"
    details = ArticleDetails.from_tags(published, signer=PK)
    event = build_article(body, PK, "hafen", media=[full_record()],
                          extra_tags=details.published_tags(body, author=(PK, "")))
    imetas = [tag for tag in event["tags"] if tag[0] == "imeta"]
    assert [tag[1] for tag in imetas] == [f"url {URL}", f"url {OURS}/{OTHER_SHA}.png"]
    assert "dim 800x600" in imetas[0]
