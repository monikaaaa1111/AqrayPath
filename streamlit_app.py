import re
import requests
import streamlit as st
from polyline import decode as decode_polyline
import folium
from folium.plugins import HeatMap
from streamlit_folium import st_folium

st.set_page_config(page_title="AqrayPath", layout="wide")
st.title("AqrayPath")
st.caption("Safety-first walking-route copilot · SDG-11")

# ---------- session defaults ----------
if "last_data" not in st.session_state:
    st.session_state.last_data = None
if "last_error" not in st.session_state:
    st.session_state.last_error = None
if "base_url" not in st.session_state:
    st.session_state.base_url = "http://127.0.0.1:8000"

# ---------- Sidebar ----------
with st.sidebar:
    st.header("Settings")
    base_url_in = st.text_input("Backend BASE_URL", st.session_state.base_url)
    m = re.search(r"(https?://[^\s]+)", base_url_in.strip())
    st.session_state.base_url = m.group(1) if m else base_url_in.strip()

    max_detour = st.number_input("Max detour (minutes)", min_value=1, max_value=20, value=6, step=1)
    night_test = st.toggle("Force night mode (demo)", value=False)

    # NEW: Crime heatmap toggle
    show_heatmap = st.checkbox("Show crime heatmap", value=False)

    if st.button("Check backend"):
        try:
            r = requests.get(f"{st.session_state.base_url.rstrip('/')}/health", timeout=10)
            st.success(f"Backend OK: {r.json()}")
        except Exception as e:
            st.error(f"Backend not reachable: {e}")

    st.markdown("---")
    st.caption("Tip: run backend: `uvicorn app:app --reload --host 127.0.0.1 --port 8000`")

# ---------- Inputs ----------
st.subheader("Plan a safer walk")
start = st.text_input("Start", "Union Station, Dallas, TX").strip()
dest = st.text_input("Destination", "Dallas City Hall, Dallas, TX").strip()

col_btn, col_status = st.columns([1, 2])
run_clicked = col_btn.button("Recommend safest route", type="primary")

# ---------- helpers ----------
def call_backend():
    url = f"{st.session_state.base_url.rstrip('/')}/recommend"
    payload = {
        "start": start,
        "destination": dest,
        "night_test": bool(night_test),
        "max_detour_min": int(max_detour),
    }
    r = requests.post(url, json=payload, timeout=60)
    r.raise_for_status()
    return r.json()

def verdict_html(agent, scores, routes):
    decision = (agent.get("decision") or "").lower()
    cur_name = routes.get("current_summary") or "Current route"
    alt_name = routes.get("candidate_summary") or "Alternative route"
    cur = scores.get("crime_current")
    alt = scores.get("crime_candidate")
    eta = scores.get("eta_change_min")

    time_badge = ""
    time_plain = ""
    if isinstance(eta, (int, float)):
        if eta > 0:
            time_badge = f"<span style='background:#fff0cc;padding:2px 6px;border-radius:6px;'>+{int(eta)} min</span>"
            time_plain = f"+{int(eta)} min longer"
        elif eta < 0:
            time_badge = f"<span style='background:#e7ffe7;padding:2px 6px;border-radius:6px;'>-{abs(int(eta))} min</span>"
            time_plain = f"{abs(int(eta))} min shorter"

    inc_badge = ""
    inc_plain = ""
    if isinstance(cur, (int, float)) and isinstance(alt, (int, float)):
        worse_alt = alt > cur
        color = "#ffe3e3" if worse_alt else "#e7ffe7"
        inc_badge = (
            f"<span style='background:{color};padding:2px 6px;border-radius:6px;'>"
            f"{int(alt)} vs {int(cur)} incidents</span>"
        )
        inc_plain = f"{int(alt)} vs {int(cur)} incidents"

    if decision == "continue":
        html = (
            f"<div style='font-size:1.05rem'>"
            f"<b>🛡️ Keep current:</b> <b>{cur_name}</b> — alternative shows {inc_badge}"
            f"{' and is ' + time_badge + ' longer' if time_badge and eta>0 else (' and is ' + time_badge + ' shorter' if time_badge and eta<0 else '')}."
            f"</div>"
        )
        summary = f"Keep current: {cur_name} — alternative shows {inc_plain}" + (f" and is {time_plain}." if time_plain else ".")
        return html, summary

    if decision == "ask_user":
        html = (
            f"<div style='font-size:1.05rem'>"
            f"<b>🧭 Consider alternative:</b> <b>{alt_name}</b> — {inc_badge}"
            f"{' and ' + time_badge + ' detour' if time_badge else ''}. Reroute now or continue?"
            f"</div>"
        )
        summary = f"Consider alternative: {alt_name} — {inc_plain}" + (f" and {time_plain} detour." if time_plain else ".")
        return html, summary

    html = f"<div style='font-size:1.05rem'>{agent.get('message','')}</div>"
    summary = agent.get("message", "")
    return html, summary

def one_sentence_why(agent, scores):
    cur = scores.get("crime_current")
    alt = scores.get("crime_candidate")
    eta = scores.get("eta_change_min")
    parts = []
    if isinstance(cur, (int, float)) and isinstance(alt, (int, float)):
        parts.append(f"alternative shows **{int(alt)} vs {int(cur)} crime incidents**")
    if isinstance(eta, (int, float)) and eta != 0:
        if eta > 0:
            parts.append(f"and is **+{int(eta)} min** longer")
        else:
            parts.append(f"and is **{abs(int(eta))} min** shorter")
    if not parts:
        rs = agent.get("reasons") or []
        return "Why: " + (rs[0].rstrip(".") if rs else "safety and travel time trade-offs considered.") + "."
    return "Why: " + " ".join(parts) + "."

def lighting_tags(agent_reasons):
    reasons = " ".join(agent_reasons or []).lower()
    better_alt = False
    better_cur = False
    if ("better lighting" in reasons or "better lighting cues" in reasons) and ("candidate" in reasons or "alternative" in reasons):
        better_alt = True
    if "current route appears better lit" in reasons or "current better lit" in reasons:
        better_cur = True
    return better_cur, better_alt

def route_block(title: str, name: str, incidents: int | None, eta: float | None,
                is_night: bool, better_lighting_here: bool, weather_desc: str, precip_mm):
    box = st.container(border=True)
    with box:
        st.markdown(f"**{title}: {name}**")
        bullets = []
        if isinstance(incidents, (int, float)):
            bullets.append(f"• **Safety:** {int(incidents)} crime incidents (last 30 days)")
        else:
            bullets.append("• **Safety:** no recent incident data")
        if is_night:
            bullets.append("• **Lighting:** appears better lit for night" if better_lighting_here else "• **Lighting:** standard or lower lighting")
        else:
            bullets.append("• **Lighting:** daylight (night lighting not applied)")
        if isinstance(eta, (int, float)):
            bullets.append(f"• **ETA:** ~{eta:.1f} min" if isinstance(eta, float) else f"• **ETA:** ~{eta} min")
        bullets.append(f"• **Weather:** {weather_desc} · precip {precip_mm} mm")
        st.write("\n".join(bullets))

# ---------- call & persist ----------
if run_clicked:
    with st.spinner("Scoring routes for safety…"):
        try:
            data = call_backend()
            st.session_state.last_data = data
            st.session_state.last_error = None
        except Exception as e:
            st.session_state.last_error = str(e)
            st.session_state.last_data = None

# ---------- render ----------
err = st.session_state.last_error
data = st.session_state.last_data

if err:
    st.error(f"Request failed: {err}")
elif data:
    routes = data.get("routes", {})
    agent = data.get("agent_response", {})
    weather = data.get("weather", {})
    scores = data.get("scores", {}) or {}
    debug = data.get("debug", {})

    st.subheader("Result")
    html, share_summary = verdict_html(agent, scores, routes)
    st.markdown(html, unsafe_allow_html=True)
    st.caption(one_sentence_why(agent, scores))

    if st.button("Copy summary"):
        st.session_state._copied = share_summary
    if st.session_state.get("_copied"):
        st.code(st.session_state._copied, language=None)

    current_name = routes.get("current_summary") or "Current route"
    alt_name = routes.get("candidate_summary") or "Alternative route"
    cur_eta = routes.get("current_eta_min")
    alt_eta = routes.get("candidate_eta_min")
    cur_inc = scores.get("crime_current")
    alt_inc = scores.get("crime_candidate")
    is_night = bool(scores.get("is_night"))
    better_cur, better_alt = lighting_tags(agent.get("reasons", []))
    wx_desc = f"{weather.get("description","")}"
    precip = weather.get("precip_mm", 0)

    colA, colB = st.columns(2)
    with colA:
        route_block("Current", current_name, cur_inc, cur_eta, is_night, better_cur, wx_desc, precip)
    with colB:
        route_block("Alternative", alt_name, alt_inc, alt_eta, is_night, better_alt, wx_desc, precip)

    # ----- Map -----
    st.markdown("### Map (recommended route thicker)")
    cur_poly = routes.get("current_polyline")
    alt_poly = routes.get("candidate_polyline")
    start_ll = routes.get("start") or {}
    dest_ll  = routes.get("destination") or {}

    def _safe_decode(points):
        try:
            return decode_polyline(points) if points else []
        except Exception:
            return []

    if cur_poly and alt_poly:
        center = [
            float((start_ll.get("lat") or dest_ll.get("lat") or 32.7767)),
            float((start_ll.get("lng") or dest_ll.get("lng") or -96.7970)),
        ]
        m = folium.Map(location=center, zoom_start=14, tiles="OpenStreetMap")

        cur_coords = _safe_decode(cur_poly)
        alt_coords = _safe_decode(alt_poly)

        highlight_alt = (agent.get("decision") == "ask_user")

        if cur_coords:
            folium.PolyLine(
                cur_coords,
                weight=6 if not highlight_alt else 3,
                opacity=0.9 if not highlight_alt else 0.6,
                tooltip=f"Current: {current_name}",
            ).add_to(m)

        if alt_coords:
            folium.PolyLine(
                alt_coords,
                weight=6 if highlight_alt else 3,
                opacity=0.9 if highlight_alt else 0.6,
                tooltip=f"Alternative: {alt_name}",
            ).add_to(m)

        # Markers
        if start_ll.get("lat") and start_ll.get("lng"):
            folium.Marker(
                [float(start_ll["lat"]), float(start_ll["lng"])],
                tooltip="Start",
                icon=folium.Icon(icon="play", prefix="fa"),
            ).add_to(m)
        if dest_ll.get("lat") and dest_ll.get("lng"):
            folium.Marker(
                [float(dest_ll["lat"]), float(dest_ll.get("lng"))],
                tooltip="Destination",
                icon=folium.Icon(icon="flag", prefix="fa"),
            ).add_to(m)

        # Heatmap (weighted by sample counts from backend)
        # Uses the probe samples already provided in debug.route_crime.{current,candidate}.samples
        if show_heatmap:
            try:
                rc = (debug or {}).get("route_crime", {})
                heat_data: list[list[float]] = []

                for label in ("current", "candidate"):
                    entry = rc.get(label, {})
                    for s in entry.get("samples", []):
                        lat = float(s.get("lat"))
                        lon = float(s.get("lon"))
                        count = float(s.get("count", 0))
                        # Weight the point by its incident count; folium HeatMap supports [lat, lon, weight]
                        heat_data.append([lat, lon, max(count, 0.0)])

                if heat_data:
                    HeatMap(
                        data=heat_data,
                        radius=20,     # larger radius for a fuller “density” look
                        blur=15,
                        max_zoom=18,
                        min_opacity=0.3,
                    ).add_to(m)
            except Exception as e:
                st.warning(f"Heatmap unavailable: {e}")

        st_folium(m, width=900, height=540)
    else:
        st.info("No polylines returned. Try again or check backend.")

else:
    with col_status:
        st.caption("Fill start & destination, then click **Recommend safest route**.")
