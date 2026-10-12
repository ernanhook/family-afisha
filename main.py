"""
Афиша для малыша — еженедельная сводка семейных мероприятий в Telegram.

Код менять не нужно: всё настраивается текстовыми файлами рядом с этим скриптом.
  settings.txt    — кто идёт, регион, день и час сводки, бюджет, погода
  criteria.txt    — что подходит и что не подходит (обычным языком, читает ИИ)
  categories.txt  — категории событий и их значки
  sources.txt     — сайты с афишами и запросы в Google News
  places.txt      — постоянные места для блока «Куда ещё сходить»
  holidays.txt    — праздники для блока «Скоро»
Секреты (ключи) берутся из переменных окружения: ANTHROPIC_API_KEY,
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.
"""
import datetime as dt
import hashlib
import json
import os
import re
import time
import urllib.parse
from zoneinfo import ZoneInfo

import feedparser
import requests
from bs4 import BeautifulSoup

HERE = os.path.dirname(os.path.abspath(__file__))
TZ = ZoneInfo("Europe/Riga")
STATE_FILE = os.path.join(HERE, "state.json")
PAGE_CHARS = 12000        # сколько текста одной страницы отдавать ИИ
CHUNK_CHARS = 60000       # примерно 20–25 тыс. токенов на один запрос
PRICE_IN, PRICE_OUT = 0.10 / 1e6, 0.50 / 1e6   # Claude Haiku 5.5, $ за токен
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) LatviaFamilyEvents/2.0"}
WD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
WD_KEYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
WD_FULL = ["понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье"]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
          "сентября", "октября", "ноября", "декабря"]


# ================= Чтение файлов настроек =================
def lines_of(name):
    path = os.path.join(HERE, name)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [l.rstrip("\n") for l in f if l.strip() and not l.lstrip().startswith("#")]


def text_of(name):
    return "\n".join(lines_of(name))


def table_of(name, cols):
    rows = []
    for l in lines_of(name):
        parts = [p.strip() for p in l.split("|")]
        rows.append(dict(zip(cols, parts + [""] * (len(cols) - len(parts)))))
    return rows


def load_settings():
    s = {}
    for l in lines_of("settings.txt"):
        if "=" in l:
            k, v = l.split("=", 1)
            s[k.strip()] = v.strip()
    defaults = {"child": "ребёнок 1,5–2 лет", "family": "семья из трёх человек",
                "home_city": "Рига", "region": "вся Латвия", "digest_day": "чт",
                "digest_hour": "10", "days_ahead": "7", "latest_start_hour": "19",
                "weather_lat": "56.95", "weather_lon": "24.11", "weather_name": "Рига",
                "places_count": "3", "holidays_ahead_days": "21",
                "monthly_budget_usd": "0.90", "model": "claude-haiku-5-5"}
    for k, v in defaults.items():
        s.setdefault(k, v)
    aliases = {"пн": 0, "по": 0, "вт": 1, "ср": 2, "чт": 3, "че": 3, "пт": 4, "пя": 4,
               "сб": 5, "су": 5, "вс": 6, "во": 6}
    s["digest_weekday"] = aliases.get(s["digest_day"].lower()[:2], 3)
    for k in ("digest_hour", "days_ahead", "latest_start_hour", "places_count",
              "holidays_ahead_days"):
        s[k] = int(s[k])
    for k in ("weather_lat", "weather_lon", "monthly_budget_usd"):
        s[k] = float(s[k].replace(",", "."))
    return s


def load_sources():
    pages, news = [], []
    for l in lines_of("sources.txt"):
        parts = l.split(None, 2)
        if parts[0] == "page" and len(parts) >= 2:
            pages.append(parts[1])
        elif parts[0] == "news" and len(parts) == 3:
            news.append((parts[1].lower(), parts[2]))
    return pages, news


def gnews_url(lang, q):
    hl, gl, ceid = {"ru": ("ru", "LV", "LV:ru"), "en": ("en", "LV", "LV:en")}.get(
        lang, ("lv", "LV", "LV:lv"))
    return ("https://news.google.com/rss/search?q=" + urllib.parse.quote(q + " when:7d")
            + f"&hl={hl}&gl={gl}&ceid={ceid}")


# ================= Секреты и общие утилиты =================
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
FORCE_DIGEST = (os.environ.get("FORCE_DIGEST") or "").lower() == "true"


class BudgetExceeded(Exception):
    pass


def key(*parts):
    return hashlib.sha1("|".join(str(p).strip().lower() for p in parts).encode()).hexdigest()[:16]


def esc(s):
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def parse_date(s):
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except Exception:
        return None


def nice_range(a, b):
    if a.month == b.month:
        return f"{a.day}–{b.day} {MONTHS[b.month - 1]}"
    return f"{a.day} {MONTHS[a.month - 1]} – {b.day} {MONTHS[b.month - 1]}"


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=0)


def tg(text):
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          data={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
                                "disable_web_page_preview": "true"}, timeout=20)
        if not r.ok:
            print("Telegram error:", r.text)
        time.sleep(1)
    except Exception as e:
        print("Telegram error:", e)


def tg_long(lines):
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > 3800:
            tg(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        tg(chunk)


# ================= Сбор =================
def page_text(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
    except Exception as e:
        print(f"[page] ошибка  {url}: {e}")
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    for t in soup(["script", "style", "nav", "footer", "form", "noscript", "svg"]):
        t.decompose()
    for a in soup.find_all("a", href=True):     # ссылки остаются в тексте для ИИ
        txt = " ".join(a.get_text(" ").split())
        if 4 <= len(txt) <= 150:
            a.replace_with(f"{txt} <{urllib.parse.urljoin(url, a['href'])}>")
    text = " ".join(soup.get_text(" ").split())
    print(f"[page] {len(text):6d} символов  {url}")
    return text[:PAGE_CHARS] if len(text) > 300 else None


def collect_sources():
    pages, news_queries = load_sources()
    sources = []
    for url in pages:
        text = page_text(url)
        if text:
            sources.append({"name": urllib.parse.urlparse(url).netloc.replace("www.", ""),
                            "link": url, "text": text})
    seen = set()
    for lang, q in news_queries:
        try:
            feed = feedparser.parse(requests.get(gnews_url(lang, q), headers=HEADERS,
                                                 timeout=30).content)
        except Exception as e:
            print(f"[news] ошибка  {q}: {e}")
            continue
        print(f"[news] {len(feed.entries):3d} записей  ({lang}) {q}")
        for e in feed.entries[:30]:
            title = e.get("title", "")
            if key(title) in seen:
                continue
            seen.add(key(title))
            summary = BeautifulSoup(e.get("summary", ""), "html.parser").get_text(" ")
            sources.append({"name": "новости", "link": e.get("link", ""),
                            "text": f"{title}. {summary[:400]}"})
    return sources


# ================= ИИ =================
EVENTS_PROMPT = """Ты помогаешь семье найти, куда сходить.
Кто идёт: {family}, {child}. Живут: {home}. Регион поиска: {region}.
Сегодня {today}. Нужны мероприятия с {start} по {end} включительно.

ПРАВИЛА ОТБОРА (их написала семья, соблюдай их):
{criteria}
- события, которые начинаются позже {latest}:00, не подходят.

КАТЕГОРИИ (поле "cat" — один из кодов):
{categories}

Ниже пронумерованные источники: тексты страниц с афишами и новости.
Найди ВСЕ подходящие мероприятия. Ответь ТОЛЬКО JSON-массивом (пустой [] если ничего нет):
[{{"n": номер_источника, "cat": "код категории",
   "title": "название как в источнике", "about": "что это, 3–8 слов по-русски",
   "date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD или ''", "time": "HH:MM или ''",
   "city": "город", "venue": "место или ''", "age": "возраст, если указан, иначе ''",
   "price": "цена, если указана, иначе ''", "free": true/false,
   "ongoing": true, если это выставка/программа на много дней, иначе false,
   "link": "прямая ссылка на событие, если видна в тексте, иначе ''"}}]
Только события, у которых есть хотя бы один день в периоде {start} — {end}.
Не выдумывай даты: если даты нет в источнике — не включай событие.

ИСТОЧНИКИ:
"""

PLACES_PROMPT = """Кто идёт: {family}, {child}. Живут: {home}. Прогноз на выходные:
{weather}
Найдено мероприятий на выходные: {count}.

Из списка постоянных мест ниже выбери {n}, куда лучше всего сходить в эти выходные
с учётом погоды (дождь/холод — крытые места, хорошая погода — улица) и разнообразия.
Не предлагай места, которые были на прошлой неделе: {last}.

Ответь ТОЛЬКО JSON-массивом:
[{{"name": "название как в списке", "why": "почему сейчас, 5–12 слов по-русски"}}]

СПИСОК МЕСТ:
{places}
"""


def claude(prompt, state, cfg, max_tokens=4000):
    u = state["usage"]
    if u["cost"] >= cfg["monthly_budget_usd"]:
        raise BudgetExceeded()
    headers = {"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    payload = {"model": cfg["model"], "max_tokens": max_tokens,
               "messages": [{"role": "user", "content": prompt}]}
    for attempt in range(4):
        r = requests.post("https://api.anthropic.com/v1/messages", json=payload,
                          headers=headers, timeout=180)
        if r.status_code in (429, 500, 502, 503, 529):
            time.sleep(20 * (attempt + 1))
            continue
        if not r.ok:
            raise RuntimeError(f"Claude {r.status_code}: {r.text[:300]}")
        j = r.json()
        us = j.get("usage", {})
        tin = (us.get("input_tokens", 0) + us.get("cache_creation_input_tokens", 0)
               + us.get("cache_read_input_tokens", 0))
        u["cost"] += tin * PRICE_IN + us.get("output_tokens", 0) * PRICE_OUT
        u["calls"] = u.get("calls", 0) + 1
        text = "".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text")
        m = re.search(r"\[.*\]", text, re.S)
        try:
            data = json.loads(m.group(0)) if m else []
        except json.JSONDecodeError:
            print("Не JSON:", text[:300])
            data = []
        return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []
    raise RuntimeError("Claude недоступен")


def find_events(sources, state, cfg, cats, today, start, end):
    header = EVENTS_PROMPT.format(
        family=cfg["family"], child=cfg["child"], home=cfg["home_city"], region=cfg["region"],
        today=today, start=start, end=end, latest=cfg["latest_start_hour"],
        criteria=text_of("criteria.txt") or "всё, что подходит семье с маленьким ребёнком",
        categories="\n".join(f"- {c['code']}: {c['about']}" for c in cats))
    chunks, cur, size = [], [], 0
    for s in sources:
        if cur and size + len(s["text"]) > CHUNK_CHARS:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(s)
        size += len(s["text"])
    if cur:
        chunks.append(cur)

    events = []
    for chunk in chunks:
        body = header + "\n\n".join(f"[{i}] ({s['name']}) {s['link']}\n{s['text']}"
                                    for i, s in enumerate(chunk))
        for ev in claude(body, state, cfg):
            n = ev.get("n")
            if not isinstance(n, int) or not 0 <= n < len(chunk):
                continue
            d1 = parse_date(ev.get("date"))
            d2 = parse_date(ev.get("end_date")) or d1
            if not d1 or d2 < start or d1 > end:
                continue
            ev["_d1"], ev["_d2"] = d1, d2
            if not str(ev.get("link", "")).startswith("http"):
                ev["link"] = chunk[n]["link"]
            ev["source"] = chunk[n]["name"]
            events.append(ev)
        time.sleep(2)
    return events


# ================= Погода =================
WMO = [((0,), "☀️", "ясно"), ((1, 2), "🌤", "переменная облачность"), ((3,), "☁️", "пасмурно"),
       ((45, 48), "🌫", "туман"), (tuple(range(51, 68)), "🌧", "дождь"),
       (tuple(range(71, 78)), "❄️", "снег"), ((80, 81, 82), "🌦", "ливни"),
       ((85, 86), "🌨", "снегопад"), ((95, 96, 99), "⛈", "гроза")]


def weather(cfg, days):
    try:
        r = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": cfg["weather_lat"], "longitude": cfg["weather_lon"],
            "timezone": "Europe/Riga", "forecast_days": 14,
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                     "precipitation_probability_max"}, timeout=20)
        d = r.json()["daily"]
    except Exception as e:
        print("Погода недоступна:", e)
        return []
    out = []
    for day in days:
        if day.isoformat() not in d["time"]:
            continue
        i = d["time"].index(day.isoformat())
        code = d["weather_code"][i]
        emoji, word = next(((e, w) for codes, e, w in WMO if code in codes), ("🌡", ""))
        out.append(f"{WD[day.weekday()]} {day:%d.%m}: {emoji} "
                   f"{round(d['temperature_2m_min'][i]):+d}…{round(d['temperature_2m_max'][i]):+d}°, "
                   f"{word}, осадки {d['precipitation_probability_max'][i] or 0}%")
    return out


# ================= Праздники =================
def easter(year, orthodox=False):
    if orthodox:
        a, b, c = year % 4, year % 7, year % 19
        d = (19 * c + 15) % 30
        e = (2 * a + 4 * b - d + 34) % 7
        month, day = divmod(d + e + 114, 31)
        return dt.date(year, month, day + 1) + dt.timedelta(days=13)
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return dt.date(year, month, day + 1)


def holiday_date(spec, year):
    spec = spec.replace(" ", "").lower()
    m = re.fullmatch(r"(easter|orthodox)([+-]\d+)?", spec)
    if m:
        return easter(year, m.group(1) == "orthodox") + dt.timedelta(days=int(m.group(2) or 0))
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})", spec)
    if m:
        return dt.date(year, int(m.group(2)), int(m.group(1)))
    return None


def upcoming_holidays(today, days):
    out = []
    for h in table_of("holidays.txt", ["date", "name", "hint"]):
        for y in (today.year, today.year + 1):
            try:
                d = holiday_date(h["date"], y)
            except ValueError:
                d = None
            if d and today < d <= today + dt.timedelta(days=days):
                line = f"• {WD[d.weekday()]} {d:%d.%m} — <b>{esc(h['name'])}</b>"
                out.append((d, line + (f": {esc(h['hint'])}" if h["hint"] else "")))
    return [l for _, l in sorted(out)]


# ================= Сводка =================
def fmt_event(ev, emoji):
    d1, d2 = ev["_d1"], ev["_d2"]
    when = f"{WD[d1.weekday()]} {d1:%d.%m}" if d2 == d1 else f"{d1:%d.%m}–{d2:%d.%m}"
    if ev.get("time"):
        when += f", {esc(ev['time'])}"
    line = f"{emoji.get(ev.get('cat'), '•')} {when} — <b>{esc(ev.get('title'))}</b>"
    if ev.get("about"):
        line += f" ({esc(ev['about'])})"
    place = esc(ev.get("city"))
    if ev.get("venue"):
        place += ", " + esc(ev["venue"])
    details = [place, esc(ev.get("age"))]
    details.append("бесплатно" if ev.get("free") else esc(ev.get("price")))
    details.append(f'<a href="{esc(ev["link"])}">{esc(ev["source"])}</a>')
    return line + "\n   " + " · ".join(d for d in details if d)


def dedupe(events, state):
    out, keys = [], set()
    shown_ongoing = set(state.get("shown_ongoing", []))
    for ev in sorted(events, key=lambda e: (e["_d1"], e.get("time") or "")):
        title = re.sub(r"[^\w]+", "", str(ev.get("title", "")).lower())
        k = key(title, ev["_d1"].isoformat())
        if k in keys:
            continue
        if ev.get("ongoing"):                 # длинная выставка — показываем один раз
            ok = key(title, ev.get("venue", ""))
            if ok in shown_ongoing:
                continue
            shown_ongoing.add(ok)
        keys.add(k)
        out.append(ev)
    state["shown_ongoing"] = list(shown_ongoing)[-500:]
    return out


def run_digest(state, cfg, today):
    cats = table_of("categories.txt", ["code", "emoji", "label", "about"])
    emoji = {c["code"]: c["emoji"] for c in cats}
    start = today + dt.timedelta(days=1)
    end = today + dt.timedelta(days=cfg["days_ahead"])
    sat = start + dt.timedelta(days=(5 - start.weekday()) % 7)
    sun = sat + dt.timedelta(days=1)

    sources = collect_sources()
    print(f"Источников: {len(sources)}, бюджет ${state['usage']['cost']:.3f} / "
          f"${cfg['monthly_budget_usd']:.2f}")
    try:
        events = dedupe(find_events(sources, state, cfg, cats, today, start, end), state)
    except BudgetExceeded:
        tg(f"⚠️ Месячный бюджет ИИ (${cfg['monthly_budget_usd']:.2f}) исчерпан, "
           "сводка пропущена до 1-го числа.")
        return

    wx = weather(cfg, [sat, sun])
    order = lambda e: (bool(e.get("ongoing")), e["_d1"], e.get("time") or "")
    on_weekend = lambda e: e["_d1"] <= sun and e["_d2"] >= sat
    free = sorted([e for e in events if e.get("free")], key=order)
    weekend = sorted([e for e in events if not e.get("free") and on_weekend(e)], key=order)
    other = sorted([e for e in events if not e.get("free") and not on_weekend(e)], key=order)

    places = []
    places_list = text_of("places.txt")
    if places_list and cfg["places_count"] > 0:
        try:
            places = claude(PLACES_PROMPT.format(
                family=cfg["family"], child=cfg["child"], home=cfg["home_city"],
                weather="\n".join(wx) or "неизвестен", n=cfg["places_count"],
                count=len([e for e in events if on_weekend(e)]),
                last=", ".join(state.get("last_places", [])) or "—",
                places=places_list), state, cfg, max_tokens=800)[:cfg["places_count"]]
        except Exception as e:
            print("Места:", e)
    state["last_places"] = [p.get("name", "") for p in places]

    lines = [f"👶 <b>Афиша для малыша</b>: {nice_range(start, end)}"]
    if wx:
        lines.append(f"\n🌦 <b>Погода на выходные</b> ({esc(cfg['weather_name'])})")
        lines += wx
    for title, group in ((f"🗓 <b>Выходные</b> ({sat:%d.%m}–{sun:%d.%m})", weekend),
                         ("📅 <b>На неделе</b>", other), ("🆓 <b>Бесплатно</b>", free)):
        if group:
            lines.append("\n" + title)
            lines += [fmt_event(e, emoji) for e in group]
    if not events:
        lines.append("\nНа этой неделе подходящих мероприятий не нашлось.")
    soon = upcoming_holidays(today, cfg["holidays_ahead_days"]) if cfg["holidays_ahead_days"] else []
    if soon:
        lines.append("\n📌 <b>Скоро</b>")
        lines += soon
    if places:
        lines.append("\n🏠 <b>Куда ещё сходить</b>")
        lines += [f"• <b>{esc(p.get('name'))}</b> — {esc(p.get('why'))}" for p in places]
    legend = " · ".join(f"{c['emoji']} {c['label']}" for c in cats if c["emoji"])
    lines.append(f"\n<i>{esc(legend)}\nПеред поездкой проверьте время и билеты на сайте.</i>")
    tg_long(lines)
    print(f"Отправлено: {len(events)} событий, {len(places)} мест. "
          f"Расход за месяц: ${state['usage']['cost']:.4f}")


def main():
    cfg = load_settings()
    now = dt.datetime.now(TZ)
    today = now.date()
    state = load_state()
    first_run = not state
    state.setdefault("usage", {"month": f"{today:%Y-%m}", "cost": 0.0, "calls": 0})
    state.setdefault("last_digest", "")

    if state["usage"]["month"] != f"{today:%Y-%m}":
        u = state["usage"]
        tg(f"📊 За {u['month']}: {u.get('calls', 0)} запросов к ИИ, ≈ ${u['cost']:.2f}")
        state["usage"] = {"month": f"{today:%Y-%m}", "cost": 0.0, "calls": 0}

    if first_run and not FORCE_DIGEST:
        tg(f"✅ Афиша запущена. Сводка — каждый {WD_FULL[cfg['digest_weekday']]} "
           f"в {cfg['digest_hour']}:00.")
        save_state(state)
        return

    due = (today.weekday() == cfg["digest_weekday"] and now.hour >= cfg["digest_hour"]
           and state["last_digest"] != today.isoformat())
    if due or FORCE_DIGEST:
        run_digest(state, cfg, today)
        state["last_digest"] = today.isoformat()
    else:
        print(f"Сегодня не день сводки (сводка: {WD_FULL[cfg['digest_weekday']]}, "
              f"{cfg['digest_hour']}:00).")
    save_state(state)


if __name__ == "__main__":
    main()
