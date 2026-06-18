import streamlit as st
import requests
import json
import pandas as pd
import io
import os
import tempfile
from datetime import datetime, timedelta, date

import gspread
import google.auth.transport.requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# ─── Configuración de página ───────────────────────────────────────────────────
st.set_page_config(
    page_title="MottaPlotter",
    page_icon="📊",
    layout="wide",
)

# ─── Constantes ────────────────────────────────────────────────────────────────
SCOPES = " ".join([
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/spreadsheets",
])

REDIRECT_URI = "https://mottaplotter.streamlit.app/"

APP_CONFIG = {
    "Ripley":         {"companyId": 131, "clickiemotaModel": 318},
    "Santander":      {"companyId": 516, "clickiemotaModel": 318},
    "Easy":           {"companyId": 523, "clickiemotaModel": 318},
    "SMU":            {"companyId": 586, "clickiemotaModel": 318},
    "Paris":          {"companyId": 558, "clickiemotaModel": 318},
    "Jumbo":          {"companyId": 559, "clickiemotaModel": 318},
    "Caja Los Andes": {"companyId": 543, "clickiemotaModel": 318},
    "Clickie":        {"companyId": 33,  "clickiemotaModel": 318},
}

OPCIONES_TIPO = [
    "Extensión Iluminación", "Extensión Clima", "Extensión Iluminación y Clima",
    "Cancelación Extensión Horaria", "Modificación Extensión Horaria",
    "Cambio Horario Base", "Solicitud Fuera de Horario",
    "Entrega de Información", "Asistencia Remota", "Asistencia Técnica",
]

TIPOS_SIN_ADJUNTO = ["Solicitud Fuera de Horario", "Entrega de Información", "Asistencia Técnica"]
TIPOS_CON_FECHAS  = ["Extensión Iluminación", "Extensión Clima", "Extensión Iluminación y Clima",
                     "Cancelación Extensión Horaria", "Modificación Extensión Horaria"]

SHEET_ID   = st.secrets["sheets"]["sheet_id"]
SHEET_NAME = st.secrets["sheets"]["sheet_name"]
CLIENT_ID     = st.secrets["google_oauth"]["client_id"]
CLIENT_SECRET = st.secrets["google_oauth"]["client_secret"]

# ─── OAuth helpers ──────────────────────────────────────────────────────────────
def get_creds_from_session():
    if "google_token" not in st.session_state:
        return None
    token_data = st.session_state["google_token"]
    creds = Credentials(
        token=token_data["token"],
        refresh_token=token_data.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        scopes=SCOPES.split(),
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(google.auth.transport.requests.Request())
        st.session_state["google_token"]["token"] = creds.token
    return creds

# ─── Clickie API ────────────────────────────────────────────────────────────────
@st.cache_data(ttl=3600)
def obtener_token_clickie():
    url = "https://api.clickie.io/v1/public/auth"
    r = requests.post(url,
                      data={"username": st.secrets["clickie"]["username"],
                            "password": st.secrets["clickie"]["password"]},
                      headers={"Content-Type": "application/x-www-form-urlencoded"})
    r.raise_for_status()
    token = r.json().get("data", {}).get("Token")
    if not token:
        raise ValueError("No se pudo obtener token Clickie")
    return token

@st.cache_data(ttl=600)
def cargar_buildings(company_id):
    token = obtener_token_clickie()
    headers = {"Authorization": token, "Content-Type": "application/json"}
    r = requests.get(f"https://api.clickie.io/v1/companies/{company_id}/buildings", headers=headers)
    r.raise_for_status()
    buildings = r.json()["data"]
    return {b["building_name"]: b["id_building"] for b in buildings}

@st.cache_data(ttl=600)
def cargar_devices(company_id, building_id, model_id):
    token = obtener_token_clickie()
    headers = {"Authorization": token, "Content-Type": "application/json"}
    r = requests.get(f"https://api.clickie.io/v1/companies/{company_id}/devices", headers=headers)
    r.raise_for_status()
    devices = r.json()["data"]
    filtered = [d for d in devices
                if d["id_building"] == building_id and d["id_device_model"] == model_id]
    return {(d.get("setup_name") or d["device_identifier"]): d["device_identifier"]
            for d in filtered}

def obtener_config_api(device_id):
    token = obtener_token_clickie()
    headers = {"Authorization": token, "Account": "33"}
    dev_id = device_id.replace("CMWS", "")
    r = requests.get(f"https://v4.api.clickie.io/clickiemotas/{dev_id}/configurations/active",
                     headers=headers)
    if r.status_code != 200:
        raise ValueError(f"Error API (status {r.status_code}): {r.text}")
    data = r.json()
    config_str = data.get("data", {}).get("config")
    if not config_str:
        raise ValueError("Respuesta sin campo 'config'")
    return json.loads(config_str)

# ─── Lógica de horarios ─────────────────────────────────────────────────────────
def time_to_seconds(t):
    h, m, s = map(int, t.split(":"))
    return h * 3600 + m * 60 + s

def invert_periods(periods):
    if not periods:
        return [[0, 86400]]
    periods_sec = sorted([[time_to_seconds(p[0]), time_to_seconds(p[1])]
                          for p in periods if len(p) == 2])
    result, last_end = [], 0
    for start, end in periods_sec:
        if start > last_end:
            result.append([last_end, start])
        last_end = max(last_end, end)
    if last_end < 86400:
        result.append([last_end, 86400])
    return result

def normalize_date_str(date_str):
    parts = date_str.replace("/", "-").split("-")
    d, m = int(parts[0]), int(parts[1])
    return f"{d:02d}-{m:02d}"

def get_special_config_for_day(device, relay, date_str, global_special_days):
    device_special_days = device.get("special_days", {})
    config_x_relay = device["config_x_relay"]
    date_norm = normalize_date_str(date_str)
    special_group = next(
        (group for group, days in global_special_days.items()
         if date_norm in [normalize_date_str(d) for d in days]),
        None
    )
    if not special_group:
        return None
    for special_cfg in device_special_days.values():
        if special_group in special_cfg["day_groups"]:
            relay_cfg = special_cfg["config_x_relay"].get(relay)
            if relay_cfg:
                return relay_cfg
            relay_channels = set(config_x_relay[relay].get("channel_addresses", []) +
                                  config_x_relay[relay].get("registers", []))
            for sg_cfg in special_cfg["config_x_relay"].values():
                if relay_channels & set(sg_cfg.get("channel_addresses", []) +
                                         sg_cfg.get("registers", [])):
                    return sg_cfg
    return None

def get_on_periods(relay_cfg, weekday_num, channel_schedules, any_ww_device):
    config_type = relay_cfg.get("config")
    if config_type == "manual_apagado":
        return [["00:00:00", "23:59:59"]]
    elif config_type == "manual_encendido":
        return [] if not any_ww_device else [["00:00:00", "23:59:59"]]
    elif config_type == "automatico":
        sched_name = relay_cfg.get("schedule")
        sched = channel_schedules.get(sched_name, {})
        for group in sched.values():
            if isinstance(group, dict):
                if weekday_num in group.get("days", []):
                    on_p = group.get("on") or (group.get("status", {}).get("on")
                                               if "status" in group else None)
                    return on_p if on_p is not None else [["00:00:00", "23:59:59"]]
            elif isinstance(group, list):
                return group
        if "on" in sched:
            return sched["on"]
        if "status" in sched and "on" in sched["status"]:
            return sched["status"]["on"]
        if isinstance(sched, list):
            return sched
    return [["00:00:00", "23:59:59"]]

def get_relay_on_intervals(relay_cfg, weekday_num, channel_schedules, any_ww_device):
    periods = get_on_periods(relay_cfg, weekday_num, channel_schedules, any_ww_device)
    config_type = relay_cfg.get("config")
    on_intervals = []
    if any_ww_device:
        if periods != [] and config_type != "manual_apagado":
            if periods == [["00:00:00", "23:59:59"]] or config_type == "manual_encendido":
                on_intervals.append([0, 86400])
            else:
                on_intervals.extend([
                    [time_to_seconds(p[0]), time_to_seconds(p[1])]
                    for p in periods if len(p) == 2
                    and time_to_seconds(p[0]) < time_to_seconds(p[1])
                ])
    else:
        if periods == []:
            on_intervals.append([0, 86400])
        elif periods != [["00:00:00", "23:59:59"]] and config_type != "manual_apagado":
            on_intervals.extend(invert_periods(periods))
    return on_intervals

def diff_intervals(normal_on, special_on):
    result = []
    for ss, se in special_on:
        cur = ss
        for ns, ne in sorted(normal_on):
            if ne <= cur: continue
            if ns > cur: result.append((cur, min(ns, se)))
            cur = max(cur, ne)
            if cur >= se: break
        if cur < se: result.append((cur, se))
    return [(s, e) for s, e in result if e > s]

def merge_intervals(intervals):
    if not intervals: return []
    res = []
    for s, e in sorted(intervals):
        if res and s <= res[-1][1]: res[-1] = (res[-1][0], max(res[-1][1], e))
        else: res.append((s, e))
    return res

# ─── Generador de HTML del gráfico ─────────────────────────────────────────────
def build_chart_html(config_data, sucursal_name, start_date, end_date, solicitud_data=None, view_mode="dia"):
    if isinstance(config_data, list):
        config_data = config_data[0]
    config = config_data.get("config", config_data)
    relay_control    = config["lambda_functions"]["GG_relay_control"]
    channel_schedules = relay_control["channel_schedules"]
    global_special_days = relay_control["special_days"]
    any_ww_device = any(d.get("device_type", "").startswith("WW-")
                        for d in relay_control["devices"].values())

    # Rango de días (extendido ±1 para contexto)
    start_dt = start_date - timedelta(days=1)
    end_dt   = end_date   + timedelta(days=1)
    date_list = [start_dt + timedelta(days=i)
                 for i in range((end_dt - start_dt).days + 1)]

    # Días de extensión (sin el ±1)
    ext_dates = set()
    cur = start_date
    while cur <= end_date:
        ext_dates.add(cur.strftime("%d-%m-%Y"))
        cur += timedelta(days=1)

    _base_colors = ["#378ADD", "#1D9E75", "#7F77DD", "#E67E22", "#E74C3C",
                    "#16A085", "#8E44AD", "#D35400", "#C0392B", "#2980B9"]
    device_names = list(relay_control["devices"].keys())
    device_color_map = {d: _base_colors[i % len(_base_colors)]
                        for i, d in enumerate(device_names)}

    # Helper: calcula relay_rows y ext_delta para un dispositivo+día
    def compute_device_day(device_name, device, date_obj):
        color        = device_color_map[device_name]
        day_str      = date_obj.strftime("%d-%m-%Y")
        weekday_num  = date_obj.isoweekday()
        date_str_raw = f"{date_obj.day}-{date_obj.month}"
        is_ext = day_str in ext_dates

        relay_rows  = {}
        normal_rows = {}
        for relay_key, normal_cfg in device["config_x_relay"].items():
            normal_on = get_relay_on_intervals(normal_cfg, weekday_num,
                                               channel_schedules, any_ww_device)
            normal_rows[relay_key] = normal_on
            special_cfg = get_special_config_for_day(device, relay_key,
                                                      date_str_raw, global_special_days)
            active_cfg  = special_cfg if special_cfg else normal_cfg
            relay_rows[relay_key] = get_relay_on_intervals(active_cfg, weekday_num,
                                                            channel_schedules, any_ww_device)
        ext_delta = []
        if is_ext:
            all_delta = []
            for rk in relay_rows:
                all_delta.extend(diff_intervals(normal_rows[rk], relay_rows[rk]))
            ext_delta = merge_intervals(all_delta)
        return {
            "device_name": device_name, "day_str": day_str,
            "is_ext": is_ext, "color": color,
            "relay_rows": relay_rows, "ext_delta": ext_delta,
        }

    segments = []
    if view_mode == "dia":
        # Vista por día: día → dispositivos → relays
        for date_obj in date_list:
            for device_name, device in relay_control["devices"].items():
                segments.append(compute_device_day(device_name, device, date_obj))
    else:
        # Vista por canal: dispositivo → relay → días
        # Cada segmento es UN relay con sus días como filas
        # El delta se guarda POR FILA (por día) para dibujarlo solo en su fila
        for device_name, device in relay_control["devices"].items():
            color = device_color_map[device_name]
            for relay_key in device["config_x_relay"].keys():
                relay_rows  = {}
                row_deltas  = {}  # day_label → delta de ese día para este relay
                any_ext = False
                for date_obj in date_list:
                    dd = compute_device_day(device_name, device, date_obj)
                    day_label = f"{dd['day_str'][:5]}{'  ★' if dd['is_ext'] else ''}"
                    relay_rows[day_label] = dd["relay_rows"].get(relay_key, [])
                    if dd["is_ext"]:
                        any_ext = True
                        # Delta específico de ESTE relay en ESTE día
                        weekday_num  = date_obj.isoweekday()
                        date_str_raw = f"{date_obj.day}-{date_obj.month}"
                        normal_cfg = device["config_x_relay"][relay_key]
                        normal_on  = get_relay_on_intervals(normal_cfg, weekday_num,
                                                            channel_schedules, any_ww_device)
                        special_on = dd["relay_rows"].get(relay_key, [])
                        row_deltas[day_label] = merge_intervals(
                            diff_intervals(normal_on, special_on)
                        )
                segments.append({
                    "device_name": device_name,
                    "day_str": relay_key,
                    "is_ext": any_ext,
                    "color": color,
                    "relay_rows": relay_rows,
                    "ext_delta": [],          # sin delta de segmento en vista canal
                    "row_deltas": row_deltas, # delta por fila
                })

    # Construir filas planas
    all_rows = []
    seg_meta = []
    for seg in segments:
        keys      = list(seg["relay_rows"].keys())
        first_row = len(all_rows)
        row_deltas = seg.get("row_deltas", {})
        for key in keys:
            all_rows.append({
                "label":        key,
                "day_str":      seg["day_str"],
                "on_intervals": seg["relay_rows"][key],
                "color":        seg["color"],
                "device_name":  seg["device_name"],
                "is_ext":       seg["is_ext"],
                "row_delta":    row_deltas.get(key, []),
            })
        seg_meta.append({
            "first":       first_row,
            "last":        len(all_rows) - 1,
            "is_ext":      seg["is_ext"],
            "ext_delta":   seg["ext_delta"],
            "device_name": seg["device_name"],
            "day_str":     seg["day_str"],
        })

    ext_label_str = (f" · extensión {start_date.strftime('%d/%m')}–{end_date.strftime('%d/%m')}"
                     if start_date and end_date else "")

    # Meta line from solicitud_data
    meta_parts = []
    desc_line  = ""
    if solicitud_data:
        if solicitud_data.get("servicio"):   meta_parts.append(solicitud_data["servicio"])
        if solicitud_data.get("fecha_inicio"): meta_parts.append(solicitud_data["fecha_inicio"])
        if solicitud_data.get("fecha_fin"):  meta_parts.append("→ " + solicitud_data["fecha_fin"])
        if solicitud_data.get("solicitante"): meta_parts.append("· " + solicitud_data["solicitante"])
        if solicitud_data.get("descripcion"): desc_line = solicitud_data["descripcion"]
    meta_str  = "  ".join(meta_parts)
    meta_html = f'<div class="meta">{meta_str}</div>' if meta_str else ""
    desc_html = f'<div class="desc">{desc_line}</div>' if desc_line else ""

    # Leyenda
    legend_html = ""
    for d in device_names:
        c = device_color_map[d]
        legend_html += (f'<span style="display:flex;align-items:center;gap:5px">'
                        f'<span style="width:10px;height:10px;border-radius:2px;'
                        f'background:{c};flex-shrink:0"></span>'
                        f'<span>{d}</span></span>')
    if ext_dates:
        legend_html += ('<span style="display:flex;align-items:center;gap:5px">'
                        '<span style="width:10px;height:10px;border-radius:2px;'
                        'background:#FEF3CD;border:1px dashed #BA7517;flex-shrink:0"></span>'
                        '<span>Extensión</span></span>')

    import json as _json
    rows_json      = _json.dumps(all_rows, default=str)
    seg_json       = _json.dumps(seg_meta, default=str)
    # Build day_groups for alternating backgrounds and separator labels
    _seen, _di, day_groups_list = {}, 0, []
    for r in all_rows:
        if r["day_str"] not in _seen:
            _seen[r["day_str"]] = True
            day_groups_list.append({"day": r["day_str"], "is_ext": r["is_ext"], "day_idx": _di})
            _di += 1
    # Attach firstRow/lastRow
    for dg in day_groups_list:
        dg["firstRow"] = next(i for i,r in enumerate(all_rows) if r["day_str"]==dg["day"])
        dg["lastRow"]  = max(i for i,r in enumerate(all_rows) if r["day_str"]==dg["day"])
    day_groups_json = _json.dumps(day_groups_list, default=str)

    ROW_H    = 26
    BAR_H    = 10
    PAD_TOP  = 8
    PAD_EXT  = 5
    Y_AXIS_W = 220
    PLOT_W   = 1000
    CHART_W  = Y_AXIS_W + PLOT_W + 20
    canvas_h = len(all_rows) * ROW_H + 4 + 18  # +18 = TOP_PAD para primera etiqueta

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:white;font-family:Arial,sans-serif;padding:16px}}
.title{{font-size:13px;font-weight:600;color:#222;margin-bottom:6px}}
.legend{{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:12px;font-size:11px;color:#444}}
.chart-wrap{{display:flex}}
.y-axis{{width:{Y_AXIS_W}px;flex-shrink:0;padding-top:18px}}
.y-row{{height:{ROW_H}px;display:flex;align-items:flex-start;justify-content:flex-end;
        padding-right:8px;padding-top:{PAD_TOP}px;font-size:9px;color:#555;
        white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.x-axis{{display:flex;border-top:1px solid #ddd;margin-top:2px}}
.x-tick{{flex:1;font-size:8px;color:#999;padding-top:2px;text-align:center}}
.meta{{font-size:11px;color:#888;margin-bottom:4px;margin-top:2px}}
.desc{{font-size:11px;color:#aaa;font-style:italic;margin-bottom:8px}}
</style>
</head>
<body>
<div id="captureRoot" style="background:white;display:inline-block;min-width:100%;padding-bottom:4px;padding-right:24px">
<div class="title">Programación configurada — {sucursal_name}{ext_label_str}</div>
{meta_html}
{desc_html}
<div class="legend">{legend_html}</div>
<div class="chart-wrap">
  <div class="y-axis" id="yaxis"></div>
  <div style="position:relative;flex:1">
    <canvas id="cv" width="{PLOT_W}" height="{canvas_h}"></canvas>
    <div class="x-axis" id="xaxis"></div>
  </div>
</div>
<script>
const allRows={rows_json};
const segMeta={seg_json};
const dayGroups={day_groups_json};
const ROW_H={ROW_H},BAR_H={BAR_H},PAD_TOP={PAD_TOP},PAD_EXT={PAD_EXT},W={PLOT_W};
const VIEW_MODE='{view_mode}';
const TOP_PAD=18;

const yAxis=document.getElementById('yaxis');
allRows.forEach((row,i)=>{{
  const prev=i>0?allRows[i-1]:null;
  // Only show day label on first row of that day (across ALL devices)
  const showDay=!prev||prev.day_str!==row.day_str;
  const prefix=VIEW_MODE==='dia'?row.day_str.slice(0,5):row.day_str;
  const dayPart=showDay?('<b>'+prefix+(row.is_ext&&VIEW_MODE==='dia'?' ★':'')+'</b>  '):'<span style="visibility:hidden">'+prefix+' </span>';
  const div=document.createElement('div');
  div.className='y-row';
  div.style.color=row.device_name.toLowerCase().includes('clima')?'#085041':'#0C447C';
  div.innerHTML=dayPart+row.label;
  div.title=row.day_str+' · '+row.device_name+' · '+row.label;
  yAxis.appendChild(div);
}});

const xAxis=document.getElementById('xaxis');
for(let h=0;h<=24;h+=2){{
  const d=document.createElement('div');
  d.className='x-tick';
  d.textContent=String(h).padStart(2,'0')+':00';
  xAxis.appendChild(d);
}}

const cv=document.getElementById('cv');
const ctx=cv.getContext('2d');
function secToX(s){{return s/86400*W;}}
function roundRect(ctx,x,y,w,h,r){{
  ctx.beginPath();
  ctx.moveTo(x+r,y);ctx.lineTo(x+w-r,y);ctx.quadraticCurveTo(x+w,y,x+w,y+r);
  ctx.lineTo(x+w,y+h-r);ctx.quadraticCurveTo(x+w,y+h,x+w-r,y+h);
  ctx.lineTo(x+r,y+h);ctx.quadraticCurveTo(x,y+h,x,y+h-r);
  ctx.lineTo(x,y+r);ctx.quadraticCurveTo(x,y,x+r,y);ctx.closePath();
}}

// Shift everything down to leave room for first group label
ctx.translate(0, TOP_PAD);

// Alternating day backgrounds
const dayBgs=['rgba(245,247,250,0.0)','rgba(210,222,240,0.4)'];
dayGroups.forEach((dg,di)=>{{
  const y=dg.firstRow*ROW_H;
  const h=(dg.lastRow-dg.firstRow+1)*ROW_H;
  ctx.fillStyle=dg.is_ext?'rgba(254,248,220,0.3)':dayBgs[di%2];
  ctx.fillRect(0,y,W,h);
}});

// Grid lines
ctx.strokeStyle='rgba(0,0,0,0.05)';ctx.lineWidth=0.5;
for(let h=0;h<=24;h+=2){{const px=secToX(h*3600);ctx.beginPath();ctx.moveTo(px,0);ctx.lineTo(px,allRows.length*ROW_H);ctx.stroke();}}

// Amber highlights
// Vista 'dia': delta del segmento completo (todas las barritas del dispositivo)
// Vista 'canal': delta por fila individual (solo días extendidos, solo su rango)
if (VIEW_MODE === 'dia') {{
  segMeta.forEach(sm=>{{
    if(!sm.is_ext||!sm.ext_delta||!sm.ext_delta.length)return;
    const amberTop = sm.first*ROW_H + PAD_TOP - PAD_EXT;
    const amberBot = sm.last*ROW_H  + PAD_TOP + BAR_H + PAD_EXT;
    const amberH   = amberBot - amberTop;
    sm.ext_delta.forEach(([s,e])=>{{
      const x0=secToX(s),x1=secToX(e);
      ctx.fillStyle='rgba(254,243,205,0.92)';
      ctx.fillRect(x0,amberTop,x1-x0,amberH);
      ctx.setLineDash([3,3]);ctx.strokeStyle='rgba(186,117,23,0.75)';ctx.lineWidth=1;
      ctx.strokeRect(x0,amberTop,x1-x0,amberH);ctx.setLineDash([]);
    }});
  }});
}} else {{
  allRows.forEach((row,i)=>{{
    if(!row.row_delta||!row.row_delta.length)return;
    const amberTop = i*ROW_H + PAD_TOP - PAD_EXT;
    const amberH   = BAR_H + PAD_EXT*2;
    row.row_delta.forEach(([s,e])=>{{
      const x0=secToX(s),x1=secToX(e);
      ctx.fillStyle='rgba(254,243,205,0.92)';
      ctx.fillRect(x0,amberTop,x1-x0,amberH);
      ctx.setLineDash([3,3]);ctx.strokeStyle='rgba(186,117,23,0.75)';ctx.lineWidth=1;
      ctx.strokeRect(x0,amberTop,x1-x0,amberH);ctx.setLineDash([]);
    }});
  }});
}}

// Bars
allRows.forEach((row,i)=>{{
  const barY=i*ROW_H+PAD_TOP;
  ctx.fillStyle='#e8e8e8';roundRect(ctx,0,barY,W,BAR_H,2);ctx.fill();
  row.on_intervals.forEach(([s,e])=>{{
    if(e<=s)return;
    const x0=secToX(s),x1=secToX(e);
    ctx.fillStyle=row.color;roundRect(ctx,x0,barY,x1-x0,BAR_H,2);ctx.fill();
  }});
}});

// Day separator lines with label pill centered in line
// El primer grupo también lleva etiqueta (sin línea, solo la píldora arriba)
dayGroups.forEach((dg,di)=>{{
  const y=dg.firstRow*ROW_H;
  if(di>0){{
    ctx.strokeStyle='rgba(60,80,120,0.35)';ctx.lineWidth=2;ctx.setLineDash([]);
    ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(W,y);ctx.stroke();
  }}
  const label=(dg.is_ext&&VIEW_MODE==='dia'?'★ ':'')+(VIEW_MODE==='dia'?dg.day.slice(0,6):dg.day);
  const fontSize=10;
  ctx.font='bold '+fontSize+'px Arial';
  const tw=ctx.measureText(label).width;
  const px=10,py=2;
  // El primer grupo dibuja su píldora arriba de las barras (espacio del TOP_PAD)
  const pillY = di===0 ? y - fontSize/2 - py - 2 : y;
  ctx.fillStyle='white';
  ctx.beginPath();ctx.roundRect(W/2-tw/2-px,pillY-fontSize/2-py,tw+px*2,fontSize+py*2,3);ctx.fill();
  ctx.strokeStyle='rgba(60,80,120,0.2)';ctx.lineWidth=0.5;ctx.stroke();
  ctx.fillStyle=dg.is_ext?'#7A4F00':'#3C5080';
  ctx.textBaseline='middle';ctx.fillText(label,W/2-tw/2,pillY);
}});

// Device separators — subtle dotted between devices of same day
segMeta.forEach((sm,i)=>{{
  if(i===segMeta.length-1)return;
  const next=segMeta[i+1];
  if(next.day_str!==sm.day_str)return;
  const y=(sm.last+1)*ROW_H;
  ctx.strokeStyle='rgba(0,0,0,0.18)';ctx.lineWidth=1;ctx.setLineDash([4,3]);
  ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(W,y);ctx.stroke();ctx.setLineDash([]);
}});

// Relay separators (fine dotted)
segMeta.forEach(sm=>{{
  for(let r=sm.first;r<sm.last;r++){{
    const y=(r+1)*ROW_H;
    ctx.strokeStyle='rgba(0,0,0,0.07)';ctx.lineWidth=0.5;ctx.setLineDash([2,4]);
    ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(W,y);ctx.stroke();ctx.setLineDash([]);
  }}
}});

</script>

</div><!-- /captureRoot -->

<div style="position:fixed;top:10px;right:14px;z-index:1000;display:flex;align-items:center;gap:8px">
  <span id="copyMsg" style="font-size:11px;color:#2E7D32;display:none;font-family:Arial,sans-serif;
    background:white;padding:4px 10px;border-radius:4px;box-shadow:0 1px 4px rgba(0,0,0,0.15)">
    ✅ Copiado — pégalo con Ctrl+V
  </span>
  <button id="btnCopy" onclick="copyChart()" style="
    background:#378ADD;color:white;border:none;border-radius:6px;
    padding:8px 14px;font-size:12px;cursor:pointer;font-family:Arial,sans-serif;
    box-shadow:0 2px 6px rgba(0,0,0,0.2)">
    📋 Copiar
  </button>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js"></script>
<script>
async function copyChart() {{
  const btn = document.getElementById('btnCopy');
  const msg = document.getElementById('copyMsg');
  const btnRow = btn.parentElement;
  btn.textContent = '⏳ Capturando...';
  btn.disabled = true;
  // Ocultar el botón durante la captura
  btnRow.style.visibility = 'hidden';
  try {{
    const target = document.getElementById('captureRoot');
    const fullW = target.scrollWidth;
    const fullH = target.scrollHeight;
    const canvas = await html2canvas(target, {{
      backgroundColor: '#ffffff',
      scale: 2,
      useCORS: true,
      logging: false,
      width: fullW,
      height: fullH,
      windowWidth: fullW + 50,
      windowHeight: fullH + 50,
    }});
    btnRow.style.visibility = 'visible';
    canvas.toBlob(async (blob) => {{
      try {{
        await navigator.clipboard.write([new ClipboardItem({{'image/png': blob}})]);
        btn.textContent = '📋 Copiar gráfico al portapapeles';
        btn.disabled = false;
        msg.style.display = 'inline';
        setTimeout(() => msg.style.display = 'none', 4000);
      }} catch(clipErr) {{
        // Fallback: descargar
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url; a.download = 'programacion.png'; a.click();
        URL.revokeObjectURL(url);
        btn.textContent = '📋 Copiar gráfico al portapapeles';
        btn.disabled = false;
        msg.textContent = '💾 Clipboard bloqueado — descargado como PNG';
        msg.style.display = 'inline';
        setTimeout(() => {{
          msg.style.display = 'none';
          msg.textContent = '✅ Copiado — pégalo con Ctrl+V';
        }}, 4000);
      }}
    }}, 'image/png');
  }} catch(e) {{
    btnRow.style.visibility = 'visible';
    btn.textContent = '📋 Copiar gráfico al portapapeles';
    btn.disabled = false;
    alert('Error al capturar: ' + e.message);
  }}
}}
</script>
</body></html>"""
    return html, CHART_W, canvas_h + 60

# ─── Drive helpers ──────────────────────────────────────────────────────────────
def get_or_create_folder(drive_service, name, parent_id=None):
    q = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    if parent_id:
        q += f" and '{parent_id}' in parents"
    res = drive_service.files().list(q=q, spaces="drive", fields="files(id)").execute()
    files = res.get("files", [])
    if files:
        return files[0]["id"]
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        meta["parents"] = [parent_id]
    folder = drive_service.files().create(body=meta, fields="id").execute()
    return folder["id"]

def upload_to_drive(drive_service, file_bytes, filename, mimetype, folder_id):
    media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=mimetype)
    file_meta = {"name": filename, "parents": [folder_id]}
    f = drive_service.files().create(body=file_meta, media_body=media,
                                     fields="id,webViewLink").execute()
    drive_service.permissions().create(
        fileId=f["id"], body={"role": "reader", "type": "anyone"}
    ).execute()
    return f.get("webViewLink", "")

# ─── App principal ──────────────────────────────────────────────────────────────
def extraer_datos_solicitud(img_bytes: bytes, mime_type: str) -> dict:
    """Usa Claude Vision para extraer campos del formulario de solicitud."""
    import anthropic, base64
    client = anthropic.Anthropic(api_key=st.secrets["anthropic"]["api_key"])
    img_b64 = base64.standard_b64encode(img_bytes).decode("utf-8")
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": mime_type, "data": img_b64},
                },
                {
                    "type": "text",
                    "text": (
                        "Este es un formulario de solicitud de extensión horaria en tienda Ripley. "
                        "Extrae los campos visibles y responde ÚNICAMENTE con JSON puro sin markdown ni texto adicional.\n"
                        "Formato exacto (una sola línea):\n"
                        '{"tienda":"R0XX - Nombre","solicitante":"Nombre Apellido","servicio":"Extensión Iluminación",'
                        '"fecha_inicio":"DD/MM/YYYY HH:MM","fecha_fin":"DD/MM/YYYY HH:MM","descripcion":"texto"}\n'
                        "Si un campo no aparece usa null. Responde SOLO con el JSON, empezando con { y terminando con }."
                    )
                }
            ]
        }]
    )
    import json as _json, re as _re
    text = msg.content[0].text.strip()
    # Strip markdown code fences if present
    text = _re.sub(r'^```[a-z]*\n?', '', text)
    text = _re.sub(r'\n?```$', '', text)
    text = text.strip()
    # Extract JSON object if there's surrounding text
    match = _re.search(r'\{.*\}', text, _re.DOTALL)
    if match:
        text = match.group(0)
    return _json.loads(text)


def parse_fecha(fecha_str):
    """Convierte 'DD/MM/YYYY HH:MM' a date."""
    if not fecha_str:
        return None
    try:
        return datetime.strptime(fecha_str.split()[0], "%d/%m/%Y").date()
    except Exception:
        return None


def main():
    creds = get_creds_from_session()
    if not creds:
        st.markdown("## 📊 MottaPlotter")
        st.markdown("Inicia sesión con tu cuenta **@clickie.io** para continuar.")
        import urllib.parse, secrets as _secrets
        state = st.session_state.get("oauth_state") or _secrets.token_urlsafe(16)
        st.session_state["oauth_state"] = state
        params = {
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": SCOPES,
            "access_type": "offline",
            # consent fuerza a Google a entregar refresh_token (sesión persistente)
            "prompt": "consent select_account",
            "state": state,
        }
        auth_url = "https://accounts.google.com/o/oauth2/auth?" + urllib.parse.urlencode(params)
        # Login en la MISMA pestaña (evita dejar ventana vieja abierta)
        st.markdown(
            f'''<a href="{auth_url}" target="_self" style="
                display:inline-block;background:#378ADD;color:white;text-decoration:none;
                padding:10px 24px;border-radius:8px;font-weight:600;font-family:sans-serif">
                Iniciar sesión con Google</a>''',
            unsafe_allow_html=True
        )
        qp = st.query_params
        if "code" in qp and "error" not in qp:
            import httpx
            token_resp = httpx.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": qp["code"],
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "redirect_uri": REDIRECT_URI,
                    "grant_type": "authorization_code",
                },
            )
            if token_resp.status_code == 200:
                token = token_resp.json()
                # Conservar refresh_token previo si Google no devuelve uno nuevo
                prev_refresh = st.session_state.get("google_token", {}).get("refresh_token", "")
                st.session_state["google_token"] = {
                    "token": token["access_token"],
                    "refresh_token": token.get("refresh_token") or prev_refresh,
                }
                user_info = httpx.get(
                    "https://www.googleapis.com/oauth2/v3/userinfo",
                    headers={"Authorization": f"Bearer {token['access_token']}"}
                ).json()
                st.session_state["user_email"] = user_info.get("email", "")
                st.session_state["user_name"]  = user_info.get("name", "")
                st.query_params.clear()
                st.rerun()
            else:
                st.error(f"Error al obtener token: {token_resp.text}")
        return

    user_email = st.session_state.get("user_email", "")
    user_name  = st.session_state.get("user_name", "")

    with st.sidebar:
        st.markdown(f"**{user_name}**")
        st.caption(user_email)
        if st.button("Cerrar sesión"):
            for k in ["google_token", "user_email", "user_name"]:
                st.session_state.pop(k, None)
            st.rerun()
        st.divider()

    st.markdown("## 📊 MottaPlotter")

    col_izq, col_der = st.columns([1, 1], gap="large")

    # ── Columna izquierda: configuración ──
    with col_izq:
        st.subheader("Configuración")

        empresa = st.selectbox("Empresa", list(APP_CONFIG.keys()),
                               key="sel_empresa")
        config = APP_CONFIG[empresa]

        try:
            building_map = cargar_buildings(config["companyId"])
        except Exception as e:
            st.error(f"Error cargando sucursales: {e}")
            return

        sucursal = st.selectbox("Sucursal", [""] + list(building_map.keys()),
                                key="sel_sucursal")

        device_map = {}
        if sucursal and sucursal in building_map:
            try:
                device_map = cargar_devices(
                    config["companyId"],
                    building_map[sucursal],
                    config["clickiemotaModel"]
                )
            except Exception as e:
                st.error(f"Error cargando dispositivos: {e}")

        # FIX 2: primera opción seleccionada por default (sin opción vacía)
        device_keys = list(device_map.keys())
        clickiemota = st.selectbox(
            "Clickiemota",
            device_keys if device_keys else [""],
            key="sel_clickiemota"
        )

        col_f1, col_f2 = st.columns(2)
        with col_f1:
            _fi_default = st.session_state.get("_fecha_inicio_pre")
            fecha_inicio = st.date_input("Fecha inicio", value=_fi_default, key="date_inicio")
        with col_f2:
            _ff_default = st.session_state.get("_fecha_fin_pre")
            fecha_fin = st.date_input("Fecha fin", value=_ff_default, key="date_fin")

        vista = st.radio(
            "Vista del gráfico",
            ["Por día", "Por canal"],
            horizontal=True,
            help="Por día: agrupa canales bajo cada fecha. Por canal: agrupa fechas bajo cada canal (estilo Motta Plotter clásico)."
        )

        json_file = st.file_uploader("O sube un JSON manualmente", type=["json"],
                                     key="json_upload")

        # Feature: imagen de solicitud para Ripley
        solicitud_data = None
        if empresa == "Ripley":
            st.markdown("---")
            solicitud_img = st.file_uploader(
                "📋 Imagen de solicitud (opcional — pre-rellena el formulario)",
                type=["png", "jpg", "jpeg"],
                key="solicitud_img"
            )
            if solicitud_img and st.button("Extraer datos de solicitud", key="btn_extraer"):
                with st.spinner("Analizando imagen con IA..."):
                    try:
                        solicitud_data = extraer_datos_solicitud(
                            solicitud_img.read(), solicitud_img.type
                        )
                        st.session_state["solicitud_data"] = solicitud_data
                        st.success("✅ Datos extraídos")
                        # Pre-rellenar fechas
                        fi = parse_fecha(solicitud_data.get("fecha_inicio"))
                        ff = parse_fecha(solicitud_data.get("fecha_fin"))
                        if fi: st.session_state["_fecha_inicio_pre"] = fi
                        if ff: st.session_state["_fecha_fin_pre"]    = ff
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error extrayendo datos: {e}")

            if "solicitud_data" in st.session_state:
                sd = st.session_state["solicitud_data"]
                with st.expander("Datos extraídos de la solicitud", expanded=True):
                    st.markdown(f"**Tienda:** {sd.get('tienda', '—')}")
                    st.markdown(f"**Solicitante:** {sd.get('solicitante', '—')}")
                    st.markdown(f"**Servicio:** {sd.get('servicio', '—')}")
                    st.markdown(f"**Inicio:** {sd.get('fecha_inicio', '—')}")
                    st.markdown(f"**Fin:** {sd.get('fecha_fin', '—')}")
                    st.markdown(f"**Descripción:** {sd.get('descripcion', '—')}")
                    if st.button("🗑️ Borrar datos extraídos", key="btn_borrar_solicitud"):
                        st.session_state.pop("solicitud_data", None)
                        st.session_state.pop("_fecha_inicio_pre", None)
                        st.session_state.pop("_fecha_fin_pre", None)
                        st.rerun()

        if st.button("Visualizar", type="primary", use_container_width=True, key="btn_visualizar"):
            if not fecha_inicio or not fecha_fin:
                st.error("Selecciona fecha inicio y fecha fin.")
            elif not clickiemota and not json_file:
                st.error("Selecciona una Clickiemota o sube un JSON.")
            else:
                with st.spinner("Obteniendo configuración..."):
                    try:
                        if json_file:
                            config_data = json.load(json_file)
                        else:
                            config_data = obtener_config_api(device_map[clickiemota])

                        fi = fecha_inicio
                        ff = fecha_fin
                        html_chart, chart_w, chart_h = build_chart_html(
                            config_data, sucursal, fi, ff,
                            solicitud_data=st.session_state.get("solicitud_data"),
                            view_mode="dia" if vista == "Por día" else "canal"
                        )
                        st.session_state["chart_html"]   = html_chart
                        st.session_state["chart_w"]      = chart_w
                        st.session_state["chart_h"]      = chart_h
                        st.session_state["config_data"]  = config_data
                        st.session_state["sucursal_sel"] = sucursal
                        st.success("✅ Gráfico generado")
                    except Exception as e:
                        st.error(f"Error: {e}")

    # ── Columna derecha: bitácora ──
    with col_der:
        st.subheader("Bitácora de ticket")

        # FIX 1: keys fijos para preservar selección de texto entre reruns
        ticket_num = st.text_input("N° Ticket", placeholder="Ej: 12345", key="input_ticket")

        # Pre-rellenar tipo desde solicitud extraída
        tipo_default = 0
        if "solicitud_data" in st.session_state:
            servicio = st.session_state["solicitud_data"].get("servicio", "")
            if "Iluminación" in servicio or "Iluminacion" in servicio:
                tipo_default = OPCIONES_TIPO.index("Extensión Iluminación")
            elif "Clima" in servicio:
                tipo_default = OPCIONES_TIPO.index("Extensión Clima")

        tipo_ticket = st.selectbox("Tipo", OPCIONES_TIPO, index=tipo_default, key="sel_tipo")
        estado      = st.selectbox("Estado", ["Resuelto", "Pendiente", "Esperando al Cliente"],
                                   key="sel_estado")

        # Pre-rellenar observaciones desde descripción extraída
        obs_default = ""
        if "solicitud_data" in st.session_state:
            obs_default = st.session_state["solicitud_data"].get("descripcion", "") or ""

        observaciones = st.text_area("Observaciones",
                                     value=obs_default,
                                     placeholder="Detalles o comentarios...",
                                     height=100,
                                     key="input_obs")

        imagen_upload = None
        if tipo_ticket == "Asistencia Remota":
            imagen_upload = st.file_uploader("Imagen adjunta",
                                             type=["png", "jpg", "jpeg", "gif"],
                                             key="img_asistencia")

        if st.button("Crear Ticket", type="primary", use_container_width=True, key="btn_crear"):
            fi = fecha_inicio
            ff = fecha_fin
            if not ticket_num or not sucursal:
                st.error("Completa N° Ticket y Sucursal.")
            elif tipo_ticket not in TIPOS_SIN_ADJUNTO and tipo_ticket != "Asistencia Remota":
                if "chart_html" not in st.session_state:
                    st.error("Primero genera el gráfico.")
                else:
                    _crear_ticket(creds, ticket_num, empresa, sucursal,
                                  estado, tipo_ticket, observaciones,
                                  fi, ff,
                                  building_map.get(sucursal, ""),
                                  user_email, imagen_upload)
            else:
                _crear_ticket(creds, ticket_num, empresa, sucursal,
                              estado, tipo_ticket, observaciones,
                              fi, ff,
                              building_map.get(sucursal, ""),
                              user_email, imagen_upload)

    # ── Gráfico ──
    if "chart_html" in st.session_state:
        st.divider()

        # ── Tarjetas de solicitud (si hay datos extraídos) ──
        sd = st.session_state.get("solicitud_data")
        if sd:
            c1, c2, c3, c4, c5 = st.columns(5)
            with c1:
                st.markdown(
                    f'''<div style="background:#1e2130;border-radius:8px;padding:10px 14px">
                    <div style="font-size:10px;color:#aaa;margin-bottom:3px">Tienda</div>
                    <div style="font-size:13px;font-weight:600;color:#e8e8e8">{sd.get("tienda","—")}</div>
                    </div>''', unsafe_allow_html=True)
            with c2:
                st.markdown(
                    f'''<div style="background:#1e2130;border-radius:8px;padding:10px 14px">
                    <div style="font-size:10px;color:#aaa;margin-bottom:3px">Solicitante</div>
                    <div style="font-size:13px;font-weight:600;color:#e8e8e8">{sd.get("solicitante","—")}</div>
                    </div>''', unsafe_allow_html=True)
            with c3:
                st.markdown(
                    f'''<div style="background:#1e2130;border-radius:8px;padding:10px 14px">
                    <div style="font-size:10px;color:#aaa;margin-bottom:3px">Servicio</div>
                    <div style="font-size:13px;font-weight:600;color:#378ADD">{sd.get("servicio","—")}</div>
                    </div>''', unsafe_allow_html=True)
            with c4:
                st.markdown(
                    f'''<div style="background:#1e2130;border-radius:8px;padding:10px 14px">
                    <div style="font-size:10px;color:#aaa;margin-bottom:3px">Inicio</div>
                    <div style="font-size:13px;font-weight:600;color:#e8e8e8">{sd.get("fecha_inicio","—")}</div>
                    </div>''', unsafe_allow_html=True)
            with c5:
                st.markdown(
                    f'''<div style="background:#1e2130;border-radius:8px;padding:10px 14px">
                    <div style="font-size:10px;color:#aaa;margin-bottom:3px">Término</div>
                    <div style="font-size:13px;font-weight:600;color:#e8e8e8">{sd.get("fecha_fin","—")}</div>
                    </div>''', unsafe_allow_html=True)
            # Descripción si existe
            if sd.get("descripcion"):
                st.caption(f"📝 {sd.get('descripcion')}")
            st.markdown("<div style='margin-top:8px'></div>", unsafe_allow_html=True)

        st.subheader(f"Programación — {st.session_state.get('sucursal_sel', '')}")
        st.components.v1.html(
            st.session_state["chart_html"],
            height=min(st.session_state["chart_h"] + 70, 900),
            scrolling=True,
        )


def _crear_ticket(creds, ticket_num, empresa, sucursal, estado, tipo_ticket,
                  observaciones, fecha_inicio, fecha_fin, id_building,
                  user_email, imagen_upload):
    with st.spinner("Creando ticket..."):
        try:
            drive_service = build("drive", "v3", credentials=creds)
            parent_id  = get_or_create_folder(drive_service, "Colbún")
            child_id   = get_or_create_folder(drive_service, "Tickets Colbún", parent_id)

            link_adjunto = ""

            # Adjunto: imagen manual (Asistencia Remota) o gráfico HTML→PNG
            if tipo_ticket == "Asistencia Remota" and imagen_upload:
                img_bytes = imagen_upload.read()
                link_adjunto = upload_to_drive(
                    drive_service, img_bytes,
                    f"ticket_{ticket_num}_{sucursal}_{imagen_upload.name}",
                    imagen_upload.type, child_id
                )
            elif tipo_ticket not in TIPOS_SIN_ADJUNTO and "chart_html" in st.session_state:
                # Exportar HTML a PNG via st.components no es posible en server-side,
                # subimos el HTML directamente como archivo visualizable
                html_bytes = st.session_state["chart_html"].encode("utf-8")
                fname = (f"ticket_{ticket_num}_{sucursal}_programacion.html"
                         if tipo_ticket != "Cambio Horario Base"
                         else f"ticket_{ticket_num}_{sucursal}_horario_base.html")
                link_adjunto = upload_to_drive(
                    drive_service, html_bytes, fname, "text/html", child_id
                )

            # Nombre del programador desde email
            email_prefix = user_email.split("@")[0]
            programador  = " ".join(p.capitalize() for p in email_prefix.split("."))

            fecha_hoy          = datetime.now().strftime("%d/%m/%Y")
            fecha_inicio_str   = fecha_inicio.strftime("%d/%m/%Y") if fecha_inicio and tipo_ticket in TIPOS_CON_FECHAS else ""
            fecha_fin_str      = fecha_fin.strftime("%d/%m/%Y")    if fecha_fin    and tipo_ticket in TIPOS_CON_FECHAS else ""

            gc        = gspread.authorize(creds)
            worksheet = gc.open_by_key(SHEET_ID).worksheet(SHEET_NAME)
            next_row  = len(worksheet.get_all_values()) + 1
            link_formula = (f'=SI(ESBLANCO(B{next_row});"";HIPERVINCULO('
                            f'"https://app.hubspot.com/help-desk/46669151/view/111672869/ticket/"'
                            f' & B{next_row}; "TK" & B{next_row}))')

            new_row = [
                fecha_hoy, ticket_num, empresa, sucursal, estado,
                fecha_inicio_str, fecha_fin_str, tipo_ticket, programador,
                "", link_formula, link_adjunto, "", observaciones, "", str(id_building)
            ]
            worksheet.append_row(new_row, value_input_option="USER_ENTERED", table_range="A1")
            st.success(f"✅ Ticket '{ticket_num}' agregado a la bitácora.")
        except Exception as e:
            st.error(f"Error al crear ticket: {e}")

if __name__ == "__main__":
    main()
