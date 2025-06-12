import os
import re
from datetime import datetime
from pymongo import MongoClient
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Updater, MessageHandler, Filters, CallbackQueryHandler, CallbackContext

# Environment variables from Koyeb
BOT_TOKEN = os.environ["BOT_TOKEN"]
MONGO_URI = os.environ["MONGO_URI"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
LOG_CHANNEL_ID = int(os.environ["LOG_CHANNEL_ID"])

# Connect to MongoDB
client = MongoClient(MONGO_URI)
db = client["payment_bot"]
collection = db["payments"]

# Regex pattern to extract info
info_pattern = re.compile(
    r"Telegram Username:\s*(?P<username>@\w+)\s*"
    r"Transaction ID:\s*(?P<txn_id>\w+)\s*"
    r"Amount Paid:\s*(?P<amount>₹?\d+)\s*"
    r"Time Period:\s*(?P<period>.+?)\n", re.DOTALL
)

# Handle messages
def handle_message(update: Update, context: CallbackContext):
    message = update.message
    user = message.from_user

    if message.text:
        match = info_pattern.search(message.text)
        if match:
            username = match.group("username")
            txn_id = match.group("txn_id")
            amount = match.group("amount")
            period = match.group("period")

            context.user_data["payment_info"] = {
                "telegram_id": user.id,
                "username": username,
                "txn_id": txn_id,
                "amount": amount,
                "period": period,
                "msg_id": message.message_id,
                "chat_id": message.chat_id
            }

            text = (
                f"🆕 New Payment Request\n\n"
                f"👤 Username: {username}\n"
                f"🆔 Telegram ID: {user.id}\n"
                f"💳 Transaction ID: {txn_id}\n"
                f"💰 Amount: {amount}\n"
                f"⏳ Period: {period}\n\n"
                f"Forwarded screenshot below 👇"
            )

            if message.photo:
                for p in message.photo:
                    context.bot.forward_message(
                        chat_id=LOG_CHANNEL_ID,
                        from_chat_id=message.chat_id,
                        message_id=message.message_id
                    )

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Approve", callback_data="approve"),
                 InlineKeyboardButton("❌ Reject", callback_data="reject")]
            ])

            context.bot.send_message(chat_id=ADMIN_ID, text=text, reply_markup=keyboard)
            message.reply_text("✅ Your payment details have been sent for approval.")
        else:
            message.reply_text("❗ Payment details not detected. Please follow the correct format.")

# Handle approval
def handle_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()

    action = query.data
    payment_info = context.user_data.get("payment_info")

    if not payment_info:
        query.edit_message_text("❗ No payment info found.")
        return

    if action == "approve":
        collection.insert_one({
            "telegram_id": payment_info["telegram_id"],
            "username": payment_info["username"],
            "transaction_id": payment_info["txn_id"],
            "amount": payment_info["amount"],
            "period": payment_info["period"],
            "timestamp": datetime.utcnow()
        })

        context.bot.send_message(
            chat_id=payment_info["chat_id"],
            text="✅ Your payment has been approved!"
        )
        query.edit_message_text("✅ Payment approved and saved to DB.")
    else:
        context.bot.send_message(
            chat_id=payment_info["chat_id"],
            text="❌ Your payment has been rejected. Please try again."
        )
        query.edit_message_text("❌ Payment rejected.")

# Start bot
def main():
    updater = Updater(BOT_TOKEN)
    dp = updater.dispatcher

    dp.add_handler(MessageHandler(Filters.text | Filters.photo, handle_message))
    dp.add_handler(CallbackQueryHandler(handle_callback))

    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
