"""MCP-сервер «Помощник мастера D&D».

Даёт языковой модели доступ к русскоязычному справочнику dnd.su:
поиск и чтение заклинаний, монстров и магических предметов, а также
бросок кубов.

Запуск (stdio):
  python server.py
"""

from __future__ import annotations

import random
import re

from mcp.server.mcpserver import MCPServer

import scraper

mcp = MCPServer(
    name="dnd-master-helper",
    instructions=(
        "Ты помощник мастера настольной игры D&D 5. Используй инструменты, чтобы "
        "находить точную информацию о заклинаниях, монстрах, магических предметах, "
        "классах, расах, предысториях, чертах и правилах на русскоязычном сайте dnd.su. "
        "Всегда ссылайся на источник и проверяй названия по оригиналу в квадратных "
        "скобках, например [Fireball]."
    ),
)

REFERENCE = (
    "Данные получены с https://dnd.su — онлайн-справочника D&D 5. "
    "Права на материалы принадлежат Wizards of the Coast."
)


def _run(fn):
    try:
        return fn()
    except scraper.DndError as exc:
        return f"Ошибка: {exc}"


def _format_result(card: dict, kind: str) -> str:
    text = card["text"] or "Описание на сайте не найдено."
    url = card.get("url") or ""
    source = f"\nИсточник: {url}" if url else ""
    return f"{card['title']} ({kind}){source}\n\n{text}\n\n{REFERENCE}"


def _format_article(article: dict, kind: str) -> str:
    text = article["text"] or "Статья на сайте не найдена."
    url = article.get("url") or ""
    source = f"\nИсточник: {url}" if url else ""
    return f"{article['title']} ({kind}){source}\n\n{text}\n\n{REFERENCE}"


def _format_rules_results(items: list[dict]) -> str:
    if not items:
        return "Ничего не найдено."
    lines = ["Найдено статей:", ""]
    for item in items:
        lines.append(f"- {item['title']} — {item['url']}")
        if item.get("desc"):
            lines.append(f"  {item['desc']}")
    lines.append("")
    lines.append("Используйте get_rules с названием статьи или её URL, чтобы получить текст.")
    return "\n".join(lines)


@mcp.tool()
def search_spells(query: str = "", limit: int = 10, level: int | None = None,
                  school: str = "", ritual: bool | None = None) -> str:
    """Ищет заклинания на dnd.su. query — часть названия (русское или английское). Необязательные фильтры: level (0–9), school (например «воплощение», «очарование»), ritual (True/False). Возвращает список со ссылками."""
    filters: dict = {}
    if level is not None:
        if not isinstance(level, int) or not 0 <= level <= 9:
            return "Ошибка: level должен быть числом от 0 до 9."
        filters["level"] = str(level)
    if school:
        try:
            filters["school"] = str(scraper._map_id(scraper.SPELL_SCHOOLS, school, "школу"))
        except scraper.DndError as exc:
            return f"Ошибка: {exc}"
    if ritual is not None:
        filters["ritual"] = "2" if ritual else "1"
    return _run(lambda: _format_search("spells", query, limit, filters))


@mcp.tool()
def get_spell(name: str) -> str:
    """Возвращает полное описание заклинания с dnd.su: уровень, школа, время накладывания, дистанция, компоненты, длительность, классы и текст эффекта. name — название заклинания (например «огненный шар» или «fireball»)."""
    return _run(lambda: _format_result(scraper.find_card("spells", name), "заклинание"))


@mcp.tool()
def search_monsters(query: str = "", limit: int = 10, cr: str = "",
                    size: str = "", creature_type: str = "",
                    environment: str = "") -> str:
    """Ищет монстров в бестиарии dnd.su. query — часть названия. Необязательные фильтры: cr (например «1/4», «2», «10»), size (крошечный/маленький/средний/большой/огромный/громадный), creature_type (дракон, нежить, зверь…), environment (лес, болото, подземье…). Возвращает список со ссылками."""
    filters: dict = {}
    if cr:
        try:
            filters["danger"] = str(scraper._danger_filter_code(cr))
        except scraper.DndError as exc:
            return f"Ошибка: {exc}"
    if size:
        try:
            filters["size"] = str(scraper._map_id(scraper.MONSTER_SIZES, size, "размер"))
        except scraper.DndError as exc:
            return f"Ошибка: {exc}"
    if creature_type:
        try:
            filters["type"] = str(scraper._map_id(scraper.MONSTER_TYPES, creature_type, "вид монстра"))
        except scraper.DndError as exc:
            return f"Ошибка: {exc}"
    if environment:
        try:
            filters["environment"] = str(scraper._map_id(scraper.MONSTER_ENVIRONMENTS, environment, "местность"))
        except scraper.DndError as exc:
            return f"Ошибка: {exc}"
    return _run(lambda: _format_search("monsters", query, limit, filters))


@mcp.tool()
def get_monster(name: str) -> str:
    """Возвращает полный блок характеристик монстра с dnd.su: размер, вид, КД, хиты, скорость, характеристики, спасброски, действия и описание. name — название монстра (например «взрослый белый дракон»)."""
    return _run(lambda: _format_result(scraper.find_card("monsters", name), "монстр"))


@mcp.tool()
def search_items(query: str = "", limit: int = 10, rarity: str = "",
                 item_type: str = "") -> str:
    """Ищет магические предметы на dnd.su. query — часть названия. Необязательные фильтры: rarity (обычный/необычный/редкий/очень редкий/легендарный/артефакт), item_type (оружие, кольцо, зелье, посох…). Возвращает список со ссылками."""
    filters: dict = {}
    if rarity:
        try:
            filters["quality"] = str(scraper._map_id(scraper.ITEM_RARITIES, rarity, "редкость"))
        except scraper.DndError as exc:
            return f"Ошибка: {exc}"
    if item_type:
        try:
            filters["type"] = str(scraper._map_id(scraper.ITEM_TYPES, item_type, "тип предмета"))
        except scraper.DndError as exc:
            return f"Ошибка: {exc}"
    return _run(lambda: _format_search("items", query, limit, filters))


@mcp.tool()
def get_item(name: str) -> str:
    """Возвращает описание магического предмета с dnd.su: тип, редкость, настройка, свойства и текст. name — название предмета (например «язык пламени»)."""
    return _run(lambda: _format_result(scraper.find_card("items", name), "магический предмет"))


@mcp.tool()
def search_classes(query: str, limit: int = 10) -> str:
    """Ищет классы на dnd.su по названию. query — часть названия. Возвращает список найденных классов со ссылками."""
    return _run(lambda: _format_search("class", query, limit))


@mcp.tool()
def get_class(name: str, section: str = "") -> str:
    """Возвращает статью о классе с dnd.su: описание, таблица уровней, классовые умения и подклассы. name — название класса (например «варвар»). section (необязательно) — конкретный раздел статьи, например «клятва мести» или «ярость»; без него вернётся статья целиком (длинные статьи обрезаются)."""
    return _run(lambda: _format_article(
        scraper.find_article("class", name, target=section or None), "класс"))


@mcp.tool()
def search_races(query: str, limit: int = 10) -> str:
    """Ищет расы на dnd.su по названию. query — часть названия. Возвращает список найденных рас со ссылками."""
    return _run(lambda: _format_search("race", query, limit))


@mcp.tool()
def get_race(name: str, section: str = "") -> str:
    """Возвращает статью о расе с dnd.su: описание, черты, характеристики. name — название расы (например «эльф»). section (необязательно) — конкретный подраздел, например «высший эльф»; без него вернётся статья целиком (длинные статьи обрезаются)."""
    return _run(lambda: _format_article(
        scraper.find_article("race", name, target=section or None), "раса"))


@mcp.tool()
def search_backgrounds(query: str, limit: int = 10) -> str:
    """Ищет предыстории на dnd.su по названию. query — часть названия. Возвращает список найденных предысторий со ссылками."""
    return _run(lambda: _format_search("background", query, limit))


@mcp.tool()
def get_background(name: str, section: str = "") -> str:
    """Возвращает статью о предыстории с dnd.su: описание, умения, владения. name — название предыстории (например «чужеземец»). section (необязательно) — конкретный раздел статьи."""
    return _run(lambda: _format_article(
        scraper.find_article("background", name, target=section or None), "предыстория"))


@mcp.tool()
def search_feats(query: str, limit: int = 10) -> str:
    """Ищет черты на dnd.su по названию. query — часть названия. Возвращает список найденных черт со ссылками."""
    return _run(lambda: _format_search("feat", query, limit))


@mcp.tool()
def get_feat(name: str, section: str = "") -> str:
    """Возвращает статью о черте с dnd.su: описание и эффект. name — название черты (например «стихийный адепт»). section (необязательно) — конкретный раздел статьи."""
    return _run(lambda: _format_article(
        scraper.find_article("feat", name, target=section or None), "черта"))


@mcp.tool()
def list_rules(section: str = "all") -> str:
    """Показывает доступные статьи по правилам на dnd.su. section: «all» (по умолчанию), «mechanics», «inventory» или «newbie». Возвращает названия статей со ссылками."""
    try:
        if not section or section == "all":
            items = scraper.all_rule_articles()
        else:
            items = scraper.list_articles(section)
    except scraper.DndError as exc:
        return f"Ошибка: {exc}"
    if not items:
        return "Статьи не найдены."
    lines = [f"Доступные статьи (раздел: {section or 'all'}):", ""]
    for item in items:
        lines.append(f"- {item['title']} — {item['url']}")
    lines.append("")
    lines.append("Используйте get_rules с названием статьи или её URL, чтобы получить текст.")
    return "\n".join(lines)


@mcp.tool()
def get_rules(query: str, section: str = "") -> str:
    """Возвращает текст статьи по правилам D&D с dnd.su. query — название статьи (например «мультиклассирование» или «основные формулы»), тема из её содержимого (например «алебарда», «спасброски») или полный URL статьи. section (необязательно) — конкретный раздел статьи. Если точного совпадения нет, вернётся список подходящих статей."""
    return _run(lambda: _format_article(
        scraper.get_article(query, target=section or None), "статья"))


@mcp.tool()
def search_rules(query: str, limit: int = 10) -> str:
    """Ищет статьи по правилам на dnd.su, включая поиск по содержимому статей. query — тема (например «алебарда», «урон оружия», «мультиклассирование»). Возвращает список подходящих статей со ссылками."""
    return _run(lambda: _format_rules_results(scraper.search_rules_content(query, limit=limit)))


@mcp.tool()
def plan_encounter(party: str, monsters: str) -> str:
    """Считает сложность боевой сцены по правилам D&D 5 (DMG): партия против монстров. party — уровни персонажей через запятую (например «5,5,4»). monsters — монстры с количеством через запятую (например «гоблин x3, лич x1» или «3x гоблин»). Вернёт суммарный опыт, множитель за число противников, пороговые значения группы и сложность (Легко/Средне/Сложно/Смертельно), а также опыт на персонажа."""
    return _run(lambda: scraper.plan_encounter(party, monsters))


@mcp.tool()
def generate_loot(category: str, tier: str = "") -> str:
    """Генерирует случайную добычу на dnd.su. category — тип: coins (монеты), gems (камни), arts (искусство), trinkets (безделушки), treasury (сокровищница), scrolls (свитки), items (магические предметы). tier — уточнение: для coins/treasury опасность «0-4», «5-10», «11-16», «17+»; для gems «10»/«50»/«100»/«500»/«1000»/«5000» зм; для arts «25»/«250»/«750»/«2500»/«7500» зм; для scrolls уровень 0–9 или школа; для items редкость (необычный, редкий…) или тип (оружие, кольцо…). Пустой tier — случайный выбор."""
    return _run(lambda: scraper.generate_loot(category, tier))


@mcp.tool()
def roll_dice(notation: str) -> str:
    """Бросает кубики D&D и возвращает результат. Формат: «2d6», «1d20+3», «3d8+1d4-2», «4к6». Поддерживает несколько кубиков и модификаторы через + и -."""
    return _run(lambda: _roll(notation))


def _format_search(section: str, query: str, limit: int, filters: dict | None = None) -> str:
    results = scraper.search(section, query, limit=limit, filters=filters)
    if not results:
        return f"По запросу «{query}» ничего не найдено."
    kinds = {
        "spells": "заклинания",
        "monsters": "монстры",
        "items": "предметы",
        "class": "классы",
        "race": "расы",
        "background": "предыстории",
        "feat": "черты",
    }
    kind = kinds.get(section, section)
    lines = [f"Найдено по запросу «{query}» (раздел: {kind}):", ""]
    for item in results:
        lines.append(f"- {item['title']} — {item['url']}")
    lines.append("")
    lines.append("Используйте get_spell / get_monster / get_item / get_class / get_race / get_background / get_feat с названием, чтобы получить полное описание.")
    return "\n".join(lines)


_DICE_PATTERN = re.compile(r"(\d*d\d+|\d+)")


def _roll(notation: str) -> str:
    clean = re.sub(r"\s+", "", notation).lower().replace("к", "d")
    if not clean:
        return "Ошибка: укажите нотацию броска. Формат: «2d6», «1d20+3», «3d8+1d4-2»."

    # Валидация: только кубики (NdM) и числа, разделённые +/-
    if not re.fullmatch(r"(\d*d\d+|\d+)([+-](\d*d\d+|\d+))*", clean):
        return f"Ошибка: не понял нотацию «{notation}». Формат: «2d6», «1d20+3», «3d8+1d4-2»."

    total = 0
    parts: list[str] = []
    for part in _DICE_PATTERN.findall(clean):
        sign = 1
        raw = part
        if raw[0] in "+-":
            sign = -1 if raw[0] == "-" else 1
            raw = raw[1:]
        if "d" in raw:
            count, sides = raw.split("d")
            count = int(count or "1")
            sides = int(sides)
            if count < 1 or sides < 1:
                return "Ошибка: количество кубиков и число граней должны быть больше нуля."
            values = [random.randint(1, sides) for _ in range(count)]
            total += sign * sum(values)
            rolls = " ".join(str(v) for v in values)
            parts.append(f"{sign * count}к{sides} ({rolls})")
        else:
            value = int(raw)
            total += sign * value
            parts.append(f"{sign * value}")

    return f"{notation} → {', '.join(parts)} = {total}"


def main() -> None:
    mcp.run(transport="stdio")  # type: ignore[arg-type]


if __name__ == "__main__":
    main()