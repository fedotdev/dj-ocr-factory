#!/usr/bin/env python3
"""
Этап 1: Конвертация djvu -> PDF (без OCR, только растеризация страниц).
Если в папке уже лежит .pdf - он просто перемещается в output без обработки.

Требует установленный djvulibre: sudo apt-get install djvulibre-bin (Linux)
                                   brew install djvulibre (macOS)
"""

import subprocess
import shutil
from pathlib import Path
import logging

from constants import DJVU_INPUT_DIR, PDF_OUTPUT_DIR

INPUT_DIR = DJVU_INPUT_DIR
OUTPUT_DIR = PDF_OUTPUT_DIR

# Качество JPEG-сжатия внутри PDF
QUALITY = 100

SCALE = 290 #dpi

# Что делать, если .pdf с таким именем уже существует в output
SKIP_EXISTING = True

# "move" - переместить (исходник удаляется из input_dir)
# "copy" - скопировать (исходник остаётся в input_dir)
PDF_TRANSFER_MODE = "move"

# Явные пути к ddjvu, если PATH не подхватывается средой (например VSCode).
# Узнать свой путь: в системном терминале выполнить `which ddjvu`
DDJVU_CANDIDATES = [
    "ddjvu",
    "/usr/bin/ddjvu",
    "/usr/local/bin/ddjvu",
    "/opt/homebrew/bin/ddjvu",  # типично для macOS Apple Silicon
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


def resolve_ddjvu_path() -> str:
    """Ищет рабочий путь к ddjvu среди кандидатов, независимо от PATH среды."""
    for candidate in DDJVU_CANDIDATES:
        found = shutil.which(candidate) or (candidate if Path(candidate).exists() else None)
        if found:
            log.info(f"ddjvu найден: {found}")
            return found
    raise RuntimeError(
        "ddjvu не найден ни по одному из указанных путей.\n"
        "Выполните `which ddjvu` в системном терминале и добавьте путь в DDJVU_CANDIDATES.\n"
        "Установка:\n"
        "  Ubuntu/Debian: sudo apt-get install djvulibre-bin\n"
        "  macOS:         brew install djvulibre"
    )


def convert_djvu_file(djvu_path: Path, output_dir: Path, ddjvu_bin: str | None) -> Path:
    """Конвертирует один .djvu файл в .pdf через ddjvu."""
    pdf_path = output_dir / f"{djvu_path.stem}.pdf"

    if SKIP_EXISTING and pdf_path.exists():
        log.info(f"Пропуск (уже существует): {pdf_path.name}")
        return pdf_path

    if ddjvu_bin is None:
        raise RuntimeError("ddjvu_bin не определён - resolve_ddjvu_path() не был вызван перед конвертацией")

    cmd = [
        ddjvu_bin,
        "-format=pdf",
        f"-quality={QUALITY}",
        f"-scale={SCALE}",
        str(djvu_path),
        str(pdf_path),
    ]

    log.info(f"Конвертация: {djvu_path.name} -> {pdf_path.name}")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        log.error(f"Ошибка при конвертации {djvu_path.name}: {result.stderr}")
        raise RuntimeError(f"ddjvu завершился с кодом {result.returncode}: {result.stderr}")

    if not pdf_path.exists() or pdf_path.stat().st_size == 0:
        raise RuntimeError(f"PDF не был создан или пустой: {pdf_path}")

    log.info(f"Готово: {pdf_path.name} ({pdf_path.stat().st_size / 1024 / 1024:.1f} MB)")
    return pdf_path


def transfer_pdf_file(pdf_path: Path, output_dir: Path) -> Path:
    """Файл уже в формате PDF - просто переносится/копируется без обработки."""
    dest_path = output_dir / pdf_path.name

    if SKIP_EXISTING and dest_path.exists():
        log.info(f"Пропуск (уже существует): {dest_path.name}")
        return dest_path

    if PDF_TRANSFER_MODE == "move":
        shutil.move(str(pdf_path), str(dest_path))
        log.info(f"Перемещён (уже PDF): {pdf_path.name}")
    elif PDF_TRANSFER_MODE == "copy":
        shutil.copy2(str(pdf_path), str(dest_path))
        log.info(f"Скопирован (уже PDF): {pdf_path.name}")
    else:
        raise ValueError(f"Неизвестный PDF_TRANSFER_MODE: {PDF_TRANSFER_MODE}")

    return dest_path


def batch_process(input_dir: Path, output_dir: Path) -> list[Path]:
    """Обрабатывает все .djvu и .pdf файлы в папке."""
    output_dir.mkdir(parents=True, exist_ok=True)

    djvu_files = sorted(input_dir.glob("*.djvu"))
    pdf_files = sorted(input_dir.glob("*.pdf"))

    if not djvu_files and not pdf_files:
        log.warning(f"Не найдено .djvu или .pdf файлов в {input_dir}")
        return []

    log.info(f"Найдено: {len(djvu_files)} .djvu, {len(pdf_files)} .pdf")

    ddjvu_bin = resolve_ddjvu_path() if djvu_files else None

    results = []
    errors = []

    for pdf_file in pdf_files:
        try:
            results.append(transfer_pdf_file(pdf_file, output_dir))
        except Exception as e:
            log.error(f"Не удалось перенести {pdf_file.name}: {e}")
            errors.append(pdf_file.name)

    for djvu_file in djvu_files:
        try:
            results.append(convert_djvu_file(djvu_file, output_dir, ddjvu_bin))
        except Exception as e:
            log.error(f"Не удалось обработать {djvu_file.name}: {e}")
            errors.append(djvu_file.name)

    total = len(djvu_files) + len(pdf_files)
    log.info(f"Успешно: {len(results)}/{total}")
    if errors:
        log.warning(f"Файлы с ошибками: {errors}")

    return results


if __name__ == "__main__":
    processed = batch_process(INPUT_DIR, OUTPUT_DIR)
    print(f"\nИтог: {len(processed)} PDF файлов в {OUTPUT_DIR}")
