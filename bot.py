import json
import logging
import os
import threading
import time
import random
import re
import io
from datetime import datetime
from typing import Any, Dict, Optional, List, Union

import requests
import telebot
from telebot import types

# ============================================================
# FILE PATHS
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
USERS_PATH = os.path.join(BASE_DIR, "users.json")
LOGS_PATH = os.path.join(BASE_DIR, "usage_logs.json")
ERROR_LOG_PATH = os.path.join(BASE_DIR, "bot_errors.log")
TRANSACTIONS_PATH = os.path.join(BASE_DIR, "transactions.json")

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(ERROR_LOG_PATH, encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# ============================================================
# JSON HELPERS
# ============================================================

file_lock = threading.RLock()

def read_json(path: str, default: Any) -> Any:
    with file_lock:
        if not os.path.exists(path):
            write_json(path, default)
            return default
        try:
            with open(path, "r", encoding="utf-8") as file:
                return json.load(file)
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Failed to read %s: %s", path, exc)
            backup_path = f"{path}.broken-{int(time.time())}"
            try:
                os.replace(path, backup_path)
            except OSError:
                pass
            write_json(path, default)
            return default

def write_json(path: str, data: Any) -> None:
    with file_lock:
        temp_path = f"{path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=4)
        os.replace(temp_path, path)

# ============================================================
# CONFIGURATION
# ============================================================

config = read_json(CONFIG_PATH, {})
BOT_TOKEN = str(config.get("bot_token", "")).strip()
ADMIN_IDS = {str(item) for item in config.get("admin_ids", [])}
SUPPORT_USERNAME = str(config.get("support_username", "@YourSupportUsername"))
REQUEST_TIMEOUT = int(config.get("request_timeout_seconds", 20))
INITIAL_BALANCE = 20
REFERRAL_BONUS = 10  # ₹10 for the referrer only

if not BOT_TOKEN or BOT_TOKEN == "PASTE_NEW_BOT_TOKEN_HERE":
    raise RuntimeError("Open config.json and replace PASTE_NEW_BOT_TOKEN_HERE with a new BotFather token.")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=True)

# ============================================================
# DATABASE
# ============================================================

users: Dict[str, Dict[str, Any]] = read_json(USERS_PATH, {})
usage_logs = read_json(LOGS_PATH, [])
pending_transactions: Dict[str, Dict[str, Any]] = read_json(TRANSACTIONS_PATH, {})

user_states: Dict[str, Dict[str, Any]] = {}
state_lock = threading.RLock()

# Payment states
PAYMENT_STATE_SELECTING = "selecting_amount"
PAYMENT_STATE_CONFIRMING = "confirming_payment"
PAYMENT_STATE_AWAITING_SCREENSHOT = "awaiting_screenshot"

# ============================================================
# HELPERS
# ============================================================

def now_string() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def save_users() -> None:
    write_json(USERS_PATH, users)

def save_logs() -> None:
    write_json(LOGS_PATH, usage_logs[-5000:])

def save_transactions() -> None:
    write_json(TRANSACTIONS_PATH, pending_transactions)

def is_admin(user_id: str) -> bool:
    return user_id in ADMIN_IDS

def generate_referral_code(user_id: str) -> str:
    return f"ref_{user_id}"

def get_referral_link(user_id: str) -> str:
    code = generate_referral_code(user_id)
    bot_username = bot.get_me().username
    return f"https://t.me/{bot_username}?start={code}"

def register_user(message: telebot.types.Message, referred_by: Optional[str] = None) -> bool:
    user_id = str(message.from_user.id)
    created = False
    with file_lock:
        if user_id not in users:
            users[user_id] = {
                "telegram_id": user_id,
                "username": message.from_user.username or "",
                "first_name": message.from_user.first_name or "",
                "last_name": message.from_user.last_name or "",
                "balance": INITIAL_BALANCE,
                "joined_at": now_string(),
                "last_seen": now_string(),
                "is_banned": False,
                "total_requests": 0,
                "successful_requests": 0,
                "referral_code": generate_referral_code(user_id),
                "referred_by": referred_by,
                "referrals": [],
                "referral_earnings": 0,
            }
            created = True
            # If referred_by is valid, add bonus ONLY to the referrer
            if referred_by and referred_by in users:
                referrer = users[referred_by]
                # Add ₹10 to referrer only
                referrer["balance"] += REFERRAL_BONUS
                referrer["referral_earnings"] = referrer.get("referral_earnings", 0) + REFERRAL_BONUS
                if "referrals" not in referrer:
                    referrer["referrals"] = []
                referrer["referrals"].append(user_id)
                # Notify referrer
                try:
                    bot.send_message(
                        int(referred_by),
                        f"🎉 <b>New Referral!</b>\n\n"
                        f"User @{message.from_user.username or user_id} joined using your referral link.\n"
                        f"You earned <b>₹{REFERRAL_BONUS}</b>!\n"
                        f"New balance: <b>₹{referrer['balance']}</b>",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
                # NOTE: We removed the extra welcome message for the new user here.
                # The unified welcome is now sent from start_command.
        else:
            users[user_id]["username"] = message.from_user.username or ""
            users[user_id]["first_name"] = message.from_user.first_name or ""
            users[user_id]["last_name"] = message.from_user.last_name or ""
            users[user_id]["last_seen"] = now_string()
        save_users()
    return created

def get_user(user_id: str) -> Dict[str, Any]:
    return users.get(user_id, {})

def get_balance(user_id: str) -> int:
    return int(users.get(user_id, {}).get("balance", 0))

def add_balance(user_id: str, amount: int) -> bool:
    with file_lock:
        if user_id not in users:
            return False
        users[user_id]["balance"] = max(0, int(users[user_id].get("balance", 0)) + amount)
        save_users()
        return True

def set_balance(user_id: str, amount: int) -> bool:
    with file_lock:
        if user_id not in users:
            return False
        users[user_id]["balance"] = max(0, amount)
        save_users()
        return True

def deduct_balance(user_id: str, amount: int) -> bool:
    if is_admin(user_id) or amount == 0:
        return True
    with file_lock:
        current = int(users.get(user_id, {}).get("balance", 0))
        if current < amount:
            return False
        users[user_id]["balance"] = current - amount
        save_users()
        return True

def add_log(user_id: str, service: str, query: str, success: bool, cost: int, message: str = "") -> None:
    with file_lock:
        user_data = users.get(user_id, {})
        username = user_data.get("username", "")
        usage_logs.append({
            "time": now_string(),
            "user_id": user_id,
            "username": username,
            "service": service,
            "query": query[:500],
            "success": success,
            "cost": cost,
            "message": message[:1000],
        })
        save_logs()

# ============================================================
# KEYBOARDS
# ============================================================

BUTTON_PRO_OSINT = "🧭 Pro OSINT"
BUTTON_MORE_TOOLS = "🧰 More Tools"
BUTTON_PROFILE = "👤 My Profile"
BUTTON_SUPPORT = "📞 Support"
BUTTON_ADD_BALANCE = "💰 Add Balance"

BUTTON_ADHAAR_SEARCH = "🪪 Adhaar Search"
BUTTON_NUMBER_INFO = "📞 Number Info"
BUTTON_TG_TO_NUM = "📞 Telegram To Number"
BUTTON_VEHICLE_TO_OWNER = "🚗 Vehicle to Owner"
BUTTON_ADVANCE_VEHICLE = "🔧 Advance Vehicle Info"
BUTTON_ADHAAR_TO_RATION = "🪪 Adhaar to Ration"
BUTTON_BOMBER = "💣 Bomber"

BUTTON_IP = "🌐 IP Lookup"
BUTTON_IFSC = "🏦 IFSC Info"
BUTTON_INSTA = "📸 Insta Lookup"
BUTTON_DOMAIN = "🌍 Domain Information"
BUTTON_EMAIL_SEARCH = "📧 Email Search"
BUTTON_INVITE = "🎁 Invite & Earn"
BUTTON_BACK = "⬅️ Back"
BUTTON_CANCEL = "❌ Cancel"

def main_keyboard() -> types.ReplyKeyboardMarkup:
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.row(
        types.KeyboardButton(BUTTON_ADHAAR_SEARCH),
        types.KeyboardButton(BUTTON_NUMBER_INFO),
    )
    markup.row(
        types.KeyboardButton(BUTTON_TG_TO_NUM),
        types.KeyboardButton(BUTTON_VEHICLE_TO_OWNER),
    )
    markup.row(
        types.KeyboardButton(BUTTON_ADVANCE_VEHICLE),
        types.KeyboardButton(BUTTON_ADHAAR_TO_RATION),
    )
    markup.row(
        types.KeyboardButton(BUTTON_MORE_TOOLS),
        types.KeyboardButton(BUTTON_PROFILE),
    )
    markup.row(
        types.KeyboardButton(BUTTON_ADD_BALANCE),
        types.KeyboardButton(BUTTON_SUPPORT),
    )
    markup.row(types.KeyboardButton(BUTTON_PRO_OSINT))
    markup.row(types.KeyboardButton(BUTTON_BOMBER))
    return markup

def more_tools_keyboard() -> types.ReplyKeyboardMarkup:
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.row(
        types.KeyboardButton(BUTTON_IP),
        types.KeyboardButton(BUTTON_IFSC),
    )
    markup.row(
        types.KeyboardButton(BUTTON_INSTA),
        types.KeyboardButton(BUTTON_DOMAIN),
    )
    markup.row(
        types.KeyboardButton(BUTTON_EMAIL_SEARCH),
        types.KeyboardButton(BUTTON_INVITE),
    )
    markup.row(types.KeyboardButton(BUTTON_BACK))
    return markup

def cancel_keyboard() -> types.ReplyKeyboardMarkup:
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton(BUTTON_CANCEL))
    return markup

# ============================================================
# VALIDATION
# ============================================================

def validate_ifsc(value: str) -> bool:
    value = value.strip().upper()
    return len(value) == 11 and value[:4].isalpha() and value[4] == "0" and value[5:].isalnum()

def validate_ip(value: str) -> bool:
    parts = value.strip().split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(part) <= 255 and str(int(part)) == part for part in parts)
    except ValueError:
        return False

def validate_domain(value: str) -> bool:
    value = value.strip().lower()
    return "." in value and " " not in value and not value.startswith(".") and not value.endswith(".") and len(value) <= 253

def validate_username(value: str) -> bool:
    value = value.strip().lstrip("@")
    return 1 <= len(value) <= 64 and all(ch.isalnum() or ch in "._-" for ch in value)

def validate_email(value: str) -> bool:
    value = value.strip()
    return "@" in value and "." in value.rsplit("@", 1)[-1] and " " not in value and len(value) <= 254

def validate_adhaar(value: str) -> bool:
    value = value.strip()
    return value.isdigit() and len(value) == 12

def clean_phone(value: str) -> str:
    digits = re.sub(r'\D', '', value)
    if len(digits) == 12 and digits.startswith('91'):
        return digits[2:]
    return digits

def validate_phone(value: str) -> bool:
    cleaned = clean_phone(value)
    return len(cleaned) == 10

def validate_vehicle(value: str) -> bool:
    value = value.strip().upper()
    cleaned = re.sub(r'\s', '', value)
    if len(cleaned) < 6 or len(cleaned) > 12:
        return False
    has_alpha = any(c.isalpha() for c in cleaned)
    has_digit = any(c.isdigit() for c in cleaned)
    return has_alpha and has_digit

def validate_any(value: str) -> bool:
    return bool(value.strip())

# ============================================================
# API LAYER
# ============================================================

def call_configured_api(service_key: str, query: str) -> Dict[str, Any]:
    service = config.get("services", {}).get(service_key, {})
    if not service or not service.get("enabled", False):
        raise RuntimeError("This service is currently disabled.")
    api_url = str(service.get("api_url", "")).strip()
    if not api_url:
        raise RuntimeError("API is not configured yet. Add the API URL in config.json.")
    method = str(service.get("method", "GET")).upper()
    parameter_name = str(service.get("parameter_name", "query"))
    headers = service.get("headers", {}) or {}
    try:
        if method == "GET":
            response = requests.get(api_url, params={parameter_name: query}, headers=headers, timeout=REQUEST_TIMEOUT)
        elif method == "POST":
            response = requests.post(api_url, json={parameter_name: query}, headers=headers, timeout=REQUEST_TIMEOUT)
        else:
            raise RuntimeError(f"Unsupported API method: {method}")
        response.raise_for_status()
        try:
            data = response.json()
        except ValueError:
            data = {"result": response.text}
        return {"ok": True, "status_code": response.status_code, "data": data}
    except requests.Timeout as exc:
        raise RuntimeError("The API request timed out. Please try again.") from exc
    except requests.RequestException as exc:
        logger.error(f"API request failed: {exc}")
        raise RuntimeError("API request failed. Please try again later.") from exc

def escape_html(value: Any) -> str:
    text = str(value)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

# ============================================================
# FORMATTERS
# ============================================================

def format_api_result(title: str, query: str, data: Any) -> str:
    # Vehicle
    if isinstance(data, dict) and "vehicleNumber" in data:
        lines = []
        lines.append(f"<b>{escape_html(title)}</b>")
        lines.append("")
        lines.append(f"<b>Query:</b> <code>{escape_html(query)}</code>")
        lines.append("")
        lines.append(f"📦 <b>Result 1:</b>")
        field_map = {
            "vehicleNumber": "🚗 Vehicle Number",
            "mobile": "📱 Mobile Number",
            "status": "📊 Status",
        }
        for field, label in field_map.items():
            value = data.get(field, "")
            if value is None:
                value = ""
            value_str = str(value).strip()
            if not value_str or value_str.lower() == "null":
                value_str = "N/A"
            lines.append(f"{label}: {escape_html(value_str)}")
        lines.append("────────────────────────────────")
        return "\n".join(lines)

    # TG‑to‑Num
    if isinstance(data, dict) and "tg_id" in data:
        lines = []
        lines.append(f"<b>{escape_html(title)}</b>")
        lines.append("")
        lines.append(f"<b>Query:</b> <code>{escape_html(query)}</code>")
        lines.append("")
        lines.append(f"📦 <b>Result 1:</b>")
        field_map = {
            "tg_id": "🆔 Telegram ID",
            "country": "🌍 Country",
            "country_code": "📞 Country Code",
            "number": "📱 Phone Number",
            "msg": "💬 Message",
        }
        for field, label in field_map.items():
            value = data.get(field, "")
            if value is None:
                value = ""
            value_str = str(value).strip()
            if not value_str or value_str.lower() == "null":
                value_str = "N/A"
            lines.append(f"{label}: {escape_html(value_str)}")
        lines.append("────────────────────────────────")
        return "\n".join(lines)

    # Number Info (data list)
    if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
        items = data["data"]
        if not items:
            return f"<b>{escape_html(title)}</b>\n\n<b>Query:</b> <code>{escape_html(query)}</code>\n\n❌ No results found."
        lines = []
        lines.append(f"<b>{escape_html(title)}</b>")
        lines.append("")
        lines.append(f"<b>Query:</b> <code>{escape_html(query)}</code>")
        lines.append("")
        for idx, item in enumerate(items, 1):
            if not isinstance(item, dict):
                continue
            lines.append(f"📦 <b>Result {idx}:</b>")
            field_map = {
                "MOBILE": "📱 Number",
                "NAME": "👤 Name",
                "fname": "👱‍♂️ Father",
                "ADDRESS": "🏠 Address",
                "alt": "📞 Alternative No.",
                "circle": "✨ Sim",
                "id": "🆔 Gov ID",
                "email": "📧 Email",
            }
            for field, label in field_map.items():
                value = item.get(field, "")
                if value is None:
                    value = ""
                value_str = str(value).strip()
                if not value_str or value_str.lower() == "null":
                    value_str = "N/A"
                lines.append(f"{label}: {escape_html(value_str)}")
            lines.append("────────────────────────────────")
        return "\n".join(lines)

    # Adhaar style numeric keys
    if isinstance(data, dict):
        numeric_keys = [k for k in data.keys() if k.isdigit()]
        if numeric_keys and all(isinstance(data[k], dict) for k in numeric_keys):
            items = sorted(numeric_keys, key=int)
            lines = []
            lines.append(f"<b>{escape_html(title)}</b>")
            lines.append("")
            lines.append(f"<b>Query:</b> <code>{escape_html(query)}</code>")
            lines.append("")
            for idx, key in enumerate(items, 1):
                item = data[key]
                lines.append(f"📦 <b>Result {idx}:</b>")
                field_map = {
                    "MOBILE": "📱 Number",
                    "NAME": "👤 Name",
                    "fname": "👱‍♂️ Father",
                    "ADDRESS": "🏠 Address",
                    "alt": "📞 Alternative No.",
                    "circle": "✨ Sim",
                    "id": "🆔 Gov ID",
                    "email": "📧 Email",
                }
                for field, label in field_map.items():
                    value = item.get(field, "")
                    if value is None:
                        value = ""
                    value_str = str(value).strip()
                    if not value_str or value_str.lower() == "null":
                        value_str = "N/A"
                    lines.append(f"{label}: {escape_html(value_str)}")
                lines.append("────────────────────────────────")
            if not items:
                return f"<b>{escape_html(title)}</b>\n\n<b>Query:</b> <code>{escape_html(query)}</code>\n\n❌ No results found."
            return "\n".join(lines)

    # Fallback flatten (metadata filtered)
    rows = flatten_data(data)
    if not rows:
        return f"<b>{escape_html(title)}</b>\n\n<b>Query:</b> <code>{escape_html(query)}</code>\n\nNo result was returned."
    lines = [f"<b>{escape_html(title)}</b>", "", f"<b>Query:</b> <code>{escape_html(query)}</code>", ""]
    skip_keys = {"key_details", "developer", "success", "status_code", "http_status", "total_records"}
    for key, value in rows[:50]:
        if any(skip in key for skip in skip_keys):
            continue
        value_text = escape_html(value)
        if len(value_text) > 500:
            value_text = value_text[:500] + "..."
        lines.append(f"<b>{escape_html(key)}:</b> {value_text}")
    return "\n".join(lines)

def flatten_data(data: Any, prefix: str = "") -> list:
    rows = []
    if isinstance(data, dict):
        for key, value in data.items():
            new_prefix = f"{prefix}.{key}" if prefix else str(key)
            rows.extend(flatten_data(value, new_prefix))
    elif isinstance(data, list):
        for index, value in enumerate(data[:20]):
            new_prefix = f"{prefix}[{index}]"
            rows.extend(flatten_data(value, new_prefix))
    else:
        rows.append((prefix or "result", data))
    return rows

def split_long_message(text: str, max_len: int = 4000) -> list:
    if len(text) <= max_len:
        return [text]
    separator = "────────────────────────────────"
    footer = ""
    main_text = text
    footer_pos = main_text.rfind("\n\n💳")
    if footer_pos != -1:
        footer = main_text[footer_pos:]
        main_text = main_text[:footer_pos]
    lines = main_text.splitlines(True)
    current = ""
    chunks = []
    for line in lines:
        current += line
        if separator in line:
            chunks.append(current)
            current = ""
    if current:
        chunks.append(current)
    final_parts = []
    current_chunk = ""
    for chunk in chunks:
        if len(current_chunk) + len(chunk) <= max_len:
            current_chunk += chunk
        else:
            if current_chunk:
                final_parts.append(current_chunk)
            current_chunk = chunk
    if current_chunk:
        final_parts.append(current_chunk)
    if footer and final_parts:
        final_parts[-1] = final_parts[-1].rstrip() + "\n" + footer
    elif footer:
        final_parts.append(footer)
    return final_parts

def format_pro_osint_plain(title: str, query: str, data: Any) -> str:
    """Plain-text formatter for Pro OSINT (used for .txt file)."""
    lines = []
    lines.append(f"{title}")
    lines.append("")
    lines.append(f"Query: {query}")
    lines.append("")

    if isinstance(data, dict) and "data" in data and isinstance(data["data"], dict):
        sources = {k: v for k, v in data["data"].items() if k.startswith("source")}
        if not sources:
            lines.append("No sources found.")
        else:
            for source_key in sorted(sources.keys(), key=lambda x: int(x.replace("source",""))):
                source_data = sources[source_key]
                if not isinstance(source_data, dict):
                    continue
                src_title = source_data.get("title", "Untitled")
                src_desc = source_data.get("description", "")
                records = source_data.get("records", [])

                lines.append(f"--- {src_title} ---")
                if src_desc:
                    lines.append(src_desc)
                    lines.append("")

                if records:
                    for idx, record in enumerate(records, 1):
                        if not isinstance(record, dict):
                            lines.append(f"Record {idx}: {record}")
                            continue
                        lines.append(f"Record {idx}:")
                        for key, value in record.items():
                            if value is None:
                                continue
                            lines.append(f"  {key}: {value}")
                        lines.append("")
                else:
                    lines.append("No records.")
                lines.append("")
    else:
        lines.append("No data structure found.")

    return "\n".join(lines)

# ============================================================
# ACCESS CHECK
# ============================================================

def ensure_access(message: telebot.types.Message) -> Optional[str]:
    register_user(message)  # now handles referral inside if needed
    user_id = str(message.from_user.id)
    if users.get(user_id, {}).get("is_banned", False):
        bot.send_message(message.chat.id, "❌ Your access has been disabled.", reply_markup=types.ReplyKeyboardRemove())
        return None
    if bool(config.get("maintenance_mode", False)) and not is_admin(user_id):
        bot.send_message(message.chat.id, "🛠️ The bot is currently under maintenance. Please try again later.", reply_markup=types.ReplyKeyboardRemove())
        return None
    return user_id

# ============================================================
# USER COMMANDS
# ============================================================

@bot.message_handler(commands=["start"])
def start_command(message: telebot.types.Message) -> None:
    # Check for referral parameter
    referrer_id = None
    if message.text and len(message.text.split()) > 1:
        payload = message.text.split()[1]
        if payload.startswith("ref_"):
            ref_code = payload
            ref_user_id = ref_code.replace("ref_", "")
            if ref_user_id.isdigit() and ref_user_id in users:
                referrer_id = ref_user_id

    # Register user (with possible referrer)
    created = register_user(message, referred_by=referrer_id)
    user_id = str(message.from_user.id)

    if users.get(user_id, {}).get("is_banned", False):
        bot.send_message(message.chat.id, "❌ Your access has been disabled.")
        return

    if bool(config.get("maintenance_mode", False)) and not is_admin(user_id):
        bot.send_message(message.chat.id, "🛠️ The bot is currently under maintenance. Please try again later.", reply_markup=types.ReplyKeyboardRemove())
        return

    first_name = escape_html(message.from_user.first_name or "User")
    balance = get_balance(user_id)

    # Build the welcome message based on whether the user is new and referred
    if created:
        if referrer_id is not None:
            # Referred new user – combined welcome with referral info
            text = (
                f"👋 Welcome, {first_name}!\n\n"
                f"🎁 You received ₹{INITIAL_BALANCE} free balance.\n\n"
                f"🔗 You joined using a referral link.\n\n"
                f"💳 Current balance: <b>₹{balance}</b>\n\n"
                f"Choose a service using the buttons below."
            )
        else:
            # Regular new user
            text = (
                f"👋 <b>Welcome, {first_name}!</b>\n\n"
                f"🎁 You received <b>₹{INITIAL_BALANCE}</b> free balance.\n\n"
                f"💳 Current balance: <b>₹{balance}</b>\n\n"
                f"Choose a service using the buttons below."
            )
    else:
        # Existing user
        text = (
            f"👋 <b>Welcome back, {first_name}!</b>\n\n"
            f"💳 Current balance: <b>₹{balance}</b>\n\n"
            f"Choose a service using the buttons below."
        )

    bot.send_message(message.chat.id, text, reply_markup=main_keyboard())

@bot.message_handler(commands=["help"])
def help_command(message: telebot.types.Message) -> None:
    if not ensure_access(message):
        return
    bot.send_message(message.chat.id,
        "<b>How to use the bot</b>\n\n"
        "1. Select a service from the keyboard.\n"
        "2. Send the requested input.\n"
        "3. Balance is deducted only after a successful result (paid services only).\n\n"
        "Commands:\n"
        "/start - Open the main menu\n"
        "/balance - Check your balance\n"
        "/myid - Show your Telegram ID\n"
        "/cancel - Cancel the current operation",
        reply_markup=main_keyboard())

@bot.message_handler(commands=["balance"])
def balance_command(message: telebot.types.Message) -> None:
    user_id = ensure_access(message)
    if not user_id:
        return
    balance = get_balance(user_id)
    bot.send_message(message.chat.id, f"💳 Your current balance: <b>₹{balance}</b>", reply_markup=main_keyboard())

@bot.message_handler(commands=["myid"])
def myid_command(message: telebot.types.Message) -> None:
    user_id = ensure_access(message)
    if not user_id:
        return
    bot.send_message(message.chat.id, f"🆔 Your Telegram ID: <code>{user_id}</code>")

@bot.message_handler(commands=["cancel"])
def cancel_command(message: telebot.types.Message) -> None:
    user_id = str(message.from_user.id)
    with state_lock:
        user_states.pop(user_id, None)
    bot.send_message(message.chat.id, "✅ Current operation cancelled.", reply_markup=main_keyboard())

# ============================================================
# BUTTON HANDLERS
# ============================================================

def begin_service(message: telebot.types.Message, service_key: str, prompt: str, validator_name: str, cost: int) -> None:
    user_id = ensure_access(message)
    if not user_id:
        return
    service = config.get("services", {}).get(service_key, {})
    if not service.get("enabled", False):
        bot.send_message(message.chat.id, "❌ This service is currently disabled.", reply_markup=main_keyboard())
        return
    if cost > 0 and not is_admin(user_id) and get_balance(user_id) < cost:
        bot.send_message(message.chat.id,
            f"❌ You do not have enough balance.\n\nRequired: <b>₹{cost}</b>\nAvailable: <b>₹{get_balance(user_id)}</b>\n\nUse <b>Add Balance</b> to recharge.",
            reply_markup=main_keyboard())
        return
    with state_lock:
        user_states[user_id] = {"service_key": service_key, "validator": validator_name, "cost": cost}
    cost_text = f" (Cost: ₹{cost})" if cost > 0 else " (Free)"
    bot.send_message(message.chat.id, f"{prompt}{cost_text}", reply_markup=cancel_keyboard())

@bot.message_handler(func=lambda message: message.text == BUTTON_ADHAAR_SEARCH)
def adhaar_search_button(message: telebot.types.Message) -> None:
    begin_service(message, "adhaar_search", "🪪 Please send the 12-digit Adhaar number.\n\nExample: <code>123456789012</code>", "adhaar", 10)

@bot.message_handler(func=lambda message: message.text == BUTTON_NUMBER_INFO)
def number_info_button(message: telebot.types.Message) -> None:
    begin_service(message, "number_info", "📞 Please send the 10-digit phone number (without + or spaces).\n\nExample: <code>9876543210</code>", "phone", 10)

@bot.message_handler(func=lambda message: message.text == BUTTON_TG_TO_NUM)
def tg_to_num_button(message: telebot.types.Message) -> None:
    begin_service(message, "tg_to_num", "📞 Please send the Telegram username or userid (without @).\n\nExample: <code>username</code>", "username", 10)

@bot.message_handler(func=lambda message: message.text == BUTTON_VEHICLE_TO_OWNER)
def vehicle_to_owner_button(message: telebot.types.Message) -> None:
    begin_service(message, "vehicle_to_owner", "🚗 Please send the vehicle registration number.\n\nExample: <code>MH12AB1234</code>", "vehicle", 25)

@bot.message_handler(func=lambda message: message.text == BUTTON_ADVANCE_VEHICLE)
def advance_vehicle_button(message: telebot.types.Message) -> None:
    begin_service(message, "advance_vehicle", "🔧 Please send the vehicle registration number for advanced details.\n\nExample: <code>MH12AB1234</code>", "vehicle", 35)

@bot.message_handler(func=lambda message: message.text == BUTTON_ADHAAR_TO_RATION)
def adhaar_to_ration_button(message: telebot.types.Message) -> None:
    begin_service(message, "adhaar_to_ration", "🪪 Please send the Adhaar number to check ration card details.\n\nExample: <code>123456789012</code>", "adhaar", 10)

@bot.message_handler(func=lambda message: message.text == BUTTON_BOMBER)
def bomber_button(message: telebot.types.Message) -> None:
    begin_service(message, "bomber", "💣 Please send a phone number (target for demonstration).\n\nExample: <code>9876543210</code>", "phone", 0)

@bot.message_handler(func=lambda message: message.text == BUTTON_IFSC)
def ifsc_button(message: telebot.types.Message) -> None:
    begin_service(message, "ifsc_info", "🏦 Please send the 11-character IFSC code.\n\nExample: <code>SBIN0001234</code>", "ifsc", 0)

@bot.message_handler(func=lambda message: message.text == BUTTON_IP)
def ip_button(message: telebot.types.Message) -> None:
    begin_service(message, "ip_info", "🌐 Please send a public IPv4 address.\n\nExample: <code>8.8.8.8</code>", "ip", 0)

@bot.message_handler(func=lambda message: message.text == BUTTON_DOMAIN)
def domain_button(message: telebot.types.Message) -> None:
    begin_service(message, "domain_info", "🌍 Please send a domain name.\n\nExample: <code>example.com</code>", "domain", 0)

@bot.message_handler(func=lambda message: message.text == BUTTON_INSTA)
def insta_button(message: telebot.types.Message) -> None:
    begin_service(message, "instagram_public", "📸 Please send a public Instagram username.\n\nExample: <code>instagram</code>", "username", 0)

@bot.message_handler(func=lambda message: message.text == BUTTON_EMAIL_SEARCH)
def email_search_button(message: telebot.types.Message) -> None:
    begin_service(message, "email_security", "📧 Please send an email address you own or are authorised to check.", "email", 0)

# ---- Invite & Earn Button (modified) ----
@bot.message_handler(func=lambda message: message.text == BUTTON_INVITE)
def invite_button(message: telebot.types.Message) -> None:
    user_id = ensure_access(message)
    if not user_id:
        return
    user = get_user(user_id)
    referral_link = get_referral_link(user_id)
    total_referrals = len(user.get("referrals", []))
    referral_earnings = user.get("referral_earnings", 0)

    # Show the raw link as plain text (Telegram makes it clickable)
    text = (
        f"🎁 <b>Invite & Earn</b>\n\n"
        f"Share your referral link with friends and earn <b>₹{REFERRAL_BONUS}</b> for each new user who joins!\n"
        f"Your link:\n{referral_link}\n\n"
        f"📊 <b>Your Stats</b>\n"
        f"• Total Referrals: <b>{total_referrals}</b>\n"
        f"• Earnings from referrals: <b>₹{referral_earnings}</b>\n\n"
        f"Share this link with your friends and start earning!"
    )
    bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=main_keyboard())

@bot.message_handler(func=lambda message: message.text == BUTTON_PRO_OSINT)
def pro_osint_button(message: telebot.types.Message) -> None:
    user_id = ensure_access(message)
    if not user_id:
        return
    user = get_user(user_id)
    first_name = user.get("first_name", "User")
    pro_info = (
        f"👋 Hello, {escape_html(first_name)}.!\n"
        "🔎 Before you the best search engine according to open data.\n"
        "Here is a list of what you can look for:\n"
        "┣📧 Email\n"
        "┣📞 Phones\n"
        "┣👤 Names\n"
        "┣👥 Nicknames\n"
        "┣📍 IP\n"
        "┣🔒 Passwords\n"
        "┣🌐 Domains\n"
        "┣🏢 Company\n"
        "┣🚗 Autonomer\n"
        "┣📇 VIN\n"
        "┣💸 Inn\n"
        "┣🪪 Snils\n"
        "┣✈ Telegram id\n"
        "┣📘 VK ID\n"
        "┣📖 Facebook ID\n"
        "┗🛂 Passports\n"
        "And many other data\n\n"
        "⚠️ Especially sensitive information (bank cards and passwords that can still be relevant) is partially hidden. "
        "The use of a bot with any evil intent is strictly prohibited.\n\n"
        "💎 For the full use of the bot, it is necessary to have a subscription.\n\n"
        "💳 <b>Cost:</b> ₹50 per search"
    )
    bot.send_message(message.chat.id, pro_info, reply_markup=main_keyboard())
    begin_service(message, "pro_osint", "🔍 Please send your search query (email, phone, name, etc.):", "any", 50)

@bot.message_handler(func=lambda message: message.text == BUTTON_PROFILE)
def profile_button(message: telebot.types.Message) -> None:
    user_id = ensure_access(message)
    if not user_id:
        return
    user = get_user(user_id)
    username = f"@{user.get('username')}" if user.get("username") else "Not set"
    balance = get_balance(user_id)
    role = "Admin" if is_admin(user_id) else "User"
    referrals = len(user.get("referrals", []))
    referral_earnings = user.get("referral_earnings", 0)

    text = (
        "✨ <b>My Profile</b> ✨\n\n"
        f"👑 <b>Role:</b> {role}\n"
        f"🆔 <b>User ID:</b> <code>{user_id}</code>\n"
        f"📛 <b>Name:</b> {escape_html(user.get('first_name') or 'Not set')}\n"
        f"🔗 <b>Username:</b> {escape_html(username)}\n"
        f"💰 <b>Balance:</b> ₹{balance}\n"
        f"📅 <b>Joined:</b> {escape_html(user.get('joined_at', 'Unknown'))}\n"
        f"📨 <b>Total requests:</b> {int(user.get('total_requests', 0))}\n"
        f"✅ <b>Successful requests:</b> {int(user.get('successful_requests', 0))}\n"
        f"🎁 <b>Referrals:</b> {referrals}\n"
        f"💵 <b>Referral Earnings:</b> ₹{referral_earnings}\n"
    )
    bot.send_message(message.chat.id, text, reply_markup=main_keyboard())

@bot.message_handler(func=lambda message: message.text == BUTTON_SUPPORT)
def support_button(message: telebot.types.Message) -> None:
    if not ensure_access(message):
        return
    bot.send_message(message.chat.id, f"📞 For support, contact {escape_html(SUPPORT_USERNAME)}", reply_markup=main_keyboard())

@bot.message_handler(func=lambda message: message.text == BUTTON_CANCEL)
def cancel_button(message: telebot.types.Message) -> None:
    cancel_command(message)

@bot.message_handler(func=lambda message: message.text == BUTTON_MORE_TOOLS)
def more_tools_button(message: telebot.types.Message) -> None:
    if not ensure_access(message):
        return
    bot.send_message(message.chat.id, "🧰 <b>More Tools (Free)</b>\n\nChoose a tool:", reply_markup=more_tools_keyboard())

@bot.message_handler(func=lambda message: message.text == BUTTON_BACK)
def back_button(message: telebot.types.Message) -> None:
    if not ensure_access(message):
        return
    bot.send_message(message.chat.id, "🏠 Main menu", reply_markup=main_keyboard())

# ============================================================
# ADVANCED PAYMENT SYSTEM (FIXED)
# ============================================================

@bot.message_handler(func=lambda message: message.text == BUTTON_ADD_BALANCE)
def add_balance_button(message: telebot.types.Message) -> None:
    user_id = ensure_access(message)
    if not user_id:
        return
    with state_lock:
        user_states[user_id] = {"payment_state": PAYMENT_STATE_SELECTING}
    markup = types.InlineKeyboardMarkup(row_width=2)
    amounts = [50, 150, 300, 500]
    for amt in amounts:
        markup.add(types.InlineKeyboardButton(f"₹{amt}", callback_data=f"pay_{amt}"))
    bot.send_message(message.chat.id, "💰 Select the amount you want to add:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("pay_"))
def handle_payment_amount_selection(call: types.CallbackQuery) -> None:
    user_id = str(call.from_user.id)
    amount = int(call.data.split("_")[1])
    user_data = get_user(user_id)
    username = user_data.get("username", "No username")
    txn_id = f"TXN_{int(time.time())}_{user_id}_{random.randint(1000,9999)}"
    pending_transactions[txn_id] = {
        "user_id": user_id,
        "username": username,
        "amount": amount,
        "status": "pending",
        "timestamp": now_string(),
        "screenshot_msg_id": None,
        "admin_action_msg_id": None,
    }
    save_transactions()
    with state_lock:
        user_states[user_id] = {
            "payment_state": PAYMENT_STATE_CONFIRMING,
            "txn_id": txn_id,
            "amount": amount
        }
    try:
        bot.delete_message(chat_id=call.message.chat.id, message_id=call.message.message_id)
    except Exception:
        pass

    upi_id = "sahilmodz@ybl"
    upi_name = "SahilModz"
    upi_link = f"upi://pay?pa={upi_id}&pn={upi_name}&am={amount}&cu=INR&tn={txn_id}"
    qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data={upi_link}"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✅ I have paid", callback_data=f"confirm_pay_{txn_id}"))
    bot.send_photo(
        call.message.chat.id,
        qr_url,
        caption=(
            f"💳 <b>Payment Request</b>\n\n"
            f"💵 Amount: ₹{amount}\n"
            f"🆔 Transaction ID: <code>{txn_id}</code>\n\n"
            f"Scan the QR code or send payment to UPI: <code>{upi_id}</code>\n"
            f"After completing the payment, click the button below.\n\n"
            f"⏳ Status: <b>Pending</b>"
        ),
        reply_markup=markup
    )
    bot.answer_callback_query(call.id, f"Transaction {txn_id} created. Pay ₹{amount}.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("confirm_pay_"))
def handle_payment_confirmation(call: types.CallbackQuery) -> None:
    user_id = str(call.from_user.id)
    txn_id = call.data.split("_", 2)[2]
    if txn_id not in pending_transactions:
        bot.answer_callback_query(call.id, "Transaction not found. Please start again.")
        return
    txn = pending_transactions[txn_id]
    if txn["user_id"] != user_id:
        bot.answer_callback_query(call.id, "This transaction does not belong to you.")
        return
    if txn["status"] != "pending":
        bot.answer_callback_query(call.id, f"This transaction is already {txn['status']}.")
        return
    with state_lock:
        user_states[user_id] = {
            "payment_state": PAYMENT_STATE_AWAITING_SCREENSHOT,
            "txn_id": txn_id
        }
    bot.edit_message_caption(
        caption=(
            f"💳 <b>Payment Request</b>\n\n"
            f"💵 Amount: ₹{txn['amount']}\n"
            f"🆔 Transaction ID: <code>{txn_id}</code>\n\n"
            f"📤 Please upload the payment screenshot now as a photo."
        ),
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=None
    )
    bot.answer_callback_query(call.id, "Please upload your payment screenshot.")

@bot.message_handler(content_types=["photo"])
def handle_payment_screenshot(message: telebot.types.Message) -> None:
    user_id = str(message.from_user.id)
    with state_lock:
        state = user_states.get(user_id, {})
    if state.get("payment_state") != PAYMENT_STATE_AWAITING_SCREENSHOT:
        return
    txn_id = state.get("txn_id")
    if not txn_id or txn_id not in pending_transactions:
        bot.send_message(message.chat.id, "❌ No pending transaction found. Please start again from Add Balance.")
        with state_lock:
            user_states.pop(user_id, None)
        return
    txn = pending_transactions[txn_id]
    if txn["user_id"] != user_id:
        bot.send_message(message.chat.id, "❌ This transaction does not belong to you.")
        with state_lock:
            user_states.pop(user_id, None)
        return
    if txn["status"] != "pending":
        bot.send_message(message.chat.id, f"❌ This transaction is already {txn['status']}.")
        with state_lock:
            user_states.pop(user_id, None)
        return
    user = get_user(user_id)
    username = user.get("username", "No username")
    first_name = user.get("first_name", "Unknown")
    admin_caption = (
        f"📸 <b>Payment Screenshot Received</b>\n\n"
        f"👤 <b>User:</b> {escape_html(first_name)} (@{escape_html(username)})\n"
        f"🆔 <b>User ID:</b> <code>{user_id}</code>\n"
        f"💵 <b>Amount:</b> ₹{txn['amount']}\n"
        f"🆔 <b>Transaction ID:</b> <code>{txn_id}</code>\n"
        f"🕒 <b>Time:</b> {now_string()}\n\n"
        f"Please approve or reject this payment."
    )
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("✅ Approve", callback_data=f"approve_{txn_id}"),
        types.InlineKeyboardButton("❌ Reject", callback_data=f"reject_{txn_id}")
    )
    for admin_id in ADMIN_IDS:
        try:
            msg = bot.send_photo(
                admin_id,
                message.photo[-1].file_id,
                caption=admin_caption,
                reply_markup=markup,
                parse_mode="HTML"
            )
            if "admin_action_msg_id" not in txn or txn["admin_action_msg_id"] is None:
                txn["admin_action_msg_id"] = msg.message_id
                txn["admin_chat_id"] = admin_id
        except Exception as e:
            logger.error(f"Failed to forward screenshot to admin {admin_id}: {e}")
    txn["screenshot_msg_id"] = message.message_id
    txn["screenshot_chat_id"] = message.chat.id
    save_transactions()
    bot.send_message(
        message.chat.id,
        "✅ Your payment screenshot has been sent to the admin for verification.\n"
        "You will be notified once it is approved or rejected.",
        reply_markup=main_keyboard()
    )
    with state_lock:
        user_states.pop(user_id, None)

@bot.callback_query_handler(func=lambda call: call.data.startswith("approve_") or call.data.startswith("reject_"))
def handle_admin_payment_action(call: types.CallbackQuery) -> None:
    admin_id = str(call.from_user.id)
    if not is_admin(admin_id):
        bot.answer_callback_query(call.id, "You are not authorised.")
        return
    action, txn_id = call.data.split("_", 1)
    if txn_id not in pending_transactions:
        bot.answer_callback_query(call.id, "Transaction not found.")
        return
    txn = pending_transactions[txn_id]
    if txn["status"] != "pending":
        bot.answer_callback_query(call.id, f"Transaction already {txn['status']}.")
        return
    user_id = txn["user_id"]
    amount = txn["amount"]

    if action == "approve":
        if add_balance(user_id, amount):
            txn["status"] = "completed"
            txn["completed_at"] = now_string()
            txn["approved_by"] = admin_id
            save_transactions()
            try:
                bot.send_message(
                    int(user_id),
                    f"✅ Your payment of <b>₹{amount}</b> has been approved!\n"
                    f"Your new balance is <b>₹{get_balance(user_id)}</b>.",
                    parse_mode="HTML",
                    reply_markup=main_keyboard()
                )
            except Exception:
                pass
            try:
                bot.edit_message_caption(
                    caption=call.message.caption + "\n\n✅ <b>Approved</b> by admin.",
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    reply_markup=None
                )
            except Exception:
                pass
            bot.answer_callback_query(call.id, f"Payment of ₹{amount} approved.")
        else:
            bot.answer_callback_query(call.id, "Failed to add balance. User not found?")
    else:
        txn["status"] = "rejected"
        txn["rejected_at"] = now_string()
        txn["rejected_by"] = admin_id
        save_transactions()
        try:
            bot.send_message(
                int(user_id),
                f"❌ Your payment of <b>₹{amount}</b> has been rejected by admin.\n"
                f"Please contact support if you think this is a mistake.",
                parse_mode="HTML",
                reply_markup=main_keyboard()
            )
        except Exception:
            pass
        try:
            bot.edit_message_caption(
                caption=call.message.caption + "\n\n❌ <b>Rejected</b> by admin.",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=None
            )
        except Exception:
            pass
        bot.answer_callback_query(call.id, "Payment rejected.")

@bot.message_handler(commands=["pending"])
def pending_transactions_command(message: telebot.types.Message) -> None:
    if not admin_only(message):
        return
    pending_list = []
    for txn_id, txn in pending_transactions.items():
        if txn["status"] == "pending":
            username = txn.get("username", "No username")
            pending_list.append(f"TXN: {txn_id} | User: @{username} ({txn['user_id']}) | ₹{txn['amount']}")
    if not pending_list:
        bot.send_message(message.chat.id, "No pending transactions.")
    else:
        bot.send_message(message.chat.id, "📋 Pending transactions:\n" + "\n".join(pending_list[:20]))

# ============================================================
# CORE – NO‑DATA DETECTION
# ============================================================

def has_valid_data(data: Any) -> bool:
    if data is None:
        return False
    if isinstance(data, dict):
        if not data:
            return False
        if "status" in data:
            status_val = data["status"]
            if isinstance(status_val, str) and status_val.lower() in ("failed", "error", "not found", "invalid"):
                return False
        error_indicators = ["error", "message", "msg"]
        for key in error_indicators:
            if key in data:
                val = data[key]
                if isinstance(val, str):
                    lower_val = val.lower()
                    if any(phrase in lower_val for phrase in ["no data", "not found", "not available", "invalid", "no aadhaar", "phone number not found", "error"]):
                        return False
        if "success" in data and data["success"] is False:
            return False
        if "data" in data:
            return has_valid_data(data["data"])
        numeric_keys = [k for k in data.keys() if k.isdigit()]
        if numeric_keys:
            for k in numeric_keys:
                item = data[k]
                if isinstance(item, dict):
                    if any(val is not None and str(val).strip() for val in item.values()):
                        return True
            return False
        for value in data.values():
            if has_valid_data(value):
                return True
        return False
    elif isinstance(data, list):
        if not data:
            return False
        for item in data:
            if has_valid_data(item):
                return True
        return False
    else:
        if isinstance(data, str) and data.strip() == "":
            return False
        if data is None:
            return False
        return True

# ============================================================
# STATE INPUT HANDLER (for services)
# ============================================================

@bot.message_handler(
    func=lambda message: (
        bool(message.text)
        and str(message.from_user.id) in user_states
        and message.text != BUTTON_CANCEL
        and user_states[str(message.from_user.id)].get("payment_state") is None
    )
)
def handle_service_input(message: telebot.types.Message) -> None:
    user_id = ensure_access(message)
    if not user_id:
        return
    with state_lock:
        state = user_states.get(user_id, {}).copy()
    if not state or state.get("payment_state"):
        return

    raw_query = message.text.strip()
    validator_name = state.get("validator")
    validators = {
        "ifsc": validate_ifsc,
        "ip": validate_ip,
        "domain": validate_domain,
        "username": validate_username,
        "email": validate_email,
        "adhaar": validate_adhaar,
        "phone": validate_phone,
        "vehicle": validate_vehicle,
        "any": validate_any,
    }
    validator = validators.get(validator_name)
    if validator and not validator(raw_query):
        if validator_name == "phone":
            bot.send_message(message.chat.id, "❌ Invalid phone number. Please enter exactly 10 digits (without + or spaces).", reply_markup=cancel_keyboard())
        else:
            bot.send_message(message.chat.id, "❌ Invalid input format. Please check it and try again, or press Cancel.", reply_markup=cancel_keyboard())
        return

    if validator_name == "phone":
        query = clean_phone(raw_query)
    else:
        query = raw_query

    service_key = str(state.get("service_key"))
    cost = int(state.get("cost", 0))

    if cost > 0 and not is_admin(user_id) and get_balance(user_id) < cost:
        with state_lock:
            user_states.pop(user_id, None)
        bot.send_message(message.chat.id, f"❌ Insufficient balance. Required: ₹{cost}", reply_markup=main_keyboard())
        return

    processing_msg = bot.send_message(message.chat.id, "⏳ Processing your request...", reply_markup=types.ReplyKeyboardRemove())
    with file_lock:
        users[user_id]["total_requests"] = int(users[user_id].get("total_requests", 0)) + 1
        save_users()

    try:
        result = call_configured_api(service_key, query)
        data = result.get("data", {})

        if not has_valid_data(data):
            raise RuntimeError("No data found")

        if cost > 0:
            if not deduct_balance(user_id, cost):
                raise RuntimeError("Insufficient balance after processing.")

        with file_lock:
            users[user_id]["successful_requests"] = int(users[user_id].get("successful_requests", 0)) + 1
            save_users()

        title_map = {
            "ifsc_info": "🏦 IFSC Information",
            "ip_info": "🌐 IP Information",
            "domain_info": "🌍 Domain Information",
            "instagram_public": "📸 Instagram Public Information",
            "email_security": "📧 Email Search Result",
            "adhaar_search": "🪪 Adhaar Search Result",
            "number_info": "📞 Number Information Result",
            "tg_to_num": "📞 Telegram To Number Result",
            "vehicle_to_owner": "🚗 Vehicle to Owner Result",
            "advance_vehicle": "🔧 Advance Vehicle Info Result",
            "adhaar_to_ration": "🪪 Adhaar to Ration Result",
            "bomber": "💣 Bomber Result",
            "pro_osint": "🧭 Pro OSINT Result",
        }

        remaining = get_balance(user_id)

        try:
            bot.delete_message(chat_id=message.chat.id, message_id=processing_msg.message_id)
        except Exception:
            pass

        if service_key == "pro_osint":
            formatted = format_pro_osint_plain(title_map.get(service_key, "Result"), query, data)
            if cost > 0:
                formatted += f"\n\nCost: ₹{cost}\nRemaining balance: ₹{remaining}"
            else:
                formatted += f"\n\nThis service is free.\nBalance: ₹{remaining}"
            file_content = formatted.encode('utf-8')
            file_name = f"pro_osint_{query[:30]}_{int(time.time())}.txt"
            bot.send_document(
                message.chat.id,
                (file_name, io.BytesIO(file_content)),
                caption=f"📄 Pro OSINT result for <code>{escape_html(query)}</code>",
                reply_markup=main_keyboard()
            )
        else:
            formatted = format_api_result(title_map.get(service_key, "Result"), query, data)
            if cost > 0:
                formatted += f"\n\n💳 <b>Cost:</b> ₹{cost}\n💰 <b>Remaining balance:</b> ₹{remaining}"
            else:
                formatted += f"\n\n💳 This service is <b>free</b>.\n💰 <b>Balance:</b> ₹{remaining}"
            parts = split_long_message(formatted, 4000)
            for part in parts:
                bot.send_message(message.chat.id, part, reply_markup=main_keyboard())

        bot.send_message(message.chat.id, "Choose another service:", reply_markup=main_keyboard())
        add_log(user_id, service_key, query, True, cost)

    except Exception as exc:
        logger.exception("Service request failed")
        try:
            bot.delete_message(chat_id=message.chat.id, message_id=processing_msg.message_id)
        except Exception:
            pass

        error_str = str(exc)
        if "503" in error_str or "Service Unavailable" in error_str:
            error_text = "❌ The API service is currently unavailable. Please try again later."
        elif "No data found" in error_str or "No results" in error_str:
            error_text = "❌ Opps! No Data Found\n• Please Try Another Number"
        else:
            error_text = "❌ API request failed. Please try again later."
        bot.send_message(message.chat.id, error_text, reply_markup=main_keyboard())
        bot.send_message(message.chat.id, "No balance was deducted.", reply_markup=main_keyboard())
        add_log(user_id, service_key, query, False, 0, str(exc))

    finally:
        with state_lock:
            user_states.pop(user_id, None)

# ============================================================
# ADMIN COMMANDS (unchanged)
# ============================================================

def admin_only(message: telebot.types.Message) -> bool:
    user_id = str(message.from_user.id)
    if not is_admin(user_id):
        bot.send_message(message.chat.id, "❌ You are not authorised.")
        return False
    return True

@bot.message_handler(commands=["addbalance"])
def addbalance_command(message: telebot.types.Message) -> None:
    if not admin_only(message):
        return
    parts = message.text.split()
    if len(parts) != 3:
        bot.send_message(message.chat.id, "Usage: <code>/addbalance USER_ID AMOUNT</code>")
        return
    target_id = parts[1]
    try:
        amount = int(parts[2])
        if amount <= 0:
            raise ValueError
    except ValueError:
        bot.send_message(message.chat.id, "❌ Amount must be a positive integer.")
        return
    if add_balance(target_id, amount):
        bot.send_message(message.chat.id, f"✅ Added ₹{amount} to <code>{target_id}</code>.\nNew balance: <b>₹{get_balance(target_id)}</b>")
    else:
        bot.send_message(message.chat.id, "❌ User not found. Ask the user to /start first.")

@bot.message_handler(commands=["setbalance"])
def setbalance_command(message: telebot.types.Message) -> None:
    if not admin_only(message):
        return
    parts = message.text.split()
    if len(parts) != 3:
        bot.send_message(message.chat.id, "Usage: <code>/setbalance USER_ID AMOUNT</code>")
        return
    target_id = parts[1]
    try:
        amount = int(parts[2])
        if amount < 0:
            raise ValueError
    except ValueError:
        bot.send_message(message.chat.id, "❌ Amount must be zero or higher.")
        return
    if set_balance(target_id, amount):
        bot.send_message(message.chat.id, f"✅ Balance for <code>{target_id}</code> set to <b>₹{amount}</b>.")
    else:
        bot.send_message(message.chat.id, "❌ User not found.")

@bot.message_handler(commands=["confirmpayment"])
def confirm_payment_command(message: telebot.types.Message) -> None:
    if not admin_only(message):
        return
    parts = message.text.split()
    if len(parts) != 2:
        bot.send_message(message.chat.id, "Usage: <code>/confirmpayment TXN_ID</code>")
        return
    txn_id = parts[1]
    if txn_id not in pending_transactions:
        bot.send_message(message.chat.id, "❌ Transaction not found or already processed.")
        return
    txn = pending_transactions[txn_id]
    if txn["status"] != "pending":
        bot.send_message(message.chat.id, f"❌ Transaction already {txn['status']}.")
        return
    user_id = txn["user_id"]
    amount = txn["amount"]
    if add_balance(user_id, amount):
        txn["status"] = "completed"
        txn["completed_at"] = now_string()
        save_transactions()
        bot.send_message(message.chat.id, f"✅ Payment confirmed. Added ₹{amount} to <code>{user_id}</code>.")
        try:
            bot.send_message(int(user_id), f"✅ Your payment of ₹{amount} has been confirmed. Your balance is now ₹{get_balance(user_id)}.")
        except Exception:
            pass
    else:
        bot.send_message(message.chat.id, "❌ Failed to add balance. User not found.")

@bot.message_handler(commands=["cancelpayment"])
def cancel_payment_command(message: telebot.types.Message) -> None:
    if not admin_only(message):
        return
    parts = message.text.split()
    if len(parts) != 2:
        bot.send_message(message.chat.id, "Usage: <code>/cancelpayment TXN_ID</code>")
        return
    txn_id = parts[1]
    if txn_id not in pending_transactions:
        bot.send_message(message.chat.id, "❌ Transaction not found.")
        return
    txn = pending_transactions[txn_id]
    if txn["status"] != "pending":
        bot.send_message(message.chat.id, f"❌ Transaction already {txn['status']}.")
        return
    txn["status"] = "cancelled"
    txn["cancelled_at"] = now_string()
    save_transactions()
    bot.send_message(message.chat.id, f"✅ Transaction {txn_id} cancelled.")
    try:
        bot.send_message(int(txn["user_id"]), f"❌ Your payment request (₹{txn['amount']}) was cancelled by admin.")
    except Exception:
        pass

@bot.message_handler(commands=["userinfo"])
def userinfo_command(message: telebot.types.Message) -> None:
    if not admin_only(message):
        return
    parts = message.text.split()
    if len(parts) != 2:
        bot.send_message(message.chat.id, "Usage: <code>/userinfo USER_ID</code>")
        return
    target_id = parts[1]
    user = users.get(target_id)
    if not user:
        bot.send_message(message.chat.id, "❌ User not found.")
        return
    bot.send_message(message.chat.id,
        "<b>User Information</b>\n\n"
        f"<b>ID:</b> <code>{target_id}</code>\n"
        f"<b>Username:</b> {escape_html(user.get('username') or 'Not set')}\n"
        f"<b>Name:</b> {escape_html(user.get('first_name') or '')}\n"
        f"<b>Balance:</b> ₹{int(user.get('balance', 0))}\n"
        f"<b>Banned:</b> {bool(user.get('is_banned', False))}\n"
        f"<b>Joined:</b> {escape_html(user.get('joined_at', 'Unknown'))}\n"
        f"<b>Last seen:</b> {escape_html(user.get('last_seen', 'Unknown'))}\n"
        f"<b>Total requests:</b> {int(user.get('total_requests', 0))}\n"
        f"<b>Successful:</b> {int(user.get('successful_requests', 0))}\n"
        f"<b>Referrals:</b> {len(user.get('referrals', []))}\n"
        f"<b>Referral earnings:</b> ₹{user.get('referral_earnings', 0)}")

@bot.message_handler(commands=["stats"])
def stats_command(message: telebot.types.Message) -> None:
    if not admin_only(message):
        return
    total_users = len(users)
    banned_users = sum(1 for user in users.values() if user.get("is_banned", False))
    total_balance = sum(int(user.get("balance", 0)) for user in users.values())
    total_requests = sum(int(user.get("total_requests", 0)) for user in users.values())
    successful = sum(int(user.get("successful_requests", 0)) for user in users.values())
    bot.send_message(message.chat.id,
        "<b>📊 Bot Statistics</b>\n\n"
        f"<b>Total users:</b> {total_users}\n"
        f"<b>Banned users:</b> {banned_users}\n"
        f"<b>Total user balance:</b> ₹{total_balance}\n"
        f"<b>Total requests:</b> {total_requests}\n"
        f"<b>Successful requests:</b> {successful}")

@bot.message_handler(commands=["ban"])
def ban_command(message: telebot.types.Message) -> None:
    if not admin_only(message):
        return
    parts = message.text.split()
    if len(parts) != 2:
        bot.send_message(message.chat.id, "Usage: <code>/ban USER_ID</code>")
        return
    target_id = parts[1]
    if target_id not in users:
        bot.send_message(message.chat.id, "❌ User not found.")
        return
    users[target_id]["is_banned"] = True
    save_users()
    bot.send_message(message.chat.id, f"✅ User <code>{target_id}</code> banned.")

@bot.message_handler(commands=["unban"])
def unban_command(message: telebot.types.Message) -> None:
    if not admin_only(message):
        return
    parts = message.text.split()
    if len(parts) != 2:
        bot.send_message(message.chat.id, "Usage: <code>/unban USER_ID</code>")
        return
    target_id = parts[1]
    if target_id not in users:
        bot.send_message(message.chat.id, "❌ User not found.")
        return
    users[target_id]["is_banned"] = False
    save_users()
    bot.send_message(message.chat.id, f"✅ User <code>{target_id}</code> unbanned.")

@bot.message_handler(commands=["broadcast"])
def broadcast_command(message: telebot.types.Message) -> None:
    if not admin_only(message):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        bot.send_message(message.chat.id, "Usage: <code>/broadcast YOUR MESSAGE</code>")
        return
    broadcast_text = parts[1].strip()
    sent = 0
    failed = 0
    status = bot.send_message(message.chat.id, "⏳ Broadcasting...")
    for target_id, user in list(users.items()):
        if user.get("is_banned", False):
            continue
        try:
            bot.send_message(int(target_id), f"📢 <b>Broadcast</b>\n\n{escape_html(broadcast_text)}")
            sent += 1
        except Exception:
            failed += 1
    bot.edit_message_text(f"✅ Broadcast complete.\n\nSent: <b>{sent}</b>\nFailed: <b>{failed}</b>",
                          chat_id=message.chat.id, message_id=status.message_id)

@bot.message_handler(commands=["users"])
def users_command(message: telebot.types.Message) -> None:
    if not admin_only(message):
        return
    total_users = len(users)
    active_users = sum(1 for user in users.values() if not user.get("is_banned", False))
    banned_users = total_users - active_users
    bot.send_message(message.chat.id,
        "👥 <b>User Summary</b>\n\n"
        f"📊 <b>Total registered:</b> {total_users}\n"
        f"✅ <b>Active:</b> {active_users}\n"
        f"🚫 <b>Banned:</b> {banned_users}")

@bot.message_handler(commands=["userlist"])
def userlist_command(message: telebot.types.Message) -> None:
    if not admin_only(message):
        return
    export_path = os.path.join(BASE_DIR, "userlist_export.txt")
    lines = ["Telegram Credit Bot - User List", f"Generated: {now_string()}", "=" * 60, ""]
    for user_id, user in sorted(users.items(), key=lambda item: item[1].get("joined_at", "")):
        username = f"@{user.get('username')}" if user.get("username") else "Not set"
        lines.extend([
            f"User ID: {user_id}",
            f"Name: {user.get('first_name') or ''} {user.get('last_name') or ''}".strip(),
            f"Username: {username}",
            f"Balance: ₹{int(user.get('balance', 0))}",
            f"Joined: {user.get('joined_at', 'Unknown')}",
            f"Last seen: {user.get('last_seen', 'Unknown')}",
            f"Banned: {bool(user.get('is_banned', False))}",
            f"Total requests: {int(user.get('total_requests', 0))}",
            f"Successful requests: {int(user.get('successful_requests', 0))}",
            f"Referrals: {len(user.get('referrals', []))}",
            f"Referral earnings: ₹{user.get('referral_earnings', 0)}",
            "-" * 60
        ])
    with open(export_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    with open(export_path, "rb") as f:
        bot.send_document(message.chat.id, f, caption=f"👥 Exported {len(users)} registered users.")

@bot.message_handler(commands=["logs"])
def logs_command(message: telebot.types.Message) -> None:
    if not admin_only(message):
        return
    if not usage_logs:
        bot.send_message(message.chat.id, "📭 No usage logs are available.")
        return
    recent = usage_logs[-15:]
    lines = ["🧾 <b>Recent API Logs</b>", ""]
    for item in reversed(recent):
        status = "✅" if item.get("success") else "❌"
        username = item.get("username", "")
        user_display = f"@{username}" if username else item.get("user_id", "")
        lines.extend([
            f"{status} <b>{escape_html(item.get('service', 'unknown'))}</b>",
            f"👤 <code>{escape_html(user_display)}</code>",
            f"🔎 {escape_html(item.get('query', ''))}",
            f"🕒 {escape_html(item.get('time', ''))}",
            f"💳 Cost: ₹{int(item.get('cost', 0))}",
            ""
        ])
    bot.send_message(message.chat.id, "\n".join(lines)[:3900])

@bot.message_handler(commands=["clearlogs"])
def clearlogs_command(message: telebot.types.Message) -> None:
    if not admin_only(message):
        return
    with file_lock:
        usage_logs.clear()
        save_logs()
    bot.send_message(message.chat.id, "🧹 All usage logs have been cleared.")

@bot.message_handler(commands=["reload"])
def reload_command(message: telebot.types.Message) -> None:
    if not admin_only(message):
        return
    try:
        global config, BOT_TOKEN, ADMIN_IDS, SUPPORT_USERNAME, REQUEST_TIMEOUT
        new_config = read_json(CONFIG_PATH, {})
        config = new_config
        BOT_TOKEN = str(config.get("bot_token", BOT_TOKEN)).strip()
        ADMIN_IDS = {str(item) for item in config.get("admin_ids", [])}
        SUPPORT_USERNAME = str(config.get("support_username", "@YourSupportUsername"))
        REQUEST_TIMEOUT = int(config.get("request_timeout_seconds", 20))
        bot.send_message(message.chat.id,
            "🔄 <b>Configuration reloaded successfully.</b>\n\n"
            f"👑 Admins: {len(ADMIN_IDS)}\n"
            f"🛠️ Maintenance: {bool(config.get('maintenance_mode', False))}\n"
            f"⚙️ Services: {len(config.get('services', {}))}")
    except Exception as exc:
        logger.exception("Config reload failed")
        bot.send_message(message.chat.id, f"❌ Reload failed: {escape_html(exc)}")

@bot.message_handler(commands=["addadmin"])
def addadmin_command(message: telebot.types.Message) -> None:
    if not admin_only(message):
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        bot.send_message(message.chat.id, "Usage: <code>/addadmin USER_ID</code>")
        return
    target_id = parts[1]
    admin_list = [str(item) for item in config.get("admin_ids", [])]
    if target_id in admin_list:
        bot.send_message(message.chat.id, "⚠️ This user is already an admin.")
        return
    admin_list.append(target_id)
    config["admin_ids"] = admin_list
    save_config()
    reload_config_from_disk()
    bot.send_message(message.chat.id, f"✅ <code>{target_id}</code> added as admin.")

@bot.message_handler(commands=["removeadmin"])
def removeadmin_command(message: telebot.types.Message) -> None:
    if not admin_only(message):
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        bot.send_message(message.chat.id, "Usage: <code>/removeadmin USER_ID</code>")
        return
    target_id = parts[1]
    current_admin = str(message.from_user.id)
    if target_id == current_admin:
        bot.send_message(message.chat.id, "❌ You cannot remove your own admin access.")
        return
    admin_list = [str(item) for item in config.get("admin_ids", [])]
    if target_id not in admin_list:
        bot.send_message(message.chat.id, "❌ This user is not an admin.")
        return
    admin_list.remove(target_id)
    config["admin_ids"] = admin_list
    save_config()
    reload_config_from_disk()
    bot.send_message(message.chat.id, f"✅ <code>{target_id}</code> removed from admins.")

@bot.message_handler(commands=["maintenance"])
def maintenance_command(message: telebot.types.Message) -> None:
    if not admin_only(message):
        return
    parts = message.text.split()
    if len(parts) != 2 or parts[1].lower() not in {"on", "off"}:
        bot.send_message(message.chat.id, "Usage: <code>/maintenance on</code> or <code>/maintenance off</code>")
        return
    enabled = parts[1].lower() == "on"
    config["maintenance_mode"] = enabled
    save_config()
    status = "enabled 🛠️" if enabled else "disabled ✅"
    bot.send_message(message.chat.id, f"Maintenance mode {status}")

@bot.message_handler(commands=["service"])
def service_command(message: telebot.types.Message) -> None:
    if not admin_only(message):
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) != 3 or parts[1].lower() not in {"on", "off"}:
        bot.send_message(message.chat.id, "Usage:\n<code>/service on NAME</code>\n<code>/service off NAME</code>\n\nExamples: <code>ifsc</code>, <code>ip</code>, <code>domain</code>")
        return
    service_key = resolve_service_name(parts[2])
    if not service_key:
        available = ", ".join(config.get("services", {}).keys()) or "None"
        bot.send_message(message.chat.id, f"❌ Service not found.\n\nAvailable: <code>{escape_html(available)}</code>")
        return
    enabled = parts[1].lower() == "on"
    config["services"][service_key]["enabled"] = enabled
    save_config()
    status = "enabled ✅" if enabled else "disabled ⛔"
    bot.send_message(message.chat.id, f"Service <code>{escape_html(service_key)}</code> {status}")

@bot.message_handler(commands=["adminhelp"])
def adminhelp_command(message: telebot.types.Message) -> None:
    if not admin_only(message):
        return
    bot.send_message(message.chat.id,
        "👑 <b>Admin Commands</b>\n\n"
        "💰 /addbalance USER_ID AMOUNT\n"
        "🎯 /setbalance USER_ID AMOUNT\n"
        "✅ /confirmpayment TXN_ID\n"
        "❌ /cancelpayment TXN_ID\n"
        "📋 /pending - List pending transactions\n"
        "🔍 /userinfo USER_ID\n"
        "👥 /users\n"
        "📄 /userlist\n"
        "🧾 /logs\n"
        "🧹 /clearlogs\n"
        "🔄 /reload\n"
        "➕ /addadmin USER_ID\n"
        "➖ /removeadmin USER_ID\n"
        "🛠️ /maintenance on|off\n"
        "⚙️ /service on|off NAME\n"
        "📊 /stats\n"
        "🚫 /ban USER_ID\n"
        "✅ /unban USER_ID\n"
        "📢 /broadcast MESSAGE")

# ============================================================
# HELPERS
# ============================================================

def reload_config_from_disk() -> None:
    global config, BOT_TOKEN, ADMIN_IDS, SUPPORT_USERNAME, REQUEST_TIMEOUT
    new_config = read_json(CONFIG_PATH, {})
    if not isinstance(new_config, dict):
        raise RuntimeError("config.json must contain a JSON object.")
    config = new_config
    BOT_TOKEN = str(config.get("bot_token", BOT_TOKEN)).strip()
    ADMIN_IDS = {str(item) for item in config.get("admin_ids", [])}
    SUPPORT_USERNAME = str(config.get("support_username", "@YourSupportUsername"))
    REQUEST_TIMEOUT = int(config.get("request_timeout_seconds", 20))

def save_config() -> None:
    write_json(CONFIG_PATH, config)

def resolve_service_name(name: str) -> Optional[str]:
    normalized = name.strip().lower().replace("-", "_").replace(" ", "_")
    services = config.get("services", {})
    aliases = {
        "ifsc": "ifsc_info",
        "ip": "ip_info",
        "domain": "domain_info",
        "insta": "instagram_public",
        "instagram": "instagram_public",
        "email": "email_security",
        "adhaar": "adhaar_search",
        "number": "number_info",
        "tg": "tg_to_num",
        "vehicle": "vehicle_to_owner",
        "advance": "advance_vehicle",
        "ration": "adhaar_to_ration",
        "bomber": "bomber",
        "pro": "pro_osint",
        "leakk": "pro_osint",
    }
    if normalized in aliases and aliases[normalized] in services:
        return aliases[normalized]
    if normalized in services:
        return normalized
    for key in services:
        if key.lower() == normalized:
            return key
    return None

# ============================================================
# FALLBACK
# ============================================================

@bot.message_handler(content_types=["text"])
def fallback_handler(message: telebot.types.Message) -> None:
    if not ensure_access(message):
        return
    user_id = str(message.from_user.id)
    with state_lock:
        state = user_states.get(user_id, {})
    if state.get("payment_state"):
        return
    bot.send_message(message.chat.id, "Please select an option from the keyboard.", reply_markup=main_keyboard())

# ============================================================
# START BOT
# ============================================================

if __name__ == "__main__":
    logger.info("Bot is starting...")
    while True:
        try:
            bot.infinity_polling(timeout=30, long_polling_timeout=30, skip_pending=True, allowed_updates=["message", "callback_query"])
        except KeyboardInterrupt:
            logger.info("Bot stopped by user.")
            break
        except Exception:
            logger.exception("Polling crashed. Restarting in 5 seconds.")
            time.sleep(5)