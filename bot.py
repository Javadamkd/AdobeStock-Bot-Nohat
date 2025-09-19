import logging
import re
import requests
import asyncio
import random
import string
import os
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

# ====== CONFIG ======
TELEGRAM_BOT_TOKEN = "7597535024:AAHJf7wvTfvhsNMljywb-zOPGH4mPct-sBQ"
API_KEY = "8bOTKxiA1JnMaGVfGDp07hm7jLBXma"
API_BASE = "https://nehtw.com/api"
HEADERS = {"X-Api-Key": API_KEY}

# Google Sheets config
GSHEET_NAME = "BotUsers"  # Your Google Sheet name
GSHEET_WORKSHEET = "Users"  # Worksheet name
SERVICE_ACCOUNT_FILE = "service_account.json"  # Path to your JSON credentials

# ====== LOGGING ======
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# ====== IN-MEMORY DATABASE ======
USERS = {}  # user_id -> {"balance": int, "token": str, "verified": bool}
ADMIN_ID = 678232202  # <-- Your Telegram ID

# ====== STATES ======
TOKEN, ADMIN_ACTION, ADMIN_ADD_USER, ADMIN_USER_AMOUNT = range(4)

# ====== GOOGLE SHEETS HELPERS ======
def init_gsheet():
    creds = Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    client = gspread.authorize(creds)
    sheet = client.open(GSHEET_NAME).worksheet(GSHEET_WORKSHEET)
    return sheet

def load_users_from_sheet():
    global USERS
    sheet = init_gsheet()
    records = sheet.get_all_records()
    USERS = {}
    for row in records:
        user_id = int(row["user_id"])
        USERS[user_id] = {
            "token": row["token"],
            "balance": int(row["balance"]),
            "verified": False
        }

def save_users_to_sheet():
    sheet = init_gsheet()
    sheet.clear()
    sheet.append_row(["user_id", "token", "balance"])
    for uid, info in USERS.items():
        sheet.append_row([uid, info["token"], info["balance"]])

def add_user_to_sheet(uid, token, balance):
    sheet = init_gsheet()
    sheet.append_row([uid, token, balance])

def update_user_balance(uid):
    sheet = init_gsheet()
    records = sheet.get_all_records()
    for i, row in enumerate(records, start=2):
        if int(row["user_id"]) == uid:
            sheet.update(f"C{i}", USERS[uid]["balance"])
            break

# ====== USER COMMANDS ======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    load_users_from_sheet()
    user_id = update.message.from_user.id
    if user_id not in USERS or not USERS[user_id].get("verified"):
        await update.message.reply_text("🔑 Please enter your access token to use the bot:")
        return TOKEN
    return await show_main_menu(update, context)

async def check_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    entered = update.message.text.strip()
    user_data = USERS.get(user_id)

    if user_data and entered == user_data.get("token"):
        USERS[user_id]["verified"] = True
        await update.message.reply_text("✅ Token verified! Access granted.")
        return await show_main_menu(update, context)
    await update.message.reply_text("❌ Invalid token. Try again:")
    return TOKEN

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("💳 Balance", callback_data="balance")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "👋 Welcome!\n\nSend me an Adobe Stock link, and I'll fetch details + price. "
        "Then you can confirm or cancel your order.",
        reply_markup=reply_markup
    )
    return ConversationHandler.END

async def balance_func(update: Update, context: ContextTypes.DEFAULT_TYPE, from_callback=False):
    user_id = update.effective_user.id
    balance = USERS.get(user_id, {}).get("balance", 0)
    text = f"💰 Your Current Balance: {balance} points"
    if from_callback:
        await update.callback_query.edit_message_text(text)
    else:
        await update.message.reply_text(text)

# ====== LINK HANDLER ======
async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message.text.strip()
    user_id = update.message.from_user.id

    if user_id not in USERS or USERS[user_id].get("balance", 0) <= 0:
        await update.message.reply_text("❌ Your balance is 0. Ask admin to add points.")
        return

    match = re.search(r"stock\.adobe\.com/.+?/(\d+)", message)
    if not match:
        await update.message.reply_text("❌ Only Adobe Stock links are allowed.")
        return

    stock_id = match.group(1)
    site = "adobestock"
    cost = 1

    context.user_data["pending_order"] = {"site": site, "stock_id": stock_id, "cost": cost}

    keyboard = [
        [InlineKeyboardButton("✅ Confirm", callback_data="confirm_order"),
         InlineKeyboardButton("❌ Cancel", callback_data="cancel_order")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"📂 File detected from *{site.capitalize()}*\n"
        f"🆔 ID: `{stock_id}`\n💲 Price: {cost} point\n\nDo you want to place this order?",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

# ====== CONFIRM / CANCEL ======
async def confirm_or_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data
    user_id = query.from_user.id
    pending = context.user_data.get("pending_order")

    if not pending:
        await query.edit_message_text("❌ No pending order found.")
        return

    if choice == "cancel_order":
        context.user_data.pop("pending_order", None)
        await query.edit_message_text("❌ Order cancelled. No points deducted.")
        return

    if choice == "confirm_order":
        site = pending["site"]
        stock_id = pending["stock_id"]
        cost = pending["cost"]

        url = f"{API_BASE}/stockorder/{site}/{stock_id}"
        r = requests.get(url, headers=HEADERS)
        data = r.json()

        if data.get("success"):
            task_id = data["task_id"]
            context.user_data.pop("pending_order", None)
            USERS[user_id]["balance"] -= cost
            update_user_balance(user_id)

            await query.edit_message_text(
                f"✅ Order placed!\n🆔 Task ID: `{task_id}`\n💲 Deducted: {cost} point\n⏳ Waiting for file to be ready...",
                parse_mode="Markdown"
            )

            for _ in range(15):
                await asyncio.sleep(5)
                status_url = f"{API_BASE}/order/{task_id}/status"
                s = requests.get(status_url, headers=HEADERS).json()
                if s.get("status") == "ready":
                    dl_url = f"{API_BASE}/v2/order/{task_id}/download?responsetype=any"
                    dl = requests.get(dl_url, headers=HEADERS).json()
                    if dl.get("success") and dl["status"] == "ready":
                        await query.message.reply_text(
                            f"✅ File Ready!\n"
                            f"📂 {dl['fileName']}\n"
                            f"🔗 {dl['downloadLink']}\n\n"
                            f"💲 Deducted: {cost} point\n"
                            f"💰 Balance Now: {USERS[user_id]['balance']} points"
                        )
                    else:
                        await query.message.reply_text(f"⚠️ Error fetching download: {dl}")
                    return
            await query.message.reply_text(f"⏳ File not ready yet. Check later with `/status {task_id}` or `/download {task_id}`")
        else:
            await query.edit_message_text(f"❌ Error: {data.get('message')}")

# ====== ADMIN PANEL ======
async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ Unauthorized.")
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton("👤 List Users", callback_data="list_users")],
        [InlineKeyboardButton("➕ Add User", callback_data="add_user")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Admin Panel:", reply_markup=reply_markup)
    return ADMIN_ACTION

async def admin_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    load_users_from_sheet()

    if data == "list_users":
        if not USERS:
            await query.edit_message_text("No users found.")
            return ADMIN_ACTION

        text = "📋 Users List:\n\n"
        for uid, info in USERS.items():
            text += f"ID: {uid}, Balance: {info['balance']}, Token: `{info['token']}`\n"

        await query.edit_message_text(text, parse_mode="Markdown")
        return ADMIN_ACTION

    elif data == "add_user":
        await query.edit_message_text("Send new user as: `user_id token balance`")
        return ADMIN_ADD_USER

# ====== ADMIN HANDLERS ======
async def admin_add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = update.message.text.strip().split()
    if len(parts) != 3:
        await update.message.reply_text("❌ Format invalid! Send as `user_id token balance`")
        return ADMIN_ADD_USER
    uid, token, balance = parts
    USERS[int(uid)] = {"balance": int(balance), "token": token, "verified": False}
    add_user_to_sheet(int(uid), token, int(balance))
    await update.message.reply_text(f"✅ Added user {uid} with balance {balance} and token {token}")
    return ConversationHandler.END

async def admin_user_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = int(update.message.text.strip())
    except:
        await update.message.reply_text("❌ Invalid number! Send a valid integer amount.")
        return ADMIN_USER_AMOUNT

    uid = context.user_data.get("admin_user_id")
    if uid not in USERS:
        USERS[uid] = {"balance": 0, "token": "defaulttoken", "verified": False}

    if context.user_data.get("deduct_mode"):
        USERS[uid]["balance"] -= amount
        await update.message.reply_text(f"✅ Deducted {amount} from user {uid}. New balance: {USERS[uid]['balance']}")
    else:
        USERS[uid]["balance"] += amount
        await update.message.reply_text(f"✅ Added {amount} to user {uid}. New balance: {USERS[uid]['balance']}")

    update_user_balance(uid)
    context.user_data.pop("admin_user_id", None)
    context.user_data.pop("deduct_mode", None)
    return ConversationHandler.END

# ====== INLINE BUTTON HANDLER ======
async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "balance":
        await balance_func(update, context, from_callback=True)

# ====== MAIN ======
def main():
    load_users_from_sheet()  # Load at startup

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # User start & token
    user_conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, check_token)]},
        fallbacks=[]
    )
    app.add_handler(user_conv)

    # Admin panel
    admin_conv = ConversationHandler(
        entry_points=[CommandHandler("admin", admin)],
        states={
            ADMIN_ACTION: [CallbackQueryHandler(admin_button), CallbackQueryHandler(button)],
            ADMIN_ADD_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_user)],
            ADMIN_USER_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_user_amount)],
        },
        fallbacks=[]
    )
    app.add_handler(admin_conv)

    # Adobe Stock links
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))

    # Confirm/Cancel
    app.add_handler(CallbackQueryHandler(confirm_or_cancel, pattern="^(confirm_order|cancel_order)$"))

    # Inline buttons for balance etc
    app.add_handler(CallbackQueryHandler(button))

    app.run_polling()

if __name__ == "__main__":
    main()
