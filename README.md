# Demo RTSP — Transmisión de Mosaico RealSense D435 y Grabación Crítica

Sistema de transmisión de vídeo en tiempo real en red local utilizando el protocolo **RTSP** con **Esteganografía LSB (Least Significant Bit)** de 128 bits y **grabación local unificada** en formato Matroska (.mkv) en forma de mosaico de una sola vista, guardada en la máquina que recibe los archivos (receptor).

El emisor captura 4 streams simultáneos (RGB, Infrarrojos y Profundidad) de una cámara **Intel RealSense D435**, inyecta metadatos síncronos ocultos (Frame ID y marcas de tiempo en nanosegundos) en cada cuadro, los transmite como canales RTSP independientes y el receptor los recibe, mostrando el mosaico, el estado de sincronía real de la red en pantalla y realizando la grabación en disco.

*Para detalles matemáticos, análisis de resiliencia del contenedor Matroska ante fallos de energía y especificación de ingeniería del pipeline, consulte la [Guía Técnica de Esteganografía y Grabación](DOCUMENTACION_TECNICA.md).*

## Arquitectura del sistema

```
   ┌────────────────────────────────────────────────────────┐
   │ Captura RealSense (wait_for_frames) -> Timestamp Único │
   └──────────────────────────┬─────────────────────────────┘
                              │
               [ Inyección LSB en 4 canales ]
                              │
                              ▼
                 ┌───────────────────────────┐
                 │ 4 FFmpeg (RTSP Server     │
                 │  directo, -rtsp_flags     │
                 │  listen)                  │
                 └────────────┬──────────────┘
                              │ rtsp://<IP>:8554-8557/stream (Red Local)
                              ▼
                       ┌──────────────┐
                       │ Receptor OSD │
                       │   (OpenCV)   │
                       └──────┬───────┘
                              │
               [ Reconstrucción Mosaico en Receptor ]
                              ├──────────────────────────────┐
                              ▼                              ▼
                 ┌───────────────────────────┐  ┌─────────────────────────┐
                 │    Ventana de Visualización│  │ 1 FFmpeg local (H.264)  │
                 └───────────────────────────┘  └────────────┬────────────┘
                                                             │ (stdin pipe)
                                                             ▼
                                                ┌─────────────────────────┐
                                                │  Archivo Matroska .mkv  │
                                                └─────────────────────────┘
                                                (Mosaico unificado/Failsafe)
```

### ¿Qué es RTSP y Esteganografía LSB?
*   **RTSP** (Real Time Streaming Protocol): Controla la entrega de flujos multimedia en tiempo real en red local.
*   **Esteganografía LSB**: Modifica el bit menos significativo de la fila 0 (canal azul de BGR o píxel de escala de grises) para ocultar 128 bits de datos de control (Frame ID + Timestamp del emisor). El impacto visual es invisible ($\approx 0.39\%$ de variación), permitiendo que el receptor audite visualmente la sincronía física y la latencia exacta de red en el HUD.

| Protocolo | Función | Transporte | Puerto por defecto |
|-----------|---------|------------|-------------------|
| RTSP      | Señalización (DESCRIBE, SETUP, PLAY, TEARDOWN) | TCP | 554 / 8554 |
| RTP       | Transporte de paquetes de vídeo H.264 | UDP o TCP entrelazado | Negociado en SETUP |
| RTCP      | Control de calidad (informes de recepción) | UDP | RTP + 1 |

### Componentes

| Componente | Rol | Descripción |
|------------|-----|-------------|
| **`emisor.py` / `emisor_ubuntu.py`** | Publicador RTSP | Captura flujos nativos, inyecta LSB y los transmite vía RTSP de manera independiente. Cada canal se sirve con FFmpeg como servidor RTSP directo. También soporta la grabación local de rango sin pérdidas. |
| **`receptor.py` / `receptor_ubuntu.py`** | Cliente RTSP y Grabador | Conecta a los 4 canales, extrae la esteganografía LSB con votación de mayoría, muestra la visualización interactiva y graba localmente en un archivo MKV unificado en mosaico 1920×1440. |
| **FFmpeg** | Codificador y Servidor RTSP | Codifica en tiempo real los frames crudos en H.264 de ultra baja latencia. Actúa simultáneamente como servidor RTSP usando `-rtsp_flags listen`, eliminando la necesidad de intermediarios externos. |

### ¿Cómo funciona el servidor RTSP integrado?

FFmpeg puede actuar como **servidor RTSP directo** gracias a la opción `-rtsp_flags listen`. En este proyecto:

1. **Un proceso FFmpeg por canal**: El emisor lanza 4 procesos FFmpeg independientes, cada uno escuchando en su propio puerto TCP (8554, 8555, 8556, 8557).
2. **Sin intermediarios**: No se necesita ningún software externo (como MediaMTX o rtsp-simple-server). FFmpeg recibe bytes crudos por `stdin`, los codifica en H.264 y los sirve directamente como streams RTSP.
3. **Conexión directa**: El receptor se conecta directamente al puerto de cada FFmpeg. El esquema de puertos es:

| Canal | Puerto | URL |
|-------|--------|-----|
| Color (RGB) | base (8554) | `rtsp://<IP>:8554/stream` |
| Profundidad | base+1 (8555) | `rtsp://<IP>:8555/stream` |
| Infrarrojo 1 | base+2 (8556) | `rtsp://<IP>:8556/stream` |
| Infrarrojo 2 | base+3 (8557) | `rtsp://<IP>:8557/stream` |

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

---

# 🪟 Windows — Instrucciones

## Requisitos previos (Windows)

### Hardware requerido
* **Cámara Intel RealSense D435** conectada a la máquina emisora.
* **Puerto y Cable USB 3.0**: Es estrictamente necesario conectar la cámara a un puerto USB 3.0 nativo (color azul o con etiqueta SS). Los puertos USB 2.0 limitan severamente el ancho de banda, lo que causará fallos de inicialización al solicitar transmisiones HD y sub-HD simultáneas.

### Software necesario

| Requisito | Versión mínima | Cómo verificar | Instalación |
|-----------|---------------|-----------------|-------------|
| Python | 3.8+ | `python --version` | [python.org](https://www.python.org/downloads/) |
| FFmpeg | 4.0+ | (Gestionado automáticamente por `imageio-ffmpeg`) | Se instala automáticamente vía `pip install -r requirements.txt` |
| SDK Intel RealSense | Drivers básicos | Automático en Windows 10/11 | Conectar el hardware |
| pip | (incluido con Python) | `pip --version` | — |

### Gestión automática de FFmpeg

En este proyecto, **no necesitas instalar FFmpeg manualmente en Windows ni configurar variables de entorno (PATH)**. 

El sistema utiliza la dependencia de Python `imageio-ffmpeg`, la cual descarga e instala automáticamente un binario ejecutable estático de FFmpeg dentro del entorno virtual (`.venv`). Esto asegura un funcionamiento listo para usar ("out of the box") sin intervenciones manuales del usuario.

### Crear entorno virtual e instalar dependencias (Windows)

Es altamente recomendable usar un entorno virtual de Python. Abre PowerShell y ejecuta:

```powershell
cd "C:\ruta\al\proyecto"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Las dependencias de Python son:
- **`opencv-python`**: visualización, procesamiento de imágenes (Jet heatmap) y utilidades de OSD.
- **`numpy`**: operaciones matriciales rápidas para la concatenación del mosaico.
- **`imageio-ffmpeg`**: proporciona el binario estático de FFmpeg.
- **`pyrealsense2`**: SDK oficial de Intel para la comunicación de bajo nivel con la cámara RealSense D435.

Para salir del entorno virtual:

```powershell
deactivate
```

### ¿Cómo encontrar la dirección IP del emisor? (Windows)

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

### Verificar que ambas máquinas están en la misma red (Windows)

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

## Cómo ejecutar (Windows)

### Paso 1: Encuentra la IP del emisor

En la máquina con la cámara, abre PowerShell y escribe:

```powershell
ipconfig
```

Busca la línea **"Dirección IPv4"** y copia ese número. Por ejemplo: `192.168.1.42`

### Paso 2: Ejecuta el emisor (máquina con la cámara RealSense)

```powershell
cd "C:\ruta\al\proyecto"
.\.venv\Scripts\Activate.ps1
python emisor.py
```

Deberías ver algo como:

```
═══════════════════════════════════════════════════════════
  EMISOR RTSP RealSense — Windows (v4)
  Esteganografía LSB activa · FFmpeg RTSP Server directo
═══════════════════════════════════════════════════════════

[1/4] Verificando FFmpeg (vía imageio-ffmpeg) ...
  ✓ FFmpeg encontrado: ...
[2/4] Abriendo dispositivo Intel RealSense (índice 0) ...
  ✓ Pipeline RealSense iniciado correctamente.
[3/4] Iniciando 4 servidores RTSP (FFmpeg directo) ...
  → color  1920×1080 @ 1100kbps → puerto 8554
  → depth  1280×720  @ 500kbps  → puerto 8555
  → ir1    1280×720  @ 200kbps  → puerto 8556
  → ir2    1280×720  @ 200kbps  → puerto 8557
[4/4] Grabación local MKV en emisor: desactivada (se realiza en el receptor)

═══════════════════════════════════════════════════════════
  ✓ TRANSMISIÓN ACTIVA — 4 Canales RTSP + LSB
───────────────────────────────────────────────────────────
  Color (RGB):   rtsp://192.168.1.42:8554/stream
  Depth Map:     rtsp://192.168.1.42:8555/stream
  Infrared 1:    rtsp://192.168.1.42:8556/stream
  Infrared 2:    rtsp://192.168.1.42:8557/stream
───────────────────────────────────────────────────────────
  LSB:  128 bits × 1px/bit = 128px en fila 0
═══════════════════════════════════════════════════════════

  Presiona Ctrl+C para detener la transmisión.
```

**Opciones del emisor:**

```powershell
python emisor.py --puerto 8554     # Puerto RTSP base (por defecto: 8554)
python emisor.py --cam 1           # Usar segunda cámara
python emisor.py --calidad 4000    # Mayor calidad de vídeo (kbps)
python emisor.py --listar-camaras  # Ver cámaras disponibles
python emisor.py --grabar-rango 150 450 # Grabar frames sin pérdidas
```

### Paso 3: Ejecuta el receptor (otra máquina)

En la otra computadora (receptora), abre PowerShell:

```powershell
cd "C:\ruta\al\proyecto"
.\.venv\Scripts\Activate.ps1
python receptor.py 192.168.1.42
```

Si deseas realizar la **grabación del mosaico unificado** en el receptor, utiliza el argumento `--grabar` indicando la ruta del archivo MKV:

```powershell
python receptor.py 192.168.1.42 --grabar C:\ruta\a\donde\guardar\video.mkv
```

Deberías ver:

```
═══════════════════════════════════════════════════════════
  RECEPTOR RTSP MULTICANAL — RealSense D435 (v4 · LSB)
═══════════════════════════════════════════════════════════

  Color (RGB):   rtsp://192.168.1.42:8554/stream
  Depth Map:     rtsp://192.168.1.42:8555/stream
  Infrared 1:    rtsp://192.168.1.42:8556/stream
  Infrared 2:    rtsp://192.168.1.42:8557/stream
  Grabación MKV: C:\ruta\a\donde\guardar\video.mkv
  Presiona 'q' en la ventana para salir.

  Recibiendo vídeo en tiempo real ...
```

Y una ventana mostrará el vídeo en tiempo real con un indicador de FPS y estado.

**Opciones del receptor:**

```powershell
python receptor.py 192.168.1.42             # IP con puerto base por defecto (8554)
python receptor.py --grabar 192.168.1.42    # Graba en grabacion.mkv en el receptor
python receptor.py --grabar C:\ruta.mkv 192.168.1.42 # Graba en la ruta especificada
python receptor.py 192.168.1.42 9554        # IP con puerto base personalizado
python receptor.py --sin-hud IP             # Sin overlay de información
```

### Paso 4: Detener los programas

- **Receptor:** presiona `q` en la ventana del vídeo, o `Ctrl+C` en la terminal.
- **Emisor:** presiona `Ctrl+C` en PowerShell. Esto detiene FFmpeg y libera la cámara automáticamente.

### Verificar con VLC (opcional — Windows)

Puedes probar el flujo RTSP con cualquier reproductor compatible. En VLC:

1. Abre VLC
2. Ve a **Medio → Abrir ubicación de red** (o `Ctrl+N`)
3. Escribe: `rtsp://192.168.1.42:8554/stream`
4. Haz clic en **Reproducir**

---

# 🐧 Ubuntu/Linux Nativo — Instrucciones (v2)

Esta es la versión actual y recomendada para Ubuntu/Linux. Diseñada desde cero para funcionar de forma nativa (no es un calco de Windows).

## Requisitos previos (Ubuntu)

### Hardware
* **Cámara Intel RealSense D435** conectada por **USB 3.0** (puerto azul).

### Software del sistema

Instala las dependencias del sistema operativo:

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip ffmpeg libgl1 libglib2.0-0
```

| Requisito | Cómo verificar | Instalación |
|-----------|---------------|-------------|
| Python 3.8+ | `python3 --version` | Incluido en Ubuntu |
| FFmpeg | `ffmpeg -version` | `sudo apt install ffmpeg` |
| pip | `pip3 --version` | `sudo apt install python3-pip` |
| libGL (OpenCV) | — | `sudo apt install libgl1 libglib2.0-0` |

### Instalar pyrealsense2 en Ubuntu

`pyrealsense2` se puede instalar de dos formas:

**Opción A — vía pip (más simple, puede no funcionar en todas las distribuciones):**
```bash
pip install pyrealsense2
```

**Opción B — vía el repositorio oficial de Intel (recomendado si pip falla):**
```bash
sudo mkdir -p /etc/apt/keyrings
curl -sSf https://librealsense.intel.com/Debian/librealsense.pgp \
  | sudo tee /etc/apt/keyrings/librealsense.pgp > /dev/null

echo "deb [signed-by=/etc/apt/keyrings/librealsense.pgp] \
  https://librealsense.intel.com/Debian/apt-repo $(lsb_release -cs) main" \
  | sudo tee /etc/apt/sources.list.d/librealsense.list

sudo apt update
sudo apt install -y librealsense2-dkms librealsense2-utils librealsense2-dev
pip install pyrealsense2
```

### Configurar permisos USB (reglas udev)

**Solo la primera vez.** Sin estas reglas, la cámara puede no abrirse sin `sudo`:

```bash
wget https://raw.githubusercontent.com/IntelRealSense/librealsense/master/config/99-realsense-libusb.rules
sudo cp 99-realsense-libusb.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Después de esto, **desconecta y reconecta** la cámara.

### Crear entorno virtual e instalar dependencias (Ubuntu)

```bash
cd /ruta/al/proyecto
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements_ubuntu.txt
```

Para salir del entorno virtual:

```bash
deactivate
```

### Diagnóstico del sistema (Ubuntu)

Si tienes problemas, ejecuta el diagnóstico integrado:

```bash
python3 emisor_ubuntu.py --diagnostico
```

Esto verificará: FFmpeg, OpenCV, NumPy, pyrealsense2, reglas udev, dispositivos USB y puertos disponibles.

## Cómo ejecutar (Ubuntu)

### ¿Cómo encontrar la IP del emisor? (Ubuntu)

```bash
ip addr show | grep "inet " | grep -v 127.0.0.1
```

O de forma más legible:

```bash
hostname -I
```

Busca la IP que empiece con `192.168.x.x` o `10.x.x.x`.

### Verificar conectividad

Desde la máquina receptora:

```bash
ping 192.168.1.42    # Reemplaza con la IP del emisor
```

### Paso 1: Ejecutar el emisor (máquina con la cámara)

```bash
cd /ruta/al/proyecto
source .venv/bin/activate
python3 emisor_ubuntu.py
```

Deberías ver:

```
══════════════════════════════════════════════════════════════
  EMISOR RTSP — Intel RealSense D435 · Ubuntu Nativo (v4)
  Esteganografía LSB activa · FFmpeg RTSP Server directo
══════════════════════════════════════════════════════════════

[1/5] Buscando FFmpeg ...
  ✓ FFmpeg: /usr/bin/ffmpeg
    Origen: sistema (ffmpeg version 6.1.1-3ubuntu5)

[2/5] Verificando dependencias Python ...
  ✓ OpenCV 4.x.x
  ✓ NumPy 1.x.x
  ✓ pyrealsense2 disponible
  ✓ Reglas udev: /etc/udev/rules.d/99-realsense-libusb.rules

[3/5] Abriendo cámara Intel RealSense (índice 0) ...
  → Dispositivo: Intel RealSense D435 (S/N: 123456789)
  ✓ Pipeline iniciado (Color 1920×1080, Depth/IR 1280×720 @ 30fps)

[4/5] Iniciando 4 servidores RTSP (FFmpeg directo) ...
  → color  1920×1080 @ 1100kbps → puerto 8554
  → depth  1280×720  @ 500kbps  → puerto 8555
  → ir1    1280×720  @ 200kbps  → puerto 8556
  → ir2    1280×720  @ 200kbps  → puerto 8557

[5/5] Grabación local MKV en emisor: desactivada (se realiza en el receptor)

══════════════════════════════════════════════════════════════
  ✓ TRANSMISIÓN ACTIVA — 4 Canales RTSP + LSB
──────────────────────────────────────────────────────────────
  Color (RGB):     rtsp://192.168.1.42:8554/stream
  Profundidad:     rtsp://192.168.1.42:8555/stream
  Infrarrojo 1:    rtsp://192.168.1.42:8556/stream
  Infrarrojo 2:    rtsp://192.168.1.42:8557/stream
──────────────────────────────────────────────────────────────
  LSB:  128 bits × 1px/bit = 128px en fila 0
  Receptor:  python3 receptor_ubuntu.py 192.168.1.42
  VLC:       vlc rtsp://192.168.1.42:8554/stream
══════════════════════════════════════════════════════════════
```

  Presiona Ctrl+C para detener.
```

**Opciones del emisor:**

```bash
python3 emisor_ubuntu.py --puerto 8554       # Puerto RTSP base (defecto: 8554)
python3 emisor_ubuntu.py --cam 1             # Segunda cámara
python3 emisor_ubuntu.py --calidad 4000      # Mayor calidad (kbps)
python3 emisor_ubuntu.py --listar-camaras    # Ver cámaras RealSense
python3 emisor_ubuntu.py --diagnostico       # Diagnóstico completo
python3 emisor_ubuntu.py --grabar-rango 150 450 # Grabar frames sin pérdidas
```

### Paso 2: Ejecutar el receptor (otra máquina o terminal)

En la máquina receptora (o en otra terminal):

```bash
cd /ruta/al/proyecto
source .venv/bin/activate
python3 receptor_ubuntu.py 192.168.1.42
```

Si deseas realizar la **grabación del mosaico unificado** en el receptor, utiliza el argumento `--grabar` indicando la ruta del archivo MKV:

```bash
python3 receptor_ubuntu.py 192.168.1.42 --grabar /ruta/a/donde/guardar/video.mkv
```

El receptor se conectará a los 4 canales y mostrará un mosaico interactivo:

```
══════════════════════════════════════════════════════════════
  RECEPTOR RTSP — RealSense D435 · Ubuntu Nativo (v4 · LSB)
══════════════════════════════════════════════════════════════
  color  → rtsp://192.168.1.42:8554/stream
  depth  → rtsp://192.168.1.42:8555/stream
  ir1    → rtsp://192.168.1.42:8556/stream
  ir2    → rtsp://192.168.1.42:8557/stream
──────────────────────────────────────────────────────────────
  Grabación MKV: /ruta/a/donde/guardar/video.mkv
  Controles: [M]osaico [1]Color [2]IR1 [3]Depth [4]IR2
             [H]UD on/off  [F]ullscreen  [Q]Salir
══════════════════════════════════════════════════════════════
```

**Controles de teclado del receptor:**

| Tecla | Acción |
|-------|--------|
| `M` | Vista Mosaico (4 cámaras combinadas) |
| `1` | Solo canal Color (RGB 1920×1080) |
| `2` | Solo canal Infrarrojo 1 (Left) |
| `3` | Solo canal Profundidad (Depth heatmap) |
| `4` | Solo canal Infrarrojo 2 (Right) |
| `H` | Mostrar/ocultar HUD (información en pantalla) |
| `F` | Pantalla completa on/off |
| `Q` / `ESC` | Salir |

**Opciones del receptor:**

```bash
python3 receptor_ubuntu.py 192.168.1.42             # IP con puerto base defecto (8554)
python3 receptor_ubuntu.py --grabar 192.168.1.42    # Graba en grabacion.mkv en el receptor
python3 receptor_ubuntu.py --grabar /ruta/video.mkv 192.168.1.42 # Graba en la ruta especificada
python3 receptor_ubuntu.py 192.168.1.42 9554        # Puerto base personalizado
python3 receptor_ubuntu.py --sin-hud 192.168.1.42   # Sin overlay de info
python3 receptor_ubuntu.py 127.0.0.1                # Prueba local
```

### Paso 3: Detener

- **Receptor:** presiona `Q` o `ESC` en la ventana, o `Ctrl+C` en la terminal.
- **Emisor:** presiona `Ctrl+C` en la terminal. Libera cámara y FFmpeg automáticamente.

### Verificar con VLC (opcional — Ubuntu)

```bash
vlc rtsp://192.168.1.42:8554/stream
```

O desde la interfaz gráfica: **Medio → Abrir ubicación de red** → `rtsp://192.168.1.42:8554/stream`

---

# 📡 Ejecutar el receptor en OTRA computadora

El protocolo RTSP está diseñado para redes. Para ver la cámara desde otra computadora física conectada al mismo WiFi o cable:

1. Descubre la IP de la computadora que tiene la cámara conectada:
   - **Windows:** abre PowerShell → `ipconfig` → busca Dirección IPv4
   - **Ubuntu:** `hostname -I` o `ip addr show`
2. Ve a la **segunda computadora** (la que va a recibir el video), abre la terminal, activa el entorno y ejecuta el script apuntando a esa IP.

**Si el receptor es Windows:**
```powershell
cd "C:\ruta\al\proyecto"
.\.venv\Scripts\Activate.ps1
python receptor.py 192.168.1.42
```

**Si el receptor es Ubuntu/Linux:**
```bash
cd /ruta/al/proyecto
source .venv/bin/activate
python3 receptor_ubuntu.py 192.168.1.42
```

---

# 🛠️ Resolución de Problemas

### "FFmpeg no está instalado o no se encuentra"

**En Windows:**
1. Asegúrate de activar el entorno virtual con `.\.venv\Scripts\Activate.ps1`.
2. Ejecuta `pip install -r requirements.txt` para instalar `imageio-ffmpeg`.

**En Ubuntu:**
1. Instala FFmpeg del sistema: `sudo apt install ffmpeg`
2. Verifica: `ffmpeg -version`

### "No se pudo abrir la cámara" / "No se detectaron cámaras"

1. Verifica que la cámara esté conectada a un **puerto USB 3.0** (azul).
2. Cierra cualquier otro programa que use la cámara (Zoom, Teams, OBS).
3. **En Ubuntu**, verifica permisos:
   ```bash
   python3 emisor_ubuntu.py --diagnostico
   ls -la /dev/video*
   ```
4. Si falta acceso USB, instala las reglas udev (ver sección de requisitos Ubuntu).

### "Puerto en uso" o "Error al iniciar servidor RTSP"

Los puertos 8554-8557 ya están en uso:
```bash
# Ubuntu:
sudo lsof -i :8554
# Usar otros puertos base:
python3 emisor_ubuntu.py --puerto 9554
```

```powershell
# Windows:
netstat -ano | findstr 8554
python emisor.py --puerto 9554
```

### "Video lento o entrecortado"

- Reduce la calidad: `python3 emisor_ubuntu.py --calidad 1000`
- Usa WiFi 5GHz o cable Ethernet.
- Verifica que ambas máquinas estén en la misma red.

---

# ⚙️ Explicación Técnica Detallada

1. **Servidor RTSP integrado en FFmpeg**: Cada proceso FFmpeg actúa como servidor RTSP directo usando `-rtsp_flags listen`. El emisor lanza 4 instancias (una por canal), cada una escuchando en un puerto TCP dedicado. No se requiere ningún intermediario externo.
2. **Procesamiento de Imágenes**: 
   - La profundidad se convierte a un mapa de calor usando `cv2.COLORMAP_JET`.
   - Las resoluciones nativas (RGB 1080p y Depth/IR 720p) se redimensionan y concatenan matemáticamente usando `numpy` en un lienzo de `1920x1440`.
3. **Compresión H.264 (FFmpeg)**: El lienzo en crudo pasa por una tubería (pipe) hacia FFmpeg, configurado con el preset `ultrafast` y `zerolatency` para empujar los paquetes vía RTP/TCP.

---

# 📁 Estructura del proyecto

```text
Demo RTSP/
├── emisor.py                  # Emisor para Windows nativo (v4 — FFmpeg RTSP Server directo)
├── emisor_ubuntu.py           # Emisor para Ubuntu nativo (v4 — FFmpeg RTSP Server directo)
├── receptor.py                # Receptor para Windows nativo (v4 — HUD y verificación LSB)
├── receptor_ubuntu.py         # Receptor para Ubuntu nativo (v4 — HUD y verificación LSB)
├── DOCUMENTACION_TECNICA.md   # Documentación detallada de esteganografía LSB y resiliencia MKV
├── requirements.txt           # Dependencias Python (Windows)
├── requirements_ubuntu.txt    # Dependencias Python (Ubuntu)
└── .venv/                     # Entorno virtual de Python
```
