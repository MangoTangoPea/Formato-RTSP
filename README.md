# Transmisión RTSP — Intel RealSense D435 (RGB + Profundidad)

Sistema de transmisión de vídeo tipo cliente-servidor que transmite los flujos **RGB y de profundidad** de una cámara **Intel RealSense D435** en tiempo real, usando el protocolo **RTSP** (Real Time Streaming Protocol, RFC 2326) a través de la red local.

## Arquitectura del sistema

```
  ┌────────────────┐  RGB + Depth   ┌────────────┐  raw frame   ┌─────────┐
  │  Intel D435    │ ─────────────► │ Procesado  │ ───────────► │ FFmpeg  │
  │ (pyrealsense2) │  via SDK       │ lado a lado│  vía stdin   │ (H.264) │
  └────────────────┘                └────────────┘              └────┬────┘
                                                                    │
                                                              RTSP/RTP push
                                                              (ANNOUNCE +
                                                               RECORD)
                                                                    │
                                                              ┌─────▼──────┐
                                                              │  MediaMTX  │
                                                              │  (server   │
                                                              │   RTSP)    │
                                                              └─────┬──────┘
                                                                    │
                                                          rtsp://<IP>:8554/camara
                                                                    │
                                                               ┌────▼─────┐
                                                               │ Receptor │
                                                               │ (OpenCV) │
                                                               └──────────┘
                                                                receptor.py
```

### Fotograma combinado transmitido

El emisor captura dos flujos de la RealSense D435:
- **Flujo RGB** (color): imagen estándar a color, 640×480.
- **Flujo de profundidad** (Depth): mapa de distancias, 640×480.

El mapa de profundidad se colorea con `cv2.applyColorMap(COLORMAP_JET)` y ambos se unen **lado a lado** en un solo fotograma de **1280×480**:

```
┌──────────────────┬──────────────────┐
│   RGB (color)    │  Depth (JET)     │
│   640 × 480      │  640 × 480       │
└──────────────────┴──────────────────┘
        → fotograma de 1280 × 480 →
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
| **`emisor.py`** | Publicador RTSP | Captura RGB + Depth con `pyrealsense2`, colorea profundidad con `cv2.applyColorMap`, une lado a lado, y envía el fotograma combinado a FFmpeg vía stdin. FFmpeg codifica en H.264 y publica al servidor MediaMTX. |
| **`receptor.py`** | Cliente RTSP | Se conecta a la URL RTSP, recibe paquetes RTP, decodifica H.264 y muestra el vídeo combinado (RGB + Depth) en una ventana OpenCV. |
| **MediaMTX** | Servidor RTSP | Servidor ligero que redistribuye el flujo a múltiples clientes. Se descarga automáticamente al ejecutar el emisor. |
| **FFmpeg** | Codificador | Convierte vídeo crudo BGR a H.264 y lo empaqueta en RTSP/RTP. Se incluye automáticamente mediante `imageio-ffmpeg`. |

---

## Requisitos previos

### Software necesario

| Requisito | Versión mínima | Cómo verificar | Instalación |
|-----------|---------------|-----------------|-------------|
| Python | 3.8+ | `python --version` | [python.org](https://www.python.org/downloads/) |
| Intel RealSense SDK | 2.0+ | (Incluido en `pyrealsense2`) | `pip install pyrealsense2` |
| FFmpeg | 4.0+ | (Gestionado automáticamente por `imageio-ffmpeg`) | `pip install imageio-ffmpeg` |
| pip | (incluido con Python) | `pip --version` | — |

### Hardware necesario

| Hardware | Detalle |
|----------|---------|
| **Intel RealSense D435** | Cámara de profundidad estéreo |
| **Puerto USB 3.0** | Requerido para el ancho de banda de los flujos RGB + Depth simultáneos |
| **Red local (LAN)** | WiFi o cable Ethernet entre emisor y receptor |

> **Nota:** MediaMTX se descarga **automáticamente** la primera vez que ejecutas `emisor.py`. No necesitas instalarlo manualmente.

### Gestión automática de FFmpeg

En este proyecto, **no necesitas instalar FFmpeg manualmente en Windows ni configurar variables de entorno (PATH)**.

El sistema utiliza la dependencia de Python `imageio-ffmpeg`, la cual descarga e instala automáticamente un binario ejecutable estático de FFmpeg dentro del entorno virtual (`.venv`). Esto asegura un funcionamiento listo para usar ("out of the box") sin intervenciones manuales del usuario.

### Crear entorno virtual e instalar dependencias

Es altamente recomendable usar un entorno virtual de Python. Abre PowerShell y ejecuta:

```powershell
cd "C:\ruta\al\proyecto"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Las dependencias de Python son:
- **`pyrealsense2`**: SDK oficial de Intel para captura de flujos RGB y de profundidad de las cámaras RealSense.
- **`opencv-python`**: procesamiento de imágenes (`applyColorMap`), decodificación H.264 y visualización de vídeo.
- **`numpy`**: manipulación de arrays de píxeles (requerido por OpenCV y pyrealsense2).
- **`imageio-ffmpeg`**: proporciona y gestiona el binario ejecutable de FFmpeg dentro del entorno de Python de forma automática.

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

En la máquina donde está el **emisor** (la que tiene la cámara RealSense), abre PowerShell y ejecuta:

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

### Paso 1: Verifica que la cámara RealSense esté conectada

Conecta la Intel RealSense D435 a un **puerto USB 3.0** del emisor. Puedes verificar que sea detectada:

```powershell
cd "C:\ruta\al\proyecto"
.\.venv\Scripts\Activate.ps1
python emisor.py --listar-camaras
```

Deberías ver algo como:

```
Buscando cámaras Intel RealSense ...

  Cámaras Intel RealSense detectadas: 1
  ────────────────────────────────────────────────────────
  Índice   Nombre                         Nº de Serie
  ────────────────────────────────────────────────────────
  0        Intel RealSense D435           038422250711
  ────────────────────────────────────────────────────────

  Uso: python emisor.py --cam <índice>
```

### Paso 2: Encuentra la IP del emisor

En la máquina con la cámara, abre PowerShell y escribe:

```powershell
ipconfig
```

Busca la línea **"Dirección IPv4"** y copia ese número. Por ejemplo: `192.168.1.42`

### Paso 3: Ejecuta el emisor (máquina con la cámara RealSense)

```powershell
cd "C:\ruta\al\proyecto"
.\.venv\Scripts\Activate.ps1
python emisor.py
```

Deberías ver algo como:

```
════════════════════════════════════════════════════════════
  EMISOR RTSP — Intel RealSense D435 (RGB + Profundidad)
════════════════════════════════════════════════════════════

[1/4] Verificando FFmpeg ...
  ✓ FFmpeg encontrado

[2/4] Preparando servidor RTSP (MediaMTX) ...
  ↓ Descargando MediaMTX v1.12.2 ...       ← (solo la primera vez)
  ✓ MediaMTX instalado en: ...\mediamtx\mediamtx.exe
  → Iniciando MediaMTX en el puerto 8554 ...
  ✓ MediaMTX iniciado (PID: 12345)

[3/4] Abriendo cámara Intel RealSense (índice 0) ...
  ✓ Cámara abierta: Intel RealSense D435
    Serial: 038422250711
    Firmware: 5.16.0.1
    Flujo RGB:   640×480 @ 30 FPS
    Flujo Depth: 640×480 @ 30 FPS
    Fotograma combinado: 1280×480

[4/4] Iniciando transmisión RTSP ...
  ✓ Transmisión RTSP activa

════════════════════════════════════════════════════════════
  URL RTSP del flujo:
  → rtsp://192.168.1.42:8554/camara

  Contenido: RGB (izquierda) | Profundidad (derecha)
  Resolución: 1280×480
  Bitrate: 2000 kbps

  Los receptores deben conectarse a esta URL.
  También puedes probar con VLC: Medio → Abrir ubicación de red
════════════════════════════════════════════════════════════

  Presiona Ctrl+C para detener la transmisión.
```

**Opciones del emisor:**

```powershell
python emisor.py --puerto 8554     # Puerto RTSP (por defecto: 8554)
python emisor.py --cam 1           # Usar segunda cámara RealSense
python emisor.py --calidad 4000    # Mayor calidad de vídeo (kbps)
python emisor.py --listar-camaras  # Ver cámaras RealSense detectadas
```

### Paso 4: Ejecuta el receptor (otra máquina)

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
════════════════════════════════════════════════════════════
  RECEPTOR RTSP — RealSense D435 (RGB + Profundidad)
════════════════════════════════════════════════════════════

  URL RTSP: rtsp://192.168.1.42:8554/camara
  Transporte: TCP entrelazado (interleaved)
  Contenido esperado: RGB (izquierda) | Depth (derecha)
  Presiona 'q' en la ventana para salir.

  [Intento 1] Conectando a rtsp://192.168.1.42:8554/camara ...
  ✓ Conectado al flujo RTSP
  Resolución: 1280x480
  FPS del flujo: 30.0
  Contenido: RGB (640×480) | Depth (640×480)

  Recibiendo vídeo en tiempo real ...
```

Y una ventana mostrará el vídeo combinado con RGB a la izquierda y el mapa de profundidad coloreado a la derecha, con indicadores de FPS, estado y etiquetas de región.

**Opciones del receptor:**

```powershell
python receptor.py 192.168.1.42             # IP con puerto por defecto (8554)
python receptor.py 192.168.1.42 9554        # IP con puerto personalizado
python receptor.py rtsp://IP:PUERTO/camara  # URL RTSP completa
python receptor.py --sin-hud IP             # Sin overlay de información
```

### Paso 5: Detener los programas

- **Receptor:** presiona `q` en la ventana del vídeo, o `Ctrl+C` en la terminal.
- **Emisor:** presiona `Ctrl+C` en PowerShell. Esto detiene FFmpeg, MediaMTX y libera la cámara RealSense automáticamente.

### Verificar con VLC (opcional)

Puedes probar el flujo RTSP con cualquier reproductor compatible. En VLC:

1. Abre VLC
2. Ve a **Medio → Abrir ubicación de red** (o `Ctrl+N`)
3. Escribe: `rtsp://192.168.1.42:8554/camara`
4. Haz clic en **Reproducir**

---

## Resolución de problemas comunes

### "pyrealsense2 no está instalado"

**Causa:** El SDK de Intel RealSense no se instaló correctamente.

**Solución:**
1. Activa el entorno virtual: `.\.venv\Scripts\Activate.ps1`
2. Instala: `pip install pyrealsense2`
3. Si falla, verifica que tu versión de Python sea compatible (3.8–3.12).

### "No se detectaron cámaras Intel RealSense"

**Solución:**
1. Verifica que la cámara esté conectada a un **puerto USB 3.0** (no 2.0).
2. Prueba con otro cable USB.
3. Verifica que la cámara no esté en uso por otro programa (Intel RealSense Viewer, etc.).
4. Lista las cámaras disponibles:
   ```powershell
   python emisor.py --listar-camaras
   ```

### "FFmpeg no está instalado o no se encuentra"

**Causa:** El entorno virtual no se activó correctamente o no se han instalado las dependencias de Python.

**Solución:**
1. Asegúrate de activar el entorno virtual con `.\.venv\Scripts\Activate.ps1`.
2. Asegúrate de ejecutar `pip install -r requirements.txt` para instalar `imageio-ffmpeg` y el resto de dependencias.

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
- Aumenta el bitrate para mayor calidad (el fotograma combinado es más ancho):
  ```powershell
  python emisor.py --calidad 4000
  ```
- Reduce el bitrate si la red es lenta:
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

1. **Verificación de FFmpeg:** El script comprueba que `ffmpeg` esté accesible ejecutando `ffmpeg -version` a través de `imageio-ffmpeg`.

2. **MediaMTX (Servidor RTSP):** Se descarga automáticamente desde GitHub Releases la primera vez. MediaMTX es un servidor RTSP/RTMP/HLS/WebRTC de un solo binario sin dependencias. Se inicia como proceso hijo y escucha en el puerto 8554 por defecto. La variable de entorno `MTX_RTSPADDRESS` permite cambiar el puerto.

3. **Captura con pyrealsense2 (Intel RealSense SDK):**
   - `rs.pipeline()` gestiona los flujos de la cámara D435
   - `rs.config()` configura dos flujos simultáneos:
     - **Color (RGB):** `rs.stream.color`, formato `rs.format.bgr8`, 640×480 @ 30 FPS
     - **Profundidad (Depth):** `rs.stream.depth`, formato `rs.format.z16`, 640×480 @ 30 FPS
   - `pipeline.wait_for_frames()` sincroniza ambos flujos temporalmente
   - El mapa de profundidad Z16 (uint16, milímetros) se normaliza a uint8 con `cv2.convertScaleAbs(alpha=0.03)`
   - Se aplica `cv2.applyColorMap(COLORMAP_JET)` para visualización en color:
     - 🔵 Azul = objetos cercanos
     - 🟢 Verde = distancia media
     - 🔴 Rojo = objetos lejanos
   - Se unen con `np.hstack()` → fotograma combinado de 1280×480

4. **Codificación y publicación con FFmpeg:**
   - Se lanza FFmpeg como subproceso con `stdin=subprocess.PIPE`
   - Formato de entrada: vídeo crudo BGR24 (el formato nativo de OpenCV) de 1280×480
   - Codificador: `libx264` con `preset=ultrafast` y `tune=zerolatency` para mínima latencia
   - Formato de salida: RTSP sobre TCP
   - FFmpeg envía ANNOUNCE + SETUP + RECORD al servidor MediaMTX
   - Cada fotograma combinado se escribe como bytes crudos en el stdin de FFmpeg

5. **Manejo de errores:** `try/finally` asegura que todos los procesos (FFmpeg, MediaMTX) se detengan y la cámara RealSense se libere (`pipeline.stop()`) al salir.

### `receptor.py` — Detalle técnico

1. **Configuración del transporte:** Se usa la variable `OPENCV_FFMPEG_CAPTURE_OPTIONS=rtsp_transport;tcp` para forzar TCP entrelazado (interleaved) en vez de UDP. Esto es más fiable en redes con firewalls.

2. **Conexión RTSP:** `cv2.VideoCapture(url_rtsp, cv2.CAP_FFMPEG)` abre la URL RTSP. Internamente, FFmpeg realiza la secuencia DESCRIBE → SETUP → PLAY automáticamente.

3. **Decodificación:** FFmpeg decodifica los paquetes RTP (H.264 NAL units) a fotogramas BGR24 que OpenCV puede mostrar directamente. El fotograma de 1280×480 contiene RGB y Depth lado a lado.

4. **HUD (Head-Up Display):** Se dibuja un overlay semitransparente con:
   - FPS en tiempo real, contador de fotogramas y tiempo transcurrido
   - Etiquetas "RGB" y "DEPTH" sobre cada mitad del fotograma
   - Línea separadora vertical blanca entre ambas regiones

5. **Reconexión automática:** Si el flujo se interrumpe, el receptor espera unos segundos y reintenta la conexión indefinidamente.

---

## Estructura del proyecto

```
Formato-RTSP/
├── emisor.py           # Script del emisor (RealSense + FFmpeg + MediaMTX)
├── receptor.py         # Script del receptor (cliente RTSP + visualización)
├── requirements.txt    # Dependencias de Python
├── README.md           # Esta documentación
├── mediamtx/           # (generado automáticamente al ejecutar emisor.py)
│   ├── mediamtx.exe    #   Servidor RTSP (descargado de GitHub)
│   └── mediamtx.yml    #   Configuración del servidor
└── .venv/              # Entorno virtual de Python
```

---

## Resumen de argumentos CLI

### `emisor.py`

| Argumento | Ejemplo | Descripción |
|-----------|---------|-------------|
| `--puerto` | `python emisor.py --puerto 8554` | Puerto RTSP (por defecto: 8554) |
| `--cam` | `python emisor.py --cam 1` | Usar una segunda cámara RealSense si hay varias conectadas |
| `--calidad` | `python emisor.py --calidad 4000` | Ajustar el bitrate del vídeo en kbps |
| `--listar-camaras` | `python emisor.py --listar-camaras` | Imprime nombre y serial de las cámaras RealSense detectadas y sale |

### `receptor.py`

| Argumento | Ejemplo | Descripción |
|-----------|---------|-------------|
| `destino` | `python receptor.py 192.168.1.42` | IP del emisor o URL RTSP completa |
| `puerto` | `python receptor.py 192.168.1.42 9554` | Puerto RTSP (por defecto: 8554) |
| `--sin-hud` | `python receptor.py --sin-hud 192.168.1.42` | Desactivar overlay de información |

---

## Notas finales

- Este proyecto usa **RTSP real** (RFC 2326) con transporte RTP para el vídeo, no sockets TCP crudos.
- La captura usa el **SDK oficial de Intel** (`pyrealsense2`), no captura genérica UVC, lo que garantiza acceso a los flujos nativos de profundidad de la D435.
- MediaMTX permite múltiples receptores simultáneos conectados al mismo flujo.
- También puedes verificar el flujo con VLC, ffplay, o cualquier reproductor RTSP.
- Para producción, considera añadir autenticación RTSP (MediaMTX lo soporta vía `mediamtx.yml`) y cifrado TLS/SRTP.
