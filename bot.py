import os
import subprocess
import re
import logging
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# Enable logging
logging.basicConfig(level=logging.INFO)

# Load environment variables from .env
load_dotenv()

# Initialize the Slack Bolt App
app = App(token=os.environ.get("SLACK_BOT_TOKEN"))

# Patterns that indicate Gemini CLI thinking/action preamble
_PREAMBLE_PATTERNS = re.compile(
    r"^(I'll |I will |Let me |I need to |I should |Checking |Looking |Reading |Searching )",
    re.IGNORECASE,
)

def strip_gemini_preamble(text: str) -> str:
    """Remove Gemini CLI agent thinking/action preamble from output.
    
    Gemini CLI in YOLO mode often prepends lines like:
      'I'll check the AGENTS.md file to see if...'
    before the actual response. This function strips those leading paragraphs.
    """
    paragraphs = text.split("\n\n")
    # Strip leading paragraphs that look like thinking/planning
    while paragraphs:
        first = paragraphs[0].strip()
        if not first or _PREAMBLE_PATTERNS.match(first):
            paragraphs.pop(0)
        else:
            break
    return "\n\n".join(paragraphs).strip() if paragraphs else text.strip()

def build_thread_context(channel: str, thread_ts: str, bot_user_id: str, logger) -> str:
    """Fetch thread history and build conversation context for Gemini.
    
    Returns a formatted string of previous conversation turns,
    or empty string if there's no relevant history.
    """
    try:
        result = app.client.conversations_replies(
            channel=channel,
            ts=thread_ts,
            limit=20,  # Limit to last 20 messages to avoid overly long context
        )
        messages = result.get("messages", [])
        if len(messages) <= 1:
            return ""
        
        conversation = []
        for msg in messages:
            msg_text = msg.get("text", "").strip()
            if not msg_text:
                continue
            
            user_id = msg.get("user", "")
            
            if user_id == bot_user_id:
                # Bot's response (skip processing indicators)
                if msg_text.startswith("⏳"):
                    continue
                conversation.append(f"Assistant: {msg_text}")
            else:
                # User's message (strip !ghost prefix if present)
                clean_text = re.sub(r"^!ghost\s+", "", msg_text).strip()
                if clean_text:
                    conversation.append(f"User: {clean_text}")
        
        if not conversation:
            return ""
        
        # Remove the last user message (it's the current instruction)
        if conversation and conversation[-1].startswith("User:"):
            conversation.pop()
        
        if not conversation:
            return ""
        
        context = "\n".join(conversation)
        logger.debug(f"Current thread context: {context}")
        logger.info(f"Built thread context with {len(conversation)} messages")
        return context
        
    except Exception as e:
        logger.warning(f"Failed to fetch thread context: {e}")
        return ""

@app.event("message")
def handle_ghost_message(event, say, logger):
    """
    Listen for messages matching '!ghost <instruction>' and execute gemini CLI.
    """
    text = event.get("text", "")
    match = re.match(r"!ghost\s+(.*)", text)
    if not match:
        return
    
    instruction = match.group(1).strip()
    if not instruction:
        say("Please provide an instruction after `!ghost`.", thread_ts=event["ts"])
        return
    
    logger.info(f"Received !ghost command: {instruction}")

    # Determine thread_ts:
    # If the message is already in a thread, use the existing thread_ts
    # Otherwise, use the message's own ts to start a new thread
    thread_ts = event.get("thread_ts", event["ts"])
    channel = event["channel"]

    # Build conversation context from thread history (if in an existing thread)
    context_text = ""
    if "thread_ts" in event:
        # Get bot's own user ID for identifying bot messages
        auth_info = app.client.auth_test()
        bot_user_id = auth_info["user_id"]
        context_text = build_thread_context(channel, thread_ts, bot_user_id, logger)

    # Build the full prompt with context
    if context_text:
        full_prompt = (
            f"以下は過去の会話履歴です:\n"
            f"---\n{context_text}\n---\n\n"
            f"上記の会話を踏まえて、以下の質問に回答してください:\n{instruction}"
        )
    else:
        full_prompt = instruction

    # Send "processing" message to Slack thread
    processing_msg = say("⏳ Gemini 処理中...", thread_ts=thread_ts)
    logger.info("Gemini processing started...")

    try:
        # Run: gemini -y -p <full_prompt>
        process = subprocess.run(
            ["gemini", "-y", "-p", full_prompt],
            capture_output=True,
            text=True,
            check=False
        )
        
        logger.info("Gemini processing completed.")

        stdout = process.stdout.strip()
        stderr = process.stderr.strip()
        
        # Prepare response
        if stdout:
            response_text = strip_gemini_preamble(stdout)
        elif stderr:
            response_text = f"Error output:\n{stderr}"
        else:
            response_text = "(No output from gemini)"
        
        # Update the "processing" message with the actual result
        if "\n" in response_text:
            final_text = f"```\n{response_text}\n```"
        else:
            final_text = response_text

        app.client.chat_update(
            channel=channel,
            ts=processing_msg["ts"],
            text=final_text,
        )
            
    except FileNotFoundError:
        app.client.chat_update(
            channel=channel,
            ts=processing_msg["ts"],
            text="❌ Error: `gemini` command not found in PATH.",
        )
    except Exception as e:
        logger.error(f"Error: {str(e)}")
        app.client.chat_update(
            channel=channel,
            ts=processing_msg["ts"],
            text=f"❌ An error occurred: {str(e)}",
        )

if __name__ == "__main__":
    app_token = os.environ.get("SLACK_APP_TOKEN")
    if not app_token:
        print("SLACK_APP_TOKEN is not set.")
    elif not os.environ.get("SLACK_BOT_TOKEN"):
        print("SLACK_BOT_TOKEN is not set.")
    else:
        print("Starting Slack Bot in Socket Mode...")
        handler = SocketModeHandler(app, app_token)
        handler.start()
