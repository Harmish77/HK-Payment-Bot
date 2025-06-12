import os
import re
from datetime import datetime, timedelta
from typing import Dict, Optional

import pymongo
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

# Load environment variables
load_dotenv()

# Configuration
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID")
MONGODB_URI = os.getenv("MONGODB_URI")

# MongoDB setup
client = pymongo.MongoClient(MONGODB_URI)
db = client["payment_bot"]
payments_collection = db["payments"]

# Payment message pattern
PAYMENT_PATTERN = re.compile(
    r"✅ I have successfully completed the payment.\s*"
    r"📱 Telegram Username: @([^\s]+)\s*"
    r"💳 Transaction ID: (\d+)\s*"
    r"💰 Amount Paid: ₹(\d+)\s*"
    r"⏳ Time Period: (\d+) Days\s*"
    r"(📸 I will send the payment screenshot shortly.\s*)?"
    r"🙏 Thank you!",
    re.IGNORECASE
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome to the Payment Bot!\n\n"
        "To submit a payment, please send a message in this format:\n\n"
        "✅ I have successfully completed the payment.\n\n"
        "📱 Telegram Username: @your_username\n"
        "💳 Transaction ID: 123456789\n"
        "💰 Amount Paid: ₹100\n"
        "⏳ Time Period: 30 Days\n\n"
        "📸 I will send the payment screenshot shortly.\n"
        "🙏 Thank you!"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    text = message.text or message.caption or ""

    # Check if message is a photo (screenshot)
    if message.photo:
        # Forward screenshot to log channel without storing in DB
        if LOG_CHANNEL_ID:
            await context.bot.send_photo(
                chat_id=LOG_CHANNEL_ID,
                photo=message.photo[-1].file_id,
                caption=f"Screenshot from @{message.from_user.username}"
            )
        await message.reply_text("Thank you for sending the screenshot!")
        return

    # Check if message matches payment pattern
    match = PAYMENT_PATTERN.match(text)
    if not match:
        await message.reply_text("Please use the correct payment format.")
        return

    username, transaction_id, amount, days = match.groups()[:4]
    user_id = message.from_user.id

    # Check if payment already exists
    existing_payment = payments_collection.find_one({
        "user_id": user_id,
        "status": {"$in": ["pending", "approved"]}
    })
    if existing_payment:
        await message.reply_text("You already have a pending or approved payment.")
        return

    # Create payment record
    payment_data = {
        "user_id": user_id,
        "username": username,
        "transaction_id": transaction_id,
        "amount": int(amount),
        "days": int(days),
        "status": "pending",
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow()
    }
    payment_id = payments_collection.insert_one(payment_data).inserted_id

    # Send to admin for approval
    keyboard = [
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"approve_{payment_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject_{payment_id}"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    admin_message = (
        f"New payment request:\n\n"
        f"👤 User: @{username} (ID: {user_id})\n"
        f"💳 Transaction ID: {transaction_id}\n"
        f"💰 Amount: ₹{amount}\n"
        f"⏳ Period: {days} days\n\n"
        f"Payment ID: {payment_id}"
    )

    await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=admin_message,
        reply_markup=reply_markup
    )

    await message.reply_text("Your payment has been submitted for approval. Thank you!")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    payment_id = data.split("_")[1]
    action = data.split("_")[0]

    # Update payment status
    payment = payments_collection.find_one({"_id": pymongo.ObjectId(payment_id)})
    if not payment:
        await query.answer("Payment not found!")
        return

    if action == "approve":
        expiry_date = datetime.utcnow() + timedelta(days=payment["days"])
        payments_collection.update_one(
            {"_id": pymongo.ObjectId(payment_id)},
            {
                "$set": {
                    "status": "approved",
                    "expiry_date": expiry_date,
                    "updated_at": datetime.utcnow()
                }
            }
        )

        # Notify user
        await context.bot.send_message(
            chat_id=payment["user_id"],
            text=f"🎉 Your payment has been approved!\n\n"
                 f"📱 Username: @{payment['username']}\n"
                 f"💳 Transaction ID: {payment['transaction_id']}\n"
                 f"💰 Amount: ₹{payment['amount']}\n"
                 f"⏳ Valid for: {payment['days']} days\n"
                 f"📅 Expires on: {expiry_date.strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )

        # Update admin message
        await query.edit_message_text(
            text=f"✅ Approved payment:\n\n{query.message.text}",
            reply_markup=None
        )

        # Log to channel
        if LOG_CHANNEL_ID:
            await context.bot.send_message(
                chat_id=LOG_CHANNEL_ID,
                text=f"💰 Payment Approved\n\n"
                     f"👤 User: @{payment['username']}\n"
                     f"💳 Transaction ID: {payment['transaction_id']}\n"
                     f"💰 Amount: ₹{payment['amount']}\n"
                     f"⏳ Period: {payment['days']} days\n"
                     f"📅 Expires: {expiry_date.strftime('%Y-%m-%d %H:%M:%S UTC')}"
            )

    elif action == "reject":
        payments_collection.update_one(
            {"_id": pymongo.ObjectId(payment_id)},
            {"$set": {"status": "rejected", "updated_at": datetime.utcnow()}}
        )

        # Notify user
        await context.bot.send_message(
            chat_id=payment["user_id"],
            text="❌ Your payment has been rejected. Please contact support if you believe this is an error."
        )

        # Update admin message
        await query.edit_message_text(
            text=f"❌ Rejected payment:\n\n{query.message.text}",
            reply_markup=None
        )

    await query.answer()

def main():
    # Create the Application
    application = Application.builder().token(BOT_TOKEN).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.PHOTO, handle_message))
    application.add_handler(CallbackQueryHandler(handle_callback))

    # Run the bot
    application.run_polling()

if __name__ == "__main__":
    main()
