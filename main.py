# main.py
import os, json, logging, time
from datetime import datetime
from typing import Optional, Dict, Any

from flask import Flask, request, abort, jsonify
from telebot import TeleBot, types
from telebot.types import Update

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError, OperationalError

from openai import OpenAI

# ---------- ЛОГИ ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("innertrade")

# ---------- ENV ----------
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY")
DATABASE_URL       = os.getenv("DATABASE_URL")
PUBLIC_URL         = os.getenv("PUBLIC_URL")
WEBHOOK_PATH       = os.getenv("WEBHOOK_PATH")  # напр. wbhk_9t3x
TG_WEBHOOK_SECRET  = os.getenv("TG_WEBHOOK_SECRET")
OPENAI_MODEL       = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

for k, v in {
    "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
    "OPENAI_API_KEY": OPENAI_API_KEY,
    "DATABASE_URL": DATABASE_URL,
    "PUBLIC_URL": PUBLIC_URL,
    "WEBHOOK_PATH": WEBHOOK_PATH,
    "TG_WEBHOOK_SECRET": TG_WEBHOOK_SECRET,
}.items():
    if not v:
        raise RuntimeError(f"{k} is missing")

# ---------- OPENAI ----------
client = OpenAI(api_key=OPENAI_API_KEY)

def gpt_coach(system: str, user: str) -> str:
    """
    Короткий «коуч-ответ» чтобы мягко поддержать оффтоп и вернуть на шаг.
    """
    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role":"system","content": system},
                {"role":"user","content": user}
            ],
            temperature=0.4,
            max_tokens=300,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        log.warning(f"OpenAI fallback: {e}")
        # безопасный дефолт
        return "Понял ваш вопрос. Коротко отвечу и вернёмся к шагу, чтобы аккуратно продвинуться."

# ---------- DB ----------
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=180,
    pool_size=5,
    max_overflow=5,
)

def db_ok() -> bool:
    try:
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception as e:
        log.error(f"DB check failed: {e}")
        return False

def ensure_user(uid: int):
    with engine.begin() as c:
        c.execute(text("""
            INSERT INTO users(user_id) VALUES (:uid)
            ON CONFLICT (user_id) DO NOTHING
        """), {"uid": uid})

def get_state(uid: int) -> Dict[str, Any]:
    with engine.begin() as c:
        r = c.execute(text("SELECT intent, step, COALESCE(data,'{}'::jsonb) AS data FROM user_state WHERE user_id=:u"), {"u": uid}).mappings().first()
        if not r:
            c.execute(text("""
                INSERT INTO user_state(user_id, intent, step, data)
                VALUES (:u, 'idle', NULL, '{}'::jsonb)
                ON CONFLICT (user_id) DO NOTHING
            """), {"u": uid})
            return {"intent":"idle","step":None,"data":{}}
        return {"intent": r["intent"], "step": r["step"], "data": r["data"]}

def set_state(uid: int, intent: str, step: Optional[str], data: Optional[Dict[str,Any]] = None):
    with engine.begin() as c:
        c.execute(text("""
            INSERT INTO user_state(user_id, intent, step, data)
            VALUES (:u, :i, :s, COALESCE(:d, '{}'::jsonb))
            ON CONFLICT (user_id) DO UPDATE
            SET intent=EXCLUDED.intent,
                step=EXCLUDED.step,
                data=EXCLUDED.data,
                updated_at=now()
        """), {"u": uid, "i": intent, "s": step, "d": json.dumps(data or {})})

def upsert_error_row(uid: int, row_id: Optional[int], fields: Dict[str, Any]) -> int:
    """
    Создаём или обновляем запись в errors. Возвращаем id.
    """
    with engine.begin() as c:
        if row_id:
            sets = ", ".join([f"{k}=:{k}" for k in fields.keys()])
            params = {"id": row_id, **fields}
            c.execute(text(f"UPDATE errors SET {sets} WHERE id=:id"), params)
            return row_id
        else:
            cols = ", ".join(fields.keys())
            vals = ", ".join([f":{k}" for k in fields.keys()])
            params = {"uid": uid, **fields}
            r = c.execute(text(f"""
                INSERT INTO errors(user_id, {cols}) VALUES (:uid, {vals})
                RETURNING id
            """), params).first()
            return int(r[0])

# ---------- TELEGRAM ----------
bot = TeleBot(TELEGRAM_TOKEN, parse_mode="Markdown")

def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🚑 У меня ошибка", "🧩 Хочу стратегию")
    kb.row("📄 Паспорт", "🗒 Панель недели")
    kb.row("🆘 Экстренно: поплыл", "🤔 Не знаю, с чего начать")
    kb.row("📊 Статус")
    return kb

# ------- Валидация/правила урока 1 -------
BEHAVIOR_VERBS = [
    "вхожу","захожу","переворачиваюсь","усредняюсь","двигаю","перетаскиваю",
    "закрываю","фиксирую","добавляю","пропускаю","пересиживаю","выставляю"
]
VAGUE_MARKERS = ["иногда","бывает","в какие-то дни","определённые дни","как-то","что-то","часто"]

def looks_like_behavior(text: str) -> bool:
    t = text.lower()
    if len(t.split()) < 3:
        return False
    if any(v in t for v in BEHAVIOR_VERBS):
        return True
    return False

def looks_vague(text: str) -> bool:
    t = text.lower()
    return any(v in t for v in VAGUE_MARKERS)

def ask_next_mercedes_step(step: str, chat_id: int):
    prompts = {
        "ctx": "КОНТЕКСТ. В какой ситуации это обычно происходит? Что предшествует ошибке? (1–2 предложения)",
        "emo": "ЭМОЦИИ. Что чувствуешь в момент ошибки? Как ощущается в теле? (несколько слов)",
        "thoughts": "МЫСЛИ. Что говоришь себе в этот момент? (цитатами, 1–2 фразы)",
        "behavior": "ПОВЕДЕНИЕ. Что ты конкретно делаешь? Опиши действие глаголами (1–2 предложения).",
    }
    bot.send_message(chat_id, prompts[step], reply_markup=main_menu())

def mercedes_done(data: Dict[str, Any]) -> bool:
    # done: есть error_text + три связки: context/emotions/thoughts/behavior
    need = ["error_text","mer_ctx","mer_emo","mer_th","mer_beh"]
    return all(k in data and data[k] for k in need)

# ------- Команды -------
@bot.message_handler(commands=["start","menu","reset"])
def cmd_start(m):
    uid = m.from_user.id
    ensure_user(uid)
    set_state(uid, "idle", None, {})
    bot.send_message(
        m.chat.id,
        "👋 Привет! Я наставник *Innertrade*.\nВыбери пункт или напиши текст.\nКоманды: /status /ping",
        reply_markup=main_menu()
    )

@bot.message_handler(commands=["ping"])
def cmd_ping(m): bot.send_message(m.chat.id, "pong")

@bot.message_handler(commands=["status"])
def cmd_status(m):
    uid = m.from_user.id
    st = get_state(uid)
    # быстрая проверка БД
    ok = "ok" if db_ok() else "fail"
    bot.send_message(
        m.chat.id,
        f"📊 *Статус*\n"
        f"DB: {ok}\n"
        f"Intent: `{st['intent']}`\n"
        f"Step: `{st['step']}`",
        reply_markup=main_menu()
    )

# ------- Интенты главного меню -------
@bot.message_handler(func=lambda msg: msg.text == "🚑 У меня ошибка")
def intent_error(m):
    uid = m.from_user.id
    ensure_user(uid)
    set_state(uid, "lesson1_error", "ask_error", {"flow":"l1","current_error_id": None})
    bot.send_message(
        m.chat.id,
        "Опиши основную ошибку 1–2 предложениями *на уровне поведения/навыка*.\n"
        "Примеры: «вхожу до формирования сигнала», «двигаю стоп после входа», «закрываю по первой коррекции».",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🧩 Хочу стратегию")
def intent_strategy(m):
    uid = m.from_user.id
    set_state(uid, "strategy", None, {})
    bot.send_message(
        m.chat.id,
        "Окей, соберём ТС по конструктору:\n"
        "1) Цели\n2) Стиль\n3) Рынки/инструменты\n4) Правила входа/выхода\n5) Риск\n6) Сопровождение\n7) Тестирование",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "📄 Паспорт")
def intent_passport(m):
    uid = m.from_user.id
    set_state(uid, "passport", None, {})
    bot.send_message(
        m.chat.id,
        "Паспорт трейдера. 1/6) На каких рынках/инструментах торгуешь?",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🗒 Панель недели")
def intent_week_panel(m):
    uid = m.from_user.id
    set_state(uid, "week_panel", None, {})
    bot.send_message(
        m.chat.id,
        "Панель недели:\n• Фокус недели\n• План (3 шага)\n• Лимиты\n• Ритуалы\n• Короткая ретро в конце недели",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🆘 Экстренно: поплыл")
def intent_panic(m):
    uid = m.from_user.id
    set_state(uid, "panic", None, {})
    bot.send_message(
        m.chat.id,
        "Стоп-протокол:\n1) Пауза 2 мин\n2) Закрой терминал/вкладку\n3) 10 медленных вдохов\n"
        "4) Запиши триггер (что выбило)\n5) Вернись к плану сделки или закрой по правилу",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🤔 Не знаю, с чего начать")
def intent_start_help(m):
    uid = m.from_user.id
    set_state(uid, "start_help", None, {})
    bot.send_message(
        m.chat.id,
        "Предлагаю так:\n1) Заполним паспорт (1–2 мин)\n2) Выберем фокус недели\n3) Соберём скелет ТС\n"
        "С чего начнём — паспорт или фокус недели?",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "📊 Статус")
def intent_status_btn(m): return cmd_status(m)

# ------- Диалог урока 1 (MERCEDES → TOTE) -------
def coach_and_reask(chat_id: int, user_text: str, reask: str):
    coach = gpt_coach(
        "Ты коуч: отвечай коротко и по-доброму, поддержи вопрос, но верни к шагу. Русский язык.",
        f"Сообщение ученика: «{user_text}». Сформулируй 1–2 предложения поддержки и переход к уточняющему вопросу."
    )
    bot.send_message(chat_id, f"{coach}\n\n{reask}", reply_markup=main_menu())

@bot.message_handler(content_types=["text"])
def flow_router(m):
    uid = m.from_user.id
    st = get_state(uid)
    intent, step, data = st["intent"], st["step"], st["data"]

    # если не активный сценарий — мягкий фолбэк
    if intent not in ("lesson1_error",):
        # не ломаем диалог — дружелюбный ответ
        bot.send_message(
            m.chat.id,
            "Принял. Чтобы было быстрее, выбери пункт в меню ниже или напиши /menu.",
            reply_markup=main_menu()
        )
        return

    # ------- Урок 1 шаги -------
    txt = (m.text or "").strip()

    # A) ask_error (done: конкретная ошибка на уровне поведения/навыка)
    if step == "ask_error":
        # Если ученик задаёт встречный вопрос — поддержать и вернуть к конкретике
        if txt.endswith("?") or txt.lower().startswith(("а можно","можно","а ")):
            return coach_and_reask(
                m.chat.id, txt,
                "Сформулируй, пожалуйста, *ошибку на уровне поведения*: что именно ты *делаешь* (глаголом) — 1–2 предложения."
            )

        if not looks_like_behavior(txt) or looks_vague(txt):
            # мягкая конкретизация через GPT
            return coach_and_reask(
                m.chat.id, txt,
                "Давай сделаем конкретнее, чтобы это было *наблюдаемое действие*.\n"
                "Примеры: «вхожу до формирования сигнала», «двигаю стоп после входа», «закрываю по первой коррекции»."
            )

        # ок, сохраняем и создаём/обновляем errors
        data["error_text"] = txt
        err_id = data.get("current_error_id")
        err_id = upsert_error_row(uid, err_id, {"error_text": txt})
        data["current_error_id"] = err_id

        set_state(uid, "lesson1_error", "ask_mer_ctx", data)
        bot.send_message(m.chat.id, "Ок. Пойдём по MERCEDES кратко, чтобы увидеть паттерн.")
        ask_next_mercedes_step("ctx", m.chat.id)
        return

    # B) MERCEDES — context
    if step == "ask_mer_ctx":
        if txt.endswith("?"):
            return coach_and_reask(m.chat.id, txt, "Опиши *ситуацию/расклад* до ошибки — 1–2 предложения.")
        data["mer_ctx"] = txt
        set_state(uid, "lesson1_error", "ask_mer_emo", data)
        ask_next_mercedes_step("emo", m.chat.id)
        return

    # C) emotions
    if step == "ask_mer_emo":
        if txt.endswith("?"):
            return coach_and_reask(m.chat.id, txt, "Несколько слов про эмоции/телесные ощущения в момент ошибки.")
        data["mer_emo"] = txt
        set_state(uid, "lesson1_error", "ask_mer_th", data)
        ask_next_mercedes_step("thoughts", m.chat.id)
        return

    # D) thoughts
    if step == "ask_mer_th":
        if txt.endswith("?"):
            return coach_and_reask(m.chat.id, txt, "Запиши 1–2 короткие фразы-мысли, которые звучат в момент ошибки.")
        data["mer_th"] = txt
        set_state(uid, "lesson1_error", "ask_mer_beh", data)
        ask_next_mercedes_step("behavior", m.chat.id)
        return

    # E) behavior
    if step == "ask_mer_beh":
        if txt.endswith("?"):
            return coach_and_reask(m.chat.id, txt, "Опиши *действие* глаголами: что конкретно делаешь.")
        data["mer_beh"] = txt

        # DONE по Mercedes?
        if not mercedes_done(data):
            # маловероятно, но подстраховка
            return coach_and_reask(m.chat.id, txt, "Ещё чуть-чуть конкретики, чтобы собрать паттерн целиком.")

        # Соберём краткое резюме-паттерн
        pattern = f"Паттерн: контекст «{data['mer_ctx']}» → эмоции «{data['mer_emo']}» → мысли «{data['mer_th']}» → поведение «{data['mer_beh']}»."
        data["pattern"] = pattern

        # обновим запись errors
        err_id = data.get("current_error_id")
        upsert_error_row(uid, err_id, {
            "pattern_behavior": data["mer_beh"],
            "pattern_emotion":  data["mer_emo"],
            "pattern_thought":  data["mer_th"],
        })

        set_state(uid, "lesson1_error", "ask_goal_new", data)
        bot.send_message(
            m.chat.id,
            f"Резюме:\n{pattern}\n\nТеперь сформулируем *новую цель* в позитивной форме (на уровне поведения)."
        )
        bot.send_message(
            m.chat.id,
            "Например: «Вхожу только при полном сигнале и *не трогаю* стоп/тейк до развязки».",
            reply_markup=main_menu()
        )
        return

    # F) Новая цель
    if step == "ask_goal_new":
        if txt.endswith("?"):
            return coach_and_reask(m.chat.id, txt, "Запиши цель как *желаемое действие* (что делаешь/не делаешь).")
        data["goal_new"] = txt

        err_id = data.get("current_error_id")
        upsert_error_row(uid, err_id, {"positive_goal": txt})

        set_state(uid, "lesson1_error", "tote_goal", data)
        bot.send_message(m.chat.id, "Перейдём к *TOTE*. Сначала *Test 1*: сформулируй коротко цель-критерий на ближайшие 3 сделки.")
        bot.send_message(m.chat.id, "Например: «В 3 следующих сделках не двигаю стоп/тейк после входа».")
        return

    # G) TOTE goal
    if step == "tote_goal":
        data["tote_goal"] = txt
        err_id = data.get("current_error_id")
        upsert_error_row(uid, err_id, {"tote_goal": txt})

        set_state(uid, "lesson1_error", "tote_ops", data)
        bot.send_message(m.chat.id, "Операции (*Operate*): перечисли 2–4 шага, которые помогут удерживать цель (чек-лист, пауза, таймер и т.д.).")
        return

    # H) TOTE ops
    if step == "tote_ops":
        data["tote_ops"] = txt
        err_id = data.get("current_error_id")
        upsert_error_row(uid, err_id, {"tote_ops": txt})

        set_state(uid, "lesson1_error", "tote_check", data)
        bot.send_message(m.chat.id, "Проверка (*Test 2*): как поймёшь, что цель удержана? (критерий «да/нет», на 3 сделки).")
        return

    # I) TOTE check
    if step == "tote_check":
        data["tote_check"] = txt
        err_id = data.get("current_error_id")
        upsert_error_row(uid, err_id, {"tote_check": txt})

        set_state(uid, "lesson1_error", "tote_exit", data)
        bot.send_message(m.chat.id, "Выход (*Exit*): что подведёшь в итогах? Если «да» — фиксируешь успех. Если «нет» — что меняешь в следующем цикле?")
        return

    # J) TOTE exit -> финал урока 1
    if step == "tote_exit":
        data["tote_exit"] = txt
        err_id = data.get("current_error_id")
        upsert_error_row(uid, err_id, {"tote_exit": txt})

        # мини-чеклисты
        checklist_pre = "- Проверил: сетап 100%\n- Пауза 10–20 сек\n- Я в ресурсе\n- План сопровождения открыт"
        checklist_post = "- Не двигал стоп/тейк\n- Выполнил план\n- Итог записан"

        upsert_error_row(uid, err_id, {"checklist_pre": checklist_pre, "checklist_post": checklist_post})

        # сброс шага
        set_state(uid, "lesson1_error", None, data)
        bot.send_message(
            m.chat.id,
            "✅ Урок 1 зафиксирован.\n"
            "Чек-листы:\n*Перед входом*\n" + checklist_pre + "\n\n*После входа*\n" + checklist_post,
            reply_markup=main_menu()
        )
        return

# ---------- FLASK / WEBHOOK ----------
app = Flask(__name__)
START_TS = time.time()
MAX_BODY = 1_000_000  # ~1MB

@app.get("/")
def root():
    return "OK"

@app.get("/health")
def health():
    return jsonify({
        "status":"ok",
        "time": datetime.utcnow().isoformat()+"Z",
        "uptime_sec": int(time.time()-START_TS),
        "db": "ok" if db_ok() else "fail"
    })

@app.post(f"/{WEBHOOK_PATH}")
def webhook():
    # секрет + лимит тела
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TG_WEBHOOK_SECRET:
        abort(401)
    if request.content_length and request.content_length > MAX_BODY:
        abort(413)
    raw = request.get_data().decode("utf-8")
    try:
        update = Update.de_json(raw)
    except Exception:
        # TeleBot ожидает dict -> используем json.loads
        update = Update.de_json(json.loads(raw))
    try:
        bot.process_new_updates([update])
    except Exception as e:
        log.exception(f"process_new_updates error: {e}")
    return "ok"

def install_webhook():
    # Снимем старый и поставим новый с секретом
    try:
        bot.remove_webhook()
    except Exception as e:
        log.warning(f"remove_webhook warn: {e}")

    url = f"{PUBLIC_URL.rstrip('/')}/{WEBHOOK_PATH}"
    ok = bot.set_webhook(
        url=url,
        secret_token=TG_WEBHOOK_SECRET,
        allowed_updates=["message","callback_query"],
        drop_pending_updates=True,
        max_connections=40
    )
    if ok:
        log.info(f"Webhook set to {url}")
    else:
        log.error("Failed to set webhook")

if __name__ == "__main__":
    install_webhook()
    port = int(os.getenv("PORT", "10000"))
    log.info(f"Starting Flask on 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port)
