#!/usr/bin/env python3
"""
Этап 1.5: Маршрутизация PDF -> Markdown (если есть текстовый слой) ИЛИ на OCR (если сканы).

Логика:
1. Для каждого PDF из PDF_INPUT_DIR проверяется, сколько текста извлекается "напрямую"
   (без OCR) через PyMuPDF - это признак того, что PDF содержит настоящий текстовый слой,
   а не просто картинки страниц.
2. Если текста достаточно (выше порога) -> файл конвертируется в .md через pymupdf4llm
   в структуру {stem}/full.md + {stem}/images/*.png (та же структура, что даёт MinerU
   после OCR) и сохраняется в MD_OUTPUT_DIR.
3. Если текста мало/нет -> файл считается сканом (нужен OCR) и перемещается/копируется
   в OCR_INPUT_DIR для дальнейшей обработки MinerU на следующем этапе.

Требует: pip install pymupdf pymupdf4llm
"""

import shutil
import re
from pathlib import Path
import logging
import fitz  # PyMuPDF
import pymupdf4llm

from constants import PDF_OUTPUT_DIR, MD_OUTPUT_DIR, OCR_INPUT_DIR
from artifact_contract import mark_stage_done, has_stage, STAGE_PYMUPDF

# ============ НАСТРОЙКИ (меняйте здесь) ============

PDF_INPUT_DIR = PDF_OUTPUT_DIR   # результат этапа 1 (djvu_to_pdf.py)
MD_OUTPUT_DIR = MD_OUTPUT_DIR    # готовые {stem}/full.md + images/
OCR_INPUT_DIR = OCR_INPUT_DIR    # PDF, которым требуется OCR (сканы)

# Порог решения "текстовый слой есть / это скан":
# Считаем среднее количество символов текста на одну страницу (через прямой extract,
# без OCR). Если среднее ниже порога - страница почти наверняка скан без текстового слоя.
# 50 символов/страницу - разумный дефолт (пустая/картиночная страница даёт около 0),
# но для страниц с одной короткой подписью может потребоваться подстройка.
MIN_CHARS_PER_PAGE = 50

# Какую долю страниц документа нужно признать "текстовыми", чтобы весь файл
# считался текстовым (не сканом). 0.8 = 80% страниц должны иметь текст.
# Понижайте, если в книгах много страниц-иллюстраций без текста (нормально для учебников).
TEXT_PAGE_RATIO_THRESHOLD = 0.8

# Сколько страниц проверять для быстрой оценки (не весь документ, чтобы не тормозить
# на больших файлах). None = проверять все страницы (медленнее, но точнее).
SAMPLE_PAGES = 15

# "move" - переместить PDF-скан в OCR_INPUT_DIR (исходник удаляется из PDF_INPUT_DIR)
# "copy" - скопировать (исходник остаётся)
OCR_TRANSFER_MODE = "move"

SKIP_EXISTING = True

# Параметры извлечения картинок при конвертации текстового PDF в Markdown.
IMAGE_DPI = 200            # разрешение рендера картинок/графиков
IMAGE_SIZE_LIMIT = 0.05    # игнорировать изображения, меньшие этой доли страницы

# Regex-валидация текста: доля символов, которые должны приходиться на "читаемые"
# классы (буквы кириллицы/латиницы, цифры, пробел, типичные знаки препинания),
# чтобы страница считалась настоящим текстом, а не мусором от битой кодировки.
# Если доля "хороших" символов ниже - текст считается подозрительным (мусором).
TEXT_QUALITY_RATIO = 0.85

# ============ КОНЕЦ НАСТРОЕК ============

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


def text_quality_ok(text: str) -> bool:
    """
    Проверяет, что извлечённый фрагмент похож на читаемый текст, а не мусор.
    Старые сканы СЦБ бывают с битой кодировкой шрифта: визуально текст нормальный,
    а при извлечении получаются случайные символы/заменяющие (U+FFFD).
    Считаем долю символов из "хороших" классов: буквы (вкл. кириллицу), цифры,
    пробелы, типичные знаки препинания. Если доля < TEXT_QUALITY_RATIO - мусор.
    """
    if not text.strip():
        return False

    # Классы, которые допускаем в читаемом тексте. \w включает буквы и цифры
    # в т.ч. кириллицу (regex3). Дополняем пробелом и типичными знаками препинания.
    good_chars_re = re.compile(r"[\w\s.,!?;:()«»\"'\-–—…%№+*/=]")
    good_count = len(good_chars_re.findall(text))
    ratio = good_count / len(text)
    return ratio >= TEXT_QUALITY_RATIO


def needs_ocr(pdf_path: Path) -> bool:
    """
    Эвристика: открывает PDF и проверяет, сколько текста извлекается без OCR.
    Возвращает True, если документ похож на скан (нужен OCR).
    """
    doc = fitz.open(pdf_path)
    total_pages = doc.page_count

    if total_pages == 0:
        doc.close()
        return True  # пустой/битый PDF - направляем на OCR для перепроверки

    pages_to_check = range(total_pages) if SAMPLE_PAGES is None else range(min(total_pages, SAMPLE_PAGES))

    text_pages = 0
    garbage_pages = 0
    checked = 0
    for i in pages_to_check:
        page = doc[i]
        text = page.get_text("text")
        if not isinstance(text, str):
            text = str(text)
        checked += 1
        if len(text.strip()) < MIN_CHARS_PER_PAGE:
            continue  # пустая/картиночная страница
        if text_quality_ok(text):
            text_pages += 1
        else:
            garbage_pages += 1

    doc.close()

    ratio = text_pages / checked if checked else 0
    log.info(
        f"{pdf_path.name}: текстовых страниц {text_pages}/{checked} "
        f"(доля {ratio:.2f}), из них с мусорным текстом: {garbage_pages}"
    )

    return ratio < TEXT_PAGE_RATIO_THRESHOLD


def _pdf_page_count(pdf_path: Path) -> int:
    """Число страниц PDF (для manifest.json)."""
    doc = fitz.open(pdf_path)
    try:
        return doc.page_count
    finally:
        doc.close()


def _md_to_relative_image_paths(md_text: str, images_dir: Path) -> str:
    """
    pymupdf4llm записывает ссылки на картинки с абсолютным путём
    (images_dir подставляется как image_path). Переписываем их на относительные
    images/имя.png, чтобы структура {stem}/full.md + images/ была переносимой.
    """
    # pymupdf4llm вставляет в markdown ровно image_path, переданный в to_markdown
    # (с заменой \ на /). Меняем его на относительный images/, чтобы структура
    # была переносимой. Покрываем и относительный, и абсолютный вариант.
    as_passed = str(images_dir).replace("\\", "/") + "/"
    as_resolved = images_dir.resolve().as_posix() + "/"
    return md_text.replace(as_passed, "images/").replace(as_resolved, "images/")


def convert_to_markdown(pdf_path: Path, output_dir: Path) -> Path:
    """
    Конвертирует PDF с текстовым слоем в {stem}/full.md + {stem}/images/*.png
    через pymupdf4llm - та же структура, что даёт MinerU после OCR, чтобы дальше
    пайплайн работал с единообразным контрактом артефактов.
    """
    result_dir = output_dir / pdf_path.stem
    images_dir = result_dir / "images"
    md_path = result_dir / "full.md"

    if SKIP_EXISTING and (has_stage(result_dir, STAGE_PYMUPDF) or md_path.exists()):
        if not has_stage(result_dir, STAGE_PYMUPDF):
            mark_stage_done(
                result_dir,
                STAGE_PYMUPDF,
                source="pymupdf4llm",
                page_count=_pdf_page_count(pdf_path),
                images_count=len(list(images_dir.glob("*"))),
            )
        log.info(f"Пропуск (этап pymupdf4llm уже пройден): {result_dir.name}/")
        return md_path

    result_dir.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir(parents=True, exist_ok=True)

    log.info(f"Конвертация в Markdown: {pdf_path.name}")
    md_text = pymupdf4llm.to_markdown(
        str(pdf_path),
        write_images=True,
        image_path=str(images_dir),
        image_format="png",
        dpi=IMAGE_DPI,
        image_size_limit=IMAGE_SIZE_LIMIT,
    )

    if not isinstance(md_text, str):
        if isinstance(md_text, list):
            md_text = "\n".join(
                item["text"] if isinstance(item, dict) and "text" in item else str(item)
                for item in md_text
            )
        else:
            md_text = str(md_text)

    md_text = _md_to_relative_image_paths(md_text, images_dir)
    md_path.write_text(md_text, encoding="utf-8")

    n_images = len(list(images_dir.glob("*")))
    mark_stage_done(
        result_dir,
        STAGE_PYMUPDF,
        source="pymupdf4llm",
        page_count=_pdf_page_count(pdf_path),
        images_count=n_images,
    )
    log.info(f"Готово: {result_dir.name}/full.md ({len(md_text)} символов, {n_images} картинок)")
    return md_path


def route_to_ocr(pdf_path: Path, ocr_dir: Path) -> Path:
    """Перемещает/копирует PDF-скан в очередь на OCR."""
    dest_path = ocr_dir / pdf_path.name

    if SKIP_EXISTING and dest_path.exists():
        log.info(f"Пропуск (уже в очереди на OCR): {dest_path.name}")
        return dest_path

    if OCR_TRANSFER_MODE == "move":
        shutil.move(str(pdf_path), str(dest_path))
    elif OCR_TRANSFER_MODE == "copy":
        shutil.copy2(str(pdf_path), str(dest_path))
    else:
        raise ValueError(f"Неизвестный OCR_TRANSFER_MODE: {OCR_TRANSFER_MODE}")

    log.info(f"Направлен на OCR: {pdf_path.name}")
    return dest_path


def batch_route(pdf_input_dir: Path, md_output_dir: Path, ocr_input_dir: Path) -> dict:
    """Обрабатывает все PDF: либо в Markdown (full.md + images/), либо в очередь на OCR."""
    md_output_dir.mkdir(parents=True, exist_ok=True)
    ocr_input_dir.mkdir(parents=True, exist_ok=True)

    pdf_files = sorted(pdf_input_dir.glob("*.pdf"))
    if not pdf_files:
        log.warning(f"Не найдено .pdf файлов в {pdf_input_dir}")
        return {"markdown": [], "ocr_queue": [], "errors": []}

    log.info(f"Найдено {len(pdf_files)} PDF для маршрутизации")

    results = {"markdown": [], "ocr_queue": [], "errors": []}

    for pdf_file in pdf_files:
        try:
            if needs_ocr(pdf_file):
                dest = route_to_ocr(pdf_file, ocr_input_dir)
                results["ocr_queue"].append(dest)
            else:
                dest = convert_to_markdown(pdf_file, md_output_dir)
                results["markdown"].append(dest)
        except Exception as e:
            log.error(f"Ошибка обработки {pdf_file.name}: {e}")
            results["errors"].append(pdf_file.name)

    log.info(
        f"Итог: {len(results['markdown'])} -> Markdown, "
        f"{len(results['ocr_queue'])} -> очередь OCR, "
        f"{len(results['errors'])} ошибок"
    )

    if results["errors"]:
        log.warning(f"Файлы с ошибками: {results['errors']}")

    return results


if __name__ == "__main__":
    outcome = batch_route(PDF_INPUT_DIR, MD_OUTPUT_DIR, OCR_INPUT_DIR)
    n_md = len(outcome["markdown"])
    n_ocr = len(outcome["ocr_queue"])
    print(f"\nMarkdown: {n_md} файлов -> {MD_OUTPUT_DIR}")
    print(f"OCR очередь: {n_ocr} файлов -> {OCR_INPUT_DIR}")
