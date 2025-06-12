import logging import re import os from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters from pymongo import MongoClient from datetime import datetime from bson.objectid import ObjectId

--- Configuration ---

TOKEN = os.getenv('TELEGRAM_BOT_TOKEN') ADMIN_CHAT_ID = int(os.getenv('TELEGRAM_ADMIN_CHAT_ID')) LOG_CHANNEL_ID = int(os.getenv('TELEGRAM_LOG_CHANNEL_ID')) MONGO_URI = os.getenv('MONGO_DB_CONNECTION_URI') DB_NAME = os.getenv('MONGO_DB_NAME', 'telegram_payments_bot')

if not TOKEN: raise ValueError("TELEGRAM_BOT_TOKEN environment variable is not set!") if not ADMIN_CHAT_ID: raise ValueError("TELEGRAM_ADMIN_CHAT_ID environment variable is not set!") if not MONGO_URI: raise ValueError("MONGO_DB_CONNECTION_URI environment variable is not set!")

--- MongoDB Setup ---

client = MongoClient(MONGO_URI) db = client[DB_NAME] payment_requests_collection = db['payment_requests']

--- Logging Setup ---

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO) logger = logging.getLogger(name)

--- Helper Function for MarkdownV2 Escaping ---

def escape_markdown_v2(text: str) -> str: escape_chars = '_*~`>#+-=|{}.!' return ''.join('\' + char if char in escape_chars else char for char in text)

--- Bot Commands ---

async def start(update: Update, context): await update.message.reply_text( "👋 Hi there! I'm your payment confirmation bot.\n\n" "Please send your payment details in the following format:\n\n" "✅ I have successfully completed the payment.\n\n" "📱 Telegram Username: @YourUsername\n" "💳 Transaction ID: YourTransactionID\n" "💰 Amount Paid: ₹X\n" "⏳ Time Period: Y Days\n\n" "📸 You can also send the payment screenshot. Please reply to my confirmation message with the screenshot.\n\n" "🙏 Thank you!" )

async def handle_message(update: Update, context): if update.message.photo: photo = update.message.photo[-1] file_id = photo.file_id new_file = await context.bot.get_file(file_id) filename = f"/tmp/screenshot_{file_id}.jpg" await new_file.download_to_drive(filename)

user_info = escape_markdown_v2(update.effective_user.username or update.effective_user.full_name)
    user_id_escaped = escape_markdown_v2(str(update.effective_user.id))
    log_message = f"📸 Screenshot received from @{user_info} (ID: `{user_id_escaped}`)."

    if update.message.reply_to_message:
        if update.message.reply_to_message.from_user.id == context.bot.id:
            match = re.search(r"Request DB ID: `(?P<request_db_id>[a-f0-9]{24})`", update.message.reply_to_message.text)
            if match:
                request_db_id = escape_markdown_v2(match.group('request_db_id'))
                log_message = f"📸 Screenshot received for Payment Request DB ID `{request_db_id}` from @{user_info} (ID: `{user_id_escaped}`)."

    if LOG_CHANNEL_ID:
        try:
            with open(filename, 'rb') as photo_file:
                await context.bot.send_photo(chat_id=LOG_CHANNEL_ID, photo=photo_file, caption=log_message, parse_mode='MarkdownV2')
        except Exception as e:
            logger.error(f"Error forwarding screenshot to log channel: {e}")
            await update.message.reply_text("❌ There was an issue forwarding your screenshot to the admin. Please try again.")
        finally:
            if os.path.exists(filename):
                os.remove(filename)
    else:
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
        if username.lower() != data['telegram_username'].lower():
            await update.message.reply_text(
                f"⚠️ The Telegram Username in your message (@{data['telegram_username']}) does not match your current username ({username})."
            )
            return

        try:
            new_request_doc = {
                'user_id': user_id,
                'username': username,
                'transaction_id': data['transaction_id'],
                'amount': data['amount'],
                'time_period': data['time_period'],
                'status': 'pending',
                'admin_notes': None,
                'created_at': datetime.now(),
                'approved_rejected_at': None
            }
            result = payment_requests_collection.insert_one(new_request_doc)
            request_db_id = str(result.inserted_id)

            keyboard = [[
                InlineKeyboardButton("✅ Approve", callback_data=f"approve_{request_db_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"reject_{request_db_id}")
            ]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            escaped_data = {k: escape_markdown_v2(v) for k, v in data.items()}
            escaped_username = escape_markdown_v2(username)
            escaped_user_id = escape_markdown_v2(str(user_id))
            escaped_request_db_id = escape_markdown_v2(request_db_id)
            escaped_text = escape_markdown_v2(text)

            admin_message = (
                f"🚨 *New Payment Request* 🚨\n\n"
                f"User: @{escaped_username} (ID: `{escaped_user_id}`)\n"
                f"Transaction ID: `{escaped_data['transaction_id']}`\n"
                f"Amount: `{escaped_data['amount']}`\n"
                f"Time Period: `{escaped_data['time_period']}`\n"
                f"Request DB ID: `{escaped_request_db_id}`\n\n"
                f"Original Message:\n```

{escaped_text}

)

                await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=admin_message, reply_markup=reply_markup, parse_mode='MarkdownV2')
                await update.message.reply_text("✅ Your payment request has been received and sent to the admin. Please reply with a screenshot if available.")

            except Exception as e:
                logger.error(f"Error processing request: {e}", exc_info=True)
                await update.message.reply_text("❌ An error occurred while processing your request. Please try again later.")
        else:
            await update.message.reply_text("Invalid format. Please follow the instructions. Type /start for help.")

async def button_callback(update: Update, context):
    query = update.callback_query
    await query.answer()

    try:
        action, request_db_id_str = query.data.split('_')
        request_db_id = ObjectId(request_db_id_str)
        admin_id = query.from_user.id
        admin_username = query.from_user.username or query.from_user.full_name

        payment_request = payment_requests_collection.find_one({'_id': request_db_id})
        if not payment_request:
            await query.edit_message_text("Payment request not found.")
            return

        if payment_request.get('status') != 'pending':
            await query.edit_message_text(f"Request already {payment_request.get('status')} by {payment_request.get('admin_notes', 'another admin')}.")
            return

        update_fields = {
            'status': action,
            'admin_notes': f"{action.capitalize()} by @{admin_username} (ID: {admin_id})",
            'approved_rejected_at': datetime.now()
        }
        payment_requests_collection.update_one({'_id': request_db_id}, {'$set': update_fields})

        escaped_info = {
            'transaction_id': escape_markdown_v2(payment_request['transaction_id']),
            'amount': escape_markdown_v2(payment_request['amount']),
            'time_period': escape_markdown_v2(payment_request['time_period']),
            'username': escape_markdown_v2(payment_request['username']),
            'request_id': escape_markdown_v2(str(request_db_id)),
            'admin_username': escape_markdown_v2(admin_username),
            'admin_id': escape_markdown_v2(str(admin_id))
        }

        if action == 'approve':
            message = (
                "🎉 *Payment Approved!* 🎉\n\n"
                f"Transaction ID: `{escaped_info['transaction_id']}`\n"
                f"Amount: `{escaped_info['amount']}`\n"
                f"Time Period: `{escaped_info['time_period']}`\n"
                "Thank you for your payment!"
            )
        else:
            message = (
                "❌ *Payment Rejected* ❌\n\n"
                f"Transaction ID: `{escaped_info['transaction_id']}`\n"
                f"Amount: `{escaped_info['amount']}`\n"
                f"Time Period: `{escaped_info['time_period']}`\n"
                "Please review your details and try again."
            )

        await context.bot.send_message(chat_id=payment_request['user_id'], text=message, parse_mode='MarkdownV2')
        await query.edit_message_text(f"Request {action}ed successfully.")

    except Exception as e:
        logger.error(f"Error in button_callback: {e}", exc_info=True)
        await query.edit_message_text("❌ Error processing your request. Please try again.")

# --- Main Execution ---
async def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, handle_message))
    app.add_handler(CallbackQueryHandler(button_callback))

    await app.run_polling()

if __name__ == '__main__':
    import asyncio
    asyncio.run(main())

