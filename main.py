import os
import re
import logging
from datetime import datetime, timedelta, timezone
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
payments_collection.create_index([("expiry_date", 1)])

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

def ensure_timezone_aware(dt):
    """Ensure datetime is timezone aware (UTC if no timezone)"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

def convert_period_to_days(period_num, period_unit):
    """Convert time period to days"""
    period_unit = period_unit.lower()
    if period_unit in ['day', 'days']:
        return int(period_num)
    elif period_unit in ['month', 'months']:
        return int(period_num) * 30
    elif period_unit == 'year':
        return int(period_num) * 365
    else:
        return int(period_num)  # Default to days if unknown unit

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
        "🔹 Use /cancel to cancel a pending payment"
    )

async def create_new_payment(user_id, username, transaction_id, amount, period_num, period_unit, context, message):
    """Helper function to create a new payment record"""
    days = convert_period_to_days(period_num, period_unit)
    
    payment_data = {
        "user_id": user_id,
        "username": username,
        "transaction_id": transaction_id,
        "amount": int(amount),
        "days": days,
        "period_display": f"{period_num} {period_unit}",  # Store original period display
        "status": "pending",
        "created_at": get_utc_now(),
        "updated_at": get_utc_now()
    }
    
    try:
        payment_id = payments_collection.insert_one(payment_data).inserted_id
        logger.info(f"New payment created: {payment_id} for user {user_id}")
        
        # Send to admin for approval
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
            f"⏳ Period: {period_num} {period_unit} ({days} days)\n\n"
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

    # Check if message is a photo (screenshot)
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

    # Check if message matches payment pattern
    match = PAYMENT_PATTERN.match(text)
    if not match:
        await message.reply_text("❌ Please use the correct payment format. Type /start to see the required format.")
        return

    username, transaction_id, amount, period_num, period_unit = match.groups()[:5]
    user_id = message.from_user.id

    # Store payment data in context for callback handling
    context.user_data['pending_payment'] = {
        'user_id': user_id,
        'username': username,
        'transaction_id': transaction_id,
        'amount': amount,
        'period_num': period_num,
        'period_unit': period_unit
    }

    # Check if user has an active approved payment
    existing_approved = payments_collection.find_one({
        "user_id": user_id,
        "status": "approved",
        "expiry_date": {"$gt": get_utc_now()}
    })

    if existing_approved:
        expiry_date = ensure_timezone_aware(existing_approved["expiry_date"])
        
        keyboard = [
            [InlineKeyboardButton("✅ Yes, submit new payment", callback_data="confirm_new")],
            [InlineKeyboardButton("❌ No, keep existing", callback_data="cancel_new")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        status_message = (
            "⚠️ You already have an active payment:\n\n"
            f"💳 Transaction ID: {existing_approved['transaction_id']}\n"
            f"💰 Amount: ₹{existing_approved['amount']}\n"
            f"⏳ Period: {existing_approved.get('period_display', f'{existing_approved["days"]} days')}\n"
            f"📅 Expires: {expiry_date.strftime('%Y-%m-%d %H:%M:%S %Z')}\n\n"
            "Do you want to submit a new payment anyway?\n"
            "(Your existing payment will remain valid until expiration)"
        )
        
        await message.reply_text(status_message, reply_markup=reply_markup)
        return

    # Check for pending payments
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
            f"⏳ Period: {existing_pending.get('period_display', f'{existing_pending["days"]} days')}\n"
            f"🕒 Submitted: {ensure_timezone_aware(existing_pending['created_at']).strftime('%Y-%m-%d %H:%M:%S %Z')}\n\n"
            "You can submit a new payment if needed - the previous pending payment will be cancelled."
        )
        
        await message.reply_text(status_message, reply_markup=reply_markup)
        return

    # If no conflicts, proceed with payment creation
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
        period_display = payment.get("period_display", f"{payment['days']} days")
        msg = (
            f"{status_emoji} {payment['status'].capitalize()} Payment\n"
            f"💳 ID: {payment['transaction_id']}\n"
            f"💰 Amount: ₹{payment['amount']}\n"
            f"⏳ Period: {period_display}\n"
            f"📅 Date: {ensure_timezone_aware(payment['created_at']).strftime('%Y-%m-%d %H:%M')}\n"
        )
        if payment["status"] == "approved":
            expiry_date = ensure_timezone_aware(payment["expiry_date"])
            msg += f"📅 Expires: {expiry_date.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
        messages.append(msg)
    
    await update.message.reply_text("\n\n".join(messages))

# ... [rest of your existing functions remain the same] ...

def main():
    """Start the bot"""
    application = Application.builder().token(BOT_TOKEN).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start))
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
