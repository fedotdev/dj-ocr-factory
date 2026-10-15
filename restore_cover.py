#!/usr/bin/env python3
"""
Этап "Реставрация обложки": берёт первую страницу PDF, рендерит в PNG,
кладёт в md_output/{название}/, отправляет на реставрацию через NanoBanana API
(image-to-image) и сохраняет результат туда же.

Документация API: https://docs.nanobananaapi.ai/quickstart
Токен получить: https://nanobananaapi.ai/api-key

ВАЖНО: NanoBanana API принимает только публичные URL картинок (imageUrls),
а не прямую загрузку файла с диска. Поэтому перед отправкой PNG обложки
нужно выложить его на публичный хостинг (см. upload_to_public_url ниже) -
по умолчанию используется бесплатный imgbb.com (нужен свой бесплатный API-ключ:
https://api.imgbb.com/). Если у вас уже есть свой сервер/S3-бакет для хранения
картинок - замените upload_to_public_url() на свою реализацию.

Требует: pip install pymupdf requests python-dotenv
"""

import os
import time
from pathlib import Path
import logging
import requests
import fitz  # PyMuPDF
from dotenv import load_dotenv

load_dotenv()

from constants import PDF_OUTPUT_DIR, OCR_INPUT_DIR, MD_OUTPUT_DIR
from artifact_contract import mark_stage_done, has_stage, STAGE_COVER

PDF_SEARCH_DIRS = [
    PDF_OUTPUT_DIR,   # PDF, которые ещё не были маршрутизированы
    OCR_INPUT_DIR,    # сканы, отправленные на OCR (route_pdf.py их туда переносит)
]

# Токен NanoBanana API.
NANOBANANA_TOKEN = os.environ.get("NANOBANANA_TOKEN")

# Токен imgbb.com для временного публичного хостинга PNG перед отправкой в API
# (nanobanana не принимает файлы напрямую, только URL).
IMGBB_TOKEN = os.environ.get("IMGBB_TOKEN")

# Разрешение рендера обложки (DPI).
RENDER_DPI = 300

# Промпт реставрации обложки. Настроен на: снятие складки от переплёта,
# удаление артефактов сжатия/сканирования, чёрных рамок по краям, теней и
# фактуры физического материала - результат должен выглядеть как чистый
# плоский векторный макет, а не фото книги. Меняйте здесь, если нужен
# другой стиль реставрации или другой набор дефектов для устранения.
RESTORE_PROMPT = """Task: Digitally restore this scanned book cover to look like a clean, flat vector mockup — as if it were a minimalist Soviet-era (USSR) book cover design shown as a flat concept sketch in Figma, not a photograph of a physical object.

Remove these specific defects:
1. Crease/fold line: There is a physical book spine crease or fold visible on the cover. Completely flatten and inpaint this area — reconstruct the underlying color, texture, and any text/graphic elements as if the surface were perfectly flat, with no shadow, indentation, or discontinuity.
2. JPEG compression artifacts: Remove blockiness, ringing, and color banding, especially around text edges and flat color areas. Smooth color gradients without introducing blur.
3. Black border strips: Remove any black or dark strips/frames along the edges of the image (scanning artifacts). Crop or inpaint them out so the cover fills the frame edge-to-edge with only its actual design.
4. Photographic artifacts: Remove all shadows, uneven lighting, glare, texture of the physical material (cloth/leather grain, paper fiber), and perspective/lens distortion. The surface should look perfectly flat and evenly lit, like a digital rendering, not a photo of a 3D object.

Preserve exactly, without altering:
- All text content, exact wording, letter shapes, and font style
- Layout, spacing, and composition of all elements
- Original color palette (do not recolor or modernize the color scheme)
- Any logos, emblems, or decorative motifs

Output style: Flat, minimalist, 2D graphic design mockup — solid clean colors, sharp vector-like edges on text and shapes, no photographic texture, no shadows, no gradients beyond what is originally part of the design, high resolution, edge-to-edge crop with no borders."""

# Соотношение сторон
IMAGE_SIZE = "auto"

RESOLUTION = "2K"

OUTPUT_FORMAT = "png"

# Таймаут ожидания результата и интервал опроса (секунды)
POLL_TIMEOUT = 300
POLL_INTERVAL = 5

SKIP_EXISTING = True

# ============ КОНЕЦ НАСТРОЕК ============

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

NANOBANANA_API = "https://api.nanobananaapi.ai/api/v1/nanobanana"


def find_source_pdf(stem: str, search_dirs: list[Path]) -> Path | None:
    """Ищет исходный PDF по имени (без расширения) во всех указанных папках по порядку."""
    for d in search_dirs:
        candidate = d / f"{stem}.pdf"
        if candidate.exists():
            return candidate
    return None


def render_first_page(pdf_path: Path, output_dir: Path) -> Path:
    """Рендерит первую страницу PDF в PNG и сохраняет в output_dir/cover_original.png."""
    output_dir.mkdir(parents=True, exist_ok=True)
    png_path = output_dir / "cover_original.png"

    if SKIP_EXISTING and png_path.exists():
        log.info(f"Пропуск рендера (уже есть): {png_path}")
        return png_path

    doc = fitz.open(pdf_path)
    page = doc[0]
    zoom = RENDER_DPI / 72  # 72 - базовый DPI PDF
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    pix.save(str(png_path))
    doc.close()

    log.info(f"Обложка отрендерена: {png_path}")
    return png_path


def upload_to_public_url(png_path: Path) -> str:
    """
    Выгружает PNG на imgbb.com и возвращает публичный URL.
    Замените эту функцию, если у вас свой хостинг/S3-бакет.
    """
    if not IMGBB_TOKEN:
        raise RuntimeError("IMGBB_TOKEN не задан в .env - нужен для публичного хостинга картинки")

    with open(png_path, "rb") as f:
        resp = requests.post(
            "https://api.imgbb.com/1/upload",
            params={"key": IMGBB_TOKEN},
            files={"image": f},
            timeout=60,
        )
    resp.raise_for_status()
    result = resp.json()

    if not result.get("success"):
        raise RuntimeError(f"Ошибка загрузки на imgbb: {result}")

    url = result["data"]["url"]
    log.info(f"Загружено на публичный хостинг: {url}")
    return url


def submit_restoration_task(image_url: str) -> str:
    """
    Отправляет задачу на реставрацию обложки через эндпоинт Nanobanana-2
    (/generate-2) - именно эта версия модели, а не старый /generate (Nanobanana 1).
    Схема параметров у generate-2 отличается от v1: используется imageUrls
    (пустой массив = text-to-image, непустой = image-to-image/reference режим),
    aspectRatio вместо image_size, resolution (1K/2K/4K), outputFormat (png/jpg).
    """
    if not NANOBANANA_TOKEN:
        raise RuntimeError("NANOBANANA_TOKEN не задан в .env")

    payload = {
        "prompt": RESTORE_PROMPT,
        "imageUrls": [image_url],   # непустой массив => режим image-to-image/reference
        "aspectRatio": IMAGE_SIZE,
        "resolution": RESOLUTION,
        "outputFormat": OUTPUT_FORMAT,
        "googleSearch": False,
    }

    resp = requests.post(
        f"{NANOBANANA_API}/generate-2",
        headers={"Authorization": f"Bearer {NANOBANANA_TOKEN}", "Content-Type": "application/json"},
        json=payload,
        timeout=60,
    )
    resp.raise_for_status()
    result = resp.json()

    if result.get("code") != 200:
        raise RuntimeError(f"Ошибка создания задачи NanoBanana-2: {result.get('message') or result.get('msg')}")

    task_id = result["data"]["taskId"]
    log.info(f"Задача создана (Nanobanana-2): {task_id}")
    return task_id


def poll_restoration_result(task_id: str) -> str:
    """
    Опрашивает статус задачи до готовности, возвращает URL результата.
    Структура ответа /record-info (подтверждено документацией):
    {
      "code": 200, "msg": "success",
      "data": {
        "successFlag": 0|1|2|3,  # 0=GENERATING, 1=SUCCESS, 2=CREATE_TASK_FAILED, 3=GENERATE_FAILED
        "response": {"resultImageUrl": "...", "originImageUrl": "..."},
        "errorCode": ..., "errorMessage": "..."
      }
    }
    successFlag и response лежат ВНУТРИ "data", не на верхнем уровне.
    """
    start = time.time()
    while time.time() - start < POLL_TIMEOUT:
        resp = requests.get(
            f"{NANOBANANA_API}/record-info",
            params={"taskId": task_id},
            headers={"Authorization": f"Bearer {NANOBANANA_TOKEN}"},
            timeout=30,
        )
        resp.raise_for_status()
        result = resp.json()

        if result.get("code") != 200:
            raise RuntimeError(f"Ошибка запроса record-info: {result.get('msg')}")

        data = result.get("data", {})
        success_flag = data.get("successFlag", 0)

        if success_flag == 1:
            result_url = data["response"]["resultImageUrl"]
            log.info(f"Реставрация завершена: {result_url}")
            return result_url
        elif success_flag in (2, 3):
            raise RuntimeError(f"Реставрация не удалась (successFlag={success_flag}): {data.get('errorMessage')}")

        log.info(f"Задача {task_id}: обрабатывается (successFlag={success_flag})...")
        time.sleep(POLL_INTERVAL)

    raise TimeoutError(f"Задача {task_id} не завершилась за {POLL_TIMEOUT}с")


def download_restored_cover(result_url: str, output_dir: Path) -> Path:
    """Скачивает готовую отреставрированную обложку."""
    restored_path = output_dir / "cover_restored.png"
    resp = requests.get(result_url, timeout=120)
    resp.raise_for_status()
    restored_path.write_bytes(resp.content)
    log.info(f"Сохранено: {restored_path}")
    return restored_path


def process_book(pdf_path: Path, md_output_dir: Path) -> Path | None:
    """Полный цикл: рендер -> загрузка -> реставрация -> скачивание для одной книги."""
    book_dir = md_output_dir / pdf_path.stem
    restored_path = book_dir / "cover_restored.png"

    if SKIP_EXISTING and (has_stage(book_dir, STAGE_COVER) or restored_path.exists()):
        log.info(f"Пропуск (этап cover уже пройден): {pdf_path.stem}")
        mark_stage_done(book_dir, STAGE_COVER)
        return restored_path

    try:
        png_path = render_first_page(pdf_path, book_dir)
        public_url = upload_to_public_url(png_path)
        task_id = submit_restoration_task(public_url)
        result_url = poll_restoration_result(task_id)
        restored_path = download_restored_cover(result_url, book_dir)
        mark_stage_done(book_dir, STAGE_COVER)
        return restored_path
    except Exception as e:
        log.error(f"Ошибка обработки {pdf_path.name}: {e}")
        return None


def batch_process(search_dirs: list[Path], md_output_dir: Path) -> dict:
    """
    Идёт от УЖЕ ГОТОВЫХ папок результата в md_output (это надёжнее, чем идти от
    PDF_INPUT_DIR, т.к. исходный PDF мог быть перемещён route_pdf.py в другую папку
    или вовсе удалён после конвертации в markdown без OCR).
    Для каждой папки книги ищет исходный PDF по всем search_dirs и запускает реставрацию.
    """
    results = {"success": [], "failed": [], "skipped": []}

    if not md_output_dir.exists():
        log.warning(f"Папка {md_output_dir} не существует - нечего обрабатывать")
        return results

    book_dirs = sorted(d for d in md_output_dir.iterdir() if d.is_dir())
    if not book_dirs:
        log.warning(f"В {md_output_dir} не найдено ни одной папки с результатами")
        return results

    for book_dir in book_dirs:
        stem = book_dir.name
        pdf_path = find_source_pdf(stem, search_dirs)

        if pdf_path is None:
            log.warning(f"Исходный PDF для '{stem}' не найден ни в одной из папок {search_dirs} - пропуск")
            results["skipped"].append(stem)
            continue

        outcome = process_book(pdf_path, md_output_dir)
        if outcome:
            results["success"].append(stem)
        else:
            results["failed"].append(stem)

    log.info(
        f"Итог: {len(results['success'])} успешно, {len(results['failed'])} с ошибками, "
        f"{len(results['skipped'])} пропущено (PDF не найден)"
    )
    return results


if __name__ == "__main__":
    outcome = batch_process(PDF_SEARCH_DIRS, MD_OUTPUT_DIR)
    print(f"\nОтреставрировано: {len(outcome['success'])}")
    print(f"С ошибками: {len(outcome['failed'])}")
    print(f"Пропущено (PDF не найден): {len(outcome['skipped'])}")
