# Night Violet affected surfaces

This checklist reconciles the original 64-entry design inventory with upstream `dev` at `57a023e5aa660749ef842e4fd8c277e33045c217`. IDs preserve the original inventory order. Some entries are shared helpers or data sources, not independent screens; they need a verified preservation decision rather than a cosmetic edit.

All rows are **planned, not implemented**. File paths are relative to the repository root. Pair this checklist with the [implementation plan](night-violet-plan.md) and its common appearance, keyboard, accessibility and lifecycle requirements.

Disposition: **Style** changes presentation; **Mixed** requires explicit Nostr context; **Preserve** retains behavior/content or a system-owned surface. A completion tick means the treatment and listed states have been verified in both appearances, or that the preservation condition has been checked.

## Blossom media

| Done | ID | Surface | Files and ownership | Treatment and state checks |
| --- | --- | --- | --- | --- |
| [ ] | 1 | Media Library | `nostr/ui/media_library_dialog.py`, `MediaLibraryDialog` | **Style.** Violet toolbar/body/footer and selection. Browse and picker modes, empty/search/loading/error/private checks, quota and Save As progress. Pictures stay neutral. |
| [ ] | 2 | Media grid tiles | `nostr/ui/media_grid.py`, `MediaGrid`, `TileDelegate` | **Style.** Violet selection/upload ring; preserve private lock, local state, failure/retry and cancellation. Check active/inactive selection, focus, thumbnails and large grids. |
| [ ] | 3 | Media Quick Look | `nostr/ui/media_preview.py`, `QuickLook`; `nostr/ui/file_preview.py` | **Style.** Violet Nostr frame/actions around neutral image, PDF, text or player. Loading, failed/stopped download, unsupported/oversized file, paging and keyboard dismissal. |
| [ ] | 4 | Media player | `nostr/ui/media_player.py`, `MediaPlayer`; callers in `media_tabs.py`, `nostr/ui/media_preview.py`, `nostr/ui/media_viewer.py` | **Mixed.** Explicit origin controls accent. Preserve local playback colors, video pixels, transport states, seeking, mute and full-screen. A cached file path is not proof of local ownership. |
| [ ] | 5 | Media Viewer | `nostr/ui/media_viewer.py`, `MediaViewer` | **Mixed.** Scope from the invoking media context; violet when Nostr-owned. Preserve local sources, neutral playback, loading/failure/retry and detached-window lifecycle. |
| [ ] | 6 | Audio/video cards in text | `nostr/ui/media_card.py`; wiring in `main_window.py` | **Mixed.** Violet Nostr upload line, neutral media; preserve local card appearance, play hit targets, determinate/indeterminate progress and failed state. No full-document repaint per tick. |
| [ ] | 7 | Media badges | `media_badge.py`, `Badge`, `paint()`; `main_window.py`, `_media_badge()` | **Mixed.** Local capsule stays dark, uploading/uploaded gain violet, failed stays red. Preserve wording, geometry, visibility gates and context menus. |
| [ ] | 8 | Upload progress model | `nostr/blossom/upload_progress.py`, `UploadProgress` | **Preserve.** Theme its consuming indicators only. Keep preparation, signer waiting, stall, upload and mirroring values/words and truthful totals. |
| [ ] | 9 | Media Servers | `nostr/ui/media_servers_dialog.py`, `MediaServersDialog` | **Style.** Violet sheet/selection/default; membership row carries orange identity. Preserve address validation, primary ordering, remove/share alerts and errors. |
| [ ] | 10 | Media Save As progress | `nostr/ui/media_saver.py`, `MediaSaver`; Library footer and `main_window.py` | **Mixed.** Nostr-origin progress/status only. Keep native file dialogs, stop behavior, counts and failures; restore neutral status after completion. |
| [ ] | 11 | Thumbnail queues | `nostr/ui/media_thumbnails.py`, `nostr/ui/thumbnail_loader.py` | **Preserve.** Background queues have no independent surface. Do not change fetching, caching or error semantics for styling. |
| [ ] | 12 | Cover field | `nostr/ui/cover_field.py`, `CoverImageField` | **Style.** Violet focus/upload affordances around the unchanged image. Inspector and publish layouts; empty, typed URL, drag/paste, upload/error and removal. |
| [ ] | 13 | Cover crop dialog | `nostr/ui/cover_crop_dialog.py`, `CoverCropDialog`, `CropCanvas` | **Style.** Violet dialog/controls; source image and readable crop handles preserved. Keyboard moves, zoom, reset, aspect choices, size warnings and upload failure. |
| [ ] | 14 | Review Images | `nostr/ui/image_review_dialog.py`, `ImageReviewDialog` | **Style.** Violet sheet/check states and default action. Keep image pixels, selected counts, loading and keyboard reachability. |
| [ ] | 15 | Share private media | `nostr/ui/publish_copy_dialog.py`, `PublishCopyDialog` | **Style.** Violet confirmation/progress; errors red. Preserve disclosure, partial completion, cancel, retry and existing privacy behavior. |
| [ ] | 16 | Private preview/decryption | `nostr/media/private_preview.py` | **Preserve.** No new surface or data behavior. Lock badge remains visible in the styled grid; do not broaden access or persist plaintext. |
| [ ] | 17 | Upload before save/publish | `main_window.py`; shared `alerts.py` | **Mixed.** Explicit Nostr alert/status styling. Preserve local-file/privacy wording, user choice, cancellation and continuation into existing publishing/draft workflows. |
| [ ] | 18 | Blossom error messages | `nostr/blossom/errors.py`; media UI consumers | **Preserve.** Keep error mapping and words. Their visible consumers retain error emphasis and retry actions within the violet context. |

Relevant existing suites: `tests/test_media_library.py`, `test_media_previews.py`, `test_media_private_preview.py`, `test_media_private_library.py`, `test_media_servers_dialog.py`, `test_media_save.py`, `test_media_document.py`, `test_media_wiring.py`, `test_cover_field.py`, `test_cover_crop.py`, `test_publish_copy_dialog.py`, `test_blossom_upload_progress.py`, `test_media_entry.py`, `test_viewer_tabs.py`.

## Profile

| Done | ID | Surface | Files and ownership | Treatment and state checks |
| --- | --- | --- | --- | --- |
| [ ] | 19 | Edit Profile | `nostr/ui/profile_window.py`, `ProfileWindow`; `nostr/ui/assistant.py` | **Style.** Violet assistant and fields/focus around neutral profile content. Reading, absent profile, field validation, unchanged/dirty, saving and failed-save states. |
| [ ] | 20 | Profile card | `nostr/ui/profile_card.py`, `ProfileCard` | **Preserve.** Neutral content and pictures; confirm parent styles/glow do not bleed into it. Long names, biography and missing image. |
| [ ] | 21 | Profile chip and account menu | `nostr/ui/profile_chip.py`, `ProfileChip` | **Style/Mixed.** Violet account capsule/outline, unchanged avatar. No-account, switching, long names and live appearance. Native account menu retains system presentation. |
| [ ] | 22 | Avatars | `nostr/ui/avatar.py`; consuming widgets | **Preserve.** Keep identity image and initials; only enclosing Nostr control/selection gets violet. Loading, fallback and cache invalidation remain correct. |

Relevant existing suites: `tests/test_profile_window.py`, `test_profile_card.py`, `test_profile_editing.py`, `test_avatar_loader.py`, `test_accounts.py`, `test_accessibility.py`.

## EINUNDZWANZIG membership

| Done | ID | Surface | Files and ownership | Treatment and state checks |
| --- | --- | --- | --- | --- |
| [ ] | 23 | Membership workflow | `nostr/ui/membership_window.py`, `MembershipWindow`; `nostr/ui/grouped_list.py`; `nostr/membership_controller.py` alert call sites | **Style.** Violet window plus orange glow; dusk service tiles; orange payment amount/action; violet account actions. Overview, application, pending, unavailable service, invoice, expired/paid, member and renewal states. Preserve QR, privacy prompts and all signing/payment behavior. |
| [ ] | 24 | Membership card | `nostr/ui/membership_card.py`, `MembershipCard` | **Style.** Reference dusk background and relay mark, orange brand, readable member/year/address, active check/dot plus text. Inactive/renewal information remains truthful and legible. |

Relevant existing suites: `tests/test_membership_window.py`, `test_membership_wiring.py`, `test_einundzwanzig_copy.py`, `test_einundzwanzig_payment_watcher.py`, `test_einundzwanzig_prices.py`.

## Account and signer

| Done | ID | Surface | Files and ownership | Treatment and state checks |
| --- | --- | --- | --- | --- |
| [ ] | 25 | Connect Signer App | `nostr/ui/connect_dialog.py`, `ConnectDialog` | **Style.** Violet tabs, focus, steps, progress and default; QR black/white. Scan/paste, countdown/expiry, waiting, error, reconnect and create/restore links. |
| [ ] | 26 | Back Up Account | `nostr/ui/account_windows.py`, `BackupAccountWindow`, `BackupForm` | **Style.** Shared assistant roles, plain readable key fields and password controls. Saving/success/error; secret copy and unsafe-backup confirmations retain their behavior. |
| [ ] | 27 | Create Account | `nostr/ui/account_windows.py`, `CreateAccountWindow` | **Style.** Violet page/step/default roles. Name, backup/skip, local key/signer choice, setup, partial setup and retry. Styling does not initiate setup. |
| [ ] | 28 | Move key / Amber steps | `nostr/ui/account_windows.py`, `MoveToSignerWindow`, `_AmberSteps` | **Style.** Consistent assistant treatment. Hidden secret QR stays hidden until requested and black/white when shown. Wrong account, back/continue and key-removal alert. |
| [ ] | 29 | Restore Account | `nostr/ui/account_windows.py`, `RestoreAccountWindow` | **Style.** Violet restore workflow. File/key input, password, invalid backup, setup and completion. Preserve secret protection and native file chooser. |
| [ ] | 30 | Account controller alerts | `nostr/account_controller.py`; `alerts.py` | **Mixed.** Nostr presentation supplied at call sites. Relay-list publication, sign-out and local-key deletion retain action roles, wording and cancellation. |
| [ ] | 31 | Needs an account | `main_window.py`, `_offer_account()`; `alerts.py` | **Mixed.** Violet context only for the existing explicit Nostr request. Preserve create/connect/cancel choices and opt-in boundary. |
| [ ] | 32 | Secret-key warnings | `nostr/ui/secret_key_check.py`; paste guard in `main_window.py`; `alerts.py` | **Mixed.** Nostr identification where appropriate; warning/error semantics remain prominent. Keep reject/replace/cancel logic and do not recolor unrelated password dialogs. |
| [ ] | 33 | Signer/relay notices | Existing status consumers in `main_window.py`, `nostr/ui/publish_sheet.py`, account/import/media UI | **Mixed.** Violet context plus existing waiting words; failures retain error treatment. No invented connection/approval state and no extra polling. |

Relevant existing suites: `tests/test_connect_dialog.py`, `test_account_controller.py`, `test_local_keys.py`, `test_secret_keys.py`, `test_secret_clipboard.py`, `test_bunker_sign_in.py`, `test_relay_sign_in.py`, `test_accessibility.py`, `test_dialog_keyboard.py`.

## Publishing

| Done | ID | Surface | Files and ownership | Treatment and state checks |
| --- | --- | --- | --- | --- |
| [ ] | 34 | Publish as Article | `nostr/ui/publish_article_dialog.py`, `PublishArticleDialog` | **Style.** Violet sheet/fields/actions around neutral preview card. Details expansion, authors/tags/cover, signer waits, lookup ambiguity, errors and success. Preserve window modality and document-detail updates. |
| [ ] | 35 | Publish as Note | `nostr/ui/publish_note_dialog.py`, `PublishNoteDialog`, `NoteText` | **Style.** Violet frame/actions, neutral editable composition. Mentions/count, waiting/failure/retry and appearance switch. The source document remains unchanged. |
| [ ] | 36 | Shared publishing parts | `nostr/ui/publish_sheet.py`, `PeopleRow`, `AccountRow`, `StatusLine`, `BottomRow` | **Style.** Consistent chips, account capsule, focus, default and progress. Preserve tagged-person meaning, account choice and red failure text. |
| [ ] | 37 | Save destination | `nostr/ui/save_destination_dialog.py`, `SaveDestinationDialog`, `_DestinationCard` | **Mixed.** Nostr card violet, local card neutral, Save follows selected destination. Remembered choice, keyboard toggles, cancel, resize and theme switch. |
| [ ] | 38 | Save as Nostr draft | `nostr/ui/stash_kind_dialog.py`, `StashKindDialog`; `main_window.py` alert/status call sites | **Style/Mixed.** Violet kind picker and Nostr progress. Preserve title/slug, encrypted save, duplicate/oversized draft, account mismatch, retry and cancellation. |
| [ ] | 39 | First-publication data | `nostr/first_publications.py` | **Preserve.** Data only. Its date text adopts the containing control's readable role; no date lookup or persistence change. |

Relevant existing suites: `tests/test_publish_sheets.py`, `test_publish_dialog_cues.py`, `test_publisher_article.py`, `test_publisher_draft.py`, `test_save_destinations.py`, `test_stash_kind_dialog.py`, `test_save_dispatch.py`, `test_article_published_at.py`.

## Writing in the editor

| Done | ID | Surface | Files and ownership | Treatment and state checks |
| --- | --- | --- | --- | --- |
| [ ] | 40 | Article Details | `nostr/ui/article_details_panel.py`, `ArticleDetailsPanel` | **Style.** Violet side panel, consistent fields/chips/focus and header. Article/note/non-editor/unsupported-front-matter states; preserve width, resizing and model binding. |
| [ ] | 41 | Article header | `nostr/ui/article_header.py`, `ArticleHeader`; `nostr/ui/article_headers.py` | **Mixed.** Small violet identity cue and controls; neutral document/cover/title/summary. Preserve edit/undo, empty/typed URL/cover states, layout and opt-in. |
| [ ] | 42 | Authors and zap shares | `nostr/ui/authors_section.py`, `AuthorsSection`; `nostr/ui/coauthors.py` | **Style/Preserve.** Violet table selection and controls in the Nostr inspector. Keep share arithmetic, authorship, warnings and equal-split behavior unchanged. |
| [ ] | 43 | Tag field | `nostr/ui/tag_field.py`, `TagField`, `_Chip` | **Style.** Reference violet chips and entry focus. Preserve wrapping, insertion/deletion, clipboard, announcements and keyboard navigation. |
| [ ] | 44 | Mention picker | `nostr/ui/mention_picker.py`, `MentionPicker` | **Style.** Violet popover and selection, unchanged avatars. Following/search/loading/no results/secret-key rejection, caret placement and Escape behavior. |
| [ ] | 45 | Insert Note or Article | `nostr/ui/reference_popover.py`, `ReferencePopover` | **Style.** Violet popover/primary action with readable errors. Preserve validation, insertion, caret anchoring and focus restoration. |
| [ ] | 46 | Draft conflict banner | `nostr/ui/draft_conflict_banner.py`, `DraftConflictBanner`; `document_notice.py` | **Mixed.** Violet only for Nostr conflict context. Keep local file changed/deleted/recovered notices neutral; preserve merge/open/keep choices and dismissal. |
| [ ] | 47 | Draft Relays | `nostr/ui/draft_relays_sheet.py`, `DraftRelaysSheet` | **Style.** Violet assistant sheet/list/default/focus. Retain add/remove, auth/validation errors and save behavior. |
| [ ] | 48 | Drafts panel | `nostr/ui/drafts_panel.py`, `DraftsPanel`; `nostr/ui/drafts_common.py` | **Style.** Violet panel, header/list/footer, selection and existing launchers. No-account/loading/empty/search/error, pending deletion, signer wait, active/inactive rows and preview keyboard flow. |
| [ ] | 49 | Draft preview popover | `nostr/ui/drafts_preview.py`, `PreviewPopover`, `PreviewController`; `nostr/ui/preview_parts.py` | **Style.** Violet frame/actions around neutral content. Compact/full, pinned, placement, dismissal, drag/detach, loading and image failure; preserve current animation/focus behavior. |
| [ ] | 50 | Post list rows | `nostr/ui/post_list.py`; `nostr/ui/drafts_common.py` | **Style.** Violet focused selection, quiet inactive selection and distinct focus. Preserve checked/selected distinctions, title/summary/date legibility and error markers. |
| [ ] | 51 | Published view | `nostr/ui/published_view.py`, `PublishedView` | **Style.** Share panel/preview rules. No account, loading, no posts, no results, partial relays, failure/retry, edit/delete and account switch. |
| [ ] | 52 | Detached reader | `nostr/ui/reader_window.py`, `ReaderWindow`; `nostr/ui/preview_parts.py` | **Style.** Violet frame/action bar, neutral reader/cover. Preserve content font, resize, detached ownership, appearance updates and close/reopen lifecycle. |

Relevant existing suites: `tests/test_article_details_panel.py`, `test_article_header.py`, `test_article_live.py`, `test_tag_field.py`, `test_mentions.py`, `test_insert_reference.py`, `test_document_notices.py`, `test_draft_relays_sheet.py`, `test_drafts_panel.py`, `test_drafts_preview.py`, `test_post_list.py`, `test_published_view.py`, `test_reader_window.py`.

## Imports

| Done | ID | Surface | Files and ownership | Treatment and state checks |
| --- | --- | --- | --- | --- |
| [ ] | 53 | Imports window | `nostr/ui/imports_window.py`, `ImportsWindow` | **Style.** Complete violet frame/toolbar/sidebar/list, neutral article pane. Current three-pane layout, empty/loading/error/search/drop overlay, source commands, resize and appearance switching. |
| [ ] | 54 | Imports sheets and source settings | `nostr/ui/imports_sheets.py`, `FollowSheet`, `FileSheet`, `LinkSheet`; `nostr/ui/imports_source_settings.py`, `SourceSettingsSheet` | **Style.** Shared violet assistant rules. Website/feed/Nostr/file/link input, discovery choices, pasted feed, drop zone, latest source-specific questions, validation, cancellation and unsubscribe. |
| [ ] | 55 | Import activity | `nostr/ui/imports_activity.py`, `ActivityButton`, `JobCard` | **Style.** Violet ring/bar and popover. Keep actual totals and paused/stopping/failed/completed words; retry and stop remain reachable. |
| [ ] | 56 | Imports sidebar | `nostr/ui/imports_sidebar.py`, `Sidebar`, `SidebarDelegate`; `nostr/ui/imports_glyphs.py` | **Style.** Violet navigation surface/glyphs/selection; source favicons untouched. Focus, inactive state, counts, failed/paused source and long names. |
| [ ] | 57 | Imports post list | `nostr/ui/imports_post_list.py`, `PostList`, `PostDelegate` | **Style.** Palette and paint roles for selected/checked/focused states and metadata. Preserve covers, status labels, scrolling performance and multiselection. |
| [ ] | 58 | Imports action bar/options | `nostr/ui/imports_actions.py`, `ActionBar`, `OptionsPopover` | **Style.** Reference primary-action emphasis and violet popover. Preserve skip/restore, mixed source defaults, image review, disabled during work and long explanations. |
| [ ] | 59 | Imports article pane | `nostr/ui/imports_article.py`, `ArticlePane`; `nostr/preview.py` | **Preserve/Mixed.** Reading content stays neutral; Nostr-only surrounding controls can use scoped roles. Preserve improved importer content fidelity, byline/link behavior, media and empty state. |
| [ ] | 60 | App icon badge | `nostr/ui/imports_badge.py`, `AppIconBadge` | **Preserve.** Native Dock/taskbar number unchanged. Existing opt-in offer can use Nostr alert presentation; badge count and settings unchanged. |

Relevant existing suites: `tests/test_imports_window.py`, `test_imports_sheets.py`, `test_imports_source_settings.py`, `test_imports_progress.py`, `test_imports_post_list.py`, `test_imports_article.py`, `test_imports_scale.py`, `test_imports_badge.py`, `test_imports_signer_change.py`.

## System and mixed surfaces

| Done | ID | Surface | Files and ownership | Treatment and state checks |
| --- | --- | --- | --- | --- |
| [ ] | 61 | Nostr and related menus | `main_window.py`, `commands.py` | **Preserve.** System menu rendering and enabled/visible command rules remain. No purple menu bar or new entry points; existing Nostr toolbar toggles get their scoped icon treatment. |
| [ ] | 62 | Status bar | `main_window.py` message producers and permanent items | **Mixed.** Violet mark/text for Nostr messages only. Preserve red failures, counts, timers and local messages. Expiry, replacement, concurrent work and sign-out restore correct context. |
| [ ] | 63 | Settings | `settings_window.py`, `SettingsWindow` | **Mixed.** Relay mark on existing Nostr upload/web-reader rows only; preserve neutral window and opt-in visibility. No separate Night Violet preference or extra settings burden. |
| [ ] | 64 | Welcome | `welcome.py` | **Mixed/Preserve.** Respect existing Nostr wording/visibility; a context mark only where already appropriate. No global purple surface, new marketing block or Nostr account requirement. |

Relevant existing suites: `tests/test_nostr_menu.py`, `test_shared_menus.py`, `test_settings_window.py`, `test_window_status.py`, `test_accounts.py`, `test_commands.py`, `test_file_menu.py`.

## Cross-cutting coverage beyond the numbered inventory

| Done | Integration point | Files | Required check |
| --- | --- | --- | --- |
| [ ] | Theme and shared helpers | `theme.py`, `nostr/ui/assistant.py`, `nostr/ui/grouped_list.py`, `nostr/ui/drafts_common.py`, `nostr/ui/preview_parts.py`, existing icon helpers | Scoped roles and palettes, preserved defaults, cache invalidation, stylesheet guard, shared relay mark and complete control states. |
| [ ] | Nostr tab identity | `main_window.py`, `tab_details.py`, `workspace.py`, `workspace_restore.py`; current tab bar ownership | Whole-tab tint from existing ownership, local file tabs unchanged. Open/restore/copy/detach/rename/reorder/close, mixed tabs, account switch and sign-out. Avoid persistence changes. |
| [ ] | Shared alerts and notices | `alerts.py`, `document_notice.py`, Nostr call sites; `password_alert.py` preservation | Explicit presentation context, neutral default, destructive/error semantics; local file/update/recovery/password cases unchanged. |
| [ ] | Neutral editor and library | `library_sidebar.py`, `file_list.py`, `library_window.py`, `editor.py`, `format_toolbar.py`, `viewer_tabs.py`, `media_tabs.py`, `sheet_tab.py` | Regression checks, not an invitation to restyle. Existing library layout, system accent, Paper Mode, viewers, spreadsheet and text formatting survive. |
| [ ] | Top-level windows and platform settings | `main_window.py`, existing owners/controllers, any narrowly scoped new native adapter | Live appearance/accessibility changes, focus/selection preserved, native frame intact and no dangling signals after close. |
| [ ] | Documents and output | `markdown_writer.py`, `export_html.py`, `export_pdf.py`, `printing.py`, existing publication builders | Presentation cannot mutate saved/exported/printed content or signed events. Test the existing output paths; no theme-specific document metadata. |

## How to maintain this checklist

At implementation start, compare upstream changes since the recorded revision. Update file ownership before editing, without dropping inventory IDs. For each row, append the completed commit, applicable automated checks and screenshot/state evidence, or a preservation verification. A file can support several rows, and one row can need several files. Do not equate file count with screen coverage.
