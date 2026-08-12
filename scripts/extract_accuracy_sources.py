from __future__ import annotations

import csv
import sys
from pathlib import Path

import openpyxl


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit("usage: extract_accuracy_sources.py OUTPUT_DIR INPUT.xlsx...")
    output = Path(sys.argv[1])
    output.mkdir(parents=True, exist_ok=True)
    for index, source_name in enumerate(sys.argv[2:], start=1):
        source = Path(source_name)
        workbook = openpyxl.load_workbook(source, read_only=True, data_only=False)
        sheet = workbook["数据明细"]
        target = output / f"source_{index}.csv"
        count = 0
        with target.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            for count, row in enumerate(sheet.iter_rows(values_only=True)):
                writer.writerow(row)
        print(f"{source.name}\t{target}\t{max(count, 0)}")


if __name__ == "__main__":
    main()
