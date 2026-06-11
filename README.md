# AIM SCADA Edge Agent

Stack de telemetría industrial para nodos Edge (planta). Implementa el patrón **Store & Forward**: captura datos MQTT del SCADA local, los almacena en un buffer InfluxDB y los reenvía a la plataforma cloud de forma robusta.

## Componentes

| Servicio | Imagen | Puerto | Función |
|---|---|---|---|
| `mosquitto_edge` | eclipse-mosquitto:2.0 | 1883 | Broker MQTT local |
| `influxdb_edge` | influxdb:2.7-alpine | 8087 | Buffer de datos (2d retención) |
| `edge_agent` | Python 3.11 custom | 8080 | Agente Store & Forward + UI admin |

## Tópicos MQTT que escucha el agente

### Formato 1 — Estándar AIM (PLC / Simulador / OPC-UA bridge)
```
{TENANT_SLUG}/{SITE_ID}/{area}/{maquina}/{sensor}
```
**Ejemplo:**
```
minera-sofia/SITE_SOFIA/molino/motor_1/corriente
```
**Payload esperado (JSON):**
```json
{
  "value": 142.5,
  "is_online": true,
  "timestamp": "2026-06-11T14:00:00Z"
}
```

### Formato 2 — ESP32 / Arduino / Dispositivos IoT
```
scada/{area}/{maquina}/{grupo_sensores}
```
**Ejemplo:**
```
scada/sala_electrica/tablero_a/energia
```
**Payload esperado (JSON):**
```json
{
  "voltaje": 380,
  "corriente": 12.3,
  "potencia": 4674,
  "is_online": true,
  "timestamp": "2026-06-11T14:00:00Z"
}
```
> Todas las claves con valores numéricos se convierten automáticamente en tags individuales.

## Configuración del PLC / Dispositivo de Campo

Para que el agente reciba datos, el dispositivo SCADA debe publicar al **Mosquitto local** de la PC Edge:

- **Host:** `<IP de la PC Edge en la red de planta>` (ej: `192.168.1.50`)
- **Puerto:** `1883`
- **Sin autenticación** (en redes industriales cerradas)
- **QoS:** 0 o 1 (recomendado QoS 1 para garantía de entrega)

## Deploy con GitHub Actions

Ver `.github/workflows/deploy.yml`. El deploy se activa automáticamente al hacer push a `main`.

## Variables requeridas (GitHub Secrets)

| Secret | Descripción |
|---|---|
| `TENANT_SLUG` | Slug del tenant en la plataforma cloud |
| `SITE_ID` | Código del sitio (ej: `SITE_SOFIA`) |
| `DEVICE_TOKEN` | Token emitido por la plataforma al crear el sitio |
| `CLOUD_API_URL` | URL del endpoint de sync en la nube |
| `INFLUX_ADMIN_USER` | Usuario admin de InfluxDB |
| `INFLUX_ADMIN_PASSWORD` | Contraseña admin de InfluxDB |
| `INFLUX_ORG` | Organización de InfluxDB |
| `INFLUX_BUCKET` | Bucket de InfluxDB |
| `INFLUX_TOKEN` | Token de acceso de InfluxDB |

## UI Admin local

El agente expone una interfaz web en `http://localhost:8080` (o `http://<IP-planta>:8080`) para:
- Ver sensores detectados en tiempo real
- Ajustar filtros deadband por sensor
- Configurar thresholds globales (sync, offline, purga)
