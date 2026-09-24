"""Local machine translation, so small local models can work in English.

Small local LLMs (7-8B) write poor Kinyarwanda but reason and call tools well in
English. With AGENT_LLM=local the agent therefore works in English:

    Kinyarwanda question --NLLB--> English --> local LLM + tools --> English answer --NLLB--> Kinyarwanda

NLLB-200 (Meta, open weights) covers 200 languages including Kinyarwanda
(kin_Latn). The distilled 1.3B model runs on the GPU in well under a second per
sentence. It translates sentence by sentence (NLLB drops text after the first
sentence of long inputs).
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from abc import ABC, abstractmethod
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)

NLLB_CODES = {"rw": "kin_Latn", "en": "eng_Latn", "fr": "fra_Latn", "sw": "swh_Latn"}
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=\S)")


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE.split(text.strip()) if s.strip()]


class Translator(ABC):
    name: str

    @abstractmethod
    async def translate(self, text: str, source: str, target: str) -> str:
        """Translate text between language codes ("rw", "en", ...)."""


class NLLBTranslator(Translator):
    name = "nllb"

    def __init__(self, model_name: str = "facebook/nllb-200-distilled-1.3B", device: str = "auto") -> None:
        self.model_name = model_name
        self.device = device
        self._model: Any = None
        self._tokenizers: dict[str, Any] = {}
        self._load_lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._cache: dict[tuple[str, str, str], str] = {}

    def _load(self) -> None:
        with self._load_lock:
            if self._model is not None:
                return
            import torch
            from transformers import AutoModelForSeq2SeqLM

            device = self.device
            if device == "auto":
                device = "cuda" if torch.cuda.is_available() else "cpu"
            dtype = torch.float16 if device.startswith("cuda") else torch.float32
            self._model = AutoModelForSeq2SeqLM.from_pretrained(self.model_name, torch_dtype=dtype).to(device).eval()
            self._device = device
            logger.info("Loaded translation model %s on %s", self.model_name, device)

    def _tokenizer(self, src_code: str) -> Any:
        if src_code not in self._tokenizers:
            from transformers import AutoTokenizer

            self._tokenizers[src_code] = AutoTokenizer.from_pretrained(self.model_name, src_lang=src_code)
        return self._tokenizers[src_code]

    def _translate_sync(self, sentences: list[str], src_code: str, tgt_code: str) -> list[str]:
        self._load()
        tok = self._tokenizer(src_code)
        with self._run_lock:
            batch = tok(sentences, return_tensors="pt", padding=True, truncation=True, max_length=256).to(self._device)
            out = self._model.generate(**batch, forced_bos_token_id=tok.convert_tokens_to_ids(tgt_code), max_new_tokens=256, num_beams=4)
        return tok.batch_decode(out, skip_special_tokens=True)

    async def translate(self, text: str, source: str, target: str) -> str:
        if not text.strip() or source == target:
            return text
        src, tgt = NLLB_CODES.get(source), NLLB_CODES.get(target)
        if not src or not tgt:
            return text
        key = (text, source, target)
        if key in self._cache:
            return self._cache[key]
        sentences = split_sentences(text) or [text]
        translated = await asyncio.to_thread(self._translate_sync, sentences, src, tgt)
        result = " ".join(t.strip() for t in translated)
        if len(self._cache) > 2000:
            self._cache.clear()
        self._cache[key] = result
        return result


@lru_cache
def get_translator() -> Translator | None:
    from app.core.config import get_settings

    s = get_settings()
    if s.translation_provider == "nllb":
        return NLLBTranslator(s.nllb_model, s.translation_device)
    return None
