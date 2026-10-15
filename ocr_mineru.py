#!/usr/bin/env python3
"""
Если PDF превышает лимит страниц API (MAX_PAGES), он автоматически разбивается
на части (<= SPLIT_PAGE_SIZE страниц), каждая часть обрабатывается отдельно,
а после получения результатов части снова склеиваются в один markdown-файл
и одну папку с картинками - так, будто файл обрабатывался целиком.
"""

import os
import re
import json
import time
import hashlib
import zipfile
import shutil
from pathlib import Path
import logging
import requests
import fitz  # PyMuPDF - для разбиения крупных PDF на части
from dotenv import load_dotenv

# Типы блоков в content_list.json, которые считаются "формулой", а не реальной
# иллюстрацией/схемой.
EQUATION_BLOCK_TYPES = {"equation", "interline_equation", "isolate_formula"}

from constants import OCR_INPUT_DIR as OCR_INPUT_DIR_CFG, MD_OUTPUT_DIR as MD_OUTPUT_DIR_CFG
from artifact_contract import mark_stage_done, has_stage, STAGE_MINERU

OCR_INPUT_DIR = OCR_INPUT_DIR_CFG     # PDF-сканы, требующие OCR (из route_pdf.py)
MD_OUTPUT_DIR = MD_OUTPUT_DIR_CFG     # сюда складываются итоговые .md + images/
SPLIT_WORK_DIR = OCR_INPUT_DIR / "_split_parts"  # временная папка для разбитых частей

load_dotenv()

# --- Пул токенов MinerU + локальный счётчик квот ---
# Пул: MINERU_TOKENS (через запятую или перевод строки) либо один MINERU_TOKEN.
# Счётчик: в ocr_mineru_quotas.json хранится сколько страниц потрачено на токен;
# остаток = MINERU_TOKEN_QUOTA - потрачено. Выбирается токен с наибольшим остатком.
# ВАЖНО: batch опрашивается тем же токеном, которым создан (MinerU привязывает
# задачу к аккаунту).
MINERU_TOKENS = [
    t.strip()
    for t in re.split(r"[\n,]", os.environ.get("MINERU_TOKENS", "") or os.environ.get("MINERU_TOKEN", ""))
    if t.strip()
]
MINERU_TOKEN_QUOTA = int(os.environ.get("MINERU_TOKEN_QUOTA", "0"))  # стартовых страниц на токен (0 = без учёта)
QUOTA_FILE = Path(__file__).resolve().parent / "ocr_mineru_quotas.json"


def _load_quota() -> dict:
    if QUOTA_FILE.exists():
        try:
            return json.loads(QUOTA_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_quota(quota: dict) -> None:
    QUOTA_FILE.write_text(json.dumps(quota, ensure_ascii=False, indent=2), encoding="utf-8")


def _token_key(token: str) -> str:
    """Хеш токена для файла квот (не храним секреты в открытом виде)."""
    return hashlib.sha256(token.encode()).hexdigest()[:12]


def remaining_pages(token: str) -> int:
    if MINERU_TOKEN_QUOTA <= 0:
        return 1  # квота не настроена - токен доступен
    used = _load_quota().get(_token_key(token), 0)
    return MINERU_TOKEN_QUOTA - used


def spend_pages(token: str, pages: int) -> None:
    if pages <= 0:
        return
    quota = _load_quota()
    key = _token_key(token)
    quota[key] = quota.get(key, 0) + pages
    _save_quota(quota)


def choose_token() -> str:
    """Токен с наибольшим остатком квоты (персист между запусками)."""
    if not MINERU_TOKENS:
        raise RuntimeError(
            "MinerU токен не задан. Установите MINERU_TOKENS или MINERU_TOKEN "
            "в .env (страница apiManage)."
        )
    return max(MINERU_TOKENS, key=remaining_pages)

# Модель распознавания: "pipeline" (быстрее, старая модель) или "vlm" (рекомендуется,
# точнее на сложных таблицах/формулах/схемах). MinerU-HTML не подходит для наших PDF.
MODEL_VERSION = "vlm"

# Принудительно включить OCR (для сканов - обязательно True).
IS_OCR = True

# Распознавание формул и таблиц - критично для учебников СЦБ.
ENABLE_FORMULA = True
ENABLE_TABLE = True

# Язык документа. Для русскоязычных советских/российских учебников лучше "cyrillic"
# (охватывает русский + смежные кириллические языки), а не "ch" (китайский по умолчанию!).
LANGUAGE = "cyrillic"

# Доп. форматы экспорта помимо markdown/json (доступны: docx, html, latex).
# html включает в zip content_list.json с table_body (HTML-таблицы) —
# источник для этапа сериализации таблиц (tables_serialization.py).
EXTRA_FORMATS: list[str] = ["html"]

# Сколько файлов отправлять в одном batch-запросе (лимит API - не более 50).
BATCH_SIZE = 14

# Ограничения API (для валидации перед отправкой)
MAX_FILE_SIZE_MB = 200
MAX_PAGES = 200

# Реальный размер частей при разбиении крупных PDF. Ставим ЗАМЕТНО МЕНЬШЕ MAX_PAGES
# (не 200, а например 190), чтобы оставить запас - некоторые PDF после разбиения
# могут получать чуть больше страниц из-за особенностей рендера, и лучше не биться
# точно об лимит. Настройте под свои файлы, если знаете точные размеры книг.
SPLIT_PAGE_SIZE = 190

# Таймаут ожидания завершения batch-задачи (секунды) и интервал опроса.
POLL_TIMEOUT = 3600      # 1 час на весь batch (сканы старых книг обрабатываются небыстро)
POLL_INTERVAL = 15

SKIP_EXISTING = True


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

API_BASE = "https://mineru.net/api/v4"

# Метка, которой помечаем части разбитого файла в имени: stem__part001.pdf
PART_PATTERN = re.compile(r"^(?P<stem>.+)__part(?P<idx>\d+)$")


def get_headers(token: str) -> dict:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }


def split_pdf(pdf_path: Path, work_dir: Path, page_size: int) -> list[Path]:
    """
    Разбивает крупный PDF на части по page_size страниц.
    Части сохраняются как {stem}__part001.pdf, {stem}__part002.pdf, ...
    Возвращает список путей к частям в правильном порядке.
    """
    doc = fitz.open(pdf_path)
    total_pages = doc.page_count

    parts = []
    part_idx = 1
    for start in range(0, total_pages, page_size):
        end = min(start + page_size, total_pages)
        part_doc = fitz.open()
        part_doc.insert_pdf(doc, from_page=start, to_page=end - 1)

        part_name = f"{pdf_path.stem}__part{part_idx:03d}.pdf"
        part_path = work_dir / part_name
        part_doc.save(str(part_path))
        part_doc.close()

        parts.append(part_path)
        log.info(f"  Часть {part_idx}: страницы {start + 1}-{end} -> {part_name}")
        part_idx += 1

    doc.close()
    return parts


def preprocess_files(ocr_input_dir: Path, work_dir: Path) -> dict[str, list[Path]]:
    """
    Проходит по всем PDF в ocr_input_dir. Файлы больше MAX_PAGES разбивает на части
    и складывает их в work_dir. Возвращает словарь {original_stem: [пути к частям]},
    где для файлов, не требующих разбиения, список содержит один элемент - сам файл.
    """
    work_dir.mkdir(parents=True, exist_ok=True)

    pdf_files = sorted(p for p in ocr_input_dir.glob("*.pdf") if work_dir not in p.parents)

    mapping: dict[str, list[Path]] = {}

    for pdf_path in pdf_files:
        doc = fitz.open(pdf_path)
        page_count = doc.page_count
        doc.close()

        if page_count > MAX_PAGES:
            log.info(f"{pdf_path.name}: {page_count} стр. > лимита {MAX_PAGES} - разбиваем на части")
            parts = split_pdf(pdf_path, work_dir, SPLIT_PAGE_SIZE)
            mapping[pdf_path.stem] = parts
        else:
            mapping[pdf_path.stem] = [pdf_path]

    return mapping


def validate_file(pdf_path: Path) -> bool:
    """Проверяет ограничения API перед отправкой (размер файла)."""
    size_mb = pdf_path.stat().st_size / (1024 * 1024)
    if size_mb > MAX_FILE_SIZE_MB:
        log.error(f"{pdf_path.name}: размер {size_mb:.1f}MB превышает лимит {MAX_FILE_SIZE_MB}MB - пропущен")
        return False
    return True


def already_processed(original_stem: str, md_output_dir: Path) -> bool:
    """Проверяет, прошла ли папка этап OCR (manifest.json), иначе fallback на наличие full.md."""
    if not SKIP_EXISTING:
        return False
    result_dir = md_output_dir / original_stem
    if has_stage(result_dir, STAGE_MINERU):
        return True
    md_file = result_dir / "full.md"
    return md_file.exists()


def truncate_data_id(stem: str, max_len: int = 128) -> str:
    """
    MinerU ограничивает поле data_id максимум 128 символами. Имена файлов старых
    учебников (особенно после разбиения на части __part001 и т.д.) могут легко
    превысить этот лимит - обрезаем с конца, сохраняя хвост (там номер части),
    и добавляем короткий хэш от полного имени, чтобы избежать коллизий при
    обрезании двух разных длинных имён до одного и того же префикса.
    """
    if len(stem) <= max_len:
        return stem
    h = hashlib.md5(stem.encode("utf-8")).hexdigest()[:8]
    keep = max_len - len(h) - 1
    return f"{stem[:keep]}_{h}"


def request_upload_urls(file_batch: list[Path], token: str) -> tuple[str, list[str]]:
    """Запрашивает у MinerU signed upload URLs для батча файлов (частей или целых файлов)."""
    payload = {
        "files": [{"name": f.name, "data_id": truncate_data_id(f.stem)} for f in file_batch],
        "model_version": MODEL_VERSION,
        "is_ocr": IS_OCR,
        "enable_formula": ENABLE_FORMULA,
        "enable_table": ENABLE_TABLE,
        "language": LANGUAGE,
    }
    if EXTRA_FORMATS:
        payload["extra_formats"] = EXTRA_FORMATS

    resp = requests.post(f"{API_BASE}/file-urls/batch", headers=get_headers(token), json=payload, timeout=60)
    resp.raise_for_status()
    result = resp.json()

    if result["code"] != 0:
        raise RuntimeError(f"Ошибка запроса upload URLs: {result['msg']}")

    return result["data"]["batch_id"], result["data"]["file_urls"]


def upload_files(file_batch: list[Path], upload_urls: list[str]) -> None:
    """Загружает файлы по полученным signed URLs (PUT, без заголовков Content-Type)."""
    for pdf_path, upload_url in zip(file_batch, upload_urls):
        log.info(f"Загрузка: {pdf_path.name}")
        with open(pdf_path, "rb") as f:
            resp = requests.put(upload_url, data=f, timeout=300)
        if resp.status_code != 200:
            raise RuntimeError(f"Не удалось загрузить {pdf_path.name}: HTTP {resp.status_code}")
        log.info(f"Загружено: {pdf_path.name}")


def poll_batch_result(batch_id: str, token: str) -> list[dict]:
    """Опрашивает статус batch-задачи до завершения всех файлов (тем же токеном, что создал)."""
    start = time.time()
    url = f"{API_BASE}/extract-results/batch/{batch_id}"

    while time.time() - start < POLL_TIMEOUT:
        resp = requests.get(url, headers=get_headers(token), timeout=30)
        resp.raise_for_status()
        result = resp.json()

        if result["code"] != 0:
            raise RuntimeError(f"Ошибка опроса batch: {result['msg']}")

        extract_results = result["data"]["extract_result"]
        states = [item["state"] for item in extract_results]

        if all(s in ("done", "failed") for s in states):
            elapsed = int(time.time() - start)
            log.info(f"Batch {batch_id} завершён за {elapsed}с")
            return extract_results

        pending = sum(1 for s in states if s not in ("done", "failed"))
        log.info(f"Batch {batch_id}: ожидание ({pending} файлов в обработке)...")
        time.sleep(POLL_INTERVAL)

    raise TimeoutError(f"Batch {batch_id} не завершился за {POLL_TIMEOUT}с")


def remove_equation_images(result_dir: Path) -> int:
    """
    Читает content_list.json в result_dir, удаляет из images/ файлы, относящиеся
    к формулам (EQUATION_BLOCK_TYPES), и вычищает ссылки на них из full.md -
    формула уже присутствует как LaTeX-текст, картинка-дубликат не нужна и не
    должна попадать в описание изображений на следующем этапе пайплайна.
    Возвращает количество удалённых файлов.
    """
    candidates = list(result_dir.glob("*_content_list.json")) + list(result_dir.glob("content_list.json"))
    if not candidates:
        return 0

    try:
        data = json.loads(candidates[0].read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"Не удалось прочитать {candidates[0]}: {e}")
        return 0

    equation_img_names = set()
    removed = 0
    for block in data:
        if block.get("type") in EQUATION_BLOCK_TYPES:
            img_path = block.get("img_path")
            if not img_path:
                continue
            equation_img_names.add(Path(img_path).name)
            full_path = result_dir / img_path
            if full_path.exists():
                full_path.unlink()
                removed += 1

    if removed:
        log.info(f"  Удалено {removed} картинок-формул (дубликаты LaTeX из full.md)")

    # Вычищаем markdown-ссылки на удалённые картинки-формулы, чтобы в full.md
    # не остались битые ссылки вида ![](images/formula_12.jpg).
    md_path = result_dir / "full.md"
    if md_path.exists() and equation_img_names:
        md_text = md_path.read_text(encoding="utf-8")
        pattern = re.compile(r"!\[[^\]]*\]\([^)]*(?:" + "|".join(re.escape(n) for n in equation_img_names) + r")\)\n?")
        cleaned_text, n_removed_links = pattern.subn("", md_text)
        if n_removed_links:
            md_path.write_text(cleaned_text, encoding="utf-8")
            log.info(f"  Удалено {n_removed_links} ссылок на картинки-формулы из full.md")

    return removed


def download_and_extract_part(file_name: str, zip_url: str, staging_dir: Path) -> Path:
    """
    Скачивает zip с результатом части, распаковывает в staging_dir/{part_stem}/,
    затем удаляет картинки-формулы (см. remove_equation_images) - они не нужны,
    т.к. формула уже вписана как текст/LaTeX в сам full.md.
    """
    stem = Path(file_name).stem
    result_dir = staging_dir / stem
    result_dir.mkdir(parents=True, exist_ok=True)

    zip_path = result_dir / "_result.zip"
    log.info(f"Скачивание результата: {file_name}")

    resp = requests.get(zip_url, timeout=120)
    resp.raise_for_status()
    zip_path.write_bytes(resp.content)

    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(result_dir)

    zip_path.unlink()

    remove_equation_images(result_dir)

    log.info(f"Готово: {result_dir}")
    return result_dir


def merge_content_list(final_dir: Path, part_entries: list[tuple[int, Path | None]]) -> None:
    """
    Склеивает *_content_list.json частей в единый final_dir/content_list.json,
    сдвигая page_idx на число страниц предыдущих частей (иначе граница частей
    ломает контекст страницы для сериализации таблиц).
    Если ни у одной части content_list.json нет — ничего не делает.
    """
    part_files: list[tuple[int, Path]] = []
    for part_idx, part_dir in part_entries:
        if part_dir is None:
            continue
        candidates = list(part_dir.glob("*_content_list.json")) + list(part_dir.glob("content_list.json"))
        if candidates:
            part_files.append((part_idx, candidates[0]))

    if not part_files:
        return

    merged: list[dict] = []
    page_offset = 0
    for part_idx, cl_path in sorted(part_files):
        try:
            blocks = json.loads(cl_path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning(f"Не удалось прочитать {cl_path}: {e}")
            continue
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if isinstance(block.get("page_idx"), int):
                block["page_idx"] += page_offset
            merged.append(block)
        page_offset += max((b.get("page_idx") for b in blocks if isinstance(b.get("page_idx"), int)), default=-1) + 1

    if merged:
        final_dir.mkdir(parents=True, exist_ok=True)
        (final_dir / "content_list.json").write_text(
            json.dumps(merged, ensure_ascii=False), encoding="utf-8"
        )
        log.info(f"  content_list.json: {len(merged)} блоков (для сериализации таблиц)")


def merge_parts(original_stem: str, part_entries: list[tuple[int, Path | None]], md_output_dir: Path) -> Path:
    """
    Склеивает результаты частей одного оригинального PDF в единый full.md
    и объединённую папку images/ (с переименованием картинок, чтобы избежать
    коллизий имён между частями).

    part_entries - список (номер_части, путь_к_результату_или_None).
    Если путь None (часть не обработалась из-за ошибки/сбоя API), в итоговый
    markdown вставляется явный маркер-заглушка, а не пропускается молча -
    склейка ВСЕГДА производит единый full.md файл, даже при частичном сбое.
    """
    final_dir = md_output_dir / original_stem
    final_images_dir = final_dir / "images"
    final_dir.mkdir(parents=True, exist_ok=True)
    final_images_dir.mkdir(parents=True, exist_ok=True)

    combined_md = []
    failed_parts = []

    for part_idx, part_dir in part_entries:
        if part_dir is None:
            marker = f"\n\n> **[ОШИБКА OCR: часть {part_idx} не распознана. Требуется повторная обработка или ручная вставка.]**\n\n"
            combined_md.append(marker)
            failed_parts.append(part_idx)
            log.warning(f"{original_stem}: часть {part_idx} отсутствует, вставлен маркер-заглушка")
            continue

        part_md_path = part_dir / "full.md"
        if not part_md_path.exists():
            marker = f"\n\n> **[ОШИБКА OCR: часть {part_idx} не содержит full.md. Требуется повторная обработка.]**\n\n"
            combined_md.append(marker)
            failed_parts.append(part_idx)
            log.warning(f"Не найден full.md в {part_dir}, вставлен маркер-заглушка")
            continue

        md_text = part_md_path.read_text(encoding="utf-8")

        part_images_dir = part_dir / "images"
        if part_images_dir.exists():
            for img_path in sorted(part_images_dir.glob("*")):
                new_name = f"part{part_idx:03d}_{img_path.name}"
                shutil.copy2(img_path, final_images_dir / new_name)
                md_text = md_text.replace(f"images/{img_path.name}", f"images/{new_name}")

        combined_md.append(md_text)

    final_md_path = final_dir / "full.md"
    final_md_path.write_text("\n\n".join(combined_md), encoding="utf-8")

    merge_content_list(final_dir, part_entries)

    n_images = len(list(final_images_dir.glob("*"))) if final_images_dir.exists() else 0
    mark_stage_done(
        final_dir,
        STAGE_MINERU,
        source="mineru",
        page_count=len(part_entries),
        images_count=n_images,
    )

    if failed_parts:
        log.warning(f"Файл склеен С ПРОБЕЛАМИ: {final_dir} (не распознаны части: {failed_parts})")
    else:
        log.info(f"Части склеены полностью: {final_dir} ({len(part_entries)} частей)")

    return final_dir


def count_pages(file_batch: list[Path]) -> int:
    """Суммарное число страниц батча (для списания квоты)."""
    total = 0
    for pdf_path in file_batch:
        doc = fitz.open(pdf_path)
        total += doc.page_count
        doc.close()
    return total


def process_batch(file_batch: list[Path], staging_dir: Path) -> dict:
    """Полный цикл обработки одного батча: upload -> poll -> download (без склейки частей)."""
    token = choose_token()
    batch_id, upload_urls = request_upload_urls(file_batch, token)
    log.info(f"Batch создан: {batch_id}, файлов: {len(file_batch)}")

    upload_files(file_batch, upload_urls)

    extract_results = poll_batch_result(batch_id, token)
    spend_pages(token, count_pages(file_batch))

    outcomes = {"success": {}, "failed": []}
    for item in extract_results:
        file_name = item["file_name"]
        if item["state"] == "done":
            try:
                result_dir = download_and_extract_part(file_name, item["full_zip_url"], staging_dir)
                outcomes["success"][Path(file_name).stem] = result_dir
            except Exception as e:
                log.error(f"Ошибка скачивания результата {file_name}: {e}")
                outcomes["failed"].append(file_name)
        else:
            log.error(f"Не удалось распознать {file_name}: {item.get('err_msg', 'неизвестная ошибка')}")
            outcomes["failed"].append(file_name)

    return outcomes


def run_pipeline(ocr_input_dir: Path, md_output_dir: Path, split_work_dir: Path) -> dict:
    """
    Точка входа:
    1. Разбивает крупные PDF на части (если нужно).
    2. Собирает все файлы/части в единый список, режет на батчи API, обрабатывает.
    3. Склеивает результаты частей обратно в единый .md на оригинальный файл.
    """
    md_output_dir.mkdir(parents=True, exist_ok=True)

    mapping = preprocess_files(ocr_input_dir, split_work_dir)
    if not mapping:
        log.warning(f"Не найдено .pdf файлов в {ocr_input_dir}")
        return {"success": [], "partial": [], "failed": [], "skipped": []}

    # Плоский список всех частей/файлов, которые реально нужно отправить в API
    to_process: list[Path] = []
    skipped: list[str] = []

    for original_stem, parts in mapping.items():
        if already_processed(original_stem, md_output_dir):
            result_dir = md_output_dir / original_stem
            if not has_stage(result_dir, STAGE_MINERU):
                images_dir = result_dir / "images"
                mark_stage_done(
                    result_dir,
                    STAGE_MINERU,
                    source="mineru",
                    images_count=len(list(images_dir.glob("*"))) if images_dir.exists() else 0,
                )
            log.info(f"Пропуск (уже обработан): {original_stem}")
            skipped.append(original_stem)
            continue
        for part in parts:
            if validate_file(part):
                to_process.append(part)
            else:
                skipped.append(part.name)

    log.info(f"К обработке: {len(to_process)} файлов/частей, пропущено: {len(skipped)}")

    staging_dir = split_work_dir / "_staging"
    staging_dir.mkdir(parents=True, exist_ok=True)

    all_part_results: dict[str, Path] = {}
    all_failed: list[str] = []

    for i in range(0, len(to_process), BATCH_SIZE):
        batch = to_process[i : i + BATCH_SIZE]
        log.info(f"--- Батч {i // BATCH_SIZE + 1} ({len(batch)} файлов/частей) ---")
        try:
            outcome = process_batch(batch, staging_dir)
            all_part_results.update(outcome["success"])
            all_failed.extend(outcome["failed"])
        except Exception as e:
            log.error(f"Batch провалился целиком: {e}")
            all_failed.extend([f.name for f in batch])

    # Склейка частей обратно в единый файл на каждый оригинальный документ.
    # ВАЖНО: склейка производится ВСЕГДА, даже если часть файлов не распозналась -
    # недостающие части заменяются явным маркером-заглушкой в тексте, а не пропуском.
    all_success: list[str] = []
    all_partial: list[str] = []
    for original_stem, parts in mapping.items():
        if original_stem in skipped:
            continue

        part_entries: list[tuple[int, Path | None]] = []
        has_failure = False
        for part_idx, part in enumerate(parts, start=1):
            result_dir = all_part_results.get(part.stem)
            if result_dir is None:
                has_failure = True
            part_entries.append((part_idx, result_dir))

        try:
            merge_parts(original_stem, part_entries, md_output_dir)
            if has_failure:
                all_partial.append(original_stem)
            else:
                all_success.append(original_stem)
        except Exception as e:
            log.error(f"Критическая ошибка склейки {original_stem}: {e}")
            all_failed.append(original_stem)

    # Очистка временных файлов (разбитые части и staging)
    if split_work_dir.exists():
        shutil.rmtree(split_work_dir, ignore_errors=True)

    log.info(
        f"Итог: {len(all_success)} успешно, {len(all_partial)} с пробелами (частично), "
        f"{len(all_failed)} с ошибками (не склеены), {len(skipped)} пропущено"
    )
    if all_partial:
        log.warning(f"Файлы с пробелами (проверьте маркеры [ОШИБКА OCR] в тексте): {all_partial}")
    if all_failed:
        log.warning(f"Файлы с ошибками (не склеены совсем): {all_failed}")

    return {"success": all_success, "partial": all_partial, "failed": all_failed, "skipped": skipped}


if __name__ == "__main__":
    outcome = run_pipeline(OCR_INPUT_DIR, MD_OUTPUT_DIR, SPLIT_WORK_DIR)
    print(f"\nУспешно обработано (без пробелов): {len(outcome['success'])}")
    print(f"Склеено частично (с маркерами ошибок): {len(outcome['partial'])}")
    print(f"Не склеено (критическая ошибка): {len(outcome['failed'])}")
    print(f"Пропущено (уже готово ранее): {len(outcome['skipped'])}")
