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
        print(f"[DB] Init OK: {DB_PATH}")
    except Exception as e:
        print(f"[DB ERROR] {e}")


def get_conversation_id(chat_id):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT conversation_id FROM conversations WHERE chat_id = ?", (str(chat_id),))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else ""
    except Exception as e:
        print(f"[DB ERROR] get: {e}")
        return ""


def save_conversation_id(chat_id, conversation_id):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO conversations (chat_id, conversation_id, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET conversation_id = ?, updated_at = ?
        """, (str(chat_id), conversation_id, time.time(), conversation_id, time.time()))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[DB ERROR] save: {e}")


def delete_conversation_id(chat_id):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM conversations WHERE chat_id = ?", (str(chat_id),))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[DB ERROR] delete: {e}")


init_db()


def markdown_to_html(text):
    text = text.replace('\\\\', '\x00SLASH\x00')
    text = text.replace('\\', '')
    text = text.replace('\x00SLASH\x00', '\\')
    text = re.sub(r'  +\n', '\n', text)
    text = re.sub(r'  +$', '', text, flags=re.MULTILINE)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = text.replace('&', '&amp;')
    text = text.replace('<', '&lt;')
    text = text.replace('>', '&gt;')
    text = re.sub(r'\*\*\*(.+?)\*\*\*', r'<b><i>\1</i></b>', text)
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text)
    text = text.replace('*', '')
    return text


def strip_all_formatting(text):
    text = re.sub(r'```[\s\S]*?```', '', text)
    text = re.sub(r'`(.+?)`', r'\1', text)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = text.replace('***', '')
    text = text.replace('**', '')
    text = text.replace('*', '')
    text = re.sub(r'  +\n', '\n', text)
    text = re.sub(r'  +$', '', text, flags=re.MULTILINE)
    return text


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


def send_telegram_message(chat_id, text):
    tg_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    html_text = markdown_to_html(text)
    try:
        response = requests.post(tg_url, json={"chat_id": chat_id, "text": html_text, "parse_mode": "HTML"}, timeout=10)
        result = response.json()
        if result.get("ok"):
            msg_id = result["result"]["message_id"]
            print(f"[TG SEND] OK (HTML), message_id={msg_id}")
            return msg_id
        else:
            print(f"[TG SEND] HTML failed: {result.get('description', '')}")
            clean = strip_all_formatting(text)
            r2 = requests.post(tg_url, json={"chat_id": chat_id, "text": clean}, timeout=10)
            res2 = r2.json()
            if res2.get("ok"):
                msg_id = res2["result"]["message_id"]
                print(f"[TG SEND] OK (plain), message_id={msg_id}")
                return msg_id
            print(f"[TG SEND ERROR] {res2}")
            return None
    except Exception as e:
        print(f"[TG SEND EX] {e}")
        return None


def edit_telegram_message(chat_id, message_id, new_text):
    tg_url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
    html_text = markdown_to_html(new_text)
    if len(html_text) <= 4096:
        try:
            response = requests.post(tg_url, json={"chat_id": chat_id, "message_id": message_id, "text": html_text, "parse_mode": "HTML"}, timeout=10)
            result = response.json()
            if result.get("ok"):
                print(f"[TG EDIT] OK (HTML)")
            else:
                print(f"[TG EDIT] HTML failed: {result.get('description', '')}")
                clean = strip_all_formatting(new_text)
                r2 = requests.post(tg_url, json={"chat_id": chat_id, "message_id": message_id, "text": clean}, timeout=10)
                if not r2.json().get("ok"):
                    send_telegram_message(chat_id, new_text)
        except Exception as e:
            print(f"[TG EDIT EX] {e}")
            send_telegram_message(chat_id, new_text)
    else:
        chunks = split_text(html_text, 4096)
        try:
            r = requests.post(tg_url, json={"chat_id": chat_id, "message_id": message_id, "text": chunks[0], "parse_mode": "HTML"}, timeout=10)
            if not r.json().get("ok"):
                clean = strip_all_formatting(chunks[0])
                requests.post(tg_url, json={"chat_id": chat_id, "message_id": message_id, "text": clean}, timeout=10)
        except Exception:
            send_telegram_message(chat_id, chunks[0])
        for chunk in chunks[1:]:
            send_telegram_message(chat_id, chunk)


def update_timer(chat_id, message_id, stop_event):
    phrases = ["Анализирую вопрос", "Ищу информацию", "Копаюсь в материалах", "Подбираю лучший ответ", "Формулирую мысль", "Проверяю данные", "Почти готово", "Собираю ответ воедино", "Финальные штрихи", "Ещё чуть-чуть"]
    frames = ["⏳", "⌛"]
    fi = 0
    ts = 0
    pi = 0
    while not stop_event.is_set():
        wt = random.uniform(2.0, 5.0)
        waited = 0
        while waited < wt:
            if stop_event.is_set():
                return
            time.sleep(0.3)
            waited += 0.3
        if stop_event.is_set():
            return
        ts += int(round(wt))
        txt = f"{frames[fi % 2]} {phrases[pi % len(phrases)]}... ({ts} сек)"
        fi += 1
        pi += 1
        try:
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText", json={"chat_id": chat_id, "message_id": message_id, "text": txt}, timeout=5)
        except Exception:
            pass
        send_typing_action(chat_id)


def ask_dify(user_text, chat_id, client_id):
    conv_id = get_conversation_id(chat_id)
    headers = {"Authorization": f"Bearer {DIFY_API_KEY}", "Content-Type": "application/json"}
    payload = {"inputs": {}, "query": user_text, "response_mode": "blocking", "conversation_id": conv_id, "user": str(client_id)}
    try:
        response = requests.post("https://api.dify.ai/v1/chat-messages", headers=headers, json=payload, timeout=120)
        result = response.json()
        answer = result.get("answer", "")
        new_conv = result.get("conversation_id", "")
        if new_conv:
            save_conversation_id(chat_id, new_conv)
        return answer if answer else "Упс, мой мозг завис! Попробуй ещё раз"
    except requests.exceptions.Timeout:
        return "Ой, я слишком долго думал. Попробуй ещё раз!"
    except Exception as e:
        print(f"[DIFY ERROR] {e}")
        return "Что-то пошло не так. Попробуй ещё раз!"


def process_message(user_text, chat_id, client_id):
    placeholder_id = send_telegram_message(chat_id, "⏳ Анализирую вопрос...")
    if not placeholder_id:
        answer = ask_dify(user_text, chat_id, client_id)
        send_telegram_message(chat_id, answer)
        with spam_lock:
            processing[str(chat_id)] = False
        return
    stop_event = threading.Event()
    timer_thread = threading.Thread(target=update_timer, args=(chat_id, placeholder_id, stop_event))
    timer_thread.start()
    answer = ask_dify(user_text, chat_id, client_id)
    stop_event.set()
    timer_thread.join(timeout=5)
    time.sleep(0.3)
    parts = answer.split("===SPLIT===")
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) <= 1:
        edit_telegram_message(chat_id, placeholder_id, answer)
    else:
        edit_telegram_message(chat_id, placeholder_id, parts[0])
        for i, part in enumerate(parts[1:], start=2):
            pause = random.uniform(7.0, 12.0)
            send_typing_action(chat_id)
            time.sleep(pause)
            send_typing_action(chat_id)
            send_telegram_message(chat_id, part)
    with spam_lock:
        processing[str(chat_id)] = False


@app.route("/ask", methods=["POST"])
def ask():
    data = request.json
    user_text = data.get("question", "")
    chat_id = data.get("chat_id", "")
    client_id = data.get("client_id", "user")
    if not user_text or not chat_id:
        return json.dumps({"status": "error"})
    chat_id_str = str(chat_id)
    current_time = time.time()
    ts = data.get("timestamp", None)
    if ts:
        try:
            if current_time - float(ts) > MAX_MESSAGE_AGE:
                return json.dumps({"status": "too_old"})
        except (ValueError, TypeError):
            pass
    with spam_lock:
        if processing.get(chat_id_str, False):
            send_telegram_message(chat_id, "✋ Подожди, я ещё думаю над прошлым вопросом!")
            return json.dumps({"status": "busy"})
        if current_time - last_message_time.get(chat_id_str, 0) < MIN_DELAY:
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
