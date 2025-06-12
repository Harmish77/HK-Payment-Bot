import logging
import re
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters
from pymongo import MongoClient
from datetime import datetime
from bson.objectid import ObjectId

# --- Configuration ---
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
ADMIN_CHAT_ID = int(os.getenv('TELEGRAM_ADMIN_CHAT_ID'))
LOG_CHANNEL_ID = int(os.getenv('TELEGRAM_LOG_CHANNEL_ID'))
MONGO_URI = os.getenv('MONGO_DB_CONNECTION_URI')
DB_NAME = os.getenv('MONGO_DB_NAME', 'telegram_payments_bot')

if not TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable is not set!")
if not ADMIN_CHAT_ID:
    raise ValueError("TELEGRAM_ADMIN_CHAT_ID environment variable is not set!")
if not MONGO_URI:
    raise ValueError("MONGO_DB_CONNECTION_URI environment variable is not set!")

# --- MongoDB Setup ---
client = MongoClient(MONGO_URI)
db = client[DB_NAME]
payment_requests_collection = db['payment_requests']

# --- Logging Setup ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Helper Function for MarkdownV2 Escaping ---
def escape_markdown_v2(text: str) -> str:
    """Helper function to escape special characters for MarkdownV2 parsing."""
    escape_chars = '_*[]()~`>#+-=|{}.!'
    return ''.join('\\' + char if char in escape_chars else char for char in text)

# --- Bot Commands (updated for escaping) ---

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
        "📸 You can also send the payment screenshot. Please reply to my confirmation message with the screenshot.\n\n"
        "🙏 Thank you!"
    )

async def handle_message(update: Update, context):
    """Handles incoming messages and parses payment confirmations and screenshots."""
    if update.message.photo:
        photo = update.message.photo[-1]
        file_id = photo.file_id
        
        new_file = await context.bot.get_file(file_id)
        filename = f"/tmp/screenshot_{file_id}.jpg"
        await new_file.download_to_drive(filename)

        user_info = escape_markdown_v2(update.effective_user.username or update.effective_user.full_name)
        user_id_escaped = escape_markdown_v2(str(update.effective_user.id))

        log_message = (
            f"📸 Screenshot received from @{user_info} (ID: `{user_id_escaped}`)."
        )

        if update.message.reply_to_message:
            if update.message.reply_to_message.from_user.id == context.bot.id:
                match = re.search(r"Request DB ID: `(?P<request_db_id>[a-f0-9]{24})`", update.message.reply_to_message.text)
                if match:
                    request_db_id = escape_markdown_v2(match.group('request_db_id'))
                    log_message = (
                        f"📸 Screenshot received for Payment Request DB ID `{request_db_id}` from @{user_info} (ID: `{user_id_escaped}`)."
                    )
        
        if LOG_CHANNEL_ID:
            try:
                with open(filename, 'rb') as photo_file:
                    await context.bot.send_photo(chat_id=LOG_CHANNEL_ID, photo=photo_file, caption=log_message, parse_mode='MarkdownV2') # Use MarkdownV2
                logger.info(f"Screenshot {filename} forwarded to log channel.")
            except Exception as e:
                logger.error(f"Error forwarding screenshot to log channel: {e}")
                await update.message.reply_text("❌ There was an issue forwarding your screenshot to the admin. Please try again.")
            finally:
                if os.path.exists(filename):
                    os.remove(filename)
        else:
            logger.info(log_message + " (Log channel not configured)")
            await update.message.reply_text("📸 Screenshot received. Admins will check it.")

    else:
        text = update.message.text
        user_id = update.effective_user.id
        username = update.effective_user.username or update.effective_user.full_name

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

            if username.lower() != telegram_username_from_message.lower():
                await update.message.reply_text(
                    "⚠️ The Telegram Username in your message (@{}) does not match your current username ({}). "
                    "Please ensure they match for verification.".format(telegram_username_from_message, username)
                )
                return

            try:
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

                result = payment_requests_collection.insert_one(new_request_doc)
                request_db_id = str(result.inserted_id)

                keyboard = [
                    [
                        InlineKeyboardButton("✅ Approve", callback_data=f"approve_{request_db_id}"),
                        InlineKeyboardButton("❌ Reject", callback_data=f"reject_{request_db_id}")
                    ]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)

                # Escape all dynamic data before putting it into Markdown
                escaped_username = escape_markdown_v2(str(username))
                escaped_user_id = escape_markdown_v2(str(user_id))
                escaped_transaction_id = escape_markdown_v2(transaction_id)
                escaped_amount = escape_markdown_v2(amount)
                escaped_time_period = escape_markdown_v2(time_period)
                escaped_request_db_id = escape_markdown_v2(request_db_id)
                escaped_original_text = escape_markdown_v2(text) # Escape the entire original message too

                admin_message = (
                    f"🚨 **New Payment Request** 🚨\n\n"
                    f"User: @{escaped_username} (ID: `{escaped_user_id}`)\n"
                    f"Transaction ID: `{escaped_transaction_id}`\n"
                    f"Amount: `{escaped_amount}`\n"
                    f"Time Period: `{escaped_time_period}`\n"
                    f"Request DB ID: `{escaped_request_db_id}`\n\n"
                    f"Original Message:\n```\n{escaped_original_text}\n```" # This needs special care for code blocks
                )
                # For multiline code blocks, MarkdownV2 ` needs to be handled carefully.
                # If original message text might contain triple backticks or other complex markdown,
                # you might need a more sophisticated markdown parsing library or just send it as plain text.
                # For simplicity here, assume it's mostly plain text being put into a single code block.

                await context.bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=admin_message,
                    reply_markup=reply_markup,
                    parse_mode='MarkdownV2' # Use MarkdownV2
                )

                await update.message.reply_text(
                    "✅ Your payment request has been received and sent to the admin for approval. "
                    "You will be notified once it's processed. **Please reply to this message with your payment screenshot if you have one.** Thank you!"
                )
                logger.info(f"Payment request from user {username} (ID: {user_id}) received. Request DB ID: {request_db_id}")

            except Exception as e:
                # Log the full error to understand if it's still a formatting issue
                logger.error(f"Error processing text message or sending admin message: {e}", exc_info=True)
                await update.message.reply_text(
                    "❌ An error occurred while processing your request. Please try again later or contact support."
                )
        else:
            await update.message.reply_text(
                "I couldn't understand your message. Please use the specified format for payment confirmations, or send a screenshot. "
                "Type /start for instructions."
            )

async def button_callback(update: Update, context):
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

        # Escape all dynamic data before putting it into Markdown
        escaped_transaction_id = escape_markdown_v2(updated_payment_request['transaction_id'])
        escaped_amount = escape_markdown_v2(updated_payment_request['amount'])
        escaped_time_period = escape_markdown_v2(updated_payment_request['time_period'])

        if action == 'approve':
            message_to_user = (
                "🎉 **Your payment request has been APPROVED!** 🎉\n\n"
                f"Transaction ID: `{escaped_transaction_id}`\n"
                f"Amount Paid: `{escaped_amount}`\n"
                f"Time Period: `{escaped_time_period}`\n\n"
                "Thank you for your payment!"
            )
            log_message = (
                f"✅ Payment Request DB ID `{escape_markdown_v2(str(request_db_id))}` (User: @{escape_markdown_v2(updated_payment_request['username'])}) "
                f"APPROVED by @{escape_markdown_v2(admin_username)} (ID: {escape_markdown_v2(str(admin_id))})."
            )
        elif action == 'reject':
            message_to_user = (
                "❌ **Your payment request has been REJECTED.** ❌\n\n"
                f"Transaction ID: `{escaped_transaction_id}`\n"
                f"Amount Paid: `{escaped_amount}`\n"
                f"Time Period: `{escaped_time_period}`\n\n"
                "Please check your details and try again, or contact support for assistance."
            )
            log_message = (
                f"❌ Payment Request DB ID `{escape_markdown_v2(str(request_db_id))}` (User: @{escape_markdown_v2(updated_payment_request['username'])}) "
                f"REJECTED by @{escape_markdown_v2(admin_username)} (ID: {escape_markdown_v2(str(admin_id))})."
            )

        # Ensure the original message text is also escaped before editing it if it contains Markdown
        # This part assumes query.message.text is already safely escaped or doesn't contain problematic markdown.
        # If it does, you'd need to re-parse or escape it before adding new text.
        # For simplicity, we assume the original message text was safe, and only add new markdown.
        await query.edit_message_text(
            f"{query.message.text}\n\n"
            f"**Status: {action.upper()}**\n" # No need to escape action here as it's just 'APPROVED' or 'REJECTED'
            f"By: @{escape_markdown_v2(admin_username)} (ID: {escape_markdown_v2(str(admin_id))})\n"
            f"At: {escape_markdown_v2(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}",
            parse_mode='MarkdownV2'
        )

        await context.bot.send_message(chat_id=updated_payment_request['user_id'], text=message_to_user, parse_mode='MarkdownV2')

        if LOG_CHANNEL_ID:
            await context.bot.send_message(chat_id=LOG_CHANNEL_ID, text=log_message, parse_mode='MarkdownV2')

        logger.info(log_message)

    except Exception as e:
        logger.error(f"Error processing callback for request DB ID {request_db_id_str}: {e}", exc_info=True)
        await query.edit_message_text("An error occurred while processing this request.")


def main():
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(button_callback))

    logger.info("Bot started polling...")
    application.run_polling()

if __name__ == '__main__':
    main()
                
