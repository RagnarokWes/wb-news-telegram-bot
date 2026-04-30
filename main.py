import os
import json
import time
import html
import re
from pathlib import Path
from datetime import datetime, timedelta, timezone

import requests


WB_NEWS_URL = "https://common-api.wildberries.ru/api/communications/v2/news"
STATE_FILE = Path("state.json")

MSK = timezone(timedelta(hours=3))


def get_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Не найдена переменная окружения: {name}")
    return value


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def clean_text(text: str) -> str:
    if not text:
        return ""

    text = str(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def shorten(text: str, limit: int = 700) -> str:
    text = clean_text(text)
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def parse_wb_date(date_text: str):
    if not date_text:
        return None

    # WB обычно отдаёт дату вида: 2025-02-05T14:10:35+03:00
    date_text = date_text.replace("Z", "+00:00")

    try:
        dt = datetime.fromisoformat(date_text)
    except ValueError:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MSK)

    return dt.astimezone(MSK)


def fetch_wb_news(wb_token: str, from_date: str) -> list:
    headers = {
        "Authorization": wb_token,
    }

    params = {
        "from": from_date,
    }

    response = requests.get(
        WB_NEWS_URL,
        headers=headers,
        params=params,
        timeout=30
    )

    if response.status_code == 401:
        raise RuntimeError("WB API вернул 401. Проверь WB_TOKEN.")

    if response.status_code == 429:
        raise RuntimeError("WB API вернул 429. Слишком много запросов.")

    response.raise_for_status()

    data = response.json()
    return data.get("data", [])


def filter_news_by_date(news: list, target_date) -> list:
    result = []

    for item in news:
        dt = parse_wb_date(item.get("date"))
        if not dt:
            continue

        if dt.date() == target_date:
            result.append(item)

    result.sort(key=lambda x: parse_wb_date(x.get("date")) or datetime.min.replace(tzinfo=MSK))
    return result


def build_telegram_messages(news_items: list, target_date) -> list:
    date_ru = target_date.strftime("%d.%m.%Y")

    title = f"<b>🟣 Новости Wildberries за {date_ru}</b>"
    blocks = []

    for index, item in enumerate(news_items, start=1):
        header = html.escape(clean_text(item.get("header", "Без заголовка")))
        news_id = item.get("id")

        if news_id:
            news_link = f"https://seller.wildberries.ru/news-v2/news-details?id={news_id}"
            block = f'<b>{index}. {header}</b>\n<a href="{news_link}">Открыть новость</a>'
        else:
            block = f"<b>{index}. {header}</b>"

        blocks.append(block)

    messages = []
    current = title

    for block in blocks:
        addition = "\n\n" + block

        if len(current) + len(addition) > 3800:
            messages.append(current)
            current = title + "\n\n" + block
        else:
            current += addition

    messages.append(current)
    return messages

def send_telegram_message(bot_token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "disable_notification": True,
    }

    response = requests.post(url, json=payload, timeout=30)

    if response.status_code == 401:
        raise RuntimeError("Telegram вернул 401. Проверь TELEGRAM_BOT_TOKEN.")

    if response.status_code == 400:
        raise RuntimeError(f"Telegram вернул 400. Ответ: {response.text}")

    response.raise_for_status()


def main():
    wb_token = get_env("WB_TOKEN")
    telegram_bot_token = get_env("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = get_env("TELEGRAM_CHAT_ID")

    # По умолчанию отправляем новости за вчера.
    # Это надёжнее, чем новости за сегодня: день уже закончился, список стабильнее.
    days_ago = int(os.getenv("NEWS_DAYS_AGO", "1"))

    now_msk = datetime.now(MSK)
    target_date = now_msk.date() - timedelta(days=days_ago)
    target_date_text = target_date.isoformat()

    state = load_state()

    if state.get("last_sent_date") == target_date_text:
        print(f"Новости за {target_date_text} уже были обработаны. Повторной отправки не будет.")
        return

    print(f"Запрашиваем новости WB начиная с даты: {target_date_text}")

    all_news = fetch_wb_news(wb_token, target_date_text)
    target_news = filter_news_by_date(all_news, target_date)

    print(f"Найдено новостей за {target_date_text}: {len(target_news)}")

    if target_news:
        messages = build_telegram_messages(target_news, target_date)

        for message in messages:
            send_telegram_message(
                telegram_bot_token,
                telegram_chat_id,
                message
            )
            time.sleep(1)

        print("Новости успешно отправлены в Telegram.")
    else:
        print("Новостей за эту дату нет. Сообщение в Telegram не отправляем.")

    # Даже если новостей нет, дату отмечаем как обработанную,
    # чтобы при повторном запуске в тот же день бот не делал лишнюю работу.
    state["last_sent_date"] = target_date_text
    state["last_run_at_msk"] = now_msk.isoformat()
    save_state(state)

    print("state.json обновлён.")


if __name__ == "__main__":
    main()
