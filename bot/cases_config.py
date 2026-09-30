"""
КЕЙСЫ И NFT-ПОДАРКИ — единственное место, где задаются шансы, цены и призы.

Хотите поменять шанс или цену — правьте только этот файл.
Проверить окупаемость (RTP) каждого кейса:   python bot/cases_config.py

Формат приза:  ("stars", количество, шанс_в_процентах)   или   ("gift", "ключ_подарка", шанс_в_процентах)
Сумма шансов в кейсе должна быть 100 (иначе при запуске в логе появится предупреждение).
"""
import logging
import random

logger = logging.getLogger(__name__)
_rng = random.SystemRandom()

# ---------------------------------------------------------------
# NFT-подарки. price — за сколько звёзд бот выкупает подарок у игрока.
# image — необязательно: URL/путь к картинке подарка (например "/static/gifts/plush_pepe.png").
# Если картинки нет, мини-апп рисует фирменную иконку-медальон по полю art/tone.
# ---------------------------------------------------------------
GIFTS = {
    "plush_pepe":     {"name": "Plush Pepe",     "price": 100_000, "art": "pepe",   "tone": "#4c9a5f", "image": ""},
    "snoop_dogg":     {"name": "Snoop Dogg",     "price": 25_000,  "art": "dog",    "tone": "#c98a3d", "image": ""},
    "snoop_cigare":   {"name": "Snoop Cigare",   "price": 25_000,  "art": "cigar",  "tone": "#a4572f", "image": ""},
    "durovs_cap":     {"name": "Durov's Cap",    "price": 10_000,  "art": "cap",    "tone": "#3f86c9", "image": ""},
    "vintage_cigare": {"name": "Vintage Cigare", "price": 10_000,  "art": "cigar",  "tone": "#8a6a3a", "image": ""},
    "mood_pack":      {"name": "Mood Pack",      "price": 4_000,   "art": "pack",   "tone": "#b45a8c", "image": ""},
}

# ---------------------------------------------------------------
# Кейсы
# ---------------------------------------------------------------
CASES = [
    {
        "id": "durov",
        "name": "Кейс Дурова",
        "tagline": "Звёзды и два NFT-подарка.",
        "price": 1000,
        "tone": "#3f86c9",
        "drops": [
            ("stars", 100, 50),
            ("stars", 500, 25),
            ("stars", 1000, 15),
            ("gift", "mood_pack", 7),
            ("gift", "durovs_cap", 3),
        ],
    },
    {
        "id": "rich",
        "name": "Богач",
        "tagline": "Дорогой кейс — дорогие подарки.",
        "price": 2500,
        "tone": "#b45a8c",
        "drops": [
            ("stars", 500, 50),
            ("stars", 1000, 30),
            ("gift", "vintage_cigare", 4),
            ("gift", "durovs_cap", 10),
            ("gift", "snoop_dogg", 3),
            ("gift", "snoop_cigare", 3),
        ],
    },
    {
        "id": "plush_pepe",
        "name": "Plush Pepe",
        "tagline": "Один шанс на сто — Plush Pepe.",
        "price": 2000,
        "tone": "#4c9a5f",
        "drops": [
            ("stars", 100, 95),
            ("stars", 1000, 4),
            ("gift", "plush_pepe", 1),
        ],
    },
]

CASES_BY_ID = {c["id"]: c for c in CASES}


def drop_value(drop) -> int:
    """Стоимость приза в звёздах (подарок оценивается по цене выкупа)."""
    kind, value, _ = drop
    return int(value) if kind == "stars" else int(GIFTS[value]["price"])


def case_rtp(case) -> float:
    """Средний возврат игроку в % от цены кейса (подарки по цене выкупа)."""
    total = sum(w for _, _, w in case["drops"]) or 1
    ev = sum(drop_value(d) * d[2] for d in case["drops"]) / total
    return ev / case["price"] * 100


def roll_case(case):
    """Выбрать приз по шансам. Возвращает (kind, value)."""
    total = sum(w for _, _, w in case["drops"])
    point = _rng.uniform(0, total)
    acc = 0.0
    for kind, value, weight in case["drops"]:
        acc += weight
        if point < acc:
            return kind, value
    kind, value, _ = case["drops"][-1]
    return kind, value


def public_catalog() -> dict:
    """Что отдаём мини-аппу: кейсы, призы, шансы, подарки."""
    cases = []
    for c in CASES:
        cases.append({
            "id": c["id"], "name": c["name"], "tagline": c.get("tagline", ""),
            "price": c["price"], "tone": c.get("tone", "#d4a24c"),
            "drops": [
                {"kind": k, "value": v, "chance": w,
                 "worth": drop_value((k, v, w))}
                for k, v, w in c["drops"]
            ],
        })
    gifts = {k: {"key": k, **g} for k, g in GIFTS.items()}
    return {"cases": cases, "gifts": gifts}


for _c in CASES:
    _sum = sum(w for _, _, w in _c["drops"])
    if abs(_sum - 100) > 0.001:
        logger.warning("Кейс %s: сумма шансов %s%% (должно быть 100%%)", _c["id"], _sum)
    for _k, _v, _w in _c["drops"]:
        if _k == "gift" and _v not in GIFTS:
            raise ValueError(f"Кейс {_c['id']}: неизвестный подарок {_v}")


if __name__ == "__main__":
    print(f"{'Кейс':<18}{'Цена':>7}{'Ср. выигрыш':>14}{'RTP':>9}")
    for c in CASES:
        total = sum(w for _, _, w in c["drops"]) or 1
        ev = sum(drop_value(d) * d[2] for d in c["drops"]) / total
        print(f"{c['name']:<18}{c['price']:>7}{ev:>14.0f}{case_rtp(c):>8.1f}%")
    print("\nRTP > 100% — игрок в плюсе, казино в среднем теряет звёзды.")
