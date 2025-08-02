import os
import socket
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import logging
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from telegram import Update, InlineQueryResultArticle, InputTextMessageContent
from telegram.ext import Application, CommandHandler, InlineQueryHandler, ContextTypes
from services.search_service import JobSearchService
from settings import Settings
from logger import Logger

logger = Logger.get_logger(__name__, file_prefix='server')

# Add these lines to suppress initialization logs
logging.getLogger('services.hh_location_service').setLevel(logging.WARNING)
logging.getLogger('job_sites.hh').setLevel(logging.WARNING)
logging.getLogger('job_sites.geekjob').setLevel(logging.WARNING)

# Load environment variables
load_dotenv()

app = Flask(__name__)
executor = ThreadPoolExecutor(max_workers=4)
search_service = JobSearchService()

# Initialize Telegram bot
try:
    telegram_app = Application.builder().token(os.getenv('TELEGRAM_TOKEN')).build()
    telegram_loop = asyncio.new_event_loop()
except Exception as e:
    print(f"Failed to initialize Telegram bot: {e}")
    telegram_app = None
    telegram_loop = None

def search_jobs(keyword):
    """Search jobs using JobSearchService."""
    try:
        sites = request.args.get('sites', ','.join(Settings.DEFAULT_SITE_CHOICES)).split(',')
        sites = [site.strip().lower() for site in sites if site.strip().lower() in Settings.AVAILABLE_SITES]
        if not sites:
            logger.warning("No valid sites specified in request, using default sites")
            sites = Settings.DEFAULT_SITE_CHOICES

        results = search_service.search_all_sites(keyword, None, sites)
        formatted_results = {
            "global_time_ms": results.get('global_time', 0),
            "results": {
                site_name: {
                    "jobs": result.get('jobs', []),
                    "timing_ms": result.get('timing', 0)
                } for site_name, result in results.items() if site_name != 'global_time' and isinstance(result, dict)
            }
        }
        logger.info(f"Search request completed for keyword: {keyword}, sites: {sites}, found {sum(len(r['jobs']) for r in formatted_results['results'].values())} jobs")
        return formatted_results
    except Exception as e:
        logger.error(f"Error in search_jobs for keyword: {keyword}: {e}")
        return {"error": str(e)}

def search_jobs_for_inline(keyword, sites=None):
    """Search jobs for inline queries - simplified version without Flask request context."""
    try:
        if sites is None:
            sites = Settings.DEFAULT_SITE_CHOICES
        
        results = search_service.search_all_sites(keyword, None, sites)
        formatted_results = {
            "global_time_ms": results.get('global_time', 0),
            "results": {
                site_name: {
                    "jobs": result.get('jobs', []),
                    "timing_ms": result.get('timing', 0)
                } for site_name, result in results.items() if site_name != 'global_time' and isinstance(result, dict)
            }
        }
        logger.info(f"Inline search completed for keyword: {keyword}, found {sum(len(r['jobs']) for r in formatted_results['results'].values())} jobs")
        return formatted_results
    except Exception as e:
        logger.error(f"Error in search_jobs_for_inline for keyword: {keyword}: {e}")
        return {"error": str(e)}

# Webhook route
@app.route('/webhook', methods=['POST'])
def telegram_webhook():
    if not telegram_app:
        logger.error("Webhook request failed: Telegram bot not initialized")
        return jsonify({'status': 'error', 'message': 'Telegram bot not initialized'}), 500

    try:
        update = Update.de_json(request.get_json(force=True), telegram_app.bot)
        user_id = update.effective_user.id if update.effective_user else 'unknown'

        # Single consolidated log message
        logger.info(
            f"Webhook processed | "
            f"User: {user_id} | "
            f"Type: {update.update_id} | "
            f"Content: {update.to_dict().get('message', {}).get('text', 'no-text')}"
        )

        future = executor.submit(
            lambda: asyncio.run_coroutine_threadsafe(
                telegram_app.process_update(update),
                telegram_loop
            ).result()
        )
        return jsonify({'status': 'success'}), 200
    except Exception as e:
        logger.error(f"Webhook error (User: {user_id}): {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

# Command handler
async def handle_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        keyword = ' '.join(context.args)
        if not keyword:
            await update.message.reply_text("Usage: /search <job_keyword>")
            logger.warning(f"User {update.effective_user.id} sent /search without keyword")
            return

        logger.debug(f"Processing /search command for user {update.effective_user.id}, keyword: {keyword}")
        results = await asyncio.get_event_loop().run_in_executor(
            executor,
            lambda: search_jobs(keyword)
        )

        if "error" in results:
            await update.message.reply_text(f"🚨 Error: {results['error']}")
            logger.error(f"Search command failed for user {update.effective_user.id}, keyword: {keyword}: {results['error']}")
            return

        response = ["🔍 Search Results:"]
        for site, data in results.get('results', {}).items():
            response.append(f"\n🏢 {Settings.get_site_name(site)} ({data['timing_ms']:.0f} ms):")
            for idx, job in enumerate(data.get('jobs', [])[:3], 1):
                response.append(f"{idx}. {job}")
            response.append("")

        message = '\n'.join(response) if len(response) > 1 else "No jobs found"
        await update.message.reply_text(message, disable_web_page_preview=True)
        logger.info(f"Displayed {sum(len(r['jobs']) for r in results.get('results', {}).values())} jobs for user {update.effective_user.id}, keyword: {keyword}")
    except Exception as e:
        logger.error(f"Search command error for user {update.effective_user.id}: {e}")
        await update.message.reply_text(f"🚨 Error: {str(e)}")

# Inline query handler
async def handle_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline queries for job searching."""
    try:
        query = update.inline_query.query.strip()
        user_id = update.effective_user.id if update.effective_user else 'unknown'
        
        # If query is empty, show usage instructions
        if not query:
            results = [
                InlineQueryResultArticle(
                    id="usage",
                    title="💼 IT Jobs Finder Bot",
                    description="Type a job keyword to search for jobs (e.g., 'python developer')",
                    input_message_content=InputTextMessageContent(
                        message_text="Usage: @itjobsfinder_bot <job_keyword>\nExample: @itjobsfinder_bot python developer"
                    )
                )
            ]
            await update.inline_query.answer(results, cache_time=300)
            return
        
        # Show loading result first
        loading_results = [
            InlineQueryResultArticle(
                id="loading",
                title=f"🔍 Searching for '{query}'...",
                description="Please wait while we search job sites",
                input_message_content=InputTextMessageContent(
                    message_text=f"🔍 Searching for jobs: {query}..."
                )
            )
        ]
        await update.inline_query.answer(loading_results, cache_time=1)
        
        logger.debug(f"Processing inline query for user {user_id}, keyword: {query}")
        
        # Perform job search
        search_results = await asyncio.get_event_loop().run_in_executor(
            executor,
            lambda: search_jobs_for_inline(query)
        )
        
        if "error" in search_results:
            error_results = [
                InlineQueryResultArticle(
                    id="error",
                    title="❌ Search Error",
                    description=f"Error: {search_results['error']}",
                    input_message_content=InputTextMessageContent(
                        message_text=f"🚨 Search Error for '{query}': {search_results['error']}"
                    )
                )
            ]
            await update.inline_query.answer(error_results, cache_time=60)
            logger.error(f"Inline search error for user {user_id}, keyword: {query}: {search_results['error']}")
            return
        
        # Format results for inline display
        inline_results = []
        total_jobs = sum(len(r['jobs']) for r in search_results.get('results', {}).values())
        
        if total_jobs == 0:
            inline_results.append(
                InlineQueryResultArticle(
                    id="no_results",
                    title="😔 No Jobs Found",
                    description=f"No jobs found for '{query}'",
                    input_message_content=InputTextMessageContent(
                        message_text=f"😔 No jobs found for: {query}\n\nTry different keywords or check back later."
                    )
                )
            )
        else:
            # Create summary result
            summary_text = [f"🔍 Job Search Results for: {query}\n"]
            for site, data in search_results.get('results', {}).items():
                jobs_count = len(data.get('jobs', []))
                if jobs_count > 0:
                    summary_text.append(f"🏢 {Settings.get_site_name(site)}: {jobs_count} jobs ({data['timing_ms']:.0f}ms)")
                    for idx, job in enumerate(data.get('jobs', [])[:5], 1):  # Show up to 5 jobs per site
                        summary_text.append(f"  {idx}. {job}")
                    summary_text.append("")
            
            inline_results.append(
                InlineQueryResultArticle(
                    id="summary",
                    title=f"💼 {total_jobs} Jobs Found",
                    description=f"Found {total_jobs} jobs for '{query}'",
                    input_message_content=InputTextMessageContent(
                        message_text='\n'.join(summary_text),
                        disable_web_page_preview=True
                    )
                )
            )
            
            # Create individual results for each site with jobs
            for site, data in search_results.get('results', {}).items():
                jobs = data.get('jobs', [])
                if jobs:
                    site_text = [f"🏢 {Settings.get_site_name(site)} - {query}\n"]
                    for idx, job in enumerate(jobs[:10], 1):  # Show up to 10 jobs
                        site_text.append(f"{idx}. {job}")
                    
                    inline_results.append(
                        InlineQueryResultArticle(
                            id=f"site_{site}",
                            title=f"🏢 {Settings.get_site_name(site)} ({len(jobs)} jobs)",
                            description=f"{len(jobs)} jobs found on {Settings.get_site_name(site)}",
                            input_message_content=InputTextMessageContent(
                                message_text='\n'.join(site_text),
                                disable_web_page_preview=True
                            )
                        )
                    )
        
        # Answer the inline query
        await update.inline_query.answer(
            inline_results[:50],  # Telegram limit is 50 results
            cache_time=300  # Cache for 5 minutes
        )
        
        logger.info(f"Inline query answered for user {user_id}, keyword: {query}, found {total_jobs} jobs")
        
    except Exception as e:
        logger.error(f"Inline query error for user {user_id}: {e}")
        error_results = [
            InlineQueryResultArticle(
                id="error_general",
                title="❌ Error",
                description="An error occurred while searching",
                input_message_content=InputTextMessageContent(
                    message_text=f"🚨 An error occurred while searching: {str(e)}"
                )
            )
        ]
        await update.inline_query.answer(error_results, cache_time=60)

# Register handlers
if telegram_app:
    telegram_app.add_handler(CommandHandler("search", handle_search))
    telegram_app.add_handler(InlineQueryHandler(handle_inline_query))