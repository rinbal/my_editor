# Multilingual native authoring: implementation and review

2026-11-21. Implementation evidence for the approved multilingual integration plan. This records local candidate behavior, not a production deployment or live relay publication.

## Native surfaces

| Surface | Implementation | Verification |
| --- | --- | --- |
| Nostr writing toolbar | Preview, Languages and reference icons in that order; no language-only row. The Nostr menu reaches the same chooser. | Native walkthrough, account transitions, Cocoa Light/Dark captures. |
| Ordinary local editor | Existing Write/Preview/Split segmented control and local document behavior preserved. Article details remain lazy when Nostr is inactive. | Preview, toolbar, front-matter and account-state regression suites. |
| Language chooser | Offline catalogue, native/localized labels, search, recent/existing choices, bounded result pages, IME-aware Return and keyboard navigation. | Thai, Lao/Laos, Khmer, Japanese and Hmong searches; native-script input; actual Khmer popup capture. |
| Linked writing | Each language is a normal document/tab with its own UUID, text, caret and undo history. Existing versions reopen their document. An unlabeled source stays unknown; the interface language never labels it implicitly. | Full first-time walkthrough, duplicate identifiers across unrelated groups, kept front-matter restrictions, source-language corrections. |
| Reference and Details | Read-only reference; primary writing remains fixed. One supplementary pane at a time. Approximately two thirds writing, one third reference; dragging is retained. Narrow windows show one document, with the reference action returning to the target. | Measured minimum widths, divider preservation, Preview-to-reference transitions, Details and narrow-mode walkthroughs. |
| Article head | Linked documents hide empty cover/summary prompts. Existing metadata and the normal local article header are preserved; Details retains optional editing. | Article-header/live-details suites and native captures. |
| Publication review | Named active-language action and explicit private/public scope. Optional metadata stays collapsed. The attached arrow reveals deliberate pair review, with both previews and a named final action. Empty cover placeholders collapse. | Private-source sentinels, exact target checks, pair review visibility, native Light/Dark captures. |
| Native button behavior | A normal push button remains the native button-box item. Its options arrow is a separate focusable child, preserving platform Cancel/Publish order and the existing Return/Escape behavior. | Existing publication suites and actual Cocoa review. |
| Persistence | Private linkage survives ordinary Markdown front matter, encrypted inner drafts, crash recovery and update workspace records. Public article bodies/tags and editorial work exclude that private record. | File/encrypted/recovery round trips and direct-publication/editorial sentinel checks. |
| Source review | Public availability is checked independently from persisted signatures or flags. The recorded source pin remains unchanged until explicit review. Quiet read-only checks run on open/restore and are throttled while writing. A foreground send joins an in-flight check. Changes to writing metadata count; link-only metadata does not. Unpublished source edits receive a contextual note only when the live projection differs from the actual published snapshot. An existing public target keeps its established root. | Shared source checks and native review/root-retention walkthroughs. |
| Interrupted pair | An accepted source updates the intended target's private recovery record only when the complete reviewed projection matches. A failed target can resume from that accepted source; no source publication is inferred from a private flag. | Accepted-source/no-target-signature fixture, recovery/encrypted restoration and public-source verification; includes an untitled source whose title comes from its heading. |
| Account changes | Linked controls respect their owner; asynchronous availability reads cannot continue an old send under another account. | Owner/group/document/source-ID guard and sign-out/sign-in walkthroughs. |

The reference remains on the right in native MyEditor. This preserves the existing primary editor binding at splitter position zero and the app's established supplementary-pane convention. The writing/reference width hierarchy follows the approved design. Widget indices are resolved explicitly when a preview pane also exists.

## Architecture and privacy boundaries

`multilingual_authoring.py` coordinates normal tabs and private provenance. `main_window.py` supplies existing editor, save, preview and publication adapters. The catalogue chooser and attached action button are small shared Qt components. Language parsing, public relation parsing, availability verification, editorial formats and the durable publication journal remain separate domain modules.

Private file provenance uses `standup_multilingual`. Encrypted inner drafts use the optional `standup_multilingual` root field. It is inserted only before the existing draft encryption path, and is never represented as an event tag. The tested older clients discard the unknown root field; their public builders cannot accidentally copy its private snapshot into public tags. A plain article's content is produced from the document body, excluding file front matter. Tests place a private-source sentinel in file provenance and verify it is absent from both direct unsigned article events and editorial work. A serialized signed source is not publication proof: restored public relationships require verified public availability. A fresh linked UUID uses the compare-before-send preflight instead of asking a second first-publication question. A journal replay records the identifier and first date of the actual reused signed event.

Publication defaults to the active version. A missing public reference leaves that route available and says only the active version will be published. An already public target retains its carried provenance rather than silently changing groups. Pair publication rejects an unreviewed source projection or unprepared local files and exposes the existing original. It never uploads or publishes a private reference as a consequence of ordinary active-version Publish.

## Apple guidance applied

Current [Buttons](https://developer.apple.com/design/human-interface-guidelines/buttons.md) guidance informs recognizable icons, concise accessible names/tooltips, a clear primary action and a separate attached secondary menu. The native app retains its existing desktop control metrics and Qt button roles; the web implementation separately verifies touch targets.

[Split views](https://developer.apple.com/design/human-interface-guidelines/split-views.md) informs an adjustable supplementary reference, preserved divider choice and a readable single-pane fallback. [Accessibility](https://developer.apple.com/design/human-interface-guidelines/accessibility.md) informs keyboard access, system typography, explicit labels and information that does not depend on color. Measured existing semantic roles: toolbar icons are 11.39:1 Light and 10.33:1 Dark; chooser text is 16.18:1 and 15.14:1; white text against the less favorable primary-gradient endpoint is 5.12:1; card secondary text is 4.66:1 and 4.74:1. These are concrete adaptations and measurements, not a certification of HIG or assistive-technology compliance.

## Evidence and limits

The latest complete affected UI/model/front-matter/draft batch passed 782 cases. A subsequent 174-case focused batch passed after the encrypted-root privacy change and quiet source checks. It includes the new-UUID single-review route, the original publication date of a replayed signed event, invalid-pair recovery, private draft serialization and localization. The integration record contains the broader final suite and shared protocol/publication checks. The final 36-case inspector/authoring check also verifies that opaque file metadata disables language changes and that an inspector refresh restores the control when that restriction is removed. Translation completeness, compilation and whitespace checks passed.

Ten actual Cocoa client captures live in the workspace's `WorkPictures/multilingual-implementation/`. `native-manifest.json` records macOS, Qt/PySide, production Fusion/AppStyle and stylesheet guard, scale, logical dimensions, source hashes and synthetic fixture provenance. Captures cover writing, Khmer search, normal and paired publication in Light/Dark, plus narrow writing/reference. They were inspected; placeholder cover blocks, initial equal-width panes and clipped pair previews found during inspection were corrected and recaptured.

The fixture uses an isolated home, a synthetic profile provider and example documents. It invokes no signer and publishes no events. These are native Cocoa client captures, not evidence of VoiceOver, real signer interaction, other operating systems, native frame parity or a seven-client interoperability audit. The capture log reports Qt's Apple-system-font OpenType fallback warning for Khmer; the native popup renders readable Khmer glyphs. Real external-signer and cross-platform acceptance remain separately identified integration checks.

## PR integration review — 2026-11-21

An adversarial reviewer and a separate integrator reproduced and resolved the remaining native integration findings:

- Independently confirmed incoming backlinks retain their established root even when that root has no local document. Conflicting proven roots remain unresolved.
- Publication receipts update the intended document or the same group’s source under the signing owner. An unrelated imported document with the same bare identifier cannot receive that receipt. A first identifier assigned in the publication sheet still updates its intended document.
- Optional private ownership omitted or empty in a restored record adopts the selected account; an explicit different owner remains protected. This adoption never proves a public relationship.
- Exact event-ID deletions apply irrespective of clock skew; address deletions retain their timestamp cutoff, matching [NIP-09](https://github.com/nostr-protocol/nips/blob/master/09.md). The publication preflight reuses this same availability verifier.
- A late application-focus callback safely ignores a destroyed Qt object.

The final 115-case source-proof, actual-window receipt/restoration, authoring, journal and publication-sheet batch passed without the teardown error. German/English translation completeness and whitespace checks passed. These fixes extend existing ownership and verifier boundaries; they introduce no second routing, recovery or signing subsystem.

[Actual Light/Dark evidence](https://github.com/DoktorShift/my_editor/tree/1aa32be7d60a5e528c9abc250f85ad8d9c071ce7/multilingual-articles) identifies the capture revision `29c38cee`. The later corrections above do not change the captured composition; the original capture hashes are retained rather than labeling a newer commit as captured.
