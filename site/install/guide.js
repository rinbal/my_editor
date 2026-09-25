// Entry point of the install guide: reads who is visiting, then hands the
// page to the Guide. See index.html for the page structure and
// docs/install-guide.md for how the site is built and deployed.

import { applyDownloads, loadDownloads } from "./downloads.js";
import { detectMacArch, readContext } from "./platform.js";
import { Guide } from "./wizard.js";

const doc = document;
const root = doc.documentElement;
const context = readContext(location.search, navigator);

applyContext(context);

const guide = new Guide(doc);
const [hashSystem, hashStep] = locationFromHash(location.hash);
guide.show(hashSystem || context.system, hashStep, { notify: false });
guide.onChange = (step) => history.replaceState(null, "", `${location.search}#${step.id}`);

addCopyButtons();
doc.addEventListener("click", onClick);
revealViewToggle();

loadDownloads().then((data) => data && applyDownloads(doc, data));
if (!context.arch) {
  detectMacArch(navigator).then((arch) => {
    if (arch) root.dataset.arch = arch;
  });
}

// -- setup --------------------------------------------------------------------

function applyContext({ update, arch, package: pkg }) {
  root.dataset.mode = update ? "update" : "install";
  if (arch) root.dataset.arch = arch;
  // Most Linux desktops run a Debian-family system, so the .deb path is the
  // default. A link from the app names its package, and then the other
  // download is hidden, because the app knows how it was installed.
  root.dataset.package = pkg || "deb";
  if (pkg) {
    for (const choice of doc.querySelectorAll(".choice")) choice.hidden = choice.dataset.package !== pkg;
  }
  if (update) {
    for (const el of doc.querySelectorAll("[data-update-version]")) el.textContent = update;
    doc.title = "Update MyEditor";
  }
}

/** "#mac-allow" opens that step; "#windows" opens that system. */
function locationFromHash(hash) {
  const target = hash.length > 1 ? doc.getElementById(decodeURIComponent(hash.slice(1))) : null;
  if (target?.classList.contains("track")) return [target.dataset.os, null];
  if (target?.classList.contains("step")) return [target.closest(".track").dataset.os, target.id];
  return [null, null];
}

function revealViewToggle() {
  const toggle = doc.querySelector('[data-action="toggle-view"]');
  toggle.hidden = false;
}

function addCopyButtons() {
  for (const box of doc.querySelectorAll(".command")) {
    const button = doc.createElement("button");
    button.type = "button";
    button.className = "copy";
    button.textContent = "Copy";
    box.append(button);
  }
}

// -- events -------------------------------------------------------------------

function onClick(event) {
  const target = event.target.closest(
    "[data-os-link], [data-nav], [data-go], [data-action], .command .copy, a[data-advance]",
  );
  if (!target) return;

  if (target.dataset.osLink) {
    event.preventDefault();
    guide.show(target.dataset.osLink);
  } else if (target.dataset.nav === "next") {
    guide.next({ focus: true });
  } else if (target.dataset.nav === "back") {
    guide.back({ focus: true });
  } else if (target.dataset.go) {
    guide.go(target.dataset.go, { focus: true });
  } else if (target.dataset.action === "toggle-view") {
    toggleView(target);
  } else if (target.classList.contains("copy")) {
    copyCommand(target);
  } else if (target.matches("a[data-advance]")) {
    advanceAfterDownload(target);
  }
}

/** A download link starts the download (its default action) and, in the
 *  step-by-step view, moves on to what to do with the file. */
function advanceAfterDownload(link) {
  if (link.dataset.choosePackage) {
    root.dataset.package = link.dataset.choosePackage;
    guide.render({ notify: false });
  }
  if (root.classList.contains("list-view") || link.closest(".step") !== guide.current) return;
  setTimeout(() => guide.next({ focus: true }), 400);
}

function toggleView(button) {
  const listView = root.classList.toggle("list-view");
  button.textContent = listView ? "Show one step at a time" : "Show all steps";
  guide.render({ notify: false });
}

async function copyCommand(button) {
  const code = button.parentElement.querySelector("code");
  try {
    await navigator.clipboard.writeText(code.textContent);
    flash(button, "Copied");
  } catch {
    // No clipboard access: select the text so a keyboard copy works.
    const range = doc.createRange();
    range.selectNodeContents(code);
    getSelection().removeAllRanges();
    getSelection().addRange(range);
    flash(button, "Selected");
  }
}

function flash(button, text) {
  button.textContent = text;
  setTimeout(() => {
    button.textContent = "Copy";
  }, 1600);
}
