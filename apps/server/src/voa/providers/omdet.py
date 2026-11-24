"""OmDet-Turbo open-vocabulary detector for Stage C1a (ADR-0008; INP-05).

The text prompts are the manifest `labels` (for `omdet-turbo-fire`: fire, smoke),
so the provider never emits a label the manifest does not declare. Frames go
through the same `RtDetrProvider` adapter as RT-DETR (label filter, score floor,
bbox normalization, empty Draft when nothing is seen); only the `Detector`
differs. Weights are local files checked against the manifest sha256 before
torch or transformers is imported (see `voa.providers.detectors`). Zero-shot
output is exploration evidence, not an accuracy claim (ADR-0008 section 2).
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from voa.providers.rtdetr import WEIGHTS_FILE, RawDetection, VisionUnavailable


class TransformersOmDet:
    """OmDetTurboForObjectDetection on CPU, prompted with a fixed label list."""

    def __init__(self, folder: Path, prompts: Sequence[str], score_floor: float) -> None:
        if not prompts:
            raise ValueError("prompts must not be empty")
        try:
            import torch
            from safetensors.torch import load_file
            from transformers import AutoProcessor, OmDetTurboConfig, OmDetTurboForObjectDetection
        except ImportError as error:
            raise VisionUnavailable(
                "OmDet-Turbo needs the optional 'vision' dependencies (run in scripts/vision.sh)"
            ) from error
        self._torch = torch
        try:  # the processor needs torchvision and the Swin backbone needs timm
            self._processor = AutoProcessor.from_pretrained(folder, local_files_only=True)
            config = OmDetTurboConfig.from_pretrained(folder, local_files_only=True)
            # Built on CPU, not via from_pretrained: its meta-device init leaves the timm
            # Swin `attn_mask` buffers on `meta`. Every weight must load (strict).
            self._model = OmDetTurboForObjectDetection(config)
        except ImportError as error:
            raise VisionUnavailable(
                "OmDet-Turbo needs the optional 'vision' dependencies (run in scripts/vision.sh)"
            ) from error
        self._model.load_state_dict(load_file(str(folder / WEIGHTS_FILE)), strict=True)
        self._model.eval()
        self._prompts = list(prompts)
        self._score_floor = score_floor

    def detect(self, image: Any) -> tuple[int, int, list[RawDetection]]:
        width, height = image.size
        torch = self._torch
        inputs = self._processor(image, text=self._prompts, return_tensors="pt")
        with torch.inference_mode():
            outputs = self._model(**inputs)
        [result] = self._processor.post_process_grounded_object_detection(
            outputs,
            text_labels=[self._prompts],
            target_sizes=[(height, width)],
            threshold=self._score_floor,
        )
        names = result.get("text_labels") or result.get("classes")
        found = [
            RawDetection(
                str(name),
                float(score),
                (float(box[0]), float(box[1]), float(box[2]), float(box[3])),
            )
            for score, name, box in zip(
                result["scores"].tolist(), names, result["boxes"].tolist(), strict=True
            )
        ]
        return int(width), int(height), found
