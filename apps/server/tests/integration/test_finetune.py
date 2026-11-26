"""TASK-38 fine-tuning helpers and a CPU smoke run (ADR-0008 sections 2, 4; INP-05).

The pure helpers run in the default image. The smoke run needs the `vision`
stack and the user's COCO RT-DETR weights, so it is `@pytest.mark.model`; its
images are generated in memory into tmp_path at test time, never committed.
"""

import json
import math
import os
from pathlib import Path

import pytest

from voa.train.finetune import (
    Metrics,
    Sample,
    TrainError,
    coco_annotations,
    hflip_boxes,
    iou,
    keep_negatives,
    load_split,
    lr_factor,
    prior_bias,
    sweep,
)
from voa.train.finetune import main as finetune_main

SERVER = Path(__file__).resolve().parents[2]


def test_split_rows_load_as_samples(tmp_path: Path) -> None:
    (tmp_path / "train.jsonl").write_text(
        '{"image":"train/images/a.jpg","boxes":[{"label":"fire","box":[0.1,0.2,0.3,0.4]}]}\n'
        '{"image":"train/images/b.jpg","boxes":[]}\n',
        encoding="utf-8",
    )
    samples = load_split(tmp_path, "train")
    assert samples == [
        Sample("train/images/a.jpg", (("fire", (0.1, 0.2, 0.3, 0.4)),)),
        Sample("train/images/b.jpg", ()),
    ]
    with pytest.raises(TrainError, match="val.jsonl missing"):
        load_split(tmp_path, "val")


def test_corner_boxes_become_coco_pixel_boxes() -> None:
    sample = Sample("x.jpg", (("fire", (0.25, 0.5, 0.75, 1.0)),))
    coco = coco_annotations(sample, 3, 640, 480, {"smoke": 0, "fire": 1})
    [annotation] = coco["annotations"]
    assert coco["image_id"] == 3
    assert annotation["bbox"] == [160.0, 240.0, 320.0, 240.0]
    assert annotation["category_id"] == 1 and annotation["area"] == 320.0 * 240.0


def test_horizontal_flip_mirrors_x_only() -> None:
    [(label, (x1, y1, x2, y2))] = hflip_boxes((("fire", (0.1, 0.2, 0.4, 0.9)),))
    assert label == "fire" and (y1, y2) == (0.2, 0.9)
    assert x1 == pytest.approx(0.6) and x2 == pytest.approx(0.9)


def test_learning_rate_warms_up_then_decays_to_zero() -> None:
    assert lr_factor(0, 10, 100) == pytest.approx(0.1)
    assert lr_factor(9, 10, 100) == pytest.approx(1.0)
    assert lr_factor(10, 10, 100) == pytest.approx(1.0)
    assert lr_factor(55, 10, 100) == pytest.approx(0.5)
    assert lr_factor(100, 10, 100) == pytest.approx(0.0, abs=1e-12)
    assert all(0 <= lr_factor(s, 10, 100) <= 1 for s in range(120))
    assert not math.isnan(lr_factor(0, 0, 0))


def test_prior_bias_starts_sigmoid_at_the_prior() -> None:
    assert 1 / (1 + math.exp(-prior_bias(0.01))) == pytest.approx(0.01)
    assert prior_bias(0.01) == pytest.approx(-4.59512, abs=1e-5)
    for bad in (0.0, 1.0):
        with pytest.raises(ValueError):
            prior_bias(bad)


def test_negative_images_can_be_thinned_deterministically() -> None:
    fire = (("fire", (0.1, 0.1, 0.2, 0.2)),)
    samples = [Sample(f"p{i}.jpg", fire) for i in range(5)]
    samples += [Sample(f"n{i}.jpg", ()) for i in range(200)]
    assert keep_negatives(samples, 1.0) == samples
    none = keep_negatives(samples, 0.0)
    assert none == samples[:5]
    some = keep_negatives(samples, 0.25)
    assert some[:5] == samples[:5] and 25 <= len(some) - 5 <= 75
    assert keep_negatives(samples, 0.25) == some
    with pytest.raises(TrainError):
        keep_negatives(samples, 1.5)


def test_iou_edges() -> None:
    assert iou((0, 0, 1, 1), (0, 0, 1, 1)) == 1.0
    assert iou((0, 0, 1, 1), (1, 1, 2, 2)) == 0.0
    assert iou((0, 0, 2, 1), (1, 0, 3, 1)) == pytest.approx(1 / 3)


def test_metrics_match_boxes_and_count_fire_images() -> None:
    metrics = Metrics(["smoke", "fire"])
    fire = (0.1, 0.1, 0.3, 0.3)
    # hit + one duplicate (fp); smoke missed (fn)
    metrics.add(
        [("fire", fire), ("smoke", (0.5, 0.5, 0.9, 0.9))],
        [("fire", 0.9, fire), ("fire", 0.8, (0.11, 0.1, 0.3, 0.3))],
    )
    metrics.add([("fire", fire)], [])  # missed fire image
    metrics.add([], [("fire", 0.7, (0.6, 0.6, 0.7, 0.7))])  # false fire on a clean image
    metrics.add([], [])  # clean and quiet
    summary = metrics.summary()
    assert summary["fire"] == {
        "tp": 1, "fp": 2, "fn": 1, "precision": 0.3333, "recall": 0.5, "f1": 0.4,
    }  # fmt: skip
    assert summary["smoke"]["fn"] == 1 and summary["smoke"]["tp"] == 0
    assert summary["fire_image_recall"] == 0.5
    assert summary["false_fire_image_rate"] == 0.5
    assert summary["images"] == 4


def test_sweep_reports_the_threshold_with_the_best_fire_f1() -> None:
    fire = (0.1, 0.1, 0.3, 0.3)
    items = [
        ([("fire", fire)], [("fire", 0.25, fire)]),  # good box with a low (VFL-like) score
        ([("fire", fire)], [("fire", 0.15, fire)]),
        ([], [("fire", 0.12, (0.6, 0.6, 0.7, 0.7))]),  # weak false box
    ]
    summary = sweep(["smoke", "fire"], items, thresholds=(0.1, 0.2, 0.5))
    assert summary["fire_f1_by_threshold"] == {"0.1": 0.8, "0.2": 0.6667, "0.5": 0.0}
    assert summary["best_threshold"] == 0.1
    assert summary["fire"]["tp"] == 2 and summary["fire"]["fp"] == 1
    assert summary["false_fire_image_rate"] == 1.0


def test_metrics_without_images_are_tbd() -> None:
    summary = Metrics(["fire"]).summary()
    assert summary["fire_image_recall"] == "TBD" and summary["false_fire_image_rate"] == "TBD"


def test_cli_rejects_bad_options_and_missing_folders(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    common = ["--prep", str(tmp_path / "p"), "--images", str(tmp_path / "i")]
    common += ["--base", str(tmp_path / "b"), "--out", str(tmp_path / "o")]
    assert finetune_main([*common, "--epochs", "0"]) == 2
    code = finetune_main(common)
    err = capsys.readouterr().err
    assert code == 2 and str(tmp_path) not in err


# --- CPU smoke run with the real base weights (excluded by default) ---


@pytest.mark.model
def test_two_step_cpu_smoke_run_writes_loadable_weights_and_a_record(tmp_path: Path) -> None:
    from PIL import Image, ImageDraw

    from voa.providers.detectors import load_detector
    from voa.providers.rtdetr import Frame

    weights = Path(os.environ.get("VOA_MODEL_WEIGHTS_DIR", SERVER / "var" / "models"))
    images, prep = tmp_path / "images", tmp_path / "prep"
    (images / "train").mkdir(parents=True)
    prep.mkdir()
    rows = []
    for i in range(4):
        image = Image.new("RGB", (320, 240), (30, 30, 30))
        ImageDraw.Draw(image).ellipse((120, 80, 180, 200), fill=(255, 140, 0))
        image.save(images / "train" / f"{i}.jpg")
        rows.append({"image": f"train/{i}.jpg", "boxes": [
            {"label": "fire", "box": [0.375, 0.333, 0.5625, 0.8333]}
        ]})  # fmt: skip
    body = "".join(json.dumps(r) + "\n" for r in rows)
    (prep / "train.jsonl").write_text(body, encoding="utf-8")
    (prep / "val.jsonl").write_text(body, encoding="utf-8")
    (prep / "test.jsonl").write_text(body, encoding="utf-8")
    (prep / "dataset.json").write_text(json.dumps({"class_names": ["smoke", "fire"]}))

    out = tmp_path / "out"
    code = finetune_main(
        [
            "--prep", str(prep), "--images", str(images),
            "--base", str(weights / "rtdetr-r18-coco"), "--out", str(out),
            "--max-steps", "2", "--batch", "2", "--workers", "0", "--device", "cpu",
            "--log-every", "1",
        ]
    )  # fmt: skip
    assert code == 0
    record = json.loads((out / "train_record.json").read_text(encoding="utf-8"))
    assert record["steps"] == 2 and record["class_names"] == ["smoke", "fire"]
    assert len(record["best_sha256"]) == 64
    assert record["test_images"] == 4 and record["test"]["images"] == 4
    assert "best_threshold" in record["test"]
    first = record["epochs"][0]
    assert first["val"]["fire_top_score_mean"] != "TBD"
    assert first["train_loss"] < 200  # head starts at the prior: no 500+ first loss
    config = json.loads((out / "best" / "config.json").read_text(encoding="utf-8"))
    assert config["model_type"] == "rt_detr"
    assert config["id2label"] == {"0": "smoke", "1": "fire"}

    class Manifest:
        model_id, revision, kind = "dfire-smoke", "local", "object_detection"
        labels = ["smoke", "fire"]
        sha256 = record["best_sha256"]

    (tmp_path / "w").mkdir()
    (out / "best").rename(tmp_path / "w" / "dfire-smoke")
    provider = load_detector(Manifest(), tmp_path / "w", score_floor=0.0)
    with Image.open(images / "train" / "0.jpg") as raw:
        [draft] = provider.process(Frame("f0", 0, raw.convert("RGB")))
    assert {d.label for d in draft.payload.detections} <= {"smoke", "fire"}  # type: ignore[union-attr]
