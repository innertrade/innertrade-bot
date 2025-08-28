# main.py
import os, json, logging, re, threading
from datetime import datetime, timezone
from flask import Flask, request, jsonify, abort
from telebot import TeleBot, types
from openai import OpenAI
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

# ---------- ЛОГИ ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# ---------- ENV ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DATABASE_URL   = os.getenv("DATABASE_URL")  # optional
PUBLIC_URL     = os.getenv("PUBLIC_URL")    # e.g. https://innertrade-bot.onrender.com
WEBHOOK_PATH   = os.getenv("WEBHOOK_PATH", "webhook")
TG_WEBHOOK_SECRET = os.getenv("TG_WEBHOOK_SECRET")

required = {
    "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
    "OPENAI_API_KEY": OPENAI_API_KEY,
    "PUBLIC_URL": PUBLIC_URL,
    "TG_WEBHOOK_SECRET": TG_WEBHOOK_SECRET
}
missing = [k for k,v in required.items() if not v]
if missing:
    raise RuntimeError(f"Missing env vars: {', '.join(missing)}")

# ---------- OPENAI ----------
oa = OpenAI(api_key=OPENAI_API_KEY)

# ---------- DB ----------
engine = None
if DATABASE_URL:
    try:
        engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
        with engine.connect() as conn:
            # user_state таблица уже есть в твоей схеме; создаём на всякий случай
            conn.execute(text("""
            CREATE TABLE IF NOT EXISTS user_state (
              user_id BIGINT PRIMARY KEY,
              intent  TEXT,
              step    TEXT,
              data    JSONB,
              updated_at TIMESTAMPTZ DEFAULT now()
            );
            """))
        logging.info("DB connected & migrated")
    except OperationalError as e:
        logging.warning(f"DB not available yet: {e}")
        engine = None
else:
    logging.info("DATABASE_URL not set — running without DB persistence")

def load_state(user_id: int) -> dict:
    if not engine: return {"intent":"greet","step":"warmup","data":{}}
    with engine.begin() as conn:
        row = conn.execute(text("SELECT intent, step, data FROM user_state WHERE user_id=:u"),
                           {"u": user_id}).mappings().first()
        if not row:
            conn.execute(text("""
                INSERT INTO user_state(user_id, intent, step, data)
                VALUES (:u, 'greet', 'warmup', '{}'::jsonb)
                ON CONFLICT (user_id) DO NOTHING
            """), {"u": user_id})
            return {"intent":"greet","step":"warmup","data":{}}
        return {"intent": row["intent"] or "greet",
                "step": row["step"] or "warmup",
                "data": row["data"] or {}}

def save_state(user_id: int, intent: str=None, step: str=None, data: dict=None):
    if not engine: return
    cur = load_state(user_id)
    if intent is None: intent = cur["intent"]
    if step   is None: step   = cur["step"]
    merged = cur["data"].copy()
    if data: merged.update(data)
    with engine.begin() as conn:
        conn.execute(text("""
        INSERT INTO user_state(user_id, intent, step, data, updated_at)
        VALUES (:u, :i, :s, :d, now())
        ON CONFLICT (user_id) DO UPDATE
        SET intent=:i, step=:s, data=:d, updated_at=now()
        """), {"u": user_id, "i": intent, "s": step, "d": json.dumps(merged)})

# ---------- UI ----------
bot = TeleBot(TELEGRAM_TOKEN, parse_mode="Markdown")

def main_menu(address="ты"):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🚑 У меня ошибка", "🧩 Хочу стратегию")
    kb.row("📄 Паспорт", "🗒 Панель недели")
    kb.row("🆘 Экстренно: поплыл", "🤔 Не знаю, с чего начать")
    if address == "вы":
        # ничего особого — просто пример на будущее
        pass
    return kb

# ---------- НАТУРАЛЬНЫЙ ДИАЛОГ / ХЕЛПЕРЫ ----------
INTENT_FREE = "free_talk"     # свободное общение
INTENT_ERROR = "error_flow"   # разбор ошибки

def user_address(data: dict, msg) -> str:
    # address: "ты"|"вы". По умолчанию "ты".
    return (data or {}).get("address") or "ты"

def reflect_and_question(text_in: str, address: str) -> str:
    # Короткая эмпатия + один вопрос (без давления).
    # Не упоминаем названия техник.
    sys = (
        "Ты — коуч-наставник по трейдингу. Говори тепло и просто, одно короткое уточняющее "
        "вопросительное предложение. Не навязывай шаги курса. Не задавай длинные списки. "
        "Если пользователь назвал проблему, отзеркаль её одной фразой и задай один мягкий вопрос."
    )
    usr = f"Адрес обращения: {address}. Сообщение пользователя: «{text_in}»"
    try:
        r = oa.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role":"system","content":sys},
                {"role":"user","content":usr}
            ],
            temperature=0.4,
            max_tokens=180
        )
        return r.choices[0].message.content.strip()
    except Exception:
        # Фолбэк
        q = "Что именно в этом волнует больше всего?" if address=="ты" else "Что именно в этом волнует вас больше всего?"
        return q

behavior_verbs = re.compile(r"\b(вхожу|войти|закрываю|закрыть|двигаю|двинул|усредняю|усреднить|вмешиваюсь|вмешаться|завышаю|пересиживаю|пересидеть)\b", re.IGNORECASE)
common_markers = re.compile(r"\b(просадк|стоп|тейк|правил|нарушаю|сетап|ран(о|ьше)|поторопил|паник|страх)\w*", re.IGNORECASE)

def detect_problem_statement(text_in: str) -> bool:
    # Done-условие для фиксации ошибки: есть глагол поведения ИЛИ маркеры
    return bool(behavior_verbs.search(text_in) or common_markers.search(text_in)) and len(text_in.split()) >= 3

def paraphrase_problem(text_in: str, address: str) -> str:
    # Короткая, конкретная переформулировка на уровне поведения/навыка; без названий техник
    sys = ("Сделай одну короткую конкретную формулировку торговой проблемы на уровне поведения/навыка. "
           "Не давай советы, не объясняй теории. Не пиши более 1 предложения.")
    r = oa.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role":"system","content":sys},
            {"role":"user","content":f"Адрес: {address}. Текст: {text_in}"}
        ],
        temperature=0.2,
        max_tokens=80
    )
    return r.choices[0].message.content.strip().strip(" .") + "."

def confirm_kb():
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("Да, так и есть", callback_data="confirm_problem_yes"),
        types.InlineKeyboardButton("Не совсем / уточнить", callback_data="confirm_problem_no"),
    )
    return kb

# ---------- КОМАНДЫ ----------
@bot.message_handler(commands=["start","menu","reset"])
def cmd_start(m):
    st = load_state(m.from_user.id)
    # Сброс в «тёплое» приветствие, имя + как обращаться
    data = st["data"]
    # Автозаполняем имя из Telegram, если нет
    if not data.get("name"):
        data["name"] = m.from_user.first_name or "друг"
    # адрес пока неизвестен — спросим
    data.pop("address", None)
    save_state(m.from_user.id, intent="greet", step="warmup", data=data)
    name = data["name"]
    bot.send_message(
        m.chat.id,
        f"👋 Привет, {name}! Давай знакомиться. Как удобнее обращаться — *ты* или *вы*?",
        reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True).add("ты","вы")
    )

@bot.message_handler(commands=["ping"])
def cmd_ping(m): bot.send_message(m.chat.id, "pong")

@bot.message_handler(commands=["status"])
def cmd_status(m):
    st = load_state(m.from_user.id)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    resp = {
        "ok": True,
        "time": now,
        "intent": st["intent"],
        "step": st["step"],
        "db": "ok" if engine else "no-db"
    }
    bot.send_message(m.chat.id, f"```\n{json.dumps(resp, ensure_ascii=False, indent=2)}\n```", parse_mode="Markdown")

# ---------- ОТВЕТ НА КНОПКИ ГЛАВНОГО МЕНЮ ----------
def goto_error_flow(m, st):
    addr = user_address(st["data"], m)
    save_state(m.from_user.id, intent=INTENT_ERROR, step="ask_problem")
    bot.send_message(
        m.chat.id,
        "Окей, давай разберём твою текущую трудность. Коротко опиши её 1–2 предложениями.",
        reply_markup=main_menu(addr)
    )

@bot.message_handler(func=lambda msg: msg.text in ["🚑 У меня ошибка","🧩 Хочу стратегию","📄 Паспорт","🗒 Панель недели","🆘 Экстренно: поплыл","🤔 Не знаю, с чего начать"])
def on_menu_click(m):
    st = load_state(m.from_user.id)
    addr = user_address(st["data"], m)
    t = m.text
    if t == "🚑 У меня ошибка":
        goto_error_flow(m, st)
    elif t == "🆘 Экстренно: поплыл":
        bot.send_message(m.chat.id,
            "Стоп-протокол:\n1) Пауза 2 минуты\n2) Закрой терминал/вкладку\n3) 10 медленных вдохов\n"
            "4) Запиши триггер (что выбило)\n5) Вернись к плану сделки или закрой по правилу",
            reply_markup=main_menu(addr))
    elif t == "🤔 Не знаю, с чего начать":
        bot.send_message(m.chat.id,
            "Предлагаю так: 1) коротко обозначим боль, 2) зафиксируем ближайшую цель, 3) намечаем шаги. С чего начнём?",
            reply_markup=main_menu(addr))
    elif t == "🗒 Панель недели":
        bot.send_message(m.chat.id,
            "Панель недели: • фокус недели • 1–2 цели • лимиты • короткие чек-ины • ретроспектива в конце.",
            reply_markup=main_menu(addr))
    elif t == "📄 Паспорт":
        bot.send_message(m.chat.id,
            "Паспорт трейдера: рынки/ТФ, стиль, риск-настройки, типовые ошибки, триггеры и рабочие ритуалы. Готов добавить позже.",
            reply_markup=main_menu(addr))
    elif t == "🧩 Хочу стратегию":
        bot.send_message(m.chat.id,
            "Соберём скелет ТС: 1) подход/ТФ/вход, 2) стоп/сопровождение/выход/риски. Начнём при следующем заходе.",
            reply_markup=main_menu(addr))

# ---------- ОБРАБОТКА ТЕКСТА (естественный диалог + шаги) ----------
@bot.message_handler(content_types=["text"])
def on_text(m):
    st = load_state(m.from_user.id)
    data = st["data"]; addr = user_address(data, m)
    txt = (m.text or "").strip().lower()

    # 1) первичное определение обращения "ты/вы"
    if st["intent"] == "greet" and st["step"] == "warmup":
        if txt in ["ты","вы"]:
            data["address"] = txt
            # имя уже есть, отвечаем тепло и даём свободный вход
            save_state(m.from_user.id, intent=INTENT_FREE, step="warmup_1", data=data)
            sal = "Принято." if txt=="вы" else "Окей."
            bot.send_message(m.chat.id,
                f"{sal} Можем спокойно поговорить — расскажи, что сейчас болит в торговле. "
                f"Если хочешь, снизу есть меню.", reply_markup=main_menu(txt))
            return
        # если спросили "как тебя зовут?"
        if "как тебя зовут" in m.text.lower():
            bot.send_message(m.chat.id, "Я — Kai. Можно просто Кай 🙂")
            return
        # если ответили не «ты/вы» — мягко переспросим
        bot.send_message(m.chat.id, "Напиши, пожалуйста, «ты» или «вы».")
        return

    # 2) свободная беседа (до 2–3 тёплых заходов) с авто-детектом проблемы
    if st["intent"] == INTENT_FREE:
        # если прямой вопрос «как тебя зовут»
        if "как тебя зовут" in m.text.lower():
            bot.send_message(m.chat.id, "Я — Kai. Можно просто Кай.")
            return

        # подбираем мягкий ответ
        reply = reflect_and_question(m.text, addr)

        # копим warmup count
        wc = int(data.get("warmup_count", 0)) + 1
        data["warmup_count"] = wc

        # если пользователь уже дал годную формулировку — предлагаем подтвердить
        if detect_problem_statement(m.text):
            short = paraphrase_problem(m.text, addr)
            save_state(m.from_user.id, intent=INTENT_ERROR, step="confirm_problem",
                       data={**data, "problem_candidate": short})
            bot.send_message(m.chat.id, f"Зафиксирую так: *{short}*\nПодходит?", reply_markup=confirm_kb())
            return

        # после 2 заходов — мягко предложить обозначить конкретнее и перейти
        if wc >= 2:
            tip = "Если ок, сформулируй в одном предложении: что именно ты обычно *делаешь* не так (на уровне действия)."
            bot.send_message(m.chat.id, f"{reply}\n\n{tip}", reply_markup=main_menu(addr))
        else:
            bot.send_message(m.chat.id, reply, reply_markup=main_menu(addr))
        save_state(m.from_user.id, data=data)
        return

    # 3) поток «разбор ошибки»
    if st["intent"] == INTENT_ERROR:
        step = st["step"]

        # A) шаг: спросить проблему, принять свободный текст, попытаться конкретизировать, подтвердить
        if step == "ask_problem":
            if not detect_problem_statement(m.text):
                hint = "Опиши, *что именно делаешь* или *как это проявляется* в сделке (1–2 предложения)."
                bot.send_message(m.chat.id, f"Понял. Дай, пожалуйста, чуть конкретнее. {hint}",
                                 reply_markup=main_menu(addr))
                return
            short = paraphrase_problem(m.text, addr)
            save_state(m.from_user.id, step="confirm_problem", data={**data, "problem_candidate": short})
            bot.send_message(m.chat.id, f"Так сформулируем: *{short}*\nПодходит?", reply_markup=confirm_kb())
            return

        # B) после подтверждения — идём в мягкий опрос (без названий техник)
        if step == "context":
            bot.send_message(m.chat.id, "В какой ситуации это обычно происходит? Что предшествует? (1–2 предложения)",
                             reply_markup=main_menu(addr))
            save_state(m.from_user.id, step="context_wait")
            return
        if step == "context_wait":
            save_state(m.from_user.id, step="emotions", data={**data, "ctx": m.text})
            bot.send_message(m.chat.id, "Что чувствуешь в момент этой ошибки? (несколько слов)")
            return
        if step == "emotions":
            save_state(m.from_user.id, step="thoughts", data={**data, "emo": m.text})
            bot.send_message(m.chat.id, "Что говоришь себе в этот момент? (1–2 фразы)")
            return
        if step == "thoughts":
            save_state(m.from_user.id, step="behavior", data={**data, "thoughts": m.text})
            bot.send_message(m.chat.id, "И что именно ты делаешь? Опиши действие глаголами (1–2 предложения).")
            return
        if step == "behavior":
            # Итог мини-резюме без навязывания терминов
            data.update({"beh": m.text})
            problem = data.get("problem_confirmed") or data.get("problem_candidate") or "ошибка"
            resume = f"Резюме: {problem}\nКонтекст: {data.get('ctx','—')}\nЭмоции: {data.get('emo','—')}\nМысли: {data.get('thoughts','—')}\nПоведение: {data.get('beh','—')}"
            bot.send_message(m.chat.id, f"Ок, вижу картину.\n\n{resume}\n\nСформулируем новую цель одним предложением (что хочешь делать вместо прежнего поведения)?")
            save_state(m.from_user.id, step="new_goal", data=data)
            return
        if step == "new_goal":
            goal = m.text.strip()
            # мягкий переход в план (аналог TOTE, без названия)
            save_state(m.from_user.id, step="mini_plan", data={**data, "new_goal": goal})
            bot.send_message(m.chat.id, "Какие 2–3 шага помогут держаться этой цели в ближайших 3 сделках?")
            return
        if step == "mini_plan":
            plan = m.text.strip()
            goal = data.get("new_goal","Цель")
            bot.send_message(m.chat.id, f"Отлично. Цель: *{goal}*\nШаги: {plan}\n\nГотово. Можем добавить это в недельный фокус позже.")
            save_state(m.from_user.id, intent=INTENT_FREE, step="warmup_1", data=data)
            return

    # 4) общий фолбэк — естественный короткий ответ + приглашение к меню
    reply = reflect_and_question(m.text, addr)
    bot.send_message(m.chat.id, reply + "\n\n(Снизу есть меню на случай, если удобнее кнопками.)", reply_markup=main_menu(addr))

# ---------- CALLBACKS (подтверждение формулировки) ----------
@bot.callback_query_handler(func=lambda c: c.data in ["confirm_problem_yes","confirm_problem_no"])
def cb_confirm(call):
    st = load_state(call.from_user.id); data = st["data"]; addr = user_address(data, call)
    if call.data == "confirm_problem_yes":
        confirmed = data.get("problem_candidate") or "ошибка"
        save_state(call.from_user.id, intent=INTENT_ERROR, step="context",
                   data={**data, "problem_confirmed": confirmed})
        bot.answer_callback_query(call.id, "Зафиксировали.")
        bot.send_message(call.message.chat.id, "Принято. Двигаемся дальше шаг за шагом.", reply_markup=main_menu(addr))
    else:
        save_state(call.from_user.id, intent=INTENT_ERROR, step="ask_problem", data=data)
        bot.answer_callback_query(call.id, "Ок, уточним формулировку.")
        bot.send_message(call.message.chat.id, "Сформулируй по-другому в одном предложении, как ты это видишь.", reply_markup=main_menu(addr))

# ---------- FLASK (webhook, health) ----------
app = Flask(__name__)

@app.get("/health")
def health():
    return jsonify({"status":"ok","time":datetime.now(timezone.utc).isoformat()})

@app.post(f"/{WEBHOOK_PATH}")
def webhook():
    # Безопасность
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TG_WEBHOOK_SECRET:
        abort(401)
    if request.content_length and request.content_length > 1_000_000:
        abort(413)
    update = request.get_data().decode("utf-8", errors="ignore")
    try:
        bot.process_new_updates([types.Update.de_json(json.loads(update))])
    except Exception as e:
        logging.exception("update handling failed: %s", e)
    return "OK", 200

# ---------- СЕТАП ВЕБХУКА ----------
def setup_webhook():
    import requests
    url = f"{PUBLIC_URL}/{WEBHOOK_PATH}"
    r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook",
                     params={
                         "url": url,
                         "secret_token": TG_WEBHOOK_SECRET,
                         "allowed_updates": "message,callback_query",
                         "drop_pending_updates": "true"
                     }, timeout=10)
    logging.info("Webhook set resp: %s", r.text)

if __name__ == "__main__":
    # Установим вебхук при старте
    try:
        setup_webhook()
    except Exception as e:
        logging.warning("Webhook setup warn: %s", e)

    port = int(os.getenv("PORT","10000"))
    logging.info("Starting server on 0.0.0.0:%s", port)
    app.run(host="0.0.0.0", port=port)
