#!/usr/bin/env python3
"""
Единый контракт артефактов (п. 3.5 плана).

В каждой папке результата `{name}/` лежит `manifest.json`:
    {
      "source": "mineru" | "docling" | "pymupdf4llm",
      "stages_completed": ["mineru", "cover", ...],
      "warnings": [...],
      "page_count": N,
      "images_count": N
    }

Этапы (значения stages_completed):
  "pymupdf4llm" - текст-слой сконвертирован route_pdf.py
  "mineru"      - OCR-скан обработан ocr_mineru.py
  "cover"       - обложка отреставрирована restore_cover.py

Модуль заменяет разрозненные SKIP_EXISTING-проверки в каждом скрипте
единым источником истины о том, какие этапы папка уже прошла.
"""

import json
from pathlib import Path

MANIFEST_NAME = "manifest.json"

# Имена этапов, чтобы не расходились между скриптами.
STAGE_PYMUPDF = "pymupdf4llm"
STAGE_MINERU = "mineru"
STAGE_COVER = "cover"


def manifest_path(book_dir: Path) -> Path:
    return book_dir / MANIFEST_NAME


def read_manifest(book_dir: Path) -> dict:
    p = manifest_path(book_dir)
    if not p.exists():
        return {"stages_completed": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"stages_completed": []}


def write_manifest(book_dir: Path, manifest: dict) -> Path:
    book_dir.mkdir(parents=True, exist_ok=True)
    p = manifest_path(book_dir)
    p.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def mark_stage_done(book_dir: Path, stage: str, **extra: object) -> dict:
    """
    Помечает этап как пройденный в manifest.json и дописывает доп. поля
    (например page_count/images_count). Идемпотентно: повторный вызов не дублирует.
    """
    manifest = read_manifest(book_dir)
    stages = manifest.setdefault("stages_completed", [])
    if stage not in stages:
        stages.append(stage)
    manifest.update(extra)
    write_manifest(book_dir, manifest)
    return manifest


def has_stage(book_dir: Path, stage: str) -> bool:
    return stage in read_manifest(book_dir).get("stages_completed", [])
