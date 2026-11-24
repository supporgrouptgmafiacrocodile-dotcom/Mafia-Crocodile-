import json
import os
import re
import threading
import time
import telebot
from telebot.types import ChatPermissions

# 1. На хостинге задай переменную окружения с именем TOKEN_BOT и значением токена.
# 2. Либо впиши токен напрямую в FALLBACK_TOKEN ниже (для быстрого запуска/теста).
FALLBACK_TOKEN = ""

env_token = (os.environ.get("TOKEN_BOT") or "").strip()
fallback_token = (FALLBACK_TOKEN or "").strip()

if fallback_token == "ТВОЙ_ТОКЕН_ЗДЕСЬ":
  fallback_token = ""

print(
    f"[DEBUG] TOKEN_BOT из env найден: {bool(env_token)} (длина: {len(env_token)})"
)
print(
    f"[DEBUG] FALLBACK_TOKEN задан: {bool(fallback_token)} (длина: {len(fallback_token)})"
)

TOKEN = env_token or fallback_token
TARGET_GROUP = "@chat_mafia_lol"

if not TOKEN:
  raise RuntimeError(
      "Токен не найден! Проверь:\n"
      "1) В настройках Environment сервиса ключ называется точно TOKEN_BOT (без пробелов), а в значении стоит сам токен.\n"
      "2) После сохранения Environment нажми Restart / Redeploy.\n"
      "3) Или впиши токен в FALLBACK_TOKEN в main.py."
  )

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USERS_FILE = os.path.join(BASE_DIR, "users_db.json")
MUTES_FILE = os.path.join(BASE_DIR, "active_mutes.json")

bot = telebot.TeleBot(TOKEN)
group_started = False
state_lock = threading.Lock()


def load_json(filename, default):
  if not os.path.exists(filename):
    return default
  try:
    with open(filename, "r", encoding="utf-8") as f:
      return json.load(f)
  except Exception:
    return default


def save_json(filename, data):
  try:
    with open(filename, "w", encoding="utf-8") as f:
      json.dump(data, f, ensure_ascii=False, indent=2)
  except Exception as e:
    print(f"Ошибка сохранения {filename}: {e}")


users_db = load_json(USERS_FILE, {})
active_mutes = load_json(MUTES_FILE, [])


def clean_name_text(text):
  if not text:
    return ""
  cleaned = re.sub(r"[^0-9A-Za-zА-Яа-яЁёІіЇїЄєҐґ\s]", " ", text)
  return re.sub(r"\s+", " ", cleaned).strip().lower()


def remember_user(user):
  if not user or getattr(user, "is_bot", False):
    return
  uid = str(user.id)
  raw_name = user.first_name or ""
  cleaned_name = clean_name_text(raw_name)
  username = (user.username or "").lower()

  with state_lock:
    users_db[uid] = {
        "id": user.id,
        "username": user.username or "",
        "first_name": raw_name,
        "clean_name": cleaned_name,
        "username_lower": username,
    }
    save_json(USERS_FILE, users_db)


def log_message(message):
  try:
    if message.from_user:
      remember_user(message.from_user)
    if message.reply_to_message and message.reply_to_message.from_user:
      remember_user(message.reply_to_message.from_user)
    print(
        f"[MSG] chat={message.chat.id} (@{message.chat.username}) "
        f"from={message.from_user.id if message.from_user else '?'} "
        f"name={repr(message.from_user.first_name if message.from_user else '')} "
        f"text={repr(message.text)}"
    )
  except Exception as e:
    print(f"Ошибка логирования: {e}")


def format_user_mention(user_id, username="", first_name=""):
  if username:
    return f"@{username.lstrip('@')}"
  if first_name:
    return f"{first_name} (ID: {user_id})"
  return f"ID: {user_id}"


def is_admin(chat_id, user_id):
  try:
    member = bot.get_chat_member(chat_id, user_id)
    return member.status in ["administrator", "creator"]
  except Exception as e:
    print(f"Ошибка проверки админа: {e}")
    return False


def is_target_group(message):
  if message.chat.type not in ["group", "supergroup"]:
    return False
  if message.chat.username:
    return f"@{message.chat.username.lower()}" == TARGET_GROUP.lower()
  return False


TIME_UNIT_REGEX = re.compile(
    r"\b(\d+)\s*(секунд[ауы]?|сек\.?|мину[ттыы]?|мин\.?|час[аоов]?|ч\.?|год[аоов]?|лет)\b",
    flags=re.IGNORECASE | re.UNICODE,
)


def parse_duration_and_rest(text_line):
  match = TIME_UNIT_REGEX.search(text_line)
  if not match:
    return None, None, text_line

  amount = int(match.group(1))
  unit_raw = match.group(2).lower()

  if unit_raw.startswith("сек"):
    seconds = amount
    unit_label = (
        "секунд"
        if amount % 10 == 0 or 5 <= amount % 10 <= 9 or 11 <= amount % 100 <= 14
        else ("секунда" if amount % 10 == 1 else "секунды")
    )
  elif unit_raw.startswith("мин"):
    seconds = amount * 60
    unit_label = (
        "минут"
        if amount % 10 == 0 or 5 <= amount % 10 <= 9 or 11 <= amount % 100 <= 14
        else ("минута" if amount % 10 == 1 else "минуты")
    )
  elif unit_raw.startswith("час") or unit_raw in ["ч", "ч."]:
    seconds = amount * 3600
    unit_label = (
        "часов"
        if amount % 10 == 0 or 5 <= amount % 10 <= 9 or 11 <= amount % 100 <= 14
        else ("час" if amount % 10 == 1 else "часа")
    )
  else:
    seconds = amount * 365 * 24 * 3600
    unit_label = (
        "лет"
        if amount % 10 == 0 or 5 <= amount % 10 <= 9 or 11 <= amount % 100 <= 14
        else ("год" if amount % 10 == 1 else "года")
    )

  duration_text = f"{amount} {unit_label}"
  cleaned_line = (
      text_line[: match.start()] + " " + text_line[match.end() :]
  ).strip()
  cleaned_line = re.sub(r"\s+", " ", cleaned_line)
  return seconds, duration_text, cleaned_line


def resolve_target_user(message, arg_line_without_time):
  if message.reply_to_message and message.reply_to_message.from_user:
    u = message.reply_to_message.from_user
    return u.id, u.username or "", u.first_name or ""

  u_match = re.search(r"@([A-Za-z0-9_]{3,})", arg_line_without_time)
  if u_match:
    uname_lower = u_match.group(1).lower()
    for info in users_db.values():
      if info.get("username_lower") == uname_lower:
        return info["id"], info.get("username", ""), info.get("first_name", "")
    try:
      chat_obj = bot.get_chat(f"@{u_match.group(1)}")
      remember_user(chat_obj)
      return chat_obj.id, chat_obj.username or "", chat_obj.first_name or ""
    except Exception:
      return None, None, None

  id_match = re.search(r"\b(\d{5,15})\b", arg_line_without_time)
  if id_match:
    uid = int(id_match.group(1))
    info = users_db.get(str(uid))
    if info:
      return info["id"], info.get("username", ""), info.get("first_name", "")
    try:
      chat_obj = bot.get_chat(uid)
      return chat_obj.id, chat_obj.username or "", chat_obj.first_name or ""
    except Exception:
      return uid, "", ""

  return None, None, None


def build_punishment_message(
    action_past, target_mention, duration_text, admin_mention, reason
):
  time_part = f"на {duration_text}" if duration_text else "навсегда"
  reason_text = reason.strip() if reason and reason.strip() else "не указана"
  return (
      f"{target_mention} был {action_past} {time_part},\n"
      f"администратор: {admin_mention}\n"
      f'Причина: "{reason_text}"'
  )


def unmute_user_action(chat_id, user_id):
  bot.restrict_chat_member(
      chat_id,
      user_id,
      permissions=ChatPermissions(
          can_send_messages=True,
          can_send_media_messages=True,
          can_send_polls=True,
          can_send_other_messages=True,
          can_add_web_page_previews=True,
      ),
  )


def mute_watcher_loop():
  while True:
    now = int(time.time())
    to_notify = []
    with state_lock:
      remaining = []
      for item in active_mutes:
        if now >= item["unmute_at"]:
          to_notify.append(item)
        else:
          remaining.append(item)
      if len(remaining) != len(active_mutes):
        active_mutes[:] = remaining
        save_json(MUTES_FILE, active_mutes)

    for item in to_notify:
      try:
        unmute_user_action(item["chat_id"], item["user_id"])
      except Exception:
        pass
      try:
        bot.send_message(
            item["chat_id"],
            f"🔊 Пользователь {item['user_label']} размучен (срок мута истёк).",
        )
      except Exception:
        pass

    time.sleep(2)


@bot.message_handler(
    content_types=["text"],
    func=lambda m: bool(m.text)
    and bool(re.match(r"^/start(?:@\w+)?$", m.text.strip(), flags=re.IGNORECASE)),
)
def handle_start(message):
  global group_started
  log_message(message)

  if message.chat.type == "private":
    bot.send_message(
        message.chat.id,
        "Здравствуйте! Я официальный бот администратор группы"
        " 🕴️Mafia/Crocodile🐊! Я предназначен для одной группы, все штуки и"
        " т.д. главной группы уже настроены, а изменить их нельзя.",
    )
  elif message.chat.type in ["group", "supergroup"]:
    if is_target_group(message):
      if not group_started:
        bot.reply_to(
            message,
            f"Здравствуйте! Так как я бот именно группы {TARGET_GROUP} тут уже"
            " всё настроено.",
        )
        group_started = True
      else:
        bot.reply_to(
            message, f"Я уже настроен, так как я создан для группы {TARGET_GROUP}"
        )
    else:
      bot.reply_to(
          message, f"Я работаю только в официальной группе {TARGET_GROUP}!"
      )


@bot.message_handler(
    content_types=["text"],
    func=lambda m: bool(m.text)
    and bool(
        re.match(r"^/команды(?:@\w+)?$", m.text.strip(), flags=re.IGNORECASE)
    ),
)
def handle_commands(message):
  log_message(message)
  text = (
      "📋 Список команд администратора (регистр не важен):\n\n"
      "• /мут [время] [@username / ID / ответ]\n"
      "  На второй строке можно указать причину.\n"
      "  Без времени — мут навсегда.\n\n"
      "• /размут [@username / ID / ответ] — снять мут\n\n"
      "• /бан [время] [@username / ID / ответ] — бан (без времени — навсегда)\n"
      "  На второй строке можно указать причину.\n\n"
      "• /разбан [@username / ID / ответ] — разбан\n\n"
      "• /поискайди(БЕТА ТЕСТ!) имя — найти ID пользователя по похожему имени"
  )
  bot.reply_to(message, text)


@bot.message_handler(
    content_types=["text"],
    func=lambda m: bool(m.text)
    and bool(
        re.match(
            r"^/поискайди(?:@\w+)?(?:\s+.*)?$",
            m.text.strip(),
            flags=re.IGNORECASE | re.DOTALL,
        )
    ),
)
def handle_find_id(message):
  log_message(message)
  if message.chat.type == "private":
    return bot.reply_to(message, "Эта команда работает только в группе.")
  if not is_admin(message.chat.id, message.from_user.id):
    return bot.reply_to(message, "❌ Эта команда только для администраторов.")

  raw_args = re.sub(
      r"^/поискайди(?:@\w+)?", "", message.text.strip(), flags=re.IGNORECASE
  ).strip()
  raw_args = raw_args.strip("\"'«»“”")
  query = clean_name_text(raw_args)

  if not query:
    return bot.reply_to(
        message,
        "⚠️ Напиши имя после команды. Пример: /поискайди саша\n"
        f"В базе сейчас записей: {len(users_db)}",
    )

  with state_lock:
    current_users = dict(users_db)

  matches = []
  for info in current_users.values():
    raw_first = info.get("first_name", "")
    cleaned_first = clean_name_text(raw_first)
    if not cleaned_first:
      continue
    if query in cleaned_first:
      matches.append((info["id"], raw_first, info.get("username", "")))

  if not matches:
    return bot.reply_to(
        message,
        f'🔍 По имени "{raw_args}" никого не нашёл.\n'
        f"Записей в базе: {len(current_users)} (бот запоминает тех, кто пишет"
        " в чат после его запуска).",
    )

  if len(matches) == 1:
    uid, fname, uname = matches[0]
    u_str = f"@{uname}" if uname else "нет"
    return bot.reply_to(
        message,
        f"✅ Найден 1 пользователь:\nИмя: {fname}\nUsername: {u_str}\nID: {uid}",
    )

  lines = [f'🔎 Найдено совпадений по "{raw_args}": {len(matches)}']
  for idx, (uid, fname, uname) in enumerate(matches[:20], start=1):
    u_str = f"@{uname}" if uname else "без юзернейма"
    lines.append(f"{idx}. {fname} ({u_str}) — ID: {uid}")
  bot.reply_to(message, "\n".join(lines))


@bot.message_handler(
    content_types=["text"],
    func=lambda m: bool(m.text)
    and bool(re.match(r"^/мут(?:\b|@|\s|$)", m.text.strip(), flags=re.IGNORECASE)),
)
def handle_mute(message):
  log_message(message)
  if message.chat.type == "private":
    return
  if not is_admin(message.chat.id, message.from_user.id):
    return bot.reply_to(message, "❌ Эта команда только для администраторов.")

  lines = message.text.splitlines()
  first_line = re.sub(
      r"^/мут(?:@\w+)?", "", lines[0].strip(), flags=re.IGNORECASE
  ).strip()
  reason = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""

  seconds, duration_text, line_without_time = parse_duration_and_rest(
      first_line
  )
  target_id, target_uname, target_fname = resolve_target_user(
      message, line_without_time
  )

  if not target_id:
    return bot.reply_to(
        message,
        "⚠️ Не удалось определить пользователя. Укажите @username, ID или"
        " ответьте на сообщение.",
    )

  if is_admin(message.chat.id, target_id):
    return bot.reply_to(message, "❌ Нельзя замутить администратора.")

  target_mention = format_user_mention(target_id, target_uname, target_fname)
  admin_mention = format_user_mention(
      message.from_user.id,
      message.from_user.username,
      message.from_user.first_name,
  )

  try:
    if seconds is not None:
      tg_until = int(time.time()) + min(max(seconds, 30), 366 * 24 * 3600)
      bot.restrict_chat_member(
          message.chat.id,
          target_id,
          until_date=tg_until,
          permissions=ChatPermissions(can_send_messages=False),
      )
      unmute_at = int(time.time()) + seconds
      with state_lock:
        active_mutes[:] = [
            x
            for x in active_mutes
            if not (x["chat_id"] == message.chat.id and x["user_id"] == target_id)
        ]
        active_mutes.append({
            "chat_id": message.chat.id,
            "user_id": target_id,
            "user_label": target_mention,
            "unmute_at": unmute_at,
            "duration_text": duration_text,
        })
        save_json(MUTES_FILE, active_mutes)
    else:
      bot.restrict_chat_member(
          message.chat.id,
          target_id,
          until_date=0,
          permissions=ChatPermissions(can_send_messages=False),
      )

    msg = build_punishment_message(
        "замучен", target_mention, duration_text, admin_mention, reason
    )
    bot.reply_to(message, msg)
  except Exception as e:
    bot.reply_to(message, f"❌ Ошибка при выдаче мута: {e}")


@bot.message_handler(
    content_types=["text"],
    func=lambda m: bool(m.text)
    and bool(
        re.match(r"^/размут(?:\b|@|\s|$)", m.text.strip(), flags=re.IGNORECASE)
    ),
)
def handle_unmute(message):
  log_message(message)
  if message.chat.type == "private":
    return
  if not is_admin(message.chat.id, message.from_user.id):
    return bot.reply_to(message, "❌ Эта команда только для администраторов.")

  lines = message.text.splitlines()
  first_line = re.sub(
      r"^/размут(?:@\w+)?", "", lines[0].strip(), flags=re.IGNORECASE
  ).strip()
  target_id, target_uname, target_fname = resolve_target_user(
      message, first_line
  )

  if not target_id:
    return bot.reply_to(
        message,
        "⚠️ Укажите @username, ID или ответьте на сообщение пользователя.",
    )

  target_mention = format_user_mention(target_id, target_uname, target_fname)

  try:
    unmute_user_action(message.chat.id, target_id)
    with state_lock:
      active_mutes[:] = [
          x
          for x in active_mutes
          if not (x["chat_id"] == message.chat.id and x["user_id"] == target_id)
      ]
      save_json(MUTES_FILE, active_mutes)

    bot.reply_to(message, f"🔊 Пользователь {target_mention} размучен.")
  except Exception as e:
    bot.reply_to(message, f"❌ Ошибка при снятии мута: {e}")


@bot.message_handler(
    content_types=["text"],
    func=lambda m: bool(m.text)
    and bool(re.match(r"^/бан(?:\b|@|\s|$)", m.text.strip(), flags=re.IGNORECASE)),
)
def handle_ban(message):
  log_message(message)
  if message.chat.type == "private":
    return
  if not is_admin(message.chat.id, message.from_user.id):
    return bot.reply_to(message, "❌ Эта команда только для администраторов.")

  lines = message.text.splitlines()
  first_line = re.sub(
      r"^/бан(?:@\w+)?", "", lines[0].strip(), flags=re.IGNORECASE
  ).strip()
  reason = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""

  seconds, duration_text, line_without_time = parse_duration_and_rest(
      first_line
  )
  target_id, target_uname, target_fname = resolve_target_user(
      message, line_without_time
  )

  if not target_id:
    return bot.reply_to(
        message,
        "⚠️ Не удалось определить пользователя. Укажите @username, ID или"
        " ответьте на сообщение.",
    )

  if is_admin(message.chat.id, target_id):
    return bot.reply_to(message, "❌ Нельзя забанить администратора.")

  target_mention = format_user_mention(target_id, target_uname, target_fname)
  admin_mention = format_user_mention(
      message.from_user.id,
      message.from_user.username,
      message.from_user.first_name,
  )

  try:
    if seconds is not None:
      tg_until = int(time.time()) + min(max(seconds, 30), 366 * 24 * 3600)
      bot.ban_chat_member(message.chat.id, target_id, until_date=tg_until)
    else:
      bot.ban_chat_member(message.chat.id, target_id)

    msg = build_punishment_message(
        "забанен", target_mention, duration_text, admin_mention, reason
    )
    bot.reply_to(message, msg)
  except Exception as e:
    bot.reply_to(message, f"❌ Ошибка при бане: {e}")


@bot.message_handler(
    content_types=["text"],
    func=lambda m: bool(m.text)
    and bool(
        re.match(r"^/разбан(?:\b|@|\s|$)", m.text.strip(), flags=re.IGNORECASE)
    ),
)
def handle_unban(message):
  log_message(message)
  if message.chat.type == "private":
    return
  if not is_admin(message.chat.id, message.from_user.id):
    return bot.reply_to(message, "❌ Эта команда только для администраторов.")

  lines = message.text.splitlines()
  first_line = re.sub(
      r"^/разбан(?:@\w+)?", "", lines[0].strip(), flags=re.IGNORECASE
  ).strip()
  target_id, target_uname, target_fname = resolve_target_user(
      message, first_line
  )

  if not target_id:
    return bot.reply_to(
        message,
        "⚠️ Укажите @username, ID или ответьте на сообщение пользователя.",
    )

  target_mention = format_user_mention(target_id, target_uname, target_fname)

  try:
    bot.unban_chat_member(message.chat.id, target_id, only_if_banned=True)
    bot.reply_to(message, f"✅ Пользователь {target_mention} разбанен.")
  except Exception as e:
    bot.reply_to(message, f"❌ Не удалось разбанить: {e}")


@bot.message_handler(content_types=["text", "photo", "sticker", "voice"])
def handle_other_messages(message):
  log_message(message)


threading.Thread(target=mute_watcher_loop, daemon=True).start()
print("Бот Mafia/Crocodile запущен!")

while True:
  try:
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
  except Exception as e:
    print(f"[POLLING ERROR] {e}. Перезапуск через 5 секунд...")
    time.sleep(5)
    
