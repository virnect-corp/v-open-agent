"""Prepare a user-downloaded D-Fire copy for fine-tuning (ADR-0008 section 3; NFR-04).

    python -m voa.train.dfire --src var/datasets/dfire --out var/datasets/dfire-prep

Reads the YOLO layout (`<split>/images/*.jpg` with `<split>/labels/*.txt`, class
index then normalized cx, cy, w, h) and writes `train.jsonl`, `val.jsonl`,
`test.jsonl` and `dataset.json` into an empty output folder. Each JSONL row is
`{"image": <path relative to src>, "boxes": [{"label", "box": [x1, y1, x2, y2]}]}`
with normalized corner boxes; images with no objects are kept as negatives.

- Images whose name starts with `PublicDataset` come from third-party sources with
  their own terms and are left out unless `--include-public` is given.
- `test` stays test. `val` is the source `val`/`valid` split, or a deterministic
  slice of `train` chosen by a hash of the file path (no random state).
- Malformed label lines, label files without an image and images without a label
  file are skipped and counted. The source folder is only read.
- `dataset.json` records the counts, the split hash, the license and the citation.
  The class order is an argument (default `smoke,fire`); compare `boxes_per_class`
  with the D-Fire README (fire 14,692, smoke 11,865) before training.
Messages and records never contain host paths.
"""

import argparse
import hashlib
import json
import math
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png"})
DEFAULT_CLASSES = ("smoke", "fire")
PUBLIC_PREFIX = "PublicDataset"
LICENSE = "CC0-1.0"
SOURCE_URL = "https://github.com/gaiasd/DFireDataset"
CITATION = (
    "Venancio, P. V. A. B., Lisboa, A. C., Barbosa, A. V. An automatic fire detection "
    "system based on deep convolutional neural networks for low-power, resource-constrained "
    "devices. Neural Computing and Applications, 2022."
)
_VAL_NAMES = frozenset({"val", "valid", "validation"})


class PrepError(ValueError):
    """The source or output folder or an option is unusable. Messages contain no paths."""


def _source_split(rel: Path) -> str:
    parts = {part.lower() for part in rel.parts[:-1]}
    if "test" in parts:
        return "test"
    if parts & _VAL_NAMES:
        return "val"
    return "train"


def _label_path(image: Path) -> Path:
    parts = list(image.parts)
    for index in range(len(parts) - 2, -1, -1):
        if parts[index] == "images":
            parts[index] = "labels"
            return Path(*parts).with_suffix(".txt")
    return image.with_suffix(".txt")


def _is_val(rel: str, fraction: float) -> bool:
    digest = hashlib.sha256(rel.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / 2**64 < fraction


def _parse(text: str, classes: Sequence[str]) -> tuple[list[dict[str, Any]], int]:
    boxes: list[dict[str, Any]] = []
    skipped = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = line.split()
        try:
            if len(fields) != 5:
                raise ValueError("field count")
            index = int(fields[0])
            cx, cy, w, h = (float(v) for v in fields[1:])
            if not 0 <= index < len(classes):
                raise ValueError("class")
            if not all(math.isfinite(v) for v in (cx, cy, w, h)) or w <= 0 or h <= 0:
                raise ValueError("box")
        except ValueError:
            skipped += 1
            continue
        x1, y1 = max(cx - w / 2, 0.0), max(cy - h / 2, 0.0)
        x2, y2 = min(cx + w / 2, 1.0), min(cy + h / 2, 1.0)
        if not (x1 < x2 and y1 < y2):
            skipped += 1
            continue
        boxes.append({"label": classes[index], "box": [round(v, 6) for v in (x1, y1, x2, y2)]})
    return boxes, skipped


def prepare(
    src: str | Path,
    out: str | Path,
    *,
    include_public: bool = False,
    val_fraction: float = 0.1,
    classes: Sequence[str] = DEFAULT_CLASSES,
) -> dict[str, Any]:
    root, target = Path(src), Path(out)
    if not root.is_dir():
        raise PrepError("source is not a folder")
    if not (math.isfinite(val_fraction) and 0.0 <= val_fraction < 1.0):
        raise PrepError("val_fraction must be within [0, 1)")
    if not classes or len(set(classes)) != len(classes):
        raise PrepError("class names must be unique and not empty")
    if target.exists() and any(target.iterdir()):
        raise PrepError("output folder is not empty")

    images = sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES and not p.name.startswith(".")
    )  # fmt: skip
    image_labels = {_label_path(p) for p in images}
    orphans = sum(1 for p in root.rglob("*.txt") if "labels" in p.parts and p not in image_labels)

    splits: dict[str, list[dict[str, Any]]] = {"train": [], "val": [], "test": []}
    counts = {name: 0 for name in classes}
    excluded = no_label = skipped = 0
    for image in images:
        rel = image.relative_to(root).as_posix()
        if image.name.startswith(PUBLIC_PREFIX) and not include_public:
            excluded += 1
            continue
        label = _label_path(image)
        if not label.is_file():
            no_label += 1
            continue
        boxes, bad = _parse(label.read_text(encoding="utf-8", errors="replace"), classes)
        skipped += bad
        for box in boxes:
            counts[box["label"]] += 1
        split = _source_split(image.relative_to(root))
        if split == "train" and _is_val(rel, val_fraction):
            split = "val"
        splits[split].append({"image": rel, "boxes": boxes})

    if not any(splits.values()):
        raise PrepError("no labeled images found")

    target.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    for split in ("train", "val", "test"):
        lines = [json.dumps(row, separators=(",", ":")) for row in splits[split]]
        body = "".join(line + "\n" for line in lines)
        (target / f"{split}.jsonl").write_text(body, encoding="utf-8")
        digest.update(
            f"{split}\n".encode() + "".join(r["image"] + "\n" for r in splits[split]).encode()
        )

    record: dict[str, Any] = {
        "dataset": "D-Fire",
        "source_url": SOURCE_URL,
        "license": LICENSE,
        "citation": CITATION,
        "class_names": list(classes),
        "include_public": include_public,
        "excluded_public": excluded,
        "val_fraction": val_fraction,
        "images": {split: len(rows) for split, rows in splits.items()},
        "negative_images": sum(1 for rows in splits.values() for r in rows if not r["boxes"]),
        "boxes_per_class": counts,
        "skipped_label_lines": skipped,
        "images_without_label_file": no_label,
        "labels_without_image": orphans,
        "split_sha256": digest.hexdigest(),
    }
    (target / "dataset.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return record


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m voa.train.dfire")
    parser.add_argument("--src", required=True, help="user-downloaded D-Fire folder")
    parser.add_argument("--out", required=True, help="empty output folder")
    parser.add_argument("--include-public", action="store_true")
    parser.add_argument("--val-fraction", type=float, default=0.1)
    parser.add_argument("--classes", default=",".join(DEFAULT_CLASSES))
    args = parser.parse_args(argv)
    try:
        record = prepare(
            args.src,
            args.out,
            include_public=args.include_public,
            val_fraction=args.val_fraction,
            classes=[c.strip() for c in args.classes.split(",")],
        )
    except PrepError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
