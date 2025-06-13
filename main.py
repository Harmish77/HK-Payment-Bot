import os
import re
import logging
import asyncio
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
transactions_collection = db["transactions"]
users_collection = db["users"]

# Create indexes
payments_collection.create_index([("user_id", 1)])
payments_collection.create_index([("status", 1)])
transactions_collection.create_index([("transaction_id", 1)], unique=True)
users_collection.create_index([("user_id", 1)], unique=True)

# Payment message pattern
PAYMENT_PATTERN = re.compile(
    r"✅ I have successfully completed the payment.\s*"
    r"📱 Telegram Username: @([^\s]+)\s*"
    r"💳 Transaction ID: (\d+)\s*"
    r"💰 Amount Paid: ₹(\d+)\s*"
    r"⏳ Time Period: (\d+)\s*(day|days|month|months|year)\s*"
    r"🙏 Thank you!",
    re.IGNORECASE
)

def get_utc_now():
    """Get current time in UTC with timezone awareness"""
    return datetime.now(timezone.utc)

def convert_period_to_days(period_num, period_unit):
    """Convert time period to days"""
    period_unit = period_unit.lower()
    if period_unit in ['day', 'days']:
        return int(period_num)
    elif period_unit in ['month', 'months']:
        return int(period_num) * 30
    elif period_unit == 'year':
        return int(period_num) * 365
    return int(period_num)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message with instructions"""
    user_id = update.message.from_user.id
    users_collection.update_one(
        {"user_id": user_id},
        {"$set": {"username": update.message.from_user.username, "last_seen": get_utc_now()}},
        upsert=True
    )
    
    await update.message.reply_text(
        "Welcome to the Payment Bot! 💰\n\n"
        "📝 To submit a payment:\n"
        "1. Send payment details in format:\n\n"
        "✅ I have successfully completed the payment.\n"
        "📱 Telegram Username: @your_username\n"
        "💳 Transaction ID: 123456789\n"
        "💰 Amount Paid: ₹100\n"
        "⏳ Time Period: 30 days\n"
        "🙏 Thank you!\n\n"
        "2. Then send payment screenshot\n\n"
        "🔹 Commands:\n"
        "/mypayments - View your payment history\n"
        "/help - Show instructions"
    )

async def handle_payment_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle initial payment message"""
    message = update.message
    user_id = message.from_user.id
    
    match = PAYMENT_PATTERN.match(message.text)
    if not match:
        await message.reply_text("❌ Invalid format. Please use the correct format shown in /start.")
        return

    username, transaction_id, amount, period_num, period_unit = match.groups()[:5]
    
    if transactions_collection.find_one({"transaction_id": transaction_id}):
        await message.reply_text("❌ This Transaction ID was already used. Each ID can only be used once.")
        return

    context.user_data['pending_payment'] = {
        'user_id': user_id,
        'username': username,
        'transaction_id': transaction_id,
        'amount': amount,
        'period_num': period_num,
        'period_unit': period_unit,
        'text': message.text
    }

    await message.reply_text("✅ Payment details received. Please now send your payment screenshot as a photo.")

async def handle_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle payment screenshot"""
    message = update.message
    user_id = message.from_user.id
    
    if 'pending_payment' not in context.user_data:
        await message.reply_text("❌ Please send payment details first.")
        return

    payment_data = context.user_data['pending_payment']
    
    try:
        transactions_collection.insert_one({
            "transaction_id": payment_data['transaction_id'],
            "user_id": user_id,
            "registered_at": get_utc_now()
        })
    except pymongo.errors.DuplicateKeyError:
        await message.reply_text("❌ This Transaction ID was already used. Please contact support.")
        return

    days = convert_period_to_days(payment_data['period_num'], payment_data['period_unit'])
    payment_id = payments_collection.insert_one({
        "user_id": user_id,
        "username": payment_data['username'],
        "transaction_id": payment_data['transaction_id'],
        "amount": int(payment_data['amount']),
        "days": days,
        "period_display": f"{payment_data['period_num']} {payment_data['period_unit']}",
        "status": "pending",
        "created_at": get_utc_now()
    }).inserted_id

    # Send to admin
    admin_msg = (
        f"📦 New Payment Submission\n\n"
        f"👤 User: @{payment_data['username']} (ID: {user_id})\n"
        f"💳 Transaction ID: {payment_data['transaction_id']}\n"
        f"💰 Amount: ₹{payment_data['amount']}\n"
        f"⏳ Period: {payment_data['period_num']} {payment_data['period_unit']}\n\n"
        f"🆔 Payment ID: {payment_id}"
    )
    
    await context.bot.send_message(ADMIN_CHAT_ID, admin_msg)
    await context.bot.send_photo(
        ADMIN_CHAT_ID, 
        photo=message.photo[-1].file_id,
        caption=f"Screenshot for {payment_data['transaction_id']}"
    )
    
    keyboard = [
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"approve_{payment_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject_{payment_id}"),
        ]
    ]
    await context.bot.send_message(
        ADMIN_CHAT_ID,
        "Please review this payment:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    await message.reply_text(
        "✅ Your payment has been submitted for admin approval.\n"
        "You'll be notified when it's processed.\n\n"
        "Use /mypayments to check status."
    )
    del context.user_data['pending_payment']

async def my_payments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user's payment history"""
    user_id = update.message.from_user.id
    payments = list(payments_collection.find({"user_id": user_id}).sort("created_at", -1).limit(10))
    
    if not payments:
        await update.message.reply_text("📭 You don't have any payment history yet.")
        return
    
    messages = []
    for payment in payments:
        status_emoji = "⏳" if payment["status"] == "pending" else "✅" if payment["status"] == "approved" else "❌"
        msg = (
            f"{status_emoji} {payment['status'].capitalize()}\n"
            f"💳 ID: {payment['transaction_id']}\n"
            f"💰 Amount: ₹{payment['amount']}\n"
            f"⏳ Period: {payment.get('period_display', 'N/A')}\n"
            f"📅 Date: {payment['created_at'].strftime('%Y-%m-%d %H:%M')}\n"
        )
        messages.append(msg)
    
    await update.message.reply_text("📋 Your Payment History:\n\n" + "\n".join(messages))

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show admin statistics"""
    if str(update.message.from_user.id) != ADMIN_CHAT_ID:
        await update.message.reply_text("❌ Admin only command.")
        return
    
    try:
        total_users = users_collection.count_documents({})
        total_payments = payments_collection.count_documents({})
        pending_payments = payments_collection.count_documents({"status": "pending"})
        
        db_stats = db.command("dbStats")
        storage_size = db_stats.get("storageSize", 0) / (1024 * 1024)  # Convert to MB
        
        stats_msg = (
            "📊 Admin Statistics\n\n"
            f"👥 Total Users: {total_users}\n"
            f"💰 Total Payments: {total_payments}\n"
            f"⏳ Pending Approvals: {pending_payments}\n"
            f"💾 Storage Used: {storage_size:.2f} MB\n\n"
            "🔹 /manage_payments - View pending payments"
        )
        
        await update.message.reply_text(stats_msg)
    except Exception as e:
        logger.error(f"Error getting stats: {e}")
        await update.message.reply_text("❌ Error retrieving statistics.")

async def manage_payments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin payment management interface"""
    if str(update.message.from_user.id) != ADMIN_CHAT_ID:
        await update.message.reply_text("❌ Admin only command.")
        return
    
    pending_payments = list(payments_collection.find({"status": "pending"}).sort("created_at", -1).limit(10))
    
    if not pending_payments:
        await update.message.reply_text("✅ No pending payments to review.")
        return
    
    for payment in pending_payments:
        payment_msg = (
            f"🆔 Payment ID: {payment['_id']}\n"
            f"👤 User: @{payment['username']} (ID: {payment['user_id']})\n"
            f"💳 Transaction ID: {payment['transaction_id']}\n"
            f"💰 Amount: ₹{payment['amount']}\n"
            f"⏳ Period: {payment.get('period_display', 'N/A')}\n"
            f"📅 Submitted: {payment['created_at'].strftime('%Y-%m-%d %H:%M')}\n\n"
            "Use buttons below to approve/reject:"
        )
        
        keyboard = [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"approve_{payment['_id']}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"reject_{payment['_id']}"),
            ]
        ]
        
        await update.message.reply_text(
            payment_msg,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send broadcast message to all users (admin only)"""
    if str(update.message.from_user.id) != ADMIN_CHAT_ID:
        await update.message.reply_text("❌ Admin only command.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return

    message = ' '.join(context.args)
    users = users_collection.find({})
    total = 0
    success = 0

    await update.message.reply_text(f"📢 Starting broadcast to {users_collection.count_documents({})} users...")

    for user in users:
        try:
            await context.bot.send_message(
                chat_id=user["user_id"],
                text=f"📢 Announcement:\n\n{message}"
            )
            success += 1
        except Exception as e:
            logger.error(f"Failed to send to user {user['user_id']}: {e}")
        total += 1
        
        # Small delay to avoid rate limiting
        if total % 5 == 0:
            await asyncio.sleep(1)

    await update.message.reply_text(
        f"✅ Broadcast completed!\n"
        f"• Total users: {total}\n"
        f"• Successful deliveries: {success}\n"
        f"• Failed: {total - success}"
        )
async def handle_callback(update: Update, context: CallbackContext):
    """Handle admin approval/rejection callbacks"""
    query = update.callback_query
    await query.answer()
    
    if str(query.from_user.id) != ADMIN_CHAT_ID:
        await query.edit_message_text("❌ Only admins can perform this action.")
        return

    try:
        action, payment_id = query.data.split("_")
        payment_id = ObjectId(payment_id)
        
        payment = payments_collection.find_one({"_id": payment_id})
        if not payment:
            await query.edit_message_text("❌ Payment not found!")
            return

        if action == "approve":
            payments_collection.update_one(
                {"_id": payment_id},
                {"$set": {"status": "approved", "updated_at": get_utc_now()}}
            )
            
            await context.bot.send_message(
                payment["user_id"],
                "🎉 Your payment has been approved!\n\n"
                f"💳 Transaction ID: {payment['transaction_id']}\n"
                f"💰 Amount: ₹{payment['amount']}\n"
                f"⏳ Period: {payment.get('period_display', 'N/A')}\n\n"
                "Thank you for your payment!"
            )
            
            await query.edit_message_text(
                f"✅ Approved payment:\n\n{query.message.text}",
                reply_markup=None
            )
            
        elif action == "reject":
            payments_collection.update_one(
                {"_id": payment_id},
                {"$set": {"status": "rejected", "updated_at": get_utc_now()}}
            )
            
            await context.bot.send_message(
                payment["user_id"],
                "❌ Your payment was rejected.\n\n"
                f"Transaction ID: {payment['transaction_id']}\n"
                f"Amount: ₹{payment['amount']}\n\n"
                "Please contact support if you believe this was an error."
            )
            
            await query.edit_message_text(
                f"❌ Rejected payment:\n\n{query.message.text}",
                reply_markup=None
            )
            
    except Exception as e:
        logger.error(f"Error in callback: {e}")
        await query.edit_message_text("❌ Error processing request.")

def main():
    """Start the bot"""
    application = Application.builder().token(BOT_TOKEN).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("mypayments", my_payments))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("manage_payments", manage_payments))
    application.add_handler(CommandHandler("broadcast", broadcast))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_payment_message))
    application.add_handler(MessageHandler(filters.PHOTO, handle_screenshot))
    application.add_handler(CallbackQueryHandler(handle_callback, pattern="^(approve|reject)_"))
    
    logger.info("Bot is starting...")
    application.run_polling()

if __name__ == "__main__":
    main()
