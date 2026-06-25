# Demo RTSP — Transmisión de Mosaico RealSense D435

Sistema de transmisión de vídeo en tiempo real en red local utilizando el protocolo **RTSP**. El emisor captura streams simultáneos (RGB, Infrarrojos y Profundidad) de una cámara **Intel RealSense D435**, compone un mosaico de alta resolución (1920x1440) con OSD y lo transmite de forma fluida.

## Arquitectura del sistema

```
  ┌──────────────────┐               
  │  RealSense D435  │               
  │ (RGB, Depth, IR) │               
  └────────┬─────────┘               
           │ (4 streams)             
           ▼                         
      ┌──────────┐    mosaico raw     ┌─────────┐   RTSP/RTP    ┌──────────┐
      │  Emisor  │ ─────────────────► │ FFmpeg  │ ────────────► │ MediaMTX │
      │ (Python) │   vía pipe/stdin   │ (H.264) │  ANNOUNCE +   │ (server  │
      │          │   (1920x1440 BGR)  │         │  RECORD        │  RTSP)   │
      └──────────┘                    └─────────┘               └────┬─────┘
       emisor.py                                                     │
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
| **`emisor.py`** | Publicador RTSP | Captura 4 flujos nativos (Color, Depth, IR1, IR2) de la Intel RealSense D435, compone el mosaico, añade overlays de texto HUD, y envía el canvas raw (1920x1440) a FFmpeg vía stdin. |
| **`receptor.py`** | Cliente RTSP | Se conecta al servidor, recibe y decodifica el flujo de mosaico H.264 para mostrarlo en tiempo real. |
| **MediaMTX** | Servidor RTSP (Broker) | Servidor ligero de alta velocidad que gestiona el streaming RTSP. Recibe la publicación de FFmpeg y la retransmite bajo demanda. |
| **FFmpeg** | Codificador de Vídeo | Codifica en tiempo real los frames crudos BGR24 en paquetes H.264 comprimidos de baja latencia. |

### ¿Para qué se usa MediaMTX en este proyecto?

**MediaMTX** (anteriormente conocido como rtsp-simple-server) es un servidor multimedia e intermediario de streaming ("RTSP broker") de alto rendimiento. En este proyecto cumple un papel crítico:

1. **Multiplexado de Clientes**: Una cámara física RealSense solo puede estar abierta por un proceso de captura a la vez. MediaMTX permite que el emisor envíe el vídeo **una sola vez** al servidor, y este se encarga de retransmitir el flujo simultáneamente a todos los clientes (receptores) que se conecten, sin saturar la cámara.
2. **Puente entre Codificador y Clientes**: FFmpeg se encarga de codificar pero no actúa como un servidor de red pasivo. MediaMTX corre de fondo esperando conexiones, y permite a FFmpeg publicar su flujo mediante el comando `ANNOUNCE` y `RECORD`, manteniendo el canal abierto.
3. **Capa de Control de Protocolo**: Gestiona la señalización formal de la comunicación RTSP (DESCRIBE, SETUP, PLAY, TEARDOWN) y el multiplexado de los paquetes de datos RTP/RTCP tanto por UDP como por TCP.

---

## Requisitos previos

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
- **`opencv-python`**: visualización, procesamiento de imágenes (Jet heatmap) y utilidades de OSD.
- **`numpy`**: operaciones matriciales rápidas para la concatenación del mosaico.
- **`imageio-ffmpeg`**: proporciona el binario estático de FFmpeg.
- **`pyrealsense2`**: SDK oficial de Intel para la comunicación de bajo nivel con la cámara RealSense D435.

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

### Paso 2: Ejecuta el emisor (máquina con la cámara RealSense)

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

### "FFmpeg no está instalado o no se encuentra"

**Causa:** El entorno virtual no se activó correctamente o no se han instalado las dependencias de Python.

**Solución:**
1. Asegúrate de activar el entorno virtual con `.\.venv\Scripts\Activate.ps1`.
2. Asegúrate de ejecutar `pip install -r requirements.txt` para instalar `imageio-ffmpeg` y el resto de dependencias.

### "No se pudo abrir la cámara"

**Solución:**
1. Asegúrate de que la cámara esté conectada y no en uso por otro programa (Zoom, Teams, etc.)
2. Lista las cámaras disponibles:
   ```powershell
   python emisor.py
   ```
   *(Anota la URL RTSP y la IP que aparece en la consola).*
3. **Ejecutar el Receptor:**
   Abre otro PowerShell (o en otra computadora de tu red) y ejecuta:
   ```powershell
   python receptor.py 127.0.0.1  # Usa la IP real si es en otra computadora
   ```

---

### Opción B: Ejecutar en Ubuntu Nativo (Raspberry Pi, Jetson, PC)
En un sistema Linux nativo, el kernel ya incluye los controladores de video necesarios.

1. **Clonar e instalar dependencias:**
   ```bash
   sudo apt update && sudo apt install python3-venv python3-pip libgl1 libglib2.0-0
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
2. **Dar permisos USB a la cámara (Solo la primera vez):**
   ```bash
   wget https://raw.githubusercontent.com/IntelRealSense/librealsense/master/config/99-realsense-libusb.rules
   sudo cp 99-realsense-libusb.rules /etc/udev/rules.d/
   sudo udevadm control --reload-rules && sudo udevadm trigger
   ```
3. **Ejecutar el Emisor:**
   Abre una terminal y ejecuta:
   ```bash
   cd /ruta/al/proyecto
   source .venv/bin/activate
   python3 emisor_ubuntu.py
   ```
4. **Ejecutar el Receptor:**
   En otra terminal o en otra computadora, ejecuta:
   ```bash
   cd /ruta/al/proyecto
   source .venv/bin/activate
   python3 receptor_ubuntu.py 127.0.0.1  # Usa la IP real si es en otra computadora
   ```

---

### Opción C: Ejecutar en WSL2 (Desarrollo avanzado)
WSL2 no tiene drivers de video USB. Requiere puentear el USB desde Windows y compilar una librería especial. Sigue estos 6 pasos exactos:

**Paso 1: Vincular la cámara a WSL**
En un **PowerShell de Windows como Administrador**, instala la herramienta y vincula la cámara:
```powershell
winget install --interactive --exact dorssel.usbipd-win
usbipd list
usbipd bind --busid <TU_BUSID>    # (ej: 2-1)
usbipd attach --wsl --busid <TU_BUSID>
```

**Paso 2: Compilar librealsense en Ubuntu (WSL)**
Abre tu terminal de Ubuntu y ejecuta el script de instalación (tomará 15-30 minutos):
```bash
cd /mnt/c/Users/Lenovo/Documents/GitHub/Formato-RTSP
bash install_realsense.sh
```

**Paso 3: Ejecutar el Emisor (con superpermisos)**
No cierres esta terminal mientras uses la cámara:
```bash
# Cambia la ruta según donde tengas tu proyecto clonado
sudo /mnt/c/Users/Lenovo/Documents/GitHub/Formato-RTSP/.venvv/bin/python3 emisor_ubuntu.py
```

**Paso 4: Ejecutar el Receptor (en paralelo)**
Abre una **NUEVA** pestaña de Ubuntu (WSL) y ejecuta:
```bash
cd /mnt/c/Users/Lenovo/Documents/GitHub/Formato-RTSP
source .venvv/bin/activate
python3 receptor_ubuntu.py 127.0.0.1
```
*(Si WSL no abre la ventana gráfica, corre `python receptor.py 127.0.0.1` desde un PowerShell de Windows).*

**Paso 5: Limpiar almacenamiento (Opcional)**
La compilación dejó 2GB de basura. Bórrala en Ubuntu con:
```bash
rm -rf ~/librealsense
```

**Paso 6: Devolver la cámara a Windows**
En PowerShell como Administrador:
```powershell
usbipd detach --busid <TU_BUSID>
usbipd unbind --busid <TU_BUSID>
# Para borrar el programa si ya no lo usas: winget uninstall dorssel.usbipd-win
```

---

## 📡 Ejecutar el receptor en OTRA computadora
El protocolo RTSP está diseñado para redes. Para ver la cámara desde otra computadora física conectada al mismo WiFi o cable:

1. Descubre la IP de la computadora que tiene la cámara conectada (En Windows abre PowerShell y escribe `ipconfig` -> busca Dirección IPv4, ej: `192.168.1.42`).
2. Ve a la **segunda computadora** (la que va a recibir el video), abre la terminal, activa el entorno y ejecuta el script apuntando a esa IP.

**Si el receptor es Windows:**
```powershell
cd "C:\ruta\al\proyecto"
.\venv\Scripts\Activate.ps1
python receptor.py 192.168.1.42
```

**Si el receptor es Ubuntu/Linux:**
```bash
cd /ruta/al/proyecto
source .venv/bin/activate
python3 receptor_ubuntu.py 192.168.1.42
```

*(Nota: Si usas el emisor dentro de WSL, Windows bloqueará la conexión a otras computadoras físicas. Para transmitir a otra PC, es altamente recomendado usar el emisor en Windows Nativo u Ubuntu Nativo).*

---

## 🛠️ Resolución de Problemas

- **"No se detectaron cámaras"**: Asegúrate de que no haya otro programa usando la cámara (Zoom, OBS). En WSL, verifica que hayas hecho el `usbipd attach`.
- **"MediaMTX terminó inesperadamente"**: El puerto `8554` ya está en uso. Usa `python emisor.py --puerto 9554` y conéctate con `python receptor.py <IP> 9554`.
- **"Video lento o entrecortado"**: Reduce la calidad con `python emisor.py --calidad 1000`. Usa WiFi 5GHz o cable Ethernet.

---

## ⚙️ Explicación Técnica Detallada

1. **Multiplexado con MediaMTX**: El emisor envía el video **una sola vez** al servidor local. MediaMTX se encarga de retransmitirlo a todos los receptores (clientes) que se conecten, evitando saturar el hardware de la cámara.
2. **Procesamiento de Imágenes**: 
   - La profundidad se convierte a un mapa de calor usando `cv2.COLORMAP_JET`.
   - Las resoluciones nativas (RGB 1080p y Depth/IR 720p) se redimensionan y concatenan matemáticamente usando `numpy` en un lienzo de `1920x1440`.
3. **Compresión H.264 (FFmpeg)**: El lienzo en crudo pasa por una tubería (pipe) hacia FFmpeg, configurado con el preset `ultrafast` y `zerolatency` para empujar los paquetes vía RTP/TCP.

### Estructura del proyecto
```text
Demo RTSP/
├── emisor.py              # Emisor para Windows nativo
├── emisor_ubuntu.py       # Emisor para Linux/WSL
├── receptor.py            # Receptor para Windows nativo
├── receptor_ubuntu.py     # Receptor para Linux/WSL
├── install_realsense.sh   # Script de compilación USB para WSL
├── requirements.txt       # Dependencias de Python (opencv-python, numpy, etc.)
└── .venv/                 # Entorno virtual de Python
```
