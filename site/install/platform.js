// Works out which system the install guide should open on.
//
// Pure functions with no DOM access, so tests/site/platform.test.mjs can run
// them under `node --test`. The app links here with exact answers in the
// query string (see update_flow.guide_url); a plain visit falls back to what
// the browser reports.

export const SYSTEMS = ["mac", "windows", "linux"];
const ARCHES = ["arm64", "x86_64"];
const PACKAGES = ["deb", "appimage"];
const VERSION = /^\d+(\.\d+){0,3}$/;

/** The visitor's system from what the browser reports. Phones get the
 *  desktop system their owners most likely use. */
export function detectSystem(nav) {
  const platform = String(nav?.userAgentData?.platform || nav?.platform || "").toLowerCase();
  const agent = String(nav?.userAgent || "").toLowerCase();
  if (/iphone|ipad|ipod/.test(agent)) return "mac";
  if (agent.includes("android")) return "windows";
  if (platform.startsWith("win") || agent.includes("windows")) return "windows";
  if (platform.startsWith("mac") || agent.includes("macintosh")) return "mac";
  if (/linux|x11|cros/.test(`${platform} ${agent}`)) return "linux";
  return "windows";
}

/** Everything the page needs to know before it renders. Unknown or
 *  malformed query values are ignored rather than trusted. */
export function readContext(search, nav) {
  const params = new URLSearchParams(search || "");
  const pick = (name, allowed) => (allowed.includes(params.get(name)) ? params.get(name) : "");
  const update = params.get("update") || "";
  return {
    system: pick("os", SYSTEMS) || detectSystem(nav),
    arch: pick("arch", ARCHES),
    package: pick("package", PACKAGES),
    update: VERSION.test(update) ? update : "",
  };
}

/** Apple silicon or Intel, when the browser says (Chromium only). Safari and
 *  Firefox don't, so the page keeps its Apple silicon default and shows the
 *  Intel download as a second link. */
export async function detectMacArch(nav) {
  const data = nav?.userAgentData;
  if (!data || typeof data.getHighEntropyValues !== "function") return "";
  try {
    const { architecture } = await data.getHighEntropyValues(["architecture"]);
    if (architecture === "arm") return "arm64";
    if (architecture === "x86") return "x86_64";
  } catch {
    // The browser declined to say.
  }
  return "";
}
