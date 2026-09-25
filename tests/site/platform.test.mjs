// SPDX-FileCopyrightText: 2026 rinbal
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Tests for the install guide's pure helpers. Run with:
//     node --test tests/site/*.test.mjs
// (tests/test_install_site.py runs this too when Node is installed.)

import assert from "node:assert/strict";
import { test } from "node:test";

import { formatSize } from "../../site/install/downloads.js";
import { detectMacArch, detectSystem, readContext } from "../../site/install/platform.js";

const UA = {
  mac: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/19.0 Safari/605.1.15",
  windows: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36",
  ubuntu: "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:142.0) Gecko/20100101 Firefox/142.0",
  chromebook: "Mozilla/5.0 (X11; CrOS x86_64 16000.0.0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36",
  iphone: "Mozilla/5.0 (iPhone; CPU iPhone OS 19_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
  android: "Mozilla/5.0 (Linux; Android 16) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Mobile Safari/537.36",
};

test("detects each desktop system from the browser", () => {
  assert.equal(detectSystem({ userAgent: UA.mac, platform: "MacIntel" }), "mac");
  assert.equal(detectSystem({ userAgent: UA.windows, platform: "Win32" }), "windows");
  assert.equal(detectSystem({ userAgent: UA.ubuntu, platform: "Linux x86_64" }), "linux");
  assert.equal(detectSystem({ userAgent: UA.chromebook, platform: "Linux x86_64" }), "linux");
  assert.equal(detectSystem({ userAgent: "", userAgentData: { platform: "macOS" } }), "mac");
});

test("phones open on the desktop system their owners most likely use", () => {
  assert.equal(detectSystem({ userAgent: UA.iphone, platform: "iPhone" }), "mac");
  assert.equal(detectSystem({ userAgent: UA.android, platform: "Linux armv8l" }), "windows");
});

test("an unknown browser gets a sensible default", () => {
  assert.equal(detectSystem({}), "windows");
  assert.equal(detectSystem(undefined), "windows");
});

test("the app's link wins over detection", () => {
  const nav = { userAgent: UA.windows, platform: "Win32" };
  assert.deepEqual(readContext("?os=mac&arch=x86_64&update=3.3", nav), {
    system: "mac", arch: "x86_64", package: "", update: "3.3",
  });
  assert.deepEqual(readContext("?os=linux&package=appimage", nav), {
    system: "linux", arch: "", package: "appimage", update: "",
  });
});

test("unknown or malformed query values are ignored", () => {
  const nav = { userAgent: UA.ubuntu, platform: "Linux x86_64" };
  assert.deepEqual(readContext("?os=beos&arch=ppc&package=rpm&update=<b>3</b>", nav), {
    system: "linux", arch: "", package: "", update: "",
  });
  assert.equal(readContext("?update=3.3.1", nav).update, "3.3.1");
  assert.equal(readContext("?update=3.3-beta", nav).update, "");
});

test("the Mac chip comes from Chromium's high-entropy hints when offered", async () => {
  const hints = (architecture) => ({ userAgentData: { getHighEntropyValues: async () => ({ architecture }) } });
  assert.equal(await detectMacArch(hints("arm")), "arm64");
  assert.equal(await detectMacArch(hints("x86")), "x86_64");
  assert.equal(await detectMacArch(hints("")), "");
  assert.equal(await detectMacArch({}), "");
  const refusing = { userAgentData: { getHighEntropyValues: async () => { throw new Error("no"); } } };
  assert.equal(await detectMacArch(refusing), "");
});

test("file sizes read the way Finder and Explorer show them", () => {
  assert.equal(formatSize(48_976_225), "49 MB");
  assert.equal(formatSize(41_545_251), "42 MB");
  assert.equal(formatSize(512_000), "512 KB");
  assert.equal(formatSize(10), "1 KB");
});
