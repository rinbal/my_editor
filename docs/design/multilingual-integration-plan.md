# Multilingual articles: coordinated integration

2026-11-21. Planning and UI review only; no feature code, release or live publication. Product behavior is defined in [the reviewed multilingual contract](multilingual-articles-plan.md).

## Verified integration bases

| Product | Target | Verified revision | Implementation branch proposal |
| --- | --- | --- | --- |
| MyEditor | `rinbal/my_editor:dev` | `a88c403a4d4ce99cfc00874d33c5940e28530787` | `feat/multilingual-articles` from current dev |
| STANDUP, creator and public reader | `DoktorShift/standup:main` | `ebcfc35aa69474d37e353290dd9d1bc695240a27`, version 1.14.0 | `feat/multilingual-articles` in the working repository, based on current official main |

Both references were fetched and inspected. The MyEditor working branch `feat/standup-submissions` and STANDUP's `feat/redaktion-backend` are historical ancestors, respectively 354 and 1,168 commits behind these bases. Do not implement on those branches or cherry-pick their older editorial implementation over the newer one. Use isolated worktrees and recheck upstream before PR delivery. Preserve current untracked planning files.

STANDUP's current local source is also available in `standup_working-guest` at the verified main revision. Existing editorial fixture files in current MyEditor and STANDUP already match; extend that shared evidence rather than replacing it with a second fixture system.

## Release-sized outcome

Authors can write a linked version in MyEditor or STANDUP, keep the reference beside their writing, publish the active language with one normal review, and recover a failed publication. A private original can stay private. Existing submissions and corrections preserve the relationship. Readers see one appropriate card and can switch between confirmed public versions.

No machine-translation setup, group-management page, translation-health dashboard, sibling checkbox matrix or new payment settings. Grouped editorial inboxes, commissions and group-aware collections remain later work. Current collections and social/payment actions continue targeting specific article addresses.

## Dependency and delivery order

| Work package | MyEditor | STANDUP | Gate before proceeding |
| --- | --- | --- | --- |
| A — shared contract | Vendor the pinned language/group/source schema and vectors; define file/encrypted-draft provenance. | Own the canonical versioned schema beside existing language rules; run Python and JavaScript vectors. | Identical version/hash; marker precedence, private-source behavior and actual-source identity settled. |
| B — preserve and edit safely | Language editing, contributor roles, draft/file round trips, submit/revise and correction builders. | Editor state/event round trips, editorial work builders, validated publish/correction templates and capability negotiation. | New-to-new and mixed-version mutation matrix passes. A reviewed source pin survives correction. |
| C — public model and reading | Consume resolved public alternatives without adding authoring yet. | Index relations and evidence, backfill, grouped queries, SSR/dynamic cards, reader switching, feeds and SEO. | Deletion/approval/index rebuild, query/pagination and fallback checks pass; no guessed groups. |
| D — authoring in both apps | One language chooser, linked documents, reference pane and source review. | Same behavior inside the existing editor/inspector and responsive drawer. | Native/web walkthrough passes: one setup choice, fixed target, no lost work or accidental source publication. |
| E — publication and end-to-end | Durable operation record, single-version default, explicit pair action, idempotent retry. | Matching publication semantics, server conflict checks, editorial reports and public projection updates. | Both cross-app directions, private/public paths, account signing and recovery pass against the same candidate pair. |
| F — coordinated activation | Ship the validated native build after the server/reader candidate is available. | Deploy compatible server/readers and updated web creator together; activate authoring only for supported paths. | Reviewed builds, release checks, migration/recovery evidence and UI review attached. |

A precedes B. B and C can be developed together after A; D depends on B, E integrates B/C/D, and F depends on all gates. These packages are coherent commits/review stages, not six automatic production deployments. Keep the feature dormant while its supporting pieces land.

## Shared contract and mixed versions

The canonical contract covers validated language identity and fallback, root/source reference forms, translator roles, membership/approval, variant preference, singleton promotion, deletion, source freshness and deterministic event templates. Source tags must identify the English reference when Spanish is written from English, regardless of a German root. Valid complete new `a/e source` provenance takes precedence over a valid legacy root `e original` pin; contradictory or malformed multiple source declarations yield unknown provenance, never a guessed pin. Group membership is evaluated separately.

The short suggested-language list is not the supported catalogue. Bundle the full active language catalogue from the IANA registry and use canonical codes as keys, with English/localized names, native names and aliases for search. The review's snapshot has 7,923 language entries, including all 184 two-letter entries; File-Date 2026-11-18, SHA-256 `8ac4e58567593c96e9e6d66e2017dbb8d3953dbcb42fe32ecd3b3fccdfdaba7b`. Exclude deprecated codes, collections, private ranges and non-language placeholders. Thai/ไทย, Lao/ລາວ (Laos), Khmer/ខ្មែរ and Japanese/日本語 (Japan) must resolve directly. Existing/recent choices remain a small palette; search and All languages reach the full catalogue offline with bounded result pages. UI localization, speech/spelling models and content-language detection do not constrain writing choices.

The two-letter-only validation in both current codebases must be replaced as part of A/B. Keep existing ISO-639-1 labels where applicable; use the documented NIP-32 `lang`/BCP-47 convention for registered three-letter or qualified codes and preserve imported variants. Update language filters, draft/front-matter round trips, submissions, correction templates and shared vectors together. Selecting a listed language must never succeed in the chooser and then silently lose its label at Publish. No new article kind or translation service is needed.

Keep copies pinned inside both applications: no runtime HTTP fetch of a schema and no dependency on another repository being available. Shared vectors include expected parser output and complete unsigned publication/correction templates, not merely JSON validity.

**Editorial mutation boundary:** current revision v1 accepting `extra_tags` is insufficient. Old builders reconstruct work and discard unfamiliar metadata. Define a new editorial revision format for multilingual work, with required preservation semantics, and advertise accepted revision formats through verified publication capabilities. Upgraded clients must read v1 and the new format, but only mutate formats they can preserve. A destination with no verified support keeps the ordinary v1 workflow; the app retains the multilingual draft and explains the unavailable submission action locally rather than stripping its relationships.

Package A must assign the exact revision-format identifier and signed capability field in `docs/editorial-contract.md`, extend validators and shared vectors, and demonstrate that existing clients reject unsupported revisions before authoring is enabled. Do not treat an arbitrary additive JSON field as a negotiated capability. Preserve all historical encrypted/signed revisions byte for byte; new semantics produce new revisions. This is a bounded protocol design gate, not a choice exposed to authors.

Compatibility acceptance:

| Producer / consumer | Required behavior |
| --- | --- |
| New / new | Full view, revision, publication, correction, provenance and credit preservation. |
| Old ordinary work / new app | Existing v1 behavior, with no migration prompt. |
| New multilingual work / old editorial app | Existing format-version guard refuses unsupported mutation; no lossy re-save or expected-event signing. Confirm from an actual old build. |
| New app / destination without support | Ordinary submissions remain available. Multilingual work stays intact locally; the unsupported send is unavailable with a concise explanation. |
| External Nostr client | Each public version remains an ordinary article. Rendering and metadata round trips need the client spike; no claim that our capability negotiation can constrain arbitrary third-party Nostr writers. |

## Exact implementation boundaries

| Surface | Existing code to extend | Concrete change |
| --- | --- | --- |
| MyEditor model | `nostr/article_details.py`, `nostr/publisher.py` | One language value, validated provenance, role-aware people handling and stable identity through file/draft/publication. |
| MyEditor UI | `nostr/ui/article_details_panel.py`, `publish_article_dialog.py`, main-window document controller, `preview_pane.py` | Translation action beside article language in Details and the existing document menu; linked target uses actual document model; one supplementary reference pane; fixed Publish target. |
| MyEditor editorial | `nostr/standup/drafting.py`, `formats.py`, `publishing.py` | Carry supported translation data, enforce version/capability boundary and build identical initial/corrected events. |
| STANDUP creator | `src/views/dashboard/EditorView.vue`, `src/components/editor/EditorMetadata.vue`, `EditorInspector.vue`, `ArticleEditor.vue`, existing editor state/event services | Reuse Details, publisher and formatting toolbar. Put the shared Languages icon beside Preview; no additional language row, inspector tab or translation toolbar. |
| STANDUP editorial | `src/services/editorial/submission-work.js`, `publish-event.js`, revision validators; backend publication/submission boundaries | Preserve data on fresh submission and revision; validate supported provenance mutation; capability/version evidence; maintain existing writer/account authority. |
| Public ingestion/index | `backend/core/public_events.py`, `backend/db/public_events.py`, article/language models, migrations and existing crawler | Store verified relation evidence and source projection; derive group membership and available variants; reuse current ingest, deletion and edit handling. |
| Public queries | Existing Discover query/search services and feed endpoints | Apply visibility, language eligibility and text matching before group deduplication/pagination. Keep stable first-appearance ordering and version action targets. |
| Public UI | `backend/htmlgen/discover.py`, `discover_feeds.js`, `discover_reading.py`, `article_page.py`, `components.py`, `rss.py` | Update SSR and loaded batches together; one card marker, byline language menu, language-specific feeds and reciprocal SEO alternatives. |

**Correction gap:** both current correction builders preserve carried tags from the published event. A newly reviewed pin inside revised work would currently be ignored. Define a narrow, validated set of mutable translation provenance and language fields. Replace only those fields from the accepted new revision; preserve unrelated tags, identity, payment splits and protected authorship. Translator-credit changes need the same explicit editorial authorization already required for credit changes, rather than being smuggled through a pin update. Compare the complete signed-template expectation across Python and JavaScript.

**Concurrency:** current STANDUP `/api/events` has `expected_event_id` compare-and-set and must remain in the path before relay publication for publication-account events. Reuse it. Direct relay writes have no global compare-and-swap; MyEditor still rechecks and reconciles divergence. Preserve the newer main-branch deletion protections, first-appearance timestamps, published-edit behavior and confirmed zap splits.

## UI placement and behavior to review

| Product | Preserve | Proposed addition |
| --- | --- | --- |
| MyEditor | Neutral writing page; Night Violet Nostr panels; native document navigation and Nostr commands; Publish review. | Shared Languages icon beside Preview, existing Nostr menu entry, optional Details and a larger writing pane. Remove the extra language row entirely. |
| STANDUP creator | White/gray canvas, Inconsolata interface, orange publish action, back/save commands and Article/Appearance Details. | Same Preview/Languages/reference/Details icons; no language-only row. Details uses the reference slot or a narrow-screen overlay. |
| Discover | Warm neutral surfaces, Newsreader headlines, attribution and participation actions. | Languages icon plus available count on cards; Languages icon with a full-name menu by the article byline. A distinct global globe controls interface language. |
| Publication blogs | Publication's existing theme, page hierarchy and sharing actions. | Byline language menu using that blog's tokens; no Discover/MyEditor palette imposed on it. |

Target-language choice opens the document immediately. An existing own-language version opens in place. New target body is blank with “Write or paste your English version.” Existing details are inherited once without a review-settings step; title follows the translated heading, summary and credit stay optional.

Default Publish sends the active version. If the reference is private: “English will be public. German stays a draft.” Pair publication is secondary, previews both documents and names both in its final action. A successful sibling is never republished on retry. Reference selection never retargets Publish. Narrow layouts show one document/reference at a time, with “Back to English”; explicit hiding is respected on resize.

The writing-space pass keeps target body text at 16px in the review and removes repeated rows instead of shrinking typography. Existing-version navigation preserves each language's text. Details starts closed for a new version, then follows the author's choice; it never creates a third competing pane. Keep only title, preview, signing identity and scope in the normal Publish review. Optional metadata is behind Details, and the attached publication-options menu holds the deliberate pair action. Private/public scope and retry remain immediately visible.

The owner's follow-up removes the final language-only row from both editors. The shared toolbar Languages icon sits immediately beside Preview; existing Nostr access remains in MyEditor. Only the visible reference has a small inline caption. Discover replaces bare language labels/code badges with the same icon and a published-language count on cards. Menus, tooltips and accessible names retain the full human-readable choices. Reader menus contain only available published alternatives; they do not offer every authoring language.

Measured within the latest review's product frame at a 1024px browser viewport: MyEditor's body starts at 112px instead of the original 268px, and Standup's at 136px instead of 303px. The editable text width grows from 451px to 582px in MyEditor and from 430px to 582px in Standup. At 390px, the body starts 192px higher in MyEditor and 232px higher in Standup. These compare the original and latest prototypes with the same 16px body font; they are layout measurements, not production performance or user-study results. Previous standalone exports remain available for comparison.

STANDUP's current preview split starts at 1920px. Do not copy that threshold blindly or force an unreadable pair into a smaller workspace. Measure two readable columns with the actual sidebar/inspector widths; fold the inspector or show the one-pane reference fallback. Phone controls retain existing 44px touch targets. MyEditor uses its native measured layout instead of browser breakpoints.

## Data rollout and recovery

Use additive, reversible database projections and indexes for relations, source snapshots, membership and singleton aliases. The signed public events remain authoritative. Backfill only verified relations; unlabeled/unrelated historic articles remain ordinary articles. Batch work, bound candidates and monitor rebuild progress without blocking reads.

Invalidate affected group/list/reader/SEO caches on revision, language change, relationship approval/revocation, deletion and site placement. A group cannot show private, unapproved or unavailable alternatives. Preserve exact article URLs, bookmark/payment/comment targets and established root IDs. Keep active cursor snapshots consistent until refresh; avoid deduplication holes after a variant arrives.

On disable/rollback, stop new multilingual mutations and keep reading existing articles individually; do not discard metadata, drafts or operation journals. An older deployment that cannot safely handle new editorial formats must reject those mutations. Test database/application rollback compatibility before choosing the release version; a downgrade requiring an old writer to rewrite new work is not a safe rollback.

## Verification and release gates

- Shared Python/JavaScript parser and complete-template vectors; file/encrypted-draft/publication round trips; credit overlaps; private-source metadata omission; actual English source vs German root; approval and deletion.
- Four integration directions: MyEditor → Standup editorial; Standup → MyEditor editorial; direct Nostr publication → public reader; source revision/correction in either app → review in the other. Include publication-account signing.
- Native MyEditor Light/Dark, resize, reference focus/selection, Details binding, undo/caret, close/reopen and external signer. Browser 1440/768/390px plus 1920px existing split threshold, keyboard/touch, long German labels and enlarged text. Preserve ordinary local documents and single-language writing.
- Publication failure, signer refusal, unknown send, restart, concurrent source edits and identical-event retry; no new signatures or resumed sends merely on relaunch.
- Real server group/search/feed/pagination/cache/backfill checks with deletion and later singleton promotion; valid canonical/hreflang output from the shared home-address resolver. #123 remains a separate issue; do not introduce a second resolver or silently close it.
- Actual first-time walkthrough: one setup choice, one in-app Publish review, zero reader setup and one visible retry action. External-signer approvals stay explicit.
- STANDUP: relevant frontend/backend/browser suites, release metadata check and complete build, following `RELEASING.md`. Frontend and backend are one versioned release. Official main triggers deployment; no merge/deploy/tag/publication is implied by this planning task.
- MyEditor: affected model, publishing, editorial, window/document and localization suites; then required full/platform checks on the final implementation candidate. The repository's release process determines packaging/versioning at delivery time.

Reader/index support is the first deployable dependency; enable no multilingual authoring until preservation and mixed-version gates pass in both applications. Prepare one coordinated STANDUP feature release and the matching native release; determine version numbers from current releases and compatibility at delivery, rather than reserving a stale number now.

## Visual review provenance

The interactive review uses each product's current source layout and palette. Proposed controls are interactive design previews, not screenshots of implemented multilingual behavior. The Original UI view contains unmodified source screenshots for comparison.

Current MyEditor native baseline: isolated clean checkout `a88c403a`, macOS 15.6, Qt 6.11.1, Cocoa, English, 2×. `scripts/night_violet_capture.py --native --only editor-drafts publishing` completed Light/Dark captures with deterministic demo documents and fake services; no real account or publication. An earlier offscreen attempt captured the Publish sheet then aborted; it is not native evidence and is not used for the editor baseline. The successful native run records no new-feature state checks.

Standup's original editor reference is the September 16 1.2.0 inspector review, and its Discover reader reference is October 29. These are historical screenshots; current source at `ebcfc35a` supplies later inspector tabs and behavior in the proposed layout. They do not establish the current production UI or validate the new controls. Actual implementation screenshots and end-to-end acceptance remain required before release.

The interactive design preview passed its browser interaction checks: blank target creation, fixed publication target after reference focus, private-source single publication, explicitly named pair publication, partial-failure retry, reader language switching, no single-language switch, narrow reference return with retained text, and phone Publish-sheet fit. Sixteen surface/width checks at 1024/768/390/320px found no horizontal overflow; all four original images loaded and no page errors were observed. Desktop, phone, Publish and retry screenshots were visually inspected. These checks validate the review prototype only; application tests and new-feature native acceptance have not run.

The final writing-space revision was rechecked across the same sixteen layouts plus both touch editors at 390/320px. Checks also preserve the actual entered paragraphs while toggling Details/Preview and switching languages, retain the same editor element during supplementary-view changes, confirm optional publication fields stay accessible behind Details, and exercise explicit pair scope through the attached Publish menu. Touch publication fields remain 16px. Revised desktop, narrow and publication screenshots were visually inspected; original screenshot payloads are unchanged. The standalone HTML export is refreshed, with the previous export retained separately. No application runtime code changed in this pass.

The subsequent owner review removes the remaining language row, aligns the shared icon immediately after Preview in both editors, and replaces bare language buttons/badges on Discover. The updated browser walkthrough verifies native-script searches, selection and reopening of Thai, Lao, Khmer, Japanese and Hmong in both editors, canonical code identity, retained English text, absence of language-header rows, icon placement and accessible reader controls. The same sixteen layout and four touch-editor checks pass without page errors. Original screenshot references remain unchanged; this is still prototype evidence.

The task-owned HTML source keeps a small complete two-letter fallback catalogue for inline rendering. `WorkPictures/multilingual-review/export-review.py` uses the visualization renderer and embeds the full catalogue from `language-catalog.json` into the standalone review; regenerate the deliverable through that exporter so search includes all 7,923 entries. The exported file needs no language-catalogue API or translation service. This review asset is not the future canonical production schema: package A must vendor the pinned registry and shared vectors into both applications through the agreed contract.
