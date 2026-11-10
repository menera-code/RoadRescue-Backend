"""
RoadRescue incident classifier.

Uses Google's pretrained Gemini 2.5 Flash model for:
- Incident classification
- Severity estimation
- Vehicle identification from incident photographs
- Vehicle mentions in English, Tagalog, and Taglish reports

No local YOLO model or model training is required.
"""

import json
import math
import os
import re
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    Request,
    build_opener,
)

# ---------------------------------------------------------------------------
# CONSTANTS
# ---------------------------------------------------------------------------

ROAD_INCIDENT_TYPES = [
    "flat_tire",
    "battery",
    "fuel",
    "stalled_vehicle",
    "minor_collision",
    "major_collision",
    "vehicle_fire",
    "road_hazard",
    "other",
]

SEVERITY_LEVELS = ["low", "medium", "high", "critical"]

VEHICLE_TYPES = {
    "car",
    "truck",
    "bus",
    "motorcycle",
    "bicycle",
    "jeepney",
    "tricycle",
    "van",
    "pickup",
    "suv",
}

TYPE_LABELS = {
    "flat_tire": "flat tire, punctured tire, or blown tire",
    "battery": "dead battery or vehicle will not start",
    "fuel": "out of fuel or ran out of gasoline",
    "stalled_vehicle": "stalled vehicle, breakdown, or engine failure",
    "minor_collision": "minor crash or low-speed collision",
    "major_collision": "major crash, serious collision, or multiple vehicles",
    "vehicle_fire": "vehicle fire or burning vehicle",
    "road_hazard": "pothole, debris, fallen tree, flooding, or road obstruction",
    "other": "incident that does not fit the other categories",
}

# Limit image processing to control API usage and memory on Render.
MAX_IMAGES = 2
MAX_IMAGE_BYTES = 4 * 1024 * 1024
IMAGE_TIMEOUT_SECONDS = 12

# Only accept Firebase / Google Cloud Storage download hosts.
ALLOWED_IMAGE_HOSTS = {
    "firebasestorage.googleapis.com",
    "storage.googleapis.com",
}

ALLOWED_IMAGE_MIME_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
}


# ---------------------------------------------------------------------------
# FIREBASE STORAGE IMAGE DOWNLOAD
# ---------------------------------------------------------------------------

def _validate_image_url(url: str) -> None:
    """Reject non-HTTPS URLs and hosts outside Firebase / GCS."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("Image URL is empty")

    parsed = urlsplit(url.strip())

    if parsed.scheme != "https":
        raise ValueError("Only HTTPS image URLs are accepted")

    if parsed.hostname not in ALLOWED_IMAGE_HOSTS:
        raise ValueError("Image URL is not from an approved Firebase Storage host")

    if parsed.username or parsed.password:
        raise ValueError("Image URL credentials are not allowed")


class _SafeRedirectHandler(HTTPRedirectHandler):
    """Validate redirect destinations before following them."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_image_url(newurl)
        return super().redirect_request(
            req, fp, code, msg, headers, newurl
        )


def _download_firebase_image(url: str):
    """
    Download one Firebase Storage image into memory.

    Returns (image_bytes, mime_type).
    Does not save images to Render's filesystem.
    """
    _validate_image_url(url)

    request = Request(
        url,
        headers={
            "User-Agent": "RoadRescue-Backend/1.0",
            "Accept": "image/jpeg,image/png,image/webp,image/gif",
        },
    )

    opener = build_opener(_SafeRedirectHandler())

    with opener.open(request, timeout=IMAGE_TIMEOUT_SECONDS) as response:
        _validate_image_url(response.geturl())

        content_type = (
            response.headers.get("Content-Type", "")
            .split(";")[0]
            .strip()
            .lower()
        )

        if content_type not in ALLOWED_IMAGE_MIME_TYPES:
            raise ValueError(f"Unsupported image type: {content_type or 'unknown'}")

        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > MAX_IMAGE_BYTES:
            raise ValueError("Image exceeds the 4 MB limit")

        image_bytes = response.read(MAX_IMAGE_BYTES + 1)

    if not image_bytes:
        raise ValueError("Downloaded image is empty")

    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise ValueError("Image exceeds the 4 MB limit")

    return image_bytes, content_type


def _load_images(image_urls: Optional[List[str]]):
    """
    Download at most MAX_IMAGES images.

    Failed downloads are recorded without exposing signed URLs.
    """
    image_parts = []
    errors = []

    for index, url in enumerate((image_urls or [])[:MAX_IMAGES]):
        try:
            image_bytes, mime_type = _download_firebase_image(url)

            # Gemini SDK accepts inline image data in this format.
            image_parts.append({
                "mime_type": mime_type,
                "data": image_bytes,
            })

        except Exception as exc:
            print(
                f"[image] Could not load image {index + 1}: "
                f"{type(exc).__name__}: {str(exc)[:160]}"
            )
            errors.append({
                "image_index": index,
                "error": str(exc)[:160],
            })

    return image_parts, errors


# ---------------------------------------------------------------------------
# JSON AND VALUE VALIDATION
# ---------------------------------------------------------------------------

def _parse_json_from_llm(raw: str) -> Dict[str, Any]:
    """Extract JSON from a Gemini response."""
    cleaned = re.sub(
        r"^```(?:json)?\s*",
        "",
        (raw or "").strip(),
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        data = json.loads(match.group(0))
        if isinstance(data, dict):
            return data

    raise ValueError("Gemini did not return valid JSON")


def _safe_confidence(value: Any, default: float = 0.5) -> float:
    """Keep model-reported confidence within the 0–1 range."""
    try:
        number = float(value)
        if not math.isfinite(number):
            return default
        return round(max(0.0, min(1.0, number)), 3)
    except (TypeError, ValueError):
        return default


def _normalize_vehicle(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None

    vehicle = value.strip().lower()

    aliases = {
        "motorbike": "motorcycle",
        "motorbike/scooter": "motorcycle",
        "bike": "bicycle",
        "jeep": "jeepney",
        "trike": "tricycle",
        "lorry": "truck",
    }

    vehicle = aliases.get(vehicle, vehicle)

    return vehicle if vehicle in VEHICLE_TYPES else None


def _clean_string_list(value: Any, limit: int = 5) -> List[str]:
    if not isinstance(value, list):
        return []

    cleaned = []
    for item in value:
        if isinstance(item, str) and item.strip():
            cleaned.append(item.strip().lower()[:60])

    return list(dict.fromkeys(cleaned))[:limit]


def _normalize_image_vehicles(value: Any):
    """Validate Gemini's vehicle counts and confidence values."""
    vehicles: Dict[str, int] = {}
    detections: List[Dict[str, Any]] = []

    if not isinstance(value, list):
        return vehicles, detections

    for item in value[:20]:
        if not isinstance(item, dict):
            continue

        vehicle = _normalize_vehicle(item.get("type"))
        if not vehicle:
            continue

        try:
            count = int(item.get("count", 1))
        except (TypeError, ValueError):
            count = 1

        count = max(1, min(count, 20))
        vehicles[vehicle] = vehicles.get(vehicle, 0) + count

        detections.append({
            "object": vehicle,
            "count": count,
            "confidence": _safe_confidence(item.get("confidence")),
            "source": "gemini_vision",
        })

    return vehicles, detections


# ---------------------------------------------------------------------------
# GEMINI MULTIMODAL ANALYSIS
# ---------------------------------------------------------------------------

def classify_with_gemini(
    text: str,
    image_parts: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Analyze report text and optional photographs with Gemini 2.5 Flash.

    This is inference using Google's pretrained model. RoadRescue does
    not train or fine-tune the model.
    """
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured")

    import google.generativeai as genai

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-2.5-flash")

    type_list = "\n".join(
        f'- "{key}": {label}'
        for key, label in TYPE_LABELS.items()
    )

    report_text = (text or "").strip() or "[No written description provided]"

    prompt = f"""
You are the incident-analysis assistant for RoadRescue,
a Philippine road-safety and roadside-assistance application.

Analyze the citizen report and any attached incident photographs.

REPORT:
{report_text}

Choose exactly ONE incident type:
{type_list}

Choose exactly ONE severity:
- "low": minor inconvenience with no apparent serious hazard
- "medium": vehicle breakdown or assistance needed, with no reported serious injury
- "high": serious collision, significant road obstruction, or credible indication of injury
- "critical": explicit life-threatening emergency, serious reported injuries,
  or clearly visible vehicle fire

IMPORTANT SAFETY RULES:
1. Do not invent injuries, casualties, or facts not present in the report.
2. Do not claim that a person is injured just because they are lying down
   or appear near a vehicle in a photograph.
3. Use the written report as the primary evidence for injury severity.
4. Photographs may help identify visible vehicles and visible hazards.
5. If evidence is insufficient, choose "medium" and set
   "needs_human_review" to true.
6. Do not guess a vehicle type if it is not clearly visible.
7. Do not return bounding boxes or claim exact object-detection coordinates.
8. A vehicle count is an estimate, not a guaranteed exact count.
9. The confidence value is your own estimate, not a statistically
   calibrated probability.

Extract:
- keywords: up to 5 important report keywords
- mentioned_vehicles: vehicle types explicitly mentioned in the text
- image_vehicles: vehicle types clearly visible in the photographs,
  with estimated count and confidence
- visual_observations: up to 5 short observations supported by the images
- needs_human_review: true if evidence is uncertain or severity needs verification

Use these vehicle labels where applicable:
car, truck, bus, motorcycle, bicycle, jeepney, tricycle, van, pickup, suv.

Return ONLY valid JSON in this structure:
{{
  "type": "major_collision",
  "severity": "high",
  "confidence": 0.80,
  "keywords": ["collision", "motorcycle"],
  "mentioned_vehicles": ["motorcycle"],
  "image_vehicles": [
    {{"type": "motorcycle", "count": 1, "confidence": 0.85}}
  ],
  "visual_observations": ["A motorcycle is visible near the roadside."],
  "needs_human_review": true
}}
"""

    contents = [prompt] + (image_parts or [])

    response = model.generate_content(
        contents,
        generation_config={
            "temperature": 0.1,
            "max_output_tokens": 1200,
            "response_mime_type": "application/json",
        },
    )

    raw = (response.text or "").strip()
    if not raw:
        raise ValueError("Gemini returned an empty response")

    data = _parse_json_from_llm(raw)

    incident_type = data.get("type", "other")
    if incident_type not in ROAD_INCIDENT_TYPES:
        incident_type = "other"

    severity = data.get("severity", "medium")
    if severity not in SEVERITY_LEVELS:
        severity = "medium"

    mentioned_vehicles = []
    for vehicle in data.get("mentioned_vehicles", []):
        normalized = _normalize_vehicle(vehicle)
        if normalized and normalized not in mentioned_vehicles:
            mentioned_vehicles.append(normalized)

    image_vehicles, detections = _normalize_image_vehicles(
        data.get("image_vehicles")
    )

    observations = _clean_string_list(
        data.get("visual_observations"), limit=5
    )

    return {
        "type": incident_type,
        "severity": severity,
        "confidence": _safe_confidence(data.get("confidence")),
        "keywords": _clean_string_list(data.get("keywords"), limit=5),
        "mentioned_vehicles": mentioned_vehicles,
        "image_vehicles": image_vehicles,
        "detections": detections,
        "visual_observations": observations,
        "needs_human_review": bool(data.get("needs_human_review", False)),
        "source": "gemini",
    }


# ---------------------------------------------------------------------------
# KEYWORD FALLBACK
# ---------------------------------------------------------------------------

def classify_text_with_keywords(text: str) -> Dict[str, Any]:
    """Offline fallback when Gemini is unavailable."""
    t = (text or "").lower()

    vehicle_map = {
        "car": ["car", "sedan", "suv"],
        "truck": ["truck", "lorry", "dump truck"],
        "motorcycle": ["motorcycle", "motorbike", "scooter", "motor"],
        "bus": ["bus", "minibus"],
        "jeepney": ["jeepney", "jeep"],
        "tricycle": ["tricycle", "trike"],
        "van": ["van"],
        "pickup": ["pickup"],
    }

    mentioned = [
        vehicle
        for vehicle, words in vehicle_map.items()
        if any(word in t for word in words)
    ]

    if any(k in t for k in ["on fire", "burning", "nasusunog", "sunog"]):
        return _keyword_result("vehicle_fire", "critical", mentioned)

    if any(k in t for k in [
        "major", "malaking", "grabe", "critical", "severe",
        "multiple vehicles", "injuries", "nasugatan",
    ]):
        return _keyword_result("major_collision", "high", mentioned)

    if any(k in t for k in [
        "collision", "crash", "accident", "bangga",
        "bumangga", "sagasa", "rear-ended",
    ]):
        return _keyword_result("minor_collision", "medium", mentioned)

    if any(k in t for k in [
        "flat", "punctured", "blown", "butas", "sabog ang gulong",
    ]):
        return _keyword_result("flat_tire", "low", mentioned)

    if any(k in t for k in [
        "battery", "baterya", "won't start", "dead battery",
        "ayaw mag-start",
    ]):
        return _keyword_result("battery", "low", mentioned)

    if any(k in t for k in ["out of fuel", "no gas", "ubos ang gas"]):
        return _keyword_result("fuel", "low", mentioned)

    if any(k in t for k in [
        "stalled", "breakdown", "broken down", "engine died",
        "tirik", "ayaw umandar",
    ]):
        return _keyword_result("stalled_vehicle", "medium", mentioned)

    if any(k in t for k in [
        "pothole", "lubak", "debris", "fallen tree",
        "flood", "baha", "obstruction", "road block",
    ]):
        return _keyword_result("road_hazard", "medium", mentioned)

    return _keyword_result("other", "medium", mentioned)


def _keyword_result(
    incident_type: str,
    severity: str,
    mentioned_vehicles: List[str],
) -> Dict[str, Any]:
    return {
        "type": incident_type,
        "severity": severity,
        "confidence": 0.4,
        "keywords": [],
        "mentioned_vehicles": mentioned_vehicles,
        "image_vehicles": {},
        "detections": [],
        "visual_observations": [],
        "needs_human_review": True,
        "source": "keyword",
    }


# ---------------------------------------------------------------------------
# TOP-LEVEL PREDICTION
# ---------------------------------------------------------------------------

def predict_incident(
    text: str,
    image_urls: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Analyze one RoadRescue incident.

    Existing response fields are retained for backend compatibility.
    Additional fields distinguish text and image analysis.
    """
    text = (text or "").strip()

    image_parts, image_errors = _load_images(image_urls)

    if not text and not image_parts:
        text_result = _keyword_result("other", "medium", [])
        text_result["confidence"] = 0.3
        text_result["source"] = "empty"
    else:
        try:
            text_result = classify_with_gemini(
                text=text,
                image_parts=image_parts,
            )
        except Exception as exc:
            print(
                f"[predict] Gemini analysis failed: "
                f"{type(exc).__name__}: {str(exc)[:200]}"
            )
            text_result = classify_text_with_keywords(text)

    image_vehicles = text_result.get("image_vehicles", {})
    mentioned_vehicles = text_result.get("mentioned_vehicles", [])

    # Keep the legacy `vehicles` field useful for existing analytics.
    vehicles = image_vehicles or {
        vehicle: 1 for vehicle in mentioned_vehicles
    }

    all_vehicles = sorted(
        set(list(image_vehicles.keys()) + list(mentioned_vehicles))
    )

    sources = []
    if text_result.get("source") == "gemini":
        sources.append("gemini")
        if image_parts:
            sources.append("gemini_vision")
    elif text_result.get("source") == "keyword":
        sources.append("keyword")
    else:
        sources.append("empty")

    image_analysis = None
    if image_parts or image_errors:
        image_analysis = {
            "vehicles": image_vehicles,
            "detections": text_result.get("detections", []),
            "observations": text_result.get("visual_observations", []),
            "images_received": len(image_urls or []),
            "images_analyzed": (
                len(image_parts)
                if text_result.get("source") == "gemini"
                else 0
            ),
            "errors": image_errors,
        }

    return {
        "predicted_type": text_result.get("type", "other"),
        "predicted_severity": text_result.get("severity", "medium"),
        "confidence": text_result.get("confidence", 0.4),
        "keywords": text_result.get("keywords", []),
        "mentioned_vehicles": mentioned_vehicles,
        "image_vehicles": image_vehicles,
        "detections": text_result.get("detections", []),
        "vehicles": vehicles,
        "all_vehicles": all_vehicles,
        "sources": sources,
        "needs_human_review": text_result.get(
            "needs_human_review", True
        ),
        "severity_basis": (
            "text_and_image" if text and image_parts
            else "text" if text
            else "image" if image_parts
            else "insufficient_evidence"
        ),
        "text_analysis": text_result,
        "image_analysis": image_analysis,
    }


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv

    load_dotenv()

    sample = (
        " ".join(sys.argv[1:])
        if len(sys.argv) > 1
        else "Nagkaroon ng banggaan sa highway, may nasugatan."
    )

    print(json.dumps(
        predict_incident(sample),
        indent=2,
        ensure_ascii=False,
    ))
