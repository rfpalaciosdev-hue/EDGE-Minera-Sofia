# Reporte mínimo de salud para AinData

Se añadió un bloque opcional `edge_health` a los POST existentes del agente.
No agrega endpoints, dependencias ni servicios al compose.

## Métricas

- Conexión real del cliente MQTT (`is_connected()`), independiente del silencio
  de los sensores. El campo histórico `status.scada_connected` queda intacto.
- Resultado de `/health` de InfluxDB.
- Tamaño estimado de datos TSM + WAL de la instancia InfluxDB, no espacio libre
  ni capacidad del disco ni tamaño garantizado de un bucket.
- Tiempo activo del proceso.
- Último HTTP, último ACK 200 (datos o heartbeat) y errores consecutivos de envío.

El recolector lee Influx en un hilo independiente cada 30 s, con timeouts de 2 s.
Los envíos adjuntan una copia de la última medición; no consultan Influx por cada
paquete. La fecha `collected_at` corresponde a la muestra. Los datos no
disponibles son null/unknown: un fallo no se informa como cero bytes.

El reporte puede tardar un ciclo en reflejar un envío exitoso. El primer reporte
después del arranque puede no tener todavía un ACK.

## Protección de la telemetría

Si falla el monitor, la telemetría sigue enviándose. Si falla la lectura del
histórico Influx, se intenta un heartbeat vacío de salud y no se avanza el cursor
por ese heartbeat. No se cambiaron captura, filtros, reconexiones, cursores,
resets existentes, watchdog ni purga. Sus riesgos preexistentes siguen pendientes.

No se envían tokens, configuración ni payloads industriales dentro de la salud.

## Estado y despliegue

Cambios preparados y probados en el checkout local. **No desplegados ni enviados
a la rama main.** No se ejecutó el agente contra producción durante las pruebas.

Para visualizar las métricas debe estar desplegada primero la versión compatible
de AinData, con la migración master `i3j4k5l6m7n8`, el receptor de `edge_health`
y la pantalla Empresas → Salud del Edge. Un backend antiguo que ignore campos
adicionales puede seguir aceptando telemetría, pero no guardará estas métricas.

Atención: el workflow de este repositorio despliega automáticamente cuando se
hace push a main con cambios en agent/**. La publicación/despliegue requiere
autorización aparte. No hace falta borrar volúmenes ni modificar la retención.

## Pruebas sin servicios externos

```sh
python3 -m unittest discover -s tests -v
```

Cubren fallos del recolector, datos desconocidos frente a cero, MQTT sin mensajes,
ACK/reintento, reconexiones, Influx caído y el comportamiento original del cursor.
