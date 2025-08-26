# main.py
import os
import logging
from datetime import datetime
from flask import Flask, request, abort, jsonify
from telebot import TeleBot, types
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

# ----------------- ЛОГИ -----------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("innertrade")

# ----------------- ENV ------------------
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY")  # на будущее
DATABASE_URL       = os.getenv("DATABASE_URL")
PUBLIC_URL         = os.getenv("PUBLIC_URL")      # напр. https://innertrade-bot.onrender.com
WEBHOOK_PATH       = os.getenv("WEBHOOK_PATH", "tg")  # уникальный путь, напр. abcd123
TG_WEBHOOK_SECRET  = os.getenv("TG_WEBHOOK_SECRET")   # секрет для заголовка X-Telegram-Bot-Api-Secret-Token
MAX_BODY_BYTES     = int(os.getenv("MAX_BODY_BYTES", "1000000"))  # 1 МБ по умолчанию

# Жёсткие проверки критичных переменных
missing = [k for k, v in {
    "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
    "PUBLIC_URL": PUBLIC_URL,
    "TG_WEBHOOK_SECRET": TG_WEBHOOK_SECRET
}.items() if not v]
if missing:
    raise RuntimeError(f"Missing env vars: {', '.join(missing)}")

# ----------------- DB -------------------
engine = None
if DATABASE_URL:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=300)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
  user_id    BIGINT PRIMARY KEY,
  mode       TEXT NOT NULL DEFAULT 'course',
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS user_state (
  user_id    BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
  intent     TEXT,
  step       TEXT,
  data       JSONB,
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS errors (
  id                BIGSERIAL PRIMARY KEY,
  user_id           BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  error_text        TEXT NOT NULL,
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
  created_at        TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_errors_user ON errors(user_id);

CREATE TABLE IF NOT EXISTS archetypes (
  id             BIGSERIAL PRIMARY KEY,
  user_id        BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  lead_archetype TEXT,
  roles          JSONB,
  subparts       JSONB,
  conflicts      JSONB,
  created_at     TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_archetypes_user ON archetypes(user_id);

CREATE TABLE IF NOT EXISTS beliefs_values (
  id          BIGSERIAL PRIMARY KEY,
  user_id     BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  beliefs     JSONB,
  values      JSONB,
  conflicts   JSONB,
  reframes    JSONB,
  created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_beliefs_user ON beliefs_values(user_id);

CREATE TABLE IF NOT EXISTS integration (
  id            BIGSERIAL PRIMARY KEY,
  user_id       BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  key_error_refs JSONB,
  key_roles     JSONB,
  key_beliefs   JSONB,
  key_values    JSONB,
  rules_to_ts   TEXT,
  export_link   TEXT,
  created_at    TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_integration_user ON integration(user_id);
"""

def init_db():
    if not engine:
        log.info("DATABASE_URL not set — running without DB")
        return
    try:
        with engine.begin() as conn:
            conn.execute(text(SCHEMA_SQL))
        log.info("DB schema ensured")
    except SQLAlchemyError as e:
        log.error("DB init failed: %s", e)

def upsert_user(user_id: int):
    if not engine:
        return
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO users(user_id) VALUES (:uid)
                ON CONFLICT (user_id) DO UPDATE SET updated_at = now()
            """), {"uid": user_id})
    except SQLAlchemyError as e:
        log.warning("upsert_user failed uid=%s: %s", user_id, e)

def save_state(user_id: int, intent: str, step: str | None = None, data: dict | None = None):
    if not engine:
        return
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO user_state(user_id, intent, step, data, updated_at)
                VALUES (:uid, :intent, :step, COALESCE(:data, '{}'::jsonb), now())
                ON CONFLICT (user_id) DO UPDATE
                SET intent = EXCLUDED.intent,
                    step   = EXCLUDED.step,
                    data   = EXCLUDED.data,
                    updated_at = now()
            """), {"uid": user_id, "intent": intent, "step": step, "data": data})
    except SQLAlchemyError as e:
        log.warning("save_state failed uid=%s intent=%s: %s", user_id, intent, e)

# ----------------- TELEGRAM -------------------
bot = TeleBot(TELEGRAM_TOKEN, parse_mode="Markdown")

def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🚑 У меня ошибка", "🧩 Хочу стратегию")
    kb.row("📄 Паспорт", "🗒 Панель недели")
    kb.row("🆘 Экстренно: поплыл", "🤔 Не знаю, с чего начать")
    return kb

@bot.message_handler(commands=["start", "menu", "reset"])
def cmd_start(m):
    upsert_user(m.from_user.id)
    save_state(m.from_user.id, "idle")
    bot.send_message(
        m.chat.id,
        "👋 Привет! Я ИИ-наставник *Innertrade*.\nВыбери кнопку или напиши текст.\nКоманды: /ping /reset",
        reply_markup=main_menu()
    )

@bot.message_handler(commands=["ping"])
def cmd_ping(m):
    bot.send_message(m.chat.id, "pong")

# --------- Интенты (кнопки) ----------
@bot.message_handler(func=lambda msg: msg.text == "🚑 У меня ошибка")
def intent_error(m):
    save_state(m.from_user.id, "error")
    bot.send_message(
        m.chat.id,
        "Давай разберём через *MERCEDES + TOTE*.\n\n"
        "*M* Мотивация?\n*E* Эмоции?\n*R* Результат?\n*C* Контекст?\n*E* Эффект?\n*D* Действия?\n*S* Стратегия?\n\n"
        "*T* Test — что пошло не так?\n*O* Operate — что сделал?\n*T* Test — результат?\n*E* Evolve — что изменишь?",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🧩 Хочу стратегию")
def intent_strategy(m):
    save_state(m.from_user.id, "strategy")
    bot.send_message(
        m.chat.id,
        "Ок, собираем ТС по конструктору:\n"
        "1) Цели\n2) Стиль (дневной/свинг/позиционный)\n"
        "3) Рынки/инструменты\n4) Правила входа/выхода\n"
        "5) Риск (%, стоп)\n6) Сопровождение\n7) Тестирование (история/демо)",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "📄 Паспорт")
def intent_passport(m):
    save_state(m.from_user.id, "passport")
    bot.send_message(
        m.chat.id,
        "Паспорт трейдера. 1/6) На каких рынках/инструментах торгуешь?",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🗒 Панель недели")
def intent_week_panel(m):
    save_state(m.from_user.id, "week_panel")
    bot.send_message(
        m.chat.id,
        "Панель недели:\n• Фокус недели\n• План (3 шага)\n• Лимиты\n• Ритуалы\n• Короткая ретро в конце недели",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🆘 Экстренно: поплыл")
def intent_panic(m):
    save_state(m.from_user.id, "panic")
    bot.send_message(
        m.chat.id,
        "Стоп-протокол:\n1) Пауза 2 мин\n2) Закрой терминал/вкладку с графиком\n3) 10 медленных вдохов\n"
        "4) Запиши триггер (что именно выбило)\n5) Вернись к плану сделки или закрой позицию по правилу",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda msg: msg.text == "🤔 Не знаю, с чего начать")
def intent_start_help(m):
    save_state(m.from_user.id, "start_help")
    bot.send_message(
        m.chat.id,
        "Предлагаю так:\n1) Заполним паспорт (1–2 мин)\n2) Выберем фокус недели\n3) Соберём скелет ТС\n"
        "С чего начнём — паспорт или фокус недели?",
        reply_markup=main_menu()
    )

@bot.message_handler(content_types=["text"])
def fallback(m):
    bot.send_message(
        m.chat.id,
        "Принял. Чтобы было быстрее, выбери пункт в меню ниже или напиши /menu.",
        reply_markup=main_menu()
    )

# ----------------- FLASK -----------------
app = Flask(__name__)

@app.get("/")
def root():
    return "OK", 200

@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "time": datetime.utcnow().isoformat() + "Z",
        "webhook": f"{PUBLIC_URL}/webhook/{WEBHOOK_PATH}"
    })

@app.post(f"/webhook/{WEBHOOK_PATH}")
def webhook():
    # Безопасность периметра
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TG_WEBHOOK_SECRET:
        abort(401)
    if request.content_length and request.content_length > MAX_BODY_BYTES:
        abort(413)

    try:
        # TeleBot понимает Update из JSON-строки
        update_json = request.get_data(as_text=True)
        update = types.Update.de_json(update_json)
        bot.process_new_updates([update])
    except Exception as e:
        log.exception("Update handling failed: %s", e)
        return "ERR", 500
    return "OK", 200

def ensure_webhook():
    url = f"{PUBLIC_URL}/webhook/{WEBHOOK_PATH}"
    try:
        ok = bot.set_webhook(
            url=url,
            secret_token=TG_WEBHOOK_SECRET,
            drop_pending_updates=False,
            max_connections=40
        )
        if ok:
            log.info("Webhook set to %s", url)
        else:
            log.warning("bot.set_webhook returned False")
    except Exception as e:
        log.error("set_webhook failed: %s", e)

# ----------------- ENTRY -----------------
if __name__ == "__main__":
    init_db()
    ensure_webhook()
    port = int(os.getenv("PORT", "10000"))
    log.info("Starting Flask on 0.0.0.0:%s", port)
    app.run(host="0.0.0.0", port=port)
