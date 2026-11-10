"""Pins the managed R toolchain: discovery, official URLs, download safety.

NO test in this file touches the network or installs anything; urlopen is
faked where download behavior is under test. The defect classes guarded:
- non-official or non-HTTPS URLs slipping into the downloader,
- checksum mismatches being accepted,
- URL construction drifting per OS/arch,
- version discovery not falling back to the pinned versions,
- the knit environment losing RSTUDIO_PANDOC / R_LIBS_USER / PATH,
- pandoc archives with unexpected layouts silently yielding no binary.
"""

import hashlib
import io
import json
import os
import sys
import tarfile
import zipfile
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rmd_toolchain
from rmd_toolchain import (
    Cancelled,
    ToolchainError,
    _check_host,
    _download,
    discover_pandoc_version,
    discover_r_version,
    extract_pandoc_archive,
    find_pandoc,
    find_rscript,
    knit_environment,
    pandoc_archive_url,
    r_installer_url,
)


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """Point every toolchain path at a temp dir."""
    root = tmp_path / "toolchain"
    monkeypatch.setattr(rmd_toolchain, "TOOLCHAIN_DIR", str(root))
    return root


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #

def test_find_rscript_prefers_path(monkeypatch):
    monkeypatch.setattr(rmd_toolchain.shutil, "which",
                        lambda name: "/opt/bin/Rscript" if name == "Rscript" else None)
    assert find_rscript() == "/opt/bin/Rscript"


def test_find_rscript_missing(monkeypatch):
    monkeypatch.setattr(rmd_toolchain.shutil, "which", lambda name: None)
    monkeypatch.setattr(rmd_toolchain.os.path, "isfile", lambda p: False)
    assert find_rscript() is None


def test_find_pandoc_managed_beats_system(sandbox, monkeypatch):
    managed = sandbox / "pandoc" / "3.5" / "bin"
    managed.mkdir(parents=True)
    binary = managed / "pandoc"
    binary.write_text("", encoding="utf-8")
    rmd_toolchain._save_state({"pandoc": {"version": "3.5",
                                          "bin": str(binary)}})
    monkeypatch.setattr(rmd_toolchain.shutil, "which",
                        lambda name: "/usr/bin/pandoc")
    path, source = find_pandoc()
    assert path == str(managed)
    assert source == "managed"


def test_find_pandoc_falls_back_to_system(sandbox, monkeypatch):
    monkeypatch.setattr(rmd_toolchain.shutil, "which",
                        lambda name: "/usr/bin/pandoc")
    path, source = find_pandoc()
    assert path == "/usr/bin"
    assert source == "system"


# --------------------------------------------------------------------------- #
# Official URL construction
# --------------------------------------------------------------------------- #

def test_r_installer_urls_per_platform(monkeypatch):
    monkeypatch.setattr(rmd_toolchain.sys, "platform", "win32")
    assert r_installer_url("9.9.9") == \
        "https://cran.r-project.org/bin/windows/base/R-9.9.9-win.exe"
    # The pinned fallback uses the stable old/ path.
    pinned = rmd_toolchain.PINNED_R_VERSION
    assert f"/old/{pinned}/R-{pinned}-win.exe" in r_installer_url(pinned)

    monkeypatch.setattr(rmd_toolchain.sys, "platform", "darwin")
    monkeypatch.setattr(rmd_toolchain.platform, "machine", lambda: "arm64")
    assert r_installer_url("9.9.9") == \
        "https://cran.r-project.org/bin/macosx/big-sur-arm64/base/R-9.9.9-arm64.pkg"
    monkeypatch.setattr(rmd_toolchain.platform, "machine", lambda: "x86_64")
    assert "big-sur-x86_64" in r_installer_url("9.9.9")

    monkeypatch.setattr(rmd_toolchain.sys, "platform", "linux")
    with pytest.raises(ToolchainError):
        r_installer_url("9.9.9")


def test_pandoc_archive_urls_per_platform(monkeypatch):
    monkeypatch.setattr(rmd_toolchain.sys, "platform", "darwin")
    monkeypatch.setattr(rmd_toolchain.platform, "machine", lambda: "arm64")
    assert pandoc_archive_url("3.5").endswith("pandoc-3.5-arm64-macOS.zip")

    monkeypatch.setattr(rmd_toolchain.sys, "platform", "win32")
    assert pandoc_archive_url("3.5").endswith("pandoc-3.5-windows-x86_64.zip")

    monkeypatch.setattr(rmd_toolchain.sys, "platform", "linux")
    monkeypatch.setattr(rmd_toolchain.platform, "machine", lambda: "aarch64")
    assert pandoc_archive_url("3.5").endswith("pandoc-3.5-linux-arm64.tar.gz")
    monkeypatch.setattr(rmd_toolchain.platform, "machine", lambda: "x86_64")
    assert pandoc_archive_url("3.5").endswith("pandoc-3.5-linux-amd64.tar.gz")
    assert pandoc_archive_url("3.5").startswith(
        "https://github.com/jgm/pandoc/releases/download/")


# --------------------------------------------------------------------------- #
# Download safety
# --------------------------------------------------------------------------- #

def test_check_host_rejects_non_official():
    with pytest.raises(ToolchainError):
        _check_host("http://cran.r-project.org/x")       # not https
    with pytest.raises(ToolchainError):
        _check_host("https://evil.example.com/R.exe")    # wrong host
    with pytest.raises(ToolchainError):
        _check_host("https://cran.r-project.org.evil.com/x")
    _check_host("https://cran.r-project.org/bin/x")      # ok
    _check_host("https://github.com/jgm/pandoc/releases/download/x")


class _FakeResponse:
    def __init__(self, url, payload):
        self._url = url
        self._buf = io.BytesIO(payload)
        self.headers = {"Content-Length": str(len(payload))}

    def geturl(self):
        return self._url

    def read(self, n=-1):
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _fake_urlopen(monkeypatch, payload):
    monkeypatch.setattr(
        rmd_toolchain.urllib.request, "urlopen",
        lambda req, timeout=0: _FakeResponse(req.full_url, payload))


def test_download_verifies_sha256(sandbox, tmp_path, monkeypatch):
    payload = b"official bytes"
    _fake_urlopen(monkeypatch, payload)
    url = "https://cran.r-project.org/bin/thing.exe"
    dest = str(tmp_path / "thing.exe")

    good = hashlib.sha256(payload).hexdigest()
    assert _download(url, dest, sha256=good) == dest
    assert open(dest, "rb").read() == payload

    with pytest.raises(ToolchainError):
        _download(url, dest + "2", sha256="0" * 64)
    assert not os.path.exists(dest + "2")
    assert not os.path.exists(dest + "2.part")


def test_download_reports_progress_and_cancel(sandbox, tmp_path, monkeypatch):
    _fake_urlopen(monkeypatch, b"x" * 600 * 1024)
    url = "https://cran.r-project.org/big.bin"
    seen = []
    _download(url, str(tmp_path / "big.bin"),
              progress=lambda d, t: seen.append((d, t)))
    assert seen and seen[-1][0] == seen[-1][1] == 600 * 1024

    class _Cancel:
        def is_set(self):
            return True

    with pytest.raises(Cancelled):
        _download(url, str(tmp_path / "big2.bin"), cancel=_Cancel())
    assert not os.path.exists(str(tmp_path / "big2.bin"))


# --------------------------------------------------------------------------- #
# Version discovery with pinned fallback
# --------------------------------------------------------------------------- #

def test_discover_r_version_parses_and_falls_back(monkeypatch):
    monkeypatch.setattr(rmd_toolchain, "_fetch_text",
                        lambda url: 'href="R-4.9.9-win.exe"')
    assert discover_r_version() == "4.9.9"
    monkeypatch.setattr(rmd_toolchain, "_fetch_text",
                        lambda url: (_ for _ in ()).throw(OSError("offline")))
    assert discover_r_version() == rmd_toolchain.PINNED_R_VERSION


def test_discover_pandoc_version_parses_and_falls_back(monkeypatch):
    monkeypatch.setattr(rmd_toolchain, "_fetch_text",
                        lambda url: json.dumps({"tag_name": "9.8.7"}))
    assert discover_pandoc_version() == "9.8.7"
    monkeypatch.setattr(rmd_toolchain, "_fetch_text",
                        lambda url: "not json at all {")
    assert discover_pandoc_version() == rmd_toolchain.PINNED_PANDOC_VERSION


# --------------------------------------------------------------------------- #
# Archive extraction + knit environment
# --------------------------------------------------------------------------- #

def test_extract_pandoc_zip_and_tar(sandbox, tmp_path):
    # The program as this system names it (pandoc.exe on Windows), in the
    # layout of pandoc's release archives.
    binary = "pandoc.exe" if sys.platform == "win32" else "pandoc"
    zpath = str(tmp_path / "pandoc.zip")
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr(f"pandoc-3.5/bin/{binary}", "#!/bin/sh\n")
    out = extract_pandoc_archive(zpath, str(tmp_path / "zt"))
    assert out == os.path.join(str(tmp_path / "zt"), "pandoc-3.5", "bin", binary)
    assert os.path.isfile(out)
    if sys.platform != "win32":
        assert os.access(out, os.X_OK)

    tpath = str(tmp_path / "pandoc.tar.gz")
    with tarfile.open(tpath, "w:gz") as t:
        data = b"#!/bin/sh\n"
        info = tarfile.TarInfo(f"pandoc-3.5/bin/{binary}")
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    out = extract_pandoc_archive(tpath, str(tmp_path / "tt"))
    assert out == os.path.join(str(tmp_path / "tt"), "pandoc-3.5", "bin", binary)

    empty = str(tmp_path / "empty.zip")
    with zipfile.ZipFile(empty, "w") as z:
        z.writestr("README", "no binary here")
    assert extract_pandoc_archive(empty, str(tmp_path / "et")) is None


def test_knit_environment_composition(sandbox, monkeypatch):
    bin_dir = sandbox / "pandoc" / "3.5" / "bin"
    bin_dir.mkdir(parents=True)
    binary = bin_dir / "pandoc"
    binary.write_text("", encoding="utf-8")
    rmd_toolchain._save_state({"pandoc": {"version": "3.5",
                                          "bin": str(binary)}})
    monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", "/bin"]))
    monkeypatch.setenv("MYEDITOR_KNIT_TEST", "the person's own")
    env = knit_environment()
    assert env["RSTUDIO_PANDOC"] == str(bin_dir)
    assert env["PATH"] == os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"])
    assert env["R_LIBS_USER"] == str(sandbox / "library")
    assert env["MYEDITOR_KNIT_TEST"] == "the person's own"
    assert os.path.isdir(str(sandbox / "library"))


@pytest.fixture
def packaged(monkeypatch):
    """Run as a packaged Linux build does: PyInstaller put its own
    libraries first in LD_LIBRARY_PATH and kept the original."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/my-editor/_internal")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/usr/local/lib")


def test_a_system_program_gets_the_library_path_the_person_had(packaged):
    env = rmd_toolchain.program_environment()
    assert env["LD_LIBRARY_PATH"] == "/usr/local/lib"
    assert "LD_LIBRARY_PATH_ORIG" not in env
    assert rmd_toolchain.r_environment()["LD_LIBRARY_PATH"] == "/usr/local/lib"
    assert knit_environment()["LD_LIBRARY_PATH"] == "/usr/local/lib"


def test_a_library_path_the_person_did_not_have_is_dropped(packaged, monkeypatch):
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG")
    assert "LD_LIBRARY_PATH" not in rmd_toolchain.program_environment()


def test_run_from_source_the_library_path_is_left_alone(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/home/me/lib")
    assert rmd_toolchain.program_environment()["LD_LIBRARY_PATH"] == "/home/me/lib"


def test_r_is_run_with_rs_environment(sandbox, packaged, monkeypatch):
    # Both ways the toolchain runs R hand it r_environment.
    seen = []

    def run(args, **kwargs):
        seen.append(kwargs["env"])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    class Popen:
        def __init__(self, args, **kwargs):
            seen.append(kwargs["env"])
            self.stdout, self.returncode = iter(()), 0

        def wait(self):
            return 0

    monkeypatch.setattr(rmd_toolchain.subprocess, "run", run)
    monkeypatch.setattr(rmd_toolchain.subprocess, "Popen", Popen)
    rmd_toolchain._run_r("Rscript", "1")
    rmd_toolchain._stream_r("Rscript", "1", log=lambda line: None)
    assert [env["LD_LIBRARY_PATH"] for env in seen] == ["/usr/local/lib"] * 2
    assert [env["R_LIBS_USER"] for env in seen] == [str(sandbox / "library")] * 2


def test_state_roundtrip(sandbox):
    rmd_toolchain._save_state({"pandoc": {"version": "3.5", "bin": "/x"}})
    assert rmd_toolchain._load_state() == {"pandoc": {"version": "3.5",
                                                     "bin": "/x"}}
    # Corrupt state degrades to empty, not an exception.
    with open(rmd_toolchain._state_path(), "w", encoding="utf-8") as f:
        f.write("{broken")
    assert rmd_toolchain._load_state() == {}
