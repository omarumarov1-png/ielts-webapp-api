import os
import json
import hmac
import hashlib
import logging
from urllib.parse import parse_qsl
from datetime import datetime

from flask import Flask, request, jsonify
from flask_cors import CORS
import anthropic

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)  # разрешаем запросы из Telegram WebView

TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

# ─── VIP — безлимитный Premium-доступ навсегда (синхронизировано с bot.py) ──
VIP_IDS = {7383007115}

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

DB_FILE = "users.json"  # тот же файл, что использует bot.py

# ─── Проверка подлинности запроса от Telegram ──────────────────
# Это защищает эндпоинт: только запросы из настоящего Telegram Mini App
# (открытого внутри Telegram) пройдут проверку. Чужой сайт не сможет
# пользоваться твоим API-ключом бесплатно.
def verify_telegram_data(init_data: str) -> dict | None:
    try:
        parsed = dict(parse_qsl(init_data))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None

        data_check_string = "\n".join(
            f"{k}={v}" for k, v in sorted(parsed.items())
        )
        secret_key = hmac.new(b"WebAppData", TELEGRAM_TOKEN.encode(), hashlib.sha256).digest()
        computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        if computed_hash != received_hash:
            return None

        user_data = json.loads(parsed.get("user", "{}"))
        return user_data
    except Exception as e:
        logger.error(f"Verify error: {e}")
        return None

# ─── БД helpers (общие с bot.py через users.json) ──────────────
def load_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE) as f:
            return json.load(f)
    return {}

def save_db(db):
    with open(DB_FILE, "w") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)

def get_user(uid: str) -> dict:
    db = load_db()
    if uid not in db:
        db[uid] = {
            "checks_used": 0, "free_limit": 2,
            "paid": False, "premium": False, "paid_checks": 0,
            "joined": datetime.now().isoformat()
        }
        save_db(db)
    return db[uid]

def update_user(uid: str, data: dict):
    db = load_db()
    db[uid].update(data)
    save_db(db)

def can_check(uid: str) -> bool:
    if int(uid) in VIP_IDS:
        return True
    u = get_user(uid)
    if u["paid"] and u["paid_checks"] > 0:
        return True
    return u["checks_used"] < u["free_limit"]

def use_check(uid: str):
    if int(uid) in VIP_IDS:
        return  # VIP — проверки не расходуются
    u = get_user(uid)
    if u["paid"] and u["paid_checks"] > 0:
        update_user(uid, {"paid_checks": u["paid_checks"] - 1})
    else:
        update_user(uid, {"checks_used": u["checks_used"] + 1})

def is_premium(uid: str) -> bool:
    if int(uid) in VIP_IDS:
        return True
    return get_user(uid).get("premium", False)

# ─── Промпт ─────────────────────────────────────────────────────
PROMPT_CHECK = """Ты — строгий экзаменатор IELTS Writing Task 2. Оценивай честно.

Критерии:
1. Task Response (TR) — тема, позиция, аргументы
2. Coherence & Cohesion (CC) — логика, структура
3. Lexical Resource (LR) — словарный запас
4. Grammatical Range & Accuracy (GRA) — грамматика

ФОРМАТ (строго):

ОБЩИЙ БАЛЛ: X.X / 9.0

TR: X.X
[2 предложения]

CC: X.X
[2 предложения]

LR: X.X
[2 предложения + примеры из эссе]

GRA: X.X
[2 предложения + 2 ошибки с исправлением]

СИЛЬНЫЕ СТОРОНЫ:
- [пункт]
- [пункт]

УЛУЧШИТЬ:
- [главное]
- [второе]

СОВЕТ:
[1 конкретный совет]

Не завышай оценки. Среднее эссе — 5.5–6.0."""

PROMPT_POLISH = """Ты — опытный редактор IELTS эссе. Улучши эссе студента, сохранив его идеи и структуру.

Задача:
1. Улучши лексику — замени простые слова на академические синонимы
2. Исправь грамматические ошибки
3. Улучши связность — добавь/улучши linking words
4. Сохрани оригинальную позицию и аргументы автора
5. Не переписывай эссе настолько сложным языком, что оно перестанет звучать как текст этого студента — улучшения должны быть реалистичными, не выше уровня B2-C1

ФОРМАТ ОТВЕТА:

УЛУЧШЕННАЯ ВЕРСИЯ:

[полный текст улучшенного эссе]

ЧТО ИЗМЕНЕНО:
- [изменение 1 — конкретно: "заменил X на Y потому что..."]
- [изменение 2]
- [изменение 3]

ОЖИДАЕМЫЙ ПРИРОСТ БАЛЛА: +0.5 — +1.0

ЧЕСТНО О ПОТОЛКЕ:
Косметическая правка одного эссе поднимает балл, но не меняет фундаментальный уровень владения языком. Если для выхода на 8.0+ нужна более глубокая работа — над структурой аргументации, разнообразием грамматических конструкций, естественностью академического стиля — прямо скажи об этом и кратко укажи, что именно для этого нужно прорабатывать системно, а не за одну правку."""

# ─── Эндпоинт: проверка эссе ────────────────────────────────────
@app.route("/api/check", methods=["POST"])
def check_essay():
    data = request.get_json(force=True)
    init_data = data.get("initData", "")
    essay = data.get("essay", "").strip()

    user_data = verify_telegram_data(init_data)
    if not user_data:
        return jsonify({"error": "unauthorized"}), 401

    uid = str(user_data.get("id"))
    if not uid:
        return jsonify({"error": "no_user_id"}), 400

    if len(essay.split()) < 60:
        return jsonify({"error": "too_short"}), 400

    if not can_check(uid):
        return jsonify({"error": "limit_reached"}), 403

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            system=PROMPT_CHECK,
            messages=[{"role": "user", "content": f"Проверь IELTS Writing Task 2:\n\n{essay}"}]
        )
        result = response.content[0].text
        use_check(uid)

        user = get_user(uid)
        if int(uid) in VIP_IDS:
            checks_left = "∞"
        else:
            checks_left = user["paid_checks"] if user["paid"] else max(0, user["free_limit"] - user["checks_used"])

        return jsonify({
            "result": result,
            "checks_left": checks_left,
            "is_paid": user["paid"] or int(uid) in VIP_IDS,
            "is_premium": is_premium(uid)
        })
    except Exception as e:
        logger.error(f"API error: {e}")
        return jsonify({"error": "server_error"}), 500

# ─── Эндпоинт: улучшение эссе (только Premium/VIP) ──────────────
@app.route("/api/polish", methods=["POST"])
def polish_essay():
    data = request.get_json(force=True)
    init_data = data.get("initData", "")
    essay = data.get("essay", "").strip()

    user_data = verify_telegram_data(init_data)
    if not user_data:
        return jsonify({"error": "unauthorized"}), 401

    uid = str(user_data.get("id"))
    if not is_premium(uid):
        return jsonify({"error": "premium_required"}), 403

    if len(essay.split()) < 80:
        return jsonify({"error": "too_short"}), 400

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            system=PROMPT_POLISH,
            messages=[{"role": "user", "content": f"Улучши это IELTS эссе:\n\n{essay}"}]
        )
        return jsonify({"result": response.content[0].text})
    except Exception as e:
        logger.error(f"Polish API error: {e}")
        return jsonify({"error": "server_error"}), 500

# ─── Эндпоинт: статус пользователя ──────────────────────────────
@app.route("/api/status", methods=["POST"])
def status():
    data = request.get_json(force=True)
    init_data = data.get("initData", "")

    user_data = verify_telegram_data(init_data)
    if not user_data:
        return jsonify({"error": "unauthorized"}), 401

    uid = str(user_data.get("id"))
    user = get_user(uid)
    if int(uid) in VIP_IDS:
        checks_left = "∞"
    else:
        checks_left = user["paid_checks"] if user["paid"] else max(0, user["free_limit"] - user["checks_used"])

    return jsonify({
        "checks_left": checks_left,
        "is_paid": user["paid"] or int(uid) in VIP_IDS,
        "is_premium": is_premium(uid),
        "is_vip": int(uid) in VIP_IDS,
        "name": user_data.get("first_name", "")
    })

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
