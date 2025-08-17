# AqrayPath FastAPI backend (watsonx + strong guardrails + clear reasons)
# Endpoints:
#   GET  /health
#   POST /recommend {start, destination, night_test?, max_detour_min?}

import json, os, time
from typing import Dict, Any, List
import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from urllib.parse import urlencode
from dotenv import load_dotenv

# ---------- Config (from .env) ----------
load_dotenv()

IBM_API_KEY = os.getenv("IBM_CLOUD_API_KEY", "")
DEPLOYMENT_URL = os.getenv(
    "WATSONX_DEPLOYMENT_URL",
    "https://us-south.ml.cloud.ibm.com/ml/v4/deployments/19eda375-12c0-4e61-a0f2-5fd30f11a20c/ai_service?version=2021-05-01",
)
GOOGLE_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")
DALLAS_APP_TOKEN = os.getenv("SOCRATA_APP_TOKEN", "")

REQUEST_TIMEOUT = (10, 30)  # (connect, read) seconds

# ---------- App ----------
app = FastAPI(title="AqrayPath Wrapper", version="1.5.0")

# CORS for Streamlit / localhost
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class RouteRequest(BaseModel):
    start: str
    destination: str
    night_test: bool | None = None      # demo toggle to force night behavior
    max_detour_min: int | None = None   # user-tunable detour cap (defaults to 6)

# ---------- IBM helpers ----------
def get_iam_token(api_key: str) -> str:
    if not api_key:
        raise HTTPException(status_code=500, detail="Missing IBM_CLOUD_API_KEY.")
    resp = requests.post(
        "https://iam.cloud.ibm.com/identity/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=f"grant_type=urn:ibm:params:oauth:grant-type:apikey&apikey={api_key}",
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]

def call_watsonx(token: str, prompt_text: str) -> Dict[str, Any]:
    payload = {
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are AqrayPath SafetyScorer. Respond ONLY with a single JSON object, no prose. "
                    "Schema: {\"deltaSafety\": integer, \"decision\": \"ask_user\"|\"continue\", "
                    "\"message\": string, \"etaChangeMinutes\": integer, \"proposedRouteName\": string, "
                    "\"reasons\": [string, ...]}. "
                    "Hard rules: If route_crime_candidate > route_crime_current by ≥10, you MUST set decision='continue' "
                    "and the message MUST end with 'Keeping your current route.'. "
                    "If etaChangeMinutes ≥ 6 and deltaSafety ≤ 1, you MUST set decision='continue'. "
                    "Otherwise: If candidate is meaningfully safer (deltaSafety ≥ 1) and detour is reasonable, decision='ask_user' "
                    "with a short two-sentence message ending with 'Reroute now or continue?'. "
                    "If candidate is worse or detour too long, decision='continue' with one short sentence ending with "
                    "'Keeping your current route.'."
                ),
            },
            {"role": "user", "content": prompt_text},
        ]
    }
    resp = requests.post(
        DEPLOYMENT_URL,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        data=json.dumps(payload),
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()

def parse_agent_content(resp_json: Dict[str, Any]) -> Dict[str, Any]:
    content = resp_json.get("choices", [{}])[0].get("message", {}).get("content", "")
    try:
        return json.loads(content)
    except Exception:
        return {"raw": content}

def normalize_agent(parsed: Dict[str, Any], eta_change_min: int) -> Dict[str, Any]:
    out = {
        "deltaSafety": int(parsed.get("deltaSafety", 0)),
        "decision": (parsed.get("decision") or "continue").strip(),
        "message": str(parsed.get("message", "")).strip(),
        "etaChangeMinutes": int(parsed.get("etaChangeMinutes", eta_change_min)),
        "proposedRouteName": str(parsed.get("proposedRouteName", "")),
        "reasons": parsed.get("reasons", []),
    }
    if out["decision"] == "ask_user":
        if not out["message"].endswith("Reroute now or continue?"):
            out["message"] = (out["message"].rstrip(".") + ". Reroute now or continue?").strip()
    else:
        out["decision"] = "continue"
        if not out["message"].endswith("Keeping your current route."):
            base = out["message"].splitlines()[0].strip() or "Candidate offers no clear safety gain."
            out["message"] = base.rstrip(".") + ". Keeping your current route."
    return out

# ---------- Google helpers ----------
def gmaps_directions(origin: str, dest: str) -> Dict[str, Any]:
    if not GOOGLE_KEY:
        raise HTTPException(status_code=500, detail="GOOGLE_MAPS_API_KEY not set.")
    params = {
        "origin": origin,
        "destination": dest,
        "mode": "walking",
        "alternatives": "true",
        "key": GOOGLE_KEY,
    }
    r = requests.get("https://maps.googleapis.com/maps/api/directions/json", params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "OK":
        raise HTTPException(status_code=400, detail=f"Google Directions error: {data.get('status')}")
    return data

def steps_to_streets(steps: List[Dict[str, Any]]) -> List[str]:
    streets = []
    for s in steps:
        name = s.get("maneuver") or ""
        if not name:
            instr = s.get("html_instructions", "")
            name = (instr
                    .replace("<b>", "").replace("</b>", "")
                    .replace('<div style="font-size:0.9em">', " ")
                    .replace("</div>", " "))
        name = " ".join(name.split())
        if name:
            streets.append(name)
    if not streets:
        streets = [f"step {i+1}" for i in range(len(steps))]
    return streets[:8]

def pick_routes(routes: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    fastest = min(routes, key=lambda r: r["legs"][0]["duration"]["value"])
    def light_score(r):
        steps = r["legs"][0]["steps"]
        text = " ".join([s.get("html_instructions","") for s in steps]).lower()
        score = sum(kw in text for kw in ["blvd", "ave", "main", "park", "downtown", "plaza", "square"])
        return (score, -r["legs"][0]["duration"]["value"])
    alts = [r for r in routes if r is not fastest]
    candidate = max(alts, key=light_score) if alts else fastest
    return {"current": fastest, "candidate": candidate}

# ---------- Weather (Open-Meteo) ----------
def geocode_city(name: str) -> Dict[str, float]:
    g = requests.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": name, "count": 1, "language": "en", "format": "json"},
        timeout=REQUEST_TIMEOUT,
    )
    g.raise_for_status()
    j = g.json()
    if not j.get("results"):
        # fallback to Dallas center
        return {"lat": 32.7767, "lon": -96.7970}
    r = j["results"][0]
    return {"lat": r["latitude"], "lon": r["longitude"]}

def get_current_weather(lat: float, lon: float) -> Dict[str, Any]:
    w = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={"latitude": lat, "longitude": lon, "current": "temperature_2m,precipitation,weather_code,wind_speed_10m"},
        timeout=REQUEST_TIMEOUT,
    )
    w.raise_for_status()
    return w.json().get("current", {})

def wx_code_to_text(code: int) -> str:
    mapping = {
        0: "Clear", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
        45: "Fog", 48: "Depositing rime fog",
        51: "Light drizzle", 53: "Drizzle", 55: "Dense drizzle",
        61: "Light rain", 63: "Rain", 65: "Heavy rain",
        71: "Light snow", 73: "Snow", 75: "Heavy snow",
        80: "Rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
        95: "Thunderstorm", 96: "Thunderstorm with slight hail", 99: "Thunderstorm with heavy hail",
    }
    return mapping.get(code, f"Code {code}")

# ---------- Crime (Dallas Open Data) ----------
def crime_count(lat: float, lon: float, radius_m: int = 500, days: int = 30) -> int:
    endpoint = "https://www.dallasopendata.com/resource/yn72-daik.json"
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - days * 86400))
    where = f"upzdate > '{since}' AND within_circle(geocoded_column,{lat},{lon},{radius_m})"
    qs = urlencode({"$select": "count(1)", "$where": where})
    headers = {"X-App-Token": DALLAS_APP_TOKEN} if DALLAS_APP_TOKEN else {}
    r = requests.get(f"{endpoint}?{qs}", headers=headers, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    j = r.json()
    try:
        return int(j[0].get("count_1", 0))
    except Exception:
        return 0

# ---------- Route crime probes ----------
def leg_probe_points(leg: Dict[str, Any]) -> List[Dict[str, float]]:
    steps = leg.get("steps", [])
    if not steps:
        return []
    n = len(steps)
    idxs = {0 if n == 1 else max(1, n // 3), n // 2, max(0, n - 2)}
    coords = []
    for idx in sorted(idxs):
        try:
            coords.append(steps[idx]["end_location"])
        except Exception:
            pass
    # dedupe
    seen = set(); uniq = []
    for p in coords:
        try:
            key = (round(float(p.get("lat")), 6), round(float(p.get("lng")), 6))
        except Exception:
            continue
        if key in seen:
            continue
        seen.add(key)
        uniq.append({"lat": key[0], "lng": key[1]})
    return uniq

def route_crime_summary(leg: Dict[str, Any], radius_m: int = 250, days: int = 30) -> Dict[str, Any]:
    pts = leg_probe_points(leg)
    samples, total = [], 0
    for p in pts:
        try:
            lat = float(p.get("lat")); lon = float(p.get("lng"))
        except Exception:
            continue
        cnt = crime_count(lat, lon, radius_m=radius_m, days=days)
        samples.append({"lat": lat, "lon": lon, "count": cnt})
        total += cnt
    # NOTE: samples here are perfect for a weighted heatmap: [lat, lon, weight=count]
    return {"total": total, "samples": samples, "radius_m": radius_m, "days": days}

# ---------- Lighting helper ----------
def lighting_score_from_steps(steps: List[Dict[str, Any]]) -> int:
    text = " ".join([s.get("html_instructions", "") for s in steps]).lower()
    kws = ["blvd", "ave", "main", "park", "downtown", "plaza", "square"]
    return sum(k in text for k in kws)

# ---------- API ----------
@app.get("/health")
def health():
    return {"ok": True}

@app.post("/recommend")
def recommend(req: RouteRequest):
    # 1) Routes
    data = gmaps_directions(req.start, req.destination)
    if not data.get("routes"):
        raise HTTPException(status_code=404, detail="No routes found")
    pick = pick_routes(data["routes"])
    cur = pick["current"]; can = pick["candidate"]
    cur_leg = cur["legs"][0]; can_leg = can["legs"][0]

    # Map bits
    current_poly = cur.get("overview_polyline", {}).get("points", "")
    candidate_poly = can.get("overview_polyline", {}).get("points", "")
    start_ll = cur_leg.get("start_location", {}) or {}
    end_ll   = cur_leg.get("end_location", {}) or {}

    # 2) Summaries
    current_streets = " -> ".join(steps_to_streets(cur_leg["steps"]))
    candidate_streets = " -> ".join(steps_to_streets(can_leg["steps"]))
    eta_change_min = round((can_leg["duration"]["value"] - cur_leg["duration"]["value"]) / 60.0)

    # 3) Weather
    try:
        geo = geocode_city(req.destination)
    except Exception:
        geo = {"lat": 32.7767, "lon": -96.7970}
    cur_wx = get_current_weather(geo["lat"], geo["lon"])
    wx_text = wx_code_to_text(int(cur_wx.get("weather_code", 0)))
    precip = cur_wx.get("precipitation", 0)
    temp_c = cur_wx.get("temperature_2m", 0)

    # 4) Crime
    crime_30d = crime_count(geo["lat"], geo["lon"], radius_m=500, days=30)
    current_crime = route_crime_summary(cur_leg, radius_m=250, days=30)
    candidate_crime = route_crime_summary(can_leg, radius_m=250, days=30)

    now = time.strftime("%H:%M")
    context = (
        f"{wx_text}, {temp_c} degC, precipitation {precip} mm, street lighting rating 2, "
        f"crime_count_30d_500m {crime_30d}, route_crime_current {current_crime['total']}, "
        f"route_crime_candidate {candidate_crime['total']}."
    )
    proposed = can.get("summary") or "Candidate Route"
    prompt = (
        f"TIME: {now}\nCONTEXT: {context}\nCOMPARE\n"
        f"CURRENT = {current_streets}\nCANDIDATE = {candidate_streets}\n"
        f"etaChangeMinutes = {eta_change_min}\nproposedRouteName = {proposed}"
    )

    # 5) Agent decision — try Watson; fallback to heuristic
    try:
        token = get_iam_token(IBM_API_KEY)
        raw = call_watsonx(token, prompt)
        parsed = parse_agent_content(raw)
        normalized = normalize_agent(parsed, eta_change_min)
    except Exception:
        # Local fallback
        crime_margin = 10
        eta_hard_cap = req.max_detour_min if getattr(req, "max_detour_min", None) not in (None, 0) else 6
        small_gain_threshold = 1
        eta_penalty = eta_change_min
        reasons = []
        decision = "continue"
        deltaSafety = 0
        msg = "Using local heuristic due to AI service error. Keeping your current route."

        cand_worse = candidate_crime["total"] > current_crime["total"] + crime_margin
        if cand_worse:
            diff = candidate_crime["total"] - current_crime["total"]
            reasons.append("higher route crime")
            msg = f"Candidate has higher recent incident density (+{diff}). Keeping your current route."
        else:
            crime_gain = current_crime["total"] - candidate_crime["total"]
            if crime_gain >= 15 and eta_penalty <= eta_hard_cap:
                decision = "ask_user"
                deltaSafety = 2
                reasons.append("lower route crime")
                msg = f"Candidate shows a lower recent incident density (−{crime_gain}). Reroute now or continue?"
            hour = int(time.strftime("%H"))
            is_night = (hour >= 20 or hour <= 5) or bool(getattr(req, "night_test", None))
            if is_night:
                cur_light = lighting_score_from_steps(cur_leg.get("steps", []))
                can_light = lighting_score_from_steps(can_leg.get("steps", []))
                if can_light > cur_light and eta_penalty <= eta_hard_cap and not cand_worse:
                    decision = "ask_user"
                    deltaSafety = max(deltaSafety, 1)
                    reasons.append("better lighting at night")
                    if not msg.endswith("Reroute now or continue?"):
                        msg = (msg.rstrip(".") + ". Reroute now or continue?")
            if eta_penalty >= eta_hard_cap and deltaSafety <= small_gain_threshold:
                decision = "continue"
                reasons.append("large ETA penalty")
                msg = f"Detour adds ~{eta_penalty} min without a clear safety gain. Keeping your current route."

        normalized = {
            "deltaSafety": int(deltaSafety),
            "decision": "ask_user" if decision == "ask_user" else "continue",
            "message": msg if decision == "ask_user" else (msg.rstrip(".") + ". Keeping your current route."),
            "etaChangeMinutes": int(eta_penalty),
            "proposedRouteName": proposed,
            "reasons": sorted(set(reasons)),
        }

    # ---------- FINAL SAFETY OVERRIDE ----------
    crime_margin_override = 10
    if candidate_crime["total"] >= current_crime["total"] + crime_margin_override:
        diff = candidate_crime["total"] - current_crime["total"]
        normalized["decision"] = "continue"
        normalized["message"] = f"Candidate has higher recent incident density (+{diff}). Keeping your current route."
        reasons = set(normalized.get("reasons", []))
        reasons.add("higher route crime")
        normalized["reasons"] = sorted(reasons)

    # ---------- Build reasons & tags ----------
    crime_cur = current_crime["total"]
    crime_can = candidate_crime["total"]
    crime_diff = crime_cur - crime_can  # positive => candidate safer
    eta_penalty = eta_change_min
    hour = int(time.strftime("%H"))
    is_night = (hour >= 20 or hour <= 5) or bool(getattr(req, "night_test", None))
    weather_code = int(cur_wx.get("weather_code", 0))
    bad_weather = weather_code in {61,63,65,80,81,82,95,96,99,45,48}

    reasons = set(normalized.get("reasons", []))
    if crime_diff >= 15:
        reasons.add(f"Meaningfully lower recent incidents on candidate (−{crime_diff})")
    elif crime_diff <= -10:
        reasons.add(f"Higher recent incidents on candidate (+{-crime_diff})")
    if eta_penalty > 0:
        reasons.add(f"Adds about {eta_penalty} min")
    try:
        cur_light = lighting_score_from_steps(cur_leg.get("steps", []))
        can_light = lighting_score_from_steps(can_leg.get("steps", []))
        if is_night and can_light != cur_light:
            if can_light > cur_light:
                reasons.add("Better lighting cues on candidate at night")
            else:
                reasons.add("Current route appears better lit at night")
    except Exception:
        pass
    if bad_weather:
        reasons.add("Weather caution (rain/fog/storms)")

    rule_tags = []
    if candidate_crime["total"] >= current_crime["total"] + 10:
        rule_tags.append("override:higher_route_crime")
    detour_cap = req.max_detour_min if getattr(req, "max_detour_min", None) not in (None, 0) else 6
    if eta_penalty >= detour_cap and int(normalized.get("deltaSafety", 0)) <= 1:
        rule_tags.append("override:large_eta_small_gain")

    normalized["reasons"] = sorted(reasons)
    if rule_tags:
        normalized["rules_applied"] = rule_tags

    # Expose simple scores + samples for heatmap
    scores = {
        "crime_current": crime_cur,
        "crime_candidate": crime_can,
        "crime_delta": crime_diff,
        "eta_change_min": eta_penalty,
        "is_night": bool(is_night),
        "bad_weather": bool(bad_weather),
    }

    # ---------- Response ----------
    return {
        "request": {"start": req.start, "destination": req.destination},
        "routes": {
            "current_eta_min": round(cur_leg["duration"]["value"]/60.0, 1),
            "candidate_eta_min": round(can_leg["duration"]["value"]/60.0, 1),
            "eta_change_min": eta_change_min,
            "current_summary": cur.get("summary", ""),
            "candidate_summary": can.get("summary", ""),
            "current_polyline": current_poly,
            "candidate_polyline": candidate_poly,
            "start": {"lat": start_ll.get("lat"), "lng": start_ll.get("lng")},
            "destination": {"lat": end_ll.get("lat"), "lng": end_ll.get("lng")},
        },
        "weather": {"description": wx_text, "temp_c": temp_c, "precip_mm": precip},
        "agent_response": normalized,
        "scores": scores,
        "debug": {
            "prompt": prompt,
            # Include probe samples (lat, lon, count) — used for weighted heatmap in UI
            "route_crime": {"current": current_crime, "candidate": candidate_crime},
        },
    }
