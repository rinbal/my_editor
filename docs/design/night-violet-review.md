# Night Violet visual reference and baseline

The intended result is the **Night Violet** direction in the supplied HTML. The images below are labeled by origin: current upstream Qt rendering or HTML design reference. None is a screenshot of an implemented Night Violet application.

The [implementation plan](night-violet-plan.md) defines the work and acceptance criteria. The [surface checklist](night-violet-surfaces.md) tracks all 64 original entries and shared integration points.

## Source and capture conditions

- Application baseline: `rinbal/my_editor` `dev`, commit `57a023e5aa660749ef842e4fd8c277e33045c217`, fetched October 22, 2026.
- Reference: unchanged `MyEditor_Nostr_Design.html`, SHA-256 `6df3f2759381666459b32fa701e2a73812bbfff4673671aabb1938d3f070cebc`.
- [Download the original HTML](https://raw.githubusercontent.com/DoktorShift/my_editor/94b97eb7bbf39c8cb547d93a7b64fbf41b16dfe8/night-violet-plan/reference.html). Open it locally and select Night Violet in the “Every Nostr surface” direction control. The old Horizon recommendation is superseded by the chosen direction.
- HTML captures: Chromium headless shell 1248, viewport 1600×1100 CSS pixels, device scale factor 2; the page's own controls select Light/Dark and Night Violet. Each image captures an individual mockup. The HTML was not restyled for the screenshots.
- Qt captures: the existing app components at the recorded revision, Fusion `AppStyle`, offscreen rendering, example data from `tests.test_imports_window.controller_for` / `fill` and `tests.test_publish_sheets.article`. No live relay, signer, payment or production account was used.
- Current UI uses English fixtures; the supplied HTML uses German examples. Data and dimensions differ, so these are visual baselines and targets, not pixel-diff comparisons. Final implementation comparisons must use matched content and logical sizes.
- [Capture manifest and image hashes](https://github.com/DoktorShift/my_editor/blob/94b97eb7bbf39c8cb547d93a7b64fbf41b16dfe8/night-violet-plan/manifest.json). Assets live separately from the application branch and all links are pinned to an immutable commit.

Offscreen Qt captures show the client area. They do not establish native title-bar styling, window shadows, real display scaling, platform accessibility integration or VoiceOver behavior. Those require the native checks in the implementation plan.

## Current upstream application

### Imports

| Current light appearance | Current dark appearance |
| --- | --- |
| ![Current upstream Imports window in light appearance with example posts](https://raw.githubusercontent.com/DoktorShift/my_editor/94b97eb7bbf39c8cb547d93a7b64fbf41b16dfe8/night-violet-plan/baseline-imports-light.png) | ![Current upstream Imports window in dark appearance with example posts](https://raw.githubusercontent.com/DoktorShift/my_editor/94b97eb7bbf39c8cb547d93a7b64fbf41b16dfe8/night-violet-plan/baseline-imports-dark.png) |

### Publish as Article

| Current light appearance | Current dark appearance |
| --- | --- |
| ![Current upstream Publish as Article sheet in light appearance](https://raw.githubusercontent.com/DoktorShift/my_editor/94b97eb7bbf39c8cb547d93a7b64fbf41b16dfe8/night-violet-plan/baseline-publishing-light.png) | ![Current upstream Publish as Article sheet in dark appearance](https://raw.githubusercontent.com/DoktorShift/my_editor/94b97eb7bbf39c8cb547d93a7b64fbf41b16dfe8/night-violet-plan/baseline-publishing-dark.png) |

## Night Violet targets from the HTML

### Imports

Violet frame, sidebar and post list form one window; the article reader stays neutral. Active selection, checked posts and quiet navigation selection have distinct treatments.

| Reference light appearance | Reference dark appearance |
| --- | --- |
| ![Night Violet HTML reference for Imports in light appearance](https://raw.githubusercontent.com/DoktorShift/my_editor/94b97eb7bbf39c8cb547d93a7b64fbf41b16dfe8/night-violet-plan/imports-light.png) | ![Night Violet HTML reference for Imports in dark appearance](https://raw.githubusercontent.com/DoktorShift/my_editor/94b97eb7bbf39c8cb547d93a7b64fbf41b16dfe8/night-violet-plan/imports-dark.png) |

### Editor and Drafts

The document and local library retain their appearance. Nostr-owned tabs, account chip, Drafts panel and mention picker establish the violet context.

| Reference light appearance | Reference dark appearance |
| --- | --- |
| ![Night Violet HTML reference for the editor and Drafts panel in light appearance](https://raw.githubusercontent.com/DoktorShift/my_editor/94b97eb7bbf39c8cb547d93a7b64fbf41b16dfe8/night-violet-plan/editor-light.png) | ![Night Violet HTML reference for the editor and Drafts panel in dark appearance](https://raw.githubusercontent.com/DoktorShift/my_editor/94b97eb7bbf39c8cb547d93a7b64fbf41b16dfe8/night-violet-plan/editor-dark.png) |

### Publish as Article

The sheet uses violet material and restrained action emphasis. Its preview content remains neutral, and its account row identifies who publishes.

| Reference light appearance | Reference dark appearance |
| --- | --- |
| ![Night Violet HTML reference for Publish as Article in light appearance](https://raw.githubusercontent.com/DoktorShift/my_editor/94b97eb7bbf39c8cb547d93a7b64fbf41b16dfe8/night-violet-plan/publishing-light.png) | ![Night Violet HTML reference for Publish as Article in dark appearance](https://raw.githubusercontent.com/DoktorShift/my_editor/94b97eb7bbf39c8cb547d93a7b64fbf41b16dfe8/night-violet-plan/publishing-dark.png) |

### Media Library

Violet belongs to the frame, selection and progress; photographs and thumbnails retain their colors. Private and failed states still have their own marks and words.

| Reference light appearance | Reference dark appearance |
| --- | --- |
| ![Night Violet HTML reference for Media Library in light appearance](https://raw.githubusercontent.com/DoktorShift/my_editor/94b97eb7bbf39c8cb547d93a7b64fbf41b16dfe8/night-violet-plan/media-light.png) | ![Night Violet HTML reference for Media Library in dark appearance](https://raw.githubusercontent.com/DoktorShift/my_editor/94b97eb7bbf39c8cb547d93a7b64fbf41b16dfe8/night-violet-plan/media-dark.png) |

### EINUNDZWANZIG membership

The account context is violet, the brand is orange, and the card and service tiles combine them. The full HTML also includes the orange payment view and black-on-white QR treatment.

| Reference light appearance | Reference dark appearance |
| --- | --- |
| ![Night Violet HTML reference for EINUNDZWANZIG membership in light appearance](https://raw.githubusercontent.com/DoktorShift/my_editor/94b97eb7bbf39c8cb547d93a7b64fbf41b16dfe8/night-violet-plan/membership-light.png) | ![Night Violet HTML reference for EINUNDZWANZIG membership in dark appearance](https://raw.githubusercontent.com/DoktorShift/my_editor/94b97eb7bbf39c8cb547d93a7b64fbf41b16dfe8/night-violet-plan/membership-dark.png) |

## Baseline verification

The following command was run against the unchanged upstream revision using the existing local Python environment:

```sh
python -m pytest -q tests/test_theme.py tests/test_appearance.py \
  tests/test_dialog_keyboard.py tests/test_window_accessibility.py \
  tests/test_save_destinations.py tests/test_publish_sheets.py \
  -o faulthandler_timeout=60
```

The first run reported **121 passed, 1 failed**. The failure was `test_every_control_of_the_document_window_has_a_name`: its child process exited without the expected result while sandboxed. Repeating `tests/test_window_accessibility.py` outside the sandbox reported **1 passed**. All 122 selected tests therefore have passing baseline results across those runs; the initial failure is recorded rather than treated as an application regression or a single clean full-suite run.

This planning pass did not run the entire test suite or platform CI. These results validate a limited current baseline, not an implementation that does not yet exist. Native screenshot comparison, increased-contrast integration, assistive technology and Windows/Linux visual review remain implementation acceptance work.

## Implementation review record

For the implementation PR, add matched before/after application captures for each screen group and record:

- Upstream base and implementation commit, OS/Qt version, appearance, scale and fixture.
- Inventory IDs covered and any missing/error/loading state captures.
- Contrast adjustments relative to the immutable reference, including the final composite colors.
- Native title-bar differences or other deliberate departures, with rationale.
- Automated and manual checks actually completed, plus any remaining limitations.

Do not relabel these HTML images as finished application output or check off inventory entries using reference screenshots alone.
