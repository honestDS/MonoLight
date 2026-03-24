# System Prompts and Message Templates

# Tool execution confirmation prefix
CONFIRMATION_PREFIX = "FORCE_EXECUTE_CONFIRMED_"

# System notice when maximum turns reached
PROMPT_MAX_TURNS_REACHED = (
    "System notification: Maximum tool call turns ({max_turns}) reached. "
    "Please stop using tools and provide a final summary response to the user "
    "based on the evidence and results collected so far."
)

# LLM Error messages
