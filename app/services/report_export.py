"""Excel files of the report tables ("Excel'e Aktar").

One sheet per file: the report's name and what it covers on top, then the
table as the panel shows it — numbers as numbers, so the sheet can sum and
sort them, with the header frozen and filterable.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from typing import Any
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

TR = ZoneInfo("Europe/Istanbul")
HEADER_ROW = 4
FORMATS = {
    "int": "#,##0",
    "money": '#,##0.00 "₺"',
    "percent": '0.0"%"',
    "date": "dd.mm.yyyy",
    "datetime": "dd.mm.yyyy hh:mm",
}


@dataclass(frozen=True)
class Column:
    header: str
    value: Callable[[Any], Any]
    # text, int, money, percent, date, datetime or bool
    kind: str = "text"


def _cell(value: Any, kind: str) -> Any:
    if value is None:
        return None
    if kind == "bool":
        return "Evet" if value else "Hayır"
    if isinstance(value, datetime) and value.tzinfo is not None:
        # Excel has no time zones: the shop's own clock.
        return value.astimezone(TR).replace(tzinfo=None)
    if hasattr(value, "value") and not isinstance(value, (int, float, str)):
        return value.value  # an enum
    return value


def workbook(title: str, subtitle: str, columns: Sequence[Column], rows: Sequence[Any]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = title[:31]
    ws.append([title])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append([subtitle])
    ws["A2"].font = Font(color="666666")
    ws.append([])
    ws.append([c.header for c in columns])
    for cell in ws[HEADER_ROW]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="EEEEEE")

    widths = [len(c.header) for c in columns]
    for row in rows:
        values = [_cell(c.value(row), c.kind) for c in columns]
        ws.append(values)
        for i, v in enumerate(values):
            widths[i] = max(widths[i], len(str(v)) if v is not None else 0)

    last_row = HEADER_ROW + len(rows)
    for i, column in enumerate(columns, start=1):
        letter = get_column_letter(i)
        ws.column_dimensions[letter].width = min(max(widths[i - 1] + 2, 10), 50)
        fmt = FORMATS.get(column.kind)
        if fmt:
            for (cell,) in ws.iter_rows(min_row=HEADER_ROW + 1, max_row=last_row, min_col=i, max_col=i):
                cell.number_format = fmt
    ws.freeze_panes = f"A{HEADER_ROW + 1}"
    ws.auto_filter.ref = f"A{HEADER_ROW}:{get_column_letter(len(columns))}{max(last_row, HEADER_ROW)}"

    out = BytesIO()
    wb.save(out)
    return out.getvalue()
