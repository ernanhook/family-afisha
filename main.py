"""
Афиша для малыша — еженедельная сводка семейных мероприятий в Telegram.

Код менять не нужно: всё настраивается текстовыми файлами рядом с этим скриптом.
  settings.txt    — кто идёт, регион, день и час сводки, бюджет, погода
  criteria.txt    — что подходит и что не подходит (обычным языком, читает ИИ)
  categories.txt  — категории событий и их значки
  sources.txt     — сайты с афишами и запросы в Google News
  places.txt      — постоянные места для блока «Куда ещё сходить»
  holidays.txt    — праздники для блока «Скоро»
  distances.txt   — сколько ехать из Риги до городов Латвии (строка «🚗» на карточке)
  recurring.txt   — регулярные события, которые семья знает
Секреты (ключи) берутся из переменных окружения: ANTHROPIC_API_KEY,
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.
"""
import datetime as dt
import hashlib
import json
import os
import re
import time
import unicodedata
import urllib.parse
from zoneinfo import ZoneInfo

import feedparser
import requests
from bs4 import BeautifulSoup

HERE = os.path.dirname(os.path.abspath(__file__))
TZ = ZoneInfo("Europe/Riga")
STATE_FILE = os.path.join(HERE, "state.json")
PAGE_CHARS = 12000        # сколько текста одной страницы отдавать ИИ
CHUNK_CHARS = 25000       # меньше кусок — ИИ внимательнее читает каждый источник
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) LatviaFamilyEvents/2.0"}
WD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
WD_KEYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
WD_FULL_NOM = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота",
               "Воскресенье"]
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
                "monthly_budget_usd": "0.90", "model": "claude-haiku-5-5",
                "show_diagnostics": "auto", "events_api_monthly_limit": "45",
                "places_repeat_days": "28", "week_tips": "yes", "child_birth": "",
                "weekend_reminder": "пт 18", "urgent_per_week": "2", "max_cards": "30",
                "proofread_model": "claude-sonnet-5-5"}
    for k, v in defaults.items():
        s.setdefault(k, v)
    aliases = {"пн": 0, "по": 0, "вт": 1, "ср": 2, "чт": 3, "че": 3, "пт": 4, "пя": 4,
               "сб": 5, "су": 5, "вс": 6, "во": 6}
    s["digest_weekday"] = aliases.get(s["digest_day"].lower()[:2], 3)
    for k in ("digest_hour", "days_ahead", "latest_start_hour", "places_count",
              "holidays_ahead_days", "events_api_monthly_limit", "places_repeat_days",
              "urgent_per_week", "max_cards"):
        try:
            s[k] = int(s[k])
        except ValueError:
            s[k] = int(defaults[k])
    for k in ("weather_lat", "weather_lon", "monthly_budget_usd"):
        s[k] = float(s[k].replace(",", "."))
    # Возраст ребёнка: если указан месяц рождения, бот «растёт» вместе с ребёнком
    s["max_age_ok"] = 2                    # события с минимальным возрастом больше — «на вырост»
    m = re.match(r"(\d{4})-(\d{1,2})", s["child_birth"])
    if m:
        today = dt.datetime.now(TZ).date()
        months = (today.year - int(m.group(1))) * 12 + today.month - int(m.group(2))
        if 0 <= months < 240:
            y, mo = divmod(months, 12)
            s["child"] = "ребёнок " + age_words(y, mo)
            s["max_age_ok"] = max(1, -(-months // 12))     # возраст, округлённый вверх
    s["child_age_note"] = (f"события с минимальным возрастом больше {s['max_age_ok']} лет "
                           f"(«{s['max_age_ok'] + 1}+», «от {s['max_age_ok'] + 1} лет» и старше)")
    return s


def plural(n, one, few, many):
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    return one if n % 10 == 1 else few if 2 <= n % 10 <= 4 else many


def age_words(y, m):
    parts = []
    if y:
        parts.append(f"{y} {plural(y, 'год', 'года', 'лет')}")
    if m or not y:
        parts.append(f"{m} {plural(m, 'месяц', 'месяца', 'месяцев')}")
    return " ".join(parts)


def load_sources():
    pages, news, follows, events_q, portals = [], [], [], [], []
    for l in lines_of("sources.txt"):
        parts = l.split(None, 2)
        if parts[0] == "page" and len(parts) >= 2:
            pages.append(parts[1])
        elif parts[0] == "news" and len(parts) == 3:
            news.append((parts[1].lower(), parts[2]))
        elif parts[0] == "portal" and len(parts) >= 2:
            portals.append(parts[1])
        elif parts[0] == "events" and len(parts) >= 2:
            events_q.append(l.split(None, 1)[1].strip())
        elif parts[0] == "follow" and len(parts) >= 2:
            rest = l.split()[1:]          # follow <страница> <часть ссылки> <сколько>
            follows.append((rest[0], rest[1] if len(rest) > 1 else "",
                            int(rest[2]) if len(rest) > 2 and rest[2].isdigit() else 8))
    return pages, news, follows, events_q, portals


def gnews_url(lang, q):
    hl, gl, ceid = {"ru": ("ru", "LV", "LV:ru"), "en": ("en", "LV", "LV:en")}.get(
        lang, ("lv", "LV", "LV:lv"))
    return ("https://news.google.com/rss/search?q=" + urllib.parse.quote(q + " when:7d")
            + f"&hl={hl}&gl={gl}&ceid={ceid}")


# ================= Секреты и общие утилиты =================
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
EVENTS_API_KEY = os.environ.get("OPENWEBNINJA_API_KEY", "")
FORCE_DIGEST = (os.environ.get("FORCE_DIGEST") or "").lower() == "true"


class BudgetExceeded(Exception):
    pass


REPORT = []          # всё, что нашёл ИИ, с причиной отсева — сохраняется в state.json


# Что произошло за запуск — попадает в сводку, если что-то пошло не так
DIAG = {"pages_failed": [], "pages_ok": 0, "pages_total": 0, "news": 0, "ai_calls": 0, "ai_errors": [],
        "ai_found": 0, "ai_out_of_range": 0, "truncated": 0}


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
    if a == b:
        return f"{a.day} {MONTHS[a.month - 1]}"
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


# ================= Telegram =================
# В группу бот может писать не чаще 20 сообщений в минуту — держим паузу ~3 с.
CHAT = {"id": TG_CHAT, "migrated": False, "last": 0.0, "err": ""}


def tg_api(method, payload, timeout=30):
    """Вызов Bot API. Возвращает result или None (текст ошибки — в CHAT['err'])."""
    if not TG_TOKEN:
        print(f"[tg] нет TELEGRAM_BOT_TOKEN — пропускаю {method}")
        return None
    for attempt in range(4):
        try:
            r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}",
                              json=payload, timeout=timeout)
            j = r.json()
        except Exception as e:
            CHAT["err"] = str(e)[:200]
            print(f"Telegram {method}: {e}")
            time.sleep(3)
            continue
        if j.get("ok"):
            return j.get("result")
        params = j.get("parameters") or {}
        if params.get("retry_after"):                       # слишком часто — ждём, сколько просят
            time.sleep(int(params["retry_after"]) + 1)
            continue
        if params.get("migrate_to_chat_id") and "chat_id" in payload:
            # группа превратилась в супергруппу — у неё новый номер
            CHAT["id"], CHAT["migrated"] = str(params["migrate_to_chat_id"]), True
            payload["chat_id"] = CHAT["id"]
            continue
        CHAT["err"] = str(j.get("description", ""))[:200]
        print(f"Telegram {method}: {CHAT['err']}")
        return None
    return None


def valid_url(u):
    u = str(u or "").strip()
    return u.startswith(("http://", "https://")) and " " not in u and len(u) < 1500


def tg_send(text, buttons=None, notify=False):
    """Одно сообщение. buttons — список рядов кнопок-ссылок. Возвращает message_id или None."""
    wait = 3.1 - (time.time() - CHAT["last"])
    if wait > 0:
        time.sleep(wait)
    payload = {"chat_id": CHAT["id"], "text": text, "parse_mode": "HTML",
               "link_preview_options": {"is_disabled": True},
               "disable_notification": not notify}
    rows = [[b for b in row if valid_url(b.get("url"))] for row in (buttons or [])]
    rows = [r for r in rows if r]
    if rows:
        payload["reply_markup"] = {"inline_keyboard": rows}
    res = tg_api("sendMessage", payload)
    if res is None and rows:                                 # кнопка не понравилась — без кнопок
        payload.pop("reply_markup", None)
        res = tg_api("sendMessage", payload)
    if res is None and "parse" in CHAT["err"].lower():       # сломалась разметка — простым текстом
        payload.pop("parse_mode")
        payload["text"] = re.sub(r"<[^>]+>", "", text).replace("&lt;", "<").replace(
            "&gt;", ">").replace("&amp;", "&")
        res = tg_api("sendMessage", payload)
    CHAT["last"] = time.time()
    return (res or {}).get("message_id")


def tg(text):
    """Простое служебное сообщение со звуком."""
    return tg_send(text, notify=True)


def tg_list(header, items, expandable=False, notify=False, limit=3600):
    """Длинный список: делит на несколько сообщений; expandable — свернуть под «развернуть»."""
    chunks, cur = [], []
    for it in items:
        if cur and len(header) + sum(len(x) + 1 for x in cur) + len(it) > limit:
            chunks.append(cur)
            cur = []
        cur.append(it)
    if cur:
        chunks.append(cur)
    for k, ch in enumerate(chunks):
        head = header if k == 0 else "<i>…продолжение</i>"
        body = "\n".join(ch)
        if expandable:
            body = f"<blockquote expandable>{body}</blockquote>"
        tg_send(f"{head}\n{body}" if head else body, notify=notify and k == 0)


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


ARTICLE_CHARS = 6000
MAX_ARTICLE_AGE_DAYS = 21


def article_text(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
    except Exception as e:
        print(f"[follow] ошибка  {url}: {e}")
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    for t in soup(["script", "style", "nav", "footer", "header", "form", "noscript",
                   "svg", "aside"]):
        t.decompose()
    node = soup.find("article") or soup.find("main") or soup.body or soup
    text = " ".join(node.get_text(" ").split())
    return text[:ARTICLE_CHARS] if len(text) > 200 else None


def follow_links(listing, pattern, max_n, seen_urls):
    """Открывает раздел сайта и читает целиком до max_n свежих статей, чьи ссылки содержат pattern."""
    try:
        r = requests.get(listing, headers=HEADERS, timeout=30)
        r.raise_for_status()
    except Exception as e:
        print(f"[follow] ошибка  {listing}: {e}")
        return [], False
    soup = BeautifulSoup(r.text, "html.parser")
    today = dt.date.today()
    urls = []
    for a in soup.find_all("a", href=True):
        url = urllib.parse.urljoin(listing, a["href"]).split("#")[0]
        if pattern and pattern not in url:
            continue
        if url == listing or url in urls or url in seen_urls:
            continue
        m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", url)     # дата в ссылке, как у LSM
        if m:
            try:
                d = dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
                if (today - d).days > MAX_ARTICLE_AGE_DAYS:
                    continue
            except ValueError:
                pass
        urls.append(url)
    out = []
    for url in urls[:max_n]:
        seen_urls.add(url)
        text = article_text(url)
        if text:
            m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", url)
            if m:
                text = f"[опубликовано {m.group(3)}-{m.group(2)}-{m.group(1)}] " + text
            out.append({"name": urllib.parse.urlparse(url).netloc.replace("www.", ""),
                        "link": url, "text": text})
    print(f"[follow] {len(out):3d} статей  {listing}")
    return out, True


def recurring_source():
    rows = lines_of("recurring.txt")
    if not rows:
        return []
    return [{"name": "регулярные события", "link": "",
             "text": "Регулярные события, которые семья знает (включай их на подходящие дни "
                     "периода; это постоянное расписание, а не разовый анонс):\n" + "\n".join(rows)}]


def google_events(queries, start, end, state, cfg):
    """События из Google Events (сайты, билетные системы, публичные события Facebook)."""
    if not queries:
        return []
    if not EVENTS_API_KEY:
        print("[events] нет ключа OPENWEBNINJA_API_KEY — источник пропущен")
        DIAG["gevents_note"] = "нет ключа"
        return []
    DIAG["gevents_raw"] = 0
    used = state.setdefault("events_api", {"month": "", "count": 0})
    month = dt.date.today().strftime("%Y-%m")
    if used["month"] != month:
        used.update(month=month, count=0)
    out, seen = [], set()
    for q in queries:
        if used["count"] >= cfg["events_api_monthly_limit"]:
            print(f"[events] месячный лимит запросов исчерпан ({used['count']})")
            DIAG.setdefault("other_errors", []).append("лимит Google Events на месяц исчерпан")
            break
        try:
            r = requests.get("https://api.openwebninja.com/realtime-events-data/search-events",
                             params={"query": q, "date": "any", "is_virtual": "false"},
                             headers={"x-api-key": EVENTS_API_KEY}, timeout=60)
            used["count"] += 1
            r.raise_for_status()
            j = r.json()
        except Exception as e:
            print(f"[events] ошибка  {q}: {e}")
            DIAG.setdefault("other_errors", []).append(f"Google Events: {str(e)[:80]}")
            continue
        items = j.get("data", j) if isinstance(j, dict) else j
        items = items if isinstance(items, list) else []
        DIAG["gevents_raw"] += len(items)
        if isinstance(j, dict) and j.get("status") not in (None, "OK"):
            DIAG["gevents_note"] = f"ответ API: {str(j.get('status'))[:40]}"
        kept = 0
        for ev in items:
            if not isinstance(ev, dict):
                continue
            d = parse_date(ev.get("start_time") or ev.get("start_time_utc") or "")
            d2 = parse_date(ev.get("end_time") or "") or d
            if not d or d2 < start or d > end:
                continue                       # отсекаем по датам ещё до ИИ — дешевле
            eid = ev.get("event_id") or ev.get("link") or ev.get("name")
            if eid in seen:
                continue
            seen.add(eid)
            v = ev.get("venue") or {}
            link = ev.get("link") or ""
            for tl in (ev.get("ticket_links") or []) + (ev.get("info_links") or []):
                if isinstance(tl, dict) and tl.get("link"):
                    link = link or tl["link"]
            text = (f"{ev.get('name', '')}. Начало: {ev.get('start_time', '')}. "
                    f"Конец: {ev.get('end_time', '')}. Место: {v.get('name', '')}, "
                    f"{v.get('full_address', '')}. Источник: {ev.get('publisher', '')}. "
                    f"{str(ev.get('description') or '')[:400]}")
            out.append({"name": ev.get("publisher_domain") or "Google Events",
                        "link": link, "text": text})
            kept += 1
        print(f"[events] {len(items):3d} событий, в нужные даты {kept}  {q}")
    DIAG["gevents"] = len(out)
    return out


# ================= Новостные порталы =================
# Ленты крупных порталов хранят лишь последние 30–60 новостей (меньше суток),
# поэтому бот каждый час тихо собирает подходящие заметки в очередь, а в день
# сводки читает их целиком и отдаёт ИИ.
# Сильные признаки события: без хотя бы одного такого слова заметка не берётся
EVENT_WORDS = re.compile(
    r"pasākum|svētk|festivāl|tirdziņ|gadatirg|izstād|izrād[eēi]|darbnīc|koncert|"
    r"atvērto durvju|ģimeņu dien|ģimenes dien|notiks|norisināsies|"
    r"мероприят|праздн|фестивал|ярмарк|выставк|спектакл|мастер-класс|день открытых|дни открытых|"
    r"пройд[уеё]т|состоится|откроется",
    re.I)
# Слабые признаки: только повышают приоритет
WEAK_WORDS = re.compile(r"aicina|brīvdien|nedēļas nogal|sestdien|svētdien|приглаша|выходны|суббот|воскресен",
                        re.I)
FAMILY_WORDS = re.compile(
    r"bērn|ģimen|mazuļ|mazie|vecāk|zoodārz|muzej|pil[sī]|muiž|leļļ|"
    r"дет|семь|семей|малыш|родител|зоопарк|музе|замк|кукол", re.I)
# Разделы порталов, где анонсов семейных событий не бывает
SKIP_URL = re.compile(r"arzemes|sport|criminal|kriminal|politik|v-mire|/world|ekonomik|biznes|"
                      r"zinas/latvija/.*(policij|tiesa)|testi|horoskop|recept|eda-i-recepty|ljublju|"
                      r"komercparraides|zvaigznes|slavenib", re.I)
PORTAL_QUEUE_DAYS = 21
PORTAL_READ_MAX = 40


def collect_portals(state):
    _, _, _, _, portals = load_sources()
    if not portals:
        return
    q = state.setdefault("portal_queue", [])
    known = {x["link"] for x in q}
    added = 0
    for url in portals:
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            feed = feedparser.parse(r.content)
        except Exception as e:
            print(f"[portal] ошибка  {url}: {e}")
            state.setdefault("portal_errors", {})[url] = str(e)[:100]
            continue
        state.setdefault("portal_errors", {}).pop(url, None)
        host = urllib.parse.urlparse(url).netloc.replace("www.", "")
        n = 0
        for e in feed.entries:
            title = " ".join(e.get("title", "").split())
            summary = BeautifulSoup(e.get("summary", ""), "html.parser").get_text(" ")
            text = f"{title} {summary}"
            ev_hits = len(EVENT_WORDS.findall(text))
            fam_hits = len(FAMILY_WORDS.findall(text))
            weak = len(WEAK_WORDS.findall(text))
            link = e.get("link", "")
            if not ev_hits or not link or link in known or SKIP_URL.search(link):
                continue
            pub = time.strftime("%Y-%m-%d", e.published_parsed) if e.get("published_parsed") \
                else dt.date.today().isoformat()
            q.append({"link": link, "title": title, "summary": " ".join(summary.split())[:300],
                      "pub": pub, "source": host, "score": ev_hits * 2 + fam_hits * 2 + weak})
            known.add(link)
            n += 1
        added += n
        print(f"[portal] {len(feed.entries):3d} новостей, в очередь {n:2d}  {host}")
    cutoff = (dt.date.today() - dt.timedelta(days=PORTAL_QUEUE_DAYS)).isoformat()
    state["portal_queue"] = [x for x in q if x["pub"] >= cutoff][-600:]
    print(f"[portal] в очереди {len(state['portal_queue'])} заметок (+{added})")


def portal_sources(state):
    """В день сводки: самые подходящие заметки из очереди, прочитанные целиком."""
    q = sorted(state.get("portal_queue", []), key=lambda x: (x["score"], x["pub"]), reverse=True)
    out = []
    for item in q[:PORTAL_READ_MAX]:
        text = article_text(item["link"]) or f"{item['title']}. {item['summary']}"
        out.append({"name": item["source"], "link": item["link"],
                    "text": f"[опубликовано {item['pub']}] {item['title']}. {text}"})
    DIAG["portal"] = len(out)
    DIAG["portal_errors"] = list(state.get("portal_errors", {}))
    return out


def collect_sources(state=None, cfg=None, start=None, end=None):
    pages, news_queries, follows, events_q, _ = load_sources()
    sources = recurring_source()
    if state is not None:
        sources += google_events(events_q, start, end, state, cfg)
        sources += portal_sources(state)
    seen_urls = set()
    for listing, pattern, max_n in follows:
        DIAG["pages_total"] += 1
        arts, ok = follow_links(listing, pattern, max_n, seen_urls)
        DIAG.setdefault("read", {})[listing] = ok
        for a in arts:
            a["sid"] = listing
        if ok:
            DIAG["pages_ok"] += 1
            DIAG["articles"] = DIAG.get("articles", 0) + len(arts)
        else:
            DIAG["pages_failed"].append(urllib.parse.urlparse(listing).netloc.replace("www.", ""))
        sources += arts
    DIAG["pages_total"] += len(pages)
    for url in pages:
        text = page_text(url)
        DIAG.setdefault("read", {})[url] = bool(text)
        if not text:
            DIAG["pages_failed"].append(urllib.parse.urlparse(url).netloc.replace("www.", ""))
        if text:
            DIAG["pages_ok"] += 1
            sources.append({"name": urllib.parse.urlparse(url).netloc.replace("www.", ""),
                            "link": url, "text": text, "sid": url})
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
            DIAG["news"] += 1
            pub = ""
            if e.get("published_parsed"):
                pub = time.strftime("%Y-%m-%d", e.published_parsed)
            src_name = (e.get("source") or {}).get("title") if isinstance(e.get("source"), dict) else ""
            if not src_name and " - " in title:
                src_name = title.rsplit(" - ", 1)[1]
            sources.append({"name": (src_name or "новости")[:40], "link": e.get("link", ""),
                            "text": (f"[опубликовано {pub}] " if pub else "")
                                    + f"{title}. {summary[:400]}"})
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
Найди ВСЕ мероприятия двух видов и ничего не пропускай:
  "fit": "yes"   — подходит по правилам семьи выше;
  "fit": "maybe" — любое другое детское или семейное событие, которое по правилам не идеально
                   для малыша, но семье полезно о нём знать: программы на школьных каникулах,
                   события для детей постарше, катки, мастерские, детские
                   праздники в торговых центрах, семейные концерты подлиннее.
Совсем не включай (ни в "yes", ни в "maybe"): события только для взрослых, вечерние
(позже {latest}:00), распроданные, акции и скидки магазинов, политику, лекции и конференции,
а также всё, что в правилах семьи перечислено в разделе «НЕ ВКЛЮЧАТЬ СОВСЕМ».
Ответь ТОЛЬКО JSON-массивом (пустой [] если ничего нет):
[{{"n": номер_источника, "fit": "yes|maybe", "cat": "код категории",
   "why_not": "если fit=maybe — коротко по-русски почему не идеально (например «для школьников»), иначе ''",
   "title": "короткое название самого события (до 8 слов), без названия города в конце, без
             хвостов заголовка новости вроде «aicina uz…» или «- Jelgava»; либо грамотный русский
             перевод, либо оригинальное название целиком — никогда не смешивай латиницу и кириллицу
             и не транслитерируй латышские слова русскими буквами",
   "about": "что это, 3–8 слов по-русски",
   "summary": "аннотация: 1–2 коротких предложения по-русски — о чём мероприятие и что там
               будет (сюжет, программа, длительность); только факты из источника, ничего не
               придумывай; если подробностей нет — ''; не пиши, чего в источнике нет
               (никаких «время не указано»)",
   "date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD или ''", "time": "HH:MM или ''",
   "city": "город привычным русским названием (Рига, Юрмала, Елгава, Тукумс, Кулдига, Цесис)",
   "venue": "название места как в источнике, на языке источника, без перевода и транслитерации, или ''", "age": "возраст, если указан, иначе ''",
   "price": "цена, если указана, иначе ''", "free": true/false,
   "sold_out": true, если в источнике сказано, что билеты распроданы (izpārdots, sold out), иначе false,
   "ongoing": true, если это выставка/программа на много дней, иначе false,
   "link": "прямая ссылка на событие, если видна в тексте, иначе ''"}}]
Только события, у которых есть хотя бы один день в периоде {start} — {end}.
Не выдумывай даты: если даты нет в источнике — не включай событие.
Не включай общие анонсы программ и недель («неделя культуры», «месяц …») без конкретного
события с местом и датой; если в таком анонсе описаны конкретные события — включай их по отдельности.
Относительные даты («šo sestdien», «nākamajā svētdienā», «nedēļas nogalē», «в эту субботу»,
«rīt») вычисляй от даты публикации источника, указанной как [опубликовано ГГГГ-ММ-ДД].
Регулярные события («katru sestdienu», «каждую субботу») включай на ближайшие даты в периоде.

ИСТОЧНИКИ:
"""

PLACES_PROMPT = """Кто идёт: {family}, {child}. Живут: {home}. Прогноз на выходные:
{weather}
Найдено мероприятий на выходные: {count}.

Из списка постоянных мест ниже выбери {n}, куда лучше всего сходить в эти выходные
с учётом погоды (дождь/холод — крытые места, хорошая погода — улица) и разнообразия.
Места, которые недавно предлагались, уже убраны из списка — выбирай из оставшихся.

Ответь ТОЛЬКО JSON-массивом:
[{{"name": "название как в списке", "why": "почему сейчас, 5–12 слов по-русски"}}]

СПИСОК МЕСТ:
{places}
"""


TIPS_PROMPT = """Ты — заботливый советчик для семьи: {family}, {child}. Живут: {home}.
Прогноз на выходные:
{weather}
{taste}
Ниже мероприятия из сводки. Для КАЖДОЙ недели выбери ОДНО мероприятие, которое
больше всего порадует именно малыша ({child}) — и при этом будет приятно родителям.
Малышу интересно то, что можно увидеть, услышать и потрогать: живые животные, техника
и транспорт вблизи, музыка и движение, вода, свет, простор, где можно бегать, игровые
зоны для малышей, занятия «мама + малыш», праздники под открытым небом с ярмаркой.
НЕ выбирай: {age_note}, «для школьников», задания и квесты, которые надо читать или решать,
выставки «для смотрения» (растения, искусство, медали, история), лекции и всё, где надо
долго сидеть. При прочих равных выбирай то, что ближе к дому (смотри «дорога»).
Если в какую-то неделю ничего подходящего малышу нет — пропусти эту неделю.
Выбирай только событие, которое проходит в эту неделю.

{weeks}

Ответь ТОЛЬКО JSON-массивом, по одному объекту на неделю:
[{{"week": номер_недели, "n": номер_мероприятия,
   "why": "2–3 коротких предложения по-русски: чем событие понравится именно малышу и практичный совет (когда лучше прийти, что взять, на что обратить внимание)"}}]

МЕРОПРИЯТИЯ:
{events}
"""

DUPES_PROMPT = """Ниже мероприятия, собранные с разных сайтов и из новостей. Одно и то же
мероприятие могло попасть сюда несколько раз: из разных источников, на латышском и на русском,
с немного разными названиями (например «Ķirbjaugi un paprika 2026» и «Выставка тыкв и перцев»).
Найди такие повторы.
Повтор — это ОДНО И ТО ЖЕ событие: то же место (или город) и те же или пересекающиеся даты.
НЕ повторы: разные занятия или выставки в одном месте в одни даты; одинаковые регулярные занятия
в разные дни; одинаковые программы в разных торговых центрах или городах.
Если сомневаешься — это не повтор.
Ответь ТОЛЬКО JSON-массивом групп (пустой [], если повторов нет):
[{{"same": [номер, номер]}}]

МЕРОПРИЯТИЯ:
{events}
"""


def too_old(e, max_ok=2):
    """True, если событие явно для детей старше max_ok лет (по полю age и описанию)."""
    text = " ".join(str(e.get(k) or "") for k in ("age", "about", "summary", "title")).lower()
    ages = [int(x) for x in re.findall(r"(?<![\d,.])(\d{1,2})\s*\+", text)]
    ages += [int(x) for x in re.findall(
        r"(?:\bот|\bno)\s*(\d{1,2})(?![\d,.:])\s*(?:лет|года|г\b|gad)", text)]
    if any(a > max_ok for a in ages):
        return True
    # «для школьников» — да, а «ярмарка поделок школьников» — нет
    return bool(re.search(r"для школьник|только школьник|skolēniem|skolas vecuma|школьного возраста",
                          text))


def week_ranges(start, end):
    weeks, d = [], start
    while d <= end:
        weeks.append((d, min(d + dt.timedelta(days=6), end)))
        d += dt.timedelta(days=7)
    if len(weeks) > 1 and (weeks[-1][1] - weeks[-1][0]).days < 3:   # хвост из 1–3 дней
        weeks[-2:] = [(weeks[-2][0], weeks[-1][1])]                  # присоединяем к прошлой неделе
    return weeks


def event_line(i, e):
    """Строка о событии для ИИ (совет недели, поиск повторов)."""
    when = f"{e['_d1']:%d.%m}" + ("" if e["_d2"] == e["_d1"] else f"–{e['_d2']:%d.%m}")
    drive = e.get("_drive")
    return (f"[{i}] {when} {e.get('time') or ''} | {e.get('title')} | "
            f"{e.get('venue') or ''}, {e.get('city') or ''} | "
            f"дорога: {'в Риге' if not drive else str(drive) + ' мин'} | "
            f"возраст: {e.get('age') or 'не указан'} | "
            f"{'бесплатно' if e.get('free') else e.get('price') or ''} | "
            f"{e.get('about') or ''}. {e.get('summary') or ''}")


def week_tips(events, cfg, state, wx_text, start, end):
    """Совет ИИ: по одному главному событию на каждую неделю периода."""
    weeks = week_ranges(start, end)
    events = [e for e in events if not too_old(e, cfg["max_age_ok"])]
    if not events or not weeks:
        return []
    wtext = "\n".join(f"Неделя {k}: {a:%d.%m}–{b:%d.%m}" for k, (a, b) in enumerate(weeks, 1))
    res = claude(TIPS_PROMPT.format(family=cfg["family"], child=cfg["child"],
                                    home=cfg["home_city"], weather=wx_text or "неизвестен",
                                    taste=taste_text(state), age_note=cfg["child_age_note"],
                                    weeks=wtext,
                                    events="\n".join(event_line(i, e)
                                                     for i, e in enumerate(events, 1))),
                 state, cfg, max_tokens=1500)
    out, used = [], set()
    for t in res:
        try:
            k, n = int(t.get("week")), int(t.get("n"))
        except (TypeError, ValueError):
            continue
        if not (1 <= k <= len(weeks)) or not (1 <= n <= len(events)) or k in used:
            continue
        e = events[n - 1]
        a, b = weeks[k - 1]
        if e["_d1"] > b or e["_d2"] < a:              # событие не в эту неделю
            continue
        used.add(k)
        out.append((k, a, b, e, fix_ru(str(t.get("why") or ""))))
    return sorted(out, key=lambda x: x[0])


PROOF_PROMPT = """Ты — редактор семейной афиши в Telegram. Ниже карточки событий в Латвии,
которые быстро написала другая модель по латышским и русским источникам. Исправь русский язык:
- орфографию, грамматику, согласование («осенний ярмарка» → «осенняя ярмарка»), опечатки,
  разорванные и склеенные слова («в муз ее» → «в музее»);
- бессмыслицу и недопереводы: восстанови нормальное русское название по смыслу, опираясь на
  место проведения («Рудени́вая Семейная диня» → «Осенний семейный день»);
- в одном слове не должно быть смеси латиницы и кириллицы («в Куldīге» → «в Кулдиге»,
  «ContaKids нодарбības» → «Занятия ContaKids»); латышские слова не пиши русскими буквами;
- title — грамотный русский перевод или оригинальное название целиком, если это имя собственное
  (ContaKids, Mazuļu skoliņa, «Mākslas daba» можно оставить как есть);
- city — привычное русское название: Рига, Юрмала, Елгава, Тукумс, Кулдига, Вентспилс,
  Лиепая, Цесис, Сигулда, Валмиера, Мадона, Огре, Саласпилс, Бауска, Добеле, Даугавпилс;
- тон спокойный и живой, без канцелярита.
НЕЛЬЗЯ: добавлять факты, менять даты, время, цены, возраст и смысл, делать текст длиннее.
Поле venue дано только для понимания — его не возвращай. Хорошее поле верни как есть.
Ответь ТОЛЬКО JSON-массивом объектов с тем же "i" и теми же полями, что во входе.

КАРТОЧКИ:
{items}
"""


def proofread(groups, state, cfg):
    """Вычитка русского текста более сильной моделью. groups: [(объект, [поля])]."""
    model = str(cfg.get("proofread_model") or "").strip()
    if not model or model.lower() in ("no", "нет", "off") or not groups:
        return
    items, refs = [], []
    for obj, fields in groups:
        item = {f: str(obj.get(f)) for f in fields if obj.get(f)}
        if not item:
            continue
        refs.append((obj, list(item)))
        item["i"] = len(refs)
        if obj.get("venue"):
            item["venue"] = obj["venue"]
        items.append(item)
    if not items:
        return
    try:
        res = claude(PROOF_PROMPT.format(items=json.dumps(items, ensure_ascii=False, indent=0)),
                     state, cfg, max_tokens=8000, model=model)
    except BudgetExceeded:
        return
    fixed = 0
    for r in res:
        try:
            obj, fields = refs[int(r.get("i")) - 1]
        except (TypeError, ValueError, IndexError):
            continue
        for f in fields:
            new, old = fix_ru(" ".join(str(r.get(f) or "").split())), str(obj.get(f) or "")
            if new and new != old and len(new) <= len(old) * 1.6 + 30:   # без «сочинений»
                obj[f] = new
                fixed += 1
    print(f"[proofread] {model}: карточек {len(items)}, исправлено полей {fixed}")
    DIAG["proofread"] = fixed


def ai_dedupe(events, state, cfg):
    """Склеивает одно и то же событие из разных источников (в т.ч. на разных языках)."""
    if len(events) < 2:
        return events
    try:
        res = claude(DUPES_PROMPT.format(events="\n".join(
            event_line(i, e) for i, e in enumerate(events, 1))), state, cfg, max_tokens=1000)
    except BudgetExceeded:
        return events
    drop = set()
    for g in res:
        ids = [x for x in (g.get("same") or []) if isinstance(x, int) and 1 <= x <= len(events)]
        idx = [i - 1 for i in dict.fromkeys(ids) if (i - 1) not in drop]
        if len(idx) < 2:
            continue
        # все в группе должны пересекаться по датам с первым — иначе это не повтор
        first = events[idx[0]]
        town = drive_minutes(first)

        def same(e):                       # те же даты и тот же город — иначе это не повтор
            t = drive_minutes(e)
            return (e["_d1"] <= first["_d2"] and e["_d2"] >= first["_d1"]
                    and (town is None or t is None or t == town))
        idx = [i for i in idx if same(events[i])]
        if len(idx) < 2:
            continue

        def quality(e):
            return ((e.get("fit") != "maybe") * 8 + ("news.google" not in str(e.get("link"))) * 4
                    + bool(e.get("time")) * 2 + bool(e.get("price") or e.get("free"))
                    + bool(e.get("summary")) + bool(e.get("venue")))
        bi = max(idx, key=lambda i: quality(events[i]))
        best = events[bi]
        for i in idx:
            if i == bi:
                continue
            e = events[i]
            for f in ("time", "price", "age", "summary", "venue", "about"):
                if not best.get(f) and e.get(f):
                    best[f] = e[f]
            if e.get("free") and not best.get("price"):
                best["free"] = True
            drop.add(i)
    if drop:
        print(f"[dupes] склеено повторов: {len(drop)}")
        DIAG["dupes"] = len(drop)
    return [e for i, e in enumerate(events) if i not in drop]


def parse_objects(text):
    """Достаёт из ответа все JSON-объекты {…}, даже если массив оборван или окружён текстом."""
    dec, out, i = json.JSONDecoder(), [], 0
    while True:
        i = text.find("{", i)
        if i < 0:
            return out
        try:
            obj, end = dec.raw_decode(text, i)
            if isinstance(obj, dict):
                out.append(obj)
            i = end
        except json.JSONDecodeError:
            i += 1


PRICES = {"haiku": (0.10, 0.50), "sonnet": (2.0, 10.0), "opus": (4.0, 20.0)}   # $ за 1 млн токенов


def claude(prompt, state, cfg, max_tokens=8000, model=None):
    model = model or cfg["model"]
    p_in, p_out = next((v for k, v in PRICES.items() if k in model), PRICES["haiku"])
    u = state["usage"]
    if u["cost"] >= cfg["monthly_budget_usd"]:
        raise BudgetExceeded()
    headers = {"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    payload = {"model": model, "max_tokens": max_tokens,
               "system": "Отвечай сразу JSON-массивом, без рассуждений, пояснений и markdown. "
                         "Текстовые поля about, summary, why и why_not пиши по-русски, только "
                         "русскими буквами (никаких украинских і, ї, є и казахских ң, қ, ү).",
               "messages": [{"role": "user", "content": prompt}]}
    for attempt in range(4):
        try:
            r = requests.post("https://api.anthropic.com/v1/messages", json=payload,
                              headers=headers, timeout=180)
        except Exception as e:
            err = f"сеть: {e}"
            time.sleep(10)
            continue
        if r.status_code in (429, 500, 502, 503, 529):
            err = f"Claude {r.status_code}"
            time.sleep(20 * (attempt + 1))
            continue
        if not r.ok:
            try:
                msg = r.json().get("error", {}).get("message", "")
            except Exception:
                msg = r.text[:200]
            if r.status_code == 400 and "system" in payload and "system" in msg.lower():
                payload.pop("system")              # запасной вариант: без системной инструкции
                continue
            err = f"Claude {r.status_code}: {msg[:200]}"
            print("Ошибка ИИ:", err)
            DIAG["ai_errors"].append(err)
            return []
        j = r.json()
        us = j.get("usage", {})
        tin = (us.get("input_tokens", 0) + us.get("cache_creation_input_tokens", 0)
               + us.get("cache_read_input_tokens", 0))
        u["cost"] += (tin * p_in + us.get("output_tokens", 0) * p_out) / 1e6
        u["calls"] = u.get("calls", 0) + 1
        DIAG["ai_calls"] += 1
        if j.get("stop_reason") == "max_tokens":
            DIAG["truncated"] += 1
        text = "".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text")
        objs = parse_objects(text)
        print(f"[ai] {us.get('input_tokens', 0)} → {us.get('output_tokens', 0)} токенов, "
              f"объектов: {len(objs)}, stop: {j.get('stop_reason')}")
        if not objs and "[]" not in text:
            print("[ai] ответ без JSON:", text[:500])
        return objs
    print("Ошибка ИИ:", err)
    DIAG["ai_errors"].append(err)
    return []


def find_events(sources, state, cfg, cats, today, start, end):
    header = EVENTS_PROMPT.format(
        family=cfg["family"], child=cfg["child"], home=cfg["home_city"], region=cfg["region"],
        today=today, start=start, end=end, latest=cfg["latest_start_hour"],
        criteria=(text_of("criteria.txt") or "всё, что подходит семье с маленьким ребёнком")
        + f"\n- для малыша не подходят {cfg['child_age_note']} — это «maybe»."
        + taste_text(state),
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
            if isinstance(n, str) and n.isdigit():
                n = int(n)
            if not isinstance(n, int) or not 0 <= n < len(chunk):
                continue
            DIAG["ai_found"] += 1
            for f in ("title", "about", "summary", "why_not", "city", "venue", "age", "price",
                      "time"):
                if ev.get(f) is not None:
                    ev[f] = fix_ru(" ".join(str(ev[f]).split()))
            rep = {"title": str(ev.get("title", ""))[:80], "date": ev.get("date"),
                   "fit": ev.get("fit", "yes"), "source": chunk[n]["name"],
                   "sid": chunk[n].get("sid", "")}
            REPORT.append(rep)
            if ev.get("sold_out") is True or str(ev.get("sold_out")).lower() == "true":
                DIAG["sold_out"] = DIAG.get("sold_out", 0) + 1
                rep["dropped"] = "распродано"
                continue
            d1 = parse_date(ev.get("date"))
            d2 = parse_date(ev.get("end_date")) or d1
            if not d1 or d2 < start or d1 > end:
                DIAG["ai_out_of_range"] += 1
                rep["dropped"] = "вне дат" if d1 else "нет даты"
                continue
            ev["_d1"], ev["_d2"] = d1, d2
            if not str(ev.get("link", "")).startswith("http"):
                ev["link"] = chunk[n]["link"]
            ev["source"] = chunk[n]["name"]
            ev["_sid"] = chunk[n].get("sid", "")
            events.append(ev)
        time.sleep(2)
    return events


# ================= Погода =================
WMO = [((0,), "☀️", "ясно"), ((1, 2), "🌤", "переменная облачность"), ((3,), "☁️", "пасмурно"),
       ((45, 48), "🌫", "туман"), (tuple(range(51, 68)), "🌧", "дождь"),
       (tuple(range(71, 78)), "❄️", "снег"), ((80, 81, 82), "🌦", "ливни"),
       ((85, 86), "🌨", "снегопад"), ((95, 96, 99), "⛈", "гроза")]


def weather(cfg, start, end):
    """Прогноз по дням: {дата: {emoji, word, code, tmin, tmax, pop}} (до 16 дней вперёд)."""
    try:
        r = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": cfg["weather_lat"], "longitude": cfg["weather_lon"],
            "timezone": "Europe/Riga", "forecast_days": 16,
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                     "precipitation_probability_max"}, timeout=20)
        d = r.json()["daily"]
    except Exception as e:
        print("Погода недоступна:", e)
        return {}
    out = {}
    for i, ds in enumerate(d.get("time", [])):
        day = parse_date(ds)
        tmin, tmax = d["temperature_2m_min"][i], d["temperature_2m_max"][i]
        if not day or day < start or day > end or tmin is None or tmax is None:
            continue
        code = d["weather_code"][i] or 0
        emoji, word = next(((e, w) for codes, e, w in WMO if code in codes), ("🌡", ""))
        out[day] = {"emoji": emoji, "word": word, "code": code, "tmin": round(tmin),
                    "tmax": round(tmax), "pop": d["precipitation_probability_max"][i] or 0}
    return out


def wx_line(day, w):
    return (f"{w['emoji']} {WD[day.weekday()]}  {w['tmin']:+d}…{w['tmax']:+d}°, {w['word']}"
            f" · осадки {w['pop']}%")


def wx_hint(ws):
    """Короткий совет по погоде на выходные."""
    if not ws:
        return ""
    if any(w["pop"] >= 60 or w["code"] >= 51 for w in ws):
        return "☔ Возможен дождь — пригодятся дождевик и крытые места"
    tmax = max(w["tmax"] for w in ws)
    if tmax <= 2:
        return "🧤 Морозно — оденьтесь потеплее, лучше крытые места"
    if tmax <= 10:
        return "🧥 Прохладно — оденьтесь потеплее"
    if all(w["code"] <= 2 for w in ws) and tmax >= 15:
        return "☀️ Отличная погода для прогулок"
    return ""


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


# ================= Русский текст, дорога, ссылки =================
# Буквы, которых нет в русском (ИИ иногда подмешивает украинские и казахские)
NON_RU = str.maketrans({"і": "и", "І": "И", "ї": "и", "Ї": "И", "є": "е", "Є": "Е", "ґ": "г",
                        "Ґ": "Г", "ў": "у", "Ў": "У", "ң": "н", "Ң": "Н", "қ": "к", "Қ": "К",
                        "ғ": "г", "Ғ": "Г", "ү": "у", "Ү": "У", "ұ": "у", "Ұ": "У", "ө": "о",
                        "Ө": "О", "ә": "а", "Ә": "А", "һ": "х", "Һ": "Х"})


def fix_ru(s):
    return str(s or "").translate(NON_RU).replace("\u0301", "")    # и без знаков ударения


def _norm(s):
    """Для сравнения названий: нижний регистр, без диакритики и знаков."""
    s = unicodedata.normalize("NFKD", str(s or "").lower())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"[^\w]+", " ", s).strip()


_DIST = None


def distances():
    """distances.txt: «Мадона / Madona | 2:10» → [(регулярка, минуты)], длинные названия первыми."""
    global _DIST
    if _DIST is None:
        rows = []
        for row in table_of("distances.txt", ["names", "time"]):
            m = re.match(r"(\d+):(\d{2})", row["time"])
            if not m:
                continue
            mins = int(m.group(1)) * 60 + int(m.group(2))
            for n in row["names"].split("/"):
                n = _norm(n)
                if n:
                    rows.append((n, mins))
        rows.sort(key=lambda x: -len(x[0]))
        # название + до 2 букв окончания (Jelgava → Jelgavas, Мадона → Мадоне)
        _DIST = [(re.compile(rf"(?<!\w){re.escape(n)}\w{{0,2}}(?!\w)"), mins) for n, mins in rows]
    return _DIST


def drive_minutes(ev):
    """Сколько ехать из Риги (минуты) по городу события; None — если город не узнали."""
    for field in ("city", "venue"):
        text = _norm(ev.get(field))
        if not text:
            continue
        for rx, mins in distances():
            if rx.search(text):
                return mins
    return None


def drive_text(mins, short=False):
    if mins is None or mins < 15:          # Рига и совсем рядом — без пометки
        return ""
    h, m = divmod(mins, 60)
    t = (f"{h} ч {m} мин" if m else f"{h} ч") if h else f"{m} мин"
    nb = "\u00a0"                                   # неразрывный пробел: «~2 ч 10 мин» не рвётся
    return (f"🚗{nb}~{t}".replace(" ", nb)
            + (" · на целый день" if mins >= 120 and not short else ""))


def short_url(u):
    p = urllib.parse.urlparse(u)
    path = p.path.rstrip("/")
    return (p.netloc.replace("www.", "") + (path[:28] + "…" if len(path) > 28 else path))


def maps_link(ev):
    parts = [x for x in (ev.get("venue"), ev.get("city")) if x]
    parts = [x for x in parts if _norm(x) not in ("латвия", "latvija", "latvia", "разные места",
                                                  "dazadas norises vietas", "online", "онлайн")]
    if not parts:
        return ""
    return ("https://www.google.com/maps/search/?api=1&query="
            + urllib.parse.quote(", ".join(dict.fromkeys(parts)) + ", Latvia"))


def gcal_link(ev, start, end):
    """Ссылка «добавить в Google Календарь» с датой, временем и местом."""
    d1, d2 = max(ev["_d1"], start), min(ev["_d2"], end)
    if d2 < d1:
        d2 = d1
    if (d2 - d1).days >= 3:                 # долгая выставка — ставим на ближайшую субботу
        sat = d1 + dt.timedelta(days=(5 - d1.weekday()) % 7)
        d1 = d2 = sat if sat <= d2 else d1
    m = re.match(r"(\d{1,2})[:.](\d{2})", str(ev.get("time") or ""))
    if m and d1 == d2 and int(m.group(1)) < 24:
        s = dt.datetime.combine(d1, dt.time(int(m.group(1)), min(int(m.group(2)), 59)))
        dates = f"{s:%Y%m%dT%H%M%S}/{s + dt.timedelta(hours=2):%Y%m%dT%H%M%S}"
    else:
        dates = f"{d1:%Y%m%d}/{d2 + dt.timedelta(days=1):%Y%m%d}"
    details = " ".join(x for x in (ev.get("about"), ev.get("summary")) if x)[:300]
    if valid_url(ev.get("link")):
        details += "\n" + ev["link"]
    params = {"action": "TEMPLATE", "text": ev.get("title") or "Событие", "dates": dates,
              "ctz": "Europe/Riga", "details": details,
              "location": ", ".join(x for x in (ev.get("venue"), ev.get("city")) if x)}
    return "https://calendar.google.com/calendar/render?" + urllib.parse.urlencode(params)


def resolve_gnews(events):
    """Ссылки Google News (news.google.com/rss/articles/…) → прямые ссылки на статьи."""
    todo = list(dict.fromkeys(e["link"] for e in events if "news.google.com" in str(e.get("link"))))
    if not todo:
        return
    try:
        from googlenewsdecoder import gnewsdecoder
        res = gnewsdecoder(todo, interval=1)
    except Exception as e:
        print("[gnews] не удалось расшифровать ссылки:", e)
        return
    res = [res] if isinstance(res, dict) else list(res or [])
    real = {u: r["decoded_url"] for u, r in zip(todo, res)
            if isinstance(r, dict) and r.get("success") and valid_url(r.get("decoded_url"))}
    for e in events:
        u = real.get(e.get("link"))
        if u:
            e["link"] = u
            e["source"] = urllib.parse.urlparse(u).netloc.replace("www.", "")
    print(f"[gnews] прямых ссылок: {len(real)} из {len(todo)}")


# ================= Карточки =================
WD_GEN = ["понедельника", "вторника", "среды", "четверга", "пятницы", "субботы", "воскресенья"]
WD_ALIASES = {"пн": 0, "по": 0, "вт": 1, "ср": 2, "чт": 3, "че": 3, "пт": 4, "пя": 4,
              "сб": 5, "су": 5, "вс": 6, "во": 6}


def when_text(ev, start=None):
    d1, d2 = ev["_d1"], ev["_d2"]
    if d1 == d2:
        s = f"{WD[d1.weekday()]}, {d1.day} {MONTHS[d1.month - 1]}"
    elif start and d1 < start:
        s = f"до {d2.day} {MONTHS[d2.month - 1]}"
    elif d1.month == d2.month:
        s = f"{WD[d1.weekday()]} {d1.day} – {WD[d2.weekday()]} {d2.day} {MONTHS[d2.month - 1]}"
    else:
        s = f"{d1.day} {MONTHS[d1.month - 1]} – {d2.day} {MONTHS[d2.month - 1]}"
    t = str(ev.get("time") or "").strip()
    return s + (f" · {t}" if t else "")


def place_text(ev):
    return ", ".join(dict.fromkeys(x for x in (ev.get("venue"), ev.get("city")) if x))


def title_link(ev):
    t = esc(ev.get("title"))
    return f'<a href="{esc(ev["link"])}">{t}</a>' if valid_url(ev.get("link")) else t


def card_text(ev, emoji, start):
    """Карточка события — отдельное сообщение, чтобы на него можно было ответить,
    поставить 👍/👎 или переслать."""
    lines = []
    if ev.get("_tip"):
        lines.append("⭐ <b>Совет недели</b>")
    lines.append(f"{emoji.get(ev.get('cat'), '📌')} <b>{title_link(ev)}</b>")   # название — ссылка на сайт
    if ev.get("about"):
        lines.append(f"<i>{esc(ev['about'])}</i>")
    lines.append("")
    lines.append(f"📅 <b>{esc(when_text(ev, start))}</b>")
    place, drive = esc(place_text(ev)), drive_text(ev.get("_drive"))
    if place or drive:
        lines.append("📍 " + " · ".join(x for x in (place, drive) if x))
    money = ("🆓 <b>Бесплатно</b>" if ev.get("free")
             else f"💶 {esc(ev['price'])}" if ev.get("price") else "")
    extra = " · ".join(x for x in (money, f"👶 {esc(ev['age'])}" if ev.get("age") else "") if x)
    if extra:
        lines.append(extra)
    if ev.get("summary"):
        lines.append(f"<blockquote>{esc(ev['summary'])}</blockquote>")
    return "\n".join(lines)


def card_buttons(ev, start, end):
    """Две кнопки под карточкой; сайт открывается по нажатию на название."""
    row = []
    if maps_link(ev):
        row.append({"text": "🗺 Карта", "url": maps_link(ev)})
    row.append({"text": "📅 В календарь", "url": gcal_link(ev, start, end)})
    return [row]


def send_card(ev, emoji, state, start, end, dg, prefix="", notify=False):
    mid = tg_send(prefix + card_text(ev, emoji, start), card_buttons(ev, start, end), notify)
    if mid:                                   # запоминаем, чтобы понять, что оценили
        state.setdefault("cards", {})[str(mid)] = {
            "t": ev.get("title"), "a": ev.get("about"), "c": ev.get("cat"),
            "city": ev.get("city"), "dg": dg}
    return mid


def compact(ev, start, why=False):
    """Строка для коротких списков: дата, название-ссылка, место."""
    d1, d2 = ev["_d1"], ev["_d2"]
    when = (f"{WD[d1.weekday()]} {d1:%d.%m}" if d1 == d2
            else f"до {d2:%d.%m}" if d1 < start else f"{d1:%d.%m}–{d2:%d.%m}")
    sub = [esc(ev.get("venue") or ev.get("city")), drive_text(ev.get("_drive"), short=True)]
    if why and ev.get("why_not"):
        sub.append(f"<i>{esc(ev['why_not'])}</i>")
    sub = " · ".join(x for x in sub if x)
    return f"• <b>{when}</b>  {title_link(ev)}" + (f"\n     {sub}" if sub else "")


def ev_to_state(e):
    d = {k: e.get(k) for k in ("title", "about", "cat", "time", "venue", "city", "link", "free",
                               "price", "age", "summary") if e.get(k)}
    d["d1"], d["d2"] = e["_d1"].isoformat(), e["_d2"].isoformat()
    if e.get("_drive") is not None:
        d["drive"] = e["_drive"]
    if e.get("_tip"):
        d["tip"] = e["_tip"]
    return d


def ev_from_state(d):
    e = dict(d)
    e["_d1"], e["_d2"] = parse_date(d.get("d1")), parse_date(d.get("d2"))
    e["_drive"], e["_tip"] = d.get("drive"), d.get("tip")
    return e


# ================= Повторы =================
def _week(v):
    d = parse_date(v)
    return "%d-W%02d" % d.isocalendar()[:2] if d else str(v)


def dedupe(events, state, period):
    """Убирает дубли. Долгую выставку, которая уже идёт (началась раньше периода и длится
    неделю и больше), показываем в сводке одной недели: повторные запуски на той же неделе её
    не теряют, а со следующей недели она скрыта. Короткие и будущие события не скрываются."""
    out, keys = [], set()
    shown = state.get("shown_ongoing", {})
    if isinstance(shown, list):                    # старый формат — просто сбрасываем
        shown = {}
    start = parse_date(period)
    week = _week(period)
    for ev in sorted(events, key=lambda e: (e["_d1"], e.get("time") or "")):
        title = re.sub(r"[^\w]+", "", str(ev.get("title", "")).lower())
        k = key(title, ev["_d1"].isoformat())
        if k in keys:
            continue
        if start and ev["_d1"] < start and (ev["_d2"] - ev["_d1"]).days >= 7:
            ok = key(title, ev.get("venue", ""))
            if ok in shown and _week(shown[ok]) < week:
                continue                            # уже показывали на прошлых неделях
            shown.setdefault(ok, week)
        keys.add(k)
        out.append(ev)
    state["shown_ongoing"] = dict(list(shown.items())[-500:])
    return out


# ================= Оценки 👍 / 👎 =================
# Реакции на карточки (бот должен быть администратором группы) или ответы на карточку
# словом/смайликом. Бот проверяет их каждый час и учитывает при следующем отборе.
POS = {"👍", "❤", "🔥", "🥰", "👏", "😁", "🎉", "🤩", "🙏", "👌", "😍", "❤‍🔥", "💯", "⚡",
       "🏆", "🍾", "🤗", "😇", "🆒", "💘", "😘", "😎", "🦄", "🤝", "🫡", "✍"}
NEG = {"👎", "💩", "🤮", "😴", "🥱", "🙈", "💔", "😐", "🤨", "😢", "😭", "🤡", "🙉", "🙊"}


def _emo(x):
    return str(x or "").replace("️", "")


def reaction_vote(reactions):
    v = 0
    for r in reactions:
        if r.get("type") == "emoji":
            e = _emo(r.get("emoji"))
            v = 1 if e in POS else -1 if e in NEG else v
    return v


def text_vote(t):
    t = _emo(t).strip().lower()
    if not t or len(t) > 80:
        return 0
    if (any(x in t for x in ("👎", "💩", "🙈")) or t in ("-", "–", "—")
            or re.search(r"\b(нет|мимо|не интересно|неинтересно|не хочу|не надо|не то)\b", t)):
        return -1
    if (any(x in t for x in POS) or t.startswith("+")
            or re.search(r"\b(да|хочу|класс|супер|круто|пойд[её]м|идём|идем|интересно)\b", t)):
        return 1
    return 0


def apply_vote(state, mid, who, v):
    c = state.get("cards", {}).get(mid)
    if not c:
        return
    taste = state.setdefault("taste", {})
    rec = taste.pop(mid, None) or {k: c.get(k) for k in ("t", "a", "c", "city")}
    votes = rec.setdefault("votes", {})
    if v:
        votes[who] = v
    else:
        votes.pop(who, None)
    if votes:
        taste[mid] = rec                         # свежие оценки — в конец
    while len(taste) > 120:
        taste.pop(next(iter(taste)))


def taste_text(state):
    """Что семье понравилось и не понравилось — подсказка для ИИ."""
    like, dislike = [], []
    for rec in (state or {}).get("taste", {}).values():
        s = sum(rec.get("votes", {}).values())
        item = str(rec.get("t") or "")
        if rec.get("a"):
            item += f" — {rec['a']}"
        if rec.get("city") and rec["city"] != "Рига":
            item += f" ({rec['city']})"
        if s > 0:
            like.append(item)
        elif s < 0:
            dislike.append(item)
    if not like and not dislike:
        return ""
    out = ("\nВКУС СЕМЬИ (по их оценкам 👍/👎 в прошлых сводках): похожее на понравившееся — "
           "в приоритете, похожее на непонравившееся — реже или в «maybe».")
    if like:
        out += "\nПонравилось: " + "; ".join(like[-20:])
    if dislike:
        out += "\nНе понравилось: " + "; ".join(dislike[-20:])
    return out + "\n"


def process_updates(state):
    """Забирает из Telegram новые реакции и ответы на карточки."""
    off, n = state.get("tg_offset", 0), 0
    cards = state.get("cards", {})
    chat = str(CHAT["id"])
    for _ in range(5):
        res = tg_api("getUpdates", {"offset": off, "timeout": 0, "limit": 100,
                                    "allowed_updates": ["message", "message_reaction"]})
        if not res:
            break
        for u in res:
            off = max(off, u["update_id"] + 1)
            r, m = u.get("message_reaction"), u.get("message")
            if r and str(r.get("chat", {}).get("id")) == chat:
                mid = str(r.get("message_id"))
                who = str((r.get("user") or r.get("actor_chat") or {}).get("id", "anon"))
                if mid in cards:
                    apply_vote(state, mid, who, reaction_vote(r.get("new_reaction") or []))
                    n += 1
            elif m and str(m.get("chat", {}).get("id")) == chat:
                mid = str((m.get("reply_to_message") or {}).get("message_id", ""))
                v = text_vote(m.get("text") or "") if mid in cards else 0
                if v:
                    apply_vote(state, mid, str((m.get("from") or {}).get("id", "anon")), v)
                    n += 1
                    tg_api("setMessageReaction", {"chat_id": CHAT["id"],
                                                  "message_id": m["message_id"],
                                                  "reaction": [{"type": "emoji", "emoji": "👌"}]})
        if len(res) < 100:
            break
    state["tg_offset"] = off
    if n:
        print(f"[tg] учтено оценок: {n}")


def bot_is_admin(state):
    me = tg_api("getMe", {})
    if me:
        m = tg_api("getChatMember", {"chat_id": CHAT["id"], "user_id": me["id"]})
        state["bot_admin"] = bool(m) and m.get("status") in ("administrator", "creator")
    return state.get("bot_admin", False)


# ================= Здоровье источников =================
def update_health(state, today):
    """Считает сводки подряд, в которых сайт не открылся или не дал ни одного события."""
    pages, _, follows, _, _ = load_sources()
    configured = list(pages) + [f[0] for f in follows]
    h = state.setdefault("health", {})
    for sid in list(h):
        if sid not in configured:
            h.pop(sid)
    read = DIAG.get("read", {})
    ai_ok = DIAG["ai_calls"] > 0 and not DIAG["ai_errors"]
    if ai_ok and state.get("health_date") != today.isoformat():    # раз в день, без тестов
        state["health_date"] = today.isoformat()
        found = {}
        for r in REPORT:
            found[r.get("sid")] = found.get(r.get("sid"), 0) + 1
        for sid in configured:
            if sid not in read:
                continue
            rec = h.setdefault(sid, {"bad": 0, "why": ""})
            if read[sid] and found.get(sid):
                rec.update(bad=0, why="")
            else:
                rec["bad"] += 1
                rec["why"] = "не открывается" if not read[sid] else "нет событий"
    return [(sid, r) for sid, r in h.items() if r.get("bad", 0) >= 3]


# ================= Постоянные места =================
def pick_places(state, cfg, today, wx_text, weekend_count):
    places = []
    # Места, которые уже предлагались за последние places_repeat_days, исключаются
    hist = state.setdefault("places_history", [])
    cutoff = (today - dt.timedelta(days=cfg["places_repeat_days"])).isoformat()
    hist[:] = [h for h in hist if h[0] >= cutoff and h[0] != today.isoformat()]
    recent = {h[1].lower() for h in hist}
    all_places = lines_of("places.txt")
    fresh = [l for l in all_places if l.split("|")[0].strip().lower() not in recent]
    if len(fresh) < cfg["places_count"]:          # список исчерпан — начинаем круг заново
        fresh, hist[:] = all_places, []
    if fresh and cfg["places_count"] > 0:
        try:
            places = claude(PLACES_PROMPT.format(
                family=cfg["family"], child=cfg["child"], home=cfg["home_city"],
                weather=wx_text or "неизвестен", n=cfg["places_count"], count=weekend_count,
                places="\n".join(fresh)), state, cfg, max_tokens=2000)[:cfg["places_count"]]
        except Exception as e:
            print("Места:", e)
    city = {l.split("|")[0].strip().lower(): (l.split("|") + ["", "", ""])[2].strip()
            for l in all_places}
    for p in places:
        p["city"] = city.get(str(p.get("name", "")).strip().lower(), "").split(",")[0].strip()
        p["why"] = fix_ru(p.get("why"))
    state["last_places"] = [p.get("name", "") for p in places]
    hist += [[today.isoformat(), p.get("name", "")] for p in places if p.get("name")]
    return places


# ================= Сводка =================
def segments(start, end):
    """Период → куски: будни / выходные по порядку."""
    segs, d, one = [], start, dt.timedelta(days=1)
    while d <= end:
        a, we = d, d.weekday() >= 5
        while d + one <= end and ((d + one).weekday() >= 5) == we:
            d += one
        segs.append((a, d, we))
        d += one
    return segs


def seg_name(a, b, we, today):
    if we:
        return "Выходные"
    if a == today:
        return "Сегодня" if a == b else "Сегодня и завтра" if (b - a).days == 1 else "Ближайшие дни"
    return "Будни"


def seg_icon(a, b, we, today):
    return "🎈" if we else "⚡" if a == today else "🗓"


def diag_text(cfg, n_events, places):
    mode = cfg["show_diagnostics"].lower()
    problems = mode == "always" or (mode != "never" and (
        DIAG["ai_errors"] or n_events < 3 or DIAG["truncated"] or DIAG["pages_failed"]
        or (cfg["places_count"] > 0 and not places)
        or DIAG["pages_ok"] < DIAG["pages_total"] / 2))
    if not problems:
        return ""
    diag = (f"⚙️ Диагностика: сайтов прочитано {DIAG['pages_ok']} из {DIAG['pages_total']}, "
            f"порталы {DIAG.get('portal', 0)}, Google Events {DIAG.get('gevents', 0)}"
            f"{' из ' + str(DIAG['gevents_raw']) if 'gevents_raw' in DIAG else ''}"
            f"{' (' + DIAG['gevents_note'] + ')' if DIAG.get('gevents_note') else ''}, "
            f"статей {DIAG.get('articles', 0)}, новостей {DIAG['news']}, "
            f"запросов к ИИ {DIAG['ai_calls']}, ИИ нашёл {DIAG['ai_found']} "
            f"(вне дат {DIAG['ai_out_of_range']}, повторов {DIAG.get('dupes', 0)})")
    if DIAG["truncated"]:
        diag += f", обрезанных ответов {DIAG['truncated']}"
    failed = DIAG["pages_failed"] + [urllib.parse.urlparse(u).netloc.replace("www.", "")
                                      for u in DIAG.get("portal_errors", [])]
    if failed:
        diag += "\nНе прочитаны: " + ", ".join(failed)
    if DIAG["ai_errors"]:
        diag += "\nОшибка ИИ: " + "; ".join(dict.fromkeys(DIAG["ai_errors"]))[:400]
    if DIAG.get("other_errors"):
        diag += "\nДругие ошибки: " + "; ".join(dict.fromkeys(DIAG["other_errors"]))[:300]
    return diag


def footer(state, cfg, n_events, places, warnings):
    out = []
    nv = sum(len(r.get("votes", {})) for r in state.get("taste", {}).values())
    if nv:
        out.append(f"❤️ Бот учитывает ваши оценки: {nv}")
    if warnings:
        out.append("🛠 <b>Проверьте источники</b> — пусто 3 сводки подряд:")
        out += [f"• {esc(short_url(sid))} — {esc(r.get('why'))}" for sid, r in warnings[:8]]
    u = state["usage"]
    if u["cost"] >= 0.8 * cfg["monthly_budget_usd"]:
        out.append(f"💰 Бюджет ИИ на месяц: ${u['cost']:.2f} из ${cfg['monthly_budget_usd']:.2f}")
    diag = diag_text(cfg, n_events, places)
    if diag:
        out.append(f"<i>{esc(diag)}</i>")
    out.append("<i>Перед поездкой проверьте время и билеты на сайте.</i>")
    return "\n".join(out)


def send_digest(state, cfg, today, start, end, events, maybe, wx, places, tips, warnings, emoji):
    dg = today.isoformat()
    segs = segments(start, end)
    anyday = [e for e in events if (min(e["_d2"], end) - max(e["_d1"], start)).days >= 5]
    long_ids = {id(e) for e in anyday}
    by_seg = [[] for _ in segs]
    for e in events:
        if id(e) in long_ids:
            continue
        d = max(e["_d1"], start)
        k = next((i for i, (a, b, we) in enumerate(segs) if a <= d <= b), len(segs) - 1)
        by_seg[k].append(e)
    free = [e for e in events if e.get("free")]

    # 1. Обложка: что внутри
    n = len(events)
    cover = ["👶 <b>АФИША ДЛЯ МАЛЫША</b>", f"<i>{nice_range(start, end)} · {esc(cfg['region'])}</i>", ""]
    if n:
        cover.append(f"<b>В выпуске {n} {plural(n, 'событие', 'события', 'событий')} для малыша</b>")
        for (a, b, we), evs in zip(segs, by_seg):
            if evs:
                cover.append(f"{seg_icon(a, b, we, today)} {seg_name(a, b, we, today)} · "
                             f"{nice_range(a, b)} — {len(evs)}")
        if anyday:
            cover.append(f"🕰 В любой день — {len(anyday)}")
        if free:
            cover.append(f"🆓 Из них бесплатных — {len(free)}")
    else:
        cover.append("Подходящих малышу событий в этот период не нашлось 😔")
    if maybe:
        cover.append(f"🔎 Ещё для детей и семей — {len(maybe)}")
    if tips:
        cover.append("⭐ Совет недели — в самом конце")
    cover.append("")
    if state.get("bot_admin"):
        cover.append("<i>Ставьте 👍 или 👎 на карточки — бот запомнит, что вам нравится.</i>")
    else:
        cover.append("<i>Ответьте на карточку «👍» или «👎» — бот запомнит, что вам нравится. "
                     "Сделайте бота администратором группы — и можно будет просто ставить реакции.</i>")
    tg_send("\n".join(cover), notify=True)

    # 2. Разделы по дням с карточками
    cap, shown, overflow = cfg["max_cards"], 0, []

    def cards(evs):
        nonlocal shown
        for e in evs:
            if shown < cap:
                send_card(e, emoji, state, start, end, dg)
                shown += 1
            else:
                overflow.append(e)

    first_weekend = True
    for (a, b, we), evs in zip(segs, by_seg):
        show_anyway = we and first_weekend          # погоду ближайших выходных показываем всегда
        first_weekend = first_weekend and not we
        if not evs and not show_anyway:
            continue
        head = [f"{seg_icon(a, b, we, today)} <b>{seg_name(a, b, we, today).upper()} · "
                f"{nice_range(a, b).upper()}</b>"]
        if we:
            ws = [(d, wx[d]) for d in (a, b) if d in wx]
            head += [wx_line(d, w) for d, w in dict(ws).items()]
            hint = wx_hint([w for _, w in ws])
            if hint:
                head.append(f"<i>{hint}</i>")
        if not evs:
            head.append("<i>Событий для малыша не нашлось — идеи мест ближе к концу сводки.</i>")
        tg_send("\n".join(head))
        cards(evs)
    if anyday:
        tg_send("🕰 <b>В ЛЮБОЙ ДЕНЬ</b>\n<i>выставки и программы, которые идут весь период</i>")
        cards(anyday)
    if overflow:
        tg_list("📋 <b>И ЕЩЁ ПОДХОДЯЩЕЕ</b>", [compact(e, start) for e in overflow])

    # 3. Короткие списки
    if free:
        tg_list("🆓 <b>БЕСПЛАТНО</b> · всё в одном месте\n", [compact(e, start) for e in free])
    if maybe:
        tg_list(f"🔎 <b>ЕЩЁ ДЛЯ ДЕТЕЙ И СЕМЕЙ</b> · {len(maybe)}\n"
                "<i>для детей постарше или не совсем для малыша — нажмите, чтобы развернуть</i>",
                [compact(e, start, why=True) for e in maybe], expandable=True)
    parts = []
    soon = upcoming_holidays(today, cfg["holidays_ahead_days"]) if cfg["holidays_ahead_days"] else []
    if soon:
        parts.append("📌 <b>СКОРО</b>\n" + "\n".join(soon))
    if places:
        lines = ["🏠 <b>КУДА ЕЩЁ СХОДИТЬ</b>", "<i>постоянные места — под погоду выходных</i>"]
        for p in places:
            m = maps_link({"venue": p.get("name"), "city": p.get("city")})
            lines.append(f"• <b>{esc(p.get('name'))}</b>"
                         + (f' · <a href="{esc(m)}">на карте</a>' if m else ""))
            if p.get("why"):
                lines.append(f"     {esc(p['why'])}")
        parts.append("\n".join(lines))
    if parts:
        tg_send("\n\n".join(parts))

    # 4. Совет недели и подвал — последним сообщением
    last = []
    if tips:
        t = ["⭐ <b>СОВЕТ НЕДЕЛИ</b>", "<i>если успеть только одно</i>"]
        for k, a, b, e, why in tips:
            t += ["", f"<b>{nice_range(a, b)}</b>",
                  f"{emoji.get(e.get('cat'), '📌')} {title_link(e)}",
                  f"📅 {esc(when_text(e, start))}"
                  + (f" · 📍 {esc(e.get('venue') or e.get('city'))}" if e.get("venue") or e.get("city") else "")]
            if why:
                t.append(f"<blockquote>{esc(why)}</blockquote>")
        last.append("\n".join(t))
    last.append(footer(state, cfg, len(events), places, warnings))
    tg_send("\n\n".join(last))


def run_digest(state, cfg, today):
    cats = table_of("categories.txt", ["code", "emoji", "label", "about"])
    emoji = {c["code"]: c["emoji"] for c in cats}
    start = today   # сегодняшние события тоже входят в сводку
    end = today + dt.timedelta(days=cfg["days_ahead"])

    sources = collect_sources(state, cfg, start, end)
    print(f"Источников: {len(sources)}, бюджет ${state['usage']['cost']:.3f} / "
          f"${cfg['monthly_budget_usd']:.2f}")
    try:
        found = find_events(sources, state, cfg, cats, today, start, end)
    except BudgetExceeded:
        tg(f"⚠️ Месячный бюджет ИИ (${cfg['monthly_budget_usd']:.2f}) исчерпан, "
           "сводка пропущена до 1-го числа. Поднять лимит можно в settings.txt "
           "(monthly_budget_usd).")
        return
    warnings = update_health(state, today)
    events = ai_dedupe(dedupe(found, state, start.isoformat()), state, cfg)
    for e in events:
        e["_drive"] = drive_minutes(e)
        if e.get("fit") != "maybe" and too_old(e, cfg["max_age_ok"]):
            e["fit"] = "maybe"               # явно для детей постарше — не в основной список
            e["why_not"] = e.get("why_not") or (f"возраст {e['age']}" if e.get("age")
                                                 else "для детей постарше")
    order = lambda e: (e["_d1"], e.get("time") or "")
    maybe = sorted([e for e in events if e.get("fit") == "maybe"], key=order)
    events = sorted([e for e in events if e.get("fit") != "maybe"], key=order)

    wx = weather(cfg, start, end)
    sat = start + dt.timedelta(days=(5 - start.weekday()) % 7)
    sun = sat + dt.timedelta(days=1)
    wx_text = "\n".join(wx_line(d, wx[d]) for d in (sat, sun) if d in wx)
    places = pick_places(state, cfg, today, wx_text,
                         len([e for e in events if e["_d1"] <= sun and e["_d2"] >= sat]))
    tips = []
    if cfg["week_tips"].lower() in ("yes", "да", "on", "1"):
        try:
            tips = week_tips(events, cfg, state, wx_text, start, end)
        except BudgetExceeded:
            pass
        except Exception as e:
            print("Совет недели:", e)
    for k, a, b, e, why in tips:
        e["_tip"] = why or "—"
    proofread([(e, ["title", "about", "summary", "city", "_tip"]) for e in events]
              + [(e, ["title", "why_not", "city"]) for e in maybe]
              + [(p, ["why"]) for p in places], state, cfg)
    for k, (wk, a, b, e, why) in enumerate(tips):      # совет недели берём уже вычитанным
        tips[k] = (wk, a, b, e, e.get("_tip") if e.get("_tip") not in (None, "—") else why)
    resolve_gnews(events + maybe)

    send_digest(state, cfg, today, start, end, events, maybe, wx, places, tips, warnings, emoji)
    state["digest_events"] = {"date": today.isoformat(), "events": [ev_to_state(e) for e in events]}
    state["last_report"] = {
        "date": today.isoformat(),
        "sources": [{"name": x["name"], "chars": len(x["text"]), "link": x["link"][:120]}
                    for x in sources],
        "found": REPORT[:300],
    }
    print(f"Отправлено: {len(events)} событий, {len(maybe)} «ещё», {len(places)} мест. "
          f"Расход за месяц: ${state['usage']['cost']:.4f}")


# ================= Напоминание в пятницу =================
def weekend_reminder(state, cfg, now):
    """В пятницу вечером — короткий список на выходные из последней сводки и свежая погода."""
    spec = str(cfg.get("weekend_reminder", "")).lower().split()
    if not spec or spec[0] in ("no", "нет", "off", "0"):
        return
    wd = WD_ALIASES.get(spec[0][:2])
    hour = int(spec[1]) if len(spec) > 1 and spec[1].isdigit() else 18
    today = now.date()
    if wd is None or today.weekday() != wd or now.hour < hour \
            or state.get("last_reminder") == today.isoformat():
        return
    dg = state.get("digest_events") or {}
    if not parse_date(dg.get("date")) or (today - parse_date(dg["date"])).days > 7:
        return
    state["last_reminder"] = today.isoformat()
    sat = today - dt.timedelta(days=1) if today.weekday() == 6 else \
        today + dt.timedelta(days=(5 - today.weekday()) % 7)
    days = [d for d in (sat, sat + dt.timedelta(days=1)) if d >= today]
    evs = [ev_from_state(x) for x in dg.get("events", [])]
    evs = [e for e in evs if e["_d1"] and e["_d1"] <= days[-1] and e["_d2"] >= days[0]]
    if not evs:
        print("Напоминание: на выходные событий нет")
        return
    wx = weather(cfg, days[0], days[-1])
    lines = [f"🎈 <b>НА ЭТИ ВЫХОДНЫЕ · {nice_range(days[0], days[-1]).upper()}</b>"]
    lines += [wx_line(d, wx[d]) for d in days if d in wx]
    hint = wx_hint([wx[d] for d in days if d in wx])
    if hint:
        lines.append(f"<i>{hint}</i>")

    def line(e, multi=False):
        t = f"<b>{esc(e['time'])}</b>  " if e.get("time") and not multi else ""
        sub = " · ".join(x for x in (esc(e.get("venue") or e.get("city")),
                                      drive_text(e.get("_drive"), short=True),
                                      "🆓" if e.get("free") else "",
                                      f"до {e['_d2']:%d.%m}" if multi else "") if x)
        return f"• {t}{'⭐ ' if e.get('_tip') else ''}{title_link(e)}" + (f"\n     {sub}" if sub else "")

    for d in days:
        single = sorted([e for e in evs if e["_d1"] == e["_d2"] == d],
                        key=lambda e: (not e.get("time"), e.get("time") or ""))
        if single:
            lines += ["", f"<b>{WD_FULL_NOM[d.weekday()]}</b>"] + [line(e) for e in single]
    multi = [e for e in evs if e["_d1"] != e["_d2"]]
    if multi:
        lines += ["", "<b>В любой из дней</b>"] + [line(e, True) for e in multi]
    lines += ["", "<i>Подробности, карта и календарь — в карточках сводки. Хороших выходных!</i>"]
    tg_send("\n".join(lines), notify=True)


# ================= Срочные анонсы =================
URGENT_WORDS = re.compile(
    r"cirk|цирк|шапито|festivāl|фестивал|expo|izstād|выставк|svētk|праздн|tirdziņ|gadatirg|"
    r"ярмарк|zoodārz|зоопарк|dinozaur|динозавр|gaismas|ģimeņu|ģimenes|семейн|bērnu|детск|"
    r"малыш|mazuļ", re.I)

URGENT_PROMPT = """Это свежие заметки с новостных порталов Латвии. Семья: {family}, {child}, живут: {home}.
Выбери только анонсы КРУПНЫХ и РЕДКИХ семейных событий, о которых стоит сообщить сразу, не
дожидаясь еженедельной сводки: приезд цирка или шапито, большой семейный или детский фестиваль,
выставка животных или динозавров, праздник города с детской программой, большая сезонная ярмарка,
Kids Expo и подобное. Событие должно пройти в Латвии в ближайшие 30 дней (сегодня {today}).
Не выбирай: обычные занятия и кружки, спектакли и театр, концерты в залах, лекции, новости и итоги
уже прошедших событий, акции магазинов.
Семья уже знает об этих событиях — их не выбирай: {known}
Лучше ничего не выбрать, чем выбрать лишнее. Если подходящего нет — ответь [].
Ответь ТОЛЬКО JSON-массивом: [{{"n": номер_заметки}}]

ЗАМЕТКИ:
{items}
"""


def urgent_check(state, cfg, now):
    """Раз в час днём: если на порталах объявили что-то крупное — сразу короткая карточка."""
    limit = cfg["urgent_per_week"]
    today = now.date()
    if limit <= 0 or not (9 <= now.hour < 21):
        return
    if today.weekday() == cfg["digest_weekday"] and now.hour <= cfg["digest_hour"]:
        return                                          # скоро и так придёт сводка
    y, w, _ = today.isocalendar()
    uw = state.setdefault("urgent_week", {"week": "", "n": 0})
    if uw["week"] != f"{y}-{w}":
        uw.update(week=f"{y}-{w}", n=0)
    if uw["n"] >= limit:
        return
    checked = state.setdefault("urgent_checked", [])
    seen, since = set(checked), (today - dt.timedelta(days=2)).isoformat()
    cand = [x for x in state.get("portal_queue", []) if x["link"] not in seen
            and x["pub"] >= since and URGENT_WORDS.search(f"{x['title']} {x['summary']}")][:25]
    if not cand:
        return
    checked += [x["link"] for x in cand]
    del checked[:-800]
    known = [x.get("title") for x in (state.get("digest_events") or {}).get("events", [])]
    known += state.get("urgent_titles", [])
    known = [k for k in known if k]
    try:
        picks = claude(URGENT_PROMPT.format(
            family=cfg["family"], child=cfg["child"], home=cfg["home_city"], today=today,
            known="; ".join(known[-40:]) or "—",
            items="\n".join(f"[{i}] ({x['source']}, {x['pub']}) {x['title']}. {x['summary']}"
                            for i, x in enumerate(cand, 1))), state, cfg, max_tokens=300)
    except BudgetExceeded:
        return
    cats = table_of("categories.txt", ["code", "emoji", "label", "about"])
    emoji = {c["code"]: c["emoji"] for c in cats}
    known_norm = {_norm(k) for k in known}
    for p in picks:
        try:
            n = int(p.get("n"))
        except (TypeError, ValueError):
            continue
        if uw["n"] >= limit or not 1 <= n <= len(cand):
            continue
        item = cand[n - 1]
        text = article_text(item["link"]) or f"{item['title']}. {item['summary']}"
        src = [{"name": item["source"], "link": item["link"],
                "text": f"[опубликовано {item['pub']}] {item['title']}. {text}"}]
        end = today + dt.timedelta(days=30)
        try:
            evs = find_events(src, state, cfg, cats, today, today, end)
        except BudgetExceeded:
            return
        evs = [e for e in evs if e.get("fit") != "maybe" and not too_old(e, cfg["max_age_ok"])
               and _norm(e.get("title")) not in known_norm]
        if not evs:
            continue
        e = evs[0]
        e["_drive"] = drive_minutes(e)
        proofread([(e, ["title", "about", "summary", "city"])], state, cfg)
        send_card(e, emoji, state, today, end, today.isoformat(), notify=True,
                  prefix=f"⚡ <b>ТОЛЬКО ЧТО ОБЪЯВИЛИ</b>\n<i>не стал ждать "
                         f"{WD_GEN[cfg['digest_weekday']]} — это стоит запланировать</i>\n\n")
        uw["n"] += 1
        state.setdefault("urgent_titles", []).append(e.get("title"))
        state["urgent_titles"] = state["urgent_titles"][-60:]
        known_norm.add(_norm(e.get("title")))


def main():
    cfg = load_settings()
    now = dt.datetime.now(TZ)
    today = now.date()
    state = load_state()
    if state.get("chat_id"):
        CHAT["id"] = state["chat_id"]
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

    for step in (process_updates, collect_portals):
        try:
            step(state)
        except Exception as e:
            print(f"[{step.__name__}] сбой:", e)

    due = (today.weekday() == cfg["digest_weekday"] and now.hour >= cfg["digest_hour"]
           and state["last_digest"] != today.isoformat())
    if due or FORCE_DIGEST:
        try:
            bot_is_admin(state)
        except Exception as e:
            print("Проверка прав бота:", e)
        run_digest(state, cfg, today)
        state["last_digest"] = today.isoformat()
    else:
        print(f"Сегодня не день сводки (сводка: {WD_FULL[cfg['digest_weekday']]}, "
              f"{cfg['digest_hour']}:00).")
        for step in (weekend_reminder, urgent_check):
            try:
                step(state, cfg, now)
            except Exception as e:
                print(f"[{step.__name__}] сбой:", e)

    cutoff = (today - dt.timedelta(days=45)).isoformat()
    state["cards"] = {k: v for k, v in state.get("cards", {}).items() if v.get("dg", "") >= cutoff}
    if CHAT["migrated"]:
        state["chat_id"] = CHAT["id"]
        tg(f"ℹ️ Telegram превратил группу в супергруппу, и у неё новый номер: "
           f"<code>{CHAT['id']}</code>. Бот уже пишет по новому номеру; при случае обновите "
           f"секрет TELEGRAM_CHAT_ID в GitHub.")
    save_state(state)


if __name__ == "__main__":
    main()
