#!/usr/bin/env python3
"""
Emisor RTSP para Intel RealSense D435.

Captura los flujos de Color (RGB), Infrarrojo 1 (Left), Infrarrojo 2 (Right) 
y Profundidad (Depth) usando el SDK oficial de Intel (pyrealsense2). Compone 
un lienzo maestro en forma de mosaico de 4 vistas (1920x1440), aplica un mapa 
de calor a la profundidad, añade HUDs de estado en tiempo real (OSD) y lo 
transmite vía RTSP H.264 usando FFmpeg y el servidor local MediaMTX.

Arquitectura del sistema:
─────────────────────────
  ┌────────────────┐  RGB, Depth, IR1, IR2  ┌────────────┐  raw frame   ┌─────────┐
  │  Intel D435    │ ─────────────────────► │ Composición│ ───────────► │ FFmpeg  │
  │ (pyrealsense2) │  via SDK               │ Mosaico 4ch│  vía stdin   │ (H.264) │
  └────────────────┘                        └────────────┘              └────┬────┘
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

Mosaico de Salida (1920x1440):
  ┌──────────────────────────────────────────────────────────┐
  │                                                          │
  │                   Color (RGB)                            │
  │                   1920 x 1080                            │
  │                                                          │
  ├──────────────────┬───────────────────┬───────────────────┤
  │    Infrared 1    │   Depth Heatmap   │    Infrared 2     │
  │    640 x 360     │     640 x 360     │     640 x 360     │
  └──────────────────┴───────────────────┴───────────────────┘

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

# ─── Intentar importar pyrealsense2 ───────────────────────────────────────
try:
    import pyrealsense2 as rs
except ImportError:
    print("Error: pyrealsense2 no está instalado.")
    print("  Instálalo con:  pip install -r requirements.txt")
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
    Retorna la ruta completa al ejecutable, o None si no se encuentra.
    """
    try:
        ruta = imageio_ffmpeg.get_ffmpeg_exe()
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
        return None


def descargar_mediamtx():
    """
    Descarga y extrae el servidor MediaMTX.
    Retorna la ruta al ejecutable de MediaMTX.
    """
    exe_path = os.path.join(DIR_MEDIAMTX, "mediamtx.exe")
    if os.path.isfile(exe_path):
        print(f"  ✓ MediaMTX ya existe en: {exe_path}")
        return exe_path

    print(f"  ↓ Descargando MediaMTX {MEDIAMTX_VERSION} ...")
    print(f"    URL: {MEDIAMTX_URL}")

    os.makedirs(DIR_MEDIAMTX, exist_ok=True)
    zip_path = os.path.join(DIR_MEDIAMTX, "mediamtx.zip")
    try:
        urllib.request.urlretrieve(MEDIAMTX_URL, zip_path)
    except Exception as e:
        print(f"  ✗ Error al descargar MediaMTX: {e}")
        sys.exit(1)

    print("  ↓ Extrayendo MediaMTX ...")
    try:
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(DIR_MEDIAMTX)
    except zipfile.BadZipFile:
        print("  ✗ El archivo descargado no es un ZIP válido.")
        os.remove(zip_path)
        sys.exit(1)

    os.remove(zip_path)

    if os.path.isfile(exe_path):
        print(f"  ✓ MediaMTX instalado en: {exe_path}")
        return exe_path
    else:
        print("  ✗ No se encontró mediamtx.exe después de la extracción.")
        sys.exit(1)


def iniciar_mediamtx(puerto):
    """
    Inicia el servidor MediaMTX en el puerto indicado.
    Retorna el objeto subprocess.Popen del proceso.
    """
    exe_path = descargar_mediamtx()
    entorno = os.environ.copy()
    entorno["MTX_RTSPADDRESS"] = f":{puerto}"

    print(f"  → Iniciando MediaMTX en el puerto {puerto} ...")
    try:
        proceso_mtx = subprocess.Popen(
            [exe_path],
            cwd=DIR_MEDIAMTX,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=entorno,
            creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
        )
    except PermissionError:
        print("  ✗ Sin permisos para ejecutar MediaMTX. Ejecuta como administrador.")
        sys.exit(1)
    except Exception as e:
        print(f"  ✗ Error al iniciar MediaMTX: {e}")
        sys.exit(1)

    time.sleep(2)
    if proceso_mtx.poll() is not None:
        salida = proceso_mtx.stdout.read().decode(errors='replace')
        print(f"  ✗ MediaMTX terminó inesperadamente. Salida:")
        print(f"    {salida[:500]}")
        sys.exit(1)

    print(f"  ✓ MediaMTX iniciado (PID: {proceso_mtx.pid})")
    return proceso_mtx


def listar_camaras_realsense():
    """
    Lista todos los dispositivos Intel RealSense conectados de forma legible.
    Retorna una lista de diccionarios con información de las cámaras.
    """
    contexto = rs.context()
    dispositivos = contexto.query_devices()

    if len(dispositivos) == 0:
        print("\n  ✗ No se detectaron cámaras Intel RealSense conectadas.")
        print("    Asegúrate de que la cámara esté conectada por USB 3.0.")
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


# Alias para compatibilidad
listar_camaras = listar_camaras_realsense


def obtener_serial_por_indice(indice):
    """
    Obtiene el número de serie de la cámara RealSense en el índice dado.
    """
    contexto = rs.context()
    dispositivos = contexto.query_devices()

    if len(dispositivos) == 0:
        print("  ✗ No se detectaron cámaras Intel RealSense.")
        sys.exit(1)

    if indice >= len(dispositivos):
        print(f"  ✗ Índice de cámara {indice} fuera de rango.")
        print(f"    Solo hay {len(dispositivos)} cámara(s) disponible(s).")
        sys.exit(1)

    # Si hay solo una e índice 0, podemos usar serial directo
    return dispositivos[indice].get_info(rs.camera_info.serial_number)


def obtener_ip_local():
    """
    Obtiene la dirección IP local de la máquina en la red LAN.
    """
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def dibujar_hud_realsense(canvas, x_offset, y_offset, titulo, fps, frames, segundos, resolucion):
    """
    Dibuja un HUD con fondo gris oscuro semitransparente y 3 líneas de texto.
    """
    box_x1 = x_offset + 10
    box_y1 = y_offset + 10
    box_x2 = x_offset + 330
    box_y2 = y_offset + 85
    
    overlay = canvas.copy()
    cv2.rectangle(overlay, (box_x1, box_y1), (box_x2, box_y2), (40, 40, 40), -1)
    cv2.addWeighted(overlay, 0.6, canvas, 0.4, 0, canvas)
    
    fuente = cv2.FONT_HERSHEY_SIMPLEX
    escala = 0.45
    grosor = 1
    salto = 20
    
    # Línea 1 (Rojo en BGR)
    cv2.putText(canvas, titulo, (box_x1 + 10, box_y1 + 20),
                fuente, escala, (0, 0, 255), grosor, cv2.LINE_AA)
                
    # Línea 2 (Verde en BGR)
    cv2.putText(canvas, f"FPS: {fps:.1f} | Frames: {frames}", (box_x1 + 10, box_y1 + 20 + salto),
                fuente, escala, (0, 255, 0), grosor, cv2.LINE_AA)
                
    # Línea 3 (Verde en BGR)
    cv2.putText(canvas, f"Tiempo: {int(segundos)}s | {resolucion}", (box_x1 + 10, box_y1 + 20 + 2 * salto),
                fuente, escala, (0, 255, 0), grosor, cv2.LINE_AA)


# ═══════════════════════════════════════════════════════════════════════════
# FUNCIÓN PRINCIPAL DEL EMISOR
# ═══════════════════════════════════════════════════════════════════════════

def iniciar_emisor(indice_camara=0, puerto=PUERTO_RTSP_DEFECTO, bitrate_kbps=2000):
    """
    Función principal del emisor RTSP para Intel RealSense D435.
    """
    proceso_mediamtx = None
    proceso_ffmpeg = None
    pipeline = None
    pipeline_started = False

    try:
        # ─── Paso 1: Verificar que FFmpeg esté disponible ──────────────
        print("\n" + "═" * 60)
        print("  EMISOR RTSP RealSense — Transmisión de Mosaico 4-Streams")
        print("═" * 60)

        print("\n[1/4] Verificando FFmpeg (vía imageio-ffmpeg) ...")
        ruta_ffmpeg = obtener_ruta_ffmpeg()
        if ruta_ffmpeg is None:
            print("  ✗ No se pudo obtener el binario de FFmpeg.")
            print("  Asegúrate de haber instalado las dependencias.")
            sys.exit(1)
        print(f"  ✓ FFmpeg encontrado: {ruta_ffmpeg}")

        # ─── Paso 2: Iniciar el servidor MediaMTX ─────────────────────
        print(f"\n[2/4] Preparando servidor RTSP (MediaMTX) ...")
        proceso_mediamtx = iniciar_mediamtx(puerto)

        # ─── Paso 3: Abrir la cámara RealSense D435 y configurar Streams ───────────
        print(f"\n[3/4] Abriendo dispositivo Intel RealSense (índice {indice_camara}) ...")
        
        serial_cam = obtener_serial_por_indice(indice_camara)

        # Configurar streams con la máxima calidad y resolución nativa sin pérdida
        pipeline = rs.pipeline()
        config = rs.config()
        if serial_cam is not None:
            config.enable_device(serial_cam)

        # RGB: 1920x1080 a 30fps en formato BGR8 (nativa OpenCV)
        config.enable_stream(rs.stream.color, 1920, 1080, rs.format.bgr8, 30)
        # Infrarrojo 1 (Izquierdo) e Infrarrojo 2 (Derecho): 1280x720 a 30fps (Y8 grayscale)
        config.enable_stream(rs.stream.infrared, 1, 1280, 720, rs.format.y8, 30)
        config.enable_stream(rs.stream.infrared, 2, 1280, 720, rs.format.y8, 30)
        # Profundidad: 1280x720 a 30fps (Z16)
        config.enable_stream(rs.stream.depth, 1280, 720, rs.format.z16, 30)

        # Iniciar el pipeline
        print("  → Iniciando pipeline de RealSense ...")
        try:
            profile = pipeline.start(config)
            pipeline_started = True
            print("  ✓ Pipeline RealSense iniciado correctamente.")
        except RuntimeError as e:
            print(f"  ✗ No se pudo iniciar el pipeline de RealSense: {e}")
            print("    Asegúrate de que la cámara esté conectada por USB 3.0.")
            sys.exit(1)

        # Dimensiones del lienzo final (Mosaico)
        ancho_canvas = 1920
        alto_canvas = 1440
        fps_stream = 30 # Forzado por hardware

        # ─── Paso 4: Lanzar FFmpeg para codificar y publicar RTSP ─────
        print(f"\n[4/4] Iniciando transmisión RTSP ...")
        url_rtsp = f"rtsp://127.0.0.1:{puerto}/{RUTA_FLUJO}"

        # Comando FFmpeg configurado para el tamaño del mosaico (1920x1440)
        comando_ffmpeg = [
            ruta_ffmpeg,
            "-y",
            "-f", "rawvideo",
            "-vcodec", "rawvideo",
            "-pix_fmt", "bgr24",
            "-s", f"{ancho_canvas}x{alto_canvas}",
            "-r", str(fps_stream),
            "-i", "-",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-preset", "ultrafast",
            "-tune", "zerolatency",
            "-b:v", f"{bitrate_kbps}k",
            "-g", str(fps_stream * 2),
            "-f", "rtsp",
            "-rtsp_transport", "tcp",
            url_rtsp
        ]

        proceso_ffmpeg = subprocess.Popen(
            comando_ffmpeg,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
        )

        time.sleep(1)

        if proceso_ffmpeg.poll() is not None:
            stderr_out = proceso_ffmpeg.stderr.read().decode(errors='replace')
            print(f"  ✗ FFmpeg terminó inesperadamente:")
            print(f"    {stderr_out[:500]}")
            sys.exit(1)

        ip_local = obtener_ip_local()
        print("  ✓ Transmisión RTSP activa")
        print("\n" + "═" * 60)
        print(f"  URL RTSP del flujo:")
        print(f"  → rtsp://{ip_local}:{puerto}/{RUTA_FLUJO}")
        print(f"")
        print(f"  Resolución del Mosaico: {ancho_canvas}x{alto_canvas}")
        print(f"  Los receptores deben conectarse a esta URL.")
        print(f"  También puedes probar con VLC: Medio → Abrir ubicación de red")
        print("═" * 60)
        print("\n  Presiona Ctrl+C para detener la transmisión.\n")

        # ─── Bucle principal: capturar, componer y enviar fotogramas ────
        fotogramas_enviados = 0
        tiempo_inicio = time.time()

        while True:
            # Esperar un conjunto sincronizado de fotogramas (RGB + Depth + IR) con timeout
            try:
                frames = pipeline.wait_for_frames(timeout_ms=5000)
            except RuntimeError:
                print("  ⚠ Timeout esperando fotogramas de la cámara...")
                continue

            color_frame = frames.get_color_frame()
            ir_left_frame = frames.get_infrared_frame(1)
            ir_right_frame = frames.get_infrared_frame(2)
            depth_frame = frames.get_depth_frame()

            if not color_frame or not ir_left_frame or not ir_right_frame or not depth_frame:
                continue

            # Convertir frames a numpy arrays
            color_image = np.asanyarray(color_frame.get_data())      # 1920x1080 BGR
            ir_left_image = np.asanyarray(ir_left_frame.get_data())    # 1280x720 Grayscale
            ir_right_image = np.asanyarray(ir_right_frame.get_data())  # 1280x720 Grayscale
            depth_image = np.asanyarray(depth_frame.get_data())        # 1280x720 Z16

            # ─── 1. Procesamiento de Infrarrojos (Grayscale -> BGR) ───
            ir_left_bgr = cv2.cvtColor(ir_left_image, cv2.COLOR_GRAY2BGR)
            ir_right_bgr = cv2.cvtColor(ir_right_image, cv2.COLOR_GRAY2BGR)

            # ─── 2. Procesamiento de Profundidad (Normalización y Heatmap JET) ───
            max_depth_mm = 4000
            depth_clipped = np.clip(depth_image, 0, max_depth_mm)
            
            # Normalizar a 8 bits (0-255)
            depth_8bit = (depth_clipped * (255.0 / max_depth_mm)).astype(np.uint8)
            
            # Aplicar mapa de color JET (esquema térmico)
            depth_heatmap = cv2.applyColorMap(depth_8bit, cv2.COLORMAP_JET)
            
            # Forzar píxeles sin profundidad válida (0 mm) a color negro
            depth_heatmap[depth_image == 0] = [0, 0, 0]

            # ─── 3. Redimensionado proporcional para el Mosaico ───
            # Redimensionar fila inferior a 1/3 del ancho total (1920 / 3 = 640px)
            # Para mantener la proporción 16:9, el alto es 360px
            ir_left_resized = cv2.resize(ir_left_bgr, (640, 360), interpolation=cv2.INTER_LINEAR)
            depth_heatmap_resized = cv2.resize(depth_heatmap, (640, 360), interpolation=cv2.INTER_LINEAR)
            ir_right_resized = cv2.resize(ir_right_bgr, (640, 360), interpolation=cv2.INTER_LINEAR)

            # ─── 4. Composición de la Fila Inferior ───
            bottom_row = np.hstack([ir_left_resized, depth_heatmap_resized, ir_right_resized])

            # ─── 5. Composición del Lienzo Maestro ───
            canvas = np.vstack([color_image, bottom_row])

            # Calcular tiempos e información para etiquetas
            tiempo_actual = time.time()
            segundos_transcurridos = tiempo_actual - tiempo_inicio
            
            if segundos_transcurridos > 0:
                fps_calculados = fotogramas_enviados / segundos_transcurridos
            else:
                fps_calculados = 0.0

            # ─── 6. Dibujar las etiquetas OSD con rectángulos semitransparentes ───
            dibujar_hud_realsense(canvas, 0, 0, "Live | RGB", fps_calculados, fotogramas_enviados, segundos_transcurridos, "1920x1080")
            dibujar_hud_realsense(canvas, 0, 1080, "Live | Infrarojo 1", fps_calculados, fotogramas_enviados, segundos_transcurridos, "1280x720")
            dibujar_hud_realsense(canvas, 640, 1080, "Live | Profundidad", fps_calculados, fotogramas_enviados, segundos_transcurridos, "1280x720")
            dibujar_hud_realsense(canvas, 1280, 1080, "Live | Infrarojo 2", fps_calculados, fotogramas_enviados, segundos_transcurridos, "1280x720")

            # Escribir frame crudo (descomprimido) en FFmpeg stdin
            try:
                proceso_ffmpeg.stdin.write(canvas.tobytes())
                fotogramas_enviados += 1

                # Mostrar estadísticas cada 100 fotogramas
                if fotogramas_enviados % 100 == 0:
                    print(f"  📹 Mosaicos enviados: {fotogramas_enviados} "
                          f"| FPS promedio: {fps_calculados:.1f} "
                          f"| Tiempo: {segundos_transcurridos:.0f}s")

            except BrokenPipeError:
                print("  ✗ FFmpeg cerró la conexión (BrokenPipe).")
                break
            except OSError as e:
                print(f"  ✗ Error de E/S con FFmpeg: {e}")
                break

    except KeyboardInterrupt:
        print("\n\n  ⏹ Emisor RealSense detenido por el usuario (Ctrl+C).")

    except Exception as e:
        print(f"\n  ✗ Error inesperado en el emisor RealSense: {e}")

    finally:
        print("\n  Liberando recursos ...")

        # Detener el pipeline de RealSense
        if pipeline_started and pipeline is not None:
            try:
                pipeline.stop()
                print("  ✓ Pipeline RealSense detenido y cámara liberada")
            except Exception as e:
                print(f"  ✗ Error al detener el pipeline RealSense: {e}")

        # Cerrar el pipe de stdin de FFmpeg
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
    parser = argparse.ArgumentParser(
        description="Emisor RTSP: transmite vídeo RGB + Profundidad + Infrarrojos de una cámara Intel RealSense D435.",
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
