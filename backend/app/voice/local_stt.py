"""Local speech-to-text with Meta MMS (facebook/mms-1b-all): Kinyarwanda and English, on this machine.

MMS is a wav2vec2 CTC model with one small adapter per language (1,100+ languages,
Kinyarwanda = "kin"). Nothing is sent to a cloud service and there is no per-request cost.

The browser records WebM/Opus; ffmpeg decodes it to 16 kHz mono PCM first.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from threading import Lock
from typing import Any

import numpy as np

from app.core.errors import ProviderUnavailable
from app.voice.base import SpeechToText, Transcript

logger = logging.getLogger(__name__)

MMS_LANGS = {"rw": "kin", "en": "eng", "fr": "fra", "sw": "swh"}
SAMPLE_RATE = 16_000


def decode_audio(audio: bytes) -> np.ndarray:
    """Any browser recording -> float32 mono 16 kHz."""
    from app.video import ffmpeg

    proc = subprocess.run(
        [ffmpeg.ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "pipe:1"],
        input=audio,
        capture_output=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        raise ProviderUnavailable("Could not read the recording", code="stt_bad_audio")
    return np.frombuffer(proc.stdout, dtype=np.float32)


class MMSSpeechToText(SpeechToText):
    name = "mms"

    def __init__(self, *, model: str = "facebook/mms-1b-all", device: str = "auto") -> None:
        self._model_id = model
        self._device = device
        self._model: Any = None
        self._processor: Any = None
        self._lang: str | None = None
        self._lock = Lock()  # adapters are swapped in place: one transcription at a time

    def _load(self) -> None:
        if self._model is not None:
            return
        import torch
        from transformers import AutoProcessor, Wav2Vec2ForCTC

        device = ("cuda:0" if torch.cuda.is_available() else "cpu") if self._device == "auto" else self._device
        self._processor = AutoProcessor.from_pretrained(self._model_id)
        self._model = Wav2Vec2ForCTC.from_pretrained(self._model_id, torch_dtype=torch.float16 if device.startswith("cuda") else torch.float32).to(device)
        self._model.eval()
        logger.info("Loaded %s on %s", self._model_id, device)

    def _transcribe(self, audio: bytes, language: str) -> Transcript:
        import torch

        samples = decode_audio(audio)
        if samples.size < SAMPLE_RATE * 0.3:
            return Transcript(text="", language=language, confidence=0.0, provider=self.name)
        with self._lock:
            self._load()
            lang = MMS_LANGS.get(language, "kin")
            if lang != self._lang:
                self._processor.tokenizer.set_target_lang(lang)
                self._model.load_adapter(lang)
                self._lang = lang
            inputs = self._processor(samples, sampling_rate=SAMPLE_RATE, return_tensors="pt")
            values = inputs["input_values"].to(self._model.device, dtype=self._model.dtype)
            with torch.inference_mode():
                logits = self._model(values).logits[0].float()
            probs = logits.softmax(-1)
            ids = probs.argmax(-1)
            text = self._processor.decode(ids)
            # confidence: mean max-probability over non-blank frames
            blank = self._processor.tokenizer.pad_token_id
            keep = ids != blank
            confidence = float(probs.max(-1).values[keep].mean()) if bool(keep.any()) else 0.0
        return Transcript(text=text.strip(), language=language, confidence=round(confidence, 3), provider=self.name)

    async def transcribe(self, audio: bytes, mime_type: str, language: str = "rw") -> Transcript:
        try:
            return await asyncio.to_thread(self._transcribe, audio, language)
        except ProviderUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("Local speech recognition failed")
            raise ProviderUnavailable("Speech recognition failed", code="stt_error") from exc
