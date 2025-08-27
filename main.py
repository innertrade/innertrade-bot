# main.py
import os
import json
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from flask import Flask, request, jsonify, abort
from telebot import TeleBot, types
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

# =============== ЛОГИ ===============
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# =============== ENV ===============
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "")
DATABASE_URL    = os.getenv("DATABASE_URL", "")
PUBLIC_URL      = os.getenv("PUBLIC_URL", "")
WEBHOOK_PATH    = os.getenv("WEBHOOK_PATH", "webhook")
TG_SECRET       = os.getenv("TG_WEBHOOK_SECRET", "")  # должен совпадать с setWebhook
DISPLAY_TZ      = os.getenv("DISPLAY_TZ", "")         # напр. "Europe/Moscow" (опционально)

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN missing")
if not PUBLIC_URL:
    raise RuntimeError("PUBLIC_URL missing (e.g., https://your-app.onrender.com)")

WEBHOOK_URL = f"{PUBLIC_URL.rstrip('/')}/{WEBHOOK_PATH}"

# =============== DB ===============
engine = None
if DATABASE_URL:
    try:
        engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
        with engine.begin() as conn:
            # минимальные таблицы (idempotent)
            conn.execute(text("""
            CREATE TABLE IF NOT EXISTS users(
              user_id BIGINT PRIMARY KEY,
              mode TEXT NOT NULL DEFAULT 'course',
              created_at TIMESTAMPTZ DEFAULT now(),
              updated_at TIMESTAMPTZ DEFAULT now()
            );
            """))
            conn.execute(text("""
            CREATE TABLE IF NOT EXISTS user_state(
              user_id BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
              intent  TEXT,
              step    TEXT,
              data    JSONB,
              updated_at TIMESTAMPTZ DEFAULT now()
            );
            """))
        logging.info("DB connected & ensured minimal schema")
    except OperationalError as e:
        logging.warning(f"DB not available yet: {e}")
        engine = None
else:
    logging.info("DATABASE_URL not set — running without DB persistence")

def db_exec(sql: str, params: Optional[Dict[str, Any]] = None):
    if not engine:
        return None
    with engine.begin() as conn:
        return conn.execute(text(sql), params or {})

def ensure_user(uid: int):
    if not engine: return
    db_exec("INSERT INTO users(user_id) VALUES(:u) ON CONFLICT (user_id) DO NOTHING", {"u": uid})

def get_state(uid: int) -> Dict[str, Any]:
    if not engine:
        return {"intent": "greet", "step": None, "data": {}}
    res = db_exec("SELECT intent, step, COALESCE(data,'{}'::jsonb) AS data FROM user_state WHERE user_id=:u", {"u": uid}).mappings().first()
    return dict(res) if res else {"intent": "greet", "step": None, "data": {}}

def save_state(uid: int, intent: Optional[str] = None, step: Optional[str] = None, data: Optional[dict] = None):
    if not engine: return
    cur = get_state(uid)
    if intent is None: intent = cur.get("intent")
    if step   is None: step   = cur.get("step")
    merged = cur.get("data", {}).copy()
    if data: merged.update(data)
    db_exec("""
    INSERT INTO user_state(user_id,intent,step,data)
    VALUES(:u,:i,:s,CAST(:d AS JSONB))
    ON CONFLICT (user_id) DO UPDATE SET
      intent=EXCLUDED.intent,
      step=EXCLUDED.step,
      data=EXCLUDED.data,
      updated_at=now()
    """, {"u": uid, "i": intent, "s": step, "d": json.dumps(merged)})

# =============== NLP-хелперы (минимум логики, без упоминания «техник») ===============
BEHAVIOR_VERBS = [
    "вхожу", "войти", "захожу", "зайти",
    "выход", "выхожу", "закрываю", "переношу", "двигаю",
    "усредня", "фиксир", "фиксирую", "пытаюсь отыграться",
    "пересиживаю", "увеличиваю риск", "уменьшаю риск"
]

def looks_concrete_behavior(text: str) -> bool:
    t = text.lower()
    return any(v in t for v in BEHAVIOR_VERBS) and len(t.split()) >= 3

def too_abstract(text: str) -> bool:
    t = text.lower()
    return any(x in t for x in ["в определенные дни", "иногда", "бывает", "не всегда", "по-разному", "когда-то"])

def summarize_to_behavior(candidate: str) -> str:
    # Очень простая нормализация к поведению
    t = candidate.strip().rstrip(".")
    # Без «умничания», возвращаем как есть — бот попросит подтверждение
    return t

# =============== TELEGRAM BOT ===============
bot = TeleBot(TELEGRAM_TOKEN, parse_mode="Markdown")

def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🚑 У меня ошибка", "🧩 Хочу стратегию")
    kb.row("📄 Паспорт", "🗒 Панель недели")
    kb.row("🆘 Экстренно", "🤔 Просто поговорить")
    return kb

def yes_no_add_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.row("✅ Ок", "✏️ Добавить/уточнить", "❌ Не то")
    return kb

# =============== ВСТУПЛЕНИЕ (мягкое) ===============
def greet_user(m):
    uid = m.from_user.id
    ensure_user(uid)
    st = get_state(uid)
    data = st.get("data", {})
    # имя подтягиваем из Telegram, не просим повторно
    name = data.get("name") or (m.from_user.first_name or "").strip()
    want_form = data.get("form")  # 'ty' | 'vy' | None

    # сохраним имя, если есть
    if name and name != data.get("name"):
        save_state(uid, intent="greet", step="ask_form" if not want_form else None, data={"name": name})

    # если нет формы обращения — спросим. Сначала поздороваемся
    if not want_form:
        bot.send_message(
            m.chat.id,
            f"👋 Привет{', ' + name if name else ''}! Как удобнее общаться — на *ты* или на *вы*?",
            reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True).row("ты", "вы")
        )
        save_state(uid, intent="greet", step="ask_form")
        return

    # если форма уже есть — нормальный старт без давления
    bot.send_message(
        m.chat.id,
        f"👋 Привет{', ' + name if name else ''}! Можем просто поговорить или заняться задачей — как тебе удобнее.",
        reply_markup=main_menu()
    )
    save_state(uid, intent="idle", step=None)

# Команды
@bot.message_handler(commands=["start", "menu", "reset"])
def cmd_start(m):
    # Сбросим состояние
    ensure_user(m.from_user.id)
    save_state(m.from_user.id, intent="greet", step=None, data={"session_free_talk": 0})
    greet_user(m)

@bot.message_handler(commands=["ping"])
def cmd_ping(m):
    bot.send_message(m.chat.id, "pong")

@bot.message_handler(commands=["status"])
def cmd_status(m):
    st = get_state(m.from_user.id)
    now_utc = datetime.now(timezone.utc).isoformat()
    human = (
        f"✅ Бот жив\n"
        f"⏱ Время (UTC): {now_utc}\n"
        f"🎯 Intent: {st.get('intent')}\n"
        f"🧩 Step: {st.get('step') or '-'}\n"
        f"🗄 DB: {'ok' if engine else 'no-db'}"
    )
    bot.send_message(m.chat.id, human)

# Обработка выбора формы
@bot.message_handler(func=lambda msg: msg.text and msg.text.lower() in ["ты", "вы"])
def choose_form(m):
    st = get_state(m.from_user.id)
    if st.get("intent") != "greet" or st.get("step") != "ask_form":
        return  # не вмешиваемся
    form = "ty" if m.text.lower() == "ты" else "vy"
    save_state(m.from_user.id, intent="idle", step=None, data={"form": form})
    bot.send_message(
        m.chat.id,
        "Принято. Можем просто поговорить или выбрать пункт в меню.",
        reply_markup=main_menu()
    )

# =============== ИНТЕНТЫ-КНОПКИ ===============
@bot.message_handler(func=lambda msg: msg.text == "🚑 У меня ошибка")
def btn_error(m):
    save_state(m.from_user.id, intent="error", step="ask_issue", data={"session_free_talk": 0})
    bot.send_message(
        m.chat.id,
        "Расскажи коротко, *что именно* не так. Можно свободно — я помогу сформулировать конкретно.",
        reply_markup=types.ReplyKeyboardRemove()
    )

@bot.message_handler(func=lambda msg: msg.text == "🤔 Просто поговорить")
def btn_chat(m):
    save_state(m.from_user.id, intent="free", step="warmup", data={"session_free_talk": 0})
    bot.send_message(m.chat.id, "Окей. Что сейчас болит в трейдинге?", reply_markup=types.ReplyKeyboardRemove())

@bot.message_handler(func=lambda msg: msg.text == "🧩 Хочу стратегию")
def btn_strategy(m):
    save_state(m.from_user.id, intent="strategy", step="intro")
    bot.send_message(m.chat.id, "Соберём основу твоей ТС в 2 шага. Сначала — подход/таймфреймы/вход. Поехали?")

@bot.message_handler(func=lambda msg: msg.text == "📄 Паспорт")
def btn_passport(m):
    save_state(m.from_user.id, intent="passport", step="intro")
    bot.send_message(m.chat.id, "Паспорт трейдера. Давай начнём с рынков/инструментов, где ты работаешь?")

@bot.message_handler(func=lambda msg: msg.text == "🗒 Панель недели")
def btn_week(m):
    save_state(m.from_user.id, intent="week", step="focus")
    bot.send_message(m.chat.id, "Фокус недели: какой один узел/навык стоит усилить в ближайшие 5–7 дней?")

@bot.message_handler(func=lambda msg: msg.text == "🆘 Экстренно")
def btn_panic(m):
    save_state(m.from_user.id, intent="panic", step="run")
    bot.send_message(
        m.chat.id,
        "Стоп-протокол: 1) Пауза 2 мин; 2) Закрой терминал/вкладку; 3) 10 медленных вдохов; "
        "4) Запиши триггер; 5) Вернись к плану сделки или закрой по правилу.",
        reply_markup=main_menu()
    )

# =============== СВОБОДНЫЙ СТАРТ / РЕЗЮМЕ К ПРОБЛЕМЕ ===============
def try_converge_issue(uid: int, user_text: str) -> Optional[str]:
    """
    Пытаемся прийти к формулировке на уровне поведения:
    - если уже конкретно — вернём как есть;
    - если слишком абстрактно — вернём None (нужно задать точняк).
    """
    if looks_concrete_behavior(user_text):
        return summarize_to_behavior(user_text)
    if too_abstract(user_text):
        return None
    # если не абстрактно, но и не очень конкретно — всё равно попробуем как есть
    if len(user_text.split()) >= 4:
        return summarize_to_behavior(user_text)
    return None

def ask_for_concrete(m, msg="Чуть конкретнее, пожалуйста: что именно делаешь в момент ошибки? (глаголами)"):
    bot.send_message(m.chat.id, msg)

# =============== ГЛАВНЫЙ ХЕНДЛЕР ТЕКСТА ===============
@bot.message_handler(content_types=["text"])
def on_text(m):
    uid = m.from_user.id
    st = get_state(uid)
    intent = st.get("intent") or "idle"
    step = st.get("step")
    data = st.get("data", {})
    free_turns = int(data.get("session_free_talk", 0))

    # Входное знакомство
    if intent == "greet":
        # если ещё не выбрали форму — перехватим «ты/вы» хендлером выше; иначе привет уже сделан
        return

    # Свободный диалог для входа
    if intent in ("free", "error") and step in (None, "warmup", "ask_issue"):
        # 1) Дать выговориться 1–2 раза, поддакивать и уточнять
        if free_turns < 2 and not looks_concrete_behavior(m.text):
            save_state(uid, data={"session_free_talk": free_turns + 1})
            # Мягкая поддержка + открытый вопрос
            bot.send_message(
                m.chat.id,
                "Понимаю. Продолжай — что именно мешает или запускает ошибку? "
                "Можно на примере одной недавней сделки."
            )
            return

        # 2) Пытаемся собрать конкретику
        candidate = try_converge_issue(uid, m.text)
        if candidate is None:
            ask_for_concrete(m)
            return

        # 3) Предлагаем своё резюме и просим подтвердить/добавить
        candidate = candidate[0].upper() + candidate[1:]
        save_state(uid, intent="error", step="confirm_issue", data={"issue_text": candidate})
        bot.send_message(
            m.chat.id,
            f"Зафиксирую так: *{candidate}*.\nПодходит?",
            reply_markup=yes_no_add_kb()
        )
        return

    # Подтверждение/уточнение проблемы
    if intent == "error" and step == "confirm_issue":
        txt = (m.text or "").lower()
        if "✅" in txt or "ок" in txt or "да" in txt:
            # done-условие достигнуто — двигаемся к разбору, не называя техник
            save_state(uid, step="ask_context")
            bot.send_message(m.chat.id, "Ок. В какой ситуации это обычно происходит? Что предшествует?")
            return
        if "✏" in txt or "добав" in txt:
            save_state(uid, step="ask_issue")
            bot.send_message(m.chat.id, "Добавь/уточни, пожалуйста. Что важно учесть?")
            return
        if "❌" in txt or "не то" in txt or "нет" in txt:
            save_state(uid, step="ask_issue", data={"issue_text": None})
            bot.send_message(m.chat.id, "Понял. Давай ещё раз коротко: что именно происходит не так?")
            return
        # любое другое — трактуем как уточнение
        merged = summarize_to_behavior(m.text)
        save_state(uid, step="confirm_issue", data={"issue_text": merged})
        bot.send_message(m.chat.id, f"Правлю формулировку: *{merged}*. Подходит?", reply_markup=yes_no_add_kb())
        return

    # Последовательность уточнений после подтверждённой проблемы (без терминов)
    if intent == "error" and step == "ask_context":
        save_state(uid, step="ask_emotions", data={"ctx": m.text.strip()})
        bot.send_message(m.chat.id, "Что чувствуешь в такие моменты? (несколько слов)")
        return

    if intent == "error" and step == "ask_emotions":
        save_state(uid, step="ask_thoughts", data={"emo": m.text.strip()})
        bot.send_message(m.chat.id, "Какие мысли в голове? Можешь прямо цитатами.")
        return

    if intent == "error" and step == "ask_thoughts":
        save_state(uid, step="ask_behavior", data={"thoughts": m.text.strip()})
        bot.send_message(m.chat.id, "Что конкретно делаешь? Опиши действия глаголами.")
        return

    if intent == "error" and step == "ask_behavior":
        # Резюме-паттерн + переход к цели
        info = get_state(uid).get("data", {})
        issue = info.get("issue_text", "ошибка")
        ctx   = info.get("ctx", "")
        emo   = info.get("emo", "")
        th    = info.get("thoughts", "")
        beh   = m.text.strip()

        pattern = f"Похоже на связку: *{beh}* ← чувства (*{emo or '…'}*) ← мысли (*{th or '…'}*) в контексте (*{ctx or '…'}*)."
        bot.send_message(m.chat.id, f"Вижу паттерн. {pattern}")

        save_state(uid, step="ask_goal", data={"behavior": beh})
        bot.send_message(m.chat.id, "Как звучит желаемое поведение вместо этого? (коротко, наблюдаемо)")
        return

    if intent == "error" and step == "ask_goal":
        goal = m.text.strip()
        # Мини-проверка «наблюдаемости» — просто просим упомянуть действие
        if not looks_concrete_behavior(goal):
            bot.send_message(m.chat.id, "Сформулируй как наблюдаемое действие. Например: «дожидаюсь полного сигнала и не двигаю стоп/тейк»")
            return
        save_state(uid, step="ops", data={"goal": goal})
        bot.send_message(m.chat.id, "Ок. Какие 2–3 шага помогут удерживать это поведение? (чек-лист)")
        return

    if intent == "error" and step == "ops":
        ops = m.text.strip()
        save_state(uid, step="check", data={"ops": ops})
        bot.send_message(m.chat.id, "Как поймёшь, что получилось? (критерий: например, «3 сделки подряд без сдвига стопа»)")
        return

    if intent == "error" and step == "check":
        check = m.text.strip()
        save_state(uid, step=None, intent="idle", data={"check": check})
        bot.send_message(
            m.chat.id,
            "Готово. Зафиксировал формулировку ошибки, паттерн и новую цель с критериями. "
            "Можем добавить это в фокус недели или перейти к следующей теме.",
            reply_markup=main_menu()
        )
        return

    # Если не попали ни в один сценарий — мягкий ответ + меню
    bot.send_message(m.chat.id, "Принял. Можем продолжить разговор или выбрать пункт в меню ниже.", reply_markup=main_menu())

# =============== FLASK / WEBHOOK ===============
app = Flask(__name__)

@app.get("/")
def root():
    return "OK"

@app.get("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now(timezone.utc).isoformat()})

@app.post(f"/{WEBHOOK_PATH}")
def webhook():
    if TG_SECRET and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TG_SECRET:
        abort(401)
    # ограничение размера
    MAX_BODY = 1_000_000
    if request.content_length and request.content_length > MAX_BODY:
        abort(413)
    update = request.get_data().decode("utf-8")
    bot.process_new_updates([types.Update.de_json(update)])
    return "OK"

# =============== ЛОКАЛЬНЫЙ ЗАПУСК (polling) ===============
def start_polling():
    try:
        bot.remove_webhook()
        logging.info("Webhook removed (ok)")
    except Exception as e:
        logging.warning(f"Webhook remove warn: {e}")
    logging.info("Starting polling…")
    bot.infinity_polling(timeout=30, long_polling_timeout=30, skip_pending=True)

if __name__ == "__main__":
    mode = os.getenv("RUN_MODE", "webhook")  # "polling" для локальной отладки
    if mode == "polling":
        start_polling()
    else:
        # только вебхук на Render
        port = int(os.getenv("PORT", "10000"))
        logging.info(f"Starting Flask (webhook={WEBHOOK_URL})…")
        app.run(host="0.0.0.0", port=port)
