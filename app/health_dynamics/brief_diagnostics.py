from __future__ import annotations


def classify_brief_diagnostic(
    *,
    generation_status: str,
    model_name: str | None,
    error_message: str | None = None,
    parse_failed: bool = False,
    stored_code: str | None = None,
    stored_message: str | None = None,
) -> tuple[str | None, str | None]:
    if stored_message:
        return stored_code, stored_message

    if generation_status == "generated":
        return None, None

    if parse_failed:
        return (
            "llm_response_parse_failed",
            "The local LLM responded, but the brief could not be parsed into the required sections. A deterministic fallback summary was used.",
        )

    if model_name and model_name.endswith("(fallback)"):
        return (
            "llm_validation_failed",
            "The local LLM responded, but the brief failed validation checks. A deterministic fallback summary was used.",
        )

    if not error_message:
        return (
            "llm_fallback_used",
            "The local LLM path was not accepted, so a deterministic fallback summary was used.",
        )

    lowered = error_message.lower()
    if "timed out" in lowered:
        return (
            "llm_timeout",
            "The local LLM request timed out, so a deterministic fallback summary was used.",
        )
    if "could not connect" in lowered or "endpoint was not found" in lowered:
        return (
            "llm_connection_failed",
            "The app could not connect to the local LLM endpoint, so a deterministic fallback summary was used.",
        )
    if "configured model" in lowered and "unavailable" in lowered:
        return (
            "llm_model_unavailable",
            "The configured local LLM model was unavailable, so a deterministic fallback summary was used.",
        )
    if "authentication failed" in lowered:
        return (
            "llm_auth_failed",
            "Authentication failed for the local LLM endpoint, so a deterministic fallback summary was used.",
        )
    if "response format was not recognized" in lowered or "instruction-like response" in lowered:
        return (
            "llm_invalid_response",
            "The local LLM returned an invalid response, so a deterministic fallback summary was used.",
        )
    if "server error" in lowered:
        return (
            "llm_server_error",
            "The local LLM endpoint returned a server error, so a deterministic fallback summary was used.",
        )

    return (
        "llm_unexpected_error",
        "The local LLM returned an unexpected error, so a deterministic fallback summary was used.",
    )
