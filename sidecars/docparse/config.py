"""docparse configuration, from DOCPARSE_* environment variables."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

BACKENDS: Final = frozenset({"markitdown", "glm_ocr", "paddleocr_vl"})


@dataclass(frozen=True, slots=True)
class Config:
    backends: frozenset[str] = BACKENDS  # the gateway backends this instance answers for
    vlm_base_url: str | None = None  # OpenAI-compatible vision server (vLLM) used for OCR
    vlm_api_key: str | None = None
    vlm_model: str | None = None  # used when a request names no model
    vlm_timeout_s: float = 120.0
    render_dpi: int = 150
    text_layer_min_chars: int = 32  # a page with fewer characters is treated as a scan
    max_bytes: int = 64 << 20
    max_pages: int = 500
    max_output_tokens: int = 4096
    ocr_concurrency: int = 4  # pages recognised at the same time

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        values = os.environ if env is None else env
        listed = values.get("DOCPARSE_BACKENDS", "")
        backends = frozenset(b.strip() for b in listed.split(",") if b.strip()) or BACKENDS
        if unknown := backends - BACKENDS:
            raise ValueError(f"unknown DOCPARSE_BACKENDS: {', '.join(sorted(unknown))}")
        return cls(
            backends=backends,
            vlm_base_url=values.get("DOCPARSE_VLM_BASE_URL") or None,
            vlm_api_key=values.get("DOCPARSE_VLM_API_KEY") or None,
            vlm_model=values.get("DOCPARSE_VLM_MODEL") or None,
            vlm_timeout_s=float(values.get("DOCPARSE_VLM_TIMEOUT_S", "120")),
            render_dpi=int(values.get("DOCPARSE_RENDER_DPI", "150")),
            text_layer_min_chars=int(values.get("DOCPARSE_TEXT_LAYER_MIN_CHARS", "32")),
            max_bytes=int(values.get("DOCPARSE_MAX_BYTES", str(64 << 20))),
            max_pages=int(values.get("DOCPARSE_MAX_PAGES", "500")),
            max_output_tokens=int(values.get("DOCPARSE_MAX_OUTPUT_TOKENS", "4096")),
            ocr_concurrency=int(values.get("DOCPARSE_OCR_CONCURRENCY", "4")),
        )
