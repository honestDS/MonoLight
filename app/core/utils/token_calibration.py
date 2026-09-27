from typing import Any

INPUT_TOKEN_ESTIMATE_RAW_KEY = "input_token_estimate_raw"
INPUT_TOKEN_CALIBRATION_ESTIMATED_TOTAL_KEY = "input_token_calibration_estimated_total"
INPUT_TOKEN_CALIBRATION_PROVIDER_TOTAL_KEY = "input_token_calibration_provider_total"
INPUT_TOKEN_CALIBRATION_SAMPLES_KEY = "input_token_calibration_samples"

_MIN_CALIBRATION_FACTOR = 0.25
_MAX_CALIBRATION_FACTOR = 4.0


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _same_scope(metadata: Any, *, model_id: str, protocol: str) -> bool:
    return (
        isinstance(metadata, dict)
        and metadata.get("model_id") == model_id
        and metadata.get("protocol") == protocol
    )


def extract_token_calibration_factor(
    metadata: Any,
    *,
    model_id: str,
    protocol: str,
) -> float:
    if not _same_scope(metadata, model_id=model_id, protocol=protocol):
        return 1.0

    estimated_total = metadata.get(INPUT_TOKEN_CALIBRATION_ESTIMATED_TOTAL_KEY)
    provider_total = metadata.get(INPUT_TOKEN_CALIBRATION_PROVIDER_TOTAL_KEY)
    samples = metadata.get(INPUT_TOKEN_CALIBRATION_SAMPLES_KEY)
    if not (
        _is_positive_int(estimated_total)
        and _is_positive_int(provider_total)
        and _is_positive_int(samples)
    ):
        return 1.0

    factor = provider_total / estimated_total
    return min(_MAX_CALIBRATION_FACTOR, max(_MIN_CALIBRATION_FACTOR, factor))


def apply_token_calibration(raw_estimated_tokens: int, factor: float) -> int:
    if not _is_positive_int(raw_estimated_tokens):
        return max(int(raw_estimated_tokens), 0) if isinstance(raw_estimated_tokens, int) and not isinstance(raw_estimated_tokens, bool) else 0
    normalized_factor = factor if isinstance(factor, (int, float)) and not isinstance(factor, bool) and factor > 0 else 1.0
    normalized_factor = min(_MAX_CALIBRATION_FACTOR, max(_MIN_CALIBRATION_FACTOR, float(normalized_factor)))
    return max(1, round(raw_estimated_tokens * normalized_factor))


def build_token_calibration_metadata(
    previous_metadata: Any,
    *,
    model_id: str,
    protocol: str,
    raw_estimated_tokens: Any,
    provider_input_tokens: Any,
) -> dict[str, int]:
    if not (_is_positive_int(raw_estimated_tokens) and _is_positive_int(provider_input_tokens)):
        return {}

    estimated_total = 0
    provider_total = 0
    samples = 0
    if _same_scope(previous_metadata, model_id=model_id, protocol=protocol):
        previous_estimated_total = previous_metadata.get(INPUT_TOKEN_CALIBRATION_ESTIMATED_TOTAL_KEY)
        previous_provider_total = previous_metadata.get(INPUT_TOKEN_CALIBRATION_PROVIDER_TOTAL_KEY)
        previous_samples = previous_metadata.get(INPUT_TOKEN_CALIBRATION_SAMPLES_KEY)
        if (
            _is_positive_int(previous_estimated_total)
            and _is_positive_int(previous_provider_total)
            and _is_positive_int(previous_samples)
        ):
            estimated_total = previous_estimated_total
            provider_total = previous_provider_total
            samples = previous_samples

    return {
        INPUT_TOKEN_ESTIMATE_RAW_KEY: raw_estimated_tokens,
        INPUT_TOKEN_CALIBRATION_ESTIMATED_TOTAL_KEY: estimated_total + raw_estimated_tokens,
        INPUT_TOKEN_CALIBRATION_PROVIDER_TOTAL_KEY: provider_total + provider_input_tokens,
        INPUT_TOKEN_CALIBRATION_SAMPLES_KEY: samples + 1,
    }

