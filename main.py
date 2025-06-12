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

def get_utc_now():
    """Get current time in UTC with timezone awareness"""
    return datetime.now(timezone.utc)

def ensure_timezone_aware(dt):
    """Ensure datetime is timezone aware (UTC if no timezone)"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a welcome message with payment instructions"""
    await update.message.reply_text(
        "Welcome to the Payment Bot! 💰\n\n"
        "📝 To submit a payment, please send a message in this format:\n\n"
        "✅ I have successfully completed the payment.\n\n"
        "📱 Telegram Username: @your_username\n"
        "💳 Transaction ID: 123456789\n"
        "💰 Amount Paid: ₹100\n"
        "⏳ Time Period: 30 Days\n\n"
        "📸 I will send the payment screenshot shortly.\n"
        "🙏 Thank you!\n\n"
        "🔹 Use /mypayments to view your payment history\n"
        "🔹 Use /cancel to cancel a pending payment\n"
        "💡 You can submit new payments anytime - existing payments will remain valid until expiration"
    )

async def create_new_payment(user_id, username, transaction_id, amount, days, context, message):
    """Helper function to create a new payment record"""
    payment_data = {
        "user_id": user_id,
        "username": username,
        "transaction_id": transaction_id,
        "amount": int(amount),
        "days": int(days),
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
            f"⏳ Period: {days} days\n\n"
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
        # Forward screenshot to log channel without storing in DB
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

    username, transaction_id, amount, days = match.groups()[:4]
    user_id = message.from_user.id

    # Check if user has an active approved payment
    existing_approved = payments_collection.find_one({
        "user_id": user_id,
        "status": "approved",
        "expiry_date": {"$gt": get_utc_now()}
    })

    if existing_approved:
        expiry_date = ensure_timezone_aware(existing_approved["expiry_date"])
        remaining_time = expiry_date - get_utc_now()
        remaining_days = remaining_time.days
        
        # Create keyboard for payment override
        keyboard = [
            [
                InlineKeyboardButton("✅ Yes, submit new payment", 
                                    callback_data=f"override_{user_id}_{amount}_{days}_{transaction_id}"),
                InlineKeyboardButton("❌ No, keep existing", 
                                    callback_data="cancel_override")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        status_message = (
            "⚠️ You already have an active payment:\n\n"
            f"💳 Transaction ID: {existing_approved['transaction_id']}\n"
            f"💰 Amount: ₹{existing_approved['amount']}\n"
            f"⏱️ Remaining: {remaining_days} days\n"
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
        status_message = (
            "⏳ You already have a pending payment:\n\n"
            f"💳 Transaction ID: {existing_pending['transaction_id']}\n"
            f"💰 Amount: ₹{existing_pending['amount']}\n"
            f"🕒 Submitted: {ensure_timezone_aware(existing_pending['created_at']).strftime('%Y-%m-%d %H:%M:%S %Z')}\n\n"
            "You can submit a new payment if needed - the previous pending payment will be cancelled."
        )
        
        # Create keyboard to confirm new payment
        keyboard = [
            [
                InlineKeyboardButton("✅ Submit New Payment", 
                                    callback_data=f"newpayment_{user_id}_{amount}_{days}_{transaction_id}"),
                InlineKeyboardButton("❌ Keep Existing", 
                                    callback_data="keep_pending")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await message.reply_text(status_message, reply_markup=reply_markup)
        return

    # If no conflicts, proceed with payment creation
    await create_new_payment(user_id, username, transaction_id, amount, days, context, message)

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
        msg = (
            f"{status_emoji} {payment['status'].capitalize()} Payment\n"
            f"💳 ID: {payment['transaction_id']}\n"
            f"💰 Amount: ₹{payment['amount']}\n"
            f"⏳ Period: {payment['days']} days\n"
            f"📅 Date: {ensure_timezone_aware(payment['created_at']).strftime('%Y-%m-%d %H:%M')}\n"
        )
        if payment["status"] == "approved":
            expiry_date = ensure_timezone_aware(payment["expiry_date"])
            remaining = (expiry_date - get_utc_now()).days
            msg += f"⏱️ Remaining: {remaining} days\n"
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

            # Update payment status
            payment = payments_collection.find_one({"_id": payment_id})
            if not payment:
                await query.edit_message_text(text="❌ Payment not found!")
                return

            current_time = get_utc_now()

            if action == "approve":
                expiry_date = current_time + timedelta(days=payment["days"])
                update_result = payments_collection.update_one(
                    {"_id": payment_id},
                    {
                        "$set": {
                            "status": "approved",
                            "expiry_date": expiry_date,
                            "updated_at": current_time
                        }
                    }
                )

                if update_result.modified_count > 0:
                    # Notify user
                    try:
                        await context.bot.send_message(
                            chat_id=payment["user_id"],
                            text=(
                                "🎉 Your payment has been approved!\n\n"
                                f"📱 Username: @{payment['username']}\n"
                                f"💳 Transaction ID: {payment['transaction_id']}\n"
                                f"💰 Amount: ₹{payment['amount']}\n"
                                f"⏳ Valid for: {payment['days']} days\n"
                                f"📅 Expires on: {expiry_date.strftime('%Y-%m-%d %H:%M:%S %Z')}\n\n"
                                "Thank you for your payment!"
                            )
                        )
                    except Exception as e:
                        logger.error(f"Error notifying user: {e}")

                    # Update admin message
                    await query.edit_message_text(
                        text=f"✅ Approved payment:\n\n{query.message.text}",
                        reply_markup=None
                    )

                    # Log to channel
                    if LOG_CHANNEL_ID:
                        try:
                            await context.bot.send_message(
                                chat_id=LOG_CHANNEL_ID,
                                text=(
                                    "💰 Payment Approved\n\n"
                                    f"👤 User: @{payment['username']}\n"
                                    f"💳 Transaction ID: {payment['transaction_id']}\n"
                                    f"💰 Amount: ₹{payment['amount']}\n"
                                    f"⏳ Period: {payment['days']} days\n"
                                    f"📅 Expires: {expiry_date.strftime('%Y-%m-%d %H:%M:%S %Z')}"
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
                    # Notify user
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

                    # Update admin message
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

async def handle_override_callback(update: Update, context: CallbackContext):
    """Handle payment override callback"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    if data == "cancel_override":
        await query.edit_message_text(text="❌ New payment submission cancelled.")
        return
    elif data == "keep_pending":
        await query.edit_message_text(text="ℹ️ Keeping your existing pending payment.")
        return
    
    try:
        if data.startswith("override_"):
            # Handle override of existing active payment
            _, user_id, amount, days, transaction_id = data.split("_")
            user = await context.bot.get_chat(user_id)
            username = user.username if user.username else f"user_{user_id}"
            
            await create_new_payment(user_id, username, transaction_id, amount, days, context, query.message)
            await query.edit_message_text(text="✅ New payment submitted alongside your existing active payment!")
            
        elif data.startswith("newpayment_"):
            # Handle new payment replacing pending payment
            _, user_id, amount, days, transaction_id = data.split("_")
            user = await context.bot.get_chat(user_id)
            username = user.username if user.username else f"user_{user_id}"
            
            # Cancel existing pending payment
            payments_collection.update_one(
                {"user_id": user_id, "status": "pending"},
                {"$set": {"status": "cancelled", "updated_at": get_utc_now()}}
            )
            
            await create_new_payment(user_id, username, transaction_id, amount, days, context, query.message)
            await query.edit_message_text(text="✅ New payment submitted (previous pending payment cancelled)!")
            
    except Exception as e:
        logger.error(f"Error handling override callback: {e}")
        await query.edit_message_text(text="⚠️ Failed to process your request. Please try again.")

def main():
    """Start the bot"""
    # Create the Application
    application = Application.builder().token(BOT_TOKEN).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("mypayments", my_payments))
    application.add_handler(CommandHandler("cancel", cancel_payment))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.PHOTO, handle_message))
    application.add_handler(CallbackQueryHandler(handle_callback, pattern="^(approve|reject)_"))

    # Run the bot
    logger.info("Bot is starting...")
    application.run_polling()

if __name__ == "__main__":
    main()
