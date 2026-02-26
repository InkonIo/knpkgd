"""
efinance KNP Telegram Bot
==========================
Многопользовательский бот для мониторинга документов на lk.efinance.gov.kz

Команды:
  /start        — регистрация (ввод ИИН и пароля)
  /status       — текущие статусы документов
  /docs         — получить PDF документы прямо сейчас
  /stop         — отключить уведомления

Команды админа:
  /users        — список пользователей
  /removeuser   — удалить пользователя
  /broadcast    — отправить сообщение всем
  /admin        — панель управления

Запуск локально:
  pip install requests playwright python-dotenv
  playwright install chromium
  python bot.py
"""

from dotenv import load_dotenv
load_dotenv()

import requests
import json
import os
import time
import logging
import threading
from datetime import datetime
from playwright.sync_api import sync_playwright

from const import BOT_TOKEN, ADMIN_CHAT_ID, CHECK_INTERVAL, USERS_FILE, STATE_FILE

# ============================================================
# ЛОГИРОВАНИЕ
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8")
    ]
)
log = logging.getLogger(__name__)

# ============================================================
# ХРАНИЛИЩЕ ПОЛЬЗОВАТЕЛЕЙ
# ============================================================

def load_users() -> dict:
    if not os.path.exists(USERS_FILE):
        return {}
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_users(users: dict):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

# Временное хранилище состояния диалога
dialog_state = {}  # chat_id → {"step": "await_iin" | "await_password", "iin": ...}

# ============================================================
# TELEGRAM API
# ============================================================

def tg(method: str, _read_timeout: int = 15, **kwargs) -> dict:
    """
    Обёртка над Telegram Bot API.
    _read_timeout — таймаут чтения ответа (для getUpdates нужно больше чем timeout=).
    """
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
            json=kwargs,
            timeout=(10, _read_timeout)  # (connect_timeout, read_timeout)
        )
        return r.json()
    except Exception as e:
        log.error(f"Telegram API error [{method}]: {e}")
        return {}

def send(chat_id, text, reply_markup=None, parse_mode="HTML"):
    kwargs = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        kwargs["reply_markup"] = reply_markup
    tg("sendMessage", **kwargs)

def send_file(chat_id, content: bytes, filename: str, caption: str = ""):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
        requests.post(url, data={
            "chat_id": chat_id,
            "caption": caption,
            "parse_mode": "HTML"
        }, files={"document": (filename, content, "application/pdf")}, timeout=60)
    except Exception as e:
        log.error(f"Ошибка отправки файла: {e}")

def is_admin(chat_id: str) -> bool:
    return str(chat_id) == str(ADMIN_CHAT_ID)

# ============================================================
# КНОПКИ — Bot API 9.4 (поле style для цвета)
# ============================================================

def btn(text, data, style=None):
    """
    Инлайн кнопка.
    style: None | "blue" | "red" | "green"  — Bot API 9.4
    """
    b = {"text": text, "callback_data": data}
    if style:
        b["style"] = style
    return b

def main_keyboard():
    return {
        "inline_keyboard": [
            [
                btn("📄 Мои документы", "docs",   style="blue"),
                btn("🔄 Статусы",        "status", style="green"),
            ],
            [
                btn("⚙️ Настройки",             "settings"),
                btn("❌ Отключить уведомления",  "stop", style="red"),
            ]
        ]
    }

def admin_keyboard():
    return {
        "inline_keyboard": [
            [
                btn("👥 Пользователи", "admin_users",     style="blue"),
                btn("📢 Рассылка",     "admin_broadcast", style="green"),
            ],
            [
                btn("➕ Добавить юзера", "admin_adduser", style="blue"),
            ]
        ]
    }

# ============================================================
# АВТОРИЗАЦИЯ ЧЕРЕЗ PLAYWRIGHT
# ============================================================

def login(iin: str, password: str) -> dict | None:
    log.info(f"Авторизация для ИИН {iin[:4]}****...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        try:
            page.goto("https://lk.efinance.gov.kz/", timeout=30000)
            page.wait_for_load_state("networkidle", timeout=15000)
            page.fill("#username", iin)
            page.fill("#password", password)
            page.click("#login-btn")
            page.wait_for_url("**/lk.efinance.gov.kz/**", timeout=15000)
            page.wait_for_load_state("networkidle", timeout=15000)
            page.goto("https://lk.efinance.gov.kz/isna/myDocuments/", timeout=30000)
            page.wait_for_load_state("networkidle", timeout=15000)
            cookies = {c["name"]: c["value"] for c in context.cookies()}
            if "session" not in cookies:
                return None
            log.info(f"Авторизация успешна для {iin[:4]}****")
            return cookies
        except Exception as e:
            log.error(f"Ошибка авторизации {iin[:4]}****: {e}")
            return None
        finally:
            browser.close()

# ============================================================
# API ЗАПРОСЫ
# ============================================================

def make_session(cookies: dict) -> requests.Session:
    session = requests.Session()
    session.cookies.update(cookies)
    session.headers.update({
        "Accept":        "application/json, text/plain, */*",
        "Content-Type":  "application/json",
        "Origin":        "https://lk.efinance.gov.kz",
        "Referer":       "https://lk.efinance.gov.kz/isna/myDocuments/",
        "x-tenant-id":   "isna",
        "X-XSRF-TOKEN":  cookies.get("XSRF-TOKEN", ""),
        "Language":      "RU",
        "User-Agent":    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
    })
    return session

def get_documents(cookies: dict) -> list | None:
    session = make_session(cookies)
    all_docs = []
    endpoints = [
        {
            "url":    "https://lk.efinance.gov.kz/services/isnaknpdocs/api/filter-sent-documents",
            "params": {"page": 0, "size": 20, "sort": "submissionDate,desc"},
            "body":   {
                "documentType": "NZ", "freeText": "", "declarationTypeCodes": [],
                "periodType": None, "periodValue": None, "fnoYear": None,
                "formCodes": [], "statusCodes": [], "submissionDateFrom": None,
                "submissionDateTo": None, "taxOrgCode": None, "taxpayerCode": None
            },
            "type": "NZ",
        },
        {
            "url":    "https://lk.efinance.gov.kz/services/isnaknpdocs/api/v2/filter-sent-documents",
            "params": {"page": 0, "size": 20, "sort": "submissionDate,desc"},
            "body":   {
                "documentType": "FNO", "freeText": "", "declarationTypeCodes": [],
                "periodType": None, "periodValue": None, "fnoYear": None,
                "formCodes": [], "statusCodes": [], "submissionDateFrom": None,
                "submissionDateTo": None, "taxOrgCode": None, "taxpayerCode": None
            },
            "type": "FNO",
        },
    ]
    for ep in endpoints:
        try:
            r = session.post(ep["url"], params=ep["params"], json=ep["body"], timeout=15)
            if r.status_code == 401:
                return None
            if r.status_code != 200:
                continue
            data = r.json()
            items = data if isinstance(data, list) else data.get("payload", {}).get("results", [])
            for doc in items:
                doc["_type"] = ep["type"]
            all_docs.extend(items)
        except Exception as e:
            log.error(f"Ошибка get_documents: {e}")
    return all_docs

def get_pdf_files(cookies: dict, doc_id: int, doc_type: str) -> list:
    try:
        session = make_session(cookies)
        url = f"https://lk.efinance.gov.kz/services/isnaknpdocs/api/v2/output-forms/{doc_type}/{doc_id}"
        r = session.get(url, timeout=15)
        if r.status_code != 200:
            return []
        return [f for f in r.json() if f.get("fileUid")]
    except Exception as e:
        log.error(f"Ошибка get_pdf_files: {e}")
        return []

def download_pdf(cookies: dict, doc_id: int, doc_type: str, file_type: str) -> bytes | None:
    try:
        session = make_session(cookies)
        url = f"https://lk.efinance.gov.kz/services/isnaknpdocs/api/v2/get-output-forms/{doc_type.lower()}/{doc_id}"
        r = session.get(url, params={"fileType": file_type}, timeout=30)
        if r.status_code == 200 and len(r.content) > 100:
            return r.content
        return None
    except Exception as e:
        log.error(f"Ошибка download_pdf: {e}")
        return None

def send_docs_to_user(chat_id: str, cookies: dict, docs: list):
    """Отправляет все PDF документов пользователю."""
    if not docs:
        send(chat_id, "📭 Документов не найдено.")
        return

    for doc in docs:
        doc_id    = doc.get("id")
        doc_type  = doc.get("_type", "NZ")
        name_ru   = doc.get("documentNameRu") or doc.get("documentTypeRu") or "Документ"
        status_ru = doc.get("statusName", {}).get("nameRu", doc.get("statusCode", ""))
        form_code = doc.get("formCode", "")
        sub_date  = doc.get("submissionDate") or doc.get("submissionInstant", "")

        send(chat_id,
            f"📄 <b>{name_ru}</b>\n"
            f"Форма: {form_code}\n"
            f"Статус: <b>{status_ru}</b>\n"
            f"Дата: {sub_date}"
        )

        pdf_files = get_pdf_files(cookies, doc_id, doc_type)
        for pdf in pdf_files:
            content = download_pdf(cookies, doc_id, doc_type, pdf.get("fileType", "PDF"))
            if content:
                send_file(chat_id, content,
                          pdf.get("fileName", "document.pdf"),
                          f"📎 {pdf.get('fileName', 'document.pdf')}")

# ============================================================
# ОБРАБОТКА КОМАНД
# ============================================================

def handle_start(chat_id: str, username: str):
    users = load_users()
    if str(chat_id) in users:
        send(chat_id,
            "👋 С возвращением!\n\nВы уже зарегистрированы.",
            reply_markup=main_keyboard()
        )
        return

    dialog_state[chat_id] = {"step": "await_iin"}
    send(chat_id,
        "👋 <b>Добро пожаловать в KNP Monitor!</b>\n\n"
        "Я буду следить за вашими налоговыми документами и уведомлять об изменениях статуса.\n\n"
        "Для начала введите ваш <b>ИИН</b>:"
    )

def handle_status(chat_id: str):
    users = load_users()
    user = users.get(str(chat_id))
    if not user:
        send(chat_id, "⚠️ Сначала пройдите регистрацию: /start")
        return

    send(chat_id, "⏳ Проверяю статусы...")
    cookies = login(user["iin"], user["password"])
    if not cookies:
        send(chat_id, "❌ Не удалось войти. Проверьте ИИН и пароль (/start)")
        return

    docs = get_documents(cookies)
    if not docs:
        send(chat_id, "📭 Документов не найдено.")
        return

    text = "📊 <b>Текущие статусы:</b>\n\n"
    for doc in docs:
        name_ru   = doc.get("documentNameRu") or doc.get("documentTypeRu") or "Документ"
        status_ru = doc.get("statusName", {}).get("nameRu", "")
        form_code = doc.get("formCode", "")
        sub_date  = doc.get("submissionDate") or doc.get("submissionInstant", "")
        text += f"• <b>{form_code}</b> {name_ru}\n  Статус: <b>{status_ru}</b>\n  Дата: {sub_date}\n\n"

    send(chat_id, text, reply_markup=main_keyboard())

def handle_docs(chat_id: str):
    users = load_users()
    user = users.get(str(chat_id))
    if not user:
        send(chat_id, "⚠️ Сначала пройдите регистрацию: /start")
        return

    send(chat_id, "⏳ Загружаю документы...")
    cookies = login(user["iin"], user["password"])
    if not cookies:
        send(chat_id, "❌ Не удалось войти. Проверьте ИИН и пароль (/start)")
        return

    docs = get_documents(cookies)
    send_docs_to_user(chat_id, cookies, docs)

def handle_stop(chat_id: str):
    users = load_users()
    if str(chat_id) in users:
        users[str(chat_id)]["notifications"] = False
        save_users(users)
    send(chat_id, "🔕 Уведомления отключены.\nВключить снова: /start")

def handle_admin_users(chat_id: str):
    if not is_admin(chat_id):
        return
    users = load_users()
    if not users:
        send(chat_id, "👥 Нет зарегистрированных пользователей.")
        return
    text = "👥 <b>Пользователи:</b>\n\n"
    for uid, u in users.items():
        notif = "🔔" if u.get("notifications", True) else "🔕"
        text += f"{notif} <code>{uid}</code> — ИИН: {u['iin'][:4]}****\n"
    send(chat_id, text, reply_markup=admin_keyboard())

def handle_admin_removeuser(chat_id: str, target_id: str):
    if not is_admin(chat_id):
        return
    users = load_users()
    if target_id in users:
        del users[target_id]
        save_users(users)
        send(chat_id, f"✅ Пользователь {target_id} удалён.")
    else:
        send(chat_id, f"❌ Пользователь {target_id} не найден.")

def handle_text(chat_id: str, text: str):
    """Обрабатывает текстовый ввод (ИИН и пароль при регистрации)."""
    state = dialog_state.get(chat_id, {})
    step  = state.get("step")

    if step == "await_iin":
        if not text.isdigit() or len(text) != 12:
            send(chat_id, "❌ ИИН должен содержать 12 цифр. Попробуйте снова:")
            return
        dialog_state[chat_id] = {"step": "await_password", "iin": text}
        send(chat_id, "🔑 Введите ваш <b>пароль</b> от lk.efinance.gov.kz:")

    elif step == "await_password":
        iin      = state.get("iin")
        password = text
        dialog_state.pop(chat_id, None)

        send(chat_id, "⏳ Проверяю данные...")
        cookies = login(iin, password)

        if not cookies:
            send(chat_id, "❌ Не удалось войти. Проверьте ИИН и пароль и попробуйте снова: /start")
            return

        users = load_users()
        users[str(chat_id)] = {
            "iin":           iin,
            "password":      password,
            "notifications": True,
            "added_at":      datetime.now().isoformat(),
        }
        save_users(users)

        send(chat_id,
            "✅ <b>Вход выполнен успешно!</b>\n\n"
            "Я буду проверять ваши документы каждые 30 минут и уведомлять об изменениях.\n\n"
            "Используйте кнопки ниже:",
            reply_markup=main_keyboard()
        )

        # Показываем документы сразу после регистрации
        docs = get_documents(cookies)
        if docs:
            send_docs_to_user(chat_id, cookies, docs)

def handle_callback(chat_id: str, callback_data: str, message_id: int):
    if callback_data == "docs":
        handle_docs(chat_id)
    elif callback_data == "status":
        handle_status(chat_id)
    elif callback_data == "stop":
        handle_stop(chat_id)
    elif callback_data == "settings":
        send(chat_id,
            "⚙️ <b>Настройки</b>\n\n"
            "Для смены ИИН/пароля введите /start заново.\n"
            "Для отключения уведомлений нажмите кнопку ниже.",
            reply_markup=main_keyboard()
        )
    elif callback_data == "admin_users":
        handle_admin_users(chat_id)
    elif callback_data == "admin_adduser":
        if is_admin(chat_id):
            dialog_state[chat_id] = {"step": "await_iin"}
            send(chat_id, "➕ Введите ИИН нового пользователя:")
    elif callback_data == "admin_broadcast":
        if is_admin(chat_id):
            dialog_state[chat_id] = {"step": "await_broadcast"}
            send(chat_id, "📢 Введите сообщение для рассылки всем пользователям:")

# ============================================================
# МОНИТОРИНГ (фоновый поток)
# ============================================================

def monitor_loop():
    """Фоновый поток — проверяет документы всех пользователей."""
    log.info("Монитор запущен.")
    while True:
        try:
            users = load_users()
            state = load_state()

            for chat_id, user in users.items():
                if not user.get("notifications", True):
                    continue

                try:
                    cookies = login(user["iin"], user["password"])
                    if not cookies:
                        log.warning(f"Не удалось войти для {user['iin'][:4]}****")
                        continue

                    docs = get_documents(cookies)
                    if docs is None:
                        continue

                    user_state = state.setdefault(chat_id, {})
                    now = datetime.now().strftime("%d.%m.%Y %H:%M")

                    for doc in docs:
                        doc_id    = str(doc.get("id", ""))
                        doc_type  = doc.get("_type", "NZ")
                        name_ru   = doc.get("documentNameRu") or doc.get("documentTypeRu") or "Документ"
                        status    = doc.get("statusCode", "")
                        status_ru = doc.get("statusName", {}).get("nameRu", status)
                        form_code = doc.get("formCode", "")

                        if not doc_id:
                            continue

                        prev_status = user_state.get(doc_id)

                        if prev_status is None:
                            user_state[doc_id] = status

                        elif prev_status != status:
                            log.info(f"[{chat_id}] Смена статуса {name_ru}: {prev_status} → {status}")

                            send(chat_id,
                                f"🔄 <b>Смена статуса!</b>\n\n"
                                f"📋 Форма: {form_code}\n"
                                f"📄 {name_ru}\n"
                                f"📌 Статус: {prev_status} → <b>{status_ru}</b>\n"
                                f"🕐 Время: {now}"
                            )

                            pdf_files = get_pdf_files(cookies, int(doc_id), doc_type)
                            for pdf in pdf_files:
                                content = download_pdf(cookies, int(doc_id), doc_type, pdf.get("fileType", "PDF"))
                                if content:
                                    send_file(chat_id, content,
                                             pdf.get("fileName", "document.pdf"),
                                             f"📎 {pdf.get('fileName', 'document.pdf')}")

                            user_state[doc_id] = status

                except Exception as e:
                    log.error(f"Ошибка мониторинга для {chat_id}: {e}")

            save_state(state)

        except Exception as e:
            log.error(f"Ошибка в monitor_loop: {e}")

        log.info(f"Следующая проверка через {CHECK_INTERVAL // 60} мин.")
        time.sleep(CHECK_INTERVAL)

# ============================================================
# POLLING
# ============================================================

def polling_loop():
    """Основной поток — получает сообщения от пользователей."""
    offset = 0
    log.info("Polling запущен.")

    while True:
        try:
            result = tg("getUpdates", _read_timeout=40, offset=offset, timeout=30)
            updates = result.get("result", [])

            for update in updates:
                offset = update["update_id"] + 1

                if "callback_query" in update:
                    cq      = update["callback_query"]
                    chat_id = str(cq["message"]["chat"]["id"])
                    data    = cq.get("data", "")
                    msg_id  = cq["message"]["message_id"]
                    tg("answerCallbackQuery", callback_query_id=cq["id"])
                    handle_callback(chat_id, data, msg_id)
                    continue

                if "message" not in update:
                    continue

                msg     = update["message"]
                chat_id = str(msg["chat"]["id"])
                text    = msg.get("text", "").strip()

                if not text:
                    continue

                log.info(f"[{chat_id}] {text[:50]}")

                if text == "/start":
                    handle_start(chat_id, msg.get("from", {}).get("username", ""))
                elif text == "/status":
                    handle_status(chat_id)
                elif text == "/docs":
                    handle_docs(chat_id)
                elif text == "/stop":
                    handle_stop(chat_id)
                elif text == "/users" and is_admin(chat_id):
                    handle_admin_users(chat_id)
                elif text.startswith("/removeuser ") and is_admin(chat_id):
                    handle_admin_removeuser(chat_id, text.split(" ", 1)[1].strip())
                elif text == "/admin" and is_admin(chat_id):
                    send(chat_id, "👑 <b>Админ панель</b>", reply_markup=admin_keyboard())
                else:
                    state = dialog_state.get(chat_id, {})
                    if state.get("step") == "await_broadcast" and is_admin(chat_id):
                        dialog_state.pop(chat_id, None)
                        users = load_users()
                        count = 0
                        for uid in users:
                            send(uid, f"📢 <b>Сообщение от администратора:</b>\n\n{text}")
                            count += 1
                        send(chat_id, f"✅ Отправлено {count} пользователям.")
                    elif state.get("step"):
                        handle_text(chat_id, text)

        except Exception as e:
            log.error(f"Ошибка polling: {e}")
            time.sleep(5)

# ============================================================
# ЗАПУСК
# ============================================================

if __name__ == "__main__":
    log.info("KNP Bot запущен.")

    tg("sendMessage", chat_id=ADMIN_CHAT_ID, text="✅ <b>KNP Bot запущен!</b>", parse_mode="HTML")

    monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
    monitor_thread.start()

    polling_loop()