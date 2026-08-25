"""Dashboard SQLite database — storage for #/dashboard visualization data.

Stores daily-aggregated usage records with indexes for fast queries.
"""

# Model display order — lower number = first.  Models not listed default to 99.
MODEL_ORDER = {
    "deepseek-v4-flash": 1,
    "deepseek-v4-pro": 2,
    "mimo-v2.5": 3,
    "Qwen3.5-397B-A17B": 4,
}


def _sort_models(models: set[str]) -> list[str]:
    return sorted(models, key=lambda model: (
        MODEL_ORDER.get(model, 99), model.lower()))


def _parse_date(date_str: str) -> tuple[int, int]:
    try:
        parts = date_str.split("-")
        if len(parts) >= 2:
            return int(parts[0]), int(parts[1])
        if len(date_str) == 8:
            return int(date_str[:4]), int(date_str[4:6])
    except (AttributeError, ValueError, IndexError):
        return 0, 0
    return 0, 0


def _track_recency(last_month: dict, month_vol: dict,
                   name: str, year: int, month: int, volume: int) -> None:
    ym = year * 100 + month
    previous = last_month.get(name)
    if previous is None or ym > previous:
        last_month[name] = ym
        month_vol[name] = volume
    elif ym == previous:
        month_vol[name] = month_vol.get(name, 0) + volume



__all__ = ["MODEL_ORDER", "_sort_models", "_parse_date", "_track_recency"]
