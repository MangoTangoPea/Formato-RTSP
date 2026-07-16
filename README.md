# Sistema RTSP Modular para Intel RealSense D435 y Grabación Interactiva

Este sistema permite capturar, inyectar metadatos ocultos mediante **Esteganografía LSB (Least Significant Bit)** y transmitir en tiempo real 4 canales de vídeo independientes desde una cámara **Intel RealSense D435** utilizando el protocolo **RTSP** a través de **MediaMTX**.

El receptor se conecta a los 4 canales, reconstruye un mosaico HUD en tiempo real y ofrece la capacidad de **grabar en caliente en formato Matroska (.mkv)** con tan solo presionar una tecla.

---

## Estructura del Proyecto

*   **`camara_realsense.py`**: Módulo que inicializa robustamente la cámara (incluyendo reset por hardware). Autodetecta la versión del puerto USB y ajusta resoluciones y FPS para evitar saturación de bus.
*   **`servidor_rtsp.py`**: Módulo que descarga, configura y arranca de forma transparente el servidor **MediaMTX** en local y los publicadores **FFmpeg** en modo push.
*   **`esteganografia.py`**: Módulo de inyección y extracción LSB con redundancia de 8 píxeles por bit (majority voting) para resiliencia ante pérdidas H.264.
*   **`emisor_app.py`**: Aplicación de consola que orquesta la captura de la cámara y la transmisión RTSP.
*   **`receptor_app.py`**: Aplicación de visualización interactiva que muestra el HUD con latencias, audita la sincronía física y permite grabar en caliente.

---

## Requisitos de Software

*   **Python 3.8+**
*   **FFmpeg** instalado en el sistema (en Ubuntu: `sudo apt install ffmpeg`).
*   **SDK Intel RealSense** y reglas udev configuradas (en Ubuntu).

### Instalar dependencias en el Entorno Virtual:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## Cómo Ejecutar

### 1. Iniciar el Emisor (Máquina con la Cámara)

Desde la terminal en el emisor:

```bash
python3 emisor_app.py
```

*Nota: La primera vez descargará automáticamente el binario de MediaMTX adaptado a tu arquitectura de CPU.*

El emisor mostrará un banner con las URLs de transmisión:
- Color (RGB): `rtsp://<IP_EMISOR>:8554/color`
- Profundidad: `rtsp://<IP_EMISOR>:8554/depth`
- Infrarrojo 1: `rtsp://<IP_EMISOR>:8554/ir1`
- Infrarrojo 2: `rtsp://<IP_EMISOR>:8554/ir2`

### 2. Iniciar el Receptor (Máquina de Visualización)

Desde la máquina receptora:

```bash
python3 receptor_app.py <IP_EMISOR>
```

---

## Controles del Receptor (Ventana OpenCV)

*   **`r`** : **Iniciar/Detener la grabación local** en caliente. Guarda el vídeo Matroska (.mkv) en la carpeta local `grabaciones/` con nombre dinámico `grabacion_YYYYMMDD_HHMMSS.mkv`.
*   **`m`** : Cambiar a vista **Mosaico** (pantalla dividida en 4 cámaras).
*   **`1`** : Ver únicamente el canal **Color** (RGB 1080p).
*   **`2`** : Ver únicamente el canal **Infrarrojo Izquierdo 1** (escala de grises).
*   **`3`** : Ver únicamente el canal **Profundidad** (heatmap Jet).
*   **`4`** : Ver únicamente el canal **Infrarrojo Derecho 2** (escala de grises).
*   **`h`** : Mostrar u ocultar la barra HUD superior de control de red.
*   **`f`** : Alternar pantalla completa.
*   **`q` / `ESC`** : Cerrar la ventana del receptor de manera limpia.
