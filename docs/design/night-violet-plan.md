# Night Violet implementation plan

Nostr windows and controls should be immediately recognizable while MyEditor remains a comfortable local editor. **Night Violet from `MyEditor_Nostr_Design.html` is the selected visual reference.** Implement its violet surfaces, restrained glow, accent hierarchy, and EINUNDZWANZIG treatment through the existing application components.

This is a planning PR. It changes no application behavior. The future implementation is one complete PR, divided into reviewable commits, with every inventory entry accounted for.

- Baseline: [`rinbal/my_editor`, `dev`, `57a023e5aa660749ef842e4fd8c277e33045c217`](https://github.com/rinbal/my_editor/tree/57a023e5aa660749ef842e4fd8c277e33045c217), fetched October 22, 2026.
- [Affected surfaces and file map](night-violet-surfaces.md): all 64 original inventory entries, plus cross-cutting integration work.
- [Screenshots and verification record](night-violet-review.md): current upstream examples and Night Violet reference captures, clearly distinguished.
- [Original HTML reference](https://github.com/DoktorShift/my_editor/blob/94b97eb7bbf39c8cb547d93a7b64fbf41b16dfe8/night-violet-plan/reference.html): download and open locally; select Night Violet under “Every Nostr surface.” Its historical recommendation for Horizon is superseded by the Night Violet decision.

## Scope and invariants

Violet means the interface belongs to the Nostr account workflow. It does **not** mean that a specific item has already been uploaded, signed, or published. Imports may still be local; upload badges and existing status words remain authoritative.

The ordinary editor, local library, local file tabs, viewers, spreadsheets, printing, and exports retain their existing behavior and appearance. Nostr remains opt-in through its existing entry points. Account creation and connection windows can identify themselves as Nostr before an account exists; this must not reveal Nostr controls throughout the editor.

Content stays neutral: writing pages, article bodies, publication previews, photographs, avatars, profile cards, and QR codes. Preserve the user's writing font and document formatting. Color changes must not alter document formats, undo history, saved bytes, event construction, keys, relay traffic, upload timing, subscriptions, permissions, or payment behavior.

Keep existing layouts, component ownership, accessible names, shortcuts, focus restoration, cancellation, and sheet modality. Adapt spacing where needed to match the reference and fit real content. The mockup's sample words and data are illustrative, not a replacement for the application's translated, state-dependent copy.

## Apple guidance translated into implementation requirements

| Guidance | Requirement for this change | Evidence at review |
| --- | --- | --- |
| [Color](https://developer.apple.com/design/human-interface-guidelines/color) | Give colors consistent roles; provide light, dark, and increased-contrast treatments. Violet controls must remain distinguishable from violet surfaces. | Color-role table, measured pairs and state captures. |
| [Accessibility](https://developer.apple.com/design/human-interface-guidelines/accessibility) | Pair color with labels, shape, selection marks, or the relay mark. Preserve keyboard access and screen-reader meaning. Target at least 4.5:1 for ordinary small text and 3:1 for essential control boundaries and icons. | Contrast checks, keyboard exercises, Qt accessibility tests and native assistive-technology inspection. |
| [Buttons](https://developer.apple.com/design/human-interface-guidelines/buttons) | Emphasize the primary action, keep secondary controls quiet, include a pressed state, and never promote a destructive action to the default. | Default/focus/pressed/disabled/destructive state matrix. |
| [Dark Mode](https://developer.apple.com/design/human-interface-guidelines/dark-mode) | Honor the app's existing Automatic/Light/Dark selection and system appearance changes; preserve clear separation of foreground and background. | Switch appearance with windows, sheets, previews, and popovers open. |
| [Materials](https://developer.apple.com/design/human-interface-guidelines/materials) | Use depth to explain structure and keep effects restrained. Preserve neutral content and legibility beneath any decorative glow. | Reference comparisons, increased-contrast and reduced-transparency inspection. |
| [Designing for macOS](https://developer.apple.com/design/human-interface-guidelines/designing-for-macos) | Preserve desktop density, flexible windows, menu commands, keyboard workflows, and clear active/inactive states. | Resizing, multiple windows, keyboard-only use and native chrome review. |

These are engineering acceptance requirements, not a claim that the current HTML or Qt rendering already meets them. Use familiar desktop controls and system fonts on each supported platform. Continue the existing cross-platform icon approach; no new SF Symbols dependency.

## Visual specification from the HTML

The `--n-*` and `--v-*` variables describe the application. The presentation page's `--paper`, `--wall`, serif headings, and desktop staging shadows are not application design tokens. Retain the app's current content typography and native window decorations.

### Surface and accent roles

| HTML role | Light | Dark | Application use |
| --- | --- | --- | --- |
| `n-chrome` | `#F3EEFB` | `#1E1828` | Nostr toolbar/header background |
| `n-side` | `#EEE7F9` | `#201A2B` | Nostr navigation/sidebar |
| `n-list` | `#FBF9FE` | `#1B1623` | Nostr list background |
| `n-panel` | `#F5F0FC` | `#1E1828` | Nostr panel and dialog body |
| `n-text` | `#1F1A2B` | `#F0EBF9` | Primary interface text |
| `n-text2` | `#665E7A` | `#ADA4C1` | Readable secondary text |
| `n-text3` | `#978FAA` | `#7E7594` | Reference tertiary tone; requires role-specific contrast adjustment |
| `n-field` | `#FFFFFF` | `rgba(220,200,255,.065)` | Fields; resolve alpha against the actual parent |
| `n-ctl` | `#FFFFFF` | `rgba(220,200,255,.085)` | Secondary controls |
| `n-line` | `rgba(92,52,172,.11)` | `rgba(208,184,255,.10)` | Decorative separators, not sufficient alone for control recognition |
| `n-ctl-line` | `rgba(92,52,172,.20)` | `rgba(208,184,255,.17)` | Reference control edge; strengthen when contrast requires it |
| `n-seg-on` | `#FFFFFF` | `#3D3250` | Selected segmented-control surface |
| `n-pop` | `#FFFFFF` | `#272033` | Popover surface |
| `n-hover` | `rgba(92,52,172,.05)` | `rgba(208,184,255,.06)` | Quiet hover treatment |
| `n-tab` | `#EEE6FA` | `#261E33` | Whole Nostr-owned tab |
| `v-icon` | `#7136DD` | `#B89CFF` | Relay mark and Nostr interface icons |
| `v-ink` | `#5B28BE` | `#CDB9FF` | Nostr accent text |
| `v-sel` | `#7136DD` | `#6A43D8` | Active selection |
| `v-btn` | `#7136DD` | `#6E40E2` | Default action fill |
| `v-btn-hi` | `#8B55F0` | `#8A5CF5` | Default action gradient highlight |
| `v-soft` | `rgba(113,54,221,.10)` | `rgba(150,112,255,.17)` | Chips and quiet contextual fills |
| `v-soft-line` | `rgba(113,54,221,.24)` | `rgba(172,138,255,.32)` | Chip edge |
| `v-ring` | `rgba(113,54,221,.32)` | `rgba(160,120,255,.5)` | Reference ring, to be checked on both filled and plain controls |
| `glow` | `rgba(140,92,255,.20)` | `rgba(140,92,255,.26)` | One static violet light source |

Night Violet is also a light appearance. It must not force a light-mode user into dark windows.

The reference uses 13px system interface text, 18px action icons with 1.5px strokes, a 14px relay mark, 28px controls, 7–8px control radii, and approximately 31px sidebar rows. Treat these as visual proportions in Qt logical coordinates; do not globally replace the existing font metrics with CSS pixel sizes. Use content-based height and width, preserve localization, and inspect at 1× and 2× scaling.

The window glow is a radial gradient at the upper left, nominally 560×380, fading by 72%; panels use 420×260 at the upper right; popovers use 300×140 at the upper left. Paint these as static decoration in the owning surface, clipped to Nostr chrome. Do not intercept input, create animation timers, tint child pictures, or redraw whole windows for an upload tick.

The reference's primary buttons and active selections have a subtle top-to-bottom violet gradient and a fine highlight. Preserve that character while checking contrast over **both endpoints and the composited glow**, including translucent selected-row text. Secondary controls remain quiet. The HTML does not define a complete interaction-state specification: add pressed, focused, disabled, inactive and error states using the existing behavior.

### Contrast adjustments that need explicit review

Measured using sRGB relative luminance against the flat `n-panel` background:

| Pair | Light | Dark | Consequence |
| --- | --- | --- | --- |
| `n-text3` on `n-panel` | 2.75:1 | 3.99:1 | Do not use unchanged for ordinary readable small labels. |
| `n-text2` on `n-panel` | 5.44:1 | 7.28:1 | Suitable starting role for secondary text on that surface. |
| White on solid `v-btn` | 6.41:1 | 6.01:1 | Solid endpoint passes; does not prove the complete gradient passes. |

Use the existing `theme.contrast_ratio()` for implementation checks. Resolve translucent colors against each real surface. Keep the reference hue and hierarchy while adjusting only failing roles. Record exact before/after values and screenshots in the implementation PR. Disabled text can be quiet but should remain understandable; essential hints and placeholders are not disabled content.

### EINUNDZWANZIG

Keep brand/payment orange `#F7931A`; readable amount text is `#A85A00` in light and `#FFB561` in dark. Use the HTML's dark `#16131C` card base with violet and orange radial lights, and violet-to-orange service icon tiles. Add the orange glow in the opposite corner of the membership window. Account actions stay violet; payment-context actions use orange with dark labels. QR code pixels and quiet zones remain black and white. Preserve existing paid/pending/expired/error semantics and check marks.

## Integration with the existing code

### Maintainability and future visual changes

Keep one semantic set of appearance values for Nostr, resolved by light/dark and accessibility state. Consumers ask for roles such as panel background, readable secondary text, selected row, focus ring or default action, rather than copying hex values or checking whether a color is purple. Keep membership orange/dusk as a small explicit specialization of those roles.

Use the same resolved values for QPalette, QSS and QPainter. Keep surface painting separate from widget behavior, so changing a palette or reducing the glow does not touch publishing, accounts, storage or document code. A small immutable value object or the existing token-helper convention is sufficient; avoid a new general-purpose theming framework, widget replacement hierarchy, runtime plugin registry or user-facing theme picker.

Future changes should have these edit boundaries:

| Future adjustment | Intended place to change |
| --- | --- |
| Violet hue, text contrast or light-mode strength | Central Nostr role values and their contrast checks |
| Glow strength, extent or removal | Shared surface-painting helper, not every window |
| Button radius, highlight or pressed state | Shared control styling used by Nostr consumers |
| Relay mark artwork | One vector/icon definition with size and theme-aware rendering |
| Membership orange/dusk treatment | Membership role specialization and shared card/tile painting |
| A new Nostr screen | Explicitly opt into the existing surface/context rule and add its inventory/tests |

Preserve existing public helper defaults so unrelated callers remain neutral. Document new context arguments, their ownership and how to restore a neutral child. Add behavior checks that substitute an alternate set of role values and confirm representative QSS and painted widgets update without changing their actions or leaking into adjacent local widgets. Each screen should contain only its layout-specific styling; duplicated colors, gradients and state rules are review findings.

### 1. Scoped theme roles

Extend [theme.py](../../theme.py) beside `dialog_stylesheet()`, `accent_button_css()`, focus helpers and palette helpers. Keep current defaults for existing callers. Introduce explicit Nostr roles through a small optional theme/context argument or dedicated helper, selected after checking call sites. Do not replace the application's global accent or change `constants.py` local-editor colors.

Derive a scoped `QPalette` for Nostr containers so standard Qt controls and custom delegates agree. Define active, inactive, and disabled groups. Apply corresponding stylesheet roles for widgets that already use QSS. Reset neutral content children explicitly. Check popup ownership: menus, tooltips, combo popups and detached previews may be top-level widgets rather than children that inherit the intended palette.

Keep the stylesheet-polishing guard in `theme.py`; it prevents stale styles after widget destruction. Preserve `AppStyle` checkbox/radio rendering and button ordering. Reuse existing contrast helpers rather than introducing another color engine.

### 2. Shared components and lifecycle

- [assistant.py](../../nostr/ui/assistant.py): shared account/profile/membership/import sheets; style the window, fields, step badges, links, progress and action row. Enumerate subclasses before making its Nostr scope implicit.
- [publish_sheet.py](../../nostr/ui/publish_sheet.py): retain `make_sheet()`, sizing, scroll/focus helpers, account row, people row and status line. Keep the article card and editable note text neutral.
- [drafts_common.py](../../nostr/ui/drafts_common.py) and [preview_parts.py](../../nostr/ui/preview_parts.py): adapt shared colors and painted elements without duplicating the preview/reader system. Keep article content neutral and preserve keyboard and accessibility behavior.
- [grouped_list.py](../../nostr/ui/grouped_list.py): share row, notice and usage-bar treatment with assistants; membership provides its orange/dusk roles explicitly.
- [main_window.py](../../main_window.py): extend `_set_theme()` and existing owners' `apply_theme()` / `set_dark()` methods. Top-level assistants, Media Library and detached windows need lifecycle-aware updates as well. Keep appearance changes on the GUI thread; disconnect deleted windows and avoid repeated signal connections.

A theme change must update palettes, QSS, painted icons, cached pixmaps, media badges and custom list delegates together, preserving focus, scroll position, selection, unsaved form fields and any running operation. Account switches and sign-out must clear stale account identity and contextual styling without creating new network requests.

### 3. Custom-painted surfaces

Imports uses `QMainWindow`, its own stylesheet, and palette-driven sidebar/post delegates; Media Library uses `QDialog` and `TileDelegate`. They are not covered by `AssistantWindow`. Update their local palettes and painting paths deliberately.

Preserve the difference between the selected item, checked import items, a selected row in an unfocused list, and keyboard focus. Keep errors and upload failures recognizable. Images, source favicons and avatars retain their own pixels. Clear or re-key icon caches on appearance and scope changes. Cache decorative painting if needed; do not add work to every document repaint or large-list row beyond existing rendering needs.

### 4. Mixed surfaces and ownership

| Surface | Required boundary |
| --- | --- |
| Save destination | Keep the dialog/local card neutral. Tint the Nostr card; default Save follows the currently selected destination. Switching back restores local styling. |
| Editor tabs | Derive Nostr ownership from existing tab/workspace/draft state, not filename, extension, translated text, or merely being signed in. Cover restore, detach/copy, closing, reordering and account mismatch. A local article with metadata remains a local tab until its existing state says otherwise. |
| Media player/viewers | Pass explicit origin context from callers. `media_tabs.py` plays local files using Nostr UI components; module location or an internal cached local URL cannot determine Nostr ownership. |
| Document notices | `DocumentNotice` serves local reload, deletion, recovery and draft conflicts. Only Nostr notices gain violet; caution/error semantics take precedence. |
| Alerts | Add an explicit Nostr presentation choice at Nostr call sites. Global updates, local file warnings and document-password alerts retain current styling. Preserve destructive/default roles and Return/Escape behavior. |
| Status bar | Associate Nostr styling with message origin, not message wording. Restore neutral styling when a message expires or a local message replaces it. Keep the permanent local counters and unrelated notices neutral. |
| Settings and welcome | Mark existing Nostr rows/context without tinting the whole page or introducing new opt-in surfaces. Preserve visibility rules. |

The relay mark is the one from the HTML, rendered through the existing vector/painting conventions at small sizes. Pair it with existing titles or words. It is contextual identification, not another button or tab stop. The plan uses the supplied mark; alternative branding is outside this implementation.

### 5. Native window chrome

The HTML simulates macOS title bars, traffic lights, rounding and shadows. Qt Fusion styles client controls; it does not guarantee that a stylesheet tints the operating system's title bar. Resolve this early on one representative window.

Inspect supported macOS/Qt integration for the header/title-bar appearance while preserving the native frame, window buttons, dragging, resize regions, full-screen behavior and accessibility. Use a small isolated platform adapter only if required and supported; check Windows/Linux fallbacks. Do not replace the frame with a hand-built imitation merely to match a screenshot.

The full Night Violet frame remains the target. If a platform cannot reproduce a reference detail safely, document the exact limitation and show the closest native-compatible result for review. Do not silently reduce the design to violet buttons or declare chrome parity based on offscreen captures.

### 6. Accessibility settings and reduced effects

Supply increased-contrast color variants and remove decorative glow where it would weaken legibility. Test actual system preferences, not only a manually selected palette. Verify what the pinned PySide6/Qt version exposes; if an accessibility preference needs a native hook, isolate it with platform guards and a conservative fallback. An opaque, static treatment must remain complete and readable without blur, transparency or animation. Do not add new animated effects; preserve existing progress feedback and cancellation.

## One rule per surface type

| Type | Night Violet treatment |
| --- | --- |
| Full Nostr windows | Violet frame/client surfaces and one glow; neutral pictures and reading content. |
| Sheets/dialogs | Violet body with plain fields/content and a clear primary action. |
| Side panels | Violet region beside the neutral editor; panel header, list, and footer form one composition. |
| Popovers | Violet surface and restrained depth; preserve placement, dismissal and keyboard interaction. |
| Inline elements | Violet mark/ink/chips or tab fill only; no document-page wash. |
| Banners/status | Explicit Nostr context, words and mark; failures keep their error treatment. |
| Alerts | Nostr mark with the app identity, violet nondestructive default, red destructive actions. |
| Mixed | Tint only the Nostr part, based on current context. |
| System-owned | Preserve menu bar, Dock/taskbar badge and native file dialogs. |
| EINUNDZWANZIG | Violet context, orange brand/payment roles, dusk card and service tiles. |

## Implementation sequence and commit boundaries

Each phase ends with relevant tests and actual Qt screenshots. Complete all phases before marking the implementation PR ready; the plan PR does not implement them.

1. **Baseline and coverage.** Fetch upstream `dev` again, record its revision, reconcile any subsequent changes with the inventory, create an isolated branch, and capture representative current UI with deterministic fixtures. Preserve unrelated work. Record test/environment failures separately.
2. **Theme foundation and chrome feasibility.** Add scoped roles, light/dark/contrast variants, relay mark and shared paint helpers. Prove no leakage into a local editor. Resolve native header behavior and document any necessary deviation before broad rollout.
3. **Representative windows.** Complete Imports, Publish as Article, Drafts beside the editor, and membership. These cover delegates, neutral content, sheets, mixed ownership, and the orange exception. Compare real rendering against the HTML at equivalent logical sizes. Refine the shared rules here.
4. **Complete screen coverage.** Apply the established rules to media, profile, account flows, note publishing, draft saving, article details, mentions/references, detached readers, import sheets and popovers. Tick each inventory entry only after its states are checked.
5. **Mixed contexts and lifecycle.** Complete tab ownership, save destination, local/Nostr media, alerts/notices/status, settings and account transitions. Exercise theme switches with several windows open and close/reopen cycles. Verify styles do not leak through shared objects.
6. **Precision pass.** Inspect alignment, optical icon size, hairlines, corners, hover/pressed/focus states, truncation, scrollbars, small screens, translations, inactive windows and scaling. Adjust failing contrast roles with recorded evidence. Confirm operation progress does not cause flicker, layout jumps or repaint loops.
7. **Integration and delivery.** Rebase/merge upstream changes carefully, rerun affected checks, run full three-platform CI, capture final screenshots and reconcile the inventory. Open one implementation PR against `rinbal/dev` with coherent commits and concrete validation results. No unrelated refactors or dependency upgrades.

## Verification and acceptance

### Automated checks

Extend existing tests for observable behavior; do not add tests that merely assert new helper names or duplicate a color dictionary.

- `tests/test_theme.py`, `test_check_box_contrast.py`: composited contrast, state distinction, neutral children and shared-widget scope. Preserve local system-accent behavior.
- `tests/test_appearance.py`: live light/dark changes and owners of detached/top-level windows.
- `tests/test_dialog_keyboard.py`, `test_accessibility.py`, `test_window_accessibility.py`, `test_keyboard_navigation.py`: names, focus order, Return/Escape, disabled actions and focus recovery.
- `tests/test_window_chrome.py`, `test_small_screen.py`, `test_window_layout.py`, `test_panel_layout.py`: window behavior, usable action rows, resizing and panel layout.
- Existing screen suites named in the inventory: both appearance modes, local/Nostr boundaries and representative empty/loading/error/success cases.
- `tests/test_nostr_menu.py`, `test_accounts.py`, `test_account_controller.py`, `test_save_destinations.py`, `test_draft_routing.py`, `test_workspace_restore.py`, `test_file_tabs.py`, `test_viewer_tabs.py`, `test_media_previews.py`: opt-in, switching, restore, ownership and local workflows.
- `python scripts/i18n.py check`; then `python -m pytest -q -o faulthandler_timeout=300` using `packaging/requirements-test.lock`. Keep the existing macOS/Linux/Windows matrix in `.github/workflows/tests.yml`.

### Visual and interaction matrix

Capture every changed inventory surface in Light and Dark at its normal size. For each shared component, capture normal, hover, pressed, keyboard focus, disabled, active selection, inactive selection and error appearances. Include empty, loading, populated, partial-success, failure and retry states where the underlying feature supports them.

Across representative windows, additionally check:

- Automatic appearance change while sheets, assistants, Media Library and detached previews remain open.
- System accent Blue and Purple, Increased Contrast, Reduce Transparency and Reduce Motion; grayscale identification.
- English and German, long filenames/account names, multiline errors, no account, local key and external signer, account switch and sign-out.
- Small supported window sizes, maximized/full-screen, 1×/2× displays and fractional Windows scaling. No clipped default actions, overlapping labels or broken scroll-to-focus.
- Tab/Shift-Tab, arrow navigation, Return, Escape, Space, context menus, keyboard-only dismissal and restoring the prior focus.
- Real macOS native frame and VoiceOver; Windows/Linux rendering and keyboard/accessibility smoke checks. Offscreen tests alone cannot validate these platform details.
- Local editor and library opened alongside Nostr windows. Local open/save, viewers, spreadsheets, export/print and undo retain their behavior and colors.
- Large Imports lists and media grids, upload progress, scrolling and repeated window creation. No new layout loops, unnecessary full-document repaints or growing signal/timer counts.

### Completion evidence

- [ ] Every inventory entry has a disposition and verification result, including preserved/data-only entries.
- [ ] All intended visual roles match the HTML; departures have a reason, exact scope and screenshot.
- [ ] Contrast checks cover gradients, alpha overlays, focus and real surfaces, not just solid token pairs.
- [ ] No violet leakage into local controls or content, before/after opt-in and account changes.
- [ ] UI changes preserve document data, persistence, network/signing and payment behavior.
- [ ] Relevant tests, translation checks and full platform CI are green; any pre-existing failures are clearly separated.
- [ ] The implementation PR contains real before/after application captures, alongside labeled references, and reports which native-platform checks actually ran.

## Reference handling and review

Keep the original HTML immutable as the design source. Store PR images separately on the fork's screenshot branch and link to immutable commits. Screenshots must use fixture/example data without secrets, real payment requests, private files or production account access. Review the coherent visual result and the shared code boundaries together; cosmetic matching is insufficient if behavior or local-editor isolation regresses.
