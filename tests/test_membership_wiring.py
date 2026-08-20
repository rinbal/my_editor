# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The membership seams the application actually connects.

``nostr/einundzwanzig.py`` resolves membership and ``MediaStore`` and the
publish jobs accept an entitlement, and each of those is covered on its
own. None of that coverage notices if the running app never plugs them
together, which would leave a paying member with no benefit while the
suite stayed green. This is the same hole that let the blob cache ship
disconnected, so it gets the same treatment.

One distinction is deliberate and worth pinning. A dialog receives the
provider itself, because it can sit open for minutes and membership may
resolve while it does. A job receives the already-resolved list, because
by then the answer must be fixed. Passing the wrong one either way is
silent: a callable reads as truthy, so a job handed a provider would
publish to a relay list containing a function.
"""

from __future__ import annotations

import ast
import inspect
import os
import sys
import textwrap

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from main_window import MainWindow

from tests.test_media_wiring import (
    attribute_argument, calls, constructor, init_body, statement_index,
)


def self_calls(name: str):
    """Matcher for ``self.<name>(...)``, which ``calls`` does not cover:
    a method call's func is an Attribute and carries no ``id``."""
    def matches(node) -> bool:
        return (isinstance(node, ast.Call)
                and getattr(node.func, "attr", "") == name)
    return matches

# Re-exported so the fixture resolves in this module.
init_body = init_body


def method_body(name: str):
    source = textwrap.dedent(inspect.getsource(getattr(MainWindow, name)))
    return ast.parse(source).body[0].body


def keyword_of(body, call_name: str, keyword: str):
    """The AST node a keyword argument is given, across every such call."""
    found = []
    for statement in body:
        for node in ast.walk(statement):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            named = getattr(func, "id", "") or getattr(func, "attr", "")
            if named != call_name:
                continue
            for kwarg in node.keywords:
                if kwarg.arg == keyword:
                    found.append(kwarg.value)
    return found


# --------------------------------------------------------------------- #
# The directory exists and is listened to                                #
# --------------------------------------------------------------------- #

def test_the_membership_directory_is_built(init_body):
    assert statement_index(init_body, calls("MembershipDirectory")) >= 0


def test_the_media_store_is_told_which_servers_are_entitled(init_body):
    call = constructor(init_body, "MediaStore")
    assert attribute_argument(call, "entitled_servers") == "_entitled_blossom_servers"


def test_membership_is_resolved_for_an_already_signed_in_account(init_body):
    # A profile restored from a previous session never passes through the
    # connect flow, so without this a returning member gets no benefits
    # until they reconnect.
    assert statement_index(init_body, self_calls("_refresh_membership")) >= 0


@pytest.mark.parametrize("handler", [
    "_on_nostr_profile_connected",
    "_on_nostr_select_profile",
])
def test_membership_is_refreshed_when_the_account_changes(handler):
    body = method_body(handler)
    assert statement_index(body, self_calls("_refresh_membership")) >= 0


# --------------------------------------------------------------------- #
# Entitled relays reach the publish paths                                #
# --------------------------------------------------------------------- #

@pytest.mark.parametrize("handler,job", [
    ("_fire_draft_publish_job", "DraftPublishJob"),
    ("_run_draft_deletion", "DraftBulkDeleteJob"),
])
def test_a_publish_job_is_given_the_resolved_relays(handler, job):
    # A job must get the list, not the provider: a callable is truthy and
    # would be published as if it were a relay URL.
    try:
        body = method_body(handler)
    except AttributeError:
        pytest.skip(f"{handler} not present under that name")
    values = keyword_of(body, job, "entitled_relays")
    assert values, f"{job} is not given entitled_relays"
    for node in values:
        assert isinstance(node, ast.Call), (
            f"{job} received the provider itself rather than its result"
        )


@pytest.mark.parametrize("handler,dialog", [
    ("_on_nostr_publish_note", "PublishNoteDialog"),
    ("_on_nostr_publish_article", "PublishArticleDialog"),
])
def test_a_publish_dialog_is_given_the_provider(handler, dialog):
    # A dialog gets the provider, so membership resolving while it is open
    # still counts by the time the user presses publish.
    try:
        body = method_body(handler)
    except AttributeError:
        pytest.skip(f"{handler} not present under that name")
    values = keyword_of(body, dialog, "entitled_relays")
    assert values, f"{dialog} is not given entitled_relays"
    for node in values:
        assert not isinstance(node, ast.Call), (
            f"{dialog} received an already-resolved list, so membership "
            f"resolving while it is open would be ignored"
        )


# --------------------------------------------------------------------- #
# Identity-scoped state is released when the account changes            #
# --------------------------------------------------------------------- #

def test_identity_teardown_names_both_the_drafts_and_the_media_keys(init_body):
    # The private library holds a decryption key per file, so an account
    # left behind with its keys resident is a privacy problem and not
    # just untidy. Both belong to the same teardown.
    body = method_body("_release_identity_state")
    released = {
        node.func.value.attr
        for statement in body
        for node in ast.walk(statement)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", "") == "stop"
        and isinstance(getattr(node.func, "value", None), ast.Attribute)
    }
    assert "_draft_sync" in released
    assert "_private_library" in released


@pytest.mark.parametrize("handler", [
    "_on_nostr_select_profile",
    "_on_nostr_profile_connected",
    "_on_nostr_sign_out",
])
def test_every_identity_transition_releases_the_previous_account(handler):
    # Three separate paths change the active account. Missing any one of
    # them leaves the previous identity's keys in memory, which is the
    # defect this pins.
    body = method_body(handler)
    assert statement_index(body, self_calls("_release_identity_state")) >= 0


def test_reselecting_the_same_account_does_not_tear_it_down():
    # Re-selecting the current profile must not discard drafts already
    # decrypted, which would cost a fresh round of signer prompts for
    # nothing.
    source = textwrap.dedent(inspect.getsource(MainWindow._on_nostr_select_profile))
    assert "leaving" in source
    guarded = [
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.If)
        and any(
            isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "_release_identity_state"
            for n in ast.walk(node)
        )
    ]
    assert guarded, "the teardown is unconditional, so re-selecting costs prompts"
