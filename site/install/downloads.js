// Points the download buttons at the latest release.
//
// packaging/site/build_site.py writes downloads.json at deploy time from the
// GitHub release, keyed like release_assets.py ("mac-arm64", "deb-amd64"...).
// Without it every download link keeps its fallback, the release page, so the
// guide still works when the file is missing or stale.

const TRUSTED_ORIGIN = "https://github.com/";

export async function loadDownloads(url = "downloads.json") {
  try {
    const response = await fetch(url, { cache: "no-cache" });
    if (!response.ok) return null;
    const data = await response.json();
    return data && typeof data.files === "object" ? data : null;
  } catch {
    return null;
  }
}

export function applyDownloads(doc, data) {
  const files = data.files;

  for (const link of doc.querySelectorAll("a[data-download]")) {
    const file = files[link.dataset.download];
    if (file && isTrusted(file.url)) link.href = file.url;
  }
  for (const meta of doc.querySelectorAll("[data-file-meta]")) {
    const file = files[meta.dataset.fileMeta];
    meta.textContent = file ? `${file.name} · ${formatSize(file.size)}` : "";
  }
  for (const code of doc.querySelectorAll("[data-command-file]")) {
    const file = files[code.dataset.commandFile];
    if (file) code.textContent = code.dataset.commandTemplate.replace("{name}", file.name);
  }
  if (data.version) {
    for (const el of doc.querySelectorAll("[data-latest-version]")) el.textContent = data.version;
    doc.querySelector("[data-latest]")?.removeAttribute("hidden");
  }
  fillChecksums(doc.querySelector("table[data-checksums]"), files);
}

export function formatSize(bytes) {
  const megabytes = Number(bytes) / 1_000_000;
  return megabytes >= 1 ? `${Math.round(megabytes)} MB` : `${Math.max(1, Math.round(Number(bytes) / 1000))} KB`;
}

function isTrusted(url) {
  return typeof url === "string" && url.startsWith(TRUSTED_ORIGIN);
}

function fillChecksums(table, files) {
  if (!table) return;
  const rows = Object.values(files)
    .filter((file) => file.sha256)
    .sort((a, b) => a.name.localeCompare(b.name));
  const body = table.tBodies[0];
  body.replaceChildren();
  for (const file of rows) {
    const row = body.insertRow();
    row.insertCell().textContent = file.name;
    row.insertCell().textContent = file.sha256;
  }
  table.classList.toggle("has-rows", rows.length > 0);
}
