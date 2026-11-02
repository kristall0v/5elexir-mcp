"""Модуль работы с данными dnd.su для MCP-сервера «Помощник мастера D&D».

Используются открытые страницы сайта:
  - поиск:   https://dnd.su/<раздел>/?search=<запрос>
  - детали:  https://dnd.su<путь карточки>

Заклинания, бестиарий и магические предметы используют одинаковую вёрстку,
поэтому разбор универсальный.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

BASE_URL = "https://dnd.su"

# Раздел на сайте → путь в URL.
SECTIONS = {
    "spells": "spells",
    "monsters": "bestiary",
    "items": "items",
}

# «Статейные» справочные разделы: та же вёрстка, но карточки без data-cardlink.
ARTICLE_SECTIONS = {
    "class": "class",
    "race": "race",
    "background": "backgrounds",
    "feat": "feats",
}

# Разделы статей для инструмента «правила».
RULE_SECTIONS = {
    "inventory": "articles/inventory",
    "mechanics": "articles/mechanics",
    "newbie": "articles/newbie",
}

# Максимальная длина текста статьи (классы бывают очень длинными).
MAX_ARTICLE_LEN = 16000

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) dnd-master-helper/0.1",
}
TIMEOUT = 20

# Локальный кэш страниц (SQLite): меньше запросов к сайту и быстрее ответы.
_CACHE_DB = Path(__file__).resolve().parent / "dnd_cache.db"
_CACHE_TTL = 24 * 3600
_cache_lock = threading.Lock()


def _cache_get(url: str) -> str | None:
    try:
        with _cache_lock:
            conn = sqlite3.connect(_CACHE_DB, timeout=10)
            row = conn.execute(
                "SELECT html, fetched_at FROM page_cache WHERE url=?", (url,)
            ).fetchone()
            conn.close()
        if row and time.time() - row[1] < _CACHE_TTL:
            return row[0]
    except sqlite3.Error:
        pass
    return None


def _cache_set(url: str, html: str) -> None:
    try:
        with _cache_lock:
            conn = sqlite3.connect(_CACHE_DB, timeout=10)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS page_cache "
                "(url TEXT PRIMARY KEY, html TEXT, fetched_at REAL)"
            )
            conn.execute(
                "INSERT OR REPLACE INTO page_cache (url, html, fetched_at) VALUES (?,?,?)",
                (url, html, time.time()),
            )
            conn.commit()
            conn.close()
    except sqlite3.Error:
        pass

_WS_RE = re.compile(r"\s+")


class DndError(Exception):
    """Ошибка загрузки или разбора страницы dnd.su."""


def _normalize(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def _fetch(url: str, params: dict | None = None) -> BeautifulSoup:
    """Загружает страницу (с кэшем). Случайные эндпоинты /request/ не кэшируются."""
    full_url = url
    if params:
        full_url = url + ("&" if "?" in url else "?") + urlencode(params)

    cacheable = "/request/" not in full_url
    if cacheable:
        cached = _cache_get(full_url)
        if cached is not None:
            return BeautifulSoup(cached, "html.parser")

    try:
        resp = requests.get(full_url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise DndError(f"Не удалось загрузить {url}: {exc}") from exc

    if cacheable:
        _cache_set(full_url, resp.text)
    return BeautifulSoup(resp.text, "html.parser")


def _is_noise(tag: Tag) -> bool:
    """Вспомогательные элементы сайта, которые надо выкинуть из текста."""
    if tag.name == "sup":
        return True
    if tag.name == "span" and "no-decor" in tag.get("class", []):
        return True
    if tag.name == "svg":
        return True
    return False


def _inline_text(node) -> str:
    """Текст внутри узла: без sup/подсказок-заглушек, но со словами из span-tooltip."""
    if isinstance(node, NavigableString):
        return str(node)
    if isinstance(node, Tag):
        if _is_noise(node):
            return ""
        return "".join(_inline_text(child) for child in node.children)
    return ""


def render_blocks(node) -> list[str]:
    """Превращает фрагмент HTML (p, h3, ul, div) в список текстовых строк."""
    blocks: list[str] = []
    for child in node.children:
        if isinstance(child, NavigableString):
            text = _normalize(str(child))
            if text:
                blocks.append(text)
            continue
        if not isinstance(child, Tag):
            continue
        if _is_noise(child):
            continue
        name = child.name
        if name in ("p", "blockquote", "table"):
            text = _normalize(_inline_text(child))
            if text:
                blocks.append(text)
        elif name in ("h1", "h2", "h3", "h4", "h5"):
            text = _normalize(_inline_text(child))
            if text:
                blocks.append(f"## {text}")
        elif name in ("ul", "ol"):
            for i, li in enumerate(child.find_all("li", recursive=False), 1):
                text = _normalize(_inline_text(li))
                if text:
                    blocks.append(f"{i}. {text}" if name == "ol" else f"- {text}")
        elif name == "div":
            blocks.extend(render_blocks(child))
        elif name == "br":
            continue
        else:
            text = _normalize(_inline_text(child))
            if text:
                blocks.append(text)
    return blocks


def _render_li(li: Tag) -> list[str]:
    classes = li.get("class", [])

    if li.find("img"):
        return []

    if "abilities" in classes:
        stats = []
        for stat in li.select("div.stat"):
            label = stat.get("title") or ""
            divs = stat.find_all("div")
            value = (
                _normalize(_inline_text(divs[1]))
                if len(divs) >= 2
                else _normalize(_inline_text(stat))
            )
            if value:
                stats.append(f"{label} {value}")
        return ["Характеристики: " + ", ".join(stats)] if stats else []

    if "subsection" in classes:
        blocks: list[str] = []
        heading = li.select_one("h2.subsection-title, h3.subsection-title, h4.subsection-title")
        desc = li.select_one("div[itemprop='description']")
        if heading is not None:
            text = _normalize(_inline_text(heading))
            if text:
                blocks.append(f"## {text}")
            for sibling in heading.next_siblings:
                if isinstance(sibling, Tag) and sibling.name == "div":
                    blocks.extend(render_blocks(sibling))
        elif desc is not None:
            blocks.extend(render_blocks(desc))
        else:
            blocks.extend(render_blocks(li))
        return blocks

    text = _normalize(_inline_text(li))
    strong = li.find("strong")
    if strong is not None:
        label = _normalize(_inline_text(strong)).rstrip(":")
        value = text
        strong_text = _normalize(_inline_text(strong))
        if strong_text and text.startswith(strong_text):
            value = text[len(strong_text):].strip()
        if value:
            return [f"{label}: {value}"]
        return [f"{label}:"]
    if text:
        return [text]
    return []


def parse_card(soup: BeautifulSoup) -> dict:
    """Извлекает из страницы одну карточку (заклинание/монстра/предмет)."""
    card = soup.select_one("div.paper-1.card[data-cardlink]")
    if card is None:
        raise DndError("На странице не найден контент (возможно, изменилась вёрстка сайта)")

    title = ""
    title_node = card.select_one("h2.card-title a.item-link")
    if title_node is None:
        title_node = card.select_one("h2.card-title span[data-copy]")
    if title_node is not None:
        title = _normalize(_inline_text(title_node))

    lines: list[str] = []
    body = card.select_one("ul.params.card__article-body")
    if body is not None:
        for li in body.find_all("li", recursive=False):
            lines.extend(_render_li(li))

    url = card.get("data-cardlink") or ""
    return {
        "title": title,
        "url": f"{BASE_URL}{url}" if url.startswith("/") else url,
        "text": "\n".join(lines),
    }


def search(
    section: str,
    query: str = "",
    limit: int = 10,
    filters: dict | None = None,
) -> list[dict]:
    """Ищет карточки в разделе.

    filters — уже готовые GET-параметры сайта (например {"level": "3", "school": "1"}).
    Фильтры сайта применяются только при наличии параметра search, поэтому при
    фильтрах search передаётся всегда (даже пустым).
    """
    path = SECTIONS.get(section) or ARTICLE_SECTIONS.get(section)
    if path is None:
        raise DndError(
            f"Неизвестный раздел: {section}. Допустимо: "
            f"{', '.join(SECTIONS) + ', ' + ', '.join(ARTICLE_SECTIONS)}"
        )

    if filters:
        params = {"search": query}
        params.update(filters)
    elif query:
        params = {"search": query}
    else:
        params = None

    soup = _fetch(f"{BASE_URL}/{path}/", params=params)
    results: list[dict] = []
    for card in soup.select(
        "div.paper-1.card[data-cardlink], div.paper-1.card[data-typo-path]"
    ):
        link = card.select_one("h2.card-title a.item-link")
        if link is None:
            continue
        title = _normalize(_inline_text(link))
        href = (
            card.get("data-cardlink")
            or card.get("data-typo-path")
            or link.get("href")
            or ""
        )
        results.append({
            "title": title,
            "url": f"{BASE_URL}{href}" if href.startswith("/") else href,
        })
        if len(results) >= limit:
            break
    return results


def get_card(url_or_path: str) -> dict:
    """Загружает страницу по URL или пути и возвращает данные карточки."""
    url = url_or_path if url_or_path.startswith("http") else f"{BASE_URL}{url_or_path}"
    soup = _fetch(url)
    return parse_card(soup)


def _norm_name(name: str) -> str:
    return re.sub(r"\s*\[.*?\]", "", name).strip().lower()


def _name_forms(title: str) -> list[str]:
    """Все нормализованные формы названия: русское + английское из скобок."""
    forms = [_norm_name(title)]
    match = re.search(r"\[(.*?)\]", title)
    if match:
        forms.append(_norm_name(match.group(1)))
    return [f for f in forms if f]


def find_card(section: str, name: str) -> dict:
    """Ищет карточку по названию или URL.

    Сначала точное совпадение (по русскому или английскому названию), затем
    частичное, иначе первый результат.
    """
    name = name.strip()
    if not name:
        raise DndError("Укажите название или URL заклинания/монстра/предмета.")
    if name.startswith(("http", "/")):
        return get_card(name)

    results = search(section, name, limit=20)
    if not results:
        raise DndError(f"По запросу «{name}» в разделе «{section}» ничего не найдено")

    wanted = _norm_name(name)
    exact = None
    partial = None
    for item in results:
        forms = _name_forms(item["title"])
        if wanted in forms:
            exact = item
            break
        if partial is None and any(wanted in f for f in forms):
            partial = item
    chosen = exact or partial or results[0]
    return get_card(chosen["url"])


_INLINE_TAGS = {
    "span", "strong", "em", "b", "i", "a", "u", "s", "small", "code", "sub",
}
_BLOCK_TAGS = {"div", "p", "blockquote", "section", "article", "table", "ul", "ol"}


def _walk_article(node, events: list) -> None:
    """Рекурсивно превращает HTML-статью в события:
    ("h", уровень, заголовок) для заголовков, ("p", текст) для абзацев.
    """
    if isinstance(node, NavigableString):
        text = _normalize(str(node))
        if text:
            events.append(("p", text))
        return
    if not isinstance(node, Tag):
        return
    if _is_noise(node):
        return

    name = node.name
    if name == "br":
        return
    if name in ("h1", "h2", "h3", "h4", "h5"):
        text = _normalize(_inline_text(node))
        if text:
            events.append(("h", int(name[1]), text))
        return
    if name == "li":
        text = _normalize(_inline_text(node))
        if text:
            events.append(("p", f"- {text}"))
        return
    if name == "table":
        for row in node.find_all("tr"):
            cells = [_normalize(_inline_text(cell)) for cell in row.find_all(["td", "th"])]
            if cells:
                events.append(("p", " | ".join(cells)))
        return
    if name in ("ul", "ol"):
        classes = node.get("class", [])
        if "new-article-menu" in classes or "article-menu" in classes:
            return
        for li in node.find_all("li", recursive=False):
            _walk_article(li, events)
        return
    if name in ("p", "blockquote", "div", "section", "article"):
        if "content-switch" in node.get("class", []):
            return
        buf: list[str] = []

        def flush():
            text = _normalize("".join(buf))
            buf.clear()
            if text:
                events.append(("p", text))

        for child in node.children:
            if isinstance(child, Tag) and (
                "new-article-menu" in child.get("class", [])
                or "article-menu" in child.get("class", [])
                or "content-switch" in child.get("class", [])
            ):
                continue
            if isinstance(child, Tag) and (
                child.name in _BLOCK_TAGS or child.name in ("h1", "h2", "h3", "h4", "h5")
            ):
                flush()
                _walk_article(child, events)
            else:
                buf.append(_inline_text(child) if isinstance(child, Tag) else str(child))
        flush()
        return

    text = _normalize(_inline_text(node))
    if text:
        events.append(("p", text))


def _build_sections(events: list) -> list[dict]:
    """События → список секций: {'level', 'text', 'lines'}."""
    sections: list[dict] = []
    current: dict | None = None
    for event in events:
        if event[0] == "h":
            current = {"level": event[1], "text": event[2], "lines": []}
            sections.append(current)
        else:
            if current is None:
                current = {"level": 0, "text": "", "lines": []}
                sections.append(current)
            current["lines"].append(event[1])
    return sections


def _article_meta(card: Tag) -> tuple[str, str, list[str]]:
    """Заголовок, URL и строки метаданных (источник и т.п.) статьи."""
    title = ""
    title_node = card.select_one("h2.card-title a.item-link")
    if title_node is None:
        title_node = card.select_one("h2.card-title span[data-copy]")
    if title_node is not None:
        title = _normalize(_inline_text(title_node))

    url = card.get("data-cardlink") or card.get("data-typo-path") or ""
    url = f"{BASE_URL}{url}" if url.startswith("/") else url

    meta: list[str] = []
    body = card.select_one("div.card__body")
    if body is not None:
        for ul in body.find_all("ul", recursive=False):
            if "params" not in (ul.get("class") or []):
                continue
            for li in ul.find_all("li", recursive=False):
                text = _normalize(_inline_text(li))
                if text and "читать далее" not in text.lower():
                    meta.append(text)
    return title, url, meta


def _sections_to_text(sections: list[dict]) -> str:
    lines: list[str] = []
    for section in sections:
        if section["text"]:
            lines.append(f"## {section['text']}")
        lines.extend(section["lines"])
    return "\n".join(lines)


_SECTION_WRAPPERS = {"hide-wrapper", "addition-wrapper", "subsection"}


def _section_container(desc: Tag, heading: Tag) -> Tag:
    """Ближайший предок-обёртка секции (подкласса) или сам desc."""
    node = heading.parent
    while node is not None and node is not desc:
        classes = node.get("class") or []
        if any(c in _SECTION_WRAPPERS for c in classes):
            return node
        node = node.parent
    return desc


def _find_heading(desc: Tag, wanted: str) -> Tag | None:
    for heading in desc.find_all(["h2", "h3", "h4", "h5"]):
        text = _norm_name(_inline_text(heading))
        if text and (wanted in text or text in wanted):
            return heading
    return None


def _events_after_heading(container: Tag, heading: Tag) -> list:
    """События контейнера, идущие ПОСЛЕ события заголовка heading."""
    events: list = []
    _walk_article(container, events)
    heading_text = _norm_name(_normalize(_inline_text(heading)))
    for i, event in enumerate(events):
        if event[0] == "h" and _norm_name(event[2]) == heading_text:
            return events[i + 1:]
    return events


def _render_from(container: Tag, heading: Tag) -> str:
    """Текст от заголовка до конца контейнера (полнота важнее точности границ)."""
    return _sections_to_text(_build_sections(_events_after_heading(container, heading)))


def _render_top_section(desc: Tag, heading: Tag) -> str:
    """Текст от заголовка до следующего заголовка того же/высшего уровня."""
    level = int(heading.name[1])
    out = []
    for event in _events_after_heading(desc, heading):
        if event[0] == "h" and event[1] <= level:
            break
        out.append(event)
    return _sections_to_text(_build_sections(out))


def parse_article(
    soup: BeautifulSoup,
    max_len: int = MAX_ARTICLE_LEN,
    target: str | None = None,
) -> dict:
    """Извлекает статью. Если задан target — только раздел с подходящим заголовком."""
    card = soup.select_one(
        "div.paper-1.card[data-cardlink], div.paper-1.card[data-typo-path]"
    )
    if card is None:
        raise DndError("На странице не найден контент (возможно, изменилась вёрстка сайта)")

    title, url, meta = _article_meta(card)

    events: list = []
    desc = card.select_one("div.desc.card__article-body")
    if desc is not None:
        _walk_article(desc, events)
    sections = _build_sections(events)

    if target:
        wanted = _norm_name(target)
        desc = card.select_one("div.desc.card__article-body")
        heading = _find_heading(desc, wanted) if desc is not None else None
        if heading is None:
            available = []
            if desc is not None:
                for h in desc.find_all(["h2", "h3", "h4", "h5"]):
                    t = _normalize(_inline_text(h))
                    if t:
                        available.append(t)
            hint = "\n".join(f"- {h}" for h in available[:40])
            raise DndError(f"Раздел «{target}» в статье не найден. Доступные разделы:\n{hint}")

        container = _section_container(desc, heading)
        text = _render_from(container, heading) if container is not desc else _render_top_section(desc, heading)

        return {
            "title": title,
            "url": url,
            "text": f"{_normalize(_inline_text(heading))}\n\n{text}".strip(),
            "truncated": False,
        }

    text = "\n".join([*meta, "", _sections_to_text(sections)]).strip("\n")
    truncated = len(text) > max_len
    if truncated:
        text = text[:max_len].rstrip() + (
            "\n\n[... текст обрезан — статья длинная. Запросите нужный раздел через "
            "параметр section, например «клятва мести».]"
        )
    return {"title": title, "url": url, "text": text, "truncated": truncated}


def find_article(
    section: str,
    name: str,
    max_len: int = MAX_ARTICLE_LEN,
    target: str | None = None,
) -> dict:
    """Ищет статью в справочном разделе по названию (или URL) и возвращает её текст (или её раздел)."""
    name = name.strip()
    if not name:
        raise DndError("Укажите название или URL статьи.")
    if name.startswith(("http", "/")):
        url = name if name.startswith("http") else f"{BASE_URL}{name}"
        return parse_article(_fetch(url), max_len=max_len, target=target)

    results = search(section, name, limit=20)
    if not results:
        raise DndError(f"По запросу «{name}» в разделе «{section}» ничего не найдено")

    wanted = _norm_name(name)
    exact = None
    partial = None
    for item in results:
        forms = _name_forms(item["title"])
        if wanted in forms:
            exact = item
            break
        if partial is None and any(wanted in f for f in forms):
            partial = item
    chosen = exact or partial or results[0]
    return parse_article(_fetch(chosen["url"]), max_len=max_len, target=target)


def _section_cards(section: str, search_query: str = "") -> list[dict]:
    """Карточки статей раздела правил; при search_query — результат поиска сайта (?search=)."""
    path = RULE_SECTIONS.get(section)
    if path is None:
        raise DndError(f"Неизвестный раздел правил: {section}")
    url = f"{BASE_URL}/{path}/"
    params = {"search": search_query} if search_query else None
    soup = _fetch(url, params=params)
    items: list[dict] = []
    for card in soup.select("div.paper-1.card[data-typo-path]"):
        link = card.select_one("h2.card-title a.item-link")
        if link is None:
            continue
        title = _normalize(_inline_text(link))
        href = card.get("data-typo-path") or link.get("href") or ""
        desc = ""
        desc_node = card.select_one("div.desc.card__article-body")
        if desc_node is not None:
            desc = _normalize(_inline_text(desc_node))[:300]
        items.append({
            "title": title,
            "url": f"{BASE_URL}{href}" if href.startswith("/") else href,
            "section": section,
            "desc": desc,
        })
    return items


def list_articles(section: str) -> list[dict]:
    """Список статей в разделе правил (каталог со списка-страницы)."""
    return _section_cards(section)


def all_rule_articles() -> list[dict]:
    """Все статьи из разделов правил (инвентарь, механики, новичку)."""
    items: list[dict] = []
    for section in RULE_SECTIONS:
        items.extend(list_articles(section))
    return items


def search_rules(query: str, limit: int = 10) -> list[dict]:
    """Ищет статью правил по названию или началу описания (без поиска по содержимому)."""
    q = query.strip().lower()
    results = []
    for item in all_rule_articles():
        if q in _norm_name(item["title"]) or q in item["desc"].lower():
            results.append(item)
        if len(results) >= limit:
            break
    return results


def search_rules_content(query: str, limit: int = 5) -> list[dict]:
    """Ищет статьи правил по содержимому.

    Использует поиск сайта (?search= на страницах разделов) и проверяет, что
    запрос реально встречается в тексте статьи. Это отсекает лишние результаты
    от разделов, где поиск ничего не нашёл и вернул весь список.
    """
    q = query.strip().lower()
    results: list[dict] = []
    seen: set[str] = set()
    for section in RULE_SECTIONS:
        cards = _section_cards(section, query)
        max_probe = min(len(cards), 3) if len(cards) > 15 else len(cards)
        for card in cards[:max_probe]:
            if card["url"] in seen:
                continue
            seen.add(card["url"])
            try:
                article = parse_article(_fetch(card["url"]))
            except DndError:
                continue
            if q in article["text"].lower():
                results.append(card)
                if len(results) >= limit:
                    return results
    return results


def get_article(
    url_or_name: str,
    max_len: int = MAX_ARTICLE_LEN,
    target: str | None = None,
) -> dict:
    """Возвращает статью по URL или по названию.

    По названию сначала ищется точное совпадение в каталоге, затем — частичное;
    если ничего не найдено, выполняется поиск по содержимому статей через сайт.
    """
    if url_or_name.startswith(("http", "/")):
        url = url_or_name if url_or_name.startswith("http") else f"{BASE_URL}{url_or_name}"
        return parse_article(_fetch(url), max_len=max_len, target=target)

    if not url_or_name.strip():
        raise DndError("Укажите запрос — название статьи или её URL.")

    wanted = _norm_name(url_or_name)
    exact = None
    candidates = []
    for item in all_rule_articles():
        if _norm_name(item["title"]) == wanted:
            exact = item
            break
        if wanted in _norm_name(item["title"]):
            candidates.append(item)
    if exact is None and len(candidates) == 1:
        exact = candidates[0]

    if exact is None:
        matches = search_rules_content(url_or_name, limit=10)
        if len(matches) == 1:
            exact = matches[0]
        elif matches:
            names = "".join(f"\n- {m['title']} — {m['url']}" for m in matches)
            raise DndError(
                f"По запросу «{url_or_name}» найдено несколько статей:{names}\n"
                "Уточните запрос, назовите статью точнее или передайте её URL."
            )
        else:
            names = "".join(f"\n- {c['title']}" for c in candidates[:10])
            hint = names if names else "\n- (ничего не найдено — попробуйте list_rules)"
            raise DndError(f"По запросу «{url_or_name}» нет подходящей статьи.{hint}")

    return parse_article(_fetch(exact["url"]), max_len=max_len, target=target)


# ─────────────────────────────────────────────────────────────────────────────
# Фильтры поиска (человеческие значения → id фильтров сайта)
# ─────────────────────────────────────────────────────────────────────────────

SPELL_SCHOOLS = {
    "воплощение": 1, "вызов": 2, "иллюзия": 3, "некромантия": 4,
    "ограждение": 5, "очарование": 6, "преобразование": 7, "прорицание": 8,
}
MONSTER_SIZES = {
    "крошечный": 1, "маленький": 2, "средний": 3, "большой": 4,
    "огромный": 5, "громадный": 6,
}
MONSTER_TYPES = {
    "гуманоид": 19, "нежить": 20, "дракон": 21, "зверь": 22, "монстр": 23,
    "аберрация": 24, "великан": 25, "растение": 26, "слизь": 27,
    "небожитель": 28, "конструкт": 29, "элементаль": 30, "исчадие": 31, "фея": 32,
}
MONSTER_ENVIRONMENTS = {
    "арктика": 1, "болото": 2, "город": 3, "горы": 4, "лес": 5, "луг": 6,
    "побережье": 7, "под водой": 8, "подземье": 9, "пустыня": 10, "холмы": 11,
}
ITEM_RARITIES = {
    "обычный": 6, "необычный": 1, "редкий": 2, "очень редкий": 3,
    "легендарный": 4, "артефакт": 7, "нет редкости": 8,
}
ITEM_TYPES = {
    "чудесный предмет": 1, "зелье": 2, "кольцо": 3, "свиток": 4,
    "волшебная палочка": 5, "жезл": 6, "посох": 7, "доспех": 8, "оружие": 9,
}
LOOT_ITEM_QUALITY = {
    "обычный": "q_6", "необычный": "q_1", "редкий": "q_2",
    "очень редкий": "q_3", "легендарный": "q_4", "артефакт": "q_7",
}
LOOT_ITEM_TYPE = {
    "чудесный предмет": "t_1", "зелье": "t_2", "кольцо": "t_3", "свиток": "t_4",
    "волшебная палочка": "t_5", "жезл": "t_6", "посох": "t_7",
    "доспех": "t_8", "оружие": "t_9",
}
LOOT_SCROLL_SCHOOLS = {
    "воплощение": "s_1", "вызов": "s_2", "иллюзия": "s_3", "некромантия": "s_4",
    "ограждение": "s_5", "очарование": "s_6", "преобразование": "s_7",
    "прорицание": "s_8",
}


def _map_id(mapping: dict, value: str, label: str) -> int:
    key = value.strip().lower()
    if key in mapping:
        return mapping[key]
    allowed = ", ".join(mapping)
    raise DndError(f"Неизвестный {label} «{value}». Допустимо: {allowed}.")


# ПО → опыт (порядок и значения из калькулятора dnd.su).
CR_ORDER = ["0", "1/8", "1/4", "1/2", "1", "2", "3", "4", "5", "6", "7", "8",
            "9", "10", "11", "12", "13", "14", "15", "16", "17", "18", "19",
            "20", "21", "22", "23", "24", "25", "26", "27", "28", "29", "30"]
CR_XP = {"0": 10, "1/8": 25, "1/4": 50, "1/2": 100, "1": 200, "2": 450,
         "3": 700, "4": 1100, "5": 1800, "6": 2300, "7": 2900, "8": 3900,
         "9": 5000, "10": 5900, "11": 7200, "12": 8400, "13": 10000,
         "14": 11500, "15": 13000, "16": 15000, "17": 18000, "18": 20000,
         "19": 22000, "20": 25000, "21": 33000, "22": 41000, "23": 50000,
         "24": 62000, "25": 75000, "26": 90000, "27": 105000, "28": 120000,
         "29": 135000, "30": 155000}


def _danger_filter_code(cr: str) -> int:
    """Превращает «1/4», «2», «10» в код фильтра опасности сайта."""
    key = cr.strip().lower()
    if key in CR_ORDER:
        return 10 + CR_ORDER.index(key)
    raise DndError(f"Неизвестный показатель опасности «{cr}». Допустимо: {', '.join(CR_ORDER)}.")


# ─────────────────────────────────────────────────────────────────────────────
# Планировщик встреч (сложность боя по правилам DMG 5e)
# ─────────────────────────────────────────────────────────────────────────────

# Пороговые значения опыта по уровню персонажа: (Легко, Средне, Сложно, Смертельно).
XP_THRESHOLDS = {
    1: (25, 50, 75, 100), 2: (50, 100, 150, 200), 3: (75, 150, 225, 400),
    4: (125, 250, 375, 500), 5: (250, 500, 750, 1100), 6: (300, 600, 900, 1400),
    7: (350, 700, 1050, 1700), 8: (450, 900, 1350, 2100), 9: (550, 1100, 1650, 2400),
    10: (600, 1200, 1800, 2800), 11: (800, 1600, 2400, 3600),
    12: (1000, 2000, 3000, 4500), 13: (1100, 2200, 3300, 5100),
    14: (1250, 2500, 3750, 5700), 15: (1400, 2800, 4200, 6400),
    16: (1600, 3200, 4800, 7200), 17: (2000, 3900, 5900, 8800),
    18: (2100, 4200, 6300, 9500), 19: (2400, 4900, 7300, 10900),
    20: (2800, 5700, 8500, 12700),
}
DIFF_LABELS = ("Легко", "Средне", "Сложно", "Смертельно")
_MONSTER_RE = re.compile(r"^\s*(?:(\d+)\s*[xх]\s*)?(.+?)(?:\s*[xх]\s*(\d+))?\s*$", re.I)


def _parse_party(party: str) -> list[int]:
    levels = []
    for part in party.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            level = int(part)
        except ValueError:
            raise DndError(f"Не распознал уровень персонажа «{part}». Уровни через запятую, например «5,5,4».")
        if not 1 <= level <= 20:
            raise DndError(f"Уровень {level} вне диапазона 1–20.")
        levels.append(level)
    if not levels:
        raise DndError("Укажите уровни персонажей, например «5,5,4».")
    return levels


def _parse_monsters(monsters: str) -> list[tuple[str, int]]:
    result = []
    for part in monsters.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        match = _MONSTER_RE.match(part)
        if not match:
            raise DndError(f"Не понял запись монстра «{part}». Формат: «гоблин x3, лич».")
        leading, name, trailing = match.groups()
        name = name.strip()
        if not name:
            raise DndError(f"Не понял запись монстра «{part}».")
        count = int(trailing or leading or 1)
        if count < 1:
            raise DndError(f"Количество монстров «{part}» должно быть больше нуля.")
        result.append((name, count))
    if not result:
        raise DndError("Укажите монстров, например «гоблин x3, лич».")
    return result


def _monster_cr(name: str) -> str:
    card = find_card("monsters", name)
    for line in card["text"].splitlines():
        if line.lower().startswith("опасность"):
            value = line.split(":", 1)[-1].strip()
            # убираем лишнее вроде "2 (или 450 опыта)"
            value = re.split(r"\s*\(", value)[0].strip()
            if value:
                return value
    raise DndError(f"Не удалось определить показатель опасности монстра «{name}».")


def _count_multiplier(count: int) -> float:
    if count == 1:
        return 1.0
    if count == 2:
        return 1.5
    if count <= 6:
        return 2.0
    if count <= 10:
        return 2.5
    if count <= 14:
        return 3.0
    return 4.0


def plan_encounter(party: str, monsters: str) -> str:
    """Считает сложность боевой сцены по правилам D&D 5 (DMG)."""
    levels = _parse_party(party)
    monster_list = _parse_monsters(monsters)

    rows = []
    raw_xp = 0
    total_count = 0
    for name, count in monster_list:
        cr = _monster_cr(name)
        xp = CR_XP.get(cr)
        if xp is None:
            raise DndError(f"Для ПО {cr} нет значения опыта.")
        rows.append((name, count, cr, xp))
        raw_xp += xp * count
        total_count += count

    multiplier = _count_multiplier(total_count)
    adjusted_xp = round(raw_xp * multiplier)

    thresholds = [0, 0, 0, 0]
    for level in levels:
        t = XP_THRESHOLDS.get(level)
        if t is None:
            raise DndError(f"Для уровня {level} нет пороговых значений (доступно 1–20).")
        for i in range(4):
            thresholds[i] += t[i]

    difficulty = "Тривиальная"
    for i, label in reversed(list(enumerate(DIFF_LABELS))):
        if adjusted_xp >= thresholds[i]:
            difficulty = label
            break

    per_player = raw_xp // len(levels)

    lines = [
        f"Партия: {len(levels)} персонаж(ей), уровни {', '.join(map(str, levels))}",
        "Монстры:",
    ]
    for name, count, cr, xp in rows:
        lines.append(f"- {name} x{count} (ПО {cr}, {xp} опыта каждый)")
    lines.append("")
    lines.append(f"Суммарный опыт монстров: {raw_xp}")
    lines.append(f"Множитель за {total_count} противник(ов): x{multiplier:g}")
    lines.append(f"Скорректированный опыт: {adjusted_xp}")
    lines.append("Пороговые значения группы:")
    lines.append("  " + ", ".join(f"{label} {thresholds[i]}" for i, label in enumerate(DIFF_LABELS)))
    lines.append(f"Сложность боевой сцены: {difficulty}")
    lines.append(f"Каждый персонаж получит по {per_player} опыта (без учёта множителя).")
    lines.append("")
    lines.append("Источник: расчёт по правилам DMG (dnd.su/encounter_difficulty).")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Генератор добычи (серверные эндпоинты dnd.su /request/loot/…)
# ─────────────────────────────────────────────────────────────────────────────

LOOT_CATEGORIES = {
    "coins": "монеты", "gems": "камни", "arts": "искусство", "trinkets": "безделушки",
    "treasury": "сокровищница", "scrolls": "свитки", "items": "магические предметы",
}
_COIN_TIERS = {"0": "0", "5": "5", "11": "11", "17": "17", "0-4": "0", "5-10": "5",
               "11-16": "11", "17+": "17"}


def _fetch_loot_json(path: str) -> dict:
    try:
        resp = requests.get(f"{BASE_URL}{path}", headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        raise DndError(f"Не удалось получить добычу: {exc}") from exc
    except ValueError as exc:
        raise DndError(f"Неожиданный ответ генератора добычи: {exc}") from exc


def _strip_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    return _normalize(soup.get_text("\n"))


def generate_loot(category: str, tier: str = "") -> str:
    """Генерирует случайную добычу через серверные эндпоинты dnd.su."""
    key = category.strip().lower()
    if key not in LOOT_CATEGORIES:
        raise DndError(
            f"Неизвестный тип добычи «{category}». Допустимо: {', '.join(LOOT_CATEGORIES)}."
        )

    path = f"/request/loot/{key}/"
    tier = tier.strip().lower()

    if key == "gems":
        if tier:
            price = tier.replace(" ", "").replace("зм", "")
            if price not in {"10", "50", "100", "500", "1000", "5000"}:
                raise DndError("Для камней укажите 10/50/100/500/1000/5000 зм или оставьте пустым.")
            path += f"{price}/"
    elif key == "arts":
        if tier:
            price = tier.replace(" ", "").replace("зм", "")
            if price not in {"25", "250", "750", "2500", "7500"}:
                raise DndError("Для искусства укажите 25/250/750/2500/7500 зм или оставьте пустым.")
            path += f"{price}/"
    elif key in ("coins", "treasury"):
        if tier:
            code = _COIN_TIERS.get(tier)
            if code is None:
                raise DndError("Для монет/сокровищницы укажите опасность 0-4 / 5-10 / 11-16 / 17+.")
            path += f"{code}/"
    elif key == "scrolls":
        if tier:
            level = tier.replace("уровень", "").replace("ур.", "").strip()
            if level in {"заговор", "заклинание 0", "0"}:
                path += "l_0/"
            elif level.isdigit() and 1 <= int(level) <= 9:
                path += f"l_{level}/"
            elif level in LOOT_SCROLL_SCHOOLS:
                path += f"{LOOT_SCROLL_SCHOOLS[level]}/"
            elif tier in ("случайный", "random", "rnd"):
                path += "l_rnd/"
            else:
                raise DndError(
                    "Для свитков укажите уровень 0–9, школу заклинания или «случайный»."
                )
        else:
            path += "l_rnd/"
    elif key == "items":
        if not tier:
            path += "q_rnd/"
        elif tier in LOOT_ITEM_QUALITY:
            path += f"{LOOT_ITEM_QUALITY[tier]}/"
        elif tier in LOOT_ITEM_TYPE:
            path += f"{LOOT_ITEM_TYPE[tier]}/"
        elif tier in ("случайный", "random", "rnd"):
            path += "q_rnd/"
        else:
            raise DndError(
                "Для магических предметов укажите редкость (необычный, редкий…), "
                "тип (оружие, кольцо…) или оставьте пустым."
            )
    # trinkets: путь без значения

    data = _fetch_loot_json(path)
    if not data.get("result"):
        raise DndError("Генератор добычи не вернул результат.")
    text = _strip_html(data.get("string", ""))
    label = LOOT_CATEGORIES[key]
    return f"Сгенерировано: {label}\n{text}\n\nИсточник: https://dnd.su/loot/"