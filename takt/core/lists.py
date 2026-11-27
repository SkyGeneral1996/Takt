from __future__ import annotations

from .models import new_list_item

# name -> позиции (text, qty, unit, group)
TEMPLATES: dict[str, list[tuple]] = {
    "Покупки": [
        ("Молоко", 1, "л", "Молочное"),
        ("Хлеб", 1, "шт", "Хлебобулочные"),
        ("Яйца", 10, "шт", "Молочное"),
        ("Сыр", 0.3, "кг", "Молочное"),
        ("Яблоки", 1, "кг", "Фрукты"),
        ("Курица", 1, "кг", "Мясо"),
    ],
    "Сборы в поездку": [
        ("Паспорт", 1, "", "Документы"),
        ("Билеты", 1, "", "Документы"),
        ("Зарядка для телефона", 1, "", "Техника"),
        ("Наушники", 1, "", "Техника"),
        ("Одежда по погоде", 1, "", "Вещи"),
        ("Аптечка", 1, "", "Вещи"),
    ],
    "Чек-лист": [
        ("Шаг 1", 1, "", ""),
        ("Шаг 2", 1, "", ""),
        ("Шаг 3", 1, "", ""),
    ],
}


def template_items(name: str) -> list[dict]:
    items = []
    for pos, (text, qty, unit, group) in enumerate(TEMPLATES.get(name, [])):
        items.append(new_list_item(text=text, qty=qty, unit=unit,
                                   group=group, position=pos))
    return items


def sorted_items(items: list[dict]) -> list[dict]:
    """Неотмеченные выше отмеченных; внутри — по группе и позиции."""
    return sorted(items, key=lambda i: (bool(i.get("done")),
                                        i.get("group") or "",
                                        i.get("position", 0)))


def item_sum(item: dict) -> float:
    qty = 1 if item.get("qty") is None else item["qty"]
    price = 0 if item.get("price") is None else item["price"]
    return qty * price