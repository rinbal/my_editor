# The install guide (maintainers)

MyEditor ships without a paid Apple or Microsoft certificate, so macOS and
Windows stop every new user at a security prompt before any MyEditor code runs.
The install guide at **https://rinbal.github.io/my_editor/install/** walks
people through those prompts one step at a time, with a replica of each dialog
and the button to press highlighted. Software Update in the app uses the same
guide for installs that cannot replace themselves (macOS and the `.deb`).

This page explains how the guide is built, deployed, and kept accurate. End
users read [install.md](install.md) or the guide itself.

---

## Where things live

```
site/
  index.html             redirects the Pages root to install/
  install/
    index.html           all content: every step for every system, plus the dialog replicas
    guide.css            look and layout; the "Filters" section reads <html data-*>
    guide.js             entry point: reads the visitor's context, wires events
    platform.js          which system, chip, package and mode (pure, tested with Node)
    downloads.js         points buttons at the latest release (downloads.json)
    wizard.js            one step at a time: step list, Back and Next
packaging/site/build_site.py   builds _site/ and writes install/downloads.json
release_assets.py              file name -> key ("mac-arm64", "deb-amd64"...), shared with the updater
update_flow.py                 what Software Update tells each install to do, and the guide link
update_dialog.py               the Software Update window
.github/workflows/install-guide.yml   deploys to GitHub Pages
```

## How the page works

The page is plain HTML first. Without JavaScript it reads as a complete list of
steps for every system, and every download link points at the release page.
`guide.js` then turns it into a guide for one system, one step at a time.

What shows is steered by attributes on `<html>`, which CSS and `wizard.js` both
read:

| Attribute | Values | Set from |
|---|---|---|
| `data-mode` | `install`, `update` | `?update=<version>` in the link |
| `data-arch` | `arm64`, `x86_64` | `?arch=`, else Chromium's hints, else `arm64` |
| `data-package` | `deb`, `appimage` | `?package=`, else the Linux download the visitor clicks, else `deb` |

Elements carrying the same attribute show only when it matches, so a step or a
sentence can exist in one mode or for one package only. For example, the Mac
track has a **Quit MyEditor** step marked `data-mode="update"`.

The app builds its links with `update_flow.guide_url()`, for example
`install/?os=mac&arch=arm64&update=3.3`. It knows how it was installed, so it
says so instead of letting the page guess.

## Downloads

`build_site.py` asks the GitHub API for the latest release and writes
`install/downloads.json`, with each installer's name, URL, size and SHA-256
(GitHub's asset digest), keyed by `release_assets.asset_key()`. Download buttons
name the key they want in `data-download`. `tests/test_install_site.py` fails if
a button asks for a key the release does not have.

If you rename an installer in the packaging scripts, update the pattern in
`release_assets.py`. The updater and the guide both follow it.

## Deploying

One-time setup, by the repository owner: **Settings > Pages > Source: GitHub
Actions**.

After that, `.github/workflows/install-guide.yml` deploys:

- on every push to `main` that touches the site or its build,
- after every release: `build-installers.yml` dispatches it once the release is
  published, so the buttons point at the new files,
- on demand from the Actions tab (**Run workflow**).

## Previewing locally

```bash
python packaging/site/build_site.py --out _site            # with the latest release
python packaging/site/build_site.py --out _site --offline  # no network
python -m http.server --directory _site 8000
```

Then open http://localhost:8000/install/. Add `?update=3.3`, `?os=linux&package=appimage`
or `#mac-allow` to jump to a mode, a path, or a step.

## Keeping the replicas accurate

The replicas copy the operating systems' own wording, so people can match what
they see. Each macOS and Windows release can change a dialog. When a new
version ships (macOS every autumn, Windows feature updates), open a clean
machine or VM, install MyEditor with the guide, and compare each step:

| Step | Check on |
|---|---|
| Not Opened alert, Privacy & Security, confirmation | the newest macOS, and the oldest one MyEditor supports |
| Finder's Replace dialog (update mode) | the newest macOS |
| Edge's download warning, SmartScreen, the Inno Setup wizard | Windows 11 and Windows 10 |
| App Center warning, Files properties | the current Ubuntu LTS |

When wording changes, update the replica and the step text together, and the
matching steps in `update_flow.py`. Keep the page's own copy in Apple's style:
sentence-case headings, verbs on buttons, exact button names in bold, no "we",
and no em dashes.

Things to confirm on real machines after changes:

- The **Open Privacy & Security** button (`x-apple.systempreferences:`) opens
  System Settings from Safari and Chrome.
- A new macOS still shows **Open Anyway** for ad-hoc signed apps. CI checks
  the bundle's seal (`codesign --verify --deep --strict`), because a broken
  seal shows "damaged" with no way forward.
- An updated Mac app is blocked again on first open. The update-mode step
  offers **Skip to the end** in case it is not.

## Tests

```bash
QT_QPA_PLATFORM=offscreen python -m pytest tests/test_install_site.py tests/test_release_assets.py \
    tests/test_update_flow.py tests/test_update_dialog.py
node --test tests/site/*.test.mjs     # also run by test_install_site.py when Node is installed
```
