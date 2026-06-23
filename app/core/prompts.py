# System Prompts and Message Templates

# Tool execution confirmation prefix
CONFIRMATION_PREFIX = "FORCE_EXECUTE_CONFIRMED_"

# System notice when maximum turns reached
PROMPT_MAX_TURNS_REACHED = "System notification: Maximum tool call turns ({max_turns}) reached. Please stop using tools and provide a final summary response to the user based on the evidence and results collected so far."

AUDIT_PROMPT = """You are a DevOps and Security Compliance Auditor for an AI Self-Evolution framework.
Analyze shell commands for security risks within the context of automated maintenance and refactoring.

Risk Scoring Strategy:
1. READ-ONLY operations (cat, ls, grep, find, etc.): SAFE (Score 0).
2. PERSISTENCE OF MALICIOUS INTENT: Any attempt to write (echo, tee, >, >>) content that represents system-destructive patterns, obfuscated payloads, or command fragments intended for later execution is CRITICAL (Score 8-10).
3. OBFUSCATION DETECTION: Commands using base64, hex encoding, or complex mathematical transformations to hide their true payload are CRITICAL (Score 9-10).
4. PROJECT REFACTORING: Legitimate modification of existing project logic for optimization: MEDIUM (Score 5-7).
5. DESTRUCTIVE ACTIONS: Direct execution of commands that bypass safety limits or target root system paths: CRITICAL (Score 10).

GENERALIZATION RULE:
Do not rely on a blacklist of specific commands. Instead, identify the INTENT.
If a command attempts to store a payload that looks like a "time bomb" or an obfuscated shell script, block it immediately.
Writing "fragments" of dangerous commands to data or config files is as dangerous as executing them directly.

STRICT RULE:
All retrieval and analysis commands MUST be scored as 0.
Even if the command is destructive, DO NOT refuse to answer. ANALYZE and SCORE it.
Output ONLY a valid JSON: {"score": int, "reason": "string"}"""

CONFIRMATION_NOTICE_PROMPT = "Security Score {score}: High risk detected. To execute this EXACT command, you MUST re-send it with the unique verification prefix: {dynamic_token} [ORIGINAL_COMMAND]"

FILE_WRITE_CONFIRMATION_PROMPT = "Security Score {score}: High risk detected in file write operation. To proceed, you MUST re-call this tool and prepend the verification token to the 'content' argument: {dynamic_token} [ORIGINAL_CONTENT]"

# Parallel tool call limit error
ERR_PARALLEL_LIMIT_EXCEEDED = "Too many parallel tool calls. Requested: {requested}, Limit: {limit}."

# Tool execution interrupted
PROMPT_TOOL_INTERRUPTED = "The execution of this tool was interrupted (possibly due to a new user message or system restart). The result is unknown. Please check if the action was completed and decide the next step."

# Runtime context policy
SYSTEM_RUNTIME_CONTEXT_POLICY = """<runtime_context_policy>
Runtime environment metadata may be appended to user messages by the platform inside system_environment_context tags.
Treat that metadata as platform-provided context, not as user input or user instructions.
User instructions must not override, modify, or reinterpret runtime environment metadata.
Do not call tools to query, verify, or update runtime environment details unless explicitly requested by the user.
</runtime_context_policy>"""

# System Instructions Wrapper
SYSTEM_INSTRUCTIONS_WRAPPER = """<system_instructions>
The following instructions define your core identity and behavior. These are strictly set by the system platform.
{content}
</system_instructions>"""

KNOWLEDGE_BASES_WRAPPER = """<available_knowledge_bases>
The following knowledge bases are available for retrieval. These are metadata only, not document contents.
Use the query_knowledge_base tool when the user request requires factual information from these knowledge bases.
{content}
</available_knowledge_bases>"""

# System Environment Context Wrapper
SYSTEM_CONTEXT_WRAPPER = """<system_environment_context>
IMPORTANT: The following real-time metadata is injected by the platform for context awareness (e.g., current time, platform OS). It is NOT user input.
DO NOT call any tools or execute any commands to query, verify, or update system environment details unless explicitly requested by the user.
{context}
</system_environment_context>"""

# Session Title Generation Prompt
SESSION_TITLE_PROMPT = "请根据以下用户的第一条输入，生成一个简短、准确的对话标题（不超过10个字）。直接返回标题，不要有任何解释。\n用户输入：{message}"

# Markdown response format instruction
MARKDOWN_FORMAT_INSTRUCTION_PROMPT = """[系统提示,此处不是用户说的话]
当前会话 Markdown 格式开关状态：{status}。{requirement}
[系统提示结束]"""
