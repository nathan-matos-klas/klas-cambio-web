from __future__ import annotations

from backend import refresh_cache


def main() -> None:
    payload = refresh_cache()
    print(payload.get("updated_at") or "cache updated")
    if payload.get("error"):
        print(f'warning: {payload["error"]}')


if __name__ == "__main__":
    main()
