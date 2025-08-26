import os
import json
import logging
from datetime import datetime
from flask import Flask, request, abort, jsonify

from telebot import TeleBot, types
from telebot.types import Update

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

# ---------- ЛОГИ ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("innertrade")

# ---------- ENV ----------
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY")  # пока не используем в этом релизе
DATABASE_URL       = os.getenv("DATABASE_URL")

PUBLIC_URL         = os.getenv("PUBLIC_URL")      # https://<your-app>.onrender.com
WEBHOOK_PATH       = os.getenv("WEBHOOK_PATH")    # например: tg
TG_WEBHOOK_SECRET  = os.getenv("TG_WEBHOOK_SECRET")

for k, v, hint in [
    ("TELEGRAM_TOKEN", TELEGRAM_TOKEN, "BotFather token"),
    ("DATABASE_URL",   DATABASE_URL,   "Neon Postgres URL"),
    ("PUBLIC_URL",     PUBLIC_URL,     "e.g., https://innertrade-bot.onrender.com"),
    ("WEBHOOK_PATH",   WEBHOOK_PATH,   "short path like 'tg'"),
    ("TG_WEBHOOK_SECRET", TG_WEBHOOK_SECRET, "any random secret"),
]:
    if not v:
        raise RuntimeError(f"{k} missing ({hint})")

# ---------- DB ----------
engine = None
try:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
    with engine.connect() as conn:
        # Небольшие «страхующие» миграции под уже существующую схему
        conn.execute(text("""
        CREATE TABLE IF NOT EXISTS users (
          user_id BIGINT PRIMARY KEY,
          mode TEXT NOT NULL DEFAULT 'course',
          created_at TIMESTAMPTZ DEFAULT now(),
          updated_at TIMESTAMPTZ DEFAULT now()
        );
        """))
        conn.execute(text("""
        CREATE TABLE IF NOT EXISTS user_state (
          user_id BIGINT PRIMARY KEY,
          intent  TEXT,
          step    TEXT,
          data    JSONB,
          updated_at TIMESTAMPTZ DEFAULT now()
        );
        """))
        conn.execute(text("""
        CREATE TABLE IF NOT EXISTS errors (
          id BIGSERIAL PRIMARY KEY,
          user_id BIGINT NOT NULL,
          error_text TEXT NOT NULL,
          pattern_behavior  TEXT,
          pattern_emotion   TEXT,
          pattern_thought   TEXT,
          positive_goal     TEXT,
          tote_goal         TEXT,
          tote_ops          TEXT,
          tote_check        TEXT,
          tote_exit         TEXT,
          checklist_pre     TEXT,
          checklist_post    TEXT,
          created_at TIMESTAMPTZ DEFAULT now()
        );
        """))
    log.info("DB connected & basic ensure OK")
except OperationalError as e:
    raise RuntimeError(f"DB connection failed: {e}")

# ---------- HELPERS: DB STATE ----------
def ensure_user(uid: int):
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users(user_id) VALUES (:uid) ON CONFLICT (user_id) DO NOTHING"),
            {"uid": uid}
        )

def get_state(uid: int):
    ensure_user(uid)
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT intent, step, COALESCE(data, '{}'::jsonb) AS data FROM user_state WHERE user_id=:uid"),
            {"uid": uid}
        ).mappings().first()
        if not row:
            conn.execute(
                text("INSERT INTO user_state(user_id, intent, step, data) VALUES (:uid,'idle',NULL,'{}'::jsonb)"),
                {"uid": uid}
            )
            return {"intent": "idle", "step": None, "data": {}}
        return {"intent": row["intent"], "step": row["step"], "data": row["data"]}

def set_state(uid: int, *, intent=None, step=None, data_merge: dict | None = None):
    st = get_state(uid)
    new_intent = intent if intent is not None else st["intent"]
    new_step   = step   if step   is not None else st["step"]
    new_data   = st["data"] or {}
    if data_merge:
        new_data.update(data_merge)
    with engine.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO user_state(user_id, intent, step, data, updated_at)
            VALUES(:uid, :intent, :step, CAST(:data AS jsonb), now())
            ON CONFLICT (user_id) DO UPDATE
            SET intent=:intent, step=:step, data=CAST(:data AS jsonb), updated_at=now()
            """),
            {"uid": uid, "intent": new_intent, "step": new_step, "data": json.dumps(new_data)}
        )

def upsert_error_partial(uid: int, fields: dict) -> int:
    """
    Создаёт запись в errors при первом вызове (требуется error_text),
    далее обновляет переданные поля. Возвращает id записи.
    Идентификатор храним во временном user_state.data["current_error_id"].
    """
    st = get_state(uid)
    eid = st["data"].get("current_error_id")
    with engine.begin() as conn:
        if not eid:
            # создаём новую строку, нужен error_text
            if "error_text" not in fields or not fields["error_text"]:
                raise ValueError("error_text is required to start errors row")
            eid = conn.execute(
                text("""INSERT INTO errors(user_id, error_text) VALUES(:uid, :et) RETURNING id"""),
                {"uid": uid, "et": fields["error_text"]}
            ).scalar_one()
            # сохраняем в state
            set_state(uid, data_merge={"current_error_id": eid})
            fields = {k: v for k, v in fields.items() if k != "error_text"}
        # обновляем любые другие поля
        if fields:
            sets = ", ".join([f"{k}=:{k}" for k in fields.keys()])
            params = {"id": eid, **fields}
            conn.execute(text(f"UPDATE errors SET {sets} WHERE id=:id"), params)
    return eid

# ---------- TELEGRAM ----------
bot = TeleBot(TELEGRAM_TOKEN, parse_mode="Markdown")

def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🚑 У меня ошибка", "🧩 Хочу стратегию")
    kb.row("📄 Паспорт", "🗒 Панель недели")
    kb.row("🆘 Экстренно: поплыл", "🤔 Не знаю, с чего начать")
    return kb

# ---------- БЛОК УРОК 1: Ошибка → MERCEDES → TOTE ----------
# — Done-условие этапа: сформулирована проблема на уровне поведения/навыка + заполнены pattern_* + positive_goal + TOTE (goal/ops/check/exit).

MERCEDES_ORDER = [
    ("context",   "КОНТЕКСТ. В какой ситуации это обычно происходит? Что предшествует ошибке? (1–2 предложения)"),
    ("emotions",  "ЭМОЦИИ. Что чувствуешь в момент ошибки? Как это ощущается в теле? (несколько слов)"),
    ("thoughts",  "МЫСЛИ. Что говоришь себе в этот момент? (цитатами, 1–2 фразы)"),
    ("behavior",  "ПОВЕДЕНИЕ. Что ты конкретно делаешь? Опиши действие глаголами (1–2 предложения)."),
    # убеждения на старте НЕ копаем — оставим для урока 3
    ("state",     "СОСТОЯНИЕ. В каком состоянии входил? (напряжение/азарт/контроль/усталость — выбери или опиши)"),
]

def ask_next_mercedes(uid: int, chat_id: int):
    st = get_state(uid)
    done_keys = st["data"].get("mercedes_done", [])
    for key, prompt in MERCEDES_ORDER:
        if key not in done_keys:
            bot.send_message(chat_id, f"_{prompt}_", reply_markup=main_menu())
            set_state(uid, step=f"mer_{key}")
            return
    # Сборка паттерна и подтверждение
    data = st["data"]
    emotions  = data.get("mer_emotions", "")
    thoughts  = data.get("mer_thoughts", "")
    behavior  = data.get("mer_behavior", "")
    summary = (
        "Резюме паттерна:\n"
        f"• Поведение: {behavior or '—'}\n"
        f"• Эмоции: {emotions or '—'}\n"
        f"• Мысли: {thoughts or '—'}\n\n"
        "Если ок — напиши *Готово*, и перейдём к позитивной цели."
    )
    # сохраним в таблицу errors pattern_* поля
    upsert_error_partial(uid, {
        "pattern_behavior": behavior,
        "pattern_emotion":  emotions,
        "pattern_thought":  thoughts
    })
    bot.send_message(chat_id, summary, reply_markup=main_menu())
    set_state(uid, step="mer_confirm")

def is_too_vague(text_: str) -> bool:
    s = text_.strip().lower()
    return len(s) < 8 or s in {"не знаю", "затрудняюсь", "сложно сказать", "не уверен"}

def enforce_behavior_level(text_: str) -> str:
    """
    Если похоже на убеждение/общие слова — мягко перефразируем к поведению.
    """
    s = text_.strip()
    if any(x in s.lower() for x in ["нужно", "надо", "всегда", "никогда", "должен", "прав", "ошиб", "рынок"]):
        return f"Переформулировка на уровень действия: {s}\n→ Пример: «Вхожу до формирования сигнала» / «Передвигаю стоп после входа»."
    return s

def build_checklists(behavior: str) -> tuple[str, str]:
    pre = (
        "📝 *Чек-лист перед входом*\n"
        "1) Сетап 100% сформирован\n"
        "2) Проверил план сопровождения\n"
        "3) Короткая пауза и проверка состояния\n"
        "4) Нет желания «избежать»/«успеть»\n"
        "5) Вход по правилам"
    )
    post = (
        "🧭 *Чек-лист после входа*\n"
        "1) Таймер/контроль точек сопровождения\n"
        "2) Не трогаю стоп/тейк до условий плана\n"
        "3) Отмечаю эмоции, но не действую из них\n"
        "4) Фиксация по сценарию, не по импульсу"
    )
    if behavior:
        pre += f"\n\nФокус: не повторять «{behavior}»."
    return pre, post

# ---------- ХЕНДЛЕРЫ ИНТЕНТОВ/КОМАНД ----------
@bot.message_handler(commands=["start", "menu", "reset"])
def cmd_start(m):
    uid = m.from_user.id
    ensure_user(uid)
    set_state(uid, intent="idle", step=None, data_merge={"current_error_id": None, "mercedes_done": []})
    bot.send_message(
        m.chat.id,
        "👋 Привет! Я наставник *Innertrade*.\nВыбери пункт или напиши текст.\nКоманды: /status /ping",
        reply_markup=main_menu()
    )

@bot.message_handler(commands=["ping"])
def cmd_ping(m):
    bot.send_message(m.chat.id, "pong")

@bot.message_handler(commands=["status"])
def cmd_status(m):
    st = get_state(m.from_user.id)
    bot.send_message(
        m.chat.id,
        f"Intent: `{st['intent']}`\nStep: `{st['step'] or '-'}`\nTmp: `{json.dumps(st['data'], ensure_ascii=False)}`",
        reply_markup=main_menu()
    )

# Кнопки главного меню
@bot.message_handler(func=lambda msg: msg.text == "🚑 У меня ошибка")
def intent_error(m):
    uid = m.from_user.id
    set_state(uid, intent="error_flow", step="ask_error",
              data_merge={"current_error_id": None, "mercedes_done": []})
    bot.send_message(
        m.chat.id,
        "Опиши *основную ошибку* 1–2 предложениями **на уровне поведения/навыка**.\n"
        "Примеры: «вхожу до формирования сигнала», «двигаю стоп после входа», «закрываю по первой коррекции».",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🧩 Хочу стратегию")
def intent_strategy(m):
    uid = m.from_user.id
    set_state(uid, intent="strategy", step=None)
    bot.send_message(
        m.chat.id,
        "Ок, соберём ТС (черновик v0.1):\n1) Подход/рынки/ТФ\n2) Чек-лист входа\n3) Стоп/сопровождение/выход\n4) Лимиты и риск\n\n"
        "_(в этом релизе — после Урока 1)_",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "📄 Паспорт")
def intent_passport(m):
    uid = m.from_user.id
    set_state(uid, intent="passport", step=None)
    bot.send_message(
        m.chat.id,
        "Паспорт трейдера: позже подключим редактирование полей прямо в боте (после урока 1).",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🗒 Панель недели")
def intent_week_panel(m):
    uid = m.from_user.id
    set_state(uid, intent="week_panel", step=None)
    bot.send_message(
        m.chat.id,
        "Панель недели (MVP скоро): фокус недели, 1–2 цели, лимиты, дневные чек-ины, ретро.",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🆘 Экстренно: поплыл")
def intent_panic(m):
    uid = m.from_user.id
    set_state(uid, intent="panic", step=None)
    bot.send_message(
        m.chat.id,
        "Стоп-протокол:\n1) Пауза 2 мин\n2) Закрой терминал/вкладку\n3) 10 медленных вдохов\n"
        "4) Запиши триггер (что именно выбило)\n5) Вернись к плану или закрой позицию по правилу",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🤔 Не знаю, с чего начать")
def intent_start_help(m):
    uid = m.from_user.id
    set_state(uid, intent="start_help", step=None)
    bot.send_message(
        m.chat.id,
        "Предлагаю так:\n1) Разберём одну ключевую ошибку (Урок 1)\n2) Обновим Паспорт\n3) Соберём черновик ТС",
        reply_markup=main_menu()
    )

# ---------- РОУТИНГ ПО ТЕКСТУ (ДИАЛОГ УРОКА 1) ----------
@bot.message_handler(content_types=["text"])
def router(m):
    uid = m.from_user.id
    st = get_state(uid)
    text_in = (m.text or "").strip()

    # Если мы не в сценарии урока 1 — короткий ответ
    if st["intent"] != "error_flow":
        bot.send_message(m.chat.id, "Принял. Выбери пункт в меню или напиши /menu.", reply_markup=main_menu())
        return

    # --- Шаг 1: формулировка ошибки на уровне поведения/навыка
    if st["step"] == "ask_error":
        if is_too_vague(text_in):
            bot.send_message(m.chat.id, "Слишком коротко. Опиши 1–2 предложениями, *что именно ты делаешь* не по плану.")
            return
        normalized = enforce_behavior_level(text_in)
        # создаём строку errors и фиксируем error_text
        eid = upsert_error_partial(uid, {"error_text": normalized})
        bot.send_message(m.chat.id, "Ок. Пойдём по **MERCEDES** кратко, чтобы увидеть паттерн.")
        set_state(uid, data_merge={"mercedes_done": []})
        ask_next_mercedes(uid, m.chat.id)
        return

    # --- MERCEDES блоки
    if st["step"] and st["step"].startswith("mer_"):
        key = st["step"].split("_", 1)[1]  # context/emotions/thoughts/behavior/state
        if is_too_vague(text_in):
            bot.send_message(m.chat.id, "Добавь деталей, хотя бы 1–2 предложения/слова по существу.")
            return
        # Сохраняем во временный data; в базу занесём ключевые pattern_* (эмоции/мысли/поведение)
        data_merge = {f"mer_{key}": text_in}
        done_list = st["data"].get("mercedes_done", [])
        if key not in done_list:
            done_list.append(key)
        data_merge["mercedes_done"] = done_list
        set_state(uid, data_merge=data_merge)

        # Если это behavior/emotions/thoughts — сразу обновим errors.*
        if key == "behavior":
            upsert_error_partial(uid, {"pattern_behavior": text_in})
        elif key == "emotions":
            upsert_error_partial(uid, {"pattern_emotion": text_in})
        elif key == "thoughts":
            upsert_error_partial(uid, {"pattern_thought": text_in})

        ask_next_mercedes(uid, m.chat.id)
        return

    # --- Подтверждение резюме паттерна
    if st["step"] == "mer_confirm":
        if text_in.lower() not in {"готово", "ок", "да", "done", "подтверждаю"}:
            bot.send_message(m.chat.id, "Если резюме верно — напиши *Готово*. И перейдём дальше.")
            return
        bot.send_message(
            m.chat.id,
            "Супер. Теперь сформулируем *позитивную цель/новое поведение* в практических терминах.\n"
            "Пример: «входить только после 100% сигнала и *не трогать стоп/тейк* до условий плана».",
            reply_markup=main_menu()
        )
        set_state(uid, step="goal_new")
        return

    # --- Позитивная цель
    if st["step"] == "goal_new":
        if is_too_vague(text_in):
            bot.send_message(m.chat.id, "Цель слишком общая. Сформулируй наблюдаемо: *что делаю/не делаю*.")
            return
        upsert_error_partial(uid, {"positive_goal": text_in})
        bot.send_message(m.chat.id, "Идём по *TOTE*.\n\n*T (цель)* — на ближайшие 3 сделки. Напиши цель в формате «В течение 3 сделок я ...»")
        set_state(uid, step="tote_goal")
        return

    # --- TOTE: goal
    if st["step"] == "tote_goal":
        if is_too_vague(text_in):
            bot.send_message(m.chat.id, "Нужно чётче: цель на 3 сделки, связанная с твоей ошибкой.")
            return
        upsert_error_partial(uid, {"tote_goal": text_in})
        bot.send_message(
            m.chat.id,
            "*O (операции)* — какие шаги помогут удержать цель?\n"
            "Пример: чек-лист перед входом; пауза и дыхание; записка на мониторе; таймер после входа.",
        )
        set_state(uid, step="tote_ops")
        return

    # --- TOTE: ops
    if st["step"] == "tote_ops":
        if is_too_vague(text_in):
            bot.send_message(m.chat.id, "Добавь 2–3 шага. Коротко, по пунктам.")
            return
        upsert_error_partial(uid, {"tote_ops": text_in})
        bot.send_message(
            m.chat.id,
            "*T (проверка)* — как поймёшь, что держишься плана?\n"
            "Пример: «не двигал стоп/тейк в 3 сделках подряд», «входил только по чек-листу».",
        )
        set_state(uid, step="tote_check")
        return

    # --- TOTE: check
    if st["step"] == "tote_check":
        if is_too_vague(text_in):
            bot.send_message(m.chat.id, "Нужно чёткое условие: что именно считаем выполнением.")
            return
        upsert_error_partial(uid, {"tote_check": text_in})
        bot.send_message(
            m.chat.id,
            "*E (выход/итог)* — что сделаешь, если цель выполнена? А если нет — что изменишь в шагах?",
        )
        set_state(uid, step="tote_exit")
        return

    # --- TOTE: exit  → финализация урока 1
    if st["step"] == "tote_exit":
        if is_too_vague(text_in):
            bot.send_message(m.chat.id, "Опиши коротко оба варианта: «если ДА/если НЕТ, то ...».")
            return
        upsert_error_partial(uid, {"tote_exit": text_in})

        # Соберём чек-листы и завершим
        data = get_state(uid)["data"]
        behavior = data.get("mer_behavior", "")
        pre, post = build_checklists(behavior)
        upsert_error_partial(uid, {"checklist_pre": pre, "checklist_post": post})

        bot.send_message(m.chat.id, pre)
        bot.send_message(m.chat.id, post)
        bot.send_message(
            m.chat.id,
            "✅ *Урок 1 завершён.* Запись сохранена. Готов перейти к архетипам/ролям (Урок 2) или собрать черновик ТС.\n"
            "Открой меню и выбери следующий шаг.",
            reply_markup=main_menu()
        )
        # сброс в idle
        set_state(uid, intent="idle", step=None, data_merge={"current_error_id": None, "mercedes_done": []})
        return

    # Фолбэк в сценарии, если шаг неизвестен
    bot.send_message(m.chat.id, "Давай начнём сначала: нажми «🚑 У меня ошибка».", reply_markup=main_menu())
    set_state(uid, intent="idle", step=None, data_merge={"current_error_id": None, "mercedes_done": []})

# ---------- FLASK / WEBHOOK ----------
app = Flask(__name__)

@app.get("/")
def root():
    return "OK Innertrade v1-secure"

@app.get("/health")
def health():
    return jsonify(ok=True, ts=datetime.utcnow().isoformat())

@app.post(f"/webhook/{WEBHOOK_PATH}")
def webhook():
    # проверка секрета и лимита тела
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TG_WEBHOOK_SECRET:
        abort(401)
    if request.content_length and request.content_length > 1_000_000:
        abort(413)
    try:
        update = Update.de_json(request.get_json(force=True))
        bot.process_new_updates([update])
    except Exception as e:
        log.exception(f"update error: {e}")
        return "err", 500
    return "ok"

def setup_webhook():
    # Снимем старый и поставим наш
    try:
        bot.remove_webhook()
    except Exception:
        pass
    url = f"{PUBLIC_URL}/webhook/{WEBHOOK_PATH}"
    ok = bot.set_webhook(url=url, secret_token=TG_WEBHOOK_SECRET, max_connections=40, allowed_updates=["message"])
    if ok:
        log.info(f"Webhook set to {url}")
    else:
        log.error("Failed to set webhook")

if __name__ == "__main__":
    setup_webhook()
    port = int(os.getenv("PORT", "10000"))
    log.info("Starting Flask keepalive…")
    app.run(host="0.0.0.0", port=port)
