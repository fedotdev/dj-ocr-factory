#!/usr/bin/env python3
"""
Общий конфиг путей для всего пайплайна djvu -> md (СЦБ).

Единая схема папок по этапам:
  1-djvu_input  - исходные djvu/сканы
  2-pdf_output  - результат этапа 1 (djvu_to_pdf.py) и вход route_pdf.py
  3-md_output   - готовые {stem}/full.md + {stem}/images/ (текстовые PDF и MinerU OCR)
  4-ocr_input   - PDF-сканы, требующие OCR (очередь для ocr_mineru.py)
"""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

DJVU_INPUT_DIR = BASE_DIR / "1-djvu_input"
PDF_OUTPUT_DIR = BASE_DIR / "2-pdf_output"
MD_OUTPUT_DIR = BASE_DIR / "3-md_output"
OCR_INPUT_DIR = BASE_DIR / "4-ocr_input"

# OpenAI-совместимый API для сериализации таблиц (tables_serialization.py).
# Реальные base_url/api_key/model заполняются в .env (fallback - дефолты).
OPENAI_BASE_URL = None
OPENAI_API_KEY = None
OPENAI_MODEL = "gpt-4o-mini-2024-07-18"
LLM_TEMPERATURE = 0
