from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pandas as pd

TARGET_SHEET = "Sheet1"
MAX_HEADER_SCAN_ROWS = 20


def detect_header_row(preview: pd.DataFrame) -> int:
    best_row_index = 0
    best_score = -1

    for row_index in range(min(len(preview), MAX_HEADER_SCAN_ROWS)):
        row = preview.iloc[row_index]
        non_empty_cells = [cell for cell in row if not is_empty(cell)]
        unique_values = {normalize_cell(cell) for cell in non_empty_cells}
        string_count = sum(isinstance(cell, str) for cell in non_empty_cells)
        score = (len(non_empty_cells) * 3) + (len(unique_values) * 2) + string_count

        if score > best_score:
            best_score = score
            best_row_index = row_index

    return best_row_index


def is_empty(value: Any) -> bool:
    return pd.isna(value) or str(value).strip() == ""


def normalize_cell(value: Any) -> str:
    return str(value).strip()


def clean_value(value: Any) -> str:
    if pd.isna(value):
        return ""
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except TypeError:
            pass
    return str(value).strip()


def print_section(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inspect an Excel workbook and print a simple profiling report.",
    )
    parser.add_argument("workbook_path", help="Path to the .xlsx workbook to inspect.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    workbook_path = Path(args.workbook_path).expanduser().resolve()

    if not workbook_path.exists():
        print("Excel Exploration Report")
        print("========================")
        print(f"Workbook not found: {workbook_path}")
        return

    excel_file = pd.ExcelFile(workbook_path)

    print("Excel Exploration Report")
    print("========================")
    print(f"Workbook: {workbook_path.name}")
    print(f"Path: {workbook_path}")

    print_section("Worksheet Names")
    for sheet_name in excel_file.sheet_names:
        print(f"- {sheet_name}")

    if TARGET_SHEET not in excel_file.sheet_names:
        print_section("Sheet Error")
        print(f"Worksheet '{TARGET_SHEET}' was not found.")
        return

    preview = pd.read_excel(
        workbook_path,
        sheet_name=TARGET_SHEET,
        header=None,
        nrows=MAX_HEADER_SCAN_ROWS,
    )
    header_row_index = detect_header_row(preview)
    header_row = preview.iloc[header_row_index].tolist()
    column_names = [clean_value(value) or f"unnamed_{index + 1}" for index, value in enumerate(header_row)]

    data_frame = pd.read_excel(
        workbook_path,
        sheet_name=TARGET_SHEET,
        header=header_row_index,
    )
    data_frame.columns = column_names

    print_section("Header Row")
    print(f"Detected row number: {header_row_index + 1}")
    print("Values:")
    print(" | ".join(column_names))

    print_section("Column Names")
    for column_name in column_names:
        print(f"- {column_name}")

    print_section("First 5 Records")
    if data_frame.empty:
        print("No data rows found below the detected header.")
        return

    preview_records = data_frame.head(5).fillna("")
    print(preview_records.to_string(index=False))


if __name__ == "__main__":
    main()
