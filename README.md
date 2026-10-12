# Афиша для малыша 👶

Еженедельная сводка семейных мероприятий по Латвии в Telegram: погода, события на выходные и неделю, бесплатное, ближайшие праздники и идеи постоянных мест. Работает на GitHub Actions, ИИ — Claude Haiku (около $0.10–0.15 в месяц, лимит $0.90).

## Файлы
| Файл | Что в нём | Менять? |
|---|---|---|
| `settings.txt` | возраст, регион, день и час сводки, погода, бюджет | да |
| `criteria.txt` | что подходит и что нет, обычным языком | да |
| `categories.txt` | категории событий и их значки | да |
| `sources.txt` | сайты с афишами и запросы в Google News | да |
| `places.txt` | постоянные места для блока «Куда ещё сходить» | да |
| `holidays.txt` | праздники для блока «Скоро» | да |
| `main.py` | сам скрипт | не нужно |
| `.github/workflows/digest.yml` | расписание запуска | не нужно |
| `state.json` | память бота, создаётся сам | нет |

## Быстрый старт
1. Telegram: @BotFather → `/newbot` → токен; напишите боту `/start`; chat_id — в `https://api.telegram.org/bot<ТОКЕН>/getUpdates`.
2. Claude: platform.claude.com → пополнить баланс → API Keys → Create Key.
3. GitHub: новый публичный репозиторий → загрузить все файлы → Settings → Secrets and variables → Actions → секреты `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
4. Actions → Family events digest → Run workflow — сводка придёт сразу.

Подробная пошаговая инструкция и примеры доработок — в документе «Афиша для малыша: инструкция».
