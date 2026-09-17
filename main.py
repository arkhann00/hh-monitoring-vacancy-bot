#!/usr/bin/env python3
"""Monitor an HH saved-search URL and publish newly found vacancies to Telegram.

Run continuously:
    python3 hh_telegram_monitor.py
Test one check:
    python3 hh_telegram_monitor.py --once
Find a group id after adding the bot and sending /chatid in the group:
    python3 hh_telegram_monitor.py --telegram-updates
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parent
# Docker sets STATE_FILE=/data/state.json, mounted as a persistent volume.
STATE_FILE = Path(os.environ.get("STATE_FILE", str(ROOT / "state.json")))
ENV_FILE = ROOT / ".env"
USER_AGENT = "HHVacancyMonitor/1.0 (personal vacancy alerts)"
CLIENT_ONLY_HH_PARAMETERS = {
    "ored_clusters",
    "hhtmFrom",
    "hhtmFromLabel",
    "enable_snippets",
    "L_save_area",
}


def load_env(path: Path) -> None:
    """Load simple KEY=value pairs without requiring python-dotenv."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value or "replace_me" in value:
        raise ValueError(f"Set {name} in {ENV_FILE.name}.")
    return value


def bool_env(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as error:
        raise ValueError(f"{name} must be a whole number.") from error
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}.")
    return value


def request_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> Any:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    if extra_headers:
        headers.update(extra_headers)
    request = Request(url, data=data, method=method, headers={
        **headers,
    })
    safe_url = re.sub(r"(https://api\.telegram\.org/bot)[^/]+", r"\1***", url)
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"HTTP {error.code} from {safe_url}: {details}") from error
    except URLError as error:
        raise RuntimeError(f"Network error while calling {safe_url}: {error.reason}") from error


def hh_rss_url(search_url: str) -> str:
    parsed = urlparse(search_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc.endswith("hh.ru"):
        raise ValueError("HH_SEARCH_URL must be a complete https://hh.ru/search/vacancy?... URL.")

    # HH keeps a public RSS feed for every regular vacancy search. It avoids the
    # protected API endpoint, which no longer supports applicant applications.
    if not parsed.path.startswith("/search/vacancy"):
        raise ValueError("HH_SEARCH_URL must point to hh.ru/search/vacancy.")
    parameters = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key not in CLIENT_ONLY_HH_PARAMETERS | {"page"}
    ]
    parameter_names = {key for key, _ in parameters}
    if "order_by" not in parameter_names:
        parameters.append(("order_by", "publication_time"))
    return f"{parsed.scheme}://{parsed.netloc}/search/vacancy/rss?{urlencode(parameters, doseq=True)}"


def strip_html(value: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]*>", " ", value)).split())


def get_vacancies(search_url: str) -> list[dict[str, Any]]:
    url = hh_rss_url(search_url)
    request = Request(url, headers={"Accept": "application/rss+xml, application/xml", "User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=30) as response:
            root = ElementTree.fromstring(response.read())
    except HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"HH RSS returned HTTP {error.code}: {details}") from error
    except (URLError, ElementTree.ParseError) as error:
        raise RuntimeError(f"Could not read HH RSS: {error}") from error

    vacancies: list[dict[str, Any]] = []
    for item in root.findall("./channel/item"):
        link = item.findtext("link", default="").strip()
        identifier = item.findtext("guid", default="").strip() or link
        if not identifier or not link:
            continue
        vacancies.append({
            "id": identifier,
            "name": strip_html(item.findtext("title", default="Без названия")),
            "alternate_url": link,
            "published_at": item.findtext("pubDate", default=""),
            "rss_description": strip_html(item.findtext("description", default="")),
        })
    return vacancies


def read_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"seen_ids": []}
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Cannot read {STATE_FILE.name}: {error}") from error
    if not isinstance(state, dict) or not isinstance(state.get("seen_ids", []), list):
        raise RuntimeError(f"{STATE_FILE.name} has an invalid format.")
    return state


def write_state(seen_ids: set[str]) -> None:
    # Retaining recent IDs avoids repeat notifications while keeping state small.
    state = {
        "seen_ids": sorted(seen_ids)[-5000:],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(STATE_FILE)


def money_text(salary: dict[str, Any] | None) -> str:
    if not salary:
        return "зарплата не указана"
    amount_from, amount_to, currency = salary.get("from"), salary.get("to"), salary.get("currency", "")
    if amount_from is not None and amount_to is not None:
        return f"{amount_from:,}–{amount_to:,} {currency}".replace(",", " ")
    if amount_from is not None:
        return f"от {amount_from:,} {currency}".replace(",", " ")
    if amount_to is not None:
        return f"до {amount_to:,} {currency}".replace(",", " ")
    return "зарплата не указана"


def vacancy_message(vacancy: dict[str, Any]) -> str:
    title = html.escape(str(vacancy.get("name", "Без названия")))
    if "rss_description" in vacancy:
        details = html.escape(str(vacancy["rss_description"])[:900])
        url = html.escape(str(vacancy.get("alternate_url", "")), quote=True)
        published = html.escape(str(vacancy.get("published_at", "")))
        return f"<b>{title}</b>\n{details}\nОпубликовано: {published}\n<a href=\"{url}\">Открыть вакансию на HH</a>"
    employer = html.escape(str((vacancy.get("employer") or {}).get("name", "Работодатель не указан")))
    area = html.escape(str((vacancy.get("area") or {}).get("name", "Локация не указана")))
    salary = html.escape(money_text(vacancy.get("salary")))
    url = html.escape(str(vacancy.get("alternate_url", "")), quote=True)
    published = html.escape(str(vacancy.get("published_at", "")).replace("T", " ").replace("+0300", ""))
    return f"<b>{title}</b>\n{employer}\n{area} · {salary}\nОпубликовано: {published}\n<a href=\"{url}\">Открыть вакансию на HH</a>"


def telegram_call(token: str, method: str, payload: dict[str, Any]) -> Any:
    response = request_json(f"https://api.telegram.org/bot{token}/{method}", method="POST", payload=payload)
    if not isinstance(response, dict) or not response.get("ok"):
        description = response.get("description", "unknown Telegram error") if isinstance(response, dict) else str(response)
        raise RuntimeError(f"Telegram {method} failed: {description}")
    return response["result"]


def send_messages(token: str, chat_id: str, vacancies: list[dict[str, Any]], batch: bool) -> None:
    messages: list[str]
    if batch:
        heading = "<b>Новые вакансии HH</b>"
        messages, current = [], heading
        for vacancy in vacancies:
            item = vacancy_message(vacancy)
            # Add whole vacancy blocks only, so HTML tags and links never get cut.
            if len(item) > 3900:
                if current != heading:
                    messages.append(current)
                messages.append(item)
                current = heading
            elif len(current) + len(item) + 2 > 4000 and current != heading:
                messages.append(current)
                current = heading + "\n\n" + item
            else:
                current += "\n\n" + item
        if current != heading:
            messages.append(current)
    else:
        messages = [vacancy_message(item) for item in vacancies]
    for message in messages:
        telegram_call(token, "sendMessage", {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })


def check_once(
    token: str,
    chat_id: str,
    search_url: str,
    page_size: int,
    send_existing: bool,
    batch: bool,
) -> int:
    vacancies = get_vacancies(search_url)
    state = read_state()
    known = {str(identifier) for identifier in state.get("seen_ids", [])}
    ids = {str(item["id"]) for item in vacancies if item.get("id") is not None}

    if not known:
        write_state(ids)
        if send_existing and vacancies:
            send_messages(token, chat_id, list(reversed(vacancies)), batch)
            logging.info("First run: sent %d existing vacancies.", len(vacancies))
            return len(vacancies)
        logging.info("First run: recorded %d vacancies; nothing sent.", len(ids))
        return 0

    new_vacancies = [item for item in vacancies if str(item.get("id")) not in known]
    # API returns newest first; chronological delivery reads better in Telegram.
    new_vacancies.reverse()
    if new_vacancies:
        send_messages(token, chat_id, new_vacancies, batch)
    write_state(known | ids)
    logging.info("Checked %d vacancies, sent %d new.", len(vacancies), len(new_vacancies))
    return len(new_vacancies)


def print_group_updates(token: str) -> None:
    updates = telegram_call(token, "getUpdates", {"timeout": 0, "allowed_updates": ["message"]})
    for update in updates:
        message = update.get("message", {})
        chat = message.get("chat", {})
        if chat.get("type") in {"group", "supergroup"}:
            print(f"{chat.get('title', 'Без названия')}: {chat.get('id')}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="perform one check and exit")
    parser.add_argument("--telegram-updates", action="store_true", help="print group chat IDs from recent bot updates")
    arguments = parser.parse_args()

    load_env(ENV_FILE)
    try:
        token = require_env("TELEGRAM_BOT_TOKEN")
        if arguments.telegram_updates:
            print_group_updates(token)
            return 0
        chat_id = require_env("TELEGRAM_CHAT_ID")
        search_url = require_env("HH_SEARCH_URL")
        interval = int_env("POLL_INTERVAL_SECONDS", 300, 60, 86400)
        page_size = int_env("HH_PAGE_SIZE", 50, 1, 100)
        send_existing = bool_env("SEND_EXISTING_ON_START")
        batch = bool_env("BATCH_MESSAGES")
    except ValueError as error:
        logging.error(error)
        return 2

    while True:
        try:
            check_once(
                token,
                chat_id,
                search_url,
                page_size,
                send_existing,
                batch,
            )
        except Exception as error:  # Keep a long-running bot alive after temporary failures.
            logging.exception("Check failed: %s", error)
            if arguments.once:
                return 1
        if arguments.once:
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(main())
