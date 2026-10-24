# Salud de Windows y almacenamiento Docker

El recolector `scripts/windows/Collect-EdgeHealth.ps1` mide la PC Windows desde
Windows, y el agente adjunta su última muestra al `edge_health` ya existente.
La administración de tenants debe incorporar los campos del contrato de abajo.
Esta implementación del Edge no modifica ni despliega la plataforma cloud.

## Instalación y despliegue

El workflow registra `AinData-EdgeHealth-Minera-Sofia` en el Programador de tareas
después de levantar los servicios. Ejecuta una muestra cada minuto y refresca
almacenamiento cada cinco minutos. La tarea y el script instalado están fuera del
checkout, por lo que sobreviven al siguiente `actions/checkout`.

La carpeta predeterminada es `%LOCALAPPDATA%\AinData\EdgeHealth`, bajo la cuenta
del runner. El workflow escribe su ruta absoluta en `EDGE_HEALTH_DIR` del `.env`.
El agente monta esa carpeta como `/host-health`, de solo lectura; nunca se monta
el socket de Docker en el agente.

Para instalación manual, desde PowerShell 5.1 en la PC Windows, bajo la misma
cuenta que tiene acceso al Docker local:

```powershell
$healthDir = Join-Path $env:LOCALAPPDATA 'AinData\EdgeHealth'
./scripts/windows/Install-EdgeHealthTask.ps1 -OutputDirectory $healthDir
# Agregar al .env EDGE_HEALTH_DIR con esta ruta y barras / antes de recrear:
$healthDir.Replace('\', '/')
docker compose up -d --build
```

El instalador usa la cuenta actual: Interactive para usuarios normales (sin
guardar contraseñas) y ServiceAccount para las cuentas integradas de Windows.
La tarea Interactive se ejecuta mientras esa cuenta mantiene la sesión iniciada,
coherente con Docker Desktop. Bloquear la pantalla conserva la sesión; cerrarla
impide ejecutar esa tarea. Esa cuenta necesita permiso para registrar su tarea
y acceso al contexto Docker local.
No se cambian permisos, no se solicitan credenciales y no se expone una API Docker
por TCP. Antes del registro se obtiene una muestra inmediata. Si Windows deniega
el registro, se muestra una advertencia explícita y el deploy continúa; esa
muestra puntual no confirma monitoreo periódico y caduca a los 180 s. Otros
fallos de instalación también generan una advertencia de Actions sin impedir
las verificaciones posteriores de los servicios. Docker Desktop debe estar funcionando bajo la cuenta
correspondiente; una tarea no inicia Docker Desktop ni garantiza su disponibilidad
después de cerrar sesión. Se rechazan contextos remotos para no mezclar métricas
de distintas máquinas.

Para una instalación manual con permisos suficientes se puede seleccionar
`-UserLogonType S4U`. No se eleva automáticamente el runner ni se modifican
políticas o permisos de tareas existentes. Si Interactive también recibe
`0x80070005`, revisar con el administrador los permisos de registro y el dueño
de la tarea `AinData-EdgeHealth-Minera-Sofia`; no asumir que el monitor se instaló.

Para comprobar la instalación:

```powershell
Get-ScheduledTask -TaskName AinData-EdgeHealth-Minera-Sofia
Get-ScheduledTaskInfo -TaskName AinData-EdgeHealth-Minera-Sofia
Get-Content (Join-Path $healthDir 'host-health.json') -Raw
Invoke-RestMethod http://localhost:8080/api/health | ConvertTo-Json -Depth 16
```

`/health` es una comprobación de vida HTTP del proceso; `/api/health` devuelve la
última muestra en caché (503 hasta la primera muestra). Ninguna ruta dispara un
escaneo Docker. No se agregan tokens, variables de entorno ni payloads de sensores
al reporte. Estas rutas usan la misma interfaz local existente del agente.

Para retirar la tarea, sin tocar la DB ni los volúmenes:

```powershell
Unregister-ScheduledTask -TaskName AinData-EdgeHealth-Minera-Sofia -Confirm:$false
```

### Diagnóstico mediante el runner

El workflow `Diagnostico Edge (solo lectura)` puede ejecutarse desde Actions
cuando no se dispone de acceso a la PC, siempre que el runner esté conectado.
Publica únicamente estados predeterminados de los tres servicios y un resumen
acotado de conectividad: DNS, HTTP, antigüedad del ACK, cantidad de errores y
coincidencia con el sitio esperado. Clasifica los errores del log local dentro
de categorías fijas (timeout, TLS, DNS, JSON o headers inválidos, HTTP, conexión),
y publica sólo categorías y cantidades. Nunca publica líneas de log, mensajes de
excepciones, identificadores de la configuración o credenciales.
Ejecuta una consulta local de salud y un GET
al ping público de la nube desde el contenedor, usando el mismo cliente HTTP.
El ping comprueba conectividad; no valida el token ni confirma ingesta durable.
No reinicia servicios, no modifica cursores y no envía lotes o heartbeats.
Se activa al publicar cambios de sus propios scripts/workflow o manualmente.
Sus rutas no disparan el workflow de despliegue del stack.

## Qué se mide

- **Windows:** CPU agregada, tiempo desde el arranque del sistema, RAM total,
  disponible/usada y unidades de disco locales fijas. Se usan las clases CIM
  `Win32_Processor`, `Win32_OperatingSystem` y `Win32_LogicalDisk`.
- **Contenedores del stack:** estado, healthcheck, reinicios desde la creación,
  CPU y RAM. El porcentaje de CPU Docker puede superar 100 en varios núcleos.
  RAM se obtiene de la salida del CLI y sus unidades se convierten a bytes;
  conserva la precisión redondeada y el criterio de caché del CLI Docker.
- **Almacenamiento:** capa de escritura y logs `json-file` actuales y rotados de
  los tres servicios; volúmenes `aim_agent_state`, `aim_mosquitto_data` y
  `aim_mosquitto_logs`. No se recorre `aim_influxdb_data`.
- **Motor Docker completo:** estimaciones de imágenes, caché de compilación,
  volúmenes y capas de escritura desde `docker system df`. Son cifras de todo
  el motor, pueden incluir otros proyectos y la DB. Se guardan por separado.

La medición de archivos usa contenedores temporales basados en la imagen exacta
del agente en ejecución, sin descargar imágenes. Solo montan volúmenes ya
existentes y directorios de logs, en modo lectura; tienen red desactivada, memoria
y CPU limitadas, sin capacidades añadidas, sin socket Docker y sin acceso al
volumen de InfluxDB. El proceso publica la muestra de Windows y luego la de Docker
antes del escaneo de almacenamiento, para que una operación lenta no las bloquee.

Si Docker Desktop no permite montar sus directorios internos, los logs se
informan como `null` y el almacenamiento como `partial`; los volúmenes se miden
en otra operación y conservan sus valores. Otros drivers de logs también se
informan como desconocidos. No se sustituyen estas mediciones con el límite
configurado de 30 MB: ese límite no es el uso real.

`non_db_mutable_bytes` suma capas de escritura + logs + los tres volúmenes
anteriores. **Excluye el volumen DB, imágenes, caché, metadatos Docker y el archivo
de disco virtual de WSL2.** Es un total del almacenamiento mutable medido del
stack, no toda la huella física de Docker en Windows. Es `null` si falta un
componente; no se presenta una suma parcial como si fuera completa. Las lecturas
no son atómicas entre servicios y pueden variar durante la rotación de logs.
Son bytes lógicos, no bloques físicos; no se debe calcular este valor restando
`influx_data_bytes` al tamaño del disco o al total Docker.

## Contrato para la administración de tenants

`edge_health.schema_version` sigue siendo **1**; los bloques nuevos `host` y
`docker` son opcionales y aditivos. `resources.influx_data_bytes`, `services`,
`forwarding` y `uptime_seconds` mantienen su significado. Ese último campo es
el tiempo activo del proceso; `host.uptime_seconds` es el de la PC.

| Ruta bajo `edge_health` | Campos |
| --- | --- |
| `host` | `status`, `collected_at`, `cpu_percent`, `uptime_seconds` |
| `host.memory` | `total_bytes`, `available_bytes`, `used_bytes`, `used_percent` |
| `host.disks[]` | `drive`, `total_bytes`, `free_bytes`, `used_bytes`, `used_percent` |
| `docker` | `status`, `collected_at`, `containers[]`, `storage` |
| `docker.containers[]` | `name`, `state`, `health`, `restart_count`, `cpu_percent`, `memory_used_bytes`, `memory_limit_bytes` |
| `docker.storage` | `status`, `collected_at`, `size_basis: "logical_bytes"`, `non_db_mutable_bytes` |
| `docker.storage.containers[]` | `name`, `writable_bytes`, `log_bytes` |
| `docker.storage.volumes` | Bytes por nombre: `aim_agent_state`, `aim_mosquitto_data`, `aim_mosquitto_logs` |
| `docker.storage.engine` | `images_bytes_estimate`, `build_cache_bytes_estimate`, `volumes_bytes_estimate`, `containers_bytes_estimate` |

Todos los tamaños y porcentajes pueden ser `null`. Cero es una medición real.
Los estados de muestra son `ok`, `partial`, `unavailable`, `stale`; **`ok` indica
que la lectura está completa, no que los recursos estén por debajo de un umbral.**
Una máquina con disco al 99% puede tener una muestra `ok`.

El agente lee el archivo en su hilo de salud cada 30 s y valida nombres, tipos,
valores finitos y campos admitidos. El JSON se reemplaza de forma atómica;
su tamaño máximo admitido es 256 KiB. Nunca se reenvía el archivo sin filtrar.

Windows y Docker caducan a los **180 segundos**; almacenamiento a los **900
segundos**. Cada bloque tiene su propia fecha. Datos caducados conservan su fecha,
pero sus mediciones se vuelven `null` y el estado cambia a `stale`. Fechas sin
zona horaria o con más de 30 s en el futuro no se consideran actuales.

Para integrar en la plataforma:

1. Extender el receptor de `/api/v1/tenant/edge/sync` para aceptar, validar y
   guardar estos bloques opcionales por tenant **y sitio/dispositivo**. Usar la
   identidad autenticada existente; no confiar en un tenant incluido en el JSON.
2. Conservar tanto la fecha de recepción del servidor como las fechas de cada
   muestra. Un paquete recién recibido puede contener almacenamiento antiguo.
   Agentes anteriores deben seguir funcionando con estos bloques ausentes.
3. Incluir los bloques en la API de administración que hoy devuelve salud Edge.
4. Agregar a la pantalla actual tarjetas de CPU, RAM, discos y contenedores;
   debajo, el desglose de almacenamiento sin DB y los totales del motor Docker.
   Presentar bytes en GiB/MiB y un valor ausente como «Sin datos».
5. Recalcular caducidad al consultar/renderizar, incluso cuando ya no lleguen
   paquetes. La PC apagada o sin conexión se detecta desde la plataforma por el
   último contacto; la última muestra `ok` no prueba que siga encendida.

Umbrales iniciales sugeridos para la UI, configurables: disco ocupado 80%/90%,
RAM 85%/95% y CPU sostenida 85%/95%. Para avisar sobre CPU sostenida hace falta
guardar historial o acumular varias muestras; un único pico no alcanza. El estado
de los contenedores se presenta por separado de los umbrales del host.

## Validación

```sh
python3 -m unittest discover -s tests -v
```

```powershell
./tests/windows/Test-EdgeHealth.ps1
./tests/windows/Test-EdgeHealthInstall.ps1
```

Las pruebas de PowerShell no requieren Pester: analizan ambos scripts y ejecutan
el recolector con CIM/Docker simulados. Cubren unidades, ausencia de métricas,
rotación de caché y reemplazo atómico. Deben ejecutarse también bajo Windows
PowerShell 5.1 para validar el runtime de despliegue; el registro de la tarea,
los permisos reales y los montajes de Docker Desktop requieren esa PC.

Referencias: [almacenamiento Docker](https://docs.docker.com/engine/storage/),
[docker inspect --size](https://docs.docker.com/reference/cli/docker/inspect/),
[docker stats](https://docs.docker.com/reference/cli/docker/container/stats/),
[docker system df](https://docs.docker.com/reference/cli/docker/system/df/),
[seguridad de tareas Windows](https://learn.microsoft.com/en-us/windows/win32/taskschd/security-contexts-for-running-tasks).
