from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CACHE_PATH = ROOT / "cambio_cache.json"
HISTORICO_PATH = ROOT / "historico.csv"

CURRENCIES = ["USD", "EUR", "GBP", "AED", "ZAR"]
FIELDNAMES = ["data", "hora"] + [f"{c}_rate" for c in CURRENCIES] + [f"{C}_spread" for C in CURRENCIES]


def main() -> None:
    if not CACHE_PATH.exists():
        print("Cache não encontrado.")
        return

    cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))

    if cache.get("error"):
        print(f"Cache com erro, linha não gravada: {cache['error']}")
        return

    updated_at = cache.get("updated_at") or cache.get("fetched_on")
    if not updated_at:
        print("Cache sem data de atualização, linha não gravada.")
        return

    try:
        dt = datetime.fromisoformat(updated_at)
    except ValueError:
        dt = datetime.now()

    rates_by_code = {r["code"]: r for r in cache.get("rates", [])}

    row: dict[str, str] = {
        "data": dt.strftime("%d/%m/%Y"),
        "hora": dt.strftime("%H:%M"),
    }
    for code in CURRENCIES:
        r = rates_by_code.get(code, {})
        row[f"{code}_rate"] = str(r.get("rate") or "")
        row[f"{code}_spread"] = str(r.get("spread") or "")

    file_exists = HISTORICO_PATH.exists()
    with HISTORICO_PATH.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)

    print(f"Linha gravada: {row['data']} {row['hora']}")


if __name__ == "__main__":
    main()
