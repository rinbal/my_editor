# 1. Nostr windows wear Night Violet

Status: accepted, 2026-10-23 (plan [#59](https://github.com/rinbal/my_editor/pull/59), implementation [#60](https://github.com/rinbal/my_editor/pull/60)).

## Context

MyEditor is a local editor first, and Nostr is optional ([decision 2](0002-nostr-is-opt-in.md)). Once someone uses Nostr, windows that act for their account (publishing, drafts, the Media Library, imports, membership) looked the same as the local editor. People could not tell at a glance whether a window would sign, upload or publish anything. Two directions were drawn in an HTML mockup: Horizon and Night Violet.

## Decision

Night Violet. Violet surfaces, a restrained glow and an accent hierarchy mark every window and control that belongs to the Nostr account workflow. Horizon, the mockup's earlier recommendation, is superseded.

- Violet means "this belongs to your Nostr account", never "this was uploaded, signed or published". Status words and badges keep saying that.
- Content stays neutral: writing pages, article bodies, previews, photographs, avatars, profile cards and QR codes.
- The local editor, local files, viewers, spreadsheets, printing and exports keep their look.
- Light, dark, increased contrast and reduced transparency are all honoured; no new dependency.

## Consequences

- One module, `nostr/ui/appearance.py`, resolves the colour roles for palettes, stylesheets and painters. A window opts in by naming its owner; nothing is violet by accident. See [Look and theme](../develop/architecture.md#look-and-theme).
- New Nostr windows must declare their owner, or they stay neutral.
- The plan, the surface checklist, the visual review and the acceptance record are kept in [the archive](../archive/night-violet-plan.md).
