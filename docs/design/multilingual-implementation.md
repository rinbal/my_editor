# Multilingual articles: implementation and verification

The owner approved the reviewed design for implementation on 2026-11-21. The two earlier design documents are historical requirements; this file records the implementation candidate and the evidence actually obtained.

## Candidate boundaries

- MyEditor: `feat/multilingual-articles`, based on `rinbal/dev` at `a88c403a4d4ce99cfc00874d33c5940e28530787`.
- STANDUP: `feat/multilingual-articles`, based on official `main` at `ebcfc35aa69474d37e353290dd9d1bc695240a27`.
- The implementation uses isolated sibling worktrees on `feat/multilingual-articles`; the original working copies retain their previous branches and files.
- No production database, real keys, live Nostr publication, deployment, merge or release tag was used for verification.

## Maintenance boundaries

The canonical catalogue, schema and complete publication/correction vectors are pinned inside both repositories, with content hashes. Runtime code never downloads a schema or sends prose for language detection. Language identities are canonical registered codes; human names remain presentation.

Each language remains an ordinary document and an ordinary kind-30023 article with its own address. Root identity, the actual reference and its reviewed source revision are separate. A saved private link is never public-publication evidence. Public grouping uses verified signer relationships; credits and private links cannot authorize it. Unknown and conflicting evidence fails closed.

Open public lists pin a transactional projection generation, ordered reading preferences and query scope. Continuation uses the last displayed group’s first-appearance timestamp and identity after eligibility and language selection. A later group promotion, backdated new ingestion or earlier withdrawal cannot move an unrelated story past the next continuation. Bodies and current availability are not historical snapshots: withdrawn articles never render. The first-page token remains separate from the next-page boundary for browser Back restoration; an ordinary refresh adopts current preferences. Exact-version feeds and existing audio queries do not inherit the article boundary. The additional history retains public coordinates and presence, never article-body copies.

The native editor reuses its normal tabs, document model, undo and recovery. Its read-only reference cannot become the publication target. MyEditor's optional publication journal stores approved signed public events and delivery receipts, using SQLite transactions. STANDUP reuses the existing IndexedDB content-operation journal and server compare-and-set boundary. Retrying an unchanged payload reuses its signature; a successful sibling is not published again. Reading recovery state does not authorize a new signature or an unsent sibling.

Editorial format 2 makes required preservation explicit. Signed publication capabilities advertise supported formats. Existing ordinary work remains format 1; older validators reject unsupported format 2 before mutation. Corrections preserve protected authorship, payment and translator credit while permitting reviewed language/provenance changes. See STANDUP's `docs/editorial-contract.md`, section 19.

## Design acceptance

The current [Apple button guidance](https://developer.apple.com/design/human-interface-guidelines/buttons.md), [split-view guidance](https://developer.apple.com/design/human-interface-guidelines/split-views.md) and [accessibility guidance](https://developer.apple.com/design/human-interface-guidelines/accessibility.md) were consulted during implementation. Their adaptations are testable:

- A shared Languages icon sits beside Preview. There is no additional language-only row. Icons have accessible names and tooltips; menus retain full language names.
- The recent palette is small, while search reaches the complete offline catalogue, including Thai, Lao, Khmer, Japanese and registered three-letter languages. Selecting a target opens it directly, without a second Create step.
- One supplementary reference/Details area preserves space for writing. Narrow layouts return to the same editable document; focus, caret, undo and independent text survive toggles.
- The default review names the active language and its public/private scope. Publishing both is an explicit secondary choice over frozen reviewed documents.
- Discover presents only confirmed available published alternatives, through the same language icon. Language navigation preserves exact article URLs and normal Back behavior; interface locale remains separate behind the header’s globe menu.
- Web controls retain 44px targets, ordinary keyboard navigation and focus indication. Native controls use the existing Qt appearance and command system; ordinary local documents retain their controls.

## Validation status

Final results are recorded below. The first broad run exposed regressions that were fixed before the passing runs; the private-draft compatibility change then received additional focused storage checks. Test fixtures use throwaway identities and isolated storage. Mocked relay acceptance proves control flow and signature identity, not live relay availability. Offscreen widget tests do not establish VoiceOver or native-platform appearance.

### Recorded checks

- MyEditor affected integration batch: **1,221 passed**. Native authoring/recovery, model/front-matter, publishing, editorial, workspace, self-test and resource checks are included. Additional final focused runs cover subsequent source UX and private-draft changes, including **579 passing storage/private-state cases** and **36 passing final inspector/authoring cases**.
- STANDUP final full frontend suite: **7,026 passed, 30 skipped** across 517 files (516 passed, one skipped), after the final paging and privacy fixes.
- STANDUP final affected backend batch: **554 passed**, including language parsing, signed group trust, filters/search/keyset pagination, readers, audio isolation, feeds, deletion, editorial trust/instance/publication paths, doc guards and migration upgrade/downgrade.
- An actual PyInstaller macOS arm64 application was built and its **ten self-test checks passed**, including the offline catalogue. The spec now includes both Nostr JSON resources and the existing web JSON needed at app import; source runs alone had not exposed that packaging omission.
- Local signed interoperability: **eight actual MyEditor builder events** were signed with a throwaway key, ingested through STANDUP's parser/index and rebuilt identically. **Six actual STANDUP builder events** were signed and reopened through MyEditor's model, preserving their language, script identity and publication metadata. Spanish retained the English source pin inside a German-root group. These events were never sent to live relays.
- The private-draft regression executes older client code. It proves why an unknown private tag is unsafe and verifies the replacement non-tag `standup_multilingual` encrypted JSON field is discarded by the known old public builders. Public tag filtering remains defense in depth. Editorial format guards and direct private-draft compatibility are separate checks.
- Fourteen multilingual creator/reader/interface-language browser cases passed across desktop and phone emulation. Additional existing locale flows passed. The last reader run includes a twelve-row Back reconstruction, continuation and ordinary refresh adopting the new reading preference. Production web and listener builds passed.
- Compilation, native translation completeness and whitespace checks passed. Browser and Cocoa screenshot manifests record real rendered application fixtures; the native evidence record is `multilingual-native-review.md` in MyEditor.

The independent final paging review found a cross-surface audio cache leak, reproduced it with the old grouped query and confirmed that the restored exact-version audio queries isolate it. The final regression also covers an earlier withdrawal, tied timestamps, malformed boundaries, Unicode coordinates and browser Back replay.

A standalone HTML review with fourteen embedded actual captures lives at `WorkPictures/multilingual-implementation/implementation-ui-review.html` in the original MyEditor workspace. It was opened at 1440px and 390px; all images decoded and neither viewport overflowed horizontally. Screenshot manifests distinguish real Cocoa client rendering and Chromium phone emulation from unverified platforms.

The native reference stays on the right, preserving the primary editor's existing binding; it receives approximately one third of the workspace and retains a user-adjusted divider. This deliberate native-layout adaptation is recorded in the native review. Exact external links and normal browser navigation remain specific to the chosen public version.

## Activation and recovery

The STANDUP PR prepares version 1.15.0 with dated notes, migration/backfill instructions and the release check against official main. Its official main deploys automatically; opening the PR does not authorize merging or activation. MyEditor must follow its own native release process.

Before activation, apply additive database migrations `e01a21000114` and `e01a21000115` on a backup/test database and run the bounded public-evidence backfill. Deploy compatible public readers/server and web creator together; upgrade publication capabilities through their existing signed settings action. Do not automatically re-sign existing publication keys. Deploy the matching native candidate after the server path is available.

Rollback stops new multilingual/editorial mutations. Existing public versions remain ordinary articles, exact URLs remain valid and private drafts/operation journals must be retained. The projection migration can be downgraded without modifying article or signed-event bytes; an old editorial writer must continue refusing format 2 instead of rewriting it.

External-client rendering and edit preservation in Pareto, Habla, YakiHonne, Highlighter, Primal, Damus and Amethyst still require identified-client evidence before making interoperability claims. No live acceptance or platform/accessibility result is inferred from the shared fixtures.
