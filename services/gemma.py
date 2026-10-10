import base64
import json
import logging
import mimetypes
import os
import re
from pathlib import Path
from typing import Dict, List, Any, Optional
import requests
from config import Config

logger = logging.getLogger(__name__)

VALID_ACTIONS = {
    "URGENT_ASSESSMENT",
    "PROFESSIONAL_INSPECTION",
    "REPAIR_RECOMMENDED",
    "NO_OBVIOUS_WARNING_SIGNS",
}

FIXED_SYSTEM_INSTRUCTION = """You are an AI assistant helping identify visible signs of potential structural vulnerability in buildings and retaining walls.

Examine the provided image(s) for visible cracks, unusual deformation, exposed reinforcement, water damage, separation, or other observable signs that may warrant professional inspection.

When asked whether a structure or wall is safe, do not certify it as safe or structurally sound. A photograph alone cannot establish structural integrity or seismic safety. Explain the limitations and recommend professional assessment when appropriate.

Never claim that damage is absent merely because it is not visible in the submitted photograph. Do not invent measurements, causes, or structural conclusions.

Return your response strictly as a valid JSON object."""

FIXED_ANALYSIS_PROMPT = """[INTERNAL SYSTEM INSTRUCTION]
Analyze the provided image(s) for visible structural damage in building walls, retaining walls, or foundations in urban hillside settlements.

You MUST respond strictly with a valid JSON object matching this exact schema:
{
  "summary": "Short concise description of visible damage and structural elements observed",
  "safety_score": <integer from 1 to 10, where 1 means the most concerning visible indicators and 10 means no obvious warning signs are visible>,
  "recommended_action": "<exactly one of: 'URGENT_ASSESSMENT', 'PROFESSIONAL_INSPECTION', 'REPAIR_RECOMMENDED', 'NO_OBVIOUS_WARNING_SIGNS'>",
  "evidence": "Brief visible observations supporting the assessment (e.g. diagonal stepped cracking across mortar joints, out-of-plane bulging, or clean intact surface)"
}

Action criteria:
- 'URGENT_ASSESSMENT' (Scores 1-3): Signs suggesting an immediate threat (imminent collapse, severe out-of-plane tilting/bowing of retaining wall, major diagonal shear fracturing across load-bearing masonry).
- 'PROFESSIONAL_INSPECTION' (Scores 4-6): Significant cracking (>3mm or stepped shear), structural retaining wall surcharge pressure, efflorescence with mortar decay, or high diagnostic uncertainty.
- 'REPAIR_RECOMMENDED' (Scores 7-8): Superficial hairline plaster cracks (<1-2mm) or cosmetic weathering without load-bearing unit displacement.
- 'NO_OBVIOUS_WARNING_SIGNS' (Scores 9-10): No concerning damage or fissures visible in the submitted photograph(s). (Does NOT certify structural or seismic safety).
"""


def encode_image_to_base64(file_path: Path) -> tuple[str, str]:
    """Read an image file and return its base64 string and mime type."""
    mime_type, _ = mimetypes.guess_type(str(file_path))
    if not mime_type:
        mime_type = "image/jpeg"

    with open(file_path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("utf-8")
    return encoded, mime_type


def sanitize_safety_score(score_val: Any) -> int:
    """Clamp safety score to integer between 1 and 10."""
    try:
        score = int(round(float(score_val)))
        return max(1, min(10, score))
    except (ValueError, TypeError):
        return 5


def sanitize_recommended_action(action_val: Any, safety_score: int) -> str:
    """Normalize recommended action to one of the 4 exact predefined actions."""
    if isinstance(action_val, str):
        act_upper = action_val.strip().upper().replace(" ", "_")
        if act_upper in VALID_ACTIONS:
            return act_upper
        if "URGENT" in act_upper or "EVACUAT" in act_upper:
            return "URGENT_ASSESSMENT"
        if "INSPECT" in act_upper:
            return "PROFESSIONAL_INSPECTION"
        if "REPAIR" in act_upper or "MAINTEN" in act_upper:
            return "REPAIR_RECOMMENDED"
        if "NO_OBVIOUS" in act_upper or "SAFE" in act_upper or "NONE" in act_upper:
            return "NO_OBVIOUS_WARNING_SIGNS"

    # Default based on score if missing or invalid
    if safety_score <= 3:
        return "URGENT_ASSESSMENT"
    elif safety_score <= 6:
        return "PROFESSIONAL_INSPECTION"
    elif safety_score <= 8:
        return "REPAIR_RECOMMENDED"
    return "NO_OBVIOUS_WARNING_SIGNS"


def parse_model_json_response(raw_text: str) -> Dict[str, Any]:
    """Extract and validate structured JSON from model output."""
    cleaned = raw_text.strip()
    
    # Strip markdown code fences if wrapped in ```json ... ```
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
    if fence_match:
        cleaned = fence_match.group(1).strip()
    else:
        # Match outermost braces
        brace_match = re.search(r"(\{.*\})", cleaned, re.DOTALL)
        if brace_match:
            cleaned = brace_match.group(1).strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as err:
        logger.warning(f"Failed to parse model output as JSON: {err}. Raw: {raw_text[:200]}")
        # Robust fallback extraction from text
        score = 5
        score_match = re.search(r"safety_score[\"':\s]+(\d+)", raw_text, re.IGNORECASE)
        if score_match:
            score = sanitize_safety_score(score_match.group(1))

        action = sanitize_recommended_action(raw_text, score)

        return {
            "summary": raw_text[:300].strip(),
            "safety_score": score,
            "recommended_action": action,
            "evidence": "Visible surface observations extracted from screening response.",
            "raw_text": raw_text,
        }

    score = sanitize_safety_score(data.get("safety_score", 5))
    action = sanitize_recommended_action(data.get("recommended_action"), score)
    summary = str(data.get("summary") or "Visual structural screening completed.").strip()
    evidence = str(data.get("evidence") or "Surface inspection conducted on submitted images.").strip()

    return {
        "summary": summary,
        "safety_score": score,
        "recommended_action": action,
        "evidence": evidence,
        "raw_text": raw_text,
    }


def call_google_genai(image_paths: List[Path]) -> Dict[str, Any]:
    """Call Google AI Studio / Gemini endpoint with Gemma vision model."""
    api_key = Config.GEMMA_API_KEY
    if not api_key:
        raise ValueError("GEMMA_API_KEY is not configured.")

    model_name = Config.GEMMA_MODEL_NAME
    url_template = Config.GEMMA_API_URL
    url = url_template.format(model=model_name)
    if "key=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}key={api_key}"

    parts: List[Dict[str, Any]] = []

    for path in image_paths:
        b64_data, mime_type = encode_image_to_base64(path)
        parts.append({
            "inlineData": {
                "mimeType": mime_type,
                "data": b64_data,
            }
        })

    parts.append({"text": FIXED_ANALYSIS_PROMPT})

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": parts,
            }
        ],
        "systemInstruction": {
            "parts": [
                {"text": FIXED_SYSTEM_INSTRUCTION}
            ]
        },
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
            "maxOutputTokens": 1000,
        }
    }

    response = requests.post(
        url,
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=45,
    )

    if response.status_code != 200:
        raise RuntimeError(f"Google GenAI API returned HTTP {response.status_code}: {response.text}")

    data = response.json()
    candidates = data.get("candidates", [])
    if not candidates:
        raise RuntimeError(f"No candidate output received from model: {data}")

    text_parts = []
    content = candidates[0].get("content", {})
    for part in content.get("parts", []):
        if "text" in part:
            text_parts.append(part["text"])

    raw_text = "\n".join(text_parts).strip()
    parsed = parse_model_json_response(raw_text)
    parsed["model_name"] = model_name
    parsed["provider_used"] = "google_genai"
    return parsed


def call_openai_compatible(image_paths: List[Path]) -> Dict[str, Any]:
    """Call an OpenAI-compatible vision endpoint (e.g. Ollama, vLLM, OpenRouter)."""
    api_url = Config.GEMMA_API_URL
    model_name = Config.GEMMA_MODEL_NAME
    api_key = Config.GEMMA_API_KEY or "none"

    content_list: List[Dict[str, Any]] = []
    content_list.append({"type": "text", "text": FIXED_ANALYSIS_PROMPT})

    for path in image_paths:
        b64_data, mime_type = encode_image_to_base64(path)
        content_list.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:{mime_type};base64,{b64_data}"
            }
        })

    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": FIXED_SYSTEM_INSTRUCTION},
            {"role": "user", "content": content_list},
        ],
        "temperature": 0.1,
        "max_tokens": 1000,
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    response = requests.post(
        api_url,
        json=payload,
        headers=headers,
        timeout=60,
    )

    if response.status_code != 200:
        raise RuntimeError(f"OpenAI-compatible API returned HTTP {response.status_code}: {response.text}")

    data = response.json()
    raw_text = data["choices"][0]["message"]["content"]
    parsed = parse_model_json_response(raw_text)
    parsed["model_name"] = model_name
    parsed["provider_used"] = "openai_compatible"
    return parsed


def run_offline_structural_screening(image_paths: List[Path]) -> Dict[str, Any]:
    """
    Local structural screening fallback returning the structured Phase 2 JSON schema.
    Used for offline demonstration or when the API key is not configured.
    """
    num_images = len(image_paths)

    summary = (
        f"Visual screening of {num_images} photograph(s) indicates visible diagonal stepped fissures "
        "traversing masonry joints on a retaining wall structure, with surface efflorescence near the base."
    )
    score = 4
    action = "PROFESSIONAL_INSPECTION"
    evidence = (
        "Stepped crack traversing block joints and signs of uphill lateral soil pressure. "
        "Photographic inspection cannot determine subsurface foundation footing integrity."
    )

    return {
        "summary": summary,
        "safety_score": score,
        "recommended_action": action,
        "evidence": evidence,
        "raw_text": json.dumps({
            "summary": summary,
            "safety_score": score,
            "recommended_action": action,
            "evidence": evidence
        }),
        "model_name": f"{Config.GEMMA_MODEL_NAME} (Offline Screener)",
        "provider_used": "offline_simulation",
    }


def analyze_structural_report(
    image_paths: List[Path],
    force_offline: bool = False,
) -> Dict[str, Any]:
    """Main entry point for Gemma Phase 2 structural screening."""
    provider = Config.GEMMA_PROVIDER
    api_key = Config.GEMMA_API_KEY
    api_url = Config.GEMMA_API_URL

    logger.info(
        f"Analyzing {len(image_paths)} images with Gemma Vision (Phase 2 JSON). "
        f"Provider: {provider}, Model: {Config.GEMMA_MODEL_NAME}, Force Offline: {force_offline}"
    )

    for p in image_paths:
        if not p.is_file():
            raise FileNotFoundError(f"Image file not found: {p}")

    if force_offline or os.getenv("GEMMA_TEST_MODE") == "true":
        return run_offline_structural_screening(image_paths)

    has_live_credentials = bool(api_key or ("localhost" in api_url or "127.0.0.1" in api_url))

    if has_live_credentials:
        try:
            if provider == "openai_compatible" or "/v1/" in api_url:
                return call_openai_compatible(image_paths)
            else:
                return call_google_genai(image_paths)
        except Exception as e:
            logger.warning(f"Live Gemma API call failed: {e}")
            if Config.GEMMA_MOCK_FALLBACK:
                logger.info("Falling back to offline structural screening due to GEMMA_MOCK_FALLBACK.")
                return run_offline_structural_screening(image_paths)
            raise

    if Config.GEMMA_MOCK_FALLBACK:
        return run_offline_structural_screening(image_paths)

    raise ValueError(
        "No GEMMA_API_KEY configured and GEMMA_MOCK_FALLBACK is disabled. "
        "Please provide a valid API key or endpoint in your environment."
    )
