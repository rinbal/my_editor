# Night Violet implementation and acceptance record

This implements the full presentation rollout from [PR #59](https://github.com/rinbal/my_editor/pull/59), against `rinbal/dev` at `57a023e5aa660749ef842e4fd8c277e33045c217`. PR #59 was still open and unmerged when reconciled; it contains planning documents, not application code. The three adjacent planning documents preserve that historical record. This file is the implementation disposition and acceptance record.

The unchanged HTML remains the visual source: SHA-256 `6df3f2759381666459b32fa701e2a73812bbfff4673671aabb1938d3f070cebc`. Its [Light/Dark reference captures](night-violet-review.md#night-violet-targets-from-the-html) are explicitly HTML, not application evidence.

## Architecture and deliberate departures

`nostr/ui/appearance.py` resolves one immutable set of semantic roles for palettes, stylesheets and painters. Existing owners opt into theme refresh through a Qt property naming their appearance method. Refresh enumerates live Qt widgets; there is no per-window callback registry, extra animation timer, persisted theme ownership or new preference. Static corner light is a clipped, mouse-transparent child behind the content. Existing component layouts, native controls, system fonts, shortcuts and action semantics remain.

Ownership comes from existing document/draft state or an explicit caller argument. Local file tabs, media players, alerts, notices and status messages retain neutral defaults. Returning a reused media viewer or Save button to local scope clears the Nostr treatment. The application palette remains neutral. Nostr status messages have a relay mark and explicit origin; expiry/replacement clears that origin. Permanent counters remain neutral. Settings adds marks only to already-gated Nostr rows. Welcome and native menus are preserved.

`native_appearance.py` is a guarded Cocoa adapter using the existing Rubicon dependency: NSWindow background, transparent title-bar background for Nostr windows, and the window's Aqua/Dark Aqua appearance. It does not replace the frame or alter native style masks. Local editor/Settings/save-destination windows use neutral chrome in their chosen appearance. Other platforms retain their native frame. Qt contrast changes and macOS application activation refresh cached accessibility preferences; increased contrast strengthens readable roles and suppresses glow, and Reduce Transparency suppresses glow. No new motion was added. Existing preview animation behavior is unchanged.

The implementation follows Apple's [color](https://developer.apple.com/design/human-interface-guidelines/color), [Dark Mode](https://developer.apple.com/design/human-interface-guidelines/dark-mode), [accessibility](https://developer.apple.com/design/human-interface-guidelines/accessibility), [buttons](https://developer.apple.com/design/human-interface-guidelines/buttons), and [materials](https://developer.apple.com/design/human-interface-guidelines/materials) guidance. The opaque, static treatment avoids depending on backdrop translucency for readability. The HTML's low-contrast tertiary text and bright gradient endpoint are intentionally adjusted. Native traffic lights, menus and title-bar metrics retain platform behavior instead of reproducing HTML chrome.

## Inventory disposition

Every original ID is accounted for below. **Implemented** means the presentation change is present; **preserved** identifies unchanged behavior or neutral content. It does not assert that every possible manual state on every OS has been exercised. Screenshot stems identify the real-widget fixtures in both appearances; automated suites are repository files under `tests/`, with `test_` and `.py` omitted for readability. The full suite covers the data-only preservation rows. Cross-platform/manual limits remain below.

| ID | Result and ownership | Actual screenshot / regression evidence |
| --- | --- | --- |
| 1 | Implemented Media Library chrome, controls and selection; content neutral. | `media`; `media_library`, `media_private_library`, `media_entry` |
| 2 | Implemented grid selection/focus and upload ring on an opaque dusk backing; locks/error words retained. | `media`; `media_library`, `media_previews` |
| 3 | Implemented Quick Look frame; image/PDF/text content preserved. | `quick-look`; `media_previews`, `media_private_preview` |
| 4 | Implemented explicit Nostr player scope, transport/slider roles and reset to local. | `media-viewer-nostr`, `media-viewer-local`; `night_violet`, `media_previews`, `viewer_tabs` |
| 5 | Implemented reused viewer scope, error/retry, detached theme refresh and local reset. | `media-viewer-nostr`, `media-viewer-local`; `night_violet`, `media_wiring` |
| 6 | Implemented upload indicator on existing audio/video cards; local cards/media pixels preserved. | `media_document`, `media_wiring`, `media_previews` |
| 7 | Implemented violet upload/uploaded badges; local dark and failure red preserved. | `media_document`, `media_wiring` |
| 8 | Preserved UploadProgress model, preparation/wait/stall/totals and messages. | No source change; `blossom_upload_progress` |
| 9 | Implemented Media Servers controls, list and alerts. | `media-servers`; `media_servers_dialog` |
| 10 | Implemented explicit Nostr Save As status and red failures; native chooser and cancellation preserved. | `media_save`, `night_violet`, `media_library` |
| 11 | Preserved thumbnail queues, cache, fetching and error semantics. | No source change; `media_previews`, `media_library` |
| 12 | Implemented cover field affordances, hints and focus; image unchanged. | `publishing`, `article-details`; `cover_field` |
| 13 | Implemented crop frame, controls and canvas palette; source image/crop logic preserved. | `cover-crop`; `cover_crop` |
| 14 | Implemented image checklist palette and selected-icon pixel preservation. | `review-images`; `imports_image_review`, `night_violet` |
| 15 | Implemented private-media Share confirmation/progress; disclosure and privacy unchanged. | `share-media`; `publish_copy_dialog`, `media_place` |
| 16 | Preserved private preview/decryption and plaintext handling. | No source change; `media_private_preview`, `media_private_library` |
| 17 | Implemented explicit Nostr upload-before-publish/draft alerts and status. | `nostr-alert`; `media_document`, `save_dispatch`, `save_destinations` |
| 18 | Preserved Blossom error mapping; consuming UI retains error/retry meaning. | No mapper change; `media_library`, `media_save`, `media_place` |
| 19 | Implemented Profile assistant controls and live theme updates. | `profile`; `profile_window`, `profile_editing` |
| 20 | Preserved neutral profile card with an explicit opaque boundary against parent glow. | `profile`; `profile_card` |
| 21 | Implemented account capsule and cached-chevron refresh; account menu behavior preserved. | `editor-drafts`; `accounts`, `accessibility`, `nostr_menu` |
| 22 | Preserved avatar images, initials, loaders and cache identity. | `profile`, `editor-drafts`; `avatar_loader`, `accounts` |
| 23 | Implemented membership workflow, orange payment actions/amount, dusk service tiles and neutral QR. | `membership`, `membership-payment`; `membership_window`, `membership_wiring`, payment-watcher/price suites |
| 24 | Implemented dusk membership card, relay mark and orange brand. | `membership`; `membership_window`, `einundzwanzig_copy` |
| 25 | Implemented signer tabs, steps and actions; QR black/white and countdown preserved. | `connect`; `connect_dialog`, `bunker_sign_in`, `relay_sign_in` |
| 26 | Implemented backup assistant and explicit Nostr confirmations; secret protection preserved. | `backup-account`; `local_keys`, `account_controller`, `secret_clipboard` |
| 27 | Implemented Create Account shared assistant; no setup initiated by styling. | `create-account`; `local_keys`, `account_controller` |
| 28 | Implemented Move Key/Amber through the same assistant; hidden secret stays hidden. | `move-key`; `local_keys`, `account_controller` |
| 29 | Implemented Restore Account assistant; file/password handling unchanged. | `restore-account`; `local_keys`, `account_controller` |
| 30 | Implemented explicit Nostr account-controller alert context; deletion/default roles unchanged. | `nostr-alert`; `account_controller`, `accounts` |
| 31 | Implemented Needs Account alert context at existing request boundary. | `nostr_menu`, `accounts`, `accessibility` |
| 32 | Implemented Nostr secret-key warnings; unrelated password alerts unchanged. | `secret_keys`, `secret_clipboard`, `dialog_keyboard` |
| 33 | Implemented signer/relay status context and red relay failures; words and polling unchanged. | `relay_auth`, `drafts_panel_signer_silence`, publishing suites |
| 34 | Implemented Article publishing sheet and account capsule; preview card neutral. | `publishing`, `live-publishing`; `publish_sheets`, `publish_dialog_cues`, `publisher_article` |
| 35 | Implemented Note publishing frame; editable composition neutral. | `publish-note`; `publish_sheets`, `publish_dialog_cues` |
| 36 | Implemented shared people/account/status/action styles; failure/default semantics preserved. | `publishing`, `publish-note`; `publish_sheets`, `publish_dialog_cues` |
| 37 | Implemented violet draft destination/Save and neutral local destination, switching both ways. | `save-destination`; `night_violet`, `save_destinations`, `save_dispatch` |
| 38 | Implemented draft kind picker and Nostr status/alerts; encrypted-save routing unchanged. | `save-draft`; `stash_kind_dialog`, `publisher_draft`, `save_dispatch` |
| 39 | Preserved first-publication dates, lookup and persistence. | No source change; `article_published_at` |
| 40 | Implemented Article Details panel palette/fields/chips/header. | `article-details`; `article_details_panel`, `article_live` |
| 41 | Implemented small violet article-header control cue; document/title/cover remain neutral. | `editor-drafts`, `article-details`; `article_header`, `article_live` |
| 42 | Implemented inherited inspector selection/control scope; authorship and share math preserved. | `article-details`; `article_details_panel`, coauthor suites |
| 43 | Implemented semantic tag chips and entry; keyboard/wrapping preserved. | `publishing`, `article-details`; `tag_field` |
| 44 | Implemented mention popup/quiet selection; avatars and lookup preserved. | `mentions`; `mentions`, `secret_keys` |
| 45 | Implemented reference popup/action/errors; insertion and dismissal preserved. | `reference`; `insert_reference` |
| 46 | Implemented explicit Nostr conflict notice; local file/recovery notices remain neutral. | `draft-conflict`; `document_notices`, workspace/draft suites |
| 47 | Implemented Draft Relays assistant/list/default/focus. | `draft-relays`; `draft_relays_sheet` |
| 48 | Implemented Drafts panel, rows, chrome and launchers. | `editor-drafts`; `drafts_panel`, `drafts_panel_signer_silence` |
| 49 | Implemented compact/full preview frame and neutral full reader; existing positioning/fade/detach preserved. | `draft-preview`, `draft-preview-full`; `drafts_preview` |
| 50 | Implemented focused gradient and quiet inactive row selection, distinct focus. | `editor-drafts`, `published`; `post_list`, `drafts_panel`, `published_view` |
| 51 | Implemented Published list/preview appearance; model and account transitions preserved. | `published`; `published_view` |
| 52 | Implemented detached reader chrome/action bar with neutral document. | `reader`, `live-reader`; `reader_window`, `drafts_preview` |
| 53 | Implemented Imports complete window and live theme refresh; article pane neutral. | `imports`; `imports_window`, `imports_workspace`, `imports_scale` |
| 54 | Implemented follow/file/link/source settings assistants, including their own live styles. | `follow-source`, `import-file`, `import-link`, `source-settings`; `imports_sheets`, `imports_source_settings` |
| 55 | Implemented import activity palette/progress/popover; job totals and transitions preserved. | `import-progress`; `imports_progress` |
| 56 | Implemented sidebar surface/glyphs/selection; favicons unchanged. | `imports`; `imports_window`, `imports_scale` |
| 57 | Implemented post-list selection/focus/metadata; checked state and content unchanged. | `imports`; `imports_post_list`, `imports_scale` |
| 58 | Implemented primary action/options styles and live refresh without rebuilding choices. | `import-options`, `imports`; `night_violet`, `imports_window` |
| 59 | Preserved neutral imported article content, media and fidelity. | `imports`; `imports_article` |
| 60 | Preserved native Dock/taskbar badge and count/opt-in behavior. | No source change; `imports_badge` |
| 61 | Preserved native menus and command visibility/enabled state. | `nostr_menu`, `shared_menus`, `commands`, `file_menu` |
| 62 | Implemented explicit temporary-message origin/relay mark, failure color and expiry/reset. | `night_violet`, `window_status`, `media_save`, `relay_auth` |
| 63 | Implemented relay marks on existing gated Settings rows; neutral window and controls. | `settings`; `settings_window`, `accessibility` |
| 64 | Preserved Welcome wording, gating and neutral presentation; no new account requirement. | No source change; `accounts`, `nostr_menu`, existing welcome tests |

Additional integrations: `ContextTabBar` reads draft binding, save destination and published-without-local-file state at paint/layout time, so reorder follows the document without persisted styling. `night_violet` tests ownership/reorder and local reset; `workspace`, `workspace_restore`, `file_tabs`, `viewer_tabs` and account suites cover the existing routing/restoration behavior. Shared alert defaults remain neutral, destructive actions retain their role and Return behavior. Existing local library/editor, Paper Mode, output/export/printing and publication builders are covered by the full regression suite without format or protocol edits.

## Visual review and contrast

Actual native macOS captures exposed and corrected: mixed Light/Dark client and title-bar regions; blue crop/Review Images selection; selected-thumbnail tint; stale dark toolbar icons; a block-shaped combo arrow; tinted profile/preview reading surfaces; and insufficiently scoped media controls. Early batch captures intermittently showed an empty native QR rectangle despite correct client pixels. An isolated native window rendered correctly; waiting for native exposure and compositor settling also produced the correct batch capture. The final helper captures the compositor before the client grab and waits 1.8 seconds after exposure. Both payment and signer QR evidence must show black modules and the white quiet zone.

`tests/test_night_violet.py` measures the shared text/secondary-text roles over panel/sidebar/list/field/control/hover/selection-soft backgrounds, both action-gradient endpoints, pressed fills, control boundaries, focus, payment text, progress and the strongest alpha-composited glow. Ordinary text targets 4.5:1; essential controls/icons target 3:1. Inactive selections use text on a quiet fill. QR and source image pixels are preserved independently of accent.

Reference tertiary text (~2.75:1 Light / ~3.99:1 Dark on its panel) is replaced by `#625971` / `#B7ACC9`. The highlight endpoint is `#8050DC`, not the brighter reference endpoint. Light secondary text was darkened further after measuring the glow composite. Focus on accent uses white so it survives both gradient and pressed colors. Payment stays `#F7931A` with dusk text; the readable amount is `#8A4800` / `#FFB561`. Nostr error text is `#A52A1D` / `#FF8C82`, strengthened for the glow background without changing local error roles.

| Measured pair | Light | Dark |
| --- | ---: | ---: |
| Secondary text / panel | 5.88 | 8.02 |
| Secondary text / strongest violet glow | 4.65 | 5.81 |
| White / gradient highlight | 5.12 | 5.12 |
| White / button endpoint | 6.41 | 6.01 |
| Field boundary / field | 3.91 | 3.43 |
| Focus / field | 6.41 | 6.58 |
| Progress / track | 4.88 | 5.35 |
| Dusk payment text / orange | 7.99 | 7.99 |
| Payment amount / strongest violet glow | 4.92 | 7.16 |
| Error text / strongest violet glow | 5.02 | 5.55 |

The strongest violet composites resolve to `#E0D2FD` (Light) and `#3A2A60` (Dark). Ratios use `theme.contrast_ratio`; the machine-readable table accompanies the screenshots. Disabled controls retain distinct treatment and are not counted as ordinary enabled text.

## Verification and readiness

Use the pinned `packaging/requirements-test.lock`: Python 3.12.12 / PySide6 6.11.2. Native captures use macOS 15.6 arm64, Cocoa, the existing Fusion AppStyle and deterministic test fixtures; no live relay, signing request, payment, production key or document is used. The helper records revision, script hash, dirty state, logical size and actual display scale. Baseline and implementation captures use the same fixture script/content/window dimensions. HTML remains separately labeled.

Run commands:

```sh
python -m pytest -q
python scripts/i18n.py check
python scripts/night_violet_capture.py --output /tmp/night-violet --native --extended
```

The first integration run reported 8,739 passed, 14 skipped and 44 failed. Those failures identified test doubles missing the new optional context argument, old blue-color assertions, and one missing test import; they were corrected without removing behavioral checks. Sandbox-only failures involving subprocesses/local HTTP servers were rerun outside the sandbox. The second full run passed: **8,783 passed, 14 skipped, 3 dependency warnings, 642.13 seconds**. Subsequent focused runs passed (211 preview/settings/media, 209 media/context/membership, 190 appearance/keyboard), and the final contrast correction passed **533 affected tests**. The full run preceded the final visual refinements; exact-head platform CI is reported separately rather than mislabeling that run.

Outstanding manual acceptance: VoiceOver/Full Keyboard Access with a human listener, real system accent/increased-contrast/Reduce Transparency changes, Windows and Linux desktop composition, and physical fractional-scale displays. Injected preference tests and offscreen rendering are not substitutes for those checks. Native macOS live Light/Dark/Automatic switching and window-state checks are recorded by the fixture; platform CI reports its actual rendering platform (Windows native, macOS/Linux offscreen). Keep the implementation PR **draft** until these remaining acceptance checks are resolved; no merge is authorized.

## Delivery update

The single implementation PR is [#60](https://github.com/rinbal/my_editor/pull/60), targeting `rinbal/dev`, left open as a draft and not merged. Its description links the immutable before/after gallery, per-capture manifests, and measured contrast data on the existing `pr-screenshots` branch. Screenshot manifests identify the exact captured revision. The PR's Checks tab is the authoritative result for its final head.

The first platform runs found four quit-path tests sharing a plain-QStatusBar test fixture, plus a 40 ms expiry assertion that was too tight for native Windows timers. The fixture now uses the actual context-aware status bar and the timer test waits for observable expiry with a bounded deadline. A final native media review also caught Qt deriving a dimmed PlaceholderText color from the requested status ink; Nostr preview/status labels now use WindowText with their explicit semantic ink. All **126 affected checks** passed locally after these corrections. The macOS verification run also reported an unrelated file-list timing failure; that source was not changed, and its result remains visible in CI rather than being suppressed.

The corrected application revision `ca9240373ce85135e6ebd25a6f6b00f7705ceb0c` passed the full local suite: **8,786 passed, 14 skipped, 3 dependency warnings in 540.40 seconds**. Its [platform workflow](https://github.com/rinbal/my_editor/actions/runs/39844861822) passed Windows, macOS and Linux. The final capture-tool revision changes no application code: it keeps QApplication alive between independent fixtures and explicitly deletes closed fixture windows. Two earlier long native batches exited in Qt screen handling; capture manifests and the gallery identify the completed evidence, and multi-display transitions remain a manual acceptance item.

Native live-window checks passed for Light, Dark and Automatic, preserving unsaved title/summary, selected text and local document content, plus fullscreen/restore/resize with unchanged window flags. English and German native major-window captures are included. Actual captures contain 1× and 2× displays; each manifest records the scale per image, and comparisons use matching logical dimensions. Offscreen scope/focus/keyboard checks passed at both 1.5× and 2× (**39 each**). The existing `window_grid` divider-pixel assertion fails at 1.5× on both this branch and unchanged upstream; it passes at the standard scale. That baseline limitation is kept visible and its assertion is not weakened. Translation checks, undefined-name lint and whitespace checks passed.

The closing screenshot review found two remaining appearance defects: a detached reader retained explicit Dark document text colors after switching to Light, and native list selection could remain blue. PreviewReader now rerenders its read-only source on appearance changes while restoring selection and scroll; scoped list controls explicitly use the shared active/inactive selection roles. The affected reader, draft preview, server/relay list, image review and Night Violet suites passed **266 tests**, including new note/article selection-and-contrast checks. The gallery separately identifies these final correction captures and the earlier complete capture set. Full-suite/platform results above predate this small correction; the PR Checks tab reports the final revision without conflating the two.
