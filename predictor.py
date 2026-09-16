"""
RoadRescue incident classifier.

Combines two signals:
  1. Gemini — text classification of the citizen's free-text description
     (handles English, Tagalog, and Taglish)
  2. YOLOv8 — object detection on incident photos

Both are optional at runtime: if one fails, the other still produces a result.
If both fail, a keyword-based fallback runs.

Output is aligned with the frontend's 8 road-incident types:
  flat_tire, battery, fuel, stalled_vehicle,
  minor_collision, major_collision, vehicle_fire, road_hazard

Plus `other` as the catch-all.
"""

import json
import os
import re
import tempfile
from typing import Any, Dict, List, Optional

import numpy as np
import cv2
from PIL import Image

# ---------------------------------------------------------------------------
# CONSTANTS — defined at top so they're available everywhere
# ---------------------------------------------------------------------------

# Road-focused incident types (must match the frontend `TYPES` array in
# src/views/dashboards/citizen/ReportTab.vue)
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

# Human-readable labels for the Gemini prompt
TYPE_LABELS = {
    "flat_tire":       "flat tire / punctured tire / blown out tire",
    "battery":         "dead battery / won't start / jump start needed",
    "fuel":            "out of fuel / ran out of gas",
    "stalled_vehicle": "stalled / broken down / engine died / won't move",
    "minor_collision": "minor crash / fender bender / low-speed collision",
    "major_collision": "major crash / serious accident / multiple vehicles / injuries",
    "vehicle_fire":    "vehicle on fire / burning car or motorcycle",
    "road_hazard":     "pothole / debris / fallen tree / flooding / road obstruction",
    "other":           "anything else that doesn't fit above",
}

# YOLO vehicle classes we care about
VEHICLE_CLASSES = {
    "car", "truck", "bus", "motorcycle", "bicycle",
    "suv", "van", "pickup", "jeep", "lorry",
}

# Lazy-loaded YOLO model
_yolo_model = None


# ===========================================================================
# GEMINI TEXT CLASSIFICATION
# ===========================================================================

def classify_text_with_gemini(text: str) -> Dict[str, Any]:
    """
    Call Gemini to classify a free-text incident description.

    Returns a dict with:
        type, severity, confidence, keywords, mentioned_vehicles, source

    Raises on failure so the caller can fall back to keywords.
    """
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")

    # Import lazily so the module loads fast even if unused
    import google.generativeai as genai

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-2.5-flash")

    # Build a road-focused prompt
    type_list = "\n".join(
        f'  - "{key}": {label}'
        for key, label in TYPE_LABELS.items()
    )

    prompt = f"""You are an incident classifier for RoadRescue, a Philippine road-safety app.
Classify the following citizen report and return ONLY valid JSON.

Report (may be in English, Tagalog, or Taglish):
\"\"\"{text}\"\"\"

Choose ONE incident type from this list:
{type_list}

Choose ONE severity:
  - "low":      cosmetic, no hazard, minor inconvenience
  - "medium":   vehicle can't move, needs assistance, no injuries
  - "high":     collision with possible injury, fire, or blocking traffic
  - "critical": serious injuries, major collision, fire spreading, or life-threatening

Also extract:
  - keywords: up to 5 important words from the report (lowercase)
  - mentioned_vehicles: any vehicle types mentioned (e.g. ["car", "motorcycle"])
  - confidence: 0.0 to 1.0 — how sure you are of the type

Return JSON EXACTLY like this (no markdown, no commentary):
{{
  "type": "major_collision",
  "severity": "high",
  "confidence": 0.92,
  "keywords": ["truck", "motorcycle", "collision"],
  "mentioned_vehicles": ["truck", "motorcycle"]
}}
"""

    response = model.generate_content(
        prompt,
        generation_config={
            "temperature": 0.1,
            "max_output_tokens": 2000,   # ← was 800; Gemini 2.5 uses thinking tokens
            "response_mime_type": "application/json",
        },  
    )

    raw = (response.text or "").strip()

    # Debug: log the raw response so we can see what Gemini actually sent
    print(f"[gemini] raw response: {raw[:300]}")

    data = _parse_json_from_llm(raw)

    # Validate the type is one we accept
    incident_type = data.get("type", "other")
    if incident_type not in ROAD_INCIDENT_TYPES:
        incident_type = "other"

    severity = data.get("severity", "medium")
    if severity not in SEVERITY_LEVELS:
        severity = "medium"

    return {
        "type": incident_type,
        "severity": severity,
        "confidence": float(data.get("confidence", 0.5)),
        "keywords": list(data.get("keywords", []))[:5],
        "mentioned_vehicles": list(data.get("mentioned_vehicles", [])),
        "source": "gemini",
    }


def _parse_json_from_llm(raw: str) -> Dict[str, Any]:
    """
    Robustly extract JSON from an LLM response.

    Handles:
      - ```json ... ``` fences (case-insensitive)
      - ``` ... ``` bare fences
      - Leading/trailing prose
      - Trailing newlines / whitespace
    """
    # Strip markdown fences (case-insensitive)
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    # Try direct parse
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Fallback: extract the first {...} block
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Could not parse JSON from LLM response: {raw[:200]}")


# ===========================================================================
# KEYWORD FALLBACK
# ===========================================================================

def classify_text_with_keywords(text: str) -> Dict[str, Any]:
    """
    Simple keyword-based classifier. Used when Gemini is unavailable.
    Fast, offline, deterministic — but less accurate.
    """
    t = (text or "").lower()

    # Detect vehicles mentioned
    vehicle_map = {
        "car":        ["car", "sedan", "suv", "van"],
        "truck":      ["truck", "lorry", "dump truck"],
        "motorcycle": ["motorcycle", "motorbike", "scooter", "motor", "bike"],
        "bus":        ["bus", "minibus"],
        "jeepney":    ["jeepney", "jeep"],
        "tricycle":   ["tricycle", "trike"],
    }
    mentioned_vehicles = [
        vt for vt, words in vehicle_map.items()
        if any(w in t for w in words)
    ]

    # Detect type — ordered by priority (most severe first)
    if any(k in t for k in ["on fire", "burning", "nasusunog", "sunog"]):
        return _result("vehicle_fire", "critical", 0.75, mentioned_vehicles)

    if any(k in t for k in ["major", "malaking", "grabe", "critical", "severe",
                            "multiple vehicles", "injuries", "nasugatan"]):
        return _result("major_collision", "high", 0.7, mentioned_vehicles)

    if any(k in t for k in ["collision", "crash", "accident", "bangga",
                            "bumangga", "sagasa", "rear-ended"]):
        return _result("minor_collision", "medium", 0.65, mentioned_vehicles)

    if any(k in t for k in ["flat", "punctured", "blown", "butas",
                            "flat tire", "sabog ang gulong"]):
        return _result("flat_tire", "low", 0.75, mentioned_vehicles)

    if any(k in t for k in ["battery", "baterya", "won't start",
                            "dead battery", "ayaw mag-start"]):
        return _result("battery", "low", 0.75, mentioned_vehicles)

    if any(k in t for k in ["out of fuel", "no gas", "ubos ang gas",
                            "empty tank", "naubusan ng gasolina"]):
        return _result("fuel", "low", 0.75, mentioned_vehicles)

    if any(k in t for k in ["stalled", "breakdown", "broken down",
                            "engine died", "tirik", "ayaw umandar"]):
        return _result("stalled_vehicle", "medium", 0.7, mentioned_vehicles)

    if any(k in t for k in ["pothole", "lubak", "debris", "fallen tree",
                            "flood", "baha", "obstruction", "road block"]):
        return _result("road_hazard", "medium", 0.7, mentioned_vehicles)

    return _result("other", "medium", 0.4, mentioned_vehicles)


def _result(type_: str, severity: str, confidence: float,
            mentioned_vehicles: List[str]) -> Dict[str, Any]:
    return {
        "type": type_,
        "severity": severity,
        "confidence": confidence,
        "keywords": [],
        "mentioned_vehicles": mentioned_vehicles,
        "source": "keyword",
    }


# ===========================================================================
# YOLO IMAGE ANALYSIS
# ===========================================================================

def get_yolo_model():
    """Lazy-load YOLOv8n. Downloads on first run (~6MB), then cached."""
    global _yolo_model
    if _yolo_model is None:
        print("[YOLO] Loading yolov8n.pt (first run may download)...")
        from ultralytics import YOLO
        _yolo_model = YOLO("yolov8n.pt")
        _yolo_model.to("cpu")
        print("[YOLO] Model loaded.")
    return _yolo_model


def extract_vehicles(detections: List[Dict]) -> Dict[str, int]:
    """Count vehicle detections by class."""
    vehicles: Dict[str, int] = {}
    for d in detections:
        obj = (d.get("object") or "").lower()
        if obj in VEHICLE_CLASSES:
            vehicles[obj] = vehicles.get(obj, 0) + 1
    return vehicles


def analyze_image(image_path: str) -> Optional[Dict[str, Any]]:
    """
    Run YOLOv8 on a single image. Returns detection summary or None on error.
    """
    if not os.path.exists(image_path):
        print(f"[YOLO] image not found: {image_path}")
        return None

    try:
        img = cv2.imread(image_path)
        if img is None:
            # PIL fallback
            arr = np.array(Image.open(image_path).convert("RGB"))
            img = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)

        img = cv2.resize(img, (640, 640))

        model = get_yolo_model()
        results = model(img, verbose=False)

        detections: List[Dict[str, Any]] = []
        for r in results:
            boxes = r.boxes
            if boxes is None:
                continue
            for box in boxes:
                cls_id = int(box.cls[0])
                conf = float(box.conf[0])
                name = model.names[cls_id]
                detections.append({
                    "object": name,
                    "confidence": round(conf, 3),
                    "class_id": cls_id,
                })

        vehicles = extract_vehicles(detections)

        return {
            "detections": detections,
            "vehicles": vehicles,
            "total_objects": len(detections),
            "confidence": max((d["confidence"] for d in detections), default=0.5),
        }
    except Exception as e:
        print(f"[YOLO] image analysis failed: {e}")
        return None


# ===========================================================================
# TOP-LEVEL: COMBINE SIGNALS
# ===========================================================================

def predict_incident(
    text: str,
    image_urls: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Main entry point called by the FastAPI endpoint.

    Args:
        text: citizen's free-text description
        image_urls: optional list of image URLs (Firebase Storage download URLs)

    Returns a dict:
        {
          "predicted_type": "major_collision",
          "predicted_severity": "high",
          "confidence": 0.92,
          "keywords": [...],
          "mentioned_vehicles": [...],
          "detections": [...],
          "vehicles": {...},
          "sources": ["gemini", "yolo"],   # which signals fired
          "text_analysis": {...},           # raw gemini/keyword result
          "image_analysis": {...},          # raw YOLO result (or null)
        }
    """
    sources: List[str] = []

    # ----- Text analysis -----
    text_result: Dict[str, Any] = {}
    if text and len(text.strip()) >= 3:
        try:
            text_result = classify_text_with_gemini(text)
            sources.append("gemini")
        except Exception as e:
            print(f"[predict] Gemini failed ({e}); falling back to keywords")
            text_result = classify_text_with_keywords(text)
            sources.append("keyword")
    else:
        text_result = {
            "type": "other",
            "severity": "medium",
            "confidence": 0.3,
            "keywords": [],
            "mentioned_vehicles": [],
            "source": "empty",
        }

    # ----- Image analysis -----
    image_result: Optional[Dict[str, Any]] = None
    if image_urls:
        # For Phase A we only handle local paths.
        # Phase B adds Firebase Storage download → temp file.
        for url in image_urls[:3]:  # cap at 3 images
            result = analyze_image(url)
            if result:
                image_result = result
                sources.append("yolo")
                break

    # ----- Combine -----
    vehicles_from_image = (image_result or {}).get("vehicles", {})
    vehicles_from_text = set(text_result.get("mentioned_vehicles", []))
    all_vehicles = list(set(list(vehicles_from_image.keys()) + list(vehicles_from_text)))

    # Gemini's type wins; YOLO adds detail but doesn't override
    final_type = text_result.get("type", "other")
    final_severity = text_result.get("severity", "medium")

    # Boost severity if YOLO detected fire
    if image_result and any(
        (d.get("object") or "").lower() in ("fire", "smoke")
        for d in image_result.get("detections", [])
    ):
        final_type = "vehicle_fire"
        final_severity = "critical"

    return {
        "predicted_type": final_type,
        "predicted_severity": final_severity,
        "confidence": text_result.get("confidence", 0.5),
        "keywords": text_result.get("keywords", []),
        "mentioned_vehicles": text_result.get("mentioned_vehicles", []),
        "detections": (image_result or {}).get("detections", []),
        "vehicles": vehicles_from_image or {
            v: 1 for v in vehicles_from_text
        },
        "all_vehicles": all_vehicles,
        "sources": sources,
        "text_analysis": text_result,
        "image_analysis": image_result,
    }


if __name__ == "__main__":
    # Quick CLI test: python predictor.py "car crash on the highway"
    import sys
    from dotenv import load_dotenv
    load_dotenv()

    sample = sys.argv[1] if len(sys.argv) > 1 else "nagkaroon ng banggaan sa highway, may nasugatan"
    print(json.dumps(predict_incident(sample), indent=2))