#!/usr/bin/env python3
"""
Сериализация таблиц (п. 6 плана).

Читает {book}/content_list.json (от MinerU, см. merge_content_list в ocr_mineru.py),
каждую HTML-таблицу с контекстом страницы отправляет в OpenAI-совместимый API
(structured output, схема TableBlocksCollection) и пишет:
  - {book}/tables_serialized.json - таблицы + сериализованные блоки;
  - в конец full.md секцию "Сериализация таблиц" (самодостаточные блоки).
Манифест: этап "tables".

Использование: python tables_serialization.py [book_dir ...]
Без аргументов обрабатывает все книги из 3-md_output.
"""

import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, Field

from constants import MD_OUTPUT_DIR, OPENAI_BASE_URL, OPENAI_API_KEY, OPENAI_MODEL, LLM_TEMPERATURE
from artifact_contract import mark_stage_done, has_stage

STAGE_TABLES = "tables"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("tables_serialization")


# --- Схема ответа (из прецедента RAG-Challenge-2, 1:1) ---

SYSTEM_PROMPT = (
    "You are a table serialization agent.\n"
    "Your task is to create a set of contextually independent blocks of information based on the provided table and surrounding text.\n"
    "These blocks must be totally context-independent because they will be used as separate chunk to populate database.\n"
    "This is railway engineering documentation (SCB - railway signalling). For tables describing equipment/parameters/"
    "schedules ALWAYS include device names, parameter names, units and footnotes in each information block."
)


class SerializedInformationBlock(BaseModel):
    """A single self-contained information block enriched with comprehensive context"""

    subject_core_entity: str = Field(description="A primary focus of what this block is about. Usually located in a row header. If one row in the table doesn't make sense without neighboring rows, you can merge information from neighboring rows into one block")
    information_block: str = Field(description=(
        "Detailed information about the chosen core subject from tables and additional texts. Information SHOULD include:\n"
        "1. All related header information\n"
        "2. All related units and their descriptions\n"
        "    2.1. If header is Total, always write additional context about what this total represents in this block!\n"
        "3. All additional info for context enrichment to make ensure complete context-independency if it present in whole table. This can include:\n"
        "    - The name of the table\n"
        "    - Additional footnotes\n"
        "    - The currency used\n"
        "    - The way amounts are presented\n"
        "    - Anything else that can make context even slightly richer\n"
        "SKIPPING ANY VALUABLE INFORMATION WILL BE HEAVILY PENALIZED!"
    ))


class TableBlocksCollection(BaseModel):
    """Collection of serialized table blocks with their core entities and header relationships"""

    subject_core_entities_list: list[str] = Field(
        description="A complete list of core entities. Keep in mind, empty headers are possible - they should also be interpreted and listed (Usually it's a total or something similar). In most cases each row header represents a core entity")
    relevant_headers_list: list[str] = Field(description="A list of ALL headers relevant to the subject. These headers will serve as keys in each information block. In most cases each column header represents a core entity")
    information_blocks: list[SerializedInformationBlock] = Field(description="Complete list of fully described context-independent information blocks")


# --- Нормализация content_list.json -> json_report прецедента ---

def to_json_report(content_list: list[dict]) -> dict:
    """MinerU content_list.json -> {content: [{page, content: [...]}], tables: [{table_id, page, html}]}."""
    pages: dict[int, list[dict]] = {}
    tables: list[dict] = []
    table_seq = 1

    for block in content_list:
        page_idx = int(block.get("page_idx", 0))
        btype = block.get("type")
        pages.setdefault(page_idx, [])

        if btype == "table":
            html = block.get("table_body") or ""
            if not html:
                continue
            pages[page_idx].append({"type": "table", "table_id": table_seq})
            tables.append({"table_id": table_seq, "page": page_idx + 1, "html": html})
            table_seq += 1
        else:
            text = block.get("text")
            if not text:
                continue
            # equation (LaTeX) тоже даёт полезный контекст для соседних таблиц
            pages[page_idx].append({"type": btype, "text": text})

    return {
        "content": [{"page": p + 1, "content": pages[p]} for p in sorted(pages)],
        "tables": tables,
    }


def get_table_context(json_report: dict, target_table_id: int) -> tuple[str, str]:
    """Текст между соседними таблицами на странице (контекст до/после) — из прецедента."""
    table_info = next(t for t in json_report["tables"] if t["table_id"] == target_table_id)
    page_num = table_info["page"]

    page_content = next((p["content"] for p in json_report["content"] if p["page"] == page_num), [])

    current = next(i for i, b in enumerate(page_content) if b.get("type") == "table" and b.get("table_id") == target_table_id)

    prev_pos = -1
    for i in range(current - 1, -1, -1):
        if page_content[i]["type"] == "table":
            prev_pos = i
            break

    next_pos = -1
    for i in range(current + 1, len(page_content)):
        if page_content[i]["type"] == "table":
            next_pos = i
            break

    start = prev_pos + 1 if prev_pos != -1 else 0
    context_before = page_content[start:current]

    if next_pos == -1:
        # после таблицы - текст до конца страницы
        context_after = page_content[current + 1:]
    else:
        # до следующей таблицы, если она на той же странице
        context_after = page_content[current + 1:next_pos]

    before = "\n".join(b.get("text", "") for b in context_before if "text" in b)
    after = "\n".join(b.get("text", "") for b in context_after if "text" in b)
    return before, after


# --- LLM-вызов ---

def _client() -> tuple[OpenAI, str]:
    load_dotenv()
    base_url = OPENAI_BASE_URL or os.getenv("OPENAI_BASE_URL")
    api_key = OPENAI_API_KEY or os.getenv("OPENAI_API_KEY") or "sk-stub"
    model = os.getenv("OPENAI_MODEL") or OPENAI_MODEL
    if not base_url:
        raise RuntimeError("OPENAI_BASE_URL не задан (constants.py или .env) - сериализация таблиц невозможна")
    return OpenAI(base_url=base_url, api_key=api_key, timeout=None, max_retries=2), model


def serialize_table(client: OpenAI, model: str, table_html: str, context_before: str, context_after: str) -> dict:
    user_prompt = ""
    if context_before:
        user_prompt += f'Here is additional text before the table that might be relevant (or not):\n"""{context_before}"""\n\n'
    user_prompt += f'Here is a table in HTML format:\n"""{table_html}"""'
    if context_after:
        user_prompt += f'\n\nHere is additional text after the table that might be relevant (or not):\n"""{context_after}"""'

    try:
        completion = client.beta.chat.completions.parse(
            model=model,
            temperature=LLM_TEMPERATURE,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            response_format=TableBlocksCollection,
        )
        return completion.choices[0].message.parsed.model_dump()
    except Exception as e:
        # # ponytail: fallback на текст->JSON для провайдеров без structured output;
        # ceiling: недетерминированный парс, включать если parse стабильно недоступен.
        raise RuntimeError(f"structured output недоступен: {e}") from e


# --- Обработка книги ---

def process_book(book_dir: Path) -> str:
    if has_stage(book_dir, STAGE_TABLES):
        return f"Пропуск (этап tables уже пройден): {book_dir.name}/"

    content_list_path = book_dir / "content_list.json"
    if not content_list_path.exists():
        return f"Пропуск (нет content_list.json): {book_dir.name}/"

    content_list = json.loads(content_list_path.read_text(encoding="utf-8"))
    json_report = to_json_report(content_list)

    if not json_report["tables"]:
        return f"Пропуск (нет таблиц): {book_dir.name}/"

    client, model = _client()
    for table in json_report["tables"]:
        ctx_before, ctx_after = get_table_context(json_report, table["table_id"])
        table["serialized"] = serialize_table(client, model, table["html"], ctx_before, ctx_after)
        log.info(f"  {book_dir.name}: таблица {table['table_id']}/{len(json_report['tables'])} сериализована")

    (book_dir / "tables_serialized.json").write_text(
        json.dumps(json_report["tables"], ensure_ascii=False, indent=2), encoding="utf-8")

    # Вставка в конец full.md (позиция в тексте не привязывается — хрупко)
    md_path = book_dir / "full.md"
    if md_path.exists():
        lines = ["", "", "## Сериализация таблиц", ""]
        for table in json_report["tables"]:
            lines.append(f"### Таблица {table['table_id']} (стр. {table['page']})")
            for block in table["serialized"]["information_blocks"]:
                lines.append(f"- {block['information_block']}")
        with md_path.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines))
        log.info(f"  {book_dir.name}: секция добавлена в full.md")

    mark_stage_done(book_dir, STAGE_TABLES, tables_count=len(json_report["tables"]))
    return f"Готово: {book_dir.name}/ ({len(json_report['tables'])} таблиц)"


def main() -> None:
    book_dirs = [Path(a) for a in sys.argv[1:]] or sorted(MD_OUTPUT_DIR.glob("*"))
    book_dirs = [d for d in book_dirs if d.is_dir()]
    if not book_dirs:
        log.warning(f"Нет папок книг в {MD_OUTPUT_DIR}")
        return
    for book_dir in book_dirs:
        try:
            log.info(process_book(book_dir))
        except Exception as e:
            log.error(f"{book_dir.name}: {e}")


if __name__ == "__main__":
    main()
