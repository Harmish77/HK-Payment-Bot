import os
import re
import sys
import logging
import asyncio
import signal
import traceback
import base64 
from datetime import datetime, timezone
from typing import Dict, Optional, List

import pymongo
from bson import ObjectId
from dotenv import load_dotenv
from telegram import (Update,InlineKeyboardButton,InlineKeyboardMarkup,InputMediaPhoto,ReplyKeyboardRemove,Bot)
from telegram.ext import (Application,CommandHandler,MessageHandler,CallbackQueryHandler,Updater,filters,ContextTypes,CallbackContext)
from aiohttp import web
async def send_log_to_channel(message: str, bot: Bot = None, photo: str = None):
    """Send logs to the designated log channel"""
    if not LOG_CHANNEL_ID:
        return
        
    try:
        # Use existing bot if available, otherwise create temporary one
        close_bot = False
        if not bot:
            bot = Bot(token=BOT_TOKEN)
            close_bot = True
            
        try:
            if photo:
                await bot.send_photo(
                    chat_id=LOG_CHANNEL_ID,
                    photo=photo,
                    caption=message[:1000],
                    parse_mode="HTML"
                )
            else:
                await bot.send_message(
                    chat_id=LOG_CHANNEL_ID,
                    text=message[:4000],
                    parse_mode="HTML"
                )
        except Exception as e:
            logger.error(f"Failed to send log to channel: {e}")
        finally:
            if close_bot:
                await asyncio.sleep(1)  # Rate limiting
                await bot.close()
                
    except Exception as e:
        logger.error(f"Logging system error: {e}")
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
PREMIUM_APPROVAL_GROUP_ID = os.getenv("PREMIUM_APPROVAL_GROUP_ID")
# Validate environment variables
if not all([BOT_TOKEN, ADMIN_CHAT_ID, MONGODB_URI]):
    logger.error("Missing required environment variables")
    sys.exit(1)

# MongoDB setup
client = pymongo.MongoClient(MONGODB_URI)
db = client["payment_bot"]
payments_collection = db["payments"]
transactions_collection = db["transactions"]
users_collection = db["users"]
user_logs_collection = db["user_logs"]
message_logs_collection = db["message_logs"]

PAYMENT_PATTERN = re.compile(
    r"✅ Payment Form Submission\s*"
    r"👤 Username: @?([^\s]+)\s*"
    r"💳 TXN ID: (\d{12})\s*"
    r"💰 Amount: ₹(\d+)\s*"
    r"⏳ Period: (.+?)\s*"
    r"(📸 Please send your payment screenshot)?",
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

def parse_time_period(period_str: str) -> str:
    """Convert payment period to premium bot command format"""
    period_str = period_str.lower().strip()
    
    # Remove plural 's' if present
    if period_str.endswith('s'):
        period_str = period_str[:-1]
    
    # Standardize units
    unit_mapping = {
        'mon': 'month',
        'hr': 'hour',
        'min': 'minute',
        'day': 'day',
        'year': 'year'
    }
    
    # Extract number and unit
    match = re.match(r'(\d+)([a-zA-Z]+)', period_str)
    if not match:
        return "1month"  # Default if parsing fails
    
    num = match.group(1)
    unit = match.group(2)
    
    # Map to standard unit
    for short, long in unit_mapping.items():
        if unit.startswith(short):
            unit = long
            break
    
    return f"{num}{unit}"
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

async def register_new_user(user_id: int, username: str) -> bool:
    """Register a new user and return True if first-time registration"""
    try:
        user = users_collection.find_one({"user_id": user_id})
        
        if user:
            return False  # Existing user
            
        users_collection.insert_one({
            "user_id": user_id,
            "username": username,
            "first_seen": get_utc_now()
        })
        return True
        
    except Exception as e:
        logger.error(f"Failed to register user {user_id}: {e}")
        return False

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await register_new_user(user.id, user.username)  # Always register user
    
    if context.args:
        try:
            # 1. Fix Base64 padding and decode
            encoded = context.args[0]
            padding = len(encoded) % 4
            if padding:
                encoded += '=' * (4 - padding)
            
            decoded = base64.b64decode(encoded).decode('utf-8')
            
            # 2. Split into components and validate
            parts = decoded.split('|')
            if len(parts) != 4:
                raise ValueError("Invalid data format")
                
            username, txn_id, amount, period = parts
            
            # Validate transaction ID (12 digits)
            if not re.fullmatch(r'\d{12}', txn_id):
                raise ValueError("Transaction ID must be 12 digits")
            
            # 3. Format period display (3Months → 3 Months)
            period_display = re.sub(r'(\d+)([a-zA-Z]+)', r'\1 \2', period)
            
            # 4. Check for duplicate transaction
            if transactions_collection.find_one({"transaction_id": txn_id}):
                await update.message.reply_text("⚠️ This transaction was already processed.")
                return
            
            # 5. Store payment data
            context.user_data['payment'] = {
                'username': username,
                'transaction_id': txn_id,
                'amount': amount,
                'period': period,
                'period_display': period_display,
                'source': 'web_form'
            }
            
            # 6. Send confirmation
            await update.message.reply_text(
                "✅ Payment Received!\n\n"
                f"👤 @{username}\n"
                f"💳 {txn_id}\n"
                f"💰 ₹{amount}\n"
                f"⏳ {period_display}\n\n"
                "📸 Please send your payment screenshot now.",
                reply_markup=ReplyKeyboardRemove()
            )
            
            # Log successful processing
            logger.info(f"Processed web payment: {txn_id}")
            
        except Exception as e:
            logger.error(f"Link error: {str(e)}\nData: {context.args[0]}")
            await update.message.reply_text(
                "⚠️ Payment link invalid\n"
                "Please send manually:\n"
                "1. Screenshot\n"
                "2. TXN ID\n"
                "3. Amount\n"
                "4. Period\n\n"
                "Example:\n"
                "TXN: 56666667777\n"
                "Amount: ₹60\n"
                "Period: 3 Months"
            )
        return
    
    # Normal start command
    await update.message.reply_text(
        "Welcome to MovieHub Premium!",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Payment Form", url="https://harmish77.github.io/HK_payment_V0/")]
        ])
            )


async def handle_payment_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await send_log_to_channel(
        f"📩 <b>Payment Message Received</b>\n"
        f"👤 @{user.username}\n"
        f"📝 {update.message.text[:100]}...",
        bot=context.bot
    )
    #user = update.message.from_user
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
    user = update.effective_user
    
    if 'payment' not in context.user_data:
        await update.message.reply_text(
            "Please submit payment details first via our website:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Payment Form", url="https://harmish77.github.io/HK_payment_V0/")]
            ])
        )
        return
        
    payment_data = context.user_data['payment']
    
    # Additional validation
    if not update.message.photo:
        await update.message.reply_text("Please send the screenshot as a photo (not file).")
        return
        
    try:
        # Save to database - ensure all keys match what's stored in context.user_data
        payment_id = payments_collection.insert_one({
        "user_id": user.id,
        "username": payment_data['username'],
        "transaction_id": payment_data['transaction_id'],
        "amount": int(payment_data['amount']),
        "period": payment_data['period'],
        "period_display": payment_data.get('period_display', payment_data['period']),
        "status": "pending",
        "source": payment_data.get('source', 'bot'),
        "created_at": get_utc_now(),
        "screenshot_id": update.message.photo[-1].file_id
    }).inserted_id
        
        # Notify admin
        await context.bot.send_photo(
            chat_id=ADMIN_CHAT_ID,
            photo=update.message.photo[-1].file_id,
            caption=(
                f"🌐 <b>Payment Received</b>\n\n"
                f"👤 User: @{payment_data['username']} (ID: {user.id})\n"
                f"💳 TXN: {payment_data.get('txn_id') or payment_data.get('transaction_id')}\n"
                f"💰 ₹{payment_data['amount']}\n"
                f"⏳ Period: {payment_data['period']}\n"
                f"🆔 Payment ID: {payment_id}"
            ),
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Approve", callback_data=f"approve_{payment_id}"),
                    InlineKeyboardButton("❌ Reject", callback_data=f"reject_{payment_id}")
                ]
            ]),
            parse_mode="HTML"
        )
        
        await update.message.reply_text(
            "✅ Verification submitted!\n\n"
            "Your payment is now pending admin approval."
        )
        
        del context.user_data['payment']
        
    except KeyError as e:
        logger.error(f"Missing key in payment data: {e}")
        await update.message.reply_text(
            "⚠️ Payment data incomplete. Please start over."
        )
    except Exception as e:
        logger.error(f"Screenshot handling error: {e}")
        await update.message.reply_text(
            "⚠️ An error occurred. Please contact support."
        )

async def my_payments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    payments = list(payments_collection.find({"user_id": user.id}).sort("created_at", -1).limit(10))
    
    if not payments:
        await update.message.reply_text("📭 You don't have any payment history yet.")
        return
    
    messages = []
    for payment in payments:
        # Format period if period_display doesn't exist
        period = payment.get('period_display') 
        if not period:
            period = re.sub(r'(\d+)([a-zA-Z]+)', r'\1 \2', payment.get('period', 'N/A'))
        
        status_emoji = "⏳" if payment["status"] == "pending" else "✅" if payment["status"] == "approved" else "❌"
        msg = (
            f"{status_emoji} {payment['status'].capitalize()}\n"
            f"💳 ID: {payment['transaction_id']}\n"
            f"💰 Amount: ₹{payment['amount']}\n"
            f"⏳ Period: {period}\n"
            f"📅 Date: {payment['created_at'].strftime('%Y-%m-%d %H:%M')}\n"
        )
        messages.append(msg)
    
    await update.message.reply_text("📋 Your Payment History:\n\n" + "\n".join(messages))

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    await send_log_to_channel(
        f"📈 <b>Stats Accessed</b>\n"
        f"👨‍💻 Admin: @{admin.username}",
        bot=context.bot
    )
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
    admin = update.effective_user
    await send_log_to_channel(
        f"📝 <b>Payment Management Accessed</b>\n"
        f"👨‍💻 Admin: @{admin.username}",
        bot=context.bot
    )
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
    query = update.callback_query
    await query.answer()
    
    try:
        # Parse callback data
        action, payment_id = query.data.split('_')
        payment_id = ObjectId(payment_id)
        
        # Verify admin
        admin = query.from_user
        if str(admin.id) != ADMIN_CHAT_ID:
            await query.message.reply_text("❌ Admin only action.")
            return

        # Get payment data efficiently
        payment = payments_collection.find_one(
            {"_id": payment_id},
            {
                "user_id": 1,
                "username": 1,
                "transaction_id": 1,
                "amount": 1,
                "period": 1,
                "status": 1
            }
        )
        if not payment:
            await query.message.reply_text("❌ Payment not found!")
            return

        if action == "approve":
            # Update database with atomic operation
            update_result = payments_collection.update_one(
                {"_id": payment_id, "status": {"$ne": "approved"}},
                {"$set": {
                    "status": "approved",
                    "approved_at": get_utc_now(),
                    "approved_by": admin.username,
                    "premium_activated": False
                }}
            )
            
            if update_result.modified_count == 0:
                await query.answer("Payment already approved!", show_alert=True)
                return

            # Notify user in background
            asyncio.create_task(
                context.bot.send_message(
                    chat_id=payment["user_id"],
                    text=(
                        "🎉 Payment Approved!\n\n"
                        f"💳 TXN: {payment['transaction_id']}\n"
                        f"💰 Amount: ₹{payment['amount']}\n"
                        f"⏳ Period: {payment['period']}\n\n"
                        "Your premium access is being processed..."
                    )
                )
            )

            # Process premium activation
            period_match = re.match(r'(\d+)(\w+)', payment['period'].lower())
            if period_match:
                period_num = period_match.group(1)
                period_unit = period_match.group(2).rstrip('s')  # Remove plural
                
                # Standardize units
                unit_mapping = {
                    'month': 'month', 'mon': 'month',
                    'year': 'year', 'yr': 'year',
                    'day': 'day',
                    'hour': 'hour', 'hr': 'hour',
                    'min': 'min', 'minute': 'min'
                }
                period_unit = unit_mapping.get(period_unit, 'month')
                
                command = f"/add_premium {payment['user_id']} {period_num}{period_unit}"
                
                # Create interactive message for admin
                keyboard = [
                    [
                        InlineKeyboardButton(
                            "📋 Copy Command", 
                            callback_data=f"copy_{base64.urlsafe_b64encode(command.encode()).decode()}"
                        ),
                        InlineKeyboardButton(
                            "➡️ Open Premium Bot", 
                            url=f"https://t.me/hks_movie_bot?start=start"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "✅ Mark Completed", 
                            callback_data=f"complete_{payment_id}"
                        )
                    ]
                ]

                # Send to admin's PM
                await context.bot.send_message(
                    chat_id=admin.id,
                    text=(
                        "🛎️ *Premium Activation Required*\n\n"
                        f"👤 User ID: `{payment['user_id']}`\n"
                        f"⏳ Period: {period_num} {period_unit}\n"
                        f"💳 TXN: `{payment['transaction_id']}`\n\n"
                        "Click below to copy the command and open the premium bot:"
                    ),
                    parse_mode="MarkdownV2",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )

        elif action == "reject":
            # Atomic rejection to prevent duplicates
            update_result = payments_collection.update_one(
                {"_id": payment_id, "status": {"$ne": "rejected"}},
                {"$set": {
                    "status": "rejected",
                    "rejected_at": get_utc_now(),
                    "rejected_by": admin.username
                }}
            )
            
            if update_result.modified_count == 0:
                await query.answer("Payment already rejected!", show_alert=True)
                return

            # Notify user
            asyncio.create_task(
                context.bot.send_message(
                    chat_id=payment["user_id"],
                    text=(
                        "❌ Payment Rejected\n\n"
                        f"TXN: {payment['transaction_id']}\n"
                        "Contact support if this is an error."
                    )
                )
            )

        # Update original message
        await query.edit_message_text(
            f"✅ {action.capitalize()}d payment {payment['transaction_id']}\n"
            f"{'Check your PM for activation details' if action == 'approve' else ''}",
            reply_markup=None
        )

        # Log the action
        await send_log_to_channel(
            f"🔹 Payment {action.capitalize()}d\n"
            f"👤 User: {payment.get('username', payment['user_id'])}\n"
            f"💳 TXN: {payment['transaction_id']}\n"
            f"🛠️ By: @{admin.username}",
            bot=context.bot
        )

    except Exception as e:
        logger.error(f"Callback error: {str(e)}", exc_info=True)
        await query.answer("⚠️ Processing failed. Check logs.", show_alert=True)
        await send_log_to_channel(
            f"🔥 Payment Processing Error\n"
            f"Payment ID: {payment_id}\n"
            f"Error: {str(e)[:200]}",
            bot=context.bot
        )

async def copy_command_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    try:
        encoded_command = query.data.split('_', 1)[1]
        command = base64.urlsafe_b64decode(encoded_command.encode()).decode()
        await query.answer(f"Copied to clipboard:\n{command}", show_alert=True)
    except Exception as e:
        logger.error(f"Copy command error: {e}")
        await query.answer("❌ Copy failed", show_alert=True)

async def complete_premium_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    try:
        payment_id = ObjectId(query.data.split('_')[1])
        
        # Verify admin
        if str(query.from_user.id) != ADMIN_CHAT_ID:
            await query.answer("❌ Admin only", show_alert=True)
            return
            
        # Update payment status
        result = payments_collection.update_one(
            {"_id": payment_id, "premium_activated": False},
            {"$set": {
                "premium_activated": True,
                "activated_at": get_utc_now(),
                "activated_by": query.from_user.username
            }}
        )
        
        if result.modified_count == 1:
            await query.answer("✅ Premium activated", show_alert=True)
            await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "✅ Premium Activated", 
                        callback_data="activated"
                    )]
                ])
            )
        else:
            await query.answer("Already activated!", show_alert=True)
            
    except Exception as e:
        logger.error(f"Complete premium error: {e}")
        await query.answer("❌ Update failed", show_alert=True)

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    await send_log_to_channel(
        f"📢 <b>Broadcast Initiated</b>\n"
        f"👨‍💻 @{admin.username}\n"
        f"✉️ {context.args[:50]}...",
        bot=context.bot
    )
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

async def web_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Guide web users through payment process"""
    await update.message.reply_text(
        "To make a payment:\n\n"
        "1. Visit our payment website\n"
        "2. Complete the payment form\n"
        "3. You'll be redirected to this bot automatically\n"
        "4. Send your payment screenshot when prompted\n\n"
        "Start now:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Open Payment Form", url="https://harmish77.github.io/HK_payment_V0/")]
        ])
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

async def health_check(request):
    """Health check endpoint for Koyeb"""
    return web.Response(text="OK")
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    error = context.error
    tb = traceback.format_exc()
    
    error_msg = (
        f"⚠️ Error: {str(error)[:200]}\n"
        f"🔹 Update: {update.to_dict() if update else 'None'}"
    )
    
    await send_log_to_channel(
        f"🔥 <b>Error Occurred</b>\n{error_msg}\n<code>{tb[:1000]}</code>",
        bot=context.bot
    )
    
    if update and update.effective_message:
        await update.effective_message.reply_text(
            "An error occurred. Our team has been notified."
        )
async def start_http_server():
    """Start a simple HTTP server for health checks"""
    app = web.Application()
    app.router.add_get("/healthz", health_check)  # Koyeb expects /healthz endpoint
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    site = web.TCPSite(runner, "0.0.0.0", PORT)  # Use the PORT from your config
    await site.start()
    
    logger.info(f"HTTP server running on port {PORT}")
    return runner  # Important for proper cleanup

async def main():
    """Main entry point for the application"""
    # Initialize bot
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Add all handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("mypayments", my_payments))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("manage_payments", manage_payments))
    application.add_handler(CommandHandler("broadcast", broadcast))
    application.add_handler(CommandHandler("wipe_data", admin_wipe_data))
    application.add_handler(CommandHandler("restart", restart_bot))
    application.add_handler(CommandHandler("view_logs", view_logs))
    application.add_handler(CommandHandler("user_logs", get_user_logs))
    application.add_handler(CommandHandler("payment", web_payment))
    application.add_error_handler(error_handler)
    
    # Message handlers
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_payment_message))
    application.add_handler(MessageHandler(filters.PHOTO, handle_screenshot))
    
    # Callback handlers
    application.add_handler(CallbackQueryHandler(handle_callback, pattern="^(approve|reject)_"))
    application.add_handler(CallbackQueryHandler(handle_admin_callbacks, pattern="^(confirm_wipe|cancel_wipe)$"))
    application.add_handler(CallbackQueryHandler(copy_command_callback, pattern="^copy_"))
    application.add_handler(CallbackQueryHandler(complete_premium_callback, pattern="^complete_"))

    # Start HTTP server
    http_runner = await start_http_server()
    
    try:
        # Send startup log with delay
        await asyncio.sleep(2)  # Initial delay to avoid flood
        await send_log_to_channel(
            f"🟢 <b>Bot Starting</b>\n"
            f"⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            bot=application.bot
        )

        # Initialize with clean update state
        await application.initialize()
        await application.start()
        await application.updater.start_polling(
            poll_interval=3.0,  # Increased interval
            timeout=30,
            drop_pending_updates=True
        )
        
        logger.info("Bot is now running")
        
        # Keep the application running
        while True:
            await asyncio.sleep(3600)  # Sleep for 1 hour
            
    except asyncio.CancelledError:
        logger.info("Received shutdown signal")
    except Exception as e:
        logger.error(f"Bot error: {e}")
        await send_log_to_channel(
            f"🔴 <b>Bot Error</b>\n"
            f"⚠️ {type(e).__name__}\n"
            f"📝 {str(e)[:300]}",
            bot=application.bot
        )
    finally:
        logger.info("Shutting down...")
        try:
            await send_log_to_channel(
                f"🛑 <b>Bot Stopping</b>\n"
                f"⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
                bot=application.bot
            )
        except:
            pass
            
        # Cleanup application if it was started
        if 'application' in locals():
            try:
                if application.running:
                    await application.updater.stop()
                    await application.stop()
                    await application.shutdown()
            except:
                pass
        
        # Cleanup HTTP server
        await http_runner.cleanup()
        logger.info("Bot stopped")
if __name__ == "__main__":
    # Configure asyncio policy for Koyeb
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    # Create and set event loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        # Run main until complete
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("Bot shutting down...")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
    finally:
        # Give time for cleanup
        tasks = asyncio.all_tasks(loop=loop)
        for task in tasks:
            task.cancel()
        loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
        loop.close()
