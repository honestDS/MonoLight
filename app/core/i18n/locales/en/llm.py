MESSAGES = {
    "ERR_LLM_CONNECTION_FAILED": "Failed to connect to the large model provider gateway, please check network or proxy configuration",
    "ERR_LLM_REQUEST_TOO_LARGE": (
        "This model request is {request_size_bytes} bytes (approximately {request_size_mib} MiB), exceeding the global limit of "
        "{max_size_bytes} bytes ({max_size_mib} MiB). The request was blocked locally, so this call was not sent. Please reduce the content or attachments; "
        "an administrator can adjust the global request size limit after verifying the upstream limit."
    ),
    "ERR_LLM_UPSTREAM_REQUEST_TOO_LARGE": (
        "The upstream service rejected this request as too large (HTTP 413). Please reduce the content or attachments, "
        "and contact an administrator to lower the global request size limit appropriately based on the upstream limit so similar requests can be intercepted earlier. "
        "Lowering the limit does not automatically shrink the request."
    ),
    "ERR_LLM_API_RESPONSE_ERROR": "Large model API returned an abnormal response",
    "ERR_LLM_UNEXPECTED_ERROR": "Unexpected exception occurred when calling the large model interface",
    "ERR_LLM_UNEXPECTED_ERROR_WITH_DETAIL": "Unexpected exception occurred when calling the large model interface: {detail}",
    "ERR_LLM_API_RESPONSE_ERROR_WITH_STATUS": "Large model API returned an abnormal response [Status: {status}]: {detail}",
    "ERR_LLM_CONTEXT_LENGTH_CONFIG_MISMATCH": "The model provider reported that the context length was exceeded. Check whether this channel's configured context window matches the model's actual capability.",
    "ERR_LLM_CHANNEL_NOT_CONFIGURED": "No valid model channel config or API Key detected. Check that a Profile with valid keys is set as default or selected for the current session or message platform.",
    "ERR_LLM_EMPTY_RESPONSE": "Large model returned an empty response, please try sending the instruction again or check the model configuration",
    "ERR_LLM_FINAL_REPLY_TOOL_CORRECTION_FAILED": "This model still couldn't produce the required final reply after 3 tries. It may not be a good fit for this task. Please try another model.",
    "ERR_LLM_FIRST_CHAR_TIMEOUT": "Timeout waiting for the dialog model's first character response ({timeout} seconds)",
    "ERR_LLM_STREAM_TIMEOUT": "Dialog model stream response timed out ({timeout} seconds)",
    "ERR_API_KEY_CRYPTO_FAILED": "API Key encryption/decryption failed: {reason}",
    "ERR_API_KEY_CRYPTO_KEY_MISSING": "API key encryption/decryption failed because the system secrets file has not been initialized",
    "ERR_API_KEY_CRYPTO_KEY_INVALID": "API key encryption/decryption failed because the channel encryption key in the system secrets file is invalid",
    "ERR_API_KEY_ENCRYPT_INPUT_EMPTY": "API Key encryption failed: API Key to encrypt is empty",
    "ERR_API_KEY_DECRYPT_INPUT_EMPTY": "API Key decryption failed: API Key to decrypt is empty",
    "ERR_API_KEY_DECRYPT_FAILED": "API Key decryption failed: ciphertext is invalid or encryption key does not match",
    "ERR_API_KEY_DECRYPT_EMPTY": "API Key decryption failed: decrypted API Key is empty",
    "ERR_RERANK_FORMAT_ERROR": "Rerank interface returns abnormal format: missing results list",
    "ERR_EMBEDDING_COUNT_MISMATCH": "The number of embeddings returned by the vector model does not match the number of texts",
    "ERR_EMBEDDING_DIMENSION_MISMATCH": "The actual output dimension of the vector model is {actual}, which is inconsistent with the configured {expected}",
}
