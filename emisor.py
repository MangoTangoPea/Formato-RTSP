#!/usr/bin/env python3
"""
Emisor RTSP: captura vídeo de la cámara local y lo publica como flujo RTSP
mediante FFmpeg + MediaMTX.

Arquitectura del protocolo RTSP (Real Time Streaming Protocol):
───────────────────────────────────────────────────────────────
  ┌──────────┐   raw frames    ┌─────────┐   RTSP/RTP    ┌──────────┐
  │  Cámara  │ ──────────────► │ FFmpeg  │ ────────────► │ MediaMTX │
  │ (OpenCV) │   vía stdin     │ (H.264) │  push RTSP    │ (server) │
  └──────────┘                 └─────────┘               └──────────┘
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

Ejemplo:
    python emisor.py --puerto 8554 --cam 0 --calidad 2000
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

# ─── Intentar importar OpenCV ─────────────────────────────────────────────
try:
    import cv2
except ImportError:
    print("Error: opencv-python no está instalado.")
    print("  Instálalo con:  pip install opencv-python")
    sys.exit(1)

# ─── Intentar importar numpy ──────────────────────────────────────────────
try:
    import numpy as np
except ImportError:
    print("Error: numpy no está instalado.")
    print("  Instálalo con:  pip install numpy")
    sys.exit(1)


# ═══════════════════════════════════════════════════════════════════════════
# CONSTANTES Y CONFIGURACIÓN POR DEFECTO
# ═══════════════════════════════════════════════════════════════════════════

# Puerto RTSP estándar según RFC 2326 (Real Time Streaming Protocol)
PUERTO_RTSP_DEFECTO = 8554

# Ruta del flujo RTSP — los clientes se conectarán a rtsp://<IP>:<puerto>/camara
RUTA_FLUJO = "camara"

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

def verificar_ffmpeg():
    """
    Verifica que FFmpeg esté instalado y accesible en el PATH del sistema.
    FFmpeg es necesario para codificar el vídeo en H.264 y enviarlo al
    servidor RTSP mediante el protocolo RTP empaquetado en contenedor RTSP.

    Retorna True si FFmpeg está disponible, False en caso contrario.
    """
    try:
        # Ejecutar 'ffmpeg -version' para verificar su existencia
        resultado = subprocess.run(
            ["ffmpeg", "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
        )
        return resultado.returncode == 0
    except FileNotFoundError:
        # FFmpeg no se encontró en el PATH
        return False


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


def listar_camaras():
    """
    Intenta detectar las cámaras disponibles probando índices 0-4.
    OpenCV no tiene una API directa para enumerar dispositivos,
    así que intentamos abrir cada índice.

    Retorna una lista de índices de cámaras disponibles.
    """
    camaras = []
    for i in range(5):
        cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
        if cap.isOpened():
            camaras.append(i)
            cap.release()
    return camaras


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


# ═══════════════════════════════════════════════════════════════════════════
# FUNCIÓN PRINCIPAL DEL EMISOR
# ═══════════════════════════════════════════════════════════════════════════

def iniciar_emisor(indice_camara=0, puerto=PUERTO_RTSP_DEFECTO, bitrate_kbps=2000):
    """
    Función principal del emisor RTSP.

    Flujo de operación:
    1. Verifica que FFmpeg esté instalado
    2. Descarga/inicia MediaMTX (servidor RTSP)
    3. Abre la cámara con OpenCV
    4. Lanza FFmpeg para codificar H.264 y publicar en MediaMTX vía RTSP
    5. Lee fotogramas de la cámara y los envía por stdin a FFmpeg
    6. FFmpeg empaqueta los fotogramas en paquetes RTP y los envía al servidor

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
        indice_camara: Índice del dispositivo de captura (0 = cámara por defecto)
        puerto: Puerto TCP donde escuchará el servidor RTSP
        bitrate_kbps: Tasa de bits para la codificación H.264 en kbps
    """
    proceso_mediamtx = None
    proceso_ffmpeg = None
    camara = None

    try:
        # ─── Paso 1: Verificar que FFmpeg esté disponible ──────────────
        print("\n" + "═" * 60)
        print("  EMISOR RTSP — Transmisión de cámara local")
        print("═" * 60)

        print("\n[1/4] Verificando FFmpeg ...")
        if not verificar_ffmpeg():
            print("  ✗ FFmpeg no está instalado o no está en el PATH.")
            print("  Para instalar FFmpeg en Windows:")
            print("    1. Descarga desde: https://www.gyan.dev/ffmpeg/builds/")
            print("    2. Extrae y añade la carpeta 'bin' al PATH del sistema")
            print("    3. Reinicia la terminal")
            sys.exit(1)
        print("  ✓ FFmpeg encontrado")

        # ─── Paso 2: Iniciar el servidor MediaMTX ─────────────────────
        print(f"\n[2/4] Preparando servidor RTSP (MediaMTX) ...")
        proceso_mediamtx = iniciar_mediamtx(puerto)

        # ─── Paso 3: Abrir la cámara con OpenCV ───────────────────────
        print(f"\n[3/4] Abriendo cámara (índice {indice_camara}) ...")

        # En Windows, usar DirectShow (CAP_DSHOW) para mejor compatibilidad
        if platform.system() == "Windows":
            camara = cv2.VideoCapture(indice_camara, cv2.CAP_DSHOW)
        else:
            camara = cv2.VideoCapture(indice_camara)

        if not camara.isOpened():
            print(f"  ✗ No se pudo abrir la cámara con índice {indice_camara}")
            # Mostrar cámaras disponibles como ayuda
            disponibles = listar_camaras()
            if disponibles:
                print(f"  Cámaras disponibles: {disponibles}")
            else:
                print("  No se detectaron cámaras conectadas.")
            sys.exit(1)

        # Obtener las propiedades de la cámara para configurar FFmpeg
        ancho = int(camara.get(cv2.CAP_PROP_FRAME_WIDTH))
        alto = int(camara.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = int(camara.get(cv2.CAP_PROP_FPS))

        # Algunas cámaras reportan 0 FPS; usar 30 como valor seguro
        if fps <= 0 or fps > 120:
            fps = 30

        print(f"  ✓ Cámara abierta: {ancho}x{alto} @ {fps} FPS")

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
        # -s {ancho}x{alto}    → Resolución del vídeo de entrada
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
            "ffmpeg",
            "-y",                           # Sobrescribir sin preguntar
            "-f", "rawvideo",               # Formato de entrada: vídeo crudo
            "-vcodec", "rawvideo",          # Códec de entrada: sin compresión
            "-pix_fmt", "bgr24",            # Píxeles en BGR24 (formato OpenCV)
            "-s", f"{ancho}x{alto}",        # Resolución del frame de entrada
            "-r", str(fps),                 # FPS de entrada
            "-i", "-",                      # Entrada desde stdin (pipe)
            "-c:v", "libx264",              # Codificador H.264 (x264)
            "-pix_fmt", "yuv420p",          # Espacio de color de salida (YUV 4:2:0)
            "-preset", "ultrafast",         # Preset de velocidad (mínima latencia)
            "-tune", "zerolatency",         # Sintonizar para latencia cero
            "-b:v", f"{bitrate_kbps}k",     # Bitrate de salida en kbps
            "-g", str(fps * 2),             # Tamaño del GOP (Group of Pictures)
            "-f", "rtsp",                   # Formato de salida: RTSP
            "-rtsp_transport", "tcp",       # Transporte RTSP sobre TCP
            url_rtsp                        # URL de destino RTSP
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
        print(f"  Los receptores deben conectarse a esta URL.")
        print(f"  También puedes probar con VLC: Medio → Abrir ubicación de red")
        print("═" * 60)
        print("\n  Presiona Ctrl+C para detener la transmisión.\n")

        # ─── Bucle principal: capturar y enviar fotogramas ─────────────
        # Cada fotograma capturado por OpenCV se escribe como bytes crudos
        # en el stdin de FFmpeg. FFmpeg los codifica en H.264, los empaqueta
        # en paquetes RTP y los envía al servidor MediaMTX por RTSP/TCP.
        fotogramas_enviados = 0
        tiempo_inicio = time.time()

        while True:
            # Leer un fotograma de la cámara
            ret, fotograma = camara.read()

            if not ret:
                # Si la lectura falla (cámara desconectada, etc.)
                print("  ⚠ Error al leer de la cámara, reintentando...")
                time.sleep(0.1)
                continue

            try:
                # Escribir los bytes crudos del fotograma en el stdin de FFmpeg
                # El fotograma tiene formato BGR24 con shape (alto, ancho, 3)
                # Total de bytes por fotograma = ancho × alto × 3
                proceso_ffmpeg.stdin.write(fotograma.tobytes())
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

        # Liberar la cámara
        if camara and camara.isOpened():
            camara.release()
            print("  ✓ Cámara liberada")

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
        description="Emisor RTSP: transmite la cámara local por protocolo RTSP.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos de uso:
  python emisor.py                          # Cámara 0, puerto 8554, 2000 kbps
  python emisor.py --cam 1                  # Usar segunda cámara
  python emisor.py --puerto 9554            # Usar puerto alternativo
  python emisor.py --calidad 4000           # Mayor calidad de vídeo
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
        help="Índice de la cámara a usar (por defecto: 0)"
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
        help="Listar cámaras disponibles y salir"
    )

    args = parser.parse_args()

    # Si se pide listar cámaras, mostrar y salir
    if args.listar_camaras:
        print("Buscando cámaras disponibles ...")
        camaras = listar_camaras()
        if camaras:
            print(f"  Cámaras detectadas en índices: {camaras}")
        else:
            print("  No se detectaron cámaras.")
        sys.exit(0)

    # Iniciar el emisor con los parámetros proporcionados
    iniciar_emisor(
        indice_camara=args.cam,
        puerto=args.puerto,
        bitrate_kbps=args.calidad
    )
