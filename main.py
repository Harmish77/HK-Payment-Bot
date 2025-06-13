import os
import re
import logging
from datetime import datetime, timezone
from typing import Dict, Optional

import pymongo
from bson import ObjectId
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
    CallbackContext,
)

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Configuration
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID")
MONGODB_URI = os.getenv("MONGODB_URI")

# Validate environment variables
if not all([BOT_TOKEN, ADMIN_CHAT_ID, MONGODB_URI]):
    raise ValueError("Missing required environment variables")

# MongoDB setup
client = pymongo.MongoClient(MONGODB_URI)
db = client["payment_bot"]
payments_collection = db["payments"]

# Create indexes
payments_collection.create_index([("user_id", 1)])
payments_collection.create_index([("status", 1)])

# Updated payment message pattern with flexible time periods
PAYMENT_PATTERN = re.compile(
    r"✅ I have successfully completed the payment.\s*"
    r"📱 Telegram Username: @([^\s]+)\s*"
    r"💳 Transaction ID: (\d+)\s*"
    r"💰 Amount Paid: ₹(\d+)\s*"
    r"⏳ Time Period: (\d+)\s*(day|days|month|months|year)\s*"
    r"(📸 I will send the payment screenshot shortly.\s*)?"
    r"🙏 Thank you!",
    re.IGNORECASE
)

def get_utc_now():
    """Get current time in UTC with timezone awareness"""
    return datetime.now(timezone.utc)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a welcome message with payment instructions"""
    await update.message.reply_text(
        "Welcome to the Payment Bot! 💰\n\n"
        "📝 To submit a payment, please send a message in this format:\n\n"
        "✅ I have successfully completed the payment.\n\n"
        "📱 Telegram Username: @your_username\n"
        "💳 Transaction ID: 123456789\n"
        "💰 Amount Paid: ₹100\n"
        "⏳ Time Period: 30 days (or 1 month, 1 year, etc.)\n\n"
        "📸 I will send the payment screenshot shortly.\n"
        "🙏 Thank you!\n\n"
        "🔹 Use /mypayments to view your payment history\n"
        "🔹 Use /cancel to cancel a pending payment\n"
        "🔹 Admins: /stats to view bot statistics"
    )

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show bot statistics (admin only)"""
    user_id = update.message.from_user.id
    
    if str(user_id) != ADMIN_CHAT_ID:
        await update.message.reply_text("❌ This command is only available for admins.")
        return
    
    try:
        total_users = len(payments_collection.distinct("user_id"))
        db_stats = db.command("dbStats")
        storage_size = db_stats.get("storageSize", 0)
        data_size = db_stats.get("dataSize", 0)
        
        def convert_size(size_bytes):
            if size_bytes < 1024:
                return f"{size_bytes} bytes"
            elif size_bytes < 1024*1024:
                return f"{size_bytes/1024:.2f} KB"
            else:
                return f"{size_bytes/(1024*1024):.2f} MB"
        
        stats_message = (
            "📊 Bot Statistics\n\n"
            f"👥 Total Users: {total_users}\n"
            f"💾 Storage Used: {convert_size(storage_size)}\n"
            f"📂 Data Size: {convert_size(data_size)}\n"
            f"🔄 Last Updated: {get_utc_now().strftime('%Y-%m-%d %H:%M:%S %Z')}"
        )
        
        await update.message.reply_text(stats_message)
        
    except Exception as e:
        logger.error(f"Error getting stats: {e}")
        await update.message.reply_text("⚠️ Failed to retrieve statistics. Please try again.")

async def create_new_payment(user_id, username, transaction_id, amount, period_num, period_unit, context, message):
    """Helper function to create a new payment record"""
    payment_data = {
        "user_id": user_id,
        "username": username,
        "transaction_id": transaction_id,
        "amount": int(amount),
        "period_display": f"{period_num} {period_unit}",
        "status": "pending",
        "created_at": get_utc_now(),
        "updated_at": get_utc_now()
    }
    
    try:
        payment_id = payments_collection.insert_one(payment_data).inserted_id
        logger.info(f"New payment created: {payment_id} for user {user_id}")
        
        keyboard = [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"approve_{payment_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"reject_{payment_id}"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        admin_message = (
            f"📩 New payment request:\n\n"
            f"👤 User: @{username} (ID: {user_id})\n"
            f"💳 Transaction ID: {transaction_id}\n"
            f"💰 Amount: ₹{amount}\n"
            f"⏳ Period: {period_num} {period_unit}\n\n"
            f"🆔 Payment ID: {payment_id}"
        )

        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=admin_message,
            reply_markup=reply_markup
        )
        await message.reply_text(
            "✅ Your payment has been submitted for admin approval.\n\n"
            "You'll receive a notification once it's processed.\n"
            "You can check status with /mypayments"
        )
    except Exception as e:
        logger.error(f"Error creating payment: {e}")
        await message.reply_text("⚠️ Failed to process your payment. Please try again.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming messages - either payment submissions or screenshots"""
    message = update.message
    text = message.text or message.caption or ""

    if message.photo:
        if LOG_CHANNEL_ID:
            try:
                await context.bot.send_photo(
                    chat_id=LOG_CHANNEL_ID,
                    photo=message.photo[-1].file_id,
                    caption=f"Screenshot from @{message.from_user.username}"
                )
                await message.reply_text("✅ Thank you for sending the screenshot!")
            except Exception as e:
                logger.error(f"Error forwarding screenshot: {e}")
                await message.reply_text("⚠️ Failed to process screenshot. Please try again.")
        return

    match = PAYMENT_PATTERN.match(text)
    if not match:
        await message.reply_text("❌ Please use the correct payment format. Type /start to see the required format.")
        return

    username, transaction_id, amount, period_num, period_unit = match.groups()[:5]
    user_id = message.from_user.id

    context.user_data['pending_payment'] = {
        'user_id': user_id,
        'username': username,
        'transaction_id': transaction_id,
        'amount': amount,
        'period_num': period_num,
        'period_unit': period_unit
    }

    existing_approved = payments_collection.find_one({
        "user_id": user_id,
        "status": "approved"
    })

    if existing_approved:
        keyboard = [
            [InlineKeyboardButton("✅ Yes, submit new payment", callback_data="confirm_new")],
            [InlineKeyboardButton("❌ No, keep existing", callback_data="cancel_new")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        status_message = (
            "⚠️ You already have an active payment:\n\n"
            f"💳 Transaction ID: {existing_approved['transaction_id']}\n"
            f"💰 Amount: ₹{existing_approved['amount']}\n"
            f"⏳ Period: {existing_approved.get('period_display', 'N/A')}\n\n"
            "Do you want to submit a new payment anyway?"
        )
        
        await message.reply_text(status_message, reply_markup=reply_markup)
        return

    existing_pending = payments_collection.find_one({
        "user_id": user_id,
        "status": "pending"
    })
    
    if existing_pending:
        keyboard = [
            [InlineKeyboardButton("✅ Submit New Payment", callback_data="confirm_replace")],
            [InlineKeyboardButton("❌ Keep Existing", callback_data="keep_existing")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        status_message = (
            "⏳ You already have a pending payment:\n\n"
            f"💳 Transaction ID: {existing_pending['transaction_id']}\n"
            f"💰 Amount: ₹{existing_pending['amount']}\n"
            f"⏳ Period: {existing_pending.get('period_display', 'N/A')}\n"
            f"🕒 Submitted: {existing_pending['created_at'].strftime('%Y-%m-%d %H:%M:%S %Z')}\n\n"
            "You can submit a new payment if needed - the previous pending payment will be cancelled."
        )
        
        await message.reply_text(status_message, reply_markup=reply_markup)
        return

    await create_new_payment(user_id, username, transaction_id, amount, period_num, period_unit, context, message)

async def my_payments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user's payment history"""
    user_id = update.message.from_user.id
    payments = list(payments_collection.find({"user_id": user_id}).sort("created_at", -1))
    
    if not payments:
        await update.message.reply_text("📭 You don't have any payment history yet.")
        return
    
    messages = []
    for payment in payments:
        status_emoji = "⏳" if payment["status"] == "pending" else "✅" if payment["status"] == "approved" else "❌"
        period_display = payment.get("period_display", "N/A")
        msg = (
            f"{status_emoji} {payment['status'].capitalize()} Payment\n"
            f"💳 ID: {payment['transaction_id']}\n"
            f"💰 Amount: ₹{payment['amount']}\n"
            f"⏳ Period: {period_display}\n"
            f"📅 Date: {payment['created_at'].strftime('%Y-%m-%d %H:%M')}\n"
        )
        messages.append(msg)
    
    await update.message.reply_text("\n\n".join(messages))

async def cancel_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel a user's pending payment"""
    user_id = update.message.from_user.id
    try:
        result = payments_collection.update_one(
            {"user_id": user_id, "status": "pending"},
            {"$set": {"status": "cancelled", "updated_at": get_utc_now()}}
        )
        
        if result.modified_count > 0:
            await update.message.reply_text("✅ Your pending payment has been cancelled.")
        else:
            await update.message.reply_text("ℹ️ You don't have any pending payments to cancel.")
    except Exception as e:
        logger.error(f"Error cancelling payment: {e}")
        await update.message.reply_text("⚠️ Failed to cancel payment. Please try again.")

async def handle_callback(update: Update, context: CallbackContext):
    """Handle admin approval/rejection callbacks"""
    query = update.callback_query
    await query.answer()

    try:
        data = query.data
        if data.startswith(("approve_", "reject_")):
            action, payment_id = data.split("_")
            payment_id = ObjectId(payment_id)

            payment = payments_collection.find_one({"_id": payment_id})
            if not payment:
                await query.edit_message_text(text="❌ Payment not found!")
                return

            current_time = get_utc_now()

            if action == "approve":
                update_result = payments_collection.update_one(
                    {"_id": payment_id},
                    {
                        "$set": {
                            "status": "approved",
                            "updated_at": current_time
                        }
                    }
                )

                if update_result.modified_count > 0:
                    try:
                        await context.bot.send_message(
                            chat_id=payment["user_id"],
                            text=(
                                "🎉 Your payment has been approved!\n\n"
                                f"📱 Username: @{payment['username']}\n"
                                f"💳 Transaction ID: {payment['transaction_id']}\n"
                                f"💰 Amount: ₹{payment['amount']}\n"
                                f"⏳ Period: {payment.get('period_display', 'N/A')}\n\n"
                                "Thank you for your payment!"
                            )
                        )
                    except Exception as e:
                        logger.error(f"Error notifying user: {e}")

                    await query.edit_message_text(
                        text=f"✅ Approved payment:\n\n{query.message.text}",
                        reply_markup=None
                    )

                    if LOG_CHANNEL_ID:
                        try:
                            await context.bot.send_message(
                                chat_id=LOG_CHANNEL_ID,
                                text=(
                                    "💰 Payment Approved\n\n"
                                    f"👤 User: @{payment['username']}\n"
                                    f"💳 Transaction ID: {payment['transaction_id']}\n"
                                    f"💰 Amount: ₹{payment['amount']}\n"
                                    f"⏳ Period: {payment.get('period_display', 'N/A')}"
                                )
                            )
                        except Exception as e:
                            logger.error(f"Error logging to channel: {e}")

            elif action == "reject":
                update_result = payments_collection.update_one(
                    {"_id": payment_id},
                    {"$set": {"status": "rejected", "updated_at": current_time}}
                )

                if update_result.modified_count > 0:
                    try:
                        await context.bot.send_message(
                            chat_id=payment["user_id"],
                            text=(
                                "❌ Your payment has been rejected.\n\n"
                                f"Transaction ID: {payment['transaction_id']}\n"
                                f"Amount: ₹{payment['amount']}\n\n"
                                "Please contact support if you believe this is an error."
                            )
                        )
                    except Exception as e:
                        logger.error(f"Error notifying user: {e}")

                    await query.edit_message_text(
                        text=f"❌ Rejected payment:\n\n{query.message.text}",
                        reply_markup=None
                    )

    except ValueError as e:
        logger.error(f"Error processing callback: {e}")
        await query.edit_message_text(text="❌ Error processing request. Invalid data format.")
    except Exception as e:
        logger.error(f"Unexpected error in callback: {e}")
        await query.edit_message_text(text="❌ An unexpected error occurred. Please try again.")

async def handle_payment_decision(update: Update, context: CallbackContext):
    """Handle user's decision about new payment submission"""
    query = update.callback_query
    await query.answer()
    
    user_data = context.user_data.get('pending_payment')
    if not user_data:
        await query.edit_message_text(text="⚠️ Payment data not found. Please start over.")
        return
    
    user_id = user_data['user_id']
    username = user_data['username']
    transaction_id = user_data['transaction_id']
    amount = user_data['amount']
    period_num = user_data['period_num']
    period_unit = user_data['period_unit']
    
    if query.data == "confirm_new":
        await create_new_payment(user_id, username, transaction_id, amount, period_num, period_unit, context, query.message)
        await query.edit_message_text(text="✅ New payment submitted alongside your existing active payment!")
    
    elif query.data == "cancel_new":
        await query.edit_message_text(text="❌ New payment submission cancelled.")
    
    elif query.data == "confirm_replace":
        payments_collection.update_one(
            {"user_id": user_id, "status": "pending"},
            {"$set": {"status": "cancelled", "updated_at": get_utc_now()}}
        )
        await create_new_payment(user_id, username, transaction_id, amount, period_num, period_unit, context, query.message)
        await query.edit_message_text(text="✅ New payment submitted (previous pending payment cancelled)!")
    
    elif query.data == "keep_existing":
        await query.edit_message_text(text="ℹ️ Keeping your existing pending payment.")
    
    if 'pending_payment' in context.user_data:
        del context.user_data['pending_payment']

def main():
    """Start the bot"""
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("mypayments", my_payments))
    application.add_handler(CommandHandler("cancel", cancel_payment))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.PHOTO, handle_message))
    application.add_handler(CallbackQueryHandler(handle_callback, pattern="^(approve|reject)_"))
    application.add_handler(CallbackQueryHandler(handle_payment_decision, pattern="^(confirm_new|cancel_new|confirm_replace|keep_existing)$"))
    
    logger.info("Bot is starting...")
    application.run_polling()

if __name__ == "__main__":
    main()
