import os
import json
import time
import threading
from datetime import datetime, timezone
import paho.mqtt.client as mqtt
from influxdb_client import InfluxDBClient, Point, WriteOptions
from flask import Flask, request, jsonify

# === Configuración Local (Edge) ===
EDGE_MQTT_HOST = os.getenv("EDGE_MQTT_HOST", "localhost")
EDGE_MQTT_PORT = int(os.getenv("EDGE_MQTT_PORT", 1883))
EDGE_INFLUX_URL = os.getenv("EDGE_INFLUX_URL", "http://localhost:8086")
EDGE_INFLUX_TOKEN = os.getenv("EDGE_INFLUX_TOKEN", "edge-secret-token-12345")
EDGE_INFLUX_ORG = os.getenv("EDGE_INFLUX_ORG", "edge_planta")
EDGE_INFLUX_BUCKET = os.getenv("EDGE_INFLUX_BUCKET", "edge_buffer")

# === Tópicos MQTT adicionales de la planta (raw, separados por coma) ===
# Por defecto "#" = suscribir a TODO (cualquier PLC/SCADA funciona sin configuración).
# Sobreescribir con tópicos específicos si se quiere restringir: "linea1/#,plc2/#"
MQTT_EXTRA_TOPICS = os.getenv("MQTT_EXTRA_TOPICS", "#")

# === Configuración Cloud (Nube) ===
CLOUD_API_URL = os.getenv("CLOUD_API_URL")
DEVICE_TOKEN = os.getenv("DEVICE_TOKEN")
TENANT_SLUG = os.getenv("TENANT_SLUG")
SITE_ID = os.getenv("SITE_ID")

print(f"🏭 Iniciando Edge Agent Store & Forward para {TENANT_SLUG}/{SITE_ID}")

# === Inicializar InfluxDB Local Cliente ===
influx_client = InfluxDBClient(url=EDGE_INFLUX_URL, token=EDGE_INFLUX_TOKEN, org=EDGE_INFLUX_ORG)
# WriteAPI asíncrono para ingesta rapidísima en local
write_api = influx_client.write_api(write_options=WriteOptions(batch_size=500, flush_interval=1000))
query_api = influx_client.query_api()

# ==============================================================================
# HILO 1: STORE (MQTT -> Influx Local Búfer)
# ==============================================================================
# Variable global para trackear la última vez que Mosquitto recibió un mensaje del PLC
last_mqtt_msg_time = time.time()

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("✅ Store Thread conectado al Mosquitto Local")
        # Suscribir al topic jerárquico del tenant/site actual
        topic = f"{TENANT_SLUG}/{SITE_ID}/#"
        client.subscribe(topic)
        # Suscribir al topic genérico SCADA (ej: scada/planta1/tablero1/energia)
        client.subscribe("scada/#")
        print(f"📡 Suscripto a local: {topic} y scada/#")
        # Suscribir a tópicos adicionales de planta (raw) si están configurados
        if MQTT_EXTRA_TOPICS:
            for extra in MQTT_EXTRA_TOPICS.split(","):
                extra = extra.strip()
                if extra:
                    client.subscribe(extra)
                    print(f"📡 Suscripto a tópico extra de planta: {extra}")
    else:
        print(f"❌ Error MQTT: {rc}")

# Memoria caché para Filtro Deadband (Report By Exception)
tag_memory = {}

# Queue de tags que volvieron a ONLINE para notificación inmediata al backend
# (no depende del ciclo de InfluxDB, evita la race condition del puntero last_sync)
import queue
reconnect_queue = queue.Queue()

# Buffers limitados para visualización en tiempo real en UI
viz_buffer_raw = {}
viz_buffer_filtered = {}

def get_deadband_threshold(sensor_name: str, current_value: float, tag_path: str = "") -> tuple[float, float]:
    """
    Calcula el umbral de 'ruido' (Banda Muerta) y el tiempo máximo admisible (Heartbeat).
    Prioriza las configuraciones guardadas manualmente; de lo contrario, usa un Default global limpio.
    """
    print("hola mundo")
    if tag_path and tag_path in custom_deadbands:
        cfg = custom_deadbands[tag_path]
        pct = cfg.get("pct")
        min_abs = cfg.get("min_abs")
        heartbeat = float(cfg.get("heartbeat", 15.0))
        
        if pct is not None and min_abs is None:
            return abs(current_value * float(pct)), heartbeat
        elif min_abs is not None and pct is None:
            return float(min_abs), heartbeat
        else:
            pct_val = float(pct) if pct is not None else 0.0
            abs_val = float(min_abs) if min_abs is not None else 0.0
            return max(abs(current_value * pct_val), abs_val), heartbeat

    # Por defecto no aplicamos filtro de tolerancia (pasa toda la data)
    return 0.0, 15.0

def on_message(client, userdata, msg):
    """Recibe el paquete del simulador (PLC) o ESP32 y lo tira al Búfer (InfluxDB Local)"""
    global last_mqtt_msg_time
    last_mqtt_msg_time = time.time()
    try:
        parts = msg.topic.split('/')
        now_epoch = time.time()

        # --- Intentar parsear el payload (JSON o valor crudo) ---
        raw_payload_str = msg.payload.decode("utf-8").strip()
        try:
            payload = json.loads(raw_payload_str)
        except (json.JSONDecodeError, ValueError):
            payload = None  # No es JSON; puede ser valor numérico crudo

        print(f"📥 MENSAJE RECIBIDO: {msg.topic} | Payload: {raw_payload_str[:80]}")
        
        # Lista de (tag_path, sensor_name, value, timestamp, is_online, area, machine) a procesar
        data_points = []
        
        # Procesamiento para tópico formato ESP32: scada/planta1/tablero1/energia
        if parts[0] == "scada":
            if len(parts) < 4: return
            if not isinstance(payload, dict): return
            area = parts[1]
            machine = parts[2]
            sensor_group = parts[3]
            
            ts = payload.get("timestamp")
            is_online = payload.get("is_online", True)
            
            # Extraer todas las métricas numéricas del JSON
            for key, val in payload.items():
                if key in ("device", "timestamp", "is_online"): continue
                
                try:
                    val_float = float(val)
                except (ValueError, TypeError):
                    continue
                    
                sensor_name = f"{sensor_group}_{key}"
                tag_path = f"{msg.topic}/{key}"
                data_points.append((tag_path, sensor_name, val_float, ts, is_online, area, machine))
                
        # Procesamiento estándar AIM: tenant/site/area/machine/sensor (payload JSON)
        elif len(parts) >= 5 and isinstance(payload, dict):
            area = parts[2]
            machine = parts[3]
            sensor_name = parts[4]
            
            value = payload.get("value")
            is_online = payload.get("is_online", True)
            ts = payload.get("timestamp")
            
            if value is None: return
            try:
                val_float = float(value)
            except (ValueError, TypeError):
                return
            
            data_points.append((msg.topic, sensor_name, val_float, ts, is_online, area, machine))

        # Procesamiento PLANTA RAW: area/machine/sensor o area/machine/sub/sensor
        # Payload = valor numérico crudo (ej: "240.09" o "57.6")
        # Ejemplo: linea1/analizador/Tension_L1N = 240.09
        # Ejemplo: linea1/molinos-martillo/MM1/corriente = 0
        elif len(parts) >= 3:
            try:
                val_float = float(raw_payload_str)
            except (ValueError, TypeError):
                return  # No es numérico ni JSON — ignorar

            area = parts[0]
            if len(parts) == 3:
                # area/machine/sensor
                machine = parts[1]
                sensor_name = parts[2]
            elif len(parts) == 4:
                # area/machine/sub/sensor → machine = "machine_sub"
                machine = f"{parts[1]}_{parts[2]}"
                sensor_name = parts[3]
            else:
                # Más de 4 niveles: todo el medio como machine, último como sensor
                machine = "_".join(parts[1:-1])
                sensor_name = parts[-1]

            data_points.append((msg.topic, sensor_name, val_float, None, True, area, machine))

        else:
            return
            
        # --- 1. GUARDAR EN BUFFER RAW PARA VISUALIZADOR UI ---
        for tag_path, sensor_name, val_float, ts, is_online, area, machine in data_points:
            if tag_path not in viz_buffer_raw:
                viz_buffer_raw[tag_path] = []
            viz_buffer_raw[tag_path].append({"ts": now_epoch * 1000, "val": val_float})
            if len(viz_buffer_raw[tag_path]) > 100:  # Mantenemos los últimos 100 puntos brutos
                viz_buffer_raw[tag_path].pop(0)

            # ---- ACTUALIZACIÓN DE ESTADO VIVO (WATCHDOG MQTT) ----
            if tag_path not in tag_memory:
                tag_memory[tag_path] = {"value": val_float, "ts": now_epoch, "last_heard": now_epoch, "is_online": True}
            else:
                tag_memory[tag_path]["last_heard"] = now_epoch

            # ---- VERIFICAR SI ESTÁ IGNORADO ----
            cfg = custom_deadbands.get(tag_path, {})
            if cfg.get("ignored", False):
                tag_memory[tag_path]["value"] = val_float
                tag_memory[tag_path]["ts"] = now_epoch
                tag_memory[tag_path]["is_online"] = True
                continue

            # ---- FILTRO DE RUIDO (DEADBAND / RBE) ----
            last = tag_memory[tag_path]
            diff = abs(val_float - last["value"])
            time_elapsed = now_epoch - last["ts"]
            
            was_offline = not tag_memory[tag_path].get("is_online", True)
            
            threshold, heartbeat_sec = get_deadband_threshold(sensor_name, val_float, tag_path=tag_path)
            
            # El deadband sólo aplica si el sensor ya estaba online.
            # Si el sensor estaba OFFLINE, ignoramos el deadband completamente y
            # forzamos la escritura para notificar la reconexión.
            if not was_offline and diff < threshold and time_elapsed < heartbeat_sec:
                continue

            tag_memory[tag_path]["value"] = val_float
            tag_memory[tag_path]["ts"] = now_epoch
            tag_memory[tag_path]["is_online"] = True
            
            # --- 2. GUARDAR EN BUFFER FILTERED PARA VISUALIZADOR UI ---
            if tag_path not in viz_buffer_filtered:
                viz_buffer_filtered[tag_path] = []
            viz_buffer_filtered[tag_path].append({"ts": now_epoch * 1000, "val": val_float})
            if len(viz_buffer_filtered[tag_path]) > 100:
                viz_buffer_filtered[tag_path].pop(0)
            # ------------------------------------------

            # Guardar en InfluxDB Búfer (con is_online forzado a True, ya que llegó dato)
            p = Point("scada_tags") \
                .tag("tenant", TENANT_SLUG) \
                .tag("site", SITE_ID) \
                .tag("area", area) \
                .tag("machine", machine) \
                .tag("sensor", sensor_name) \
                .tag("tag_path", tag_path) \
                .tag("is_online", "True") \
                .field("value", float(val_float))
                
            if ts:
                p.time(ts)
                
            write_api.write(bucket=EDGE_INFLUX_BUCKET, record=p)
            
            # Si el sensor estaba offline y acaba de llegar un dato, notificar
            # inmediatamente al backend via el reconnect_queue (sin esperar al
            # ciclo normal de InfluxDB, que podría omitir el punto por el puntero
            # last_sync ya adelantado).
            if was_offline:
                print(f"✅ {tag_path} VOLVIÓ A ONLINE — notificando al backend de forma inmediata")
                reconnect_queue.put({
                    "tag_path": tag_path,
                    "value": val_float,
                    "is_online": True,
                    "timestamp": datetime.fromtimestamp(now_epoch, tz=timezone.utc).isoformat()
                })

    except Exception as e:
        print(f"Error procesando mensaje MQTT: {e}")

mqtt_client = mqtt.Client(client_id="edge_agent_store")
mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message

def run_mqtt():
    while True:
        try:
            mqtt_client.connect(EDGE_MQTT_HOST, EDGE_MQTT_PORT, 60)
            mqtt_client.loop_forever()
        except Exception as e:
            print(f"⚠️ Store Thread buscando Mosquitto... ({e})")
            time.sleep(5)

# ==============================================================================
# HILO WATCHDOG (Control de caída y purga de tags)
# ==============================================================================
def run_sensor_watchdog():
    while True:
        time.sleep(5)
        now = time.time()
        
        OFFLINE_THRESHOLD = float(global_settings.get("offline_threshold", 15.0))
        
        for tag_path, mem in list(tag_memory.items()):
            time_since_last_heard = now - mem.get("last_heard", mem["ts"])
            purge_days = float(global_settings.get("purge_days", 7.0))
            purge_threshold = purge_days * 24 * 3600
            purge_threshold = purge_days * 24 * 3600
            
            # 1. Si está caído pero no lo habíamos marcado como offline aún
            if time_since_last_heard > OFFLINE_THRESHOLD:
                if mem.get("is_online", True):
                    # Lo marcamos como offline en memoria para no spamear
                    tag_memory[tag_path]["is_online"] = False
                    
                    cfg = custom_deadbands.get(tag_path, {})
                    if not cfg.get("ignored", False):
                        # Inyectamos el punto de muerte a la DB local una sola vez
                        parts = tag_path.split('/')
                        p = Point("scada_tags") \
                            .tag("tenant", TENANT_SLUG) \
                            .tag("site", SITE_ID) \
                            .tag("area", parts[2] if len(parts)>2 else "") \
                            .tag("machine", parts[3] if len(parts)>3 else "") \
                            .tag("sensor", parts[4] if len(parts)>4 else "") \
                            .tag("tag_path", tag_path) \
                            .tag("is_online", "False") \
                            .field("value", float(mem["value"]))
                            
                        write_api.write(bucket=EDGE_INFLUX_BUCKET, record=p)
                
                # 2. Si lleva caído demasiado tiempo, es un fantasma: lo purgamos
                elif time_since_last_heard > purge_threshold:
                    del tag_memory[tag_path]
                    
                    # Limpiar también la configuración manual si existía
                    if tag_path in custom_deadbands:
                        del custom_deadbands[tag_path]
                        save_custom_deadbands(custom_deadbands)

# ==============================================================================
# ESTADO PERSISTENTE & CONFIG
# ==============================================================================
# Para el Forwarder se necesita un archivo de estado para saber hasta dónde se mandó
# Utilizamos un directorio /app/state que será mapeado a un volumen persistente en Docker
STATE_DIR = "/app/state"
os.makedirs(STATE_DIR, exist_ok=True)
STATE_FILE = os.path.join(STATE_DIR, "last_sync.txt")
CUSTOM_DEADBANDS_FILE = os.path.join(STATE_DIR, "custom_deadbands.json")
GLOBAL_SETTINGS_FILE = os.path.join(STATE_DIR, "global_settings.json")

def load_global_settings():
    if os.path.exists(GLOBAL_SETTINGS_FILE):
        try:
            with open(GLOBAL_SETTINGS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"offline_threshold": 600.0, "purge_days": 7.0}

def save_global_settings(data):
    with open(GLOBAL_SETTINGS_FILE, "w") as f:
        json.dump(data, f, indent=4)

def load_custom_deadbands():
    if os.path.exists(CUSTOM_DEADBANDS_FILE):
        try:
            with open(CUSTOM_DEADBANDS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_custom_deadbands(data):
    with open(CUSTOM_DEADBANDS_FILE, "w") as f:
        json.dump(data, f, indent=4)

custom_deadbands = load_custom_deadbands()
global_settings = load_global_settings()

# ==============================================================================
# HILO 2: FORWARD (Influx Local -> HTTP Nube)
# ==============================================================================

# Máximo tiempo de atraso antes de resetear el puntero automáticamente.
MAX_BACKLOG_HOURS = 4.0  # horas

def get_last_sync() -> str:
    """
    Lee el puntero last_sync con guard de antigüedad.
    Si tiene más de MAX_BACKLOG_HOURS de atraso, se auto-resetea
    para priorizar el tiempo real sobre el backfill histórico.
    """
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            content = f.read().strip()
            if content and content != "-1h":
                try:
                    from datetime import timedelta
                    pointer_dt = datetime.fromisoformat(content.replace('Z', '+00:00'))
                    now_utc = datetime.now(timezone.utc)
                    age_seconds = (now_utc - pointer_dt).total_seconds()
                    max_backlog_seconds = MAX_BACKLOG_HOURS * 3600
                    if age_seconds > max_backlog_seconds:
                        new_start = (now_utc - timedelta(seconds=max_backlog_seconds)).isoformat()
                        set_last_sync(new_start)
                        print(
                            f"⚠️ last_sync antiguo ({age_seconds/3600:.1f}h atrás). "
                            f"Auto-reset a -{MAX_BACKLOG_HOURS:.0f}h desde ahora."
                        )
                        return new_start
                except Exception:
                    pass
                return content
    return "-1h"

def set_last_sync(iso_ts: str):
    with open(STATE_FILE, "w") as f:
        f.write(iso_ts)

def purge_old_influx_data(purge_days: float):
    """
    Borra datos más viejos que la ventana de retención en InfluxDB.
    Mantiene el buffer liviano y evita que el puntero quede trabado
    por acumulación excesiva de datos históricos.
    """
    try:
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=purge_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        influx_client.delete_api().delete(
            start="1970-01-01T00:00:00Z",
            stop=cutoff,
            predicate='_measurement="scada_tags"',
            bucket=EDGE_INFLUX_BUCKET,
            org=EDGE_INFLUX_ORG,
        )
        print(f"🧹 InfluxDB purgado: datos anteriores a {cutoff} eliminados")
    except Exception as e:
        print(f"⚠️ Error al purgar InfluxDB: {e}")

def run_forwarder():
    import requests
    
    print("🚀 Forward Thread iniciado")
    
    session = requests.Session()
    headers = {
        "X-Device-Token": DEVICE_TOKEN,
        "Content-Type": "application/json"
    }
    
    last_pointer_value = None   # Para detectar estancamiento del puntero
    stall_count = 0             # Ciclos consecutivos sin avance
    STALL_MAX = 5               # Reset si no avanza en 5 ciclos seguidos
    last_purge_time = 0.0       # Última purga automática de InfluxDB
    PURGE_INTERVAL = 6 * 3600  # Purgar cada 6 horas
    
    while True:
        loop_start = time.time()
        
        try:
            # ==================================================================
            # PASO 0 (PRIORITARIO): Drainear el reconnect_queue
            # Si algún sensor pasó de OFFLINE → ONLINE en este ciclo, lo mandamos
            # inmediatamente al backend SIN pasar por el ciclo de InfluxDB.
            # Esto evita la race condition donde el puntero last_sync ya pasó el
            # timestamp del punto de reconexión y InfluxDB no lo incluiría en
            # la próxima consulta.
            # ==================================================================
            reconnect_items = []
            while not reconnect_queue.empty():
                try:
                    reconnect_items.append(reconnect_queue.get_nowait())
                except queue.Empty:
                    break
            
            if reconnect_items:
                scada_connected = (time.time() - last_mqtt_msg_time) < 30.0
                reconnect_body = {
                    "tenant_slug": TENANT_SLUG,
                    "site_id": SITE_ID,
                    "data": reconnect_items,
                    "status": {"is_online": True, "scada_connected": scada_connected}
                }
                print(f"⚡ Notificando reconexión instantánea de {len(reconnect_items)} tags al backend...")
                try:
                    r = session.post(CLOUD_API_URL, json=reconnect_body, headers=headers, timeout=10)
                    if r.status_code == 200:
                        print(f"✅ Reconexión notificada OK ({len(reconnect_items)} tags online)")
                    else:
                        print(f"⚠️ Reconexión: el backend respondió {r.status_code}")
                        # Re-encolar para reintentar en el próximo ciclo
                        for item in reconnect_items:
                            reconnect_queue.put(item)
                except Exception as re:
                    print(f"⚠️ Error enviando reconexión: {re} — reintentando en próximo ciclo")
                    for item in reconnect_items:
                        reconnect_queue.put(item)

            # ==================================================================
            # PASO 1: Ciclo normal de backfill desde InfluxDB
            # ==================================================================
            start_range = get_last_sync()
            
            # Detectar si estamos en modo "backfill" (hay más datos de los que caben en un batch).
            # Cuando el batch devuelve exactamente BATCH_LIMIT registros, hay más por enviar:
            # aumentamos el límite a 5000 para ponernos al día rápidamente.
            BATCH_LIMIT = 5000
            
            if start_range == "-1h":
                time_filter = ""
            else:
                time_filter = f'|> filter(fn: (r) => r._time > time(v: "{start_range}"))'

            query = f'''
                from(bucket: "{EDGE_INFLUX_BUCKET}")
                |> range(start: {('time(v: "' + start_range + '")') if start_range != "-1h" else start_range})
                |> filter(fn: (r) => r._measurement == "scada_tags")
                {time_filter}
                |> sort(columns: ["_time"], desc: false)
                |> limit(n: {BATCH_LIMIT})
            '''
            
            tables = query_api.query(query)
            
            if not tables:
                batch_payload = []
                max_time = None
            else:
                batch_payload = []
                max_time = None
                
                for table in tables:
                    for record in table.records:
                        batch_payload.append({
                            "tag_path": record.values.get("tag_path"),
                            "value": record.get_value(),
                            "is_online": record.values.get("is_online", "True") == "True",
                            "timestamp": record.get_time().isoformat()
                        })
                        r_time = record.get_time().isoformat()
                        if not max_time or r_time > max_time:
                            max_time = r_time
                
                # Avisamos si estamos en modo backfill activo
                if len(batch_payload) >= BATCH_LIMIT:
                    print(f"⚡️ Modo backfill activo: {len(batch_payload)} registros. Vaciando buffer rápidamente...")
            
            # Scada está conectado si recibimos algún mensaje en los últimos 30 segundos
            scada_connected = (time.time() - last_mqtt_msg_time) < 30.0
            
            # Enviar BATCH por HTTP a la Nube (con o sin datos de telemetría para el heartbeat)
            body = {
                "tenant_slug": TENANT_SLUG,
                "site_id": SITE_ID,
                "data": batch_payload,
                "status": {
                    "is_online": True,
                    "scada_connected": scada_connected
                }
            }
            
            if batch_payload:
                print(f"📦 Empaquetados {len(batch_payload)} datos para la Nube...")
            else:
                print(f"💓 Enviando Heartbeat (is_online=True, scada_connected={scada_connected}). Sin datos nuevos.")
            
            resp = session.post(CLOUD_API_URL, json=body, headers=headers, timeout=10)
            if resp.status_code == 200:
                if max_time:
                    print(f"☁️ Enviado OK a Nube. Avanzando puntero a {max_time}")
                    set_last_sync(max_time)
                    # ── Stall detection ──────────────────────────────────────
                    # Si el puntero avanzó → resetear el contador
                    if max_time != last_pointer_value:
                        last_pointer_value = max_time
                        stall_count = 0
                    else:
                        # El puntero no movió (mismo max_time dos veces seguidas)
                        stall_count += 1
                        if stall_count >= STALL_MAX:
                            from datetime import timedelta
                            new_ts = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
                            set_last_sync(new_ts)
                            last_pointer_value = new_ts
                            stall_count = 0
                            print(f"🚨 Puntero estancado {STALL_MAX} ciclos. Forzando reset a -5min.")
            elif resp.status_code == 403:
                print(f"⛔ Error 403: Token denegado. Credencial inválida.")
            else:
                print(f"⛔ Error {resp.status_code} desde la Nube. Reteniendo en Búfer.")
            
        except Exception as e:
            print(f"🔌 Posible corte de Internet o Nube caída. Detalle: {e}")
        
        # ── Purga periódica del buffer InfluxDB ───────────────────────────────
        # Cada PURGE_INTERVAL horas se borran los datos más viejos que purge_days.
        # Esto mantiene el buffer liviano y evita que el puntero quede trabado
        # en el futuro por acumulación excesiva.
        now_ts = time.time()
        if now_ts - last_purge_time > PURGE_INTERVAL:
            purge_days = float(global_settings.get("purge_days", 2.0))
            purge_old_influx_data(purge_days)
            last_purge_time = now_ts
        
        # Compensación activa de reloj
        elapsed = time.time() - loop_start
        current_sync_interval = float(global_settings.get("sync_interval", 5.0))
        sleep_time = max(0.1, current_sync_interval - elapsed)
        time.sleep(sleep_time)

# ==============================================================================
# HILO 3: SERVIDOR WEB FLASK (UI Edge Local)
# ==============================================================================
app = Flask(__name__)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>AIM Edge - Panel Administrador</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-main: #0B1120;
            --bg-card: #111827;
            --bg-card-hover: #1F2937;
            --text-main: #F9FAFB;
            --text-muted: #9CA3AF;
            --accent-primary: #3B82F6;
            --accent-primary-hover: #2563EB;
            --accent-success: #10B981;
            --accent-success-hover: #059669;
            --border-color: #374151;
        }
        body { font-family: 'Inter', system-ui, sans-serif; background: var(--bg-main); color: var(--text-main); padding: 2rem; margin: 0; }
        .container { max-width: 1400px; margin: 0 auto; }
        h1 { color: #38bdf8; display: flex; align-items: center; gap: 12px; font-weight: 700; font-size: 1.8rem; margin-bottom: 2rem;}
        h2 { color: #E5E7EB; font-size: 1.25rem; font-weight: 600; margin-top: 0; }
        
        /* Cards with Glassmorphism touch */
        .card { 
            background: rgba(17, 24, 39, 0.7); 
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255, 255, 255, 0.05); 
            border-radius: 12px; 
            padding: 24px; 
            box-shadow: 0 10px 25px rgba(0,0,0,0.5); 
            margin-bottom: 24px; 
            transition: transform 0.2s ease;
        }
        
        .info-box {
            background: rgba(59, 130, 246, 0.1);
            border-left: 4px solid var(--accent-primary);
            padding: 16px;
            border-radius: 0 8px 8px 0;
            margin-top: 20px;
            font-size: 0.9rem;
            color: #BFDBFE;
            line-height: 1.6;
        }

        .info-box strong { color: #EFF6FF; }

        /* Forms */
        .input-group { display: flex; flex-direction: column; gap: 8px; }
        .input-group label { color: var(--text-muted); font-size: 0.85rem; font-weight: 500; }
        input { 
            background: #0F172A; 
            border: 1px solid var(--border-color); 
            color: white; 
            padding: 10px 12px; 
            border-radius: 6px; 
            font-family: 'Inter', monospace;
            font-size: 0.9rem;
            transition: all 0.2s;
        }
        input:focus { outline: none; border-color: var(--accent-primary); box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.2); }
        
        button { 
            background: var(--accent-primary); 
            color: white; 
            border: none; 
            padding: 10px 20px; 
            border-radius: 6px; 
            cursor: pointer; 
            font-weight: 600; 
            font-size: 0.9rem;
            transition: all 0.2s;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
        }
        button:hover { background: var(--accent-primary-hover); transform: translateY(-1px); }
        button:active { transform: translateY(0); }
        
        .btn-view { background: var(--accent-success); }
        .btn-view:hover { background: var(--accent-success-hover); }
        
        .btn-action {
            display: inline-flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            padding: 8px 10px;
            font-size: 0.75rem;
            min-width: 70px;
            gap: 4px;
            line-height: 1;
        }
        .btn-action span.emoji { font-size: 1.3rem; margin-bottom: 2px; }
        
        .btn-small { padding: 8px 14px; font-size: 0.85rem; }
        
        .success { color: var(--accent-success); margin-left: 10px; display: none; font-weight: 600; font-size: 0.85rem; animation: fadeIn 0.3s ease; }
        
        /* Tables */
        .table-container { overflow-x: auto; }
        table { width: 100%; border-collapse: separate; border-spacing: 0; margin-top: 10px; font-size: 0.9rem; }
        th, td { padding: 16px; text-align: left; border-bottom: 1px solid var(--border-color); vertical-align: top; }
        th { 
            background: rgba(0,0,0,0.2); 
            color: var(--text-muted); 
            font-weight: 600; 
            text-transform: uppercase;
            font-size: 0.75rem;
            letter-spacing: 0.05em;
        }
        th:first-child { border-top-left-radius: 8px; }
        th:last-child { border-top-right-radius: 8px; }
        tr { transition: background 0.2s; }
        tr:hover td { background: rgba(255,255,255,0.02); }
        
        .column-desc { display: block; font-size: 0.75rem; color: #6B7280; margin-top: 6px; font-weight: normal; text-transform: none; letter-spacing: normal; line-height: 1.4;}
        
        .auto-badge { background: rgba(71, 85, 105, 0.3); color: #CBD5E1; padding: 4px 8px; border-radius: 4px; font-size: 0.7rem; font-weight: 600; margin-left: 8px; border: 1px solid #475569; display: inline-block; }
        .custom-badge { background: rgba(245, 158, 11, 0.2); color: #FCD34D; padding: 4px 8px; border-radius: 4px; font-size: 0.7rem; font-weight: 600; margin-left: 8px; border: 1px solid #D97706; display: inline-block; }
        
        @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
    </style>
</head>
<body>
    <div class="container">
        <h1>⚙️ AIM Edge <span style="color: var(--text-muted); font-weight: 400; font-size: 1.4rem;">| Panel Administrador</span></h1>
        
        <div class="card">
            <h2 style="display: flex; justify-content: space-between; align-items: center;">
                Configuración Global del Edge
                <span style="font-size: 0.85rem; font-weight: normal; color: #9CA3AF; background: rgba(0,0,0,0.2); padding: 4px 10px; border-radius: 12px; border: 1px solid #374151;">
                    💾 Memoria en Uso (InfluxDB): <strong id="db-size" style="color: #60A5FA;">-- MB</strong>
                </span>
            </h2>
            <div style="display: flex; gap: 24px; align-items: flex-end; flex-wrap: wrap; margin-top: 20px;">
                <div class="input-group" style="width: 250px;">
                    <label>Frecuencia de Sync (Segundos)</label>
                    <input type="number" step="0.5" id="global_sync" value="5.0" />
                </div>
                <div class="input-group" style="width: 250px;">
                    <label>Tiempo Límite Caída (Segundos)</label>
                    <input type="number" step="0.1" id="global_offline" value="600.0" />
                </div>
                <div class="input-group" style="width: 250px;">
                    <label>Tiempo Purga y Grabado (Días)</label>
                    <input type="number" step="0.1" id="global_purge" value="7.0" />
                </div>
                <div>
                    <button onclick="saveGlobalSettings()">💾 Guardar Globales</button>
                    <span class="success" id="ok-global">✔ Guardado correctamente</span>
                </div>
            </div>
            
            <div class="info-box">
                <p style="margin: 0 0 10px 0;"><strong>Frecuencia de Sync:</strong> Cada cuántos segundos el Edge empaqueta y envía todos los datos recopilados hacia la nube. Valores menores implican más tiempo real pero mayor consumo de ancho de banda.</p>
                <p style="margin: 0 0 10px 0;"><strong>Límite de Caída (Offline Threshold):</strong> Tiempo de silencio absoluto en la red MQTT antes de declarar un sensor como "Muerto" (Offline) hacia la Nube.<br><em style="color: #93C5FD;">Ejemplo: Si lo fijas en 15s y el PLC/Sensor deja de transmitir, al segundo 16 el Edge alertará automáticamente a la Nube sobre la desconexión.</em></p>
                <p style="margin: 0;"><strong>Tiempo de Purga y Grabado en Memoria:</strong> Días de retención del historial en la base de datos local InfluxDB. Además, es el tiempo que debe transcurrir sin recibir datos para que el Edge olvide y elimine permanentemente un sensor desconectado.<br><em style="color: #93C5FD;">Ejemplo: Si lo fijas en 7 días, siempre tendrás la última semana de datos en el Edge, y si desconectas físicamente un motor de la planta, su sensor desaparecerá de este panel tras 1 semana de inactividad.</em></p>
            </div>
        </div>

        <div class="card">
            <div style="margin-bottom: 24px;">
                <h2>Afinación de Sensores (Filtro por Excepción)</h2>
                <p style="color: var(--text-muted); font-size: 0.95rem; margin: 8px 0 0 0;">Configura los umbrales de banda muerta e histéresis individualmente para filtrar el "ruido" de los sensores y optimizar el envío de datos hacia la nube.</p>
            </div>
            
            <div class="table-container">
                <table>
                    <thead>
                        <tr>
                            <th style="width: 25%;">Ruta Física (Tag MQTT)</th>
                            <th style="width: 10%;">Último Valor</th>
                            <th style="width: 14%;">Tolerancia Relativa %<span class="column-desc">Ignora cambios menores a este porcentaje.<br><em>Ej: 5 significa que variaciones menores al 5% no se enviarán a la nube.</em></span></th>
                            <th style="width: 14%;">Tolerancia Absoluta<span class="column-desc">Piso mínimo de cambio requerido.<br><em>Ej: 0.5 unidades de medida. Bloquea fluctuaciones minúsculas.</em></span></th>
                            <th style="width: 14%;">Heartbeat (Seg)<span class="column-desc">Tiempo máx de silencio permitido.<br><em>Ej: 15s. Fuerza el envío del dato para avisar que el sensor sigue vivo, aunque no haya superado la tolerancia.</em></span></th>
                            <th style="width: 23%;">Acciones</th>
                        </tr>
                    </thead>
                    <tbody id="tags-body">
                        <tr><td colspan="6" style="text-align: center; color: #64748b; padding: 40px;">Buscando sensores en la red MQTT local...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>
    </div>

    <!-- Chart Card Container (Starts hidden, moved dynamically) -->
    <div id="viz-card" style="display: none; padding: 24px; background: rgba(15, 23, 42, 0.4); border-radius: 8px; margin: 16px;">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px;">
            <h2 id="viz-title" style="margin: 0; font-size: 1.15rem; color: #E5E7EB;">Comparativa en Tiempo Real</h2>
            <button class="btn-small" style="background: rgba(255, 255, 255, 0.05); border: 1px solid var(--border-color); color: var(--text-muted); cursor: pointer;" onclick="closeViz()">✕ Cerrar Gráfica</button>
        </div>
        <div style="position: relative; height: 350px; width: 100%;">
            <canvas id="vizchart"></canvas>
        </div>
    </div>

    <script>
        let currentVizTag = null;
        let chartInstance = null;

        const safeId = (str) => str.replace(/[^a-zA-Z0-9]/g, '-');

        function handleToleranceChange(path, type) {
            const pctInput = document.getElementById(`pct-${path}`);
            const absInput = document.getElementById(`abs-${path}`);
            if (type === 'pct') {
                if (pctInput.value !== '') {
                    absInput.disabled = true;
                    absInput.style.opacity = '0.3';
                    absInput.value = '';
                } else {
                    absInput.disabled = false;
                    absInput.style.opacity = '1';
                }
            } else if (type === 'abs') {
                if (absInput.value !== '') {
                    pctInput.disabled = true;
                    pctInput.style.opacity = '0.3';
                    pctInput.value = '';
                } else {
                    pctInput.disabled = false;
                    pctInput.style.opacity = '1';
                }
            }
        }

        function closeViz() {
            if (!currentVizTag) return;
            const prevTr = document.getElementById('chart-row-' + safeId(currentVizTag));
            if (prevTr) {
                prevTr.style.display = 'none';
                const td = prevTr.querySelector('td');
                if (td) td.style.borderBottom = 'none';
            }
            
            const vizCard = document.getElementById('viz-card');
            vizCard.style.display = 'none';
            document.body.appendChild(vizCard); // moverlo al body para preservarlo
            
            currentVizTag = null;
            loadTags();
        }

        function startViz(path) {
            if (currentVizTag === path) {
                closeViz();
                return;
            }
            
            if (currentVizTag) {
                const prevTr = document.getElementById('chart-row-' + safeId(currentVizTag));
                if (prevTr) {
                    prevTr.style.display = 'none';
                    const prevTd = prevTr.querySelector('td');
                    if (prevTd) prevTd.style.borderBottom = 'none';
                }
            }

            currentVizTag = path;
            const vizCard = document.getElementById('viz-card');
            vizCard.style.display = 'block';
            document.getElementById('viz-title').innerText = "Comparativa en Tiempo Real: " + path;
            
            const tr = document.getElementById('chart-row-' + safeId(path));
            const td = document.getElementById('chart-td-' + safeId(path));
            
            if (tr && td) {
                td.appendChild(vizCard);
                tr.style.display = 'table-row';
                td.style.borderBottom = '1px solid var(--border-color)';
                
                // Hacer scroll suave hacia la gráfica
                tr.scrollIntoView({ behavior: 'smooth', block: 'center' });
            }
            
            if (chartInstance) { chartInstance.destroy(); }
            
            const ctx = document.getElementById('vizchart').getContext('2d');
            
            // Gradiente para el área del dataset crudo
            const gradientRaw = ctx.createLinearGradient(0, 0, 0, 400);
            gradientRaw.addColorStop(0, 'rgba(239, 68, 68, 0.2)');
            gradientRaw.addColorStop(1, 'rgba(239, 68, 68, 0)');
            
            chartInstance = new Chart(ctx, {
                type: 'line',
                data: { 
                    datasets: [
                        { 
                            label: 'Datos CRUDOS Simulador (Ruido)', 
                            data: [], 
                            borderColor: 'rgba(239, 68, 68, 0.6)', 
                            backgroundColor: gradientRaw,
                            borderWidth: 2,
                            pointRadius: 1,
                            tension: 0.2,
                            fill: true,
                            yAxisID: 'y'
                        },
                        { 
                            label: 'Datos FILTRADOS Cloud (Limpios)', 
                            data: [], 
                            borderColor: 'rgba(16, 185, 129, 1)', 
                            backgroundColor: 'rgba(16, 185, 129, 1)',
                            borderWidth: 3,
                            stepped: true, // Visualización tipo "Retención de Muestra" de banda muerta
                            pointRadius: 4,
                            pointHoverRadius: 6,
                            yAxisID: 'y' 
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    animation: false,
                    interaction: {
                        mode: 'index',
                        intersect: false,
                    },
                    plugins: {
                        legend: {
                            labels: { color: '#E5E7EB', font: { family: 'Inter' } }
                        },
                        tooltip: {
                            backgroundColor: 'rgba(15, 23, 42, 0.9)',
                            titleColor: '#F9FAFB',
                            bodyColor: '#E5E7EB',
                            borderColor: '#374151',
                            borderWidth: 1,
                            padding: 12
                        }
                    },
                    scales: { 
                        x: { type: 'linear', display: false },
                        y: { 
                            display: true, 
                            grid: { color: 'rgba(255, 255, 255, 0.05)' },
                            ticks: { color: '#9CA3AF', font: { family: 'Inter' } }
                        } 
                    }
                }
            });
        }
        
        async function loadGlobalSettings() {
            try {
                const res = await fetch('/api/settings');
                const data = await res.json();
                document.getElementById('global_sync').value = data.sync_interval !== undefined ? data.sync_interval : 5.0;
                document.getElementById('global_offline').value = data.offline_threshold;
                document.getElementById('global_purge').value = data.purge_days;
            } catch(e) {}
        }
        
        async function saveGlobalSettings() {
            const sync = document.getElementById('global_sync').value;
            const offline = document.getElementById('global_offline').value;
            const purge = document.getElementById('global_purge').value;
            await fetch('/api/settings', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ sync_interval: sync, offline_threshold: offline, purge_days: purge })
            });
            const ok = document.getElementById('ok-global');
            ok.style.display = 'inline';
            setTimeout(() => ok.style.display = 'none', 3000);
        }

        async function pollViz() {
            if (!currentVizTag || !chartInstance) return;
            try {
                const res = await fetch('/api/viz?tag=' + encodeURIComponent(currentVizTag));
                const data = await res.json();
                if (data.raw && data.filtered) {
                    chartInstance.data.datasets[0].data = data.raw.map(d => ({x: d.ts, y: d.val}));
                    chartInstance.data.datasets[1].data = data.filtered.map(d => ({x: d.ts, y: d.val}));
                    chartInstance.update();
                }
            } catch (e) {}
        }
        setInterval(pollViz, 1000);

        async function loadTags() {
            try {
                // Pausar el repintado de la tabla si el usuario está tipeando activamente
                if (document.activeElement && document.activeElement.tagName === 'INPUT') return;

                const resp = await fetch('/api/tags');
                const data = await resp.json();
                const tbody = document.getElementById('tags-body');
                
                // Salvaguardar el chart en el body antes de borrar el tbody
                const vizCard = document.getElementById('viz-card');
                if (vizCard && vizCard.parentNode && vizCard.parentNode.tagName === 'TD') {
                    document.body.appendChild(vizCard);
                }
                
                let rows = '';
                for (const tag of data.active_tags) {
                    const hasCustom = !!data.custom_configs[tag.path];
                    const cfg = data.custom_configs[tag.path] || { pct: '', min_abs: '', heartbeat: '' };
                    const isIgnored = !!cfg.ignored;
                    
                    let badge = '';
                    if (isIgnored) {
                        badge = '<span style="background: rgba(239, 68, 68, 0.2); color: #FCA5A5; padding: 4px 8px; border-radius: 4px; font-size: 0.7rem; font-weight: 600; margin-left: 8px; border: 1px solid #DC2626; display: inline-block;">IGNORADO</span>';
                    } else if (hasCustom) {
                        badge = '<span class="custom-badge">CUSTOM</span>';
                    } else {
                        badge = '<span class="auto-badge">GLOBAL</span>';
                    }
                    
                    const tagId = safeId(tag.path);
                    const isChartRow = currentVizTag === tag.path;
                    
                    const displayPct = (cfg.pct !== '' && cfg.pct !== null && cfg.pct !== undefined) ? parseFloat((parseFloat(cfg.pct) * 100).toFixed(4)).toString() : '';
                    const displayAbs = (cfg.min_abs !== '' && cfg.min_abs !== null && cfg.min_abs !== undefined) ? cfg.min_abs : '';
                    
                    const pctDisabled = (isIgnored || displayAbs !== '') ? 'disabled style="opacity: 0.3; width: 80px;"' : 'style="width: 80px;"';
                    const absDisabled = (isIgnored || displayPct !== '') ? 'disabled style="opacity: 0.3; width: 80px;"' : 'style="width: 80px;"';
                    const commonDisabled = isIgnored ? 'disabled style="opacity: 0.3; width: 80px;"' : 'style="width: 80px;"';
                    
                    const rowOpacity = isIgnored ? 'opacity: 0.5;' : '';
                    const ignoreBtnText = isIgnored ? 'Restaurar' : 'Ignorar';
                    const ignoreBtnStyle = isIgnored ? 'background: #D97706; border-color: #B45309; color: white;' : 'background: rgba(239, 68, 68, 0.2); border-color: #DC2626; color: #FCA5A5;';
                    const ignoreEmoji = isIgnored ? '🔄' : '🚫';

                    rows += `
                        <tr style="${rowOpacity}">
                            <td>
                                <div style="font-family: 'Inter', monospace; color: #E2E8F0; word-break: break-all; margin-bottom: 6px; font-size: 0.95rem;">${tag.path}</div>
                                ${badge}
                            </td>
                            <td><strong style="color: #34D399; font-family: 'Inter', monospace; font-size: 1.1rem;">${tag.last_val.toFixed(2)}</strong></td>
                            <td><input type="number" step="0.1" min="0" max="100" id="pct-${tag.path}" value="${displayPct}" placeholder="5" ${pctDisabled} oninput="handleToleranceChange('${tag.path}', 'pct')" /></td>
                            <td><input type="number" step="0.1" id="abs-${tag.path}" value="${displayAbs}" placeholder="0.5" ${absDisabled} oninput="handleToleranceChange('${tag.path}', 'abs')" /></td>
                            <td><input type="number" step="1" id="hb-${tag.path}" value="${cfg.heartbeat}" placeholder="15.0" ${commonDisabled} /></td>
                            <td>
                                <div style="display: flex; gap: 8px; align-items: stretch; flex-wrap: nowrap;">
                                    <button class="btn-action" onclick="saveTag('${tag.path}')" ${isIgnored ? 'disabled style="opacity:0.3;"' : ''}><span class="emoji">💾</span> Guardar</button>
                                    <button class="btn-action btn-view" onclick="startViz('${tag.path}')"><span class="emoji">📊</span> Gráfica</button>
                                    <button id="btn-ignore-${tagId}" data-ignored="${isIgnored}" class="btn-action" style="${ignoreBtnStyle}; white-space: nowrap;" onclick="toggleIgnore('${tag.path}')"><span class="emoji">${ignoreEmoji}</span> ${ignoreBtnText}</button>
                                    <span class="success" id="ok-${tag.path}">✔ Ok</span>
                                </div>
                            </td>
                        </tr>
                        <tr id="chart-row-${tagId}" style="display: ${isChartRow ? 'table-row' : 'none'}; background: rgba(0,0,0,0.15);">
                            <td colspan="6" style="padding: 0; border-bottom: ${isChartRow ? '1px solid var(--border-color)' : 'none'};" id="chart-td-${tagId}"></td>
                        </tr>
                    `;
                }
                
                if (data.active_tags.length === 0) {
                    rows = '<tr><td colspan="6" style="text-align: center; color: #64748b; padding: 40px;">No se han detectado sensores en la red MQTT local aún.</td></tr>';
                }
                
                tbody.innerHTML = rows;
                
                // Restaurar el chart
                if (currentVizTag) {
                    const td = document.getElementById('chart-td-' + safeId(currentVizTag));
                    if (td) {
                        td.appendChild(vizCard);
                    }
                }
            } catch(e) { }
        }
        
        async function saveTag(path) {
            let rawPct = document.getElementById(`pct-${path}`).value;
            let pct = rawPct;
            if (rawPct !== '') {
                const pctVal = parseFloat(rawPct);
                if (pctVal < 0 || pctVal > 100) {
                    alert("La Tolerancia Relativa debe estar entre 0% y 100%.");
                    return;
                }
                pct = (pctVal / 100).toString();
            }
            const abs = document.getElementById(`abs-${path}`).value;
            let hb = document.getElementById(`hb-${path}`).value;
            
            if (parseFloat(hb) > 50.0) {
                alert("El Heartbeat no puede superar los 50.0 segundos para evitar falsas desconexiones en el Watchdog de la Nube.");
                hb = "50.0";
                document.getElementById(`hb-${path}`).value = hb;
            }
            
            const ignoreBtn = document.getElementById(`btn-ignore-${safeId(path)}`);
            const isIgnored = ignoreBtn ? ignoreBtn.getAttribute('data-ignored') === 'true' : false;
            
            await fetch('/api/tags', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ path, pct, min_abs: abs, heartbeat: hb, ignored: isIgnored })
            });
            
            const ok = document.getElementById(`ok-${path}`);
            if(ok) {
                ok.style.display = 'inline';
                setTimeout(() => ok.style.display = 'none', 3000);
            }
            loadTags();
        }

        async function toggleIgnore(path) {
            const ignoreBtn = document.getElementById(`btn-ignore-${safeId(path)}`);
            const isCurrentlyIgnored = ignoreBtn ? ignoreBtn.getAttribute('data-ignored') === 'true' : false;
            const newIgnoredState = !isCurrentlyIgnored;
            
            let pct = document.getElementById(`pct-${path}`).value;
            let abs = document.getElementById(`abs-${path}`).value;
            let hb = document.getElementById(`hb-${path}`).value;
            if (pct !== '') pct = (parseFloat(pct) / 100).toString();

            await fetch('/api/tags', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ path, pct, min_abs: abs, heartbeat: hb, ignored: newIgnoredState })
            });
            loadTags();
        }

        async function loadStats() {
            try {
                const res = await fetch('/api/stats');
                const data = await res.json();
                if (data.status === 'ok') {
                    document.getElementById('db-size').innerText = data.db_size_mb + " MB";
                }
            } catch(e) {}
        }

        loadGlobalSettings();
        loadTags();
        loadStats();
        setInterval(loadTags, 5000); // Refrescar valores
        setInterval(loadStats, 10000); // Refrescar stats cada 10s
    </script>
</body>
</html>
"""

@app.route("/")
def index():
    return HTML_TEMPLATE

@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "POST":
        data = request.json
        try:
            global_settings["sync_interval"] = float(data.get("sync_interval", 5.0))
            global_settings["offline_threshold"] = float(data.get("offline_threshold", 15.0))
            global_settings["purge_days"] = float(data.get("purge_days", 7.0))
            save_global_settings(global_settings)
        except ValueError:
            pass
        return jsonify({"status": "ok"})
    return jsonify(global_settings)

@app.route("/api/tags", methods=["GET"])
def get_tags():
    tags = []
    for path, mem in tag_memory.items():
        # Solamente tags que tengan valores numéricos asociados
        tags.append({"path": path, "last_val": mem["value"]})
    return jsonify({
        "active_tags": sorted(tags, key=lambda x: x["path"]),
        "custom_configs": custom_deadbands
    })

@app.route("/api/tags", methods=["POST"])
def post_tag():
    data = request.json
    path = data.get("path")
    pct = str(data.get("pct", "")).strip()
    min_abs = str(data.get("min_abs", "")).strip()
    hb = str(data.get("heartbeat", "")).strip()
    ignored = bool(data.get("ignored", False))
    
    if path:
        # Si envían todos los campos en blanco y no está ignorado, volver al Global Default
        if pct == "" and min_abs == "" and hb == "" and not ignored:
            if path in custom_deadbands:
                del custom_deadbands[path]
        else:
            try:
                existing = custom_deadbands.get(path, {})
                
                pct_val = float(pct) if pct != "" else None
                min_abs_val = float(min_abs) if min_abs != "" else None
                
                hb_val = float(hb) if hb != "" else existing.get("heartbeat", 15.0)
                if hb_val > 50.0:
                    hb_val = 50.0
                    
                custom_deadbands[path] = {
                    "pct": pct_val,
                    "min_abs": min_abs_val,
                    "heartbeat": hb_val,
                    "ignored": ignored
                }
            except ValueError:
                pass
        save_custom_deadbands(custom_deadbands)
        
    return jsonify({"status": "ok"})

@app.route("/api/viz", methods=["GET"])
def viz_data():
    tag = request.args.get("tag")
    if not tag:
        return jsonify({})
    return jsonify({
       "raw": viz_buffer_raw.get(tag, []),
       "filtered": viz_buffer_filtered.get(tag, [])
    })

@app.route("/api/stats", methods=["GET"])
def api_stats():
    import requests
    try:
        r = requests.get(f"{EDGE_INFLUX_URL}/metrics", timeout=3)
        if r.status_code == 200:
            total_bytes = 0
            for line in r.text.split('\n'):
                if line.startswith('storage_tsm_files_disk_bytes{') or line.startswith('storage_wal_size{'):
                    try:
                        total_bytes += float(line.split(' ')[-1])
                    except:
                        pass
            mb = total_bytes / (1024 * 1024)
            return jsonify({"status": "ok", "db_size_mb": round(mb, 2)})
    except Exception as e:
        print("Error fetch stats:", e)
    return jsonify({"status": "error", "db_size_mb": 0.0})

def run_flask():
    print("🌐 Arrancando UI Administrador Local en http://0.0.0.0:8080")
    app.run(host="0.0.0.0", port=8080, debug=False, use_reloader=False)

if __name__ == "__main__":
    t_store = threading.Thread(target=run_mqtt, daemon=True)
    t_forward = threading.Thread(target=run_forwarder, daemon=True)
    t_flask = threading.Thread(target=run_flask, daemon=True)
    t_watchdog = threading.Thread(target=run_sensor_watchdog, daemon=True)
    
    t_store.start()
    t_forward.start()
    t_flask.start()
    t_watchdog.start()
    
    # Mantener el contenedor vivo
    while True:
        time.sleep(1)
