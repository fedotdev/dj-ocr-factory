# Проект: Пайплайн djvu → md (СЦБ)

Конвертация сканов/PDF учебников СЦБ (железнодорожная автоматика) в Markdown + OCR (MinerU), реставрация обложек (NanoBanana-2), сериализация таблиц (LLM), сборка docx (Pandoc), далее RAG.

## Обвязка (важно для работы с пользователем)

- **Obsidian vault**: `C:\Users\fedor\OneDrive - РУТ (МИИТ)\other stuff\ObsidianFedor`
- **План работ (todo)**: `C:\Users\fedor\OneDrive - РУТ (МИИТ)\other stuff\ObsidianFedor\todo\Пайплайн djvu→md (СЦБ).md`
  - Журнал статусов этапов ведётся в этом файле. Обновлять его при завершении этапов.
- **Журнал разработки**: `.agents/scb-bd-development.md` (русский, история решений).
- Персональный AGENTS: `C:\Users\fedor\.config\opencode\AGENTS.md`.

## Структура проекта

| Папка | Назначение |
|-------|-----------|
| `1-djvu_input` | исходные .djvu |
| `2-pdf_output` | djvu → PDF |
| `3-md_output/{name}/` | full.md + images/ + content_list.json + tables_serialized.json + cover_restored.png + manifest.json |
| `4-ocr_input` | сканы под MinerU |

## Скрипты

- `djvu_to_pdf.py` — djvu→PDF через ddjvu (системный пакет)
- `route_pdf.py` — маршрутизация: текст-слой → md (pymupdf4llm с images/), скан → 4-ocr_input
- `ocr_mineru.py` — OCR MinerU API: пул токенов, квота, split ≤190 стр, merge_parts, content_list.json
- `restore_cover.py` — обложка: PDF→PNG→imgbb→NanoBanana-2 (`/generate-2`)→cover_restored.png
- `tables_serialization.py` — таблицы → LLM (Gemini через OpenAI-совместимый эндпоинт)
- `fix_ocr.py` — локальный словарный фикс OCR-ошибок (pymorphy3, edit-distance, склейка разрывов)
- `artifact_contract.py` — manifest.json контракт этапов (read/write/mark_stage_done/has_stage)
- `constants.py` — общие пути и настройки

## Ключевые решения (зафиксировано)

- Путь к vault и todo-файлу хранить в этом файле, чтобы не переспрашивать пользователя.
- Единая схема папок: `1-djvu_input`, `2-pdf_output`, `3-md_output`, `4-ocr_input`.
- Обложка через NanoBanana-2 (`/generate-2`), а не `/generate-pro`.
- Схема правильных ответов через Pydantic.