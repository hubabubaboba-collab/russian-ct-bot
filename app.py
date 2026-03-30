from flask import Flask, request
import requests
import threading
import json
import os
import time
import random
import sqlite3
import re

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DIFY_API_KEY = os.environ.get("DIFY_API_KEY")

# =============================================
# ЗАЩИТА ОТ СПАМА (в оперативке — это ок)
# =============================================
last_message_time = {}
processing = {}
MIN_DELAY = 2
spam_lock = threading.Lock()

# =============================================
# Максимальный возраст сообщения (в секундах)
# Если сообщение старше — игнорируем
# =============================================
MAX_MESSAGE_AGE = 60


# =============================================
# SQLite: БАЗА ДАННЫХ ДЛЯ CONVERSATION_ID
# =============================================
DB_PATH = "/opt/render/project/data/bot.db"

def init_db():
    """Создаём таблицу если её нет"""
    try:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    except Exception:
        pass

    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                chat_id TEXT PRIMARY KEY,
                conversation_id TEXT,
                updated_at REAL
            )
        """)
        conn.commit()
        conn.close()
        print(f"[DB] Инициализирована: {DB_PATH}")
    except Exception as e:
        print(f"[DB ERROR] Не удалось создать базу: {e}")


def get_conversation_id(chat_id):
    """Получить conversation_id из базы"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT conversation_id FROM conversations WHERE chat_id = ?",
            (str(chat_id),)
        )
        row = cursor.fetchone()
        conn.close()
        if row:
            return row[0]
        return ""
    except Exception as e:
        print(f"[DB ERROR] get: {e}")
        return ""


def save_conversation_id(chat_id, conversation_id):
    """Сохранить conversation_id в базу"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO conversations (chat_id, conversation_id, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(chat_id)
            DO UPDATE SET conversation_id = ?, updated_at = ?
        """, (
            str(chat_id),
            conversation_id,
            time.time(),
            conversation_id,
            time.time()
        ))
        conn.commit()
        conn.close()
        print(f"[DB] Сохранён conv_id для chat_id={chat_id}")
    except Exception as e:
        print(f"[DB ERROR] save: {e}")


def delete_conversation_id(chat_id):
    """Удалить conversation_id из базы (при /reset)"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM conversations WHERE chat_id = ?",
            (str(chat_id),)
        )
        conn.commit()
        conn.close()
        print(f"[DB] Удалён conv_id для chat_id={chat_id}")
    except Exception as e:
        print(f"[DB ERROR] delete: {e}")


# Инициализируем базу при старте
init_db()


# =============================================
# НОВОЕ: Очистка Markdown для fallback
# Убирает все звёздочки если Markdown сломался
# =============================================
def strip_markdown(text):
    """Убирает Markdown-разметку из текста"""
    # Убираем *** (жирный курсив)
    text = re.sub(r'\*\*\*(.+?)\*\*\*', r'\1', text)
    # Убираем ** (жирный)
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    # Убираем * (курсив)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    # Убираем __ (подчёркивание)
    text = re.sub(r'__(.+?)__', r'\1', text)
    # Убираем _ (курсив)
    text = re.sub(r'_(.+?)_', r'\1', text)
    # Убираем ``` (блоки кода)
    text = re.sub(r'```[\s\S]*?```', '', text)
    # Убираем ` (инлайн код)
    text = re.sub(r'`(.+?)`', r'\1', text)
    # Убираем # заголовки
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    return text


# =============================================
# ИЗМЕНЕНО: Отправка сообщения с Markdown + fallback
# =============================================
def send_telegram_message(chat_id, text):
    tg_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    # Попытка 1: отправляем с Markdown (красивый текст)
    tg_data = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    try:
        response = requests.post(tg_url, json=tg_data, timeout=10)
        result = response.json()
        if result.get("ok"):
            message_id = result["result"]["message_id"]
            print(f"[TG SEND] OK (Markdown), message_id={message_id}")
            return message_id
        else:
            # Markdown сломался — отправляем без форматирования
            print(f"[TG SEND] Markdown failed: {result.get('description', '')}")
            clean_text = strip_markdown(text)
            tg_data_plain = {"chat_id": chat_id, "text": clean_text}
            response2 = requests.post(tg_url, json=tg_data_plain, timeout=10)
            result2 = response2.json()
            if result2.get("ok"):
                message_id = result2["result"]["message_id"]
                print(f"[TG SEND] OK (plain fallback), message_id={message_id}")
                return message_id
            else:
                print(f"[TG SEND ERROR] {result2}")
                return None
    except Exception as e:
        print(f"[TG SEND EXCEPTION] {e}")
        return None


# =============================================
# ИЗМЕНЕНО: Редактирование сообщения с Markdown + fallback
# =============================================
def edit_telegram_message(chat_id, message_id, new_text):
    tg_url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"

    if len(new_text) <= 4096:
        # Попытка 1: с Markdown
        tg_data = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": new_text,
            "parse_mode": "Markdown"
        }
        try:
            response = requests.post(tg_url, json=tg_data, timeout=10)
            result = response.json()
            if result.get("ok"):
                print(f"[TG EDIT] OK (Markdown)")
            else:
                # Markdown сломался — пробуем без него
                print(f"[TG EDIT] Markdown failed: {result.get('description', '')}")
                clean_text = strip_markdown(new_text)
                tg_data_plain = {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": clean_text
                }
                response2 = requests.post(tg_url, json=tg_data_plain, timeout=10)
                result2 = response2.json()
                if not result2.get("ok"):
                    print(f"[TG EDIT ERROR] {result2}")
                    send_telegram_message(chat_id, new_text)
                else:
                    print(f"[TG EDIT] OK (plain fallback)")
        except Exception as e:
            print(f"[TG EDIT EXCEPTION] {e}")
            send_telegram_message(chat_id, new_text)
    else:
        # Длинный текст — разбиваем
        chunks = split_text(new_text, 4096)

        # Первый кусок — редактируем заглушку
        tg_data = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": chunks[0],
            "parse_mode": "Markdown"
        }
        try:
            response = requests.post(tg_url, json=tg_data, timeout=10)
            result = response.json()
            if not result.get("ok"):
                # Markdown сломался
                print(f"[TG EDIT CHUNK] Markdown failed, trying plain")
                clean_chunk = strip_markdown(chunks[0])
                tg_data_plain = {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": clean_chunk
                }
                requests.post(tg_url, json=tg_data_plain, timeout=10)
        except Exception as e:
            print(f"[TG EDIT CHUNK ERROR] {e}")
            send_telegram_message(chat_id, chunks[0])

        # Остальные куски — новыми сообщениями
        for chunk in chunks[1:]:
            send_telegram_message(chat_id, chunk)


# =============================================
# Разбивка длинного текста
# =============================================
def split_text(text, max_length=4096):
    chunks = []
    while len(text) > max_length:
        split_pos = text.rfind('\n', 0, max_length)
        if split_pos == -1:
            split_pos = text.rfind(' ', 0, max_length)
        if split_pos == -1:
            split_pos = max_length
        chunks.append(text[:split_pos])
        text = text[split_pos:].lstrip()
    if text:
        chunks.append(text)
    return chunks


# =============================================
# Показать "печатает..."
# =============================================
def send_typing_action(chat_id):
    tg_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendChatAction"
    try:
        requests.post(tg_url, json={"chat_id": chat_id, "action": "typing"}, timeout=5)
    except Exception:
        pass


# =============================================
# Динамический таймер
# =============================================
def update_timer(chat_id, message_id, stop_event):
    phrases = [
        "Анализирую вопрос",
        "Ищу информацию",
        "Копаюсь в материалах",
        "Подбираю лучший ответ",
        "Формулирую мысль",
        "Проверяю данные",
        "Почти готово",
        "Собираю ответ воедино",
        "Финальные штрихи",
        "Ещё чуть-чуть",
    ]

    frames = ["⏳", "⌛"]
    frame_index = 0
    total_seconds = 0
    phrase_index = 0

    while not stop_event.is_set():
        wait_time = random.uniform(2.0, 5.0)

        waited = 0
        while waited < wait_time:
            if stop_event.is_set():
                return
            time.sleep(0.3)
            waited += 0.3

        if stop_event.is_set():
            return

        total_seconds += int(round(wait_time))

        phrase = phrases[phrase_index % len(phrases)]
        phrase_index += 1

        frame = frames[frame_index % 2]
        frame_index += 1

        timer_text = f"{frame} {phrase}... ({total_seconds} сек)"

        tg_url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
        tg_data = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": timer_text
        }
        try:
            requests.post(tg_url, json=tg_data, timeout=5)
            print(f"[TIMER] {timer_text}")
        except Exception:
            pass

        send_typing_action(chat_id)


# =============================================
# Запрос в Dify (ТЕПЕРЬ С SQLite)
# =============================================
def ask_dify(user_text, chat_id, client_id):
    conv_id = get_conversation_id(chat_id)

    url = "https://api.dify.ai/v1/chat-messages"
    headers = {
        "Authorization": f"Bearer {DIFY_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "inputs": {},
        "query": user_text,
        "response_mode": "blocking",
        "conversation_id": conv_id,
        "user": str(client_id)
    }

    print(f"[SEND TO DIFY] query: {user_text}")
    print(f"[SEND TO DIFY] conv_id: {conv_id}")
    print(f"[SEND TO DIFY] user: {client_id}")

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=120)
        print(f"[DIFY STATUS] {response.status_code}")
        print(f"[DIFY RAW] {response.text[:500]}")
        result = response.json()
        answer = result.get("answer", "")
        new_conv_id = result.get("conversation_id", "")

        if new_conv_id:
            save_conversation_id(chat_id, new_conv_id)

        if not answer:
            answer = "Упс, мой мозг на секунду завис! Попробуй написать ещё раз"
        return answer

    except requests.exceptions.Timeout:
        print("[DIFY TIMEOUT]")
        return "Ой, я слишком долго думал и завис. Попроб
