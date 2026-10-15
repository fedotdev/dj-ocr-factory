import re
import pathlib
from html.parser import HTMLParser


class TableConverter(HTMLParser):
    """Конвертирует <table>...</table> в markdown pipe-table."""

    def __init__(self):
        super().__init__()
        self.rows = []
        self._row = []
        self._cell = []
        self._in_cell = False

    def handle_starttag(self, tag, attrs):
        if tag in ("td", "th"):
            self._in_cell = True
            self._cell = []
        elif tag == "tr":
            self._row = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._in_cell:
            self._row.append("".join(self._cell).strip())
            self._in_cell = False
        elif tag == "tr":
            if self._row:
                self.rows.append(self._row)

    def handle_data(self, data):
        if self._in_cell:
            self._cell.append(data.strip())

    def to_pipe_table(self):
        if not self.rows:
            return ""
        widths = [max(len(r[i]) if i < len(r) else 0
                       for r in self.rows) for i in range(len(self.rows[0]))]
        lines = []
        for ri, row in enumerate(self.rows):
            cells = [row[i].ljust(widths[i]) if i < len(row) else " " * widths[i]
                     for i in range(len(widths))]
            lines.append("| " + " | ".join(cells) + " |")
            if ri == 0:
                lines.append("| " + " | ".join("-" * w for w in widths) + " |")
        return "\n".join(lines)


def preprocess_md(md: str) -> str:
    """Пре-обработка markdown перед Pandoc."""
    # 1. HTML таблицы → pipe tables
    def replace_table(m):
        conv = TableConverter()
        conv.feed(m.group(0))
        return conv.to_pipe_table()
    md = re.sub(r"<table>.*?</table>", replace_table, md, flags=re.S)

    # 2. Убрать \tag{...} изнутри $$ блоков (Pandoc не понимает)
    md = re.sub(r"\\tag\{[^}]*\}", "", md)

    return md


def preprocess_file(src: pathlib.Path, dst: pathlib.Path):
    md = src.read_text(encoding="utf-8")
    dst.write_text(preprocess_md(md), encoding="utf-8")
