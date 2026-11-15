# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The speech models dictation on this computer writes with, downloaded
once, when the person chooses to (dictation/setup.py).

Two sizes of OpenAI's Whisper, in the ONNX form the sherpa-onnx project
publishes for its engine, as its maintainer put them on Hugging Face:
Standard (base, about 160 MB), and More accurate (small, about 375 MB).
Both write the languages Whisper knows, with punctuation and capitals.
With them, the Silero voice detector (under 1 MB, from sherpa-onnx's own
releases) hears where speech pauses, so a phrase is written as soon as it
is spoken, and silence is never handed to Whisper (which "hears" words in
it). Every file is pinned to its size and SHA-256 and comes only from
those hosts (verified_download.py).

They live under ~/.config/my_editor/dictation/, beside the app's other
downloaded parts (rmd_toolchain.py), until the person removes them in
Settings > Dictation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Dict, Iterator, Optional, Tuple

import user_dirs
import verified_download

STANDARD, ACCURATE = "standard", "accurate"

# Where the files are published: Hugging Face (its downloads are served from
# its own hf.co hosts) and sherpa-onnx's GitHub releases.
HOSTS = ("huggingface.co", ".hf.co", "github.com", "objects.githubusercontent.com",
         "release-assets.githubusercontent.com")

_WHISPER = "https://huggingface.co/csukuangfj/sherpa-onnx-whisper-{size}/resolve/main/{name}"


@dataclass(frozen=True)
class ModelFile:
    name: str
    url: str
    size: int
    sha256: str


@dataclass(frozen=True)
class Model:
    key: str
    encoder: ModelFile
    decoder: ModelFile
    tokens: ModelFile

    @property
    def size(self) -> int:
        return self.encoder.size + self.decoder.size + self.tokens.size


def _whisper(size: str, name: str, length: int, sha256: str) -> ModelFile:
    file = f"{size}-{name}"
    return ModelFile(f"{size}/{file}", _WHISPER.format(size=size, name=file), length, sha256)


_TOKENS_SHA256 = "b34b360dbb493e781e479794586d661700670d65564001f23024971d1f2fa126"

MODELS: Dict[str, Model] = {
    STANDARD: Model(
        STANDARD,
        _whisper("base", "encoder.int8.onnx", 29120534,
                 "0b8fb1304b6109976038efff5ace81720e00386f3ff6b54ee8c75291ca0a1e11"),
        _whisper("base", "decoder.int8.onnx", 130672026,
                 "9759d217388a01b3a4c7c15533201067b48ae819c4daafc8624e64b9409dc02d"),
        _whisper("base", "tokens.txt", 816730, _TOKENS_SHA256)),
    ACCURATE: Model(
        ACCURATE,
        _whisper("small", "encoder.int8.onnx", 112442483,
                 "4cbe7b22fa9026b843b60a68640c747de05bafb1a11b57edc0e66c232d9f33a9"),
        _whisper("small", "decoder.int8.onnx", 262226114,
                 "acad50b5c782696e91b55914cc5ab4f756f1532f76e22aa6fc615f39fb69a8ee"),
        _whisper("small", "tokens.txt", 816730, _TOKENS_SHA256)),
}

VOICE_DETECTOR = ModelFile(
    "silero_vad.onnx",
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx",
    643854, "9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6")


def folder() -> str:
    return str(user_dirs.config_dir() / "dictation")


def path_of(file: ModelFile) -> str:
    return os.path.join(folder(), *file.name.split("/"))


def files_of(model: Model) -> Tuple[ModelFile, ...]:
    """What ``model`` needs on the disk, the voice detector included."""
    return (model.encoder, model.decoder, model.tokens, VOICE_DETECTOR)


def all_files() -> Iterator[ModelFile]:
    yield VOICE_DETECTOR
    for model in MODELS.values():
        yield model.encoder
        yield model.decoder
        yield model.tokens


def _whole(file: ModelFile) -> bool:
    # Checked against its SHA-256 as it arrived; here its size says that no
    # one has cut it short since.
    try:
        return os.path.getsize(path_of(file)) == file.size
    except OSError:
        return False


def installed(model: Model) -> bool:
    return all(_whole(file) for file in files_of(model))


def download_size(model: Model) -> int:
    """How much installing ``model`` downloads: what is not there yet."""
    return sum(file.size for file in files_of(model) if not _whole(file))


def install(model: Model, *, progress: Optional[Callable[[int, int], None]] = None,
            cancel=None, fetch: Callable = verified_download.download) -> None:
    """Download what ``model`` lacks, each file checked against its pin.
    ``progress(done, total)`` over all of it. Raises as ``fetch`` does
    (verified_download.download)."""
    missing = [file for file in files_of(model) if not _whole(file)]
    total = sum(file.size for file in missing)
    before = 0
    for file in missing:
        def report(done: int, _size: int, before=before) -> None:
            if progress is not None:
                progress(before + done, total)
        os.makedirs(os.path.dirname(path_of(file)), exist_ok=True)
        fetch(file.url, path_of(file), hosts=HOSTS, sha256=file.sha256, progress=report,
              cancel=cancel, user_agent="MyEditor dictation")
        before += file.size


def remove(model: Model) -> None:
    """Delete ``model``'s files; the voice detector too when no other model
    is left to use it."""
    for file in (model.encoder, model.decoder, model.tokens):
        _remove(path_of(file))
    if not any(installed(other) for other in MODELS.values() if other is not model):
        _remove(path_of(VOICE_DETECTOR))


def _remove(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
