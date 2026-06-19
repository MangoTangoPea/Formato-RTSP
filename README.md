# Demo RTSP — Transmisión de vídeo con protocolo RTSP real (Emisor / Receptor)

Sistema de transmisión de vídeo tipo cliente-servidor que funciona en red local entre dos computadoras utilizando el protocolo **RTSP** (Real Time Streaming Protocol, RFC 2326).

## Arquitectura del sistema

```
  ┌──────────┐   frames crudos   ┌─────────┐   RTSP/RTP    ┌──────────┐
  │  Cámara  │ ────────────────► │ FFmpeg  │ ────────────► │ MediaMTX │
  │ (OpenCV) │   vía pipe/stdin  │ (H.264) │  ANNOUNCE +   │ (server  │
  │          │                   │         │  RECORD        │  RTSP)   │
  └──────────┘                   └─────────┘               └────┬─────┘
     emisor.py                                                  │
                                                      rtsp://<IP>:8554/camara
                                                                │
                                                           ┌────▼─────┐
                                                           │ Receptor │
                                                           │ (OpenCV) │
                                                           └──────────┘
                                                            receptor.py
```

### ¿Qué es RTSP?

**RTSP** (Real Time Streaming Protocol) es un protocolo de capa de aplicación diseñado para controlar la entrega de datos en tiempo real. A diferencia de HTTP, RTSP no transporta los datos multimedia directamente — usa **RTP** (Real-time Transport Protocol) para eso.

**Analogía simple:** RTSP es como el control remoto de un reproductor de vídeo: le dices "play", "pause", "stop". RTP es el cable que lleva el vídeo real.

| Protocolo | Función | Transporte | Puerto por defecto |
|-----------|---------|------------|-------------------|
| RTSP      | Señalización (DESCRIBE, SETUP, PLAY, TEARDOWN) | TCP | 554 / 8554 |
| RTP       | Transporte de paquetes de vídeo H.264 | UDP o TCP entrelazado | Negociado en SETUP |
| RTCP      | Control de calidad (informes de recepción) | UDP | RTP + 1 |

### Componentes

| Componente | Rol | Descripción |
|------------|-----|-------------|
| **`emisor.py`** | Publicador RTSP | Captura la cámara con OpenCV, envía frames crudos a FFmpeg vía stdin. FFmpeg codifica en H.264 y publica al servidor MediaMTX. |
| **`receptor.py`** | Cliente RTSP | Se conecta a la URL RTSP, recibe paquetes RTP, decodifica H.264 y muestra el vídeo en una ventana OpenCV. |
| **MediaMTX** | Servidor RTSP | Servidor ligero que redistribuye el flujo a múltiples clientes. Se descarga automáticamente al ejecutar el emisor. |
| **FFmpeg** | Codificador | Convierte vídeo crudo BGR a H.264 y lo empaqueta en RTSP/RTP. Debe instalarse manualmente. |

---

## Requisitos previos

### Software necesario

| Requisito | Versión mínima | Cómo verificar | Instalación |
|-----------|---------------|-----------------|-------------|
| Python | 3.8+ | `python --version` | [python.org](https://www.python.org/downloads/) |
| FFmpeg | 4.0+ | `ffmpeg -version` | Ver sección abajo |
| pip | (incluido con Python) | `pip --version` | — |

> **Nota:** MediaMTX se descarga **automáticamente** la primera vez que ejecutas `emisor.py`. No necesitas instalarlo manualmente.

### Instalar FFmpeg en Windows

FFmpeg es un programa externo que se debe descargar e instalar manualmente:

1. Ve a [https://www.gyan.dev/ffmpeg/builds/](https://www.gyan.dev/ffmpeg/builds/)
2. Descarga **ffmpeg-release-essentials.zip** (la versión "essentials" es suficiente)
3. Extrae el ZIP en una ubicación permanente, por ejemplo: `C:\ffmpeg`
4. Añade la carpeta `bin` al PATH del sistema:

```powershell
# Verificar que FFmpeg funcione:
ffmpeg -version
```

**Para añadir FFmpeg al PATH permanentemente:**

1. Busca "Variables de entorno" en el menú de inicio de Windows
2. En "Variables del sistema", busca `Path` y haz clic en "Editar"
3. Haz clic en "Nuevo" y añade la ruta: `C:\ffmpeg\bin` (ajusta según dónde lo extrajiste)
4. Acepta y reinicia la terminal

### Dependencias de Python

```powershell
cd "C:\Users\Luis Fdo\Documents\GitHub\Demo RTSP"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Las dependencias de Python son:
- **`opencv-python`**: captura de cámara, decodificación H.264, y visualización de vídeo
- **`numpy`**: manipulación de arrays de píxeles (requerido por OpenCV)

Para salir del entorno virtual:

```powershell
deactivate
```

---

## ¿Qué es una dirección IP? (Explicación simple)

Imagina que tienes dos casas y quieres enviar una carta de una a otra. Necesitas saber la dirección de la otra casa, ¿verdad?

**Las computadoras funcionan igual:** cada computadora en una red tiene una "dirección" llamada **dirección IP**. Esta dirección permite que una computadora encuentre y se comunique con otra.

**Ejemplo de dirección IP:** `192.168.1.42`

Una dirección IP (versión 4 / IPv4) es un número de 4 partes separadas por puntos. Cada parte es un número entre 0 y 255.

**¿Por qué usamos IPv4?**
- IPv4 significa **Internet Protocol version 4**.
- Es la versión más común de direcciones IP usadas en redes locales y la mayoría de las redes domésticas.
- En este proyecto la usamos porque los equipos de la red local suelen comunicarse con direcciones IPv4 como `192.168.x.x`.

**Qué significa cada parte de `192.168.1.42`:**
- `192.168`: identifica la red local o subred.
- `1`: identifica el segmento interno dentro de esa red.
- `42`: identifica el equipo específico dentro de esa red.

> Nota: cada bloque se llama "octeto" porque es un valor de 8 bits, y 4 octetos suman 32 bits en total.

### ¿Cómo encontrar la dirección IP del emisor?

En la máquina donde está el **emisor** (la que tiene la cámara), abre PowerShell y ejecuta:

```powershell
ipconfig
```

Verás algo como esto:

```
Adaptador de Ethernet:
   Dirección IPv4. . . . . . . . . . : 192.168.1.42
   Máscara de subred . . . . . . . . : 255.255.255.0
   Puerta de enlace predeterminada . : 192.168.1.1
```

**Busca la línea "Dirección IPv4"** y copia ese número (en este ejemplo `192.168.1.42`).

**Nota importante:**
- Si tu computadora está conectada por **WiFi**, busca bajo la sección "Adaptador inalámbrico".
- Si está conectada por **cable Ethernet**, busca bajo esa sección.
- El número que ves **cambia solo si reinicias la computadora o desconectas de la red**. Normalmente es el mismo cada vez.

### Verificar que ambas máquinas están en la misma red

Para que el receptor pueda encontrar al emisor, **ambas máquinas deben estar en la misma red local (WiFi o cable)**. Usa este comando en el **receptor** para verificar que ve al emisor:

```powershell
ping 192.168.1.42
```

Reemplaza `192.168.1.42` con la IP real del emisor.

Si ves mensajes como `Reply from 192.168.1.42`, significa que la red funciona. ✓

Si ves `Timed out` o `no se alcanzó el host`, significa que las máquinas no se ven. En ese caso:
1. Verifica que ambas máquinas estén en la misma red WiFi o cable.
2. Comprueba que estás usando la IP correcta del emisor.
3. Desactiva el firewall de Windows temporalmente para probar.

---

## Cómo ejecutar

### Paso 1: Encuentra la IP del emisor

En la máquina con la cámara, abre PowerShell y escribe:

```powershell
ipconfig
```

Busca la línea **"Dirección IPv4"** y copia ese número. Por ejemplo: `192.168.1.42`

### Paso 2: Ejecuta el emisor (máquina con la cámara)

```powershell
cd "C:\ruta\al\proyecto"
.\.venv\Scripts\Activate.ps1
python emisor.py
```

Deberías ver algo como:

```
═══════════════════════════════════════════════════════════
  EMISOR RTSP — Transmisión de cámara local
═══════════════════════════════════════════════════════════

[1/4] Verificando FFmpeg ...
  ✓ FFmpeg encontrado

[2/4] Preparando servidor RTSP (MediaMTX) ...
  ↓ Descargando MediaMTX v1.12.2 ...       ← (solo la primera vez)
  ✓ MediaMTX instalado en: ...\mediamtx\mediamtx.exe
  → Iniciando MediaMTX en el puerto 8554 ...
  ✓ MediaMTX iniciado (PID: 12345)

[3/4] Abriendo cámara (índice 0) ...
  ✓ Cámara abierta: 640x480 @ 30 FPS

[4/4] Iniciando transmisión RTSP ...
  ✓ Transmisión RTSP activa

═══════════════════════════════════════════════════════════
  URL RTSP del flujo:
  → rtsp://192.168.1.42:8554/camara

  Los receptores deben conectarse a esta URL.
  También puedes probar con VLC: Medio → Abrir ubicación de red
═══════════════════════════════════════════════════════════

  Presiona Ctrl+C para detener la transmisión.
```

**Opciones del emisor:**

```powershell
python emisor.py --puerto 8554     # Puerto RTSP (por defecto: 8554)
python emisor.py --cam 1           # Usar segunda cámara
python emisor.py --calidad 4000    # Mayor calidad de vídeo (kbps)
python emisor.py --listar-camaras  # Ver cámaras disponibles
```

### Paso 3: Ejecuta el receptor (otra máquina)

En la otra computadora, abre PowerShell:

```powershell
cd "C:\ruta\al\proyecto"
.\.venv\Scripts\Activate.ps1
python receptor.py 192.168.1.42
```

O con la URL RTSP completa:

```powershell
python receptor.py rtsp://192.168.1.42:8554/camara
```

Deberías ver:

```
═══════════════════════════════════════════════════════════
  RECEPTOR RTSP — Visualización de flujo en tiempo real
═══════════════════════════════════════════════════════════

  URL RTSP: rtsp://192.168.1.42:8554/camara
  Transporte: TCP entrelazado (interleaved)
  Presiona 'q' en la ventana para salir.

  [Intento 1] Conectando a rtsp://192.168.1.42:8554/camara ...
  ✓ Conectado al flujo RTSP
  Resolución: 640x480
  FPS del flujo: 30.0

  Recibiendo vídeo en tiempo real ...
```

Y una ventana mostrará el vídeo en tiempo real con un indicador de FPS y estado.

**Opciones del receptor:**

```powershell
python receptor.py 192.168.1.42             # IP con puerto por defecto (8554)
python receptor.py 192.168.1.42 9554        # IP con puerto personalizado
python receptor.py rtsp://IP:PUERTO/camara  # URL RTSP completa
python receptor.py --sin-hud IP             # Sin overlay de información
```

### Paso 4: Detener los programas

- **Receptor:** presiona `q` en la ventana del vídeo, o `Ctrl+C` en la terminal.
- **Emisor:** presiona `Ctrl+C` en PowerShell. Esto detiene FFmpeg, MediaMTX y libera la cámara automáticamente.

### Verificar con VLC (opcional)

Puedes probar el flujo RTSP con cualquier reproductor compatible. En VLC:

1. Abre VLC
2. Ve a **Medio → Abrir ubicación de red** (o `Ctrl+N`)
3. Escribe: `rtsp://192.168.1.42:8554/camara`
4. Haz clic en **Reproducir**

---

## Resolución de problemas comunes

### "FFmpeg no está instalado o no está en el PATH"

**Causa:** FFmpeg no se encontró en las variables de entorno del sistema.

**Solución:**
1. Descarga FFmpeg desde [gyan.dev/ffmpeg/builds](https://www.gyan.dev/ffmpeg/builds/)
2. Extrae en `C:\ffmpeg`
3. Añade `C:\ffmpeg\bin` al PATH del sistema
4. Reinicia la terminal y verifica con `ffmpeg -version`

### "No se pudo abrir la cámara"

**Solución:**
1. Asegúrate de que la cámara esté conectada y no en uso por otro programa (Zoom, Teams, etc.)
2. Lista las cámaras disponibles:
   ```powershell
   python emisor.py --listar-camaras
   ```
3. Intenta con otro índice:
   ```powershell
   python emisor.py --cam 1
   ```

### "No se pudo conectar al flujo RTSP" (en el receptor)

**Causa:** El receptor no puede alcanzar al servidor RTSP del emisor.

**Soluciones:**
1. Verifica que el emisor esté ejecutándose y muestre la URL RTSP.
2. Verifica que ambas máquinas estén en la **misma red WiFi o cable**.
3. Verifica que usaste la **IP correcta** del emisor.
4. Prueba hacer ping desde el receptor:
   ```powershell
   ping 192.168.1.42
   ```
5. Verifica que el puerto 8554 no esté bloqueado por el firewall:
   ```powershell
   Test-NetConnection 192.168.1.42 -Port 8554
   ```
   Si `TcpTestSucceeded` es `False`, permite el acceso en el firewall de Windows.

### "MediaMTX terminó inesperadamente"

**Causa:** El puerto 8554 ya está en uso, o el firewall bloqueó el proceso.

**Soluciones:**
1. Verifica que no haya otra instancia de MediaMTX corriendo:
   ```powershell
   Get-Process mediamtx -ErrorAction SilentlyContinue | Stop-Process
   ```
2. Usa un puerto diferente:
   ```powershell
   python emisor.py --puerto 9554
   # Y en el receptor:
   python receptor.py 192.168.1.42 9554
   ```
3. Si el firewall de Windows muestra un diálogo pidiendo permiso, haz clic en **Permitir acceso**.

### El vídeo es muy lento o entrecortado

**Opciones para mejorar:**
- Reduce el bitrate (menos datos = más rápido):
  ```powershell
  python emisor.py --calidad 1000
  ```
- Usa una red más rápida (WiFi 5 GHz en lugar de 2.4 GHz).
- Conecta ambas máquinas por cable Ethernet.
- Acerca las máquinas al router (menos interferencias WiFi).

---

## Explicación técnica detallada

### Flujo del protocolo RTSP

```
  EMISOR (FFmpeg)                    SERVIDOR (MediaMTX)                  RECEPTOR (OpenCV)
       │                                    │                                    │
       │── ANNOUNCE (SDP del flujo) ──────►│                                    │
       │◄── 200 OK ──────────────────────── │                                    │
       │── SETUP (negociar RTP/TCP) ──────►│                                    │
       │◄── 200 OK (puertos asignados) ──── │                                    │
       │── RECORD ────────────────────────►│                                    │
       │◄── 200 OK ──────────────────────── │                                    │
       │                                    │                                    │
       │ ═══ Envío continuo de RTP ═══════►│                                    │
       │    (paquetes H.264)               │◄── DESCRIBE ─────────────────────── │
       │                                    │── 200 OK (SDP) ──────────────────►│
       │                                    │◄── SETUP ────────────────────────── │
       │                                    │── 200 OK ────────────────────────►│
       │                                    │◄── PLAY ─────────────────────────── │
       │                                    │── 200 OK ────────────────────────►│
       │                                    │                                    │
       │ ═══ RTP ═════════════════════════►│ ═══ RTP ═══════════════════════════►│
       │                                    │    (paquetes H.264)               │
       │                                    │                                    │
       │── TEARDOWN ──────────────────────►│◄── TEARDOWN ─────────────────────── │
       │                                    │                                    │
```

### `emisor.py` — Detalle técnico

1. **Verificación de FFmpeg:** El script comprueba que `ffmpeg` esté accesible ejecutando `ffmpeg -version`.

2. **MediaMTX (Servidor RTSP):** Se descarga automáticamente desde GitHub Releases la primera vez. MediaMTX es un servidor RTSP/RTMP/HLS/WebRTC de un solo binario sin dependencias. Se inicia como proceso hijo y escucha en el puerto 8554 por defecto. La variable de entorno `MTX_RTSPADDRESS` permite cambiar el puerto.

3. **Captura con OpenCV:** `cv2.VideoCapture(0, cv2.CAP_DSHOW)` abre la cámara usando DirectShow en Windows. Se leen las propiedades de resolución y FPS para configurar FFmpeg correctamente.

4. **Codificación y publicación con FFmpeg:**
   - Se lanza FFmpeg como subproceso con `stdin=subprocess.PIPE`
   - Formato de entrada: vídeo crudo BGR24 (el formato nativo de OpenCV)
   - Codificador: `libx264` con `preset=ultrafast` y `tune=zerolatency` para mínima latencia
   - Formato de salida: RTSP sobre TCP
   - FFmpeg envía ANNOUNCE + SETUP + RECORD al servidor MediaMTX
   - Cada fotograma capturado se escribe como bytes crudos en el stdin de FFmpeg

5. **Manejo de errores:** `try/finally` asegura que todos los procesos (FFmpeg, MediaMTX) se detengan y la cámara se libere al salir.

### `receptor.py` — Detalle técnico

1. **Configuración del transporte:** Se usa la variable `OPENCV_FFMPEG_CAPTURE_OPTIONS=rtsp_transport;tcp` para forzar TCP entrelazado (interleaved) en vez de UDP. Esto es más fiable en redes con firewalls.

2. **Conexión RTSP:** `cv2.VideoCapture(url_rtsp, cv2.CAP_FFMPEG)` abre la URL RTSP. Internamente, FFmpeg realiza la secuencia DESCRIBE → SETUP → PLAY automáticamente.

3. **Decodificación:** FFmpeg decodifica los paquetes RTP (H.264 NAL units) a fotogramas BGR24 que OpenCV puede mostrar directamente.

4. **HUD (Head-Up Display):** Se dibuja un overlay semitransparente con FPS en tiempo real, contador de fotogramas y tiempo transcurrido.

5. **Reconexión automática:** Si el flujo se interrumpe, el receptor espera unos segundos y reintenta la conexión indefinidamente.

---

## Estructura del proyecto

```
Demo RTSP/
├── emisor.py           # Script del emisor (captura + FFmpeg + MediaMTX)
├── receptor.py         # Script del receptor (cliente RTSP + visualización)
├── requirements.txt    # Dependencias de Python (opencv-python, numpy)
├── README.md           # Esta documentación
├── mediamtx/           # (generado automáticamente al ejecutar emisor.py)
│   ├── mediamtx.exe    #   Servidor RTSP (descargado de GitHub)
│   └── mediamtx.yml    #   Configuración del servidor
└── .venv/              # Entorno virtual de Python
```

---

## Notas finales

- Este proyecto usa **RTSP real** (RFC 2326) con transporte RTP para el vídeo, no sockets TCP crudos.
- MediaMTX permite múltiples receptores simultáneos conectados al mismo flujo.
- También puedes verificar el flujo con VLC, ffplay, o cualquier reproductor RTSP.
- Para producción, considera añadir autenticación RTSP (MediaMTX lo soporta vía `mediamtx.yml`) y cifrado TLS/SRTP.
