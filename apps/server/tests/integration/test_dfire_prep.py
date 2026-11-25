"""TASK-37 D-Fire preparation tool (ADR-0008 section 3; NFR-04).

Synthetic YOLO folders only: the "images" are a few bytes, never real pictures.
"""

import json
from pathlib import Path

import pytest

from voa.train.dfire import PrepError, prepare
from voa.train.dfire import main as dfire_main


def write(root: Path, rel: str, data: str | bytes) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8")


@pytest.fixture
def source(tmp_path: Path) -> Path:
    """D-Fire-like layout: train/test, images/labels, YOLO lines, PublicDataset names."""
    root = tmp_path / "dfire"
    for split in ("train", "test"):
        for i in range(10):
            write(root, f"{split}/images/WEB{split}{i:03d}.jpg", b"jpg")
            write(root, f"{split}/labels/WEB{split}{i:03d}.txt", "1 0.5 0.5 0.2 0.3\n")
    write(root, "train/images/AoF00001.jpg", b"jpg")
    write(root, "train/labels/AoF00001.txt", "0 0.4 0.4 0.3 0.3\n1 0.6 0.6 0.1 0.1\n")
    write(root, "train/images/none00001.jpg", b"jpg")
    write(root, "train/labels/none00001.txt", "")  # no objects
    write(root, "train/images/PublicDataset00001.jpg", b"jpg")
    write(root, "train/labels/PublicDataset00001.txt", "1 0.5 0.5 0.2 0.2\n")
    write(root, "train/images/bad00001.jpg", b"jpg")
    write(root, "train/labels/bad00001.txt", "1 0.5 0.5 0.2\n7 0.5 0.5 0.1 0.1\n1 0.5 0.5 0 0.1\n")
    write(root, "train/images/nolabel00001.jpg", b"jpg")  # label file missing
    write(root, "train/labels/orphan00001.txt", "1 0.5 0.5 0.1 0.1\n")  # no image
    return root


def snapshot(root: Path) -> list[tuple[str, int]]:
    return sorted((str(p.relative_to(root)), p.stat().st_mtime_ns) for p in root.rglob("*"))


def rows(out: Path, split: str) -> list[dict[str, object]]:
    lines = (out / f"{split}.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def test_public_dataset_images_are_excluded_by_default(source: Path, tmp_path: Path) -> None:
    record = prepare(source, tmp_path / "out")
    names = {r["image"] for s in ("train", "val", "test") for r in rows(tmp_path / "out", s)}
    assert not any("PublicDataset" in str(n) for n in names)
    assert record["excluded_public"] == 1 and record["include_public"] is False


def test_include_public_is_an_explicit_option_and_recorded(source: Path, tmp_path: Path) -> None:
    record = prepare(source, tmp_path / "out", include_public=True)
    names = {r["image"] for s in ("train", "val", "test") for r in rows(tmp_path / "out", s)}
    assert "train/images/PublicDataset00001.jpg" in names
    assert record["excluded_public"] == 0 and record["include_public"] is True


def test_boxes_are_converted_to_labeled_corner_boxes(source: Path, tmp_path: Path) -> None:
    prepare(source, tmp_path / "out")
    by_name = {r["image"]: r for s in ("train", "val", "test") for r in rows(tmp_path / "out", s)}
    aof = by_name["train/images/AoF00001.jpg"]
    assert aof["boxes"] == [
        {"label": "smoke", "box": [0.25, 0.25, 0.55, 0.55]},
        {"label": "fire", "box": [0.55, 0.55, 0.65, 0.65]},
    ]
    assert by_name["train/images/none00001.jpg"]["boxes"] == []  # negative image kept


def test_bad_lines_and_missing_files_are_skipped_and_counted(source: Path, tmp_path: Path) -> None:
    record = prepare(source, tmp_path / "out")
    assert record["skipped_label_lines"] == 3  # 4 fields, unknown class, zero width
    assert record["images_without_label_file"] == 1
    assert record["labels_without_image"] == 1
    names = {r["image"] for s in ("train", "val", "test") for r in rows(tmp_path / "out", s)}
    assert "train/images/nolabel00001.jpg" not in names
    assert "train/images/bad00001.jpg" in names  # kept with no valid box


def test_split_is_deterministic_and_test_stays_test(source: Path, tmp_path: Path) -> None:
    first = prepare(source, tmp_path / "a", val_fraction=0.3)
    second = prepare(source, tmp_path / "b", val_fraction=0.3)
    assert first["split_sha256"] == second["split_sha256"]
    for split in ("train", "val", "test"):
        assert rows(tmp_path / "a", split) == rows(tmp_path / "b", split)
    assert all(str(r["image"]).startswith("test/") for r in rows(tmp_path / "a", "test"))
    train_val = rows(tmp_path / "a", "train") + rows(tmp_path / "a", "val")
    assert all(str(r["image"]).startswith("train/") for r in train_val)
    assert len(rows(tmp_path / "a", "val")) > 0


def test_record_counts_boxes_per_class_and_cites_the_source(source: Path, tmp_path: Path) -> None:
    record = prepare(source, tmp_path / "out")
    assert record["license"] == "CC0-1.0"
    assert "Venancio" in str(record["citation"])
    assert record["class_names"] == ["smoke", "fire"]
    assert record["boxes_per_class"] == {"smoke": 1, "fire": 21}
    on_disk = json.loads((tmp_path / "out" / "dataset.json").read_text(encoding="utf-8"))
    assert on_disk == record
    assert str(tmp_path) not in json.dumps(record)  # relative names only


def test_source_folder_is_not_changed(source: Path, tmp_path: Path) -> None:
    before = snapshot(source)
    prepare(source, tmp_path / "out")
    assert snapshot(source) == before


def test_errors_for_missing_source_nonempty_output_or_bad_fraction(
    source: Path, tmp_path: Path
) -> None:
    with pytest.raises(PrepError, match="not a folder"):
        prepare(tmp_path / "missing", tmp_path / "out")
    (tmp_path / "full").mkdir()
    (tmp_path / "full" / "x").write_text("x")
    with pytest.raises(PrepError, match="not empty"):
        prepare(source, tmp_path / "full")
    with pytest.raises(PrepError, match="val_fraction"):
        prepare(source, tmp_path / "out2", val_fraction=1.0)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(PrepError, match="no labeled images"):
        prepare(empty, tmp_path / "out3")


def test_cli_prints_the_record_and_exits_2_on_error(
    source: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert dfire_main(["--src", str(source), "--out", str(tmp_path / "out")]) == 0
    assert json.loads(capsys.readouterr().out)["excluded_public"] == 1
    assert dfire_main(["--src", str(tmp_path / "nope"), "--out", str(tmp_path / "o")]) == 2
    err = capsys.readouterr().err
    assert "error" in err and str(tmp_path) not in err
