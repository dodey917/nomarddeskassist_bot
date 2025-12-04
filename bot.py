import os
import logging
import json
import re
import base64
from datetime import datetime
from typing import Dict, List
import traceback

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext, CallbackQueryHandler
from telegram.ext import ConversationHandler

import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Conversation states
NAME, AMOUNT, DATE, CATEGORY = range(4)

class OCRProcessor:
    """Handles OCR for receipt images using Google Vision API via google-api-python-client"""
    
    def __init__(self, google_creds_json: str = None):
        self.vision_service = None
        
        # Try to initialize Google Vision API
        if google_creds_json:
            try:
                creds_dict = json.loads(google_creds_json)
                creds = Credentials.from_service_account_info(creds_dict)
                
                # Build Vision API service using google-api-python-client
                self.vision_service = build('vision', 'v1', credentials=creds)
                logger.info("✅ Google Vision API initialized via google-api-python-client")
            except Exception as e:
                logger.warning(f"Google Vision API not available: {e}")
        else:
            logger.warning("No Google credentials for Vision API")
    
    async def extract_text_from_image(self, image_bytes: bytes) -> str:
        """Extract text from image using Google Vision API"""
        if not self.vision_service:
            logger.warning("Google Vision client not available")
            return ""
        
        try:
            # Encode image bytes to base64
            image_content = base64.b64encode(image_bytes).decode('UTF-8')
            
            # Create the request
            request = {
                'requests': [{
                    'image': {'content': image_content},
                    'features': [{'type': 'TEXT_DETECTION'}]
                }]
            }
            
            # Call the Vision API
            response = self.vision_service.images().annotate(body=request).execute()
            
            # Extract text from response
            if 'responses' in response and response['responses']:
                text_annotations = response['responses'][0].get('textAnnotations', [])
                if text_annotations:
                    full_text = text_annotations[0].get('description', '')
                    logger.info(f"Extracted {len(full_text)} characters from receipt")
                    return full_text
            
            return ""
        except HttpError as e:
            logger.error(f"Google Vision API HTTP error: {e}")
            return ""
        except Exception as e:
            logger.error(f"Google Vision OCR error: {e}")
            return ""
    
    def parse_receipt_text(self, text: str) -> Dict[str, any]:
        """Parse extracted text to find receipt information"""
        parsed_info = {
            'amount': None,
            'date': None,
            'store': None
        }
        
        if not text:
            return parsed_info
        
        # Convert to lowercase for easier matching
        text_lower = text.lower()
        
        # Look for total amount (common patterns in receipts)
        amount_patterns = [
            r'total[\s:]*[\$€£]?\s*(\d+\.?\d*)',
            r'amount[\s:]*[\$€£]?\s*(\d+\.?\d*)',
            r'[\$€£]\s*(\d+\.\d+)',
            r'(\d+\.\d+)[\s]*[\$€£]',
            r'balance[\s:]*[\$€£]?\s*(\d+\.?\d*)',
            r'grand[\s]*total[\s:]*[\$€£]?\s*(\d+\.?\d*)',
            r'subtotal[\s:]*[\$€£]?\s*(\d+\.?\d*)',
            r'paid[\s:]*[\$€£]?\s*(\d+\.?\d*)'
        ]
        
        all_amounts = []
        for pattern in amount_patterns:
            matches = re.findall(pattern, text_lower)
            for match in matches:
                try:
                    amount = float(match)
                    all_amounts.append(amount)
                except ValueError:
                    continue
        
        if all_amounts:
            # Usually the largest amount is the total
            parsed_info['amount'] = max(all_amounts)
            logger.info(f"Found amount: ${parsed_info['amount']}")
        
        # Look for date patterns
        date_patterns = [
            r'(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})',  # MM/DD/YYYY or DD/MM/YYYY
            r'(\d{4}[/-]\d{1,2}[/-]\d{1,2})',    # YYYY-MM-DD
            r'(\d{1,2}\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{4})',  # 12 Dec 2024
            r'((jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}\s*,\s*\d{4})'  # Dec 12, 2024
        ]
        
        for pattern in date_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                parsed_info['date'] = match.group(1)
                logger.info(f"Found date: {parsed_info['date']}")
                break
        
        # Look for store name (often at the beginning of receipt)
        lines = text.split('\n')
        if lines:
            # Check first few lines for store name
            for line in lines[:10]:
                line = line.strip()
                if line and len(line) < 100:  # Reasonable length for store name
                    # Skip common receipt headers
                    skip_words = ['receipt', 'invoice', 'total', 'amount', 'date', 'time', 'item', 'qty']
                    if not any(skip_word in line.lower() for skip_word in skip_words):
                        parsed_info['store'] = line
                        break
        
        return parsed_info

class GoogleSheetManager:
    def __init__(self):
        logger.info("Initializing Google Sheets...")
        
        # Get credentials from environment variables
        creds_json = os.getenv('GOOGLE_CREDS_JSON')
        sheet_url = os.getenv('SHEET_URL')
        
        if not creds_json:
            raise ValueError("GOOGLE_CREDS_JSON environment variable is missing")
        
        if not sheet_url:
            raise ValueError("SHEET_URL environment variable is missing")
        
        # Ensure sheet_url is a full URL
        if not sheet_url.startswith('http'):
            # If it's just an ID, convert to full URL
            sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_url}"
        
        try:
            # Parse credentials
            creds_dict = json.loads(creds_json)
            logger.info(f"Service account: {creds_dict.get('client_email')}")
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON: {e}")
            raise
        
        SCOPES = ['https://www.googleapis.com/auth/spreadsheets',
                  'https://www.googleapis.com/auth/drive']
        
        try:
            creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
            self.client = gspread.authorize(creds)
            logger.info("✅ Google Sheets authorized")
            
            # Open the sheet
            self.sheet = self.client.open_by_url(sheet_url).sheet1
            logger.info(f"✅ Sheet opened: {self.sheet.title}")
            
        except Exception as e:
            logger.error(f"Failed to open sheet: {e}")
            raise
        
        # Initialize headers if needed
        try:
            if not self.sheet.get_all_values():
                headers = [
                    'Timestamp', 'User ID', 'Name', 'Amount', 
                    'Date', 'Category', 'Description', 'Store',
                    'Receipt Text', 'Image Available'
                ]
                self.sheet.append_row(headers)
                logger.info("📝 Initialized sheet headers")
        except Exception as e:
            logger.error(f"Failed to init headers: {e}")
    
    def add_transaction(self, data: Dict):
        """Add a new transaction to the sheet"""
        row = [
            datetime.now().isoformat(),
            data.get('user_id'),
            data.get('name'),
            data.get('amount'),
            data.get('date'),
            data.get('category'),
            data.get('description', ''),
            data.get('store', ''),
            data.get('receipt_text', '')[:500],  # Limit text length
            'Yes' if data.get('has_image') else 'No'
        ]
        self.sheet.append_row(row)
        logger.info(f"Added: {data.get('name')} - ${data.get('amount')}")
        return True
    
    def get_transactions_by_name(self, name: str) -> List[Dict]:
        """Get all transactions for a specific person"""
        all_data = self.sheet.get_all_records()
        transactions = []
        
        for row in all_data:
            if row.get('Name', '').lower() == name.lower():
                transactions.append(row)
        
        logger.info(f"Found {len(transactions)} transactions for {name}")
        return transactions
    
    def get_all_names(self) -> List[str]:
        """Get list of all unique names"""
        all_data = self.sheet.get_all_records()
        names = set()
        for row in all_data:
            name = row.get('Name', '').strip()
            if name:
                names.add(name)
        return list(names)

class ReceiptBot:
    def __init__(self, sheet_manager: GoogleSheetManager):
        self.sheet = sheet_manager
        self.ocr = OCRProcessor(os.getenv('GOOGLE_CREDS_JSON'))
        
    async def start(self, update: Update, context: CallbackContext):
        """Send welcome message"""
        user = update.effective_user
        welcome_text = f"""
👋 Hello {user.first_name}!

Welcome to Receipt Scanner Bot! 📸

I can scan receipt photos and save them to Google Sheets.

**How to use:**
1. Send me a photo of any receipt
2. I'll scan it automatically
3. Enter the person's name
4. Confirm details
5. Select category
6. ✅ Saved!

**Commands:**
/add - Add transaction manually
/search <name> - Find transactions
/list - List all people
/help - Show help

Try sending me a receipt photo now! 📸
        """
        await update.message.reply_text(welcome_text)
        return ConversationHandler.END
    
    async def handle_photo(self, update: Update, context: CallbackContext):
        """Handle receipt photo upload with OCR"""
        try:
            # Download the photo (get the largest version)
            photo_file = await update.message.photo[-1].get_file()
            photo_bytes = await photo_file.download_as_bytearray()
            
            # Store in context
            context.user_data['receipt_photo'] = photo_bytes
            context.user_data['has_image'] = True
            
            # Start OCR processing
            await update.message.reply_text("🔍 Scanning receipt...")
            
            # Extract text using Google Vision
            extracted_text = await self.ocr.extract_text_from_image(photo_bytes)
            parsed_info = self.ocr.parse_receipt_text(extracted_text)
            
            # Store extracted info
            context.user_data['receipt_text'] = extracted_text
            context.user_data['parsed_info'] = parsed_info
            
            # Create summary message
            response = "✅ Receipt scanned!\n\n"
            
            if parsed_info.get('store'):
                response += f"🏪 Store: {parsed_info['store']}\n"
            
            if parsed_info.get('amount'):
                response += f"💰 Amount detected: ${parsed_info['amount']:.2f}\n"
            else:
                response += "💰 Amount: Not detected\n"
            
            if parsed_info.get('date'):
                response += f"📅 Date detected: {parsed_info['date']}\n"
            else:
                response += "📅 Date: Not detected\n"
            
            response += "\nPlease enter the person's name for this receipt:\n"
            response += "(Or type /cancel to abort)"
            
            await update.message.reply_text(response)
            return NAME
            
        except Exception as e:
            logger.error(f"Error processing photo: {e}")
            await update.message.reply_text(
                "📸 Photo received!\n"
                "Now please enter the person's name:"
            )
            context.user_data['has_image'] = True
            return NAME
    
    async def add_receipt(self, update: Update, context: CallbackContext):
        """Start manual transaction addition"""
        await update.message.reply_text(
            "📝 Please enter the person's name for this transaction:\n"
            "(Or send a receipt photo first)\n"
            "(Type /cancel to abort)"
        )
        return NAME
    
    async def handle_name(self, update: Update, context: CallbackContext):
        """Get person's name"""
        context.user_data['name'] = update.message.text
        
        # Check if we have OCR data
        parsed_info = context.user_data.get('parsed_info', {})
        amount = parsed_info.get('amount')
        
        if amount:
            await update.message.reply_text(
                f"💰 Amount detected: ${amount:.2f}\n"
                "Press Enter to accept, or enter a different amount:"
            )
        else:
            await update.message.reply_text(
                "💰 Enter the amount (e.g., 25.50):"
            )
        
        return AMOUNT
    
    async def handle_amount(self, update: Update, context: CallbackContext):
        """Get transaction amount"""
        user_input = update.message.text.strip()
        parsed_info = context.user_data.get('parsed_info', {})
        detected_amount = parsed_info.get('amount')
        
        # If user pressed Enter and we have detected amount, use it
        if user_input == '' and detected_amount:
            amount = detected_amount
        else:
            try:
                amount = float(user_input.replace('$', '').replace(',', ''))
            except ValueError:
                await update.message.reply_text("❌ Invalid amount. Please enter a number (e.g., 25.50):")
                return AMOUNT
        
        context.user_data['amount'] = amount
        
        # Check for date from OCR
        detected_date = parsed_info.get('date')
        
        if detected_date:
            await update.message.reply_text(
                f"📅 Date detected: {detected_date}\n"
                "Press Enter to accept, or enter a different date (YYYY-MM-DD or 'today'):"
            )
        else:
            await update.message.reply_text(
                "📅 Enter the date (YYYY-MM-DD or type 'today'):"
            )
        
        return DATE
    
    async def handle_date(self, update: Update, context: CallbackContext):
        """Get transaction date"""
        user_input = update.message.text.strip()
        parsed_info = context.user_data.get('parsed_info', {})
        detected_date = parsed_info.get('date')
        
        # If user pressed Enter and we have detected date, use it
        if user_input == '' and detected_date:
            date_text = detected_date
        elif user_input.lower() == 'today':
            date_text = datetime.now().strftime('%Y-%m-%d')
        else:
            date_text = user_input
        
        context.user_data['date'] = date_text
        
        # Store store name from OCR if available
        store = parsed_info.get('store', '')
        if store:
            context.user_data['store'] = store
        
        # Show categories
        keyboard = [
            [InlineKeyboardButton("Food 🍔", callback_data="Food")],
            [InlineKeyboardButton("Transport 🚗", callback_data="Transport")],
            [InlineKeyboardButton("Shopping 🛍️", callback_data="Shopping")],
            [InlineKeyboardButton("Entertainment 🎬", callback_data="Entertainment")],
            [InlineKeyboardButton("Utilities 💡", callback_data="Utilities")],
            [InlineKeyboardButton("Medical 🏥", callback_data="Medical")],
            [InlineKeyboardButton("Other ❓", callback_data="Other")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "Select a category:",
            reply_markup=reply_markup
        )
        return CATEGORY
    
    async def handle_category(self, update: Update, context: CallbackContext):
        """Handle category selection"""
        query = update.callback_query
        await query.answer()
        
        category = query.data
        context.user_data['category'] = category
        
        # Ask for optional description
        store = context.user_data.get('store', '')
        prompt = "Enter description (optional, or type 'skip'):"
        if store:
            prompt = f"Store: {store}\n\n{prompt}"
        
        await query.edit_message_text(prompt)
        
        # We'll handle description in the next step
        context.user_data['callback_query'] = query
        return ConversationHandler.END
    
    async def handle_description(self, update: Update, context: CallbackContext):
        """Handle description input (called after category selection)"""
        if update.message:
            description = update.message.text
            if description.lower() != 'skip':
                context.user_data['description'] = description
            else:
                context.user_data['description'] = ''
            
            # Save transaction
            transaction_data = {
                'user_id': update.effective_user.id,
                'name': context.user_data.get('name'),
                'amount': context.user_data.get('amount'),
                'date': context.user_data.get('date'),
                'category': context.user_data.get('category'),
                'description': context.user_data.get('description', ''),
                'store': context.user_data.get('store', ''),
                'receipt_text': context.user_data.get('receipt_text', ''),
                'has_image': context.user_data.get('has_image', False)
            }
            
            try:
                self.sheet.add_transaction(transaction_data)
                
                # Success message
                success_msg = f"✅ Receipt saved!\n\n"
                success_msg += f"Name: {transaction_data['name']}\n"
                success_msg += f"Amount: ${transaction_data['amount']:.2f}\n"
                success_msg += f"Date: {transaction_data['date']}\n"
                success_msg += f"Category: {transaction_data['category']}\n"
                
                if transaction_data.get('store'):
                    success_msg += f"Store: {transaction_data['store']}\n"
                
                if transaction_data.get('description'):
                    success_msg += f"Description: {transaction_data['description']}\n"
                
                if transaction_data['has_image']:
                    success_msg += "📸 Receipt image processed\n"
                
                success_msg += "\nUse /search to view transactions"
                
                await update.message.reply_text(success_msg)
                
            except Exception as e:
                logger.error(f"Error saving: {e}")
                await update.message.reply_text("❌ Error saving receipt.")
            
            # Clear user data
            context.user_data.clear()
        
        return ConversationHandler.END
    
    async def search_transactions(self, update: Update, context: CallbackContext):
        """Search transactions by name"""
        if context.args:
            name = ' '.join(context.args)
            await self._show_transactions(update, name)
        else:
            await update.message.reply_text(
                "Please provide a name:\nExample: /search John Doe"
            )
    
    async def _show_transactions(self, update: Update, name: str):
        """Display transactions for a specific person"""
        try:
            transactions = self.sheet.get_transactions_by_name(name)
            
            if not transactions:
                await update.message.reply_text(f"No transactions found for {name}")
                return
            
            response = f"📊 Transactions for {name}:\n\n"
            total = 0
            
            for i, transaction in enumerate(transactions, 1):
                amount = float(transaction.get('Amount', 0))
                total += amount
                
                response += (
                    f"{i}. Date: {transaction.get('Date', 'N/A')}\n"
                    f"   Amount: ${amount:.2f}\n"
                    f"   Category: {transaction.get('Category', 'N/A')}\n"
                )
                
                store = transaction.get('Store', '')
                if store:
                    response += f"   Store: {store}\n"
                
                desc = transaction.get('Description', '')
                if desc:
                    response += f"   Note: {desc}\n"
                
                if transaction.get('Image Available') == 'Yes':
                    response += f"   📸 Has receipt image\n"
                
                response += f"   {'─' * 30}\n"
            
            response += f"\n💰 Total: ${total:.2f}"
            response += f"\n📊 Count: {len(transactions)} transactions"
            
            if len(response) > 4000:
                chunks = [response[i:i+4000] for i in range(0, len(response), 4000)]
                for chunk in chunks:
                    await update.message.reply_text(chunk)
            else:
                await update.message.reply_text(response)
                
        except Exception as e:
            logger.error(f"Error fetching: {e}")
            await update.message.reply_text("❌ Error fetching transactions.")
    
    async def list_names(self, update: Update, context: CallbackContext):
        """List all names in the database"""
        try:
            names = self.sheet.get_all_names()
            if names:
                response = "📋 People in records:\n\n"
                for i, name in enumerate(sorted(names), 1):
                    response += f"{i}. {name}\n"
                response += "\nUse /search <name> to see transactions"
            else:
                response = "No records found yet."
            
            await update.message.reply_text(response)
        except Exception as e:
            logger.error(f"Error listing: {e}")
            await update.message.reply_text("❌ Error accessing database.")
    
    async def cancel(self, update: Update, context: CallbackContext):
        """Cancel the conversation"""
        context.user_data.clear()
        await update.message.reply_text("Operation cancelled.")
        return ConversationHandler.END
    
    async def help_command(self, update: Update, context: CallbackContext):
        """Show help message"""
        help_text = """
📋 **Receipt Scanner Bot Help**

**Main Features:**
📸 Send any receipt photo - I'll scan it automatically!
📝 Or use /add for manual entry

**Commands:**
/start - Welcome message
/add - Add transaction manually
/search <name> - Find transactions by name
/list - List all people
/help - This message

**How it works:**
1. Send a receipt photo or use /add
2. I'll scan for amount, date, store
3. Enter person's name
4. Confirm/edit details
5. Select category
6. Add optional description
7. ✅ Saved to Google Sheets!

**Example:**
/search John Doe
Shows all John's receipts
        """
        await update.message.reply_text(help_text)

def main():
    """Start the bot"""
    print("🚀 Starting Receipt Scanner Bot with Google Vision OCR...")
    
    # Get token
    TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
    if not TELEGRAM_TOKEN:
        print("❌ TELEGRAM_TOKEN missing")
        return
    
    print("✅ Token found")
    
    # Initialize Google Sheets
    try:
        print("📊 Initializing Google Sheets...")
        sheet_manager = GoogleSheetManager()
        print("✅ Google Sheets ready")
    except Exception as e:
        print(f"❌ Google Sheets failed: {e}")
        return
    
    # Create bot
    bot = ReceiptBot(sheet_manager)
    
    # Create application
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Conversation handler
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('add', bot.add_receipt),
            MessageHandler(filters.PHOTO, bot.handle_photo)
        ],
        states={
            NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_name),
                CommandHandler('cancel', bot.cancel)
            ],
            AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_amount),
                CommandHandler('cancel', bot.cancel)
            ],
            DATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_date),
                CommandHandler('cancel', bot.cancel)
            ],
            CATEGORY: [
                CallbackQueryHandler(bot.handle_category),
                CommandHandler('cancel', bot.cancel)
            ]
        },
        fallbacks=[CommandHandler('cancel', bot.cancel)],
        allow_reentry=True
    )
    
    # Add handlers
    application.add_handler(CommandHandler('start', bot.start))
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler('search', bot.search_transactions))
    application.add_handler(CommandHandler('list', bot.list_names))
    application.add_handler(CommandHandler('help', bot.help_command))
    
    # Handle text messages (for description after category selection)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_description))
    
    # Start bot
    print("🤖 Bot is running with Google Vision OCR...")
    print("📱 Send /start to your bot on Telegram")
    print("📸 Try sending a receipt photo!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
