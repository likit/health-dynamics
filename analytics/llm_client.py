from __future__ import annotations

import json
import logging
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib import error, request

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    load_dotenv = None

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

if load_dotenv is not None:
    load_dotenv(BASE_DIR / ".env")

from config import Config
from app.health_dynamics.insight_rules import build_fallback_executive_brief, validate_generated_summary

logger = logging.getLogger(__name__)


class LocalLLMError(RuntimeError):
    pass


@dataclass
class LocalLLMClient:
    base_url: str
    model: str
    api_key: str | None = None
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")

    @property
    def chat_completions_url(self) -> str:
        return f"{self.base_url}/chat/completions"

    def chat(self, system_prompt: str, user_prompt: str) -> str:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        payload = {
            "model": self.model,
            "messages": messages,
        }
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        http_request = request.Request(
            self.chat_completions_url,
            data=body,
            headers=headers,
            method="POST",
        )

        try:
            with request.urlopen(http_request, timeout=self.timeout_seconds) as response:
                response_body = response.read().decode("utf-8")
                logger.info("Local LLM call succeeded for model=%s", self.model)
        except error.HTTPError as exc:
            self._raise_http_error(exc)
        except error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, socket.timeout):
                raise LocalLLMError(
                    "The local LLM request timed out. Confirm Ollama is running and the model can respond in time."
                ) from exc
            raise LocalLLMError(
                "Could not connect to the local LLM endpoint. Confirm Ollama is running and LOCAL_LLM_BASE_URL is correct."
            ) from exc
        except TimeoutError as exc:
            raise LocalLLMError(
                "The local LLM request timed out. Confirm Ollama is running and the model can respond in time."
            ) from exc

        try:
            data = json.loads(response_body)
        except json.JSONDecodeError as exc:
            logger.exception("Local LLM response JSON parsing failed for model=%s", self.model)
            raise LocalLLMError("LLM response format was not recognized.") from exc

        assistant_response = self._extract_assistant_response(data)
        logger.info(
            "Local LLM response parsed for model=%s with response_length=%s",
            self.model,
            len(assistant_response),
        )
        return assistant_response

    def _extract_assistant_response(self, response_json: dict) -> str:
        try:
            assistant_response = response_json["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            logger.exception("Local LLM response missing choices[0].message.content for model=%s", self.model)
            raise LocalLLMError("LLM response format was not recognized.") from exc

        if not isinstance(assistant_response, str):
            logger.error("Local LLM response content was not a string for model=%s", self.model)
            raise LocalLLMError("LLM response format was not recognized.")

        normalized_response = assistant_response.strip()
        if not normalized_response:
            logger.error("Local LLM response was empty for model=%s", self.model)
            raise LocalLLMError("LLM response format was not recognized.")

        if self._looks_like_instruction_response(normalized_response):
            logger.error("Local LLM returned instruction-like content for model=%s", self.model)
            raise LocalLLMError("LLM returned an invalid instruction-like response. Please retry.")

        return normalized_response

    def _looks_like_instruction_response(self, assistant_response: str) -> bool:
        lowered = assistant_response.lower()
        return assistant_response.startswith("You are Health Dynamics") or (
            "answer only from the aggregated analytics context" in lowered
        )

    def _raise_http_error(self, exc: error.HTTPError) -> None:
        details = ""
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            details = payload.get("error", {}).get("message", "")
        except Exception:
            details = ""

        if exc.code in {404, 400} and self.model in details:
            raise LocalLLMError(
                f"The configured model '{self.model}' is unavailable. Pull it with `ollama pull {self.model}` and try again."
            ) from exc
        if exc.code == 404:
            raise LocalLLMError(
                "The local LLM endpoint was not found. Confirm LOCAL_LLM_BASE_URL points to Ollama's OpenAI-compatible API."
            ) from exc
        if exc.code == 401:
            raise LocalLLMError(
                "Authentication failed for the local LLM endpoint. Check LOCAL_LLM_API_KEY or leave it blank if Ollama does not require it."
            ) from exc
        if exc.code >= 500:
            raise LocalLLMError(
                "Ollama returned a server error while processing the request. Confirm the model is loaded and try again."
            ) from exc

        message = details or f"HTTP {exc.code}"
        raise LocalLLMError(f"Local LLM request failed: {message}") from exc


def build_client(
    *,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
) -> LocalLLMClient:
    return LocalLLMClient(
        base_url=base_url or Config.LOCAL_LLM_BASE_URL,
        model=model or Config.LOCAL_LLM_MODEL,
        api_key=api_key if api_key is not None else (Config.LOCAL_LLM_API_KEY or None),
    )


def generate_dashboard_summary(
    verified_insight_payload: dict,
    *,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
) -> tuple[str, str]:
    client = build_client(base_url=base_url, model=model, api_key=api_key)
    verified_insights = verified_insight_payload.get("verified_insights", [])
    base_system_prompt = (
        "You are a trusted analyst briefing executives, HR leaders, health program managers, university administrators, "
        "and other non-technical decision makers. "
        "Use only the verified insights provided. Do not invent causes. Do not provide diagnosis or treatment advice. "
        "Write like a population health analyst preparing a one-minute briefing for leadership, not like a dashboard or statistical report. "
        "Use plain language. Keep the summary concise. "
        "Do not sound like a research paper or technical data report. "
        "Do not use technical phrases such as verified paired-year movement, denominator, prevalence, trajectory classification, or population mix change. "
        "Use 'persistent' for persistent abnormal patterns. "
        "Use 'deteriorating' only for actual trajectory movement. "
        "When describing persistent patterns, say 'remained in an abnormal category'. "
        "When describing deterioration, say 'moved toward a less favorable category'. "
        "When describing improvement, say 'moved toward a more favorable category'. "
        "Mention denominator caveats when they are provided. "
        "Do not use raw dashboard tables unless they are already represented in the verified insights. "
        "Do not simply list percentages without explaining what they mean for leaders. "
        "Use numbers as supporting evidence, not as the main story. "
        "Explain three things clearly: what the main workforce health finding is, why leadership should care, and what should be investigated next. "
        "Keep the total length between 120 and 220 words. "
        "Use this structure exactly: "
        "Headline: one sentence stating the most important workforce health finding. Avoid generic wording. "
        "Executive Summary: one to two short paragraphs. Lead with the main finding. Explain what the percentages mean in organizational terms. Explain whether the issue appears persistent, improving, or deteriorating. Do not simply repeat percentages without interpretation. "
        "Why It Matters: one short paragraph in organizational language for executives and HR leaders. "
        "Suggested Next Exploration: 2 to 4 bullet points. "
        "Do not say 'What Changed' when the main pattern is persistent rather than a true change. "
        "Do not show raw field names such as age_group. Use human-readable labels such as Age Group. "
        "Convert drill-down ideas into investigation questions, not dimension labels."
    )
    user_prompt = (
        "Write an executive brief from these verified insight packets.\n\n"
        f"Verified insight packets:\n{json.dumps(verified_insight_payload, indent=2)}"
    )

    summary = client.chat(system_prompt=base_system_prompt, user_prompt=user_prompt)
    is_valid, issues = validate_generated_summary(summary, verified_insights)
    if is_valid:
        return summary, client.model

    logger.warning(
        "Executive brief validation failed for model=%s with issues=%s",
        client.model,
        issues,
    )

    retry_prompt = (
        base_system_prompt
        + " Do not use any forbidden terms from the verified insights. "
        + "If an insight describes a persistent pattern, do not describe it as worsening, deteriorating, or increased."
    )
    retry_summary = client.chat(system_prompt=retry_prompt, user_prompt=user_prompt)
    retry_valid, retry_issues = validate_generated_summary(retry_summary, verified_insights)
    if retry_valid:
        return retry_summary, client.model

    logger.warning(
        "Executive brief retry validation failed for model=%s with issues=%s. Using deterministic fallback.",
        client.model,
        retry_issues,
    )
    return build_fallback_executive_brief(verified_insights), f"{client.model} (fallback)"


def generate_exploration_brief(
    exploration_packet: dict,
    *,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
) -> tuple[str, str]:
    client = build_client(base_url=base_url, model=model, api_key=api_key)
    system_prompt = (
        "You are a population health analyst preparing a short executive briefing for non-technical leaders. "
        "Use only the exploration insight packet provided. "
        "Do not invent causes. Do not provide diagnosis or treatment advice. "
        "Use plain language and explain what stands out, what pattern explains it, and what should be investigated next. "
        "Do not mention raw field names, paired_n, trajectory classification, or internal schema terms. "
        "Use this exact structure: "
        "Key Finding: one to two sentences. "
        "Interpretation: one short paragraph. "
        "Suggested Next Investigation: one short paragraph. "
        "Keep the total length between 90 and 160 words."
    )
    user_prompt = (
        "Write an exploration brief from this packet.\n\n"
        f"Exploration packet:\n{json.dumps(exploration_packet, indent=2)}"
    )
    summary = client.chat(system_prompt=system_prompt, user_prompt=user_prompt)

    if "age_group" in summary or "paired_n" in summary or "trajectory classification" in summary.lower():
        raise LocalLLMError("LLM returned an invalid instruction-like response. Please retry.")

    return summary, client.model


def answer_dashboard_question(
    question: str,
    aggregated_context: dict,
    *,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
) -> tuple[str, str]:
    client = build_client(base_url=base_url, model=model, api_key=api_key)
    answer = client.chat(
        system_prompt=(
            "You are Health Dynamics, a trusted analyst briefing executives, HR leaders, health program managers, "
            "university administrators, and other non-technical decision makers. "
            "Answer only from the aggregated analytics context provided. "
            "If the answer is not available, say the dashboard does not contain enough information. "
            "Do not provide diagnosis or treatment advice. "
            "Use plain language. Avoid epidemiology jargon unless absolutely necessary. Avoid technical statistical terminology. "
            "Explain findings in terms of what changed, why it matters, and what should be explored next. "
            "Use this structure unless the question clearly calls for a different short format: "
            "Headline: one sentence describing the main finding. "
            "What Changed: one short paragraph. "
            "Why It Matters: one short paragraph. "
            "Suggested Next Exploration: 2 to 4 bullet points. "
            "Avoid words such as prevalence, longitudinal burden, trajectory heterogeneity, cohort instability, and risk stratification. "
            "Prefer words such as increased, decreased, remained high, improving, worsening, and requires attention. "
            "Do not mention individuals. "
            "Do not mix cross-sectional abnormal percentages with trajectory movement. "
            "Use 'percentage points' for differences between percentages. "
            "Use 'improving' or 'worsening' only for trajectory movement, not for static abnormal percentages. "
            "Mention denominator differences when they are relevant in the provided context. "
            "Do not generate SQL, database queries, or instructions for querying the database. "
            "Do not invent exact numbers that are not present in the context. "
            "Keep the answer concise and directly responsive to the question. "
            "Maximum length is 150 words unless the user explicitly asks for more detail."
        ),
        user_prompt=(
            f"User question:\n{question}\n\n"
            "Aggregated analytics context:\n"
            f"{json.dumps(aggregated_context, indent=2)}"
        ),
    )
    return answer, client.model


def main() -> int:
    client = build_client()
    try:
        response = client.chat(
            system_prompt=(
                "You are a concise assistant summarizing a population health dashboard for internal review."
            ),
            user_prompt="Summarize this health dashboard in one paragraph.",
        )
    except LocalLLMError as exc:
        print(f"Connection test failed: {exc}")
        return 1

    print(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
