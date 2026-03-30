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

last_message_time = {}
processing = {}
MIN_DELAY = 2
spam_lock = threading.Lock()
MAX_MESSAGE_AGE = 60

DB_PATH = "/opt/render/project/data/bot.db"

def init_db():
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


init_db()


# =============================================
# КОНВЕРТЕР Markdown → HTML для Telegram
# =============================================
def markdown_to_html(text):
    # Убираем двойные пробелы DeepSeek
    text = re.sub(r'  +\n', '\n', text)
    text = re.sub(r'  +$', '', text, flags=re.MULTILINE)
    # Убираем # заголовки
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # Экранируем HTML-спецсимволы (кроме наших тегов)
    text = text.replace('&', '&amp;')
    text = text.replace('<', '&lt;')
    text = text.replace('>', '&gt;')
    # Конвертируем *** (жирный курсив) → <b><i>текст</i></b>
    text = re.sub(r'\*\*\*(.+?)\*\*\*', r'<b><i>\1</i></b>', text)
    # Конвертируем ** (жирный) → <b>текст</b>
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    # Конвертируем * (курсив) → <i>текст</i>
    text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text)
    # Убираем оставшиеся одиночные звёздочки (непарные)
    text = text.replace('*', '')
    return text


# =============================================
# Убрать всё форматирование (для fallback)
# =============================================
def strip_all_formatting(text):
    text = re.sub(r'```[\s\S]*?```', '', text)
    text = re.sub(r'`(.+?)`', r'\1', text)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = text.replace('***', '')
    text = text.replace('**', '')
    text = text.replace('*', '')
    text = re.sub(r'__(.+?)__', r'\1', text)
    text = re.sub(r'(?<!\w)_(.+?)_(?!\w)', r'\1', text)
    text = re.sub(r'  +\n', '\n', text)
    text = re.sub(r'  +$', '', text, flags=re.MULTILINE)
    return text


# =============================================
# Отправка сообщения: HTML + fallback на plain
# =============================================
def send_telegram_message(chat_id, text):
    tg_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    html_text = markdown_to_html(text)
    tg_data = {"chat_id": chat_id, "text": html_text, "parse_mode": "HTML"}
    try:
        response = requests.post(tg_url, json=tg_data, timeout=10)
        result = response.json()
        if result.get("ok"):
            message_id = result["result"]["message_id"]
            print(f"[TG SEND] OK (HTML), message_id={message_id}")
            return message_id
        else:
            print(f"[TG SEND] HTML failed: {result.get('description', '')}")
            clean_text = strip_all_formatting(text)
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
# Редактирование сообщения: HTML + fallback
# =============================================
def edit_telegram_message(chat_id, message_id, new_text):
    tg_url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"

    html_text = markdown_to_html(new_text)

    if len(html_text) <= 4096:
        tg_data = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": html_text,
            "parse_mode": "HTML"
        }
        try:
            response = requests.post(tg_url, json=tg_data, timeout=10)
            result = response.json()
            if result.get("ok"):
                print(f"[TG EDIT] OK (HTML)")
            else:
                print(f"[TG EDIT] HTML failed: {result.get('description', '')}")
                clean_text = strip_all_formatting(new_text)
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
        chunks = split_text(html_text, 4096)
        tg_data = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": chunks[0],
            "parse_mode": "HTML"
        }
        try:
            response = requests.post(tg_url, json=tg_data, timeout=10)
            result = response.json()
            if not result.get("ok"):
                clean_chunk = strip_all_formatting(chunks[0])
                tg_data_plain = {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": clean_chunk
                }
                requests.post(tg_url, json=tg_data_plain, timeout=10)
        except Exception as e:
            print(f"[TG EDIT CHUNK ERROR] {e}")
            send_telegram_message(chat_id, chunks[0])

        for chunk in chunks[1:]:
            send_telegram_message(chat_id, chunk)
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


def send_typing_action(chat_id):
    tg_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendChatAction"
    try:
        requests.post(tg_url, json={"chat_id": chat_id, "action": "typing"}, timeout=5)
    except Exception:
        pass


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
        return "Ой, я слишком долго думал и завис. Попробуй ещё раз!"
    except Exception as e:
        print(f"[DIFY ERROR] {str(e)}")
        return "Упс, что-то пошло не так. Попробуй написать ещё раз!"


def process_message(user_text, chat_id, client_id):
    placeholder_id = send_telegram_message(chat_id, "⏳ Анализирую вопрос...")
    print(f"[PLACEHOLDER] message_id={placeholder_id}")
    if not placeholder_id:
        answer = ask_dify(user_text, chat_id, client_id)
        send_telegram_message(chat_id, answer)
        with spam_lock:
            processing[str(chat_id)] = False
        return
    stop_event = threading.Event()
    timer_thread = threading.Thread(
        target=update_timer,
        args=(chat_id, placeholder_id, stop_event)
    )
    timer_thread.start()
    print(f"[TIMER STARTED]")
    answer = ask_dify(user_text, chat_id, client_id)
    stop_event.set()
    timer_thread.join(timeout=5)
    print(f"[TIMER STOPPED]")
    time.sleep(0.3)
    parts = answer.split("===SPLIT===")
    parts = [part.strip() for part in parts if part.strip()]
    if len(parts) <= 1:
        edit_telegram_message(chat_id, placeholder_id, answer)
        print(f"[DONE] Ответ отправлен (одним сообщением)")
    else:
        edit_telegram_message(chat_id, placeholder_id, parts[0])
        print(f"[SPLIT] Часть 1/{len(parts)} → заглушка заменена")
        for i, part in enumerate(parts[1:], start=2):
            pause = random.uniform(7.0, 12.0)
            send_typing_action(chat_id)
            time.sleep(pause)
            send_typing_action(chat_id)
            send_telegram_message(chat_id, part)
            print(f"[SPLIT] Часть {i}/{len(parts)} → отправлена (пауза {pause:.1f} сек)")
        print(f"[DONE] Ответ отправлен ({len(parts)} частей)")
    with spam_lock:
        processing[str(chat_id)] = False
        print(f"[UNLOCK] chat_id={chat_id} разблокирован")


@app.route("/ask", methods=["POST"])
def ask():
    data = request.json
    print(f"[RECEIVED] {data}")
    user_text = data.get("question", "")
    chat_id = data.get("chat_id", "")
    client_id = data.get("client_id", "user")
    if not user_text or not chat_id:
        print(f"[SKIP] empty question or chat_id")
        return json.dumps({"status": "error"})
    chat_id_str = str(chat_id)
    current_time = time.time()
    message_timestamp = data.get("timestamp", None)
    if message_timestamp:
        try:
            message_timestamp = float(message_timestamp)
            message_age = current_time - message_timestamp
            if message_age > MAX_MESSAGE_AGE:
                print(f"[OLD MESSAGE] chat_id={chat_id}, возраст={message_age:.0f} сек, ИГНОР")
                return json.dumps({"status": "too_old"})
        except (ValueError, TypeError):
            pass
    with spam_lock:
        if processing.get(chat_id_str, False):
            print(f"[SPAM BLOCK] chat_id={chat_id} уже обрабатывается")
            send_telegram_message(chat_id, "✋ Подожди, я ещё думаю над прошлым вопросом! Отвечу и сразу возьмусь за следующий.")
            return json.dumps({"status": "busy"})
        last_time = last_message_time.get(chat_id_str, 0)
        if current_time - last_time < MIN_DELAY:
            print(f"[SPAM DELAY] chat_id={chat_id} слишком быстро")
            return json.dumps({"status": "too_fast"})
        processing[chat_id_str] = True
        last_message_time[chat_id_str] = current_time
    t = threading.Thread(target=process_message, args=(user_text, chat_id, client_id))
    t.start()
    return json.dumps({"status": "ok"})


@app.route("/reset", methods=["POST"])
def reset():
    data = request.json
    chat_id = str(data.get("chat_id", ""))
    delete_conversation_id(chat_id)
    with spam_lock:
        processing[chat_id] = False
    return json.dumps({"status": "reset"})


@app.route("/", methods=["GET"])
def home():
    return "Bot server is running!"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
