import os
import re
import sys
import logging
import asyncio
from datetime import datetime, timezone
from typing import Dict, Optional, List

import pymongo
from bson import ObjectId
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Bot
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
from aiohttp import web

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('payment_bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Configuration
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID")
MONGODB_URI = os.getenv("MONGODB_URI")
PORT = int(os.getenv("PORT", 8080))

# Validate environment variables
if not all([BOT_TOKEN, ADMIN_CHAT_ID, MONGODB_URI]):
    raise ValueError("Missing required environment variables")

# MongoDB setup
client = pymongo.MongoClient(MONGODB_URI)
db = client["payment_bot"]
payments_collection = db["payments"]
transactions_collection = db["transactions"]
users_collection = db["users"]
user_logs_collection = db["user_logs"]
message_logs_collection = db["message_logs"]

PAYMENT_PATTERN = re.compile(
    r"✅ I have successfully completed the payment.\s*"
    r"📱 Telegram Username: @([^\s]+)\s*"
    r"💳 Transaction ID: (\d+)\s*"
    r"💰 Amount Paid: ₹(\d+)\s*"
    r"⏳ Time Period: (\d+)\s*(day|days|month|months|year)\s*"
    r"🙏 Thank you!",
    re.IGNORECASE
)

# Create indexes
payments_collection.create_index([("user_id", 1)])
payments_collection.create_index([("status", 1)])
transactions_collection.create_index([("transaction_id", 1)], unique=True)
users_collection.create_index([("user_id", 1)], unique=True)
user_logs_collection.create_index([("user_id", 1)])
user_logs_collection.create_index([("timestamp", 1)])
message_logs_collection.create_index([("timestamp", 1)])

def get_utc_now():
    """Get current time in UTC with timezone awareness"""
    return datetime.now(timezone.utc)

def convert_period_to_days(period_num: str, period_unit: str) -> int:
    """Convert time period to days"""
    period_unit = period_unit.lower()
    if period_unit in ['day', 'days']:
        return int(period_num)
    elif period_unit in ['month', 'months']:
        return int(period_num) * 30
    elif period_unit == 'year':
        return int(period_num) * 365
    return int(period_num)

async def log_user_action(user_id: int, action: str, details: Dict = None):
    """Log user actions to database"""
    try:
        log_entry = {
            "user_id": user_id,
            "action": action,
            "timestamp": get_utc_now(),
            "details": details or {}
        }
        user_logs_collection.insert_one(log_entry)
    except Exception as e:
        logger.error(f"Failed to log user action: {e}")

async def register_new_user(user_id: int, username: str):
    """Register a new user and log the event"""
    try:
        users_collection.update_one(
            {"user_id": user_id},
            {"$set": {
                "username": username,
                "first_seen": get_utc_now(),
                "last_seen": get_utc_now()
            }},
            upsert=True
        )
        await log_user_action(
            user_id=user_id,
            action="registration",
            details={"username": username}
        )
    except Exception as e:
        logger.error(f"Failed to register user {user_id}: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    user = update.message.from_user
    await register_new_user(user.id, user.username)
    
    await log_user_action(
        user_id=user.id,
        action="command",
        details={"command": "start"}
    )
    
    await update.message.reply_text(
        "Welcome to the Payment Bot! 💰\n\n"
        "📝 To submit a payment:\n"
        "1. Send payment details in this format:\n\n"
        "✅ I have successfully completed the payment.\n"
        "📱 Telegram Username: @your_username\n"
        "💳 Transaction ID: 123456789\n"
        "💰 Amount Paid: ₹100\n"
        "⏳ Time Period: 30 days\n"
        "🙏 Thank you!\n\n"
        "2. Then send your payment screenshot\n\n"
        "🔹 Commands:\n"
        "/mypayments - View your payment history\n"
        "/help - Show instructions"
    )

async def handle_payment_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle payment message submission"""
    user = update.message.from_user
    await log_user_action(
        user_id=user.id,
        action="payment_submission",
        details={"message": update.message.text}
    )
    
    match = PAYMENT_PATTERN.match(update.message.text)
    
    if not match:
        await update.message.reply_text("❌ Invalid format. Please use the correct format shown in /start.")
        return

    username, transaction_id, amount, period_num, period_unit = match.groups()[:5]
    
    if transactions_collection.find_one({"transaction_id": transaction_id}):
        await update.message.reply_text("❌ This Transaction ID was already used. Each ID can only be used once.")
        return

    context.user_data['pending_payment'] = {
        'user_id': user.id,
        'username': username,
        'transaction_id': transaction_id,
        'amount': amount,
        'period_num': period_num,
        'period_unit': period_unit,
        'text': update.message.text
    }

    await update.message.reply_text("✅ Payment details received. Please now send your payment screenshot as a photo.")

async def handle_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle payment screenshot submission"""
    user = update.message.from_user
    
    if 'pending_payment' not in context.user_data:
        await update.message.reply_text("❌ Please send payment details first.")
        return

    payment_data = context.user_data['pending_payment']
    
    try:
        transactions_collection.insert_one({
            "transaction_id": payment_data['transaction_id'],
            "user_id": user.id,
            "registered_at": get_utc_now()
        })
    except pymongo.errors.DuplicateKeyError:
        await update.message.reply_text("❌ This Transaction ID was already used. Please contact support.")
        return

    days = convert_period_to_days(payment_data['period_num'], payment_data['period_unit'])
    payment_id = payments_collection.insert_one({
        "user_id": user.id,
        "username": payment_data['username'],
        "transaction_id": payment_data['transaction_id'],
        "amount": int(payment_data['amount']),
        "days": days,
        "period_display": f"{payment_data['period_num']} {payment_data['period_unit']}",
        "status": "pending",
        "created_at": get_utc_now(),
        "screenshot_id": update.message.photo[-1].file_id
    }).inserted_id

    # Send to admin
    admin_msg = (
        f"📦 New Payment Submission\n\n"
        f"👤 User: @{payment_data['username']} (ID: {user.id})\n"
        f"💳 Transaction ID: {payment_data['transaction_id']}\n"
        f"💰 Amount: ₹{payment_data['amount']}\n"
        f"⏳ Period: {payment_data['period_num']} {payment_data['period_unit']}\n\n"
        f"🆔 Payment ID: {payment_id}"
    )
    
    await context.bot.send_message(ADMIN_CHAT_ID, admin_msg)
    await context.bot.send_photo(
        ADMIN_CHAT_ID, 
        photo=update.message.photo[-1].file_id,
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

    await update.message.reply_text(
        "✅ Your payment has been submitted for admin approval.\n"
        "You'll be notified when it's processed.\n\n"
        "Use /mypayments to check status."
    )
    
    await log_user_action(
        user_id=user.id,
        action="payment_complete",
        details={"payment_id": str(payment_id)}
    )
    
    del context.user_data['pending_payment']

async def my_payments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user payment history"""
    user = update.message.from_user
    payments = list(payments_collection.find({"user_id": user.id}).sort("created_at", -1).limit(10))
    
    await log_user_action(
        user_id=user.id,
        action="command",
        details={"command": "mypayments"}
    )
    
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
        approved_payments = payments_collection.count_documents({"status": "approved"})
        rejected_payments = payments_collection.count_documents({"status": "rejected"})
        
        db_stats = db.command("dbStats")
        storage_size = db_stats.get("storageSize", 0) / (1024 * 1024)  # Convert to MB
        
        stats_msg = (
            "📊 Admin Statistics\n\n"
            f"👥 Total Users: {total_users}\n"
            f"💰 Total Payments: {total_payments}\n"
            f"✅ Approved: {approved_payments}\n"
            f"⏳ Pending: {pending_payments}\n"
            f"❌ Rejected: {rejected_payments}\n"
            f"💾 Storage Used: {storage_size:.2f} MB\n\n"
            "🔹 /manage_payments - View pending payments\n"
            "🔹 /user_logs <user_id> - View user activity\n"
            "🔹 /broadcast - Send message to all users"
        )
        
        await update.message.reply_text(stats_msg)
    except Exception as e:
        logger.error(f"Error getting stats: {e}")
        await update.message.reply_text("❌ Error retrieving statistics.")

async def manage_payments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin payment management"""
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

async def handle_callback(update: Update, context: CallbackContext):
    """Handle admin callbacks"""
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
            
            await log_user_action(
                user_id=payment["user_id"],
                action="payment_approved",
                details={"payment_id": str(payment_id)}
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
            
            await log_user_action(
                user_id=payment["user_id"],
                action="payment_rejected",
                details={"payment_id": str(payment_id)}
            )
            
    except Exception as e:
        logger.error(f"Error in callback: {e}")
        await query.edit_message_text("❌ Error processing request.")

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin broadcast message to all users"""
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

async def admin_wipe_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Wipe all bot data (admin only)"""
    if str(update.message.from_user.id) != ADMIN_CHAT_ID:
        await update.message.reply_text("❌ Admin only command.")
        return

    keyboard = [
        [InlineKeyboardButton("✅ Confirm Wipe", callback_data="confirm_wipe")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_wipe")]
    ]
    await update.message.reply_text(
        "⚠️ This will DELETE ALL DATA including:\n"
        "- All payment records\n"
        "- All user data\n"
        "- All transaction history\n\n"
        "This action cannot be undone!",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def restart_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Restart the bot (admin only)"""
    if str(update.message.from_user.id) != ADMIN_CHAT_ID:
        await update.message.reply_text("❌ Admin only command.")
        return
    await update.message.reply_text("🔄 Bot restarting...")
    os.execv(sys.executable, ['python'] + sys.argv)

async def view_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View recent logs (admin only)"""
    if str(update.message.from_user.id) != ADMIN_CHAT_ID:
        await update.message.reply_text("❌ Admin only command.")
        return

    try:
        logs = list(message_logs_collection.find()
                   .sort("timestamp", -1)
                   .limit(50))
        
        if not logs:
            await update.message.reply_text("No logs found.")
            return
            
        log_messages = []
        for log in logs:
            log_messages.append(
                f"{log['timestamp'].strftime('%Y-%m-%d %H:%M')} "
                f"[{log['type']}] User {log['user_id']}: {log['content'][:50]}"
            )
        
        # Split into chunks to avoid message length limits
        for i in range(0, len(log_messages), 10):
            await update.message.reply_text(
                "📋 Recent Logs:\n\n" + "\n".join(log_messages[i:i+10])
            )
            await asyncio.sleep(0.5)
            
    except Exception as e:
        logger.error(f"Error viewing logs: {e}")
        await update.message.reply_text("❌ Error retrieving logs.")

async def get_user_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get logs for a specific user (admin only)"""
    if str(update.message.from_user.id) != ADMIN_CHAT_ID:
        await update.message.reply_text("❌ Admin only command.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /user_logs <user_id> [limit=10]")
        return

    try:
        user_id = int(context.args[0])
        limit = int(context.args[1]) if len(context.args) > 1 else 10
        
        user = users_collection.find_one({"user_id": user_id})
        if not user:
            await update.message.reply_text("❌ User not found.")
            return
            
        logs = list(user_logs_collection.find({"user_id": user_id})
                   .sort("timestamp", -1)
                   .limit(limit))
        
        if not logs:
            await update.message.reply_text(f"No logs found for user {user_id}.")
            return
            
        log_messages = [
            f"{log['timestamp'].strftime('%Y-%m-%d %H:%M')} - {log['action']}"
            + (f"\n{log['details']}" if log.get('details') else "")
            for log in logs
        ]
        
        await update.message.reply_text(
            f"📋 User Logs for @{user.get('username', 'unknown')} (ID: {user_id}):\n\n"
            + "\n\n".join(log_messages)
        )
        
    except Exception as e:
        logger.error(f"Error getting user logs: {e}")
        await update.message.reply_text("❌ Error retrieving logs.")

async def handle_admin_callbacks(update: Update, context: CallbackContext):
    """Handle admin management callbacks"""
    query = update.callback_query
    await query.answer()

    if str(query.from_user.id) != ADMIN_CHAT_ID:
        await query.edit_message_text("❌ Admin only action.")
        return

    if query.data == "confirm_wipe":
        try:
            # Wipe all collections
            db.drop_collection("payments")
            db.drop_collection("transactions")
            db.drop_collection("users")
            db.drop_collection("user_logs")
            db.drop_collection("message_logs")
            
            # Recreate indexes
            payments_collection.create_index([("user_id", 1)])
            transactions_collection.create_index([("transaction_id", 1)], unique=True)
            users_collection.create_index([("user_id", 1)], unique=True)
            user_logs_collection.create_index([("user_id", 1)])
            user_logs_collection.create_index([("timestamp", 1)])
            message_logs_collection.create_index([("timestamp", 1)])
            
            await query.edit_message_text("✅ All data has been wiped. Bot is now fresh.")
        except Exception as e:
            logger.error(f"Data wipe failed: {e}")
            await query.edit_message_text("❌ Failed to wipe data. Check logs.")
    
    elif query.data == "cancel_wipe":
        await query.edit_message_text("❌ Data wipe cancelled.")

# HTTP Server Setup
async def health_check(request):
    """Health check endpoint for Koyeb"""
    return web.Response(text="OK")

def setup_http_server():
    """Set up the HTTP server for health checks"""
    app = web.Application()
    app.router.add_get('/healthz', health_check)
    return app

async def start_http_server():
    """Start the HTTP server on the specified port"""
    app = setup_http_server()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"HTTP server started on port {PORT}")

async def setup_bot_application():
    """Setup and return the Telegram bot application"""
    application = Application.builder().token(BOT_TOKEN).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("mypayments", my_payments))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("manage_payments", manage_payments))
    application.add_handler(CommandHandler("broadcast", broadcast))
    application.add_handler(CommandHandler("wipe_data", admin_wipe_data))
    application.add_handler(CommandHandler("restart", restart_bot))
    application.add_handler(CommandHandler("view_logs", view_logs))
    application.add_handler(CommandHandler("user_logs", get_user_logs))
    
    # Message handlers
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_payment_message))
    application.add_handler(MessageHandler(filters.PHOTO, handle_screenshot))
    
    # Callback handlers
    application.add_handler(CallbackQueryHandler(handle_callback, pattern="^(approve|reject)_"))
    application.add_handler(CallbackQueryHandler(handle_admin_callbacks, pattern="^(confirm_wipe|cancel_wipe)$"))
    
    return application

async def main_async():
    """Async main function that runs both HTTP server and Telegram bot"""
    # Start HTTP server for health checks
    http_server_task = asyncio.create_task(start_http_server())
    
    # Setup and run Telegram bot
    application = await setup_bot_application()
    bot_task = asyncio.create_task(application.run_polling())
    
    logger.info("Bot is starting with all features...")
    
    # Run both tasks concurrently
    await asyncio.gather(http_server_task, bot_task)

def main():
    """Entry point for the application"""
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        logger.info("Bot shutting down...")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
