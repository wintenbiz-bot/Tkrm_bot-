"""Общая загрузка монохромных пар.

Источник данных — monochrome_matches.csv.
В CSV берутся только строки с human_label = yes (это подтверждённые
монохромные пары). Старый JSON-формат оставлен в качестве совместимости,
если где-то всё ещё указан явный путь к .json-файлу.
"""

import csv
import json
import logging
import os

logger = logging.getLogger(__name__)

DEFAULT_MONOCHROME_FILE = "monochrome_matches.csv"

# Считаем «положительной» любую из этих записей human_label.
POSITIVE_LABELS = {"yes", "y", "true", "1"}


def resolve_matches_path(file_name: str = DEFAULT_MONOCHROME_FILE, base_dir: str | None = None) -> str:
    """Возвращает абсолютный путь к файлу совпадений."""
    if os.path.isabs(file_name):
        return file_name
    base_dir = base_dir or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, file_name)


def load_monochrome_pairs(
    file_name: str = DEFAULT_MONOCHROME_FILE,
    base_dir: str | None = None,
) -> list[tuple[str, str, str]]:
    """
    Загружает пары (collection, model, backdrop) в нижнем регистре.

    CSV: колонки pair_key (`Collection|Model|Backdrop`) и human_label.
    JSON (legacy): список объектов с collection / model_id / backdrop_id.
    """
    path = resolve_matches_path(file_name, base_dir)

    if not os.path.exists(path):
        logger.warning(f"⚠️ monochrome: файл {path} не найден")
        return []

    pairs: set[tuple[str, str, str]] = set()
    ext = os.path.splitext(path)[1].lower()

    if ext == ".csv":
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                label = str(row.get("human_label") or "").strip().lower()
                if label not in POSITIVE_LABELS:
                    continue

                parts = str(row.get("pair_key") or "").split("|")
                if len(parts) != 3:
                    continue

                collection, model, backdrop = (
                    part.strip().lower() for part in parts
                )
                if not (collection and model and backdrop):
                    continue

                pairs.add((collection, model, backdrop))

    elif ext == ".json":
        # Legacy-поддержка: старый файл больше не используется по умолчанию.
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, list):
            raise ValueError("ожидался список пар")

        for item in data:
            if not isinstance(item, dict):
                continue

            collection = str(item.get("collection") or "").strip().lower()
            model = str(item.get("model_id") or "").strip().lower()
            backdrop = str(item.get("backdrop_id") or "").strip().lower()

            if collection and model and backdrop:
                pairs.add((collection, model, backdrop))

    else:
        raise ValueError(f"неизвестный формат файла: {path}")

    logger.info(f"✅ monochrome: загружено {len(pairs)} пар из {path}")
    return sorted(pairs)
