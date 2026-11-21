# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read-only verification of a persisted reference snapshot.

A valid signature proves authorship, not publication. This bounded read must
observe the reference on a public relay before restored private state can
produce public provenance. Newer text is reported separately; checking never
changes the reviewed source pin or either editor's contents.

Each step of the read (the reference, its deletions, the root, the root's
deletions) ends on the relays' own ends, as the other queries do
(queries._EventsQuery): once every relay asked ended, or as soon as one
relay that sent what the step looks for has ended. A slow, closed or
unreachable relay never decides alone (quiet partial failure), and each
step has its own time; one that runs out decides with what was received.
"""
from __future__ import annotations
import re
from copy import deepcopy

from PySide6.QtCore import QObject, QTimer

from .events import verify_event
from .multilingual import article_address, parse_relations, projection_hash
from .outbox import defaults
from .outbox.policy import dedupe_relays
from .outbox.lookup import fetch_addressable


def check_linked_siblings(pool, directory, versions, owner, on_done, *, exclude='', parent=None):
    """Verify public siblings, including documents whose tabs are closed."""
    return SiblingCheck(pool, directory, versions, owner, on_done, exclude=exclude, parent=parent)


class SiblingCheck(QObject):
    def __init__(self, pool, directory, versions, owner, on_done, *, exclude='', parent=None):
        super().__init__(parent)
        self._pool, self._directory, self._owner, self._done = pool, directory, owner, on_done
        self._names = list(dict.fromkeys(d for d in versions.values() if isinstance(d, str) and d and d != exclude))
        self._queue, self._proofs, self._active = [], [], 0
        self._finished = False
        self.destroyed.connect(lambda: setattr(self, '_finished', True))
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        # Room for the lookup and each check's steps (SourceCheck.STEP_MS).
        self._timer.setInterval(40000)
        self._timer.timeout.connect(lambda: self._finish(unavailable=True))
        self._timer.start()
        if not re.fullmatch('[0-9a-f]{64}', owner or '') or len(self._names) > 64:
            QTimer.singleShot(0, self, lambda: self._finish(unavailable=True))
        elif not self._names:
            QTimer.singleShot(0, self, self._finish)
        else:
            directory.publish_plan(owner, self._plan)

    def _plan(self, plan):
        if self._finished:
            return
        self._lookup = fetch_addressable(self._pool, dedupe_relays(plan.targets, defaults.INDEXER_RELAYS),
            kind=30023, author=self._owner, d_tags=self._names, on_done=self._found, parent=self)

    def _found(self, results):
        if self._finished:
            return
        self._queue = [entry.event for entry in results.values() if entry.event is not None]
        self._pump()

    def _pump(self):
        if self._finished:
            return
        while self._queue and self._active < 4:
            event = self._queue.pop(0)
            identifier = next(tag[1] for tag in event['tags'] if tag[0] == 'd')
            source = {'address': f"30023:{event['pubkey']}:{identifier}", 'eventId': event['id'], 'content': event['content']}
            self._active += 1
            check_source_snapshot(self._pool, self._directory, source, self._checked, parent=self)
        if not self._queue and not self._active:
            self._finish()

    def _checked(self, proof):
        if self._finished:
            return
        self._active -= 1
        if proof.get('available') and proof.get('confirmed_root'):
            self._proofs.append(proof)
        self._pump()

    def cancel(self):
        self._finished = True
        self._timer.stop()
        for child in self.children():
            if isinstance(child, SourceCheck):
                child.cancel()

    def _finish(self, unavailable=False):
        if self._finished:
            return
        self.cancel()
        events = [] if unavailable else [proof['current'] for proof in self._proofs]
        addresses = {SourceCheck._address_of(event): event for event in events}
        established = set()
        for proof in self._proofs if not unavailable else ():
            event = proof['current']
            relation = parse_relations(event)
            if relation.get('root') or proof['confirmed_root'] != SourceCheck._address_of(event):
                # Incoming signed backlinks can prove a root that has no local
                # draft/tab and therefore is absent from this candidate set.
                established.add(proof['confirmed_root'])
            elif any(address in addresses and addresses[address]['pubkey'] == event['pubkey']
                     for address in relation.get('translations', [])):
                established.add(proof['confirmed_root'])
        conflict = len(established) > 1
        root = next(iter(established)) if len(established) == 1 else ''
        if not root and not conflict and len(addresses) == 1:
            root = next(iter(addresses))
        # Several never-grouped public singletons may be promoted by the new
        # original's signed backlinks without rewriting any successful sibling.
        translations = sorted(addresses) if not root and not conflict else []
        self._done({'root': root, 'events': events, 'translations': translations,
                    'conflict': conflict, 'unavailable': unavailable})


def check_source_snapshot(pool, directory, source, on_done, *, parent=None):
    return SourceCheck(pool, directory, source, on_done, parent=parent)


class SourceCheck(QObject):
    """One public reference read, with an optional direct root approval read."""

    # The time one step of the read has (see the module docstring).
    STEP_MS = 8000

    def __init__(self, pool, directory, source, on_done, *, parent=None):
        super().__init__(parent)
        self._pool, self._directory = pool, directory
        self._source, self._done = deepcopy(source), on_done
        self._subscription = None
        self._events, self._deleted = {}, []
        self._checks = 0
        self._finished = False
        self.destroyed.connect(lambda: setattr(self, '_finished', True))
        self._root_phase = False
        self._root_from_backlink = False
        self._reading_deletions = False
        self._observed_source = self._current = None
        self._deleted_seen = False
        self._address = article_address(source.get('address'))
        self._id = source.get('eventId', '')
        # Which relays sent each event, and the relays that ended this step.
        self._seen_on, self._step_ended, self._holders = {}, set(), set()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(self.STEP_MS)
        self._timer.timeout.connect(self._ended)
        self._timer.start()
        if not self._address or not isinstance(self._id, str) or not re.fullmatch('[0-9a-f]{64}', self._id):
            QTimer.singleShot(0, self, self._unavailable)
            return
        _, self._signer, self._d = self._address.split(':', 2)
        directory.publish_plan(self._signer, self._plan)

    def cancel(self):
        self._finished = True
        self._timer.stop()
        self._close_subscription()

    def _plan(self, plan):
        if self._finished:
            return
        relays = dedupe_relays(plan.targets, defaults.INDEXER_RELAYS)
        self._relays = relays
        filters = [
            {'kinds': [30023], 'authors': [self._signer], '#d': [self._d], 'limit': 4},
            {'kinds': [30023], 'ids': [self._id], 'limit': 1},
            {'kinds': [30023], 'authors': [self._signer], '#a': [self._address], 'limit': 16},
            {'kinds': [5], 'authors': [self._signer], '#a': [self._address], 'limit': 4},
            {'kinds': [5], 'authors': [self._signer], '#e': [self._id], 'limit': 4},
        ]
        self._subscribe(filters)

    def _subscribe(self, filters, holders=()):
        """One step of the read. ``holders``: the relays whose end decides
        it (those that sent what it asks about); none, the first relay
        that sends what the step looks for and ends decides."""
        self._close_subscription()
        self._step_ended, self._holders = set(), set(holders)
        self._timer.start()                      # each step has its own time
        sub = self._pool.subscribe(self._relays, filters)
        self._subscription = sub
        self.destroyed.connect(sub.close)
        self.destroyed.connect(sub.deleteLater)
        sub.relay_event.connect(self._event)
        sub.relay_eose.connect(self._relay_answered)
        sub.relay_closed.connect(self._relay_gone)
        sub.relay_failed.connect(self._relay_gone)

    def _close_subscription(self):
        sub, self._subscription = self._subscription, None
        if sub is not None:
            sub.close()
            for signal, slot in ((sub.relay_event, self._event),
                                 (sub.relay_eose, self._relay_answered),
                                 (sub.relay_closed, self._relay_gone),
                                 (sub.relay_failed, self._relay_gone)):
                try:
                    signal.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
            sub.deleteLater()

    def _relay_answered(self, url):
        if self._finished:
            return
        self._step_ended.add(url)
        if url in self._holders or self._sent_what_is_sought(url) \
                or set(self._relays) <= self._step_ended:
            self._ended()

    def _relay_gone(self, url, _reason=''):
        if self._finished:
            return
        self._step_ended.add(url)
        if set(self._relays) <= self._step_ended:
            self._ended()

    def _sent_what_is_sought(self, url):
        """Whether ``url`` sent what this step looks for: the reference
        itself, or in the root step the root (deletion steps have holders)."""
        if self._reading_deletions:
            return False
        if self._root_phase:
            return any(url in self._seen_on.get(event['id'], ()) for event in self._events.values()
                       if self._address_of(event) == self._root)
        return url in self._seen_on.get(self._id, ())

    def _event(self, url, event):
        if self._finished or not isinstance(event, dict):
            return
        event_id = event.get('id')
        if event_id in self._events or any(d.get('id') == event_id for d in self._deleted):
            self._seen_on.setdefault(event_id, set()).add(url)
            return
        self._checks += 1
        if self._checks > 64:
            self._unavailable()
            return
        if not verify_event(event):
            return
        if event.get('kind') == 5:
            self._deleted.append(event)
        elif event.get('kind') == 30023:
            self._events[event_id] = event
        else:
            return
        self._seen_on.setdefault(event_id, set()).add(url)

    @staticmethod
    def _address_of(event):
        ds = [t[1] for t in event.get('tags', []) if len(t) > 1 and t[0] == 'd']
        return f"30023:{event['pubkey']}:{ds[0]}" if len(ds) == 1 else ''

    def _was_deleted(self, event, address):
        return any(deletion['pubkey'] == event['pubkey']
                   and any(len(tag) > 1 and (tag[0] == 'e' and tag[1] == event['id']
                           or tag[0] == 'a' and tag[1] == address
                           and deletion['created_at'] >= event['created_at'])
                           for tag in deletion['tags']) for deletion in self._deleted)

    def _ended(self):
        if self._finished:
            return
        if not self._reading_deletions:
            address = self._root if self._root_phase else self._address
            identifiers = [event['id'] for event in self._events.values()
                           if self._address_of(event) == address]
            if not identifiers:
                self._unavailable()
                return
            # NIP-09 allows event-ID-only deletions. Query the IDs actually
            # observed, including a newer source or the root approval, before
            # interpreting either as available public evidence.
            self._reading_deletions = True
            signer = address.split(':', 2)[1]
            holders = set().union(*(self._seen_on.get(i, set()) for i in identifiers))
            self._subscribe([{'kinds': [5], 'authors': [signer], '#e': identifiers[:16], 'limit': 32},
                             {'kinds': [5], 'authors': [signer], '#a': [address], 'limit': 16}],
                            holders)
            return
        if self._root_phase:
            roots = [event for event in self._events.values()
                     if self._address_of(event) == self._root]
            root = min(roots, key=lambda event: (-event['created_at'], event['id'])) if roots else None
            metadata = parse_relations(root) if root else {}
            current_meta = parse_relations(self._current)
            approved = root and metadata.get('root_valid') and not metadata.get('root') and not self._was_deleted(root, self._root)
            if self._root_from_backlink:
                approved = approved and root['pubkey'] == self._current['pubkey'] and self._address in metadata.get('translations', [])
                related = not current_meta.get('root')
            else:
                approved = approved and (root['pubkey'] == self._current['pubkey']
                                         or self._address in metadata.get('translations', []))
                related = current_meta.get('root') == self._root
            self._finish(available=True, confirmed_root=self._root if approved and related else '')
            return
        event = self._events.get(self._id)
        candidates = [value for value in self._events.values() if self._address_of(value) == self._address]
        current = min(candidates, key=lambda value: (-value['created_at'], value['id'])) if candidates else None
        if (not event or not current or self._address_of(event) != self._address
                or self._was_deleted(event, self._address) or self._was_deleted(current, self._address)):
            self._deleted_seen = bool(event and (self._was_deleted(event, self._address)
                                                 or current and self._was_deleted(current, self._address)))
            self._unavailable()
            return
        if event.get('content') != self._source.get('content'):
            self._unavailable()
            return
        self._observed_source, self._current = event, current
        relation = parse_relations(current)
        if not relation['root_valid']:
            self._finish(available=True, confirmed_root='')
            return
        root = relation.get('root')
        if not root:
            incoming = set()
            for candidate in self._events.values():
                address = self._address_of(candidate)
                metadata = parse_relations(candidate)
                if (address and address != self._address and candidate['pubkey'] == self._signer
                        and metadata['root_valid'] and not metadata['root']
                        and self._address in metadata['translations']):
                    incoming.add(address)
            if len(incoming) > 1:
                self._finish(available=True, confirmed_root='')
                return
            if not incoming:
                self._finish(available=True, confirmed_root=self._address)
                return
            root = next(iter(incoming))
            self._root_from_backlink = True
        self._root = root
        self._root_phase = True
        self._reading_deletions = False
        _, signer, identifier = root.split(':', 2)
        self._subscribe([{'kinds': [30023], 'authors': [signer], '#d': [identifier], 'limit': 2},
                         {'kinds': [5], 'authors': [signer], '#a': [root], 'limit': 4}])

    def _unavailable(self):
        self._finish(available=False, confirmed_root='')

    def _finish(self, *, available, confirmed_root):
        if self._finished:
            return
        self._finished = True
        self._timer.stop()
        self._close_subscription()
        event, current = self._observed_source, self._current
        self._done({'available': available, 'event': event, 'current': current,
                    'confirmed_root': confirmed_root, 'deleted': self._deleted_seen,
                    'changed': bool(available and event and current and projection_hash(event) != projection_hash(current))})
