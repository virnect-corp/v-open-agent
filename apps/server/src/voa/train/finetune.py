"""Fine-tune RT-DETR R18 on a prepared D-Fire split (ADR-0008 section 2, 4; INP-05).

    python -m voa.train.finetune --prep var/datasets/dfire-prep --images var/datasets/dfire \
        --base var/models/rtdetr-r18-coco --out var/train/rtdetr-r18-dfire

Inputs are local only: the `voa.train.dfire` split (JSONL with normalized corner
boxes), the D-Fire image folder and the COCO RT-DETR weights the user already
downloaded (checked by the caller). Nothing is downloaded. Output:

- `best/` and `last/`: `model.safetensors`, `config.json` (model_type `rt_detr`,
  labels from the split) and the image processor, loadable by
  `voa.providers.detectors.load_detector` once a manifest pins the sha256.
- `train_record.json`: arguments, dataset record, versions, per-epoch losses and
  validation metrics, durations and the sha256 of `best/model.safetensors`.
- `log.jsonl`: one line per logged step.

Validation reports per-class box precision/recall/F1 at IoU 0.5 and, for `fire`,
image-level recall (a fire image with any fire box) and the false-image rate
(an image without fire that gets a fire box), because the engine consumes
per-frame detections. `best/` is chosen by fire box F1. These are measurements,
not claims; event recall and FP/hour come from the evaluation runner later.
The pure helpers below need no torch; `train()` imports the `vision` stack lazily.
"""

import argparse
import hashlib
import json
import math
import random
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

Box = tuple[float, float, float, float]


class TrainError(ValueError):
    """Unusable inputs or options. Messages never contain host paths."""


@dataclass(frozen=True)
class Sample:
    image: str  # relative to the image root
    boxes: tuple[tuple[str, Box], ...]


@dataclass
class Options:
    prep: str
    images: str
    base: str
    out: str
    epochs: int = 30
    batch: int = 8
    lr: float = 1e-4
    backbone_lr_scale: float = 0.1
    weight_decay: float = 1e-4
    warmup_steps: int = 300
    grad_clip: float = 0.1
    workers: int = 4
    amp: bool = True
    device: str = "auto"
    seed: int = 0
    score_threshold: float = 0.5
    max_steps: int = 0  # >0: stop after this many optimizer steps (smoke runs)
    val_limit: int = 0  # >0: validate on the first N images only
    log_every: int = 50
    hflip: float = 0.5
    code_commit: str = "TBD"
    extra: dict[str, Any] = field(default_factory=dict)


# --- pure helpers (no torch) ---


def load_split(prep: Path, split: str) -> list[Sample]:
    path = prep / f"{split}.jsonl"
    if not path.is_file():
        raise TrainError(f"{split}.jsonl missing in the prepared folder")
    samples = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        boxes = tuple((str(b["label"]), tuple(float(v) for v in b["box"])) for b in row["boxes"])
        samples.append(Sample(str(row["image"]), boxes))  # type: ignore[arg-type]
    return samples


def class_names(prep: Path) -> list[str]:
    record = json.loads((prep / "dataset.json").read_text(encoding="utf-8"))
    names = record.get("class_names")
    if not isinstance(names, list) or not names:
        raise TrainError("dataset.json has no class_names")
    return [str(n) for n in names]


def coco_annotations(
    sample: Sample, image_id: int, width: int, height: int, label_ids: dict[str, int]
) -> dict[str, Any]:
    """Normalized corner boxes -> COCO pixel [x, y, w, h] for the RT-DETR processor."""
    annotations = []
    for label, (x1, y1, x2, y2) in sample.boxes:
        w, h = (x2 - x1) * width, (y2 - y1) * height
        annotations.append(
            {
                "bbox": [x1 * width, y1 * height, w, h],
                "category_id": label_ids[label],
                "area": w * h,
                "iscrowd": 0,
            }
        )
    return {"image_id": image_id, "annotations": annotations}


def hflip_boxes(boxes: tuple[tuple[str, Box], ...]) -> tuple[tuple[str, Box], ...]:
    return tuple((label, (1.0 - x2, y1, 1.0 - x1, y2)) for label, (x1, y1, x2, y2) in boxes)


def lr_factor(step: int, warmup: int, total: int) -> float:
    """Linear warmup, then cosine decay to 0 at `total`."""
    if total <= 0:
        return 1.0
    if warmup > 0 and step < warmup:
        return (step + 1) / warmup
    progress = (step - warmup) / max(total - warmup, 1)
    return 0.5 * (1.0 + math.cos(math.pi * min(max(progress, 0.0), 1.0)))


def iou(a: Box, b: Box) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


@dataclass
class Tally:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    def summary(self) -> dict[str, float | int]:
        precision = self.tp / (self.tp + self.fp) if self.tp + self.fp else 0.0
        recall = self.tp / (self.tp + self.fn) if self.tp + self.fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        return {
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
        }


@dataclass
class Metrics:
    classes: list[str]
    iou_threshold: float = 0.5
    boxes: dict[str, Tally] = field(default_factory=dict)
    fire_images: int = 0
    fire_images_hit: int = 0
    clean_images: int = 0
    clean_images_with_fire: int = 0

    def __post_init__(self) -> None:
        self.boxes = {name: Tally() for name in self.classes}

    def add(
        self,
        truth: Sequence[tuple[str, Box]],
        predicted: Sequence[tuple[str, float, Box]],
    ) -> None:
        """Greedy per-class matching by score at the IoU threshold."""
        for name in self.classes:
            gts = [box for label, box in truth if label == name]
            preds = sorted(
                ((score, box) for label, score, box in predicted if label == name),
                key=lambda p: -p[0],
            )
            used = [False] * len(gts)
            for _, box in preds:
                best, best_iou = -1, self.iou_threshold
                for index, gt in enumerate(gts):
                    overlap = iou(box, gt)
                    if not used[index] and overlap >= best_iou:
                        best, best_iou = index, overlap
                if best >= 0:
                    used[best] = True
                    self.boxes[name].tp += 1
                else:
                    self.boxes[name].fp += 1
            self.boxes[name].fn += used.count(False)
        has_fire = any(label == "fire" for label, _ in truth)
        fire_predicted = any(label == "fire" for label, _, _ in predicted)
        if has_fire:
            self.fire_images += 1
            self.fire_images_hit += int(fire_predicted)
        else:
            self.clean_images += 1
            self.clean_images_with_fire += int(fire_predicted)

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {name: tally.summary() for name, tally in self.boxes.items()}
        out["fire_image_recall"] = (
            round(self.fire_images_hit / self.fire_images, 4) if self.fire_images else "TBD"
        )
        out["false_fire_image_rate"] = (
            round(self.clean_images_with_fire / self.clean_images, 4)
            if self.clean_images
            else "TBD"
        )
        out["images"] = self.fire_images + self.clean_images
        return out


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


# --- training (needs the vision stack) ---


def train(opts: Options) -> dict[str, Any]:  # pragma: no cover - exercised by model tests
    try:
        import torch
        from PIL import Image
        from torch.utils.data import DataLoader, Dataset
        from transformers import RTDetrForObjectDetection, RTDetrImageProcessor
    except ImportError as error:
        raise TrainError("training needs the 'vision' dependencies") from error

    prep, images_root, base, out = (
        Path(opts.prep),
        Path(opts.images),
        Path(opts.base),
        Path(opts.out),
    )
    for folder, what in ((prep, "prepared"), (images_root, "image"), (base, "base model")):
        if not folder.is_dir():
            raise TrainError(f"{what} folder not found")
    if out.exists() and any(out.iterdir()):
        raise TrainError("output folder is not empty")
    names = class_names(prep)
    label_ids = {name: index for index, name in enumerate(names)}
    train_set, val_set = load_split(prep, "train"), load_split(prep, "val")
    if opts.val_limit > 0:
        val_set = val_set[: opts.val_limit]
    if not train_set:
        raise TrainError("train split is empty")

    random.seed(opts.seed)
    torch.manual_seed(opts.seed)
    if opts.device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    else:
        device = opts.device
    use_amp = opts.amp and device == "cuda"

    processor = RTDetrImageProcessor.from_pretrained(base, local_files_only=True)
    model = RTDetrForObjectDetection.from_pretrained(
        base,
        local_files_only=True,
        num_labels=len(names),
        id2label=dict(enumerate(names)),
        label2id=label_ids,
        ignore_mismatched_sizes=True,  # new class head
    ).to(device)

    class Frames(Dataset):  # type: ignore[misc]  # torch is untyped here (vision group)
        def __init__(self, samples: list[Sample], augment: bool) -> None:
            self.samples, self.augment, self.epoch = samples, augment, 0

        def __len__(self) -> int:
            return len(self.samples)

        def __getitem__(self, index: int) -> tuple[Any, Sample]:
            sample = self.samples[index]
            with Image.open(images_root / sample.image) as raw:
                image = raw.convert("RGB")
            rng = random.Random(opts.seed * 1_000_003 + self.epoch * 100_003 + index)
            if self.augment and rng.random() < opts.hflip:
                image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
                sample = Sample(sample.image, hflip_boxes(sample.boxes))
            return image, sample

    def collate(batch: list[tuple[Any, Sample]]) -> dict[str, Any]:
        images = [image for image, _ in batch]
        annotations = [
            coco_annotations(sample, i, image.width, image.height, label_ids)
            for i, (image, sample) in enumerate(batch)
        ]
        encoded = processor(images=images, annotations=annotations, return_tensors="pt")
        return {
            "pixel_values": encoded["pixel_values"],
            "labels": encoded["labels"],
            "sizes": [(image.height, image.width) for image in images],
            "samples": [sample for _, sample in batch],
        }

    train_frames = Frames(train_set, augment=True)
    loader_args: dict[str, Any] = {
        "batch_size": opts.batch,
        "collate_fn": collate,
        "num_workers": opts.workers,
        "persistent_workers": False,
    }
    val_loader = DataLoader(Frames(val_set, augment=False), shuffle=False, **loader_args)

    backbone = [p for n, p in model.named_parameters() if "backbone" in n and p.requires_grad]
    rest = [p for n, p in model.named_parameters() if "backbone" not in n and p.requires_grad]
    optimizer = torch.optim.AdamW(
        [
            {"params": backbone, "lr": opts.lr * opts.backbone_lr_scale},
            {"params": rest, "lr": opts.lr},
        ],
        weight_decay=opts.weight_decay,
    )
    base_lrs = [group["lr"] for group in optimizer.param_groups]
    steps_per_epoch = math.ceil(len(train_set) / opts.batch)
    total = opts.max_steps or opts.epochs * steps_per_epoch
    scaler = torch.amp.GradScaler("cuda", enabled=use_amp)

    out.mkdir(parents=True, exist_ok=True)
    log = (out / "log.jsonl").open("w", encoding="utf-8")
    epochs: list[dict[str, Any]] = []
    best_f1, step, started = -1.0, 0, time.monotonic()

    def evaluate() -> dict[str, Any]:
        model.eval()
        metrics, losses = Metrics(names), []
        with torch.inference_mode():
            for batch in val_loader:
                pixels = batch["pixel_values"].to(device)
                labels = [{k: v.to(device) for k, v in t.items()} for t in batch["labels"]]
                with torch.autocast("cuda", dtype=torch.float16, enabled=use_amp):
                    outputs = model(pixel_values=pixels, labels=labels)
                losses.append(float(outputs.loss))
                results = processor.post_process_object_detection(
                    outputs,
                    threshold=opts.score_threshold,
                    target_sizes=torch.tensor(batch["sizes"], device=device),
                )
                for result, (h, w), sample in zip(
                    results, batch["sizes"], batch["samples"], strict=True
                ):
                    predicted = [
                        (names[int(label)], float(score), (x1 / w, y1 / h, x2 / w, y2 / h))
                        for score, label, (x1, y1, x2, y2) in zip(
                            result["scores"].tolist(),
                            result["labels"].tolist(),
                            result["boxes"].tolist(),
                            strict=True,
                        )
                    ]
                    metrics.add(sample.boxes, predicted)
        model.train()
        summary = metrics.summary()
        summary["loss"] = round(sum(losses) / len(losses), 4) if losses else "TBD"
        return summary

    model.train()
    epoch = 0
    while step < total:
        train_frames.epoch = epoch
        generator = torch.Generator().manual_seed(opts.seed + epoch)
        loader = DataLoader(train_frames, shuffle=True, generator=generator, **loader_args)
        epoch_start, epoch_losses = time.monotonic(), []
        for batch in loader:
            factor = lr_factor(step, min(opts.warmup_steps, total // 10 or 1), total)
            for group, lr in zip(optimizer.param_groups, base_lrs, strict=True):
                group["lr"] = lr * factor
            pixels = batch["pixel_values"].to(device)
            labels = [{k: v.to(device) for k, v in t.items()} for t in batch["labels"]]
            with torch.autocast("cuda", dtype=torch.float16, enabled=use_amp):
                loss = model(pixel_values=pixels, labels=labels).loss
            optimizer.zero_grad(set_to_none=True)
            scaler.scale(loss).backward()
            scaler.unscale_(optimizer)
            torch.nn.utils.clip_grad_norm_(model.parameters(), opts.grad_clip)
            scaler.step(optimizer)
            scaler.update()
            epoch_losses.append(float(loss))
            step += 1
            if step % opts.log_every == 0 or step == 1:
                entry = {
                    "epoch": epoch,
                    "step": step,
                    "loss": round(float(loss), 4),
                    "lr": optimizer.param_groups[1]["lr"],
                    "elapsed_s": round(time.monotonic() - started, 1),
                }
                log.write(json.dumps(entry) + "\n")
                log.flush()
                print(json.dumps(entry), flush=True)
            if step >= total:
                break
        validation = evaluate()
        row = {
            "epoch": epoch,
            "steps": step,
            "train_loss": round(sum(epoch_losses) / max(len(epoch_losses), 1), 4),
            "epoch_s": round(time.monotonic() - epoch_start, 1),
            "val": validation,
        }
        epochs.append(row)
        print(json.dumps({"epoch_done": row}), flush=True)
        model.save_pretrained(out / "last")
        processor.save_pretrained(out / "last")
        fire_f1 = float(validation.get("fire", {}).get("f1", 0.0))
        if fire_f1 > best_f1:
            best_f1 = fire_f1
            model.save_pretrained(out / "best")
            processor.save_pretrained(out / "best")
        epoch += 1
    log.close()

    dataset_record = json.loads((prep / "dataset.json").read_text(encoding="utf-8"))
    record: dict[str, Any] = {
        "base_model": "rtdetr-r18-coco",
        "code_commit": opts.code_commit,
        "options": {
            k: v for k, v in asdict(opts).items() if k not in {"prep", "images", "base", "out"}
        },
        "dataset": dataset_record,
        "class_names": names,
        "device": device,
        "gpu": torch.cuda.get_device_name(0) if device == "cuda" else None,
        "versions": {"torch": torch.__version__, "cuda": torch.version.cuda},
        "train_images": len(train_set),
        "val_images": len(val_set),
        "steps": step,
        "duration_s": round(time.monotonic() - started, 1),
        "best_fire_f1": best_f1,
        "epochs": epochs,
        "best_sha256": sha256_file(out / "best" / "model.safetensors"),
    }
    (out / "train_record.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return record


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m voa.train.finetune")
    defaults = Options("", "", "", "")
    for name in ("prep", "images", "base", "out"):
        parser.add_argument(f"--{name}", required=True)
    for name, value in asdict(defaults).items():
        if name in {"prep", "images", "base", "out", "extra"}:
            continue
        flag = f"--{name.replace('_', '-')}"
        if isinstance(value, bool):
            parser.add_argument(
                flag, type=lambda v: v.lower() in {"1", "true", "yes"}, default=value
            )
        else:
            parser.add_argument(flag, type=type(value), default=value)
    args = vars(parser.parse_args(argv))
    if args["epochs"] <= 0 or args["batch"] <= 0:
        print("error: epochs and batch must be positive", file=sys.stderr)
        return 2
    try:
        record = train(Options(**args))
    except TrainError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    summary = {k: record[k] for k in ("steps", "duration_s", "best_fire_f1", "best_sha256")}
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
