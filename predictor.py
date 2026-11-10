
"""
RoadRescue incident classifier.

Uses Google's pretrained Gemini 2.5 Flash model for:
- Incident classification
- Severity estimation
- Vehicle identification from photographs
- Incident video understanding
- English, Tagalog, and Taglish reports

No local YOLO model or model training is required.
"""

import json
import math
import os
import re
import tempfile
import time

from typing import Any, Dict, List, Optional
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

MAX_IMAGES = 2
MAX_IMAGE_BYTES = 4 * 1024 * 1024
IMAGE_TIMEOUT_SECONDS = 12

MAX_VIDEO_BYTES = 50 * 1024 * 1024
VIDEO_TIMEOUT_SECONDS = 30
VIDEO_PROCESSING_TIMEOUT_SECONDS = 120
VIDEO_POLL_INTERVAL_SECONDS = 5

ALLOWED_IMAGE_HOSTS = {
    "firebasestorage.googleapis.com",
    "storage.googleapis.com",
}

ALLOWED_VIDEO_HOSTS = {
    "firebasestorage.googleapis.com",
    "storage.googleapis.com",
}

ALLOWED_IMAGE_MIME_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
}

ALLOWED_VIDEO_MIME_TYPES = {
    "video/mp4",
    "video/quicktime",
    "video/webm",
    "video/x-msvideo",
    "video/x-flv",
    "video/mpeg",
    "video/3gpp",
}


# ---------------------------------------------------------------------------
# FIREBASE STORAGE URL VALIDATION
# ---------------------------------------------------------------------------

def _validate_storage_url(url: str, allowed_hosts: set) -> None:
    """Reject non-HTTPS URLs and hosts outside Firebase / GCS."""

    if not isinstance(url, str) or not url.strip():
        raise ValueError("Storage URL is empty")

    parsed = urlsplit(url.strip())

    if parsed.scheme != "https":
        raise ValueError("Only HTTPS Storage URLs are accepted")

    if parsed.hostname not in allowed_hosts:
        raise ValueError(
            "URL is not from an approved Firebase Storage host"
        )

    if parsed.username or parsed.password:
        raise ValueError("URL credentials are not allowed")


class _SafeImageRedirectHandler(HTTPRedirectHandler):
    """Validate image redirect destinations before following them."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_storage_url(newurl, ALLOWED_IMAGE_HOSTS)
        return super().redirect_request(
            req, fp, code, msg, headers, newurl
        )


class _SafeVideoRedirectHandler(HTTPRedirectHandler):
    """Validate video redirect destinations before following them."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_storage_url(newurl, ALLOWED_VIDEO_HOSTS)
        return super().redirect_request(
            req, fp, code, msg, headers, newurl
        )


# ---------------------------------------------------------------------------
# FIREBASE STORAGE IMAGE DOWNLOAD
# ---------------------------------------------------------------------------

def _download_firebase_image(url: str):
    """
    Download one Firebase Storage image into memory.

    Returns (image_bytes, mime_type).
    """

    _validate_storage_url(url, ALLOWED_IMAGE_HOSTS)

    request = Request(
        url,
        headers={
            "User-Agent": "RoadRescue-Backend/1.0",
            "Accept": "image/jpeg,image/png,image/webp,image/gif",
        },
    )

    opener = build_opener(_SafeImageRedirectHandler())

    with opener.open(
        request,
        timeout=IMAGE_TIMEOUT_SECONDS,
    ) as response:

        _validate_storage_url(
            response.geturl(),
            ALLOWED_IMAGE_HOSTS,
        )

        content_type = (
            response.headers.get("Content-Type", "")
            .split(";")[0]
            .strip()
            .lower()
        )

        if content_type not in ALLOWED_IMAGE_MIME_TYPES:
            raise ValueError(
                f"Unsupported image type: {content_type or 'unknown'}"
            )

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
# FIREBASE STORAGE VIDEO DOWNLOAD
# ---------------------------------------------------------------------------

def _download_firebase_video(url: str):
    """
    Download one Firebase Storage video into memory.

    Returns (video_bytes, mime_type).
    Enforces the 50 MB limit.
    """

    _validate_storage_url(url, ALLOWED_VIDEO_HOSTS)

    request = Request(
        url,
        headers={
            "User-Agent": "RoadRescue-Backend/1.0",
            "Accept": "video/mp4,video/quicktime,video/webm,*/*",
        },
    )

    opener = build_opener(_SafeVideoRedirectHandler())

    with opener.open(
        request,
        timeout=VIDEO_TIMEOUT_SECONDS,
    ) as response:

        _validate_storage_url(
            response.geturl(),
            ALLOWED_VIDEO_HOSTS,
        )

        content_type = (
            response.headers.get("Content-Type", "")
            .split(";")[0]
            .strip()
            .lower()
        )

        if content_type not in ALLOWED_VIDEO_MIME_TYPES:
            raise ValueError(
                f"Unsupported video type: {content_type or 'unknown'}"
            )

        content_length = response.headers.get("Content-Length")

        if content_length and int(content_length) > MAX_VIDEO_BYTES:
            raise ValueError("Video exceeds the 50 MB limit")

        video_bytes = response.read(MAX_VIDEO_BYTES + 1)

    if not video_bytes:
        raise ValueError("Downloaded video is empty")

    if len(video_bytes) > MAX_VIDEO_BYTES:
        raise ValueError("Video exceeds the 50 MB limit")

    return video_bytes, content_type


# ---------------------------------------------------------------------------
# JSON AND VALUE VALIDATION
# ---------------------------------------------------------------------------

def _parse_json_from_llm(raw: str) -> Dict[str, Any]:
    """Safely extract a JSON object from a Gemini response."""

    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("Gemini returned an empty response")

    cleaned = raw.strip()

    cleaned = re.sub(
        r"^```(?:json)?\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )

    cleaned = re.sub(r"\s*```$", "", cleaned)

    # First, try parsing the entire response.
    try:
        data = json.loads(cleaned)

        if isinstance(data, dict):
            return data

    except json.JSONDecodeError:
        pass

    # Extract a JSON object while respecting quoted strings.
    start = cleaned.find("{")

    if start == -1:
        raise ValueError(
            f"No JSON object found: {cleaned[:500]}"
        )

    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(cleaned)):
        char = cleaned[index]

        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False

            continue

        if char == '"':
            in_string = True

        elif char == "{":
            depth += 1

        elif char == "}":
            depth -= 1

            if depth == 0:
                candidate = cleaned[start:index + 1]

                try:
                    data = json.loads(candidate)

                    if isinstance(data, dict):
                        return data

                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"Gemini returned malformed JSON: {exc}"
                    ) from exc

    raise ValueError(
        f"Gemini returned incomplete JSON: {cleaned[:1000]}"
    )


def _extract_gemini_response(response) -> str:
    """
    Extract readable text from Gemini's response.

    Logs candidate finish reasons to diagnose blocked,
    incomplete, or truncated responses.
    """

    candidates = getattr(response, "candidates", None) or []

    for index, candidate in enumerate(candidates):
        finish_reason = getattr(candidate, "finish_reason", None)

        safety_ratings = getattr(
            candidate,
            "safety_ratings",
            None,
        )

        print(
            f"[gemini] Candidate {index} finish_reason: "
            f"{finish_reason}"
        )

        if safety_ratings:
            print(
                f"[gemini] Candidate {index} safety_ratings: "
                f"{safety_ratings}"
            )

    # Try the SDK's normal response.text property.
    try:
        raw = response.text

        if isinstance(raw, str) and raw.strip():
            return raw.strip()

    except Exception as exc:
        print(
            "[gemini] response.text extraction failed: "
            f"{type(exc).__name__}: {str(exc)[:300]}"
        )

    # Try extracting text directly from candidate parts.
    extracted_parts = []

    for candidate in candidates:
        content = getattr(candidate, "content", None)

        if not content:
            continue

        parts = getattr(content, "parts", None) or []

        for part in parts:
            part_text = getattr(part, "text", None)

            if isinstance(part_text, str) and part_text.strip():
                extracted_parts.append(part_text.strip())

    if extracted_parts:
        return "\n".join(extracted_parts)

    prompt_feedback = getattr(
        response,
        "prompt_feedback",
        None,
    )

    print(f"[gemini] Prompt feedback: {prompt_feedback}")
    print(f"[gemini] Candidate count: {len(candidates)}")

    raise ValueError(
        "Gemini returned no readable response text"
    )


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
            "confidence": _safe_confidence(
                item.get("confidence")
            ),
            "source": "gemini_vision",
        })

    return vehicles, detections


# ---------------------------------------------------------------------------
# GEMINI MULTIMODAL ANALYSIS
# ---------------------------------------------------------------------------

def classify_with_gemini(
    text: str,
    image_parts: Optional[List[Dict[str, Any]]] = None,
    video_bytes: Optional[bytes] = None,
    video_mime_type: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Analyze report text, optional photographs, and optional video.

    Uses Google's pretrained Gemini 2.5 Flash model.
    RoadRescue does not train or fine-tune the model.
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

    report_text = (
        (text or "").strip()
        or "[No written description provided]"
    )

    prompt = f"""
You are the incident-analysis assistant for RoadRescue,
a Philippine road-safety and roadside-assistance application.

Analyze the citizen report, attached incident photographs,
and attached incident video, if provided.

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
1. Do not invent injuries, casualties, or facts not present in the evidence.
2. Do not claim that a person is injured just because they are lying down
   or appear near a vehicle.
3. Use the written report as primary evidence for injury severity.
4. Photographs and video may help identify visible vehicles and hazards.
5. If evidence is insufficient, choose "medium" and set
   "needs_human_review" to true.
6. Do not guess a vehicle type if it is not clearly visible.
7. Do not return bounding boxes or claim exact object-detection coordinates.
8. A vehicle count is an estimate, not a guaranteed exact count.
9. The confidence value is your own estimate, not a statistically
   calibrated probability.
10. Video observations must describe only events actually visible
    in the provided footage.
11. Do not infer an exact collision time or sequence if the footage
    does not clearly establish it.
12. If the video is unclear, state that the visual evidence is uncertain.

Extract:
- keywords: up to 5 important report keywords
- mentioned_vehicles: vehicle types explicitly mentioned in the text
- image_vehicles: vehicle types clearly visible in photographs or video,
  with estimated count and confidence
- visual_observations: up to 5 short observations supported by the media
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
  "visual_observations": [
    "A motorcycle is visible near the roadside."
  ],
  "needs_human_review": true
}}
"""

    contents = [prompt] + (image_parts or [])

    uploaded_video = None
    temporary_video_path = None
    response = None

    try:
        if video_bytes:
            suffix = {
                "video/mp4": ".mp4",
                "video/quicktime": ".mov",
                "video/webm": ".webm",
                "video/x-msvideo": ".avi",
                "video/x-flv": ".flv",
                "video/mpeg": ".mpeg",
                "video/3gpp": ".3gp",
            }.get(video_mime_type, ".mp4")

            # Gemini's File API accepts a file path.
            with tempfile.NamedTemporaryFile(
                suffix=suffix,
                delete=False,
            ) as temporary_file:
                temporary_file.write(video_bytes)
                temporary_video_path = temporary_file.name

            print("[video] Uploading incident video to Gemini...")

            uploaded_video = genai.upload_file(
                path=temporary_video_path,
                mime_type=video_mime_type,
            )

            # Wait for Gemini to finish processing the video.
            started_at = time.monotonic()

            while True:
                uploaded_video = genai.get_file(
                    name=uploaded_video.name
                )

                state_value = getattr(
                    uploaded_video,
                    "state",
                    "",
                )

                state = getattr(
                    state_value,
                    "name",
                    str(state_value),
                ).upper()

                if state.endswith("ACTIVE"):
                    break

                if state.endswith("FAILED"):
                    raise RuntimeError(
                        "Gemini video processing failed"
                    )

                if (
                    time.monotonic() - started_at
                    > VIDEO_PROCESSING_TIMEOUT_SECONDS
                ):
                    raise TimeoutError(
                        "Gemini video processing timed out"
                    )

                time.sleep(VIDEO_POLL_INTERVAL_SECONDS)

            contents.append(uploaded_video)

            print("[video] Gemini video processing completed")

        response = model.generate_content(
            contents,
            generation_config={
                "temperature": 0.1,
                "max_output_tokens": 2048,
                "response_mime_type": "application/json",
            },
        )

    finally:
        # Remove the temporary file from Render.
        if (
            temporary_video_path
            and os.path.exists(temporary_video_path)
        ):
            try:
                os.remove(temporary_video_path)

            except OSError as exc:
                print(
                    f"[video] Temporary file cleanup failed: "
                    f"{type(exc).__name__}: {str(exc)[:160]}"
                )

        # Delete the temporary Gemini upload after processing.
        if uploaded_video:
            try:
                genai.delete_file(uploaded_video.name)

            except Exception as exc:
                print(
                    f"[video] Gemini file cleanup skipped: "
                    f"{type(exc).__name__}: {str(exc)[:120]}"
                )

    if response is None:
        raise RuntimeError("Gemini did not return a response")

    raw = _extract_gemini_response(response)

    print(f"[gemini] Raw response length: {len(raw)}")
    print(f"[gemini] Raw response preview: {repr(raw[:2000])}")

    try:
        data = _parse_json_from_llm(raw)

    except Exception as exc:
        print(
            f"[gemini] JSON parsing failed: "
            f"{type(exc).__name__}: {str(exc)[:300]}"
        )

        print(
            f"[gemini] Complete response: {repr(raw[:5000])}"
        )

        raise

    # Validate incident type.
    incident_type = data.get("type", "other")

    if incident_type not in ROAD_INCIDENT_TYPES:
        incident_type = "other"

    # Validate severity.
    severity = data.get("severity", "medium")

    if severity not in SEVERITY_LEVELS:
        severity = "medium"

    # Normalize mentioned vehicles.
    mentioned_vehicles = []

    mentioned_data = data.get("mentioned_vehicles", [])

    if isinstance(mentioned_data, list):
        for vehicle in mentioned_data:
            normalized = _normalize_vehicle(vehicle)

            if (
                normalized
                and normalized not in mentioned_vehicles
            ):
                mentioned_vehicles.append(normalized)

    # Normalize image/video vehicles.
    image_vehicles, detections = _normalize_image_vehicles(
        data.get("image_vehicles")
    )

    observations = _clean_string_list(
        data.get("visual_observations"),
        limit=5,
    )

    return {
        "type": incident_type,
        "severity": severity,
        "confidence": _safe_confidence(
            data.get("confidence")
        ),
        "keywords": _clean_string_list(
            data.get("keywords"),
            limit=5,
        ),
        "mentioned_vehicles": mentioned_vehicles,
        "image_vehicles": image_vehicles,
        "detections": detections,
        "visual_observations": observations,
        "needs_human_review": bool(
            data.get("needs_human_review", True)
        ),
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
        "motorcycle": [
            "motorcycle",
            "motorbike",
            "scooter",
            "motor",
        ],
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

    if any(k in t for k in [
        "on fire", "burning", "nasusunog", "sunog"
    ]):
        return _keyword_result(
            "vehicle_fire", "critical", mentioned
        )

    if any(k in t for k in [
        "major",
        "malaking",
        "grabe",
        "critical",
        "severe",
        "multiple vehicles",
        "injuries",
        "nasugatan",
    ]):
        return _keyword_result(
            "major_collision", "high", mentioned
        )

    if any(k in t for k in [
        "collision",
        "crash",
        "accident",
        "bangga",
        "bumangga",
        "sagasa",
        "rear-ended",
    ]):
        return _keyword_result(
            "minor_collision", "medium", mentioned
        )

    if any(k in t for k in [
        "flat",
        "punctured",
        "blown",
        "butas",
        "sabog ang gulong",
    ]):
        return _keyword_result(
            "flat_tire", "low", mentioned
        )

    if any(k in t for k in [
        "battery",
        "baterya",
        "won't start",
        "dead battery",
        "ayaw mag-start",
    ]):
        return _keyword_result(
            "battery", "low", mentioned
        )

    if any(k in t for k in [
        "out of fuel",
        "no gas",
        "ubos ang gas",
    ]):
        return _keyword_result(
            "fuel", "low", mentioned
        )

    if any(k in t for k in [
        "stalled",
        "breakdown",
        "broken down",
        "engine died",
        "tirik",
        "ayaw umandar",
    ]):
        return _keyword_result(
            "stalled_vehicle", "medium", mentioned
        )

    if any(k in t for k in [
        "pothole",
        "lubak",
        "debris",
        "fallen tree",
        "flood",
        "baha",
        "obstruction",
        "road block",
    ]):
        return _keyword_result(
            "road_hazard", "medium", mentioned
        )

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
    video_url: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Analyze one RoadRescue incident.

    Supports:
    - Text-only reports
    - Photo reports
    - Video reports
    - Text + photos
    - Text + video
    - Text + photos + video

    Existing response fields are retained for backend compatibility.
    """

    text = (text or "").strip()

    image_parts, image_errors = _load_images(image_urls)

    video_bytes = None
    video_mime_type = None
    video_error = None

    # Download incident video if supplied.
    if video_url:
        try:
            video_bytes, video_mime_type = (
                _download_firebase_video(video_url)
            )

        except Exception as exc:
            print(
                f"[video] Could not load incident video: "
                f"{type(exc).__name__}: {str(exc)[:160]}"
            )

            video_error = {
                "error": str(exc)[:160]
            }

    # Handle reports with no usable evidence.
    if not text and not image_parts and not video_bytes:
        text_result = _keyword_result(
            "other",
            "medium",
            [],
        )

        text_result["confidence"] = 0.3
        text_result["source"] = "empty"

    else:
        try:
            text_result = classify_with_gemini(
                text=text,
                image_parts=image_parts,
                video_bytes=video_bytes,
                video_mime_type=video_mime_type,
            )

        except Exception as exc:
            print(
                f"[predict] Gemini analysis failed: "
                f"{type(exc).__name__}: {str(exc)[:200]}"
            )

            # If video analysis fails, retry Gemini without video.
            # This preserves text and photograph analysis.
            if video_bytes:
                video_error = {
                    "error": (
                        "Video analysis failed: "
                        f"{type(exc).__name__}: {str(exc)[:140]}"
                    )
                }

                print(
                    "[predict] Retrying Gemini without video..."
                )

                try:
                    text_result = classify_with_gemini(
                        text=text,
                        image_parts=image_parts,
                    )

                except Exception as retry_exc:
                    print(
                        f"[predict] Gemini retry failed: "
                        f"{type(retry_exc).__name__}: "
                        f"{str(retry_exc)[:160]}"
                    )

                    text_result = classify_text_with_keywords(
                        text
                    )

            else:
                text_result = classify_text_with_keywords(
                    text
                )

    image_vehicles = text_result.get(
        "image_vehicles", {}
    )

    mentioned_vehicles = text_result.get(
        "mentioned_vehicles", []
    )

    # Keep the legacy vehicles field useful for analytics.
    vehicles = image_vehicles or {
        vehicle: 1
        for vehicle in mentioned_vehicles
    }

    all_vehicles = sorted(
        set(
            list(image_vehicles.keys())
            + list(mentioned_vehicles)
        )
    )

    # Record the analysis sources.
    sources = []

    if text_result.get("source") == "gemini":
        sources.append("gemini")

        if image_parts:
            sources.append("gemini_vision")

        if video_bytes and not video_error:
            sources.append("gemini_video")

    elif text_result.get("source") == "keyword":
        sources.append("keyword")

    else:
        sources.append("empty")

    # Build image analysis result.
    image_analysis = None

    if image_parts or image_errors:
        image_analysis = {
            "vehicles": image_vehicles,
            "detections": text_result.get(
                "detections", []
            ),
            "observations": text_result.get(
                "visual_observations", []
            ),
            "images_received": len(image_urls or []),
            "images_analyzed": (
                len(image_parts)
                if text_result.get("source") == "gemini"
                else 0
            ),
            "errors": image_errors,
        }

    # Build video analysis result.
    video_analysis = {
        "video_received": bool(video_url),
        "video_downloaded": bool(video_bytes),
        "video_analyzed": (
            bool(video_bytes)
            and text_result.get("source") == "gemini"
            and not video_error
        ),
        "mime_type": video_mime_type,
        "error": video_error,
    }

    if video_analysis["video_analyzed"]:
        video_analysis["status"] = "completed"

    elif video_url:
        video_analysis["status"] = "failed"

    else:
        video_analysis["status"] = "not_provided"

    # Determine evidence used for severity classification.
    if text and image_parts and video_analysis["video_analyzed"]:
        severity_basis = "text_image_and_video"

    elif text and video_analysis["video_analyzed"]:
        severity_basis = "text_and_video"

    elif image_parts and video_analysis["video_analyzed"]:
        severity_basis = "image_and_video"

    elif video_analysis["video_analyzed"]:
        severity_basis = "video"

    elif text and image_parts:
        severity_basis = "text_and_image"

    elif text:
        severity_basis = "text"

    elif image_parts:
        severity_basis = "image"

    else:
        severity_basis = "insufficient_evidence"

    # Preserve existing response structure for backend compatibility.
    return {
        "predicted_type": text_result.get(
            "type", "other"
        ),
        "predicted_severity": text_result.get(
            "severity", "medium"
        ),
        "confidence": text_result.get(
            "confidence", 0.4
        ),
        "keywords": text_result.get(
            "keywords", []
        ),
        "mentioned_vehicles": mentioned_vehicles,
        "image_vehicles": image_vehicles,
        "detections": text_result.get(
            "detections", []
        ),
        "vehicles": vehicles,
        "all_vehicles": all_vehicles,
        "sources": sources,
        "needs_human_review": text_result.get(
            "needs_human_review", True
        ),
        "severity_basis": severity_basis,
        "text_analysis": text_result,
        "image_analysis": image_analysis,
        "video_analysis": video_analysis,
    }


# ---------------------------------------------------------------------------
# LOCAL TEST
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv

    load_dotenv()

    sample = (
        " ".join(sys.argv[1:])
        if len(sys.argv) > 1
        else "Nagkaroon ng banggaan sa highway, may nasugatan."
    )

    print(
        json.dumps(
            predict_incident(sample),
            indent=2,
            ensure_ascii=False,
        )
    )
