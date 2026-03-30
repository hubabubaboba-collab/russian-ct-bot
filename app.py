from flask import Flask, request
import requests
import threading
import json
import os

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DIFY_API_KEY = os.environ.get("DIFY_API_KEY")

conversations = {}


# =============================================
# НОВОЕ: Функция отправки сообщения в Telegram
# Возвращает message_id (нужен чтобы потом отредактировать)
# =============================================
def send_telegram_message(chat_id, text):
    """Отправляет сообщение в Telegram и возвращает message_id"""
    tg_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    tg_data = {"chat_id": chat_id, "text": text}
    try:
        response = requests.post(tg_url, json=tg_data, timeout=10)
        result = response.json()
        if result.get("ok"):
            message_id = result["result"]["message_id"]
            print(f"[TG SEND] OK, message_id={message_id}")
            return message_id
        else:
            print(f"[TG SEND ERROR] {result}")
            return None
    except Exception as e:
        print(f"[TG SEND EXCEPTION] {e}")
        return None


# =============================================
# НОВОЕ: Функция редактирования сообщения в Telegram
# Заменяет текст заглушки на готовый ответ
# =============================================
def edit_telegram_message(chat_id, message_id, new_text):
    """Заменяет текст существующего сообщения"""

    # Telegram лимит — 4096 символов
    if len(new_text) <= 4096:
        # Текст помещается — просто редактируем заглушку
        tg_url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
        tg_data = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": new_text
        }
        try:
            response = requests.post(tg_url, json=tg_data, timeout=10)
            result = response.json()
            if result.get("ok"):
                print(f"[TG EDIT] OK")
            else:
                print(f"[TG EDIT ERROR] {result}")
                # Если редактирование не сработало — отправляем новым сообщением
                send_telegram_message(chat_id, new_text)
        except Exception as e:
            print(f"[TG EDIT EXCEPTION] {e}")
            send_telegram_message(chat_id, new_text)
    else:
        # Текст слишком длинный — разбиваем на части
        chunks = split_text(new_text, 4096)

        # Первую часть вставляем в заглушку (редактируем)
        tg_url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
        tg_data = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": chunks[0]
        }
        try:
            requests.post(tg_url, json=tg_data, timeout=10)
        except Exception as e:
            print(f"[TG EDIT CHUNK ERROR] {e}")
            send_telegram_message(chat_id, chunks[0])

        # Остальные части — новыми сообщениями
        for chunk in chunks[1:]:
            send_telegram_message(chat_id, chunk)


# =============================================
# НОВОЕ: Разбивка длинного текста на куски
# =============================================
def split_text(text, max_length=4096):
    """Разбивает текст на куски по max_length символов"""
    chunks = []
    while len(text) > max_length:
        # Ищем последний перенос строки в пределах лимита
        split_pos = text.rfind('\n', 0, max_length)
        if split_pos == -1:
            # Нет переноса — ищем пробел
            split_pos = text.rfind(' ', 0, max_length)
        if split_pos == -1:
            # Вообще негде резать — режем жёстко
            split_pos = max_length
        chunks.append(text[:split_pos])
        text = text[split_pos:].lstrip()
    if text:
        chunks.append(text)
    return chunks


# =============================================
# ИЗМЕНЕНО: Основная функция обработки сообщения
# Теперь с заглушкой и заменой
# =============================================
def process_message(user_text, chat_id, client_id):
    global conversations

    # ====== ШАГ 1: МГНОВЕННО отправляем заглушку ======
    placeholder_id = send_telegram_message(chat_id, "⏳ Думаю над ответом...")
    print(f"[PLACEHOLDER] message_id={placeholder_id}")

    # ====== ШАГ 2: Отправляем запрос в Dify ======
    conv_id = conversations.get(str(chat_id), "")

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
            conversations[str(chat_id)] = new_conv_id
        if not answer:
            answer = "Упс, мой мозг на секунду завис! Попробуй написать ещё раз 😅"
    except Exception as e:
        print(f"[ERROR] {str(e)}")
        answer = "Упс, что-то пошло не так 😅 Попробуй написать ещё раз!"

    # ====== ШАГ 3: Заменяем заглушку на ответ ======
    if placeholder_id:
        # Заглушка отправилась — редактируем её
        edit_telegram_message(chat_id, placeholder_id, answer)
        print(f"[DONE] Заглушка заменена на ответ")
    else:
        # Заглушка не отправилась — шлём ответ как обычно
        send_telegram_message(chat_id, answer)
        print(f"[DONE] Ответ отправлен новым сообщением (заглушка не сработала)")


# =============================================
# БЕЗ ИЗМЕНЕНИЙ: Эндпоинты
# =============================================
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
    t = threading.Thread(target=process_message, args=(user_text, chat_id, client_id))
    t.start()
    return json.dumps({"status": "ok"})


@app.route("/reset", methods=["POST"])
def reset():
    data = request.json
    chat_id = str(data.get("chat_id", ""))
    if chat_id in conversations:
        del conversations[chat_id]
    return json.dumps({"status": "reset"})


@app.route("/", methods=["GET"])
def home():
    return "Bot server is running!"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
