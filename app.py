from flask import Flask, request
import requests
import threading
import json
import os

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DIFY_API_KEY = os.environ.get("DIFY_API_KEY")

conversations = {}

def process_message(user_text, chat_id, client_id):
    global conversations
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
        answer = result.get("answer", "Нет ответа от нейросети")
        new_conv_id = result.get("conversation_id", "")
        if new_conv_id:
            conversations[str(chat_id)] = new_conv_id
    except Exception as e:
        print(f"[ERROR] {str(e)}")
        answer = "Ошибка: " + str(e)

    tg_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    tg_data = {"chat_id": chat_id, "text": answer}
    requests.post(tg_url, json=tg_data)

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
