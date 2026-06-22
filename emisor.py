#!/usr/bin/env python3
"""
Emisor RTSP para Intel RealSense D435.

Captura el flujo RGB y el flujo de profundidad usando el SDK oficial de Intel
(pyrealsense2), colorea el mapa de profundidad con un colormap, une ambos
fotogramas lado a lado en un solo frame y lo transmite como flujo RTSP H.264
mediante FFmpeg + MediaMTX.

Arquitectura del sistema:
─────────────────────────
  ┌────────────────┐  RGB + Depth   ┌────────────┐  raw frame   ┌─────────┐
  │  Intel D435    │ ─────────────► │ Procesado  │ ───────────► │ FFmpeg  │
  │ (pyrealsense2) │  via SDK       │ lado a lado│  vía stdin   │ (H.264) │
  └────────────────┘                └────────────┘              └────┬────┘
                                                                    │
                                                              RTSP/RTP push
                                                                    │
                                                              ┌─────▼──────┐
                                                              │  MediaMTX  │
                                                              │  (server)  │
                                                              └─────┬──────┘
                                                                    │
                                                          rtsp://<IP>:8554/camara
                                                                    │
                                                               ┌────▼─────┐
                                                               │ Receptor │
                                                               └──────────┘

RTSP usa TCP para la señalización (DESCRIBE, SETUP, PLAY, TEARDOWN)
y RTP sobre UDP (o TCP entrelazado) para el transporte de los paquetes
de vídeo codificados en H.264.

Uso:
    python emisor.py [--puerto PUERTO] [--cam INDICE] [--calidad BITRATE_KBPS]
    python emisor.py --listar-camaras

Ejemplo:
    python emisor.py --puerto 8554 --cam 0 --calidad 4000
"""

import subprocess
import sys
import os
import signal
import time
import argparse
import platform
import zipfile
import shutil
import urllib.request

# ─── Configurar la codificación de la consola para Unicode en Windows ─────
# Evita UnicodeEncodeError al imprimir caracteres como ═, ✓, ✗, ⚠, etc.
if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass


# ─── Intentar importar OpenCV ─────────────────────────────────────────────
try:
    import cv2
except ImportError:
    print("Error: opencv-python no está instalado.")
    print("  Instálalo con:  pip install -r requirements.txt")
    sys.exit(1)

# ─── Intentar importar numpy ──────────────────────────────────────────────
try:
    import numpy as np
except ImportError:
    print("Error: numpy no está instalado.")
    print("  Instálalo con:  pip install -r requirements.txt")
    sys.exit(1)

# ─── Intentar importar pyrealsense2 ──────────────────────────────────────
try:
    import pyrealsense2 as rs
except ImportError:
    print("Error: pyrealsense2 no está instalado.")
    print("  Instálalo con:  pip install pyrealsense2")
    print("  O bien:         pip install -r requirements.txt")
    sys.exit(1)

# ─── Intentar importar imageio_ffmpeg ─────────────────────────────────────
# Este paquete incluye un binario estático de FFmpeg que se instala vía pip,
# eliminando la necesidad de instalar FFmpeg manualmente en el sistema.
try:
    import imageio_ffmpeg
except ImportError:
    print("Error: imageio-ffmpeg no está instalado.")
    print("  Instálalo con:  pip install -r requirements.txt")
    sys.exit(1)


# ═══════════════════════════════════════════════════════════════════════════
# CONSTANTES Y CONFIGURACIÓN POR DEFECTO
# ═══════════════════════════════════════════════════════════════════════════

# Puerto RTSP estándar según RFC 2326 (Real Time Streaming Protocol)
PUERTO_RTSP_DEFECTO = 8554

# Ruta del flujo RTSP — los clientes se conectarán a rtsp://<IP>:<puerto>/camara
RUTA_FLUJO = "camara"

# Resolución y FPS por defecto para los flujos RealSense
ANCHO_RS = 640
ALTO_RS = 480
FPS_RS = 30

# Directorio donde se almacenará MediaMTX (junto al script)
DIR_BASE = os.path.dirname(os.path.abspath(__file__))
DIR_MEDIAMTX = os.path.join(DIR_BASE, "mediamtx")

# URL de descarga de MediaMTX para Windows (versión estable)
MEDIAMTX_VERSION = "v1.12.2"
MEDIAMTX_URL = (
    f"https://github.com/bluenviron/mediamtx/releases/download/"
    f"{MEDIAMTX_VERSION}/mediamtx_{MEDIAMTX_VERSION}_windows_amd64.zip"
)


# ═══════════════════════════════════════════════════════════════════════════
# FUNCIONES AUXILIARES
# ═══════════════════════════════════════════════════════════════════════════

def obtener_ruta_ffmpeg():
    """
    Obtiene la ruta al ejecutable de FFmpeg usando imageio-ffmpeg.
    Este paquete instala un binario estático de FFmpeg dentro del entorno
    virtual de Python, por lo que no es necesario instalar FFmpeg
    manualmente en el sistema operativo.

    imageio_ffmpeg.get_ffmpeg_exe() busca FFmpeg en este orden:
      1. Variable de entorno IMAGEIO_FFMPEG_EXE (si está definida)
      2. Binario incluido en el paquete imageio-ffmpeg (instalado vía pip)
      3. Instalación del sistema (PATH) como respaldo

    Retorna la ruta completa al ejecutable de FFmpeg, o None si no se encuentra.
    """
    try:
        # Obtener la ruta al binario de FFmpeg incluido en imageio-ffmpeg
        ruta = imageio_ffmpeg.get_ffmpeg_exe()

        # Verificar que el binario funcione ejecutando 'ffmpeg -version'
        resultado = subprocess.run(
            [ruta, "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
        )
        if resultado.returncode == 0:
            return ruta
        return None
    except Exception:
        # Si imageio_ffmpeg no encuentra el binario, lanza RuntimeError
        return None


def descargar_mediamtx():
    """
    Descarga y extrae MediaMTX, el servidor RTSP/RTMP/HLS/WebRTC.
    MediaMTX actúa como el punto de distribución RTSP: recibe el flujo
    de FFmpeg y lo re-distribuye a todos los clientes (receptores) que
    se conecten a la URL RTSP.

    MediaMTX implementa la señalización RTSP completa:
      - DESCRIBE: el cliente solicita la descripción SDP del flujo
      - SETUP: se negocian los puertos RTP/RTCP para el transporte
      - PLAY: inicia la transmisión de paquetes RTP con el vídeo H.264
      - TEARDOWN: finaliza la sesión RTSP

    Retorna la ruta al ejecutable de MediaMTX.
    """
    exe_path = os.path.join(DIR_MEDIAMTX, "mediamtx.exe")

    # Si ya existe el ejecutable, no descargar de nuevo
    if os.path.isfile(exe_path):
        print(f"  ✓ MediaMTX ya existe en: {exe_path}")
        return exe_path

    print(f"  ↓ Descargando MediaMTX {MEDIAMTX_VERSION} ...")
    print(f"    URL: {MEDIAMTX_URL}")

    # Crear el directorio si no existe
    os.makedirs(DIR_MEDIAMTX, exist_ok=True)

    # Descargar el archivo ZIP
    zip_path = os.path.join(DIR_MEDIAMTX, "mediamtx.zip")
    try:
        urllib.request.urlretrieve(MEDIAMTX_URL, zip_path)
    except Exception as e:
        print(f"  ✗ Error al descargar MediaMTX: {e}")
        print("    Descárgalo manualmente desde:")
        print(f"    {MEDIAMTX_URL}")
        print(f"    y extráelo en: {DIR_MEDIAMTX}")
        sys.exit(1)

    # Extraer el contenido del ZIP
    print("  ↓ Extrayendo MediaMTX ...")
    try:
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(DIR_MEDIAMTX)
    except zipfile.BadZipFile:
        print("  ✗ El archivo descargado no es un ZIP válido.")
        os.remove(zip_path)
        sys.exit(1)

    # Limpiar el archivo ZIP
    os.remove(zip_path)

    if os.path.isfile(exe_path):
        print(f"  ✓ MediaMTX instalado en: {exe_path}")
        return exe_path
    else:
        print("  ✗ No se encontró mediamtx.exe después de la extracción.")
        sys.exit(1)


def iniciar_mediamtx(puerto):
    """
    Inicia el servidor MediaMTX como un proceso en segundo plano.
    El servidor escucha en el puerto RTSP indicado y acepta flujos
    publicados por FFmpeg.

    MediaMTX gestiona la capa de señalización RTSP (TCP, puerto 8554)
    y la capa de transporte RTP (UDP, puertos efímeros negociados
    durante el SETUP RTSP).

    Args:
        puerto: Puerto TCP en el que el servidor RTSP escuchará.

    Retorna el objeto subprocess.Popen del proceso MediaMTX.
    """
    exe_path = descargar_mediamtx()

    # Configurar la variable de entorno para cambiar el puerto RTSP
    # MediaMTX lee MTX_RTSPADDRESS para saber en qué dirección/puerto escuchar
    entorno = os.environ.copy()
    entorno["MTX_RTSPADDRESS"] = f":{puerto}"

    print(f"  → Iniciando MediaMTX en el puerto {puerto} ...")

    try:
        # Iniciar MediaMTX como proceso hijo
        # Redirigir stdout/stderr para capturar mensajes del servidor
        proceso_mtx = subprocess.Popen(
            [exe_path],
            cwd=DIR_MEDIAMTX,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=entorno,
            creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
        )
    except PermissionError:
        print("  ✗ Sin permisos para ejecutar MediaMTX.")
        print("    Intenta ejecutar como administrador o permite el acceso en el firewall.")
        sys.exit(1)
    except Exception as e:
        print(f"  ✗ Error al iniciar MediaMTX: {e}")
        sys.exit(1)

    # Esperar un momento para que el servidor inicie completamente
    time.sleep(2)

    # Verificar que el proceso siga vivo
    if proceso_mtx.poll() is not None:
        # El proceso terminó — leer su salida para diagnóstico
        salida = proceso_mtx.stdout.read().decode(errors='replace')
        print(f"  ✗ MediaMTX terminó inesperadamente. Salida:")
        print(f"    {salida[:500]}")
        sys.exit(1)

    print(f"  ✓ MediaMTX iniciado (PID: {proceso_mtx.pid})")
    return proceso_mtx


def listar_camaras_realsense():
    """
    Detecta todas las cámaras Intel RealSense conectadas al sistema
    usando el SDK pyrealsense2.

    Para cada dispositivo encontrado, imprime:
      - Índice (para usar con --cam)
      - Nombre del dispositivo (e.g., "Intel RealSense D435")
      - Número de serie (e.g., "038422250711")

    Retorna la lista de dispositivos encontrados.
    """
    contexto = rs.context()
    dispositivos = contexto.query_devices()

    if len(dispositivos) == 0:
        print("\n  ✗ No se detectaron cámaras Intel RealSense conectadas.")
        print("    Asegúrate de que la cámara esté conectada por USB 3.0.")
        print("    Verifica que el Intel RealSense SDK esté instalado.")
        return []

    print(f"\n  Cámaras Intel RealSense detectadas: {len(dispositivos)}")
    print("  " + "─" * 56)
    print(f"  {'Índice':<8} {'Nombre':<30} {'Nº de Serie'}")
    print("  " + "─" * 56)

    lista = []
    for i, dev in enumerate(dispositivos):
        nombre = dev.get_info(rs.camera_info.name)
        serie = dev.get_info(rs.camera_info.serial_number)
        print(f"  {i:<8} {nombre:<30} {serie}")
        lista.append({"indice": i, "nombre": nombre, "serie": serie})

    print("  " + "─" * 56)
    print(f"\n  Uso: python emisor.py --cam <índice>")
    return lista


def obtener_ip_local():
    """
    Obtiene la dirección IP local de la máquina en la red LAN.
    Se conecta a un socket UDP externo (sin enviar datos) para
    determinar qué interfaz de red usaría el sistema operativo
    para alcanzar la red externa.

    Retorna la IP local como cadena (e.g., '192.168.1.42').
    """
    import socket
    try:
        # Crear un socket UDP — no se envían datos realmente
        # Conectar a una IP pública para que el SO seleccione la interfaz correcta
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def obtener_serial_por_indice(indice):
    """
    Obtiene el número de serie de la cámara RealSense en el índice dado.
    Si solo hay una cámara y el índice es 0, retorna None para usar
    la cámara por defecto sin filtrar por serial.

    Args:
        indice: Índice del dispositivo (0-based).

    Retorna el número de serie como cadena, o None si se usa la cámara
    por defecto (índice 0 con un solo dispositivo).
    """
    contexto = rs.context()
    dispositivos = contexto.query_devices()

    if len(dispositivos) == 0:
        print("  ✗ No se detectaron cámaras Intel RealSense.")
        sys.exit(1)

    if indice >= len(dispositivos):
        print(f"  ✗ Índice de cámara {indice} fuera de rango.")
        print(f"    Solo hay {len(dispositivos)} cámara(s) disponible(s).")
        print(f"    Usa --listar-camaras para ver las opciones.")
        sys.exit(1)

    # Si hay una sola cámara e índice 0, no necesitamos filtrar por serial
    if len(dispositivos) == 1 and indice == 0:
        return None

    return dispositivos[indice].get_info(rs.camera_info.serial_number)


# ═══════════════════════════════════════════════════════════════════════════
# FUNCIÓN PRINCIPAL DEL EMISOR
# ═══════════════════════════════════════════════════════════════════════════

def iniciar_emisor(indice_camara=0, puerto=PUERTO_RTSP_DEFECTO, bitrate_kbps=2000):
    """
    Función principal del emisor RTSP con Intel RealSense D435.

    Flujo de operación:
    1. Verifica que FFmpeg esté instalado
    2. Descarga/inicia MediaMTX (servidor RTSP)
    3. Abre la cámara RealSense con pyrealsense2 (flujos RGB + Depth)
    4. Lanza FFmpeg para codificar H.264 y publicar en MediaMTX vía RTSP
    5. Lee fotogramas RGB y de profundidad, los une lado a lado y los envía
       por stdin a FFmpeg
    6. FFmpeg empaqueta los fotogramas en paquetes RTP y los envía al servidor

    El fotograma combinado tiene el doble de ancho (RGB | Depth coloreado):
      ┌──────────────────┬──────────────────┐
      │   RGB (color)    │  Depth (JET)     │
      │   640 × 480      │  640 × 480       │
      └──────────────────┴──────────────────┘
              → fotograma de 1280 × 480 →

    Protocolo de red involucrado:
    ───────────────────────────
    - RTSP (RFC 2326): señalización sobre TCP (puerto 8554)
      → FFmpeg envía ANNOUNCE + SETUP + RECORD al servidor MediaMTX
      → Los receptores envían DESCRIBE + SETUP + PLAY para recibir
    - RTP (RFC 3550): transporte de paquetes de vídeo H.264 sobre UDP
      → Cada fotograma H.264 se fragmenta en paquetes RTP de ~1400 bytes
    - RTCP (RFC 3550): control de calidad (informes de recepción)
      → Se intercambian periódicamente entre servidor y clientes

    Args:
        indice_camara: Índice del dispositivo RealSense (0 = primera cámara)
        puerto: Puerto TCP donde escuchará el servidor RTSP
        bitrate_kbps: Tasa de bits para la codificación H.264 en kbps
    """
    proceso_mediamtx = None
    proceso_ffmpeg = None
    pipeline = None

    try:
        # ─── Paso 1: Verificar que FFmpeg esté disponible ──────────────
        print("\n" + "═" * 60)
        print("  EMISOR RTSP — Intel RealSense D435 (RGB + Profundidad)")
        print("═" * 60)

        print("\n[1/4] Verificando FFmpeg (vía imageio-ffmpeg) ...")
        ruta_ffmpeg = obtener_ruta_ffmpeg()
        if ruta_ffmpeg is None:
            print("  ✗ No se pudo obtener el binario de FFmpeg.")
            print("  Asegúrate de haber instalado las dependencias:")
            print("    pip install -r requirements.txt")
            sys.exit(1)
        print(f"  ✓ FFmpeg encontrado: {ruta_ffmpeg}")

        # ─── Paso 2: Iniciar el servidor MediaMTX ─────────────────────
        print(f"\n[2/4] Preparando servidor RTSP (MediaMTX) ...")
        proceso_mediamtx = iniciar_mediamtx(puerto)

        # ─── Paso 3: Abrir la cámara RealSense ────────────────────────
        print(f"\n[3/4] Abriendo cámara Intel RealSense (índice {indice_camara}) ...")

        # Obtener el serial de la cámara seleccionada (para multi-cámara)
        serial = obtener_serial_por_indice(indice_camara)

        # Crear el pipeline de captura RealSense
        pipeline = rs.pipeline()
        config = rs.config()

        # Si hay varias cámaras, seleccionar la indicada por su serial
        if serial is not None:
            config.enable_device(serial)
            print(f"  → Seleccionada cámara con serial: {serial}")

        # Habilitar flujo de color (RGB) a 640×480 @ 30 FPS
        config.enable_stream(
            rs.stream.color, ANCHO_RS, ALTO_RS, rs.format.bgr8, FPS_RS
        )

        # Habilitar flujo de profundidad (Depth) a 640×480 @ 30 FPS
        config.enable_stream(
            rs.stream.depth, ANCHO_RS, ALTO_RS, rs.format.z16, FPS_RS
        )

        # Iniciar el pipeline
        try:
            perfil = pipeline.start(config)
        except RuntimeError as e:
            print(f"  ✗ No se pudo iniciar la cámara RealSense: {e}")
            print("    Verifica que la cámara esté conectada por USB 3.0.")
            print("    Usa --listar-camaras para ver dispositivos disponibles.")
            sys.exit(1)

        # Obtener información del dispositivo conectado
        dispositivo = perfil.get_device()
        nombre_cam = dispositivo.get_info(rs.camera_info.name)
        serial_cam = dispositivo.get_info(rs.camera_info.serial_number)
        firmware = dispositivo.get_info(rs.camera_info.firmware_version)

        print(f"  ✓ Cámara abierta: {nombre_cam}")
        print(f"    Serial: {serial_cam}")
        print(f"    Firmware: {firmware}")
        print(f"    Flujo RGB:   {ANCHO_RS}×{ALTO_RS} @ {FPS_RS} FPS")
        print(f"    Flujo Depth: {ANCHO_RS}×{ALTO_RS} @ {FPS_RS} FPS")

        # El fotograma combinado tiene el doble de ancho (lado a lado)
        ancho_combinado = ANCHO_RS * 2  # 1280
        alto_combinado = ALTO_RS        # 480

        print(f"    Fotograma combinado: {ancho_combinado}×{alto_combinado}")

        # Crear el filtro de colormap para el mapa de profundidad
        # Este filtro se puede usar opcionalmente, pero nosotros usaremos
        # cv2.applyColorMap para mayor flexibilidad y compatibilidad
        colorizer = rs.colorizer()

        # ─── Paso 4: Lanzar FFmpeg para codificar y publicar RTSP ─────
        print(f"\n[4/4] Iniciando transmisión RTSP ...")

        # URL RTSP donde FFmpeg publicará el flujo dentro de MediaMTX
        # FFmpeg usa RTSP en modo "publicación" (ANNOUNCE + RECORD)
        url_rtsp = f"rtsp://127.0.0.1:{puerto}/{RUTA_FLUJO}"

        # Comando FFmpeg:
        # ─────────────────────────────────────────────────────────────
        # -f rawvideo          → El formato de entrada es vídeo crudo (sin contenedor)
        # -vcodec rawvideo     → El códec de entrada es datos crudos (sin comprimir)
        # -pix_fmt bgr24       → Formato de píxeles BGR de 24 bits (formato nativo de OpenCV)
        # -s {ancho}x{alto}    → Resolución del vídeo de entrada (1280×480 combinado)
        # -r {fps}             → Tasa de fotogramas de entrada
        # -i -                 → Leer la entrada desde stdin (pipe desde Python)
        # -c:v libx264         → Codificar el vídeo con el códec H.264 (x264)
        # -pix_fmt yuv420p     → Convertir a YUV 4:2:0 (requerido por H.264)
        # -preset ultrafast    → Menor compresión pero mínima latencia
        # -tune zerolatency    → Optimizar para streaming en tiempo real
        # -b:v {bitrate}k      → Tasa de bits objetivo para el flujo de salida
        # -g {fps*2}           → Intervalo de keyframes (GOP): cada 2 segundos
        # -f rtsp              → Formato de salida: protocolo RTSP
        # -rtsp_transport tcp  → Usar TCP para el transporte RTSP (más fiable en LAN)
        # {url_rtsp}           → URL de destino en el servidor MediaMTX
        comando_ffmpeg = [
            ruta_ffmpeg,                              # Ruta al binario de FFmpeg
            "-y",                                     # Sobrescribir sin preguntar
            "-f", "rawvideo",                         # Formato de entrada: vídeo crudo
            "-vcodec", "rawvideo",                    # Códec de entrada: sin compresión
            "-pix_fmt", "bgr24",                      # Píxeles en BGR24 (formato OpenCV)
            "-s", f"{ancho_combinado}x{alto_combinado}",  # Resolución combinada
            "-r", str(FPS_RS),                        # FPS de entrada
            "-i", "-",                                # Entrada desde stdin (pipe)
            "-c:v", "libx264",                        # Codificador H.264 (x264)
            "-pix_fmt", "yuv420p",                    # Espacio de color de salida
            "-preset", "ultrafast",                   # Preset de velocidad
            "-tune", "zerolatency",                   # Sintonizar para latencia cero
            "-b:v", f"{bitrate_kbps}k",               # Bitrate de salida en kbps
            "-g", str(FPS_RS * 2),                    # Tamaño del GOP
            "-f", "rtsp",                             # Formato de salida: RTSP
            "-rtsp_transport", "tcp",                 # Transporte RTSP sobre TCP
            url_rtsp                                  # URL de destino RTSP
        ]

        # Iniciar el proceso FFmpeg con stdin como pipe para enviar fotogramas
        proceso_ffmpeg = subprocess.Popen(
            comando_ffmpeg,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
        )

        # Esperar brevemente para que FFmpeg se conecte al servidor
        time.sleep(1)

        # Verificar que FFmpeg siga corriendo
        if proceso_ffmpeg.poll() is not None:
            stderr_out = proceso_ffmpeg.stderr.read().decode(errors='replace')
            print(f"  ✗ FFmpeg terminó inesperadamente:")
            print(f"    {stderr_out[:500]}")
            sys.exit(1)

        # Obtener la IP local para mostrar la URL completa
        ip_local = obtener_ip_local()

        print("  ✓ Transmisión RTSP activa")
        print("\n" + "═" * 60)
        print(f"  URL RTSP del flujo:")
        print(f"  → rtsp://{ip_local}:{puerto}/{RUTA_FLUJO}")
        print(f"")
        print(f"  Contenido: RGB (izquierda) | Profundidad (derecha)")
        print(f"  Resolución: {ancho_combinado}×{alto_combinado}")
        print(f"  Bitrate: {bitrate_kbps} kbps")
        print(f"")
        print(f"  Los receptores deben conectarse a esta URL.")
        print(f"  También puedes probar con VLC: Medio → Abrir ubicación de red")
        print("═" * 60)
        print("\n  Presiona Ctrl+C para detener la transmisión.\n")

        # ─── Bucle principal: capturar y enviar fotogramas ─────────────
        # Cada par de fotogramas (RGB + Depth) se procesa así:
        #   1. Se capturan ambos flujos sincronizados del pipeline RealSense
        #   2. El flujo de profundidad (Z16, 16-bit) se normaliza a 8 bits
        #   3. Se aplica cv2.applyColorMap con COLORMAP_JET para colorear
        #   4. Se concatenan lado a lado con np.hstack
        #   5. El fotograma combinado se escribe como bytes crudos en stdin de FFmpeg
        fotogramas_enviados = 0
        tiempo_inicio = time.time()

        while True:
            # Esperar un conjunto sincronizado de fotogramas (RGB + Depth)
            # pipeline.wait_for_frames() bloquea hasta que ambos flujos
            # tengan un fotograma disponible con timestamp coincidente
            try:
                frames = pipeline.wait_for_frames(timeout_ms=5000)
            except RuntimeError:
                print("  ⚠ Timeout esperando fotogramas de la cámara...")
                continue

            # Extraer el fotograma de color (RGB en formato BGR8)
            frame_color = frames.get_color_frame()
            # Extraer el fotograma de profundidad (Z16, 16 bits por píxel)
            frame_depth = frames.get_depth_frame()

            if not frame_color or not frame_depth:
                # Si alguno de los flujos no está listo, saltar
                continue

            # ─── Convertir fotogramas a arrays numpy ───────────────────
            # frame_color → numpy array de shape (480, 640, 3), dtype uint8, BGR
            imagen_color = np.asanyarray(frame_color.get_data())

            # frame_depth → numpy array de shape (480, 640), dtype uint16
            # Contiene distancias en milímetros (rango típico: 0-10000)
            datos_depth = np.asanyarray(frame_depth.get_data())

            # ─── Colorear el mapa de profundidad ──────────────────────
            # Paso 1: Normalizar de uint16 (0-65535) a uint8 (0-255)
            # Usamos cv2.convertScaleAbs que satura en 255 automáticamente
            # El factor alpha controla la escala: 0.03 mapea ~8500mm a 255
            depth_normalizado = cv2.convertScaleAbs(datos_depth, alpha=0.03)

            # Paso 2: Aplicar mapa de colores JET para visualización
            # COLORMAP_JET: azul (cerca) → verde (medio) → rojo (lejos)
            depth_coloreado = cv2.applyColorMap(depth_normalizado, cv2.COLORMAP_JET)

            # ─── Unir ambos fotogramas lado a lado ─────────────────────
            # hstack: RGB a la izquierda, Depth coloreado a la derecha
            # Resultado: array de shape (480, 1280, 3), dtype uint8, BGR
            fotograma_combinado = np.hstack((imagen_color, depth_coloreado))

            try:
                # Escribir los bytes crudos del fotograma combinado en stdin de FFmpeg
                # Total de bytes por fotograma = 1280 × 480 × 3 = 1,843,200 bytes
                proceso_ffmpeg.stdin.write(fotograma_combinado.tobytes())
                fotogramas_enviados += 1

                # Mostrar estadísticas cada 100 fotogramas
                if fotogramas_enviados % 100 == 0:
                    tiempo_transcurrido = time.time() - tiempo_inicio
                    fps_real = fotogramas_enviados / tiempo_transcurrido
                    print(f"  📹 Fotogramas enviados: {fotogramas_enviados} "
                          f"| FPS real: {fps_real:.1f} "
                          f"| Tiempo: {tiempo_transcurrido:.0f}s")

            except BrokenPipeError:
                # FFmpeg cerró su stdin — probablemente terminó
                print("  ✗ FFmpeg cerró la conexión (BrokenPipe).")
                break
            except OSError as e:
                # Error del sistema operativo al escribir en el pipe
                print(f"  ✗ Error de E/S con FFmpeg: {e}")
                break

    except KeyboardInterrupt:
        # El usuario presionó Ctrl+C para detener la transmisión
        print("\n\n  ⏹ Emisor detenido por el usuario (Ctrl+C).")

    except Exception as e:
        # Cualquier otra excepción no prevista
        print(f"\n  ✗ Error inesperado: {e}")

    finally:
        # ─── Limpieza de recursos ──────────────────────────────────────
        # Es fundamental cerrar todos los procesos y liberar la cámara
        # para evitar procesos huérfanos y bloqueos de hardware.
        print("\n  Liberando recursos ...")

        # Cerrar el pipe de stdin de FFmpeg para señalar fin de datos
        if proceso_ffmpeg and proceso_ffmpeg.stdin:
            try:
                proceso_ffmpeg.stdin.close()
            except Exception:
                pass

        # Terminar el proceso FFmpeg
        if proceso_ffmpeg and proceso_ffmpeg.poll() is None:
            try:
                proceso_ffmpeg.terminate()
                proceso_ffmpeg.wait(timeout=5)
                print("  ✓ FFmpeg detenido")
            except Exception:
                proceso_ffmpeg.kill()
                print("  ✓ FFmpeg forzado a detener")

        # Detener el pipeline RealSense y liberar la cámara
        if pipeline is not None:
            try:
                pipeline.stop()
                print("  ✓ Cámara RealSense liberada")
            except Exception:
                pass

        # Detener el servidor MediaMTX
        if proceso_mediamtx and proceso_mediamtx.poll() is None:
            try:
                proceso_mediamtx.terminate()
                proceso_mediamtx.wait(timeout=5)
                print("  ✓ MediaMTX detenido")
            except Exception:
                proceso_mediamtx.kill()
                print("  ✓ MediaMTX forzado a detener")

        print("\n  Emisor finalizado.\n")


# ═══════════════════════════════════════════════════════════════════════════
# PUNTO DE ENTRADA
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Parsear argumentos de línea de comandos
    parser = argparse.ArgumentParser(
        description="Emisor RTSP: transmite vídeo RGB + Profundidad de una cámara Intel RealSense D435.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos de uso:
  python emisor.py                          # Cámara 0, puerto 8554, 2000 kbps
  python emisor.py --cam 1                  # Usar segunda cámara RealSense
  python emisor.py --puerto 9554            # Usar puerto alternativo
  python emisor.py --calidad 4000           # Mayor calidad de vídeo
  python emisor.py --listar-camaras         # Ver cámaras RealSense detectadas
  python emisor.py --cam 0 --puerto 8554    # Todos los parámetros explícitos
        """
    )

    parser.add_argument(
        "--puerto",
        type=int,
        default=PUERTO_RTSP_DEFECTO,
        help=f"Puerto TCP del servidor RTSP (por defecto: {PUERTO_RTSP_DEFECTO})"
    )
    parser.add_argument(
        "--cam",
        type=int,
        default=0,
        help="Índice de la cámara RealSense a usar (por defecto: 0)"
    )
    parser.add_argument(
        "--calidad",
        type=int,
        default=2000,
        help="Bitrate de vídeo en kbps (por defecto: 2000)"
    )
    parser.add_argument(
        "--listar-camaras",
        action="store_true",
        help="Listar cámaras Intel RealSense conectadas y salir"
    )

    args = parser.parse_args()

    # Si se pide listar cámaras, mostrar info de RealSense y salir
    if args.listar_camaras:
        print("Buscando cámaras Intel RealSense ...")
        listar_camaras_realsense()
        sys.exit(0)

    # Iniciar el emisor con los parámetros proporcionados
    iniciar_emisor(
        indice_camara=args.cam,
        puerto=args.puerto,
        bitrate_kbps=args.calidad
    )
