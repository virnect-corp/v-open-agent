"""Read-only image frame folder for Stage C0 (ADR-0007; NFR-02).

Image files (`.jpg`, `.jpeg`, `.png`, hidden files ignored) are taken in file
name order and stamped `capture_ts = index / fps`. Each file is read into memory
and decoded; nothing is copied, written or logged. A frame that cannot be decoded
is skipped and counted but keeps its time slot, so a broken file looks like a
dropped frame rather than a faster clip. Frame IDs are `frame_<index>`; file
names never leave this module.
"""

import io
import math
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from voa.providers.rtdetr import Frame, VisionUnavailable

IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png"})

Decoder = Callable[[bytes], Any]


class FramesError(ValueError):
    """The frame folder or rate is unusable. Messages never contain paths."""


def pillow_decode(data: bytes) -> Any:
    """Decode to an RGB PIL image (needs the optional `vision` group)."""
    try:
        from PIL import Image
    except ImportError as error:
        raise VisionUnavailable(
            "decoding frames needs the optional 'vision' dependencies"
        ) from error
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        return image.convert("RGB")


class FrameSource:
    def __init__(self, folder: str | Path, fps: float, *, decode: Decoder = pillow_decode) -> None:
        if not (isinstance(fps, int | float) and math.isfinite(fps) and fps > 0):
            raise FramesError("fps must be a positive number")
        root = Path(folder)
        if not root.is_dir():
            raise FramesError("frames path is not a folder")
        self._files = sorted(
            path
            for path in root.iterdir()
            if path.is_file()
            and not path.name.startswith(".")
            and path.suffix.lower() in IMAGE_SUFFIXES
        )
        if not self._files:
            raise FramesError("frames folder has no image files")
        self.name = root.name
        self.fps = fps
        self.skipped = 0
        self._decode = decode

    def __len__(self) -> int:
        return len(self._files)

    def __iter__(self) -> Iterator[Frame]:
        self.skipped = 0
        for index, path in enumerate(self._files):
            try:
                image = self._decode(path.read_bytes())
            except (ValueError, OSError):  # PIL's UnidentifiedImageError is an OSError
                self.skipped += 1
                continue
            yield Frame(
                frame_id=f"frame_{index:05d}",
                capture_ts_monotonic_ns=round(index * 1_000_000_000 / self.fps),
                image=image,
            )
