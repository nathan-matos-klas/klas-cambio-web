from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
CACHE_PATH = ROOT / "cambio_cache.json"
SPREADS_PATH = ROOT / "spreads.json"

HOST = os.getenv("CAMBIO_HOST", "127.0.0.1")
PORT = int(os.getenv("CAMBIO_PORT", "8000"))
UPDATE_HOUR = int(os.getenv("CAMBIO_UPDATE_HOUR", "11"))
UPDATE_MINUTE = int(os.getenv("CAMBIO_UPDATE_MINUTE", "30"))
API_URL = os.getenv("CAMBIO_API_URL", "").strip()
API_KEY = os.getenv("CAMBIO_API_KEY", "").strip()
API_AUTH_MODE = os.getenv("CAMBIO_API_AUTH_MODE", "url").strip().lower()
API_HEADERS_JSON = os.getenv("CAMBIO_API_HEADERS_JSON", "").strip()
BASE_CURRENCY = os.getenv("CAMBIO_BASE_CURRENCY", "USD").strip().upper() or "USD"

CURRENCIES = ["USD", "EUR", "GBP", "AED", "ZAR"]
DEFAULT_LABELS = {
    "USD": "Dólar (USD)",
    "EUR": "Euro (EUR)",
    "GBP": "Libra (GBP)",
    "AED": "Dirham (AED)",
    "ZAR": "Rand (ZAR)",
}


def _today_key() -> str:
    return datetime.now().date().isoformat()


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _load_cache() -> dict[str, Any]:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {
        "updated_at": None,
        "fetched_on": None,
        "attempted_on": None,
        "source": API_URL or None,
        "base_currency": BASE_CURRENCY,
        "brl_rate": None,
        "rates": [
            {"code": code, "name": DEFAULT_LABELS[code], "rate": None, "brl_per_unit": None}
            for code in CURRENCIES
        ],
        "error": "cache vazio",
        "spread_applied": False,
    }


def _save_cache(payload: dict[str, Any]) -> None:
    CACHE_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _load_spreads() -> dict[str, float]:
    if not SPREADS_PATH.exists():
        return {code: 0.0 for code in CURRENCIES}

    try:
        raw = json.loads(SPREADS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {code: 0.0 for code in CURRENCIES}

    spreads: dict[str, float] = {}
    for code in CURRENCIES:
        value = _to_float(raw.get(code)) if isinstance(raw, dict) else None
        spreads[code] = 0.0 if value is None else max(value, 0.0)
    return spreads


def _load_api_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "User-Agent": "cambio-web-backend/1.0",
    }

    if not API_HEADERS_JSON:
        return headers

    try:
        extra_headers = json.loads(API_HEADERS_JSON)
    except json.JSONDecodeError:
        return headers

    if isinstance(extra_headers, dict):
        for key, value in extra_headers.items():
            if value is not None:
                headers[str(key)] = str(value)

    return headers


def _build_api_request() -> Request:
    if API_URL:
        return Request(API_URL, headers=_load_api_headers())

    if not API_KEY:
        raise RuntimeError("Defina CAMBIO_API_KEY ou CAMBIO_API_URL.")

    if API_AUTH_MODE == "bearer":
        url = f"https://v6.exchangerate-api.com/v6/latest/{BASE_CURRENCY}"
        headers = {
            "Accept": "application/json",
            "User-Agent": "cambio-web-backend/1.0",
            "Authorization": f"Bearer {API_KEY}",
        }
        return Request(url, headers=headers)

    url = f"https://v6.exchangerate-api.com/v6/{API_KEY}/latest/{BASE_CURRENCY}"
    headers = {
        "Accept": "application/json",
        "User-Agent": "cambio-web-backend/1.0",
    }
    return Request(url, headers=headers)


def _first_present(mapping: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return None


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    if isinstance(value, str):
        normalized = value.strip().replace("R$", "").replace(" ", "").replace(",", ".")
        try:
            return round(float(normalized), 2)
        except ValueError:
            return None
    return None


def _normalize_rates(payload: Any) -> list[dict[str, Any]]:
    rates: list[dict[str, Any]] = []

    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict) and isinstance(payload.get("rates"), list):
        items = payload["rates"]
    elif isinstance(payload, dict):
        items = []
        for code in CURRENCIES:
            if code in payload:
                items.append({"code": code, "rate": payload[code]})
    else:
        items = []

    for item in items:
        if isinstance(item, dict):
            code = str(_first_present(item, ["code", "currency", "moeda", "sigla"]) or "").upper()
            if not code:
                continue
            rate = _to_float(_first_present(item, ["rate", "valor", "cambio", "value", "buy", "cotacao"]))
            name = _first_present(item, ["name", "label", "nome"]) or DEFAULT_LABELS.get(code, code)
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            code = str(item[0]).upper()
            rate = _to_float(item[1])
            name = DEFAULT_LABELS.get(code, code)
        else:
            continue

        if code not in CURRENCIES:
            continue

        rates.append(
            {
                "code": code,
                "name": name,
                "rate": rate,
            }
        )

    indexed = {item["code"]: item for item in rates}
    ordered_rates = []
    for code in CURRENCIES:
        item = indexed.get(code)
        if item is None:
            item = {"code": code, "name": DEFAULT_LABELS[code], "rate": None}
        ordered_rates.append(item)

    return ordered_rates


def _apply_spreads(rates: list[dict[str, Any]], spreads: dict[str, float], brl_rate: float | None) -> list[dict[str, Any]]:
    adjusted: list[dict[str, Any]] = []
    for item in rates:
        code = item.get("code")
        base_rate = _to_float(item.get("rate"))
        spread = float(spreads.get(code, 0.0) or 0.0)
        adjusted_rate = None if base_rate is None else round(base_rate * (1 + spread), 2)
        brl_per_unit = None
        if brl_rate is not None and adjusted_rate not in {None, 0}:
            brl_per_unit = round(brl_rate / adjusted_rate, 2)

        adjusted.append(
            {
                "code": code,
                "name": item.get("name", DEFAULT_LABELS.get(code, code)),
                "base_rate": base_rate,
                "spread": spread,
                "rate": adjusted_rate,
                "brl_per_unit": brl_per_unit,
            }
        )
    return adjusted


def fetch_remote_rates() -> dict[str, Any]:
    request = _build_api_request()

    with urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))

    if isinstance(payload, dict) and payload.get("result") not in {None, "success"}:
        error_type = payload.get("error-type") or payload.get("error_type") or "unknown-error"
        raise RuntimeError(f"ExchangeRate-API error: {error_type}")

    rates_source = payload.get("conversion_rates") if isinstance(payload, dict) else payload
    brl_rate = None
    if isinstance(payload, dict) and isinstance(payload.get("conversion_rates"), dict):
        raw_brl_rate = payload["conversion_rates"].get("BRL")
        brl_rate = _to_float(raw_brl_rate)
        if brl_rate is None and BASE_CURRENCY == "BRL":
            brl_rate = 1.0
    elif BASE_CURRENCY == "BRL":
        brl_rate = 1.0

    spreads = _load_spreads()
    normalized = {
        "updated_at": _first_present(
            payload if isinstance(payload, dict) else {},
            ["time_last_update_utc", "updated_at", "timestamp", "last_update"],
        ) or _now_iso(),
        "fetched_on": _today_key(),
        "source": API_URL or f"ExchangeRate-API standard endpoint ({BASE_CURRENCY})",
        "base_currency": BASE_CURRENCY,
        "brl_rate": brl_rate,
        "rates": _apply_spreads(_normalize_rates(rates_source), spreads, brl_rate),
        "error": None,
        "spread_applied": True,
    }
    return normalized


def refresh_cache(force: bool = False) -> dict[str, Any]:
    cached = _load_cache()
    today = _today_key()
    if not force and cached.get("attempted_on") == today:
        return cached

    cached["attempted_on"] = today
    _save_cache(cached)

    try:
        payload = fetch_remote_rates()
    except (HTTPError, URLError, TimeoutError, ValueError, RuntimeError) as exc:
        cached["fetched_on"] = cached.get("fetched_on")
        cached["error"] = str(exc)
        _save_cache(cached)
        return cached

    payload["attempted_on"] = today
    _save_cache(payload)
    return payload


def seconds_until_next_run(hour: int, minute: int) -> float:
    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def schedule_refresh_loop() -> None:
    while True:
        time.sleep(seconds_until_next_run(UPDATE_HOUR, UPDATE_MINUTE))
        try:
            refresh_cache()
        except Exception:
            continue


class CambioHandler(BaseHTTPRequestHandler):
    def _send_json(self, payload: dict[str, Any], status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        if self.path in {"/", "/api/cambio"}:
            self._send_json(_load_cache())
            return
        if self.path == "/api/health":
            self._send_json({"ok": True, "time": _now_iso()})
            return
        self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if self.path == "/api/refresh":
            self._send_json(refresh_cache())
            return
        self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args: Any) -> None:
        return


def main() -> None:
    _save_cache(_load_cache())
    refresh_cache()

    thread = threading.Thread(target=schedule_refresh_loop, daemon=True)
    thread.start()

    server = ThreadingHTTPServer((HOST, PORT), CambioHandler)
    print(f"Servidor de câmbio em http://{HOST}:{PORT}")
    print(f"Atualização automática agendada para {UPDATE_HOUR:02d}:{UPDATE_MINUTE:02d}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
