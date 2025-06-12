import logging
import re
import os # Import the os module
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters
from pymongo import MongoClient
from datetime import datetime
from bson.objectid import ObjectId

# --- Configuration ---
# Get sensitive information from environment variables
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
ADMIN_CHAT_ID = int(os.getenv('TELEGRAM_ADMIN_CHAT_ID')) # Convert to int
LOG_CHANNEL_ID = int(os.getenv('TELEGRAM_LOG_CHANNEL_ID')) # Convert to int (optional)
MONGO_URI = os.getenv('MONGO_DB_CONNECTION_URI')
DB_NAME = os.getenv('MONGO_DB_NAME', 'telegram_payments_bot') # Default to 'telegram_payments_bot' if not set

# --- Input Validation (Crucial for startup) ---
if not TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable is not set!")
if not ADMIN_CHAT_ID:
    raise ValueError("TELEGRAM_ADMIN_CHAT_ID environment variable is not set!")
if not MONGO_URI:
    raise ValueError("MONGO_DB_CONNECTION_URI environment variable is not set!")


# --- MongoDB Setup ---
client = MongoClient(MONGO_URI)
db = client[DB_NAME]
payment_requests_collection = db['payment_requests'] # Collection for payment requests

# --- Logging Setup ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Bot Commands (UNCHANGED from previous version, just ensure it's in the same file) ---

async def start(update: Update, context):
    """Sends a welcome message when the /start command is issued."""
    await update.message.reply_text(
        "👋 Hi there! I'm your payment confirmation bot.\n\n"
        "Please send your payment details in the following format:\n\n"
        "✅ I have successfully completed the payment.\n\n"
        "📱 Telegram Username: @YourUsername\n"
        "💳 Transaction ID: YourTransactionID\n"
        "💰 Amount Paid: ₹X\n"
        "⏳ Time Period: Y Days\n\n"
        "📸 You can also send the payment screenshot. Please reply to my confirmation message with the screenshot.\n\n" # Updated instructions
        "🙏 Thank you!"
    )

async def handle_message(update: Update, context):
    """Handles incoming messages and parses payment confirmations and screenshots."""
    if update.message.photo:
        # Handle photo messages (screenshots)
        photo = update.message.photo[-1]  # Get the highest resolution photo
        file_id = photo.file_id
        
        # Download the photo temporarily (Koyeb has ephemeral storage, it's fine for this)
        # You might consider uploading to a persistent storage service like S3 if you need to keep them
        # or if the bot crashes and restarts before forwarding. For forwarding directly, temporary is okay.
        
        # Using context.bot.get_file and then .download_to_drive is ideal for files
        new_file = await context.bot.get_file(file_id)
        filename = f"/tmp/screenshot_{file_id}.jpg" # Use /tmp for temporary storage on Linux/Koyeb
        await new_file.download_to_drive(filename)

        log_message = (
            f"📸 Screenshot received from @{update.effective_user.username or update.effective_user.full_name} (ID: `{update.effective_user.id}`)."
        )

        # Try to associate the screenshot with a payment request if it's a reply
        if update.message.reply_to_message:
            # Check if the replied message was from the bot and contains a request ID
            if update.message.reply_to_message.from_user.id == context.bot.id:
                # Look for the Request DB ID in the bot's sent message text
                match = re.search(r"Request DB ID: `(?P<request_db_id>[a-f0-9]{24})`", update.message.reply_to_message.text)
                if match:
                    request_db_id = match.group('request_db_id')
                    log_message = (
                        f"📸 Screenshot received for Payment Request DB ID `{request_db_id}` from @{update.effective_user.username or update.effective_user.full_name} (ID: `{update.effective_user.id}`)."
                    )
        
        # Forward the screenshot to the log channel
        if LOG_CHANNEL_ID:
            try:
                # Read the downloaded file in binary mode
                with open(filename, 'rb') as photo_file:
                    await context.bot.send_photo(chat_id=LOG_CHANNEL_ID, photo=photo_file, caption=log_message, parse_mode='Markdown')
                logger.info(f"Screenshot {filename} forwarded to log channel.")
            except Exception as e:
                logger.error(f"Error forwarding screenshot to log channel: {e}")
                await update.message.reply_text("❌ There was an issue forwarding your screenshot to the admin. Please try again.")
            finally:
                # Clean up the downloaded file
                if os.path.exists(filename):
                    os.remove(filename)
        else:
            logger.info(log_message + " (Log channel not configured)")
            # If no log channel, maybe just acknowledge or store path if needed elsewhere
            await update.message.reply_text("📸 Screenshot received. Admins will check it.")


    else:  # Handle text messages (payment confirmations)
        text = update.message.text
        user_id = update.effective_user.id
        username = update.effective_user.username or update.effective_user.full_name

        # Regex to parse the payment confirmation message
        match = re.search(
            r"📱 Telegram Username: @(?P<telegram_username>\w+)\n"
            r"💳 Transaction ID: (?P<transaction_id>\S+)\n"
            r"💰 Amount Paid: (?P<amount>₹\d+)\n"
            r"⏳ Time Period: (?P<time_period>\d+ Days)",
            text
        )

        if match:
            data = match.groupdict()
            telegram_username_from_message = data['telegram_username']
            transaction_id = data['transaction_id']
            amount = data['amount']
            time_period = data['time_period']

            # Check if the username in the message matches the sender's username
            if username.lower() != telegram_username_from_message.lower():
                await update.message.reply_text(
                    "⚠️ The Telegram Username in your message (@{}) does not match your current username ({}). "
                    "Please ensure they match for verification.".format(telegram_username_from_message, username)
                )
                return

            try:
                # Create a document to insert
                new_request_doc = {
                    'user_id': user_id,
                    'username': username,
                    'transaction_id': transaction_id,
                    'amount': amount,
                    'time_period': time_period,
                    'status': 'pending',
                    'admin_notes': None,
                    'created_at': datetime.now(),
                    'approved_rejected_at': None
                }

                # Insert the document into the collection
                result = payment_requests_collection.insert_one(new_request_doc)
                request_db_id = str(result.inserted_id)

                keyboard = [
                    [
                        InlineKeyboardButton("✅ Approve", callback_data=f"approve_{request_db_id}"),
                        InlineKeyboardButton("❌ Reject", callback_data=f"reject_{request_db_id}")
                    ]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)

                admin_message = (
                    f"🚨 **New Payment Request** 🚨\n\n"
                    f"User: @{username} (ID: `{user_id}`)\n"
                    f"Transaction ID: `{transaction_id}`\n"
                    f"Amount: `{amount}`\n"
                    f"Time Period: `{time_period}`\n"
                    f"Request DB ID: `{request_db_id}`\n\n"
                    f"Original Message:\n```\n{text}\n```"
                )
                await context.bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=admin_message,
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )

                await update.message.reply_text(
                    "✅ Your payment request has been received and sent to the admin for approval. "
                    "You will be notified once it's processed. **Please reply to this message with your payment screenshot if you have one.** Thank you!"
                )
                logger.info(f"Payment request from user {username} (ID: {user_id}) received. Request DB ID: {request_db_id}")

            except Exception as e:
                logger.error(f"Error saving payment request to MongoDB: {e}")
                await update.message.reply_text(
                    "❌ An error occurred while processing your request. Please try again later or contact support."
                )
        else:
            await update.message.reply_text(
                "I couldn't understand your message. Please use the specified format for payment confirmations, or send a screenshot. "
                "Type /start for instructions."
            )

async def button_callback(update: Update, context):
    """Handles inline keyboard button presses from admins."""
    query = update.callback_query
    await query.answer()

    data = query.data
    action, request_db_id_str = data.split('_')
    request_db_id = ObjectId(request_db_id_str)
    admin_id = query.from_user.id
    admin_username = query.from_user.username or query.from_user.full_name

    try:
        payment_request = payment_requests_collection.find_one({'_id': request_db_id})

        if not payment_request:
            await query.edit_message_text("Error: Payment request not found in the database.")
            logger.warning(f"Admin {admin_username} tried to act on non-existent request DB ID: {request_db_id}")
            return

        if payment_request.get('status') != 'pending':
            await query.edit_message_text(f"This request has already been {payment_request.get('status')} by {payment_request.get('admin_notes', 'another admin')}.")
            return

        update_fields = {
            'status': action,
            'admin_notes': f"{action.capitalize()} by @{admin_username} (ID: {admin_id})",
            'approved_rejected_at': datetime.now()
        }

        payment_requests_collection.update_one(
            {'_id': request_db_id},
            {'$set': update_fields}
        )

        updated_payment_request = payment_requests_collection.find_one({'_id': request_db_id})

        if action == 'approve':
            message_to_user = (
                "🎉 **Your payment request has been APPROVED!** 🎉\n\n"
                f"Transaction ID: `{updated_payment_request['transaction_id']}`\n"
                f"Amount Paid: `{updated_payment_request['amount']}`\n"
                f"Time Period: `{updated_payment_request['time_period']}`\n\n"
                "Thank you for your payment!"
            )
            log_message = (
                f"✅ Payment Request DB ID `{request_db_id_str}` (User: @{updated_payment_request['username']}) "
                f"APPROVED by @{admin_username} (ID: {admin_id})."
            )
        elif action == 'reject':
            message_to_user = (
                "❌ **Your payment request has been REJECTED.** ❌\n\n"
                f"Transaction ID: `{updated_payment_request['transaction_id']}`\n"
                f"Amount Paid: `{updated_payment_request['amount']}`\n"
                f"Time Period: `{updated_payment_request['time_period']}`\n\n"
                "Please check your details and try again, or contact support for assistance."
            )
            log_message = (
                f"❌ Payment Request DB ID `{request_db_id_str}` (User: @{updated_payment_request['username']}) "
                f"REJECTED by @{admin_username} (ID: {admin_id})."
            )

        await query.edit_message_text(
            f"{query.message.text}\n\n"
            f"**Status: {updated_payment_request['status'].upper()}**\n"
            f"By: @{admin_username} (ID: {admin_id})\n"
            f"At: {updated_payment_request['approved_rejected_at'].strftime('%Y-%m-%d %H:%M:%S')}",
            parse_mode='Markdown'
        )

        await context.bot.send_message(chat_id=updated_payment_request['user_id'], text=message_to_user, parse_mode='Markdown')

        if LOG_CHANNEL_ID:
            await context.bot.send_message(chat_id=LOG_CHANNEL_ID, text=log_message, parse_mode='Markdown')

        logger.info(log_message)

    except Exception as e:
        logger.error(f"Error processing callback for request DB ID {request_db_id_str}: {e}")
        await query.edit_message_text("An error occurred while processing this request.")


def main():
    """Starts the bot."""
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    # Use filters.ALL to catch both text and photo messages
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(button_callback))

    logger.info("Bot started polling...")
    application.run_polling()

if __name__ == '__main__':
    main()
            
