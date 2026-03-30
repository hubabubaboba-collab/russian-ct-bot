from flask import Flask, request
import requests
import threading
import json
import os
import time

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DIFY_API_KEY = os.environ.get("DIFY_API_KEY")

conversations = {}


# =============================================
# Отправка сообщения в Telegram (возвращает message_id)
# =============================================
def send_telegram_message(chat_id, text):
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
# Редактирование сообщения (с разбивкой если длинный текст)
# =============================================
def edit_telegram_message(chat_id, message_id, new_text):
    if len(new_text) <= 4096:
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
                send_telegram_message(chat_id, new_text)
        except Exception as e:
            print(f"[TG EDIT EXCEPTION] {e}")
            send_telegram_message(chat_id, new_text)
    else:
        chunks = split_text(new_text, 4096)
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
# Показать "печатает..." в Telegram
# =============================================
def send_typing_action(chat_id):
    tg_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendChatAction"
    try:
        requests.post(tg_url, json={"chat_id": chat_id, "action": "typing"}, timeout=5)
    except Exception:
        pass


# =============================================
# НОВОЕ: Динамический таймер
# Обновляет заглушку каждые 2 секунды пока Dify думает
# =============================================
def update_timer(chat_id, message_id, stop_event):
    """
    Работает в отдельном потоке.
    Каждые 2 секунды обновляет текст заглушки: (2 сек), (4 сек), (6 сек)...
    Останавливается когда stop_event установлен (= Dify ответил).
    """
    seconds = 0
    # Чередуем песочные часы для анимации
    frames = ["⏳", "⌛"]
    frame_index = 0

    while not stop_event.is_set():
        # Ждём 2 секунды (но проверяем stop_event каждые 0.5 сек)
        for _ in range(4):
            if stop_event.is_set():
                return
            time.sleep(0.5)

        seconds += 2

        # Если за время ожидания Dify уже ответил — выходим
        if stop_event.is_set():
            return

        # Выбираем кадр анимации
        frame = frames[frame_index % 2]
        frame_index += 1

        # Обновляем текст заглушки
        timer_text = f"{frame} Думаю... ({seconds} сек)"

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
            pass  # Не критично если одно обновление не прошло

        # Обновляем статус "печатает..."
        send_typing_action(chat_id)


# =============================================
# Запрос в Dify
# =============================================
def ask_dify(user_text, chat_id, client_id):
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
            answer = "Упс, мой мозг на секунду завис! Попробуй написать ещё раз"
        return answer

    except requests.exceptions.Timeout:
        print("[DIFY TIMEOUT]")
        return "Ой, я слишком долго думал и завис. Попробуй ещё раз!"

    except Exception as e:
        print(f"[DIFY ERROR] {str(e)}")
        return "Упс, что-то пошло не так. Попробуй написать ещё раз!"


# =============================================
# ГЛАВНАЯ ФУНКЦИЯ: заглушка + таймер + замена
# =============================================
def process_message(user_text, chat_id, client_id):
    global conversations

    # ====== ШАГ 1: Отправляем заглушку с таймером ======
    placeholder_id = send_telegram_message(chat_id, "⏳ Думаю... (0 сек)")
    print(f"[PLACEHOLDER] message_id={placeholder_id}")

    if not placeholder_id:
        # Заглушка не отправилась — работаем без таймера
        answer = ask_dify(user_text, chat_id, client_id)
        send_telegram_message(chat_id, answer)
        return

    # ====== ШАГ 2: Запускаем таймер в отдельном потоке ======
    stop_event = threading.Event()
    timer_thread = threading.Thread(
        target=update_timer,
        args=(chat_id, placeholder_id, stop_event)
    )
    timer_thread.start()
    print(f"[TIMER STARTED]")

    # ====== ШАГ 3: Запрашиваем ответ у Dify ======
    # Пока эта строка выполняется (5-60 сек) — таймер тикает
    answer = ask_dify(user_text, chat_id, client_id)

    # ====== ШАГ 4: Останавливаем таймер ======
    stop_event.set()  # Говорим потоку таймера: "стоп!"
    timer_thread.join(timeout=5)  # Ждём пока поток завершится
    print(f"[TIMER STOPPED]")

    # Маленькая пауза чтобы таймер точно остановился
    time.sleep(0.3)

    # ====== ШАГ 5: Заменяем заглушку на ответ ======
    edit_telegram_message(chat_id, placeholder_id, answer)
    print(f"[DONE] Заглушка заменена на ответ")


# =============================================
# Эндпоинты (БЕЗ ИЗМЕНЕНИЙ)
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
