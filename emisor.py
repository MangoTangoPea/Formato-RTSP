#!/usr/bin/env python3
"""
Emisor RTSP para Intel RealSense D435 — Windows (v3).

Rediseñado con esteganografía LSB y grabación de mosaico unificado:
  - Inyecta Frame ID (64 bits) + Timestamp (64 bits) en la fila 0 vía LSB.
  - Soporta grabación local en Matroska (.mkv) como mosaico unificado (--grabar).
  - Usa un único stdin pipe para FFmpeg (sin FIFOs o archivos temporales complejos).
  - Oculta ventanas de consola de FFmpeg y MediaMTX en Windows.

Publica 4 streams RTSP independientes:
  rtsp://<IP>:8554/color    — RGB 1920x1080
  rtsp://<IP>:8554/depth    — Profundidad con heatmap JET 1280x720
  rtsp://<IP>:8554/ir1      — Infrarrojo izquierdo 1280x720
  rtsp://<IP>:8554/ir2      — Infrarrojo derecho 1280x720

Grabación MKV Mosaico:
  Combina los 4 canales (ya con LSB inyectado) en un mosaico unificado
  de 1920x1440 y lo codifica como un solo track H.264.
  Layout del mosaico idéntico al receptor:
    ┌──────────────────────────────┐
    │           Color              │
    │        1920 × 1080           │
    ├──────────┬──────────┬────────┤
    │   IR1    │  Depth   │  IR2   │
    │ 640×360  │ 640×360  │ 640×360│
    └──────────┴──────────┴────────┘
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
import struct
import queue
import threading
import datetime

# ─── Configurar la codificación de la consola para Unicode en Windows ─────
if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

# ─── Verificar dependencias Python ────────────────────────────────────────
try:
    import cv2
except ImportError:
    print("Error: opencv-python no está instalado.")
    print("  Instálalo con:  pip install -r requirements.txt")
    sys.exit(1)

try:
    import numpy as np
except ImportError:
    print("Error: numpy no está instalado.")
    print("  Instálalo con:  pip install -r requirements.txt")
    sys.exit(1)

try:
    import pyrealsense2 as rs
except ImportError:
    print("Error: pyrealsense2 no está instalado.")
    print("  Instálalo con:  pip install -r requirements.txt")
    sys.exit(1)

try:
    import imageio_ffmpeg
except ImportError:
    print("Error: imageio-ffmpeg no está instalado.")
    print("  Instálalo con:  pip install -r requirements.txt")
    sys.exit(1)


# ═══════════════════════════════════════════════════════════════════════════
# CONSTANTES Y CONFIGURACIÓN POR DEFECTO
# ═══════════════════════════════════════════════════════════════════════════

PUERTO_RTSP_DEFECTO = 8554
DIR_BASE = os.path.dirname(os.path.abspath(__file__))
DIR_MEDIAMTX = os.path.join(DIR_BASE, "mediamtx")

MEDIAMTX_VERSION = "v1.12.2"
MEDIAMTX_URL = (
    f"https://github.com/bluenviron/mediamtx/releases/download/"
    f"{MEDIAMTX_VERSION}/mediamtx_{MEDIAMTX_VERSION}_windows_amd64.zip"
)

# Flags de creación de proceso para Windows (oculta ventanas emergentes de consola)
_CREATION_FLAGS = subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0


# ═══════════════════════════════════════════════════════════════════════════
# ESTEGANOGRAFÍA LSB (Least Significant Bit)
# ═══════════════════════════════════════════════════════════════════════════

BITS_POR_BLOQUE = 1
TOTAL_BITS = 128
PIXELES_LSB = TOTAL_BITS * BITS_POR_BLOQUE  # 128 (píxeles del 0 al 127)


def inyectar_lsb(frame, frame_id, timestamp_ns):
    """
    Inyecta 128 bits de metadatos en la primera fila (fila 0) del frame.
    
    Payload:
      - Bits [0..63]:   Frame ID (entero secuencial de 64 bits, inicia en 1)
      - Bits [64..127]: Timestamp del emisor (nanosegundos, time.time_ns())

    Cada bit lógico se replica en BITS_POR_BLOQUE (8) píxeles contiguos.
    Modifica el frame in-place y lo retorna.
    """
    ancho = frame.shape[1]
    if ancho < PIXELES_LSB:
        return frame

    # Empaquetar a 16 bytes big-endian
    datos = struct.pack('>QQ',
                        frame_id & 0xFFFFFFFFFFFFFFFF,
                        timestamp_ns & 0xFFFFFFFFFFFFFFFF)

    # Desempaquetar a 128 bits y duplicar cada bit por el factor de redundancia
    bits_arr = np.unpackbits(np.frombuffer(datos, dtype=np.uint8))
    mascara = np.repeat(bits_arr, BITS_POR_BLOQUE)

    # Modificar in-place el LSB del frame
    if frame.ndim == 3:
        fila = frame[0, :PIXELES_LSB, 0]   # Canal Azul en BGR
    else:
        fila = frame[0, :PIXELES_LSB]       # Grayscale directo

    fila[:] = (fila & np.uint8(0xFE)) | mascara.astype(fila.dtype)
    return frame


def extraer_lsb(frame):
    """
    Extrae 128 bits de metadatos LSB de la primera fila del frame.
    Aplica majority voting en bloques de 8 píxeles.
    """
    ancho = frame.shape[1] if frame.ndim >= 2 else 0
    if ancho < PIXELES_LSB:
        return None, None

    if frame.ndim == 3:
        fila = frame[0, :PIXELES_LSB, 0]
    else:
        fila = frame[0, :PIXELES_LSB]

    lsbs = (fila & np.uint8(1)).reshape(TOTAL_BITS, BITS_POR_BLOQUE)
    bits = (lsbs.sum(axis=1) > BITS_POR_BLOQUE // 2).astype(np.uint8)

    datos = np.packbits(bits)
    frame_id, timestamp_ns = struct.unpack('>QQ', datos.tobytes())
    return frame_id, timestamp_ns


# ═══════════════════════════════════════════════════════════════════════════
# GRABACIÓN DE RANGO SIN PÉRDIDAS (Asíncrona)
# ═══════════════════════════════════════════════════════════════════════════

class GrabadorRango:
    """
    Grabador en segundo plano para registrar un rango específico de frames sin pérdidas.
    Guarda las imágenes en carpetas (PNG) y la profundidad en 16 bits nativos (Z16).
    """
    def __init__(self, dir_salida, frame_inicio, frame_fin):
        self.dir_salida = dir_salida
        self.inicio = frame_inicio
        self.fin = frame_fin
        self.cola = queue.Queue()
        self.corriendo = True
        self.hilo = threading.Thread(target=self._bucle_guardado, name="GrabadorRango", daemon=True)
        
        for subdir in ["color", "depth", "ir1", "ir2"]:
            os.makedirs(os.path.join(self.dir_salida, subdir), exist_ok=True)
            
        self.csv_path = os.path.join(self.dir_salida, "metadata.csv")
        with open(self.csv_path, "w", encoding="utf-8") as f:
            f.write("frame_id,timestamp_ns,timestamp_utc\n")
            
        self.hilo.start()

    def agregar_frame(self, frame_id, color, depth_raw, ir1, ir2, timestamp_ns):
        if self.inicio <= frame_id <= self.fin:
            self.cola.put((frame_id, color.copy(), depth_raw.copy(), ir1.copy(), ir2.copy(), timestamp_ns))

    def _bucle_guardado(self):
        while self.corriendo or not self.cola.empty():
            try:
                item = self.cola.get(timeout=0.2)
            except queue.Empty:
                continue
                
            frame_id, color, depth_raw, ir1, ir2, timestamp_ns = item
            filename = f"{frame_id:08d}.png"
            
            cv2.imwrite(os.path.join(self.dir_salida, "color", filename), color)
            cv2.imwrite(os.path.join(self.dir_salida, "depth", filename), depth_raw)
            cv2.imwrite(os.path.join(self.dir_salida, "ir1", filename), ir1)
            cv2.imwrite(os.path.join(self.dir_salida, "ir2", filename), ir2)
            
            try:
                dt_utc = datetime.datetime.fromtimestamp(timestamp_ns / 1e9, datetime.timezone.utc)
                fecha_utc = dt_utc.isoformat()
            except Exception:
                fecha_utc = "unknown"
                
            with open(self.csv_path, "a", encoding="utf-8") as f:
                f.write(f"{frame_id},{timestamp_ns},{fecha_utc}\n")
                
            self.cola.task_done()

    def detener(self):
        self.corriendo = False
        if self.hilo.is_alive():
            self.hilo.join(timeout=15.0)


# ═══════════════════════════════════════════════════════════════════════════
# FUNCIONES AUXILIARES
# ═══════════════════════════════════════════════════════════════════════════

def obtener_ruta_ffmpeg():
    """Obtiene la ruta al ejecutable de FFmpeg usando imageio-ffmpeg."""
    try:
        ruta = imageio_ffmpeg.get_ffmpeg_exe()
        resultado = subprocess.run(
            [ruta, "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=_CREATION_FLAGS
        )
        if resultado.returncode == 0:
            return ruta
        return None
    except Exception:
        return None


def descargar_mediamtx():
    """Descarga y extrae el servidor MediaMTX. Retorna la ruta al ejecutable."""
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
    """Inicia el servidor MediaMTX en el puerto indicado."""
    exe_path = descargar_mediamtx()
    entorno = os.environ.copy()
    entorno["MTX_RTSPADDRESS"] = f":{puerto}"

    print(f"  → Iniciar MediaMTX en el puerto {puerto} ...")
    try:
        proceso_mtx = subprocess.Popen(
            [exe_path],
            cwd=DIR_MEDIAMTX,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=entorno,
            creationflags=_CREATION_FLAGS
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
    """Lista todos los dispositivos Intel RealSense conectados."""
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
    return lista


def obtener_serial_por_indice(indice):
    """Obtiene el número de serie de la cámara RealSense en el índice dado."""
    contexto = rs.context()
    dispositivos = contexto.query_devices()

    if len(dispositivos) == 0:
        print("  ✗ No se detectaron cámaras Intel RealSense.")
        sys.exit(1)

    if indice >= len(dispositivos):
        print(f"  ✗ Índice de cámara {indice} fuera de rango.")
        print(f"    Solo hay {len(dispositivos)} cámara(s) disponible(s).")
        sys.exit(1)

    return dispositivos[indice].get_info(rs.camera_info.serial_number)


def obtener_ip_local():
    """Obtiene la dirección IP local de la máquina en la red LAN."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ═══════════════════════════════════════════════════════════════════════════
# PROCESOS FFMPEG
# ═══════════════════════════════════════════════════════════════════════════

def crear_proceso_ffmpeg(ruta_ffmpeg, url_stream, ancho, alto, pix_fmt, fps, bitrate_kbps):
    """Lanza FFmpeg para codificar rawvideo → H.264 → RTSP."""
    comando = [
        ruta_ffmpeg,
        "-y",
        "-f", "rawvideo",
        "-vcodec", "rawvideo",
        "-pix_fmt", pix_fmt,
        "-s", f"{ancho}x{alto}",
        "-r", str(fps),
        "-i", "-",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-preset", "ultrafast",
        "-tune", "zerolatency",
        "-x264-params", "sliced-threads=1:rc-lookahead=0",
        "-b:v", f"{bitrate_kbps}k",
        "-g", str(fps * 2),
        "-f", "rtsp",
        "-rtsp_transport", "tcp",
        url_stream
    ]
    return subprocess.Popen(
        comando,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=_CREATION_FLAGS
    )


# ═══════════════════════════════════════════════════════════════════════════
# FUNCIÓN PRINCIPAL DEL EMISOR
# ═══════════════════════════════════════════════════════════════════════════

def iniciar_emisor(indice_camara=0, puerto=PUERTO_RTSP_DEFECTO,
                   bitrate_kbps=2000,
                   rango_grabacion=None, dir_salida=None):
    """
    Emisor RTSP v3 con esteganografía LSB para Windows.
    """
    proceso_mediamtx = None
    proceso_color = None
    proceso_depth = None
    proceso_ir1 = None
    proceso_ir2 = None
    pipeline = None
    pipeline_started = False

    try:
        print("\n" + "═" * 62)
        print("  EMISOR RTSP RealSense — Windows (v3)")
        print("  Esteganografía LSB activa · Bloques de 8px por bit")
        print("═" * 62)

        print("\n[1/5] Verificando FFmpeg (vía imageio-ffmpeg) ...")
        ruta_ffmpeg = obtener_ruta_ffmpeg()
        if ruta_ffmpeg is None:
            print("  ✗ No se pudo obtener el binario de FFmpeg.")
            sys.exit(1)
        print(f"  ✓ FFmpeg encontrado: {ruta_ffmpeg}")

        print(f"\n[2/5] Preparando servidor RTSP (MediaMTX) ...")
        proceso_mediamtx = iniciar_mediamtx(puerto)

        print(f"\n[3/5] Abriendo dispositivo Intel RealSense (índice {indice_camara}) ...")
        serial_cam = obtener_serial_por_indice(indice_camara)

        pipeline = rs.pipeline()
        config = rs.config()
        if serial_cam is not None:
            config.enable_device(serial_cam)

        config.enable_stream(rs.stream.color, 1920, 1080, rs.format.bgr8, 30)
        config.enable_stream(rs.stream.infrared, 1, 1280, 720, rs.format.y8, 30)
        config.enable_stream(rs.stream.infrared, 2, 1280, 720, rs.format.y8, 30)
        config.enable_stream(rs.stream.depth, 1280, 720, rs.format.z16, 30)

        print("  → Iniciando pipeline de RealSense ...")
        try:
            pipeline.start(config)
            pipeline_started = True
            print("  ✓ Pipeline RealSense iniciado correctamente.")
        except RuntimeError as e:
            print(f"  ✗ No se pudo iniciar el pipeline de RealSense: {e}")
            sys.exit(1)

        fps_stream = 30

        print(f"\n[4/5] Iniciando transmisión RTSP de 4 canales ...")
        bitrate_color = max(100, int(bitrate_kbps * 0.6))
        bitrate_depth = max(100, int(bitrate_kbps * 0.2))
        bitrate_ir1 = max(100, int(bitrate_kbps * 0.1))
        bitrate_ir2 = max(100, int(bitrate_kbps * 0.1))

        url_color = f"rtsp://127.0.0.1:{puerto}/color"
        url_depth = f"rtsp://127.0.0.1:{puerto}/depth"
        url_ir1 = f"rtsp://127.0.0.1:{puerto}/ir1"
        url_ir2 = f"rtsp://127.0.0.1:{puerto}/ir2"

        proceso_color = crear_proceso_ffmpeg(ruta_ffmpeg, url_color, 1920, 1080, "bgr24", fps_stream, bitrate_color)
        proceso_depth = crear_proceso_ffmpeg(ruta_ffmpeg, url_depth, 1280, 720, "bgr24", fps_stream, bitrate_depth)
        proceso_ir1 = crear_proceso_ffmpeg(ruta_ffmpeg, url_ir1, 1280, 720, "gray", fps_stream, bitrate_ir1)
        proceso_ir2 = crear_proceso_ffmpeg(ruta_ffmpeg, url_ir2, 1280, 720, "gray", fps_stream, bitrate_ir2)

        time.sleep(1)

        for proc, name in [(proceso_color, "color"), (proceso_depth, "depth"),
                           (proceso_ir1, "ir1"), (proceso_ir2, "ir2")]:
            if proc.poll() is not None:
                stderr_out = proc.stderr.read().decode(errors='replace')
                print(f"  ✗ FFmpeg ({name}) terminó inesperadamente:")
                print(f"    {stderr_out[:300]}")
                sys.exit(1)

        # Grabación local MKV en emisor desactivada
        print(f"\n[5/5] Grabación local MKV en emisor: desactivada (se realiza en el receptor)")

        grabador_rango = None
        if rango_grabacion is not None:
            try:
                frame_inicio, frame_fin = rango_grabacion
                if dir_salida is None:
                    dir_salida = f"rango_grabacion_{frame_inicio}_{frame_fin}"
                dir_salida_abs = os.path.abspath(dir_salida)
                print(f"\n[5.5] Preparando grabación de rango sin pérdidas ...")
                grabador_rango = GrabadorRango(dir_salida_abs, frame_inicio, frame_fin)
            except Exception as e:
                print(f"  ⚠ No se pudo iniciar el grabador de rango: {e}")

        ip_local = obtener_ip_local()
        print("\n" + "═" * 62)
        print("  ✓ TRANSMISIÓN ACTIVA — 4 Canales RTSP + LSB")
        print("─" * 62)
        print(f"  Color (RGB):   rtsp://{ip_local}:{puerto}/color")
        print(f"  Depth Map:     rtsp://{ip_local}:{puerto}/depth")
        print(f"  Infrared 1:    rtsp://{ip_local}:{puerto}/ir1")
        print(f"  Infrared 2:    rtsp://{ip_local}:{puerto}/ir2")
        print("─" * 62)
        print(f"  LSB:  128 bits × {BITS_POR_BLOQUE}px/bit = {PIXELES_LSB}px en fila 0")
        if grabador_rango is not None:
            print(f"  🔴 GRABANDO RANGO [{frame_inicio} - {frame_fin}] → {os.path.abspath(dir_salida)}")
        print("═" * 62)
        print("\n  Presiona Ctrl+C para detener la transmisión.\n")

        # ─── Bucle principal ────────────────────────────────────────────
        # El Frame ID secuencial inicia en 1
        frame_id = 1
        tiempo_inicio = time.time()

        while True:
            try:
                frames = pipeline.wait_for_frames(timeout_ms=5000)
            except RuntimeError:
                print("  ⚠ Timeout esperando fotogramas de la cámara...")
                continue

            timestamp_ns = time.time_ns()

            color_frame = frames.get_color_frame()
            ir_left_frame = frames.get_infrared_frame(1)
            ir_right_frame = frames.get_infrared_frame(2)
            depth_frame = frames.get_depth_frame()

            if not color_frame or not ir_left_frame or not ir_right_frame or not depth_frame:
                continue

            color_image = np.array(color_frame.get_data())
            ir_left_image = np.array(ir_left_frame.get_data())
            ir_right_image = np.array(ir_right_frame.get_data())
            depth_raw = np.asanyarray(depth_frame.get_data())

            # Procesamiento de Profundidad
            max_depth_mm = 4000
            depth_clipped = np.clip(depth_raw, 0, max_depth_mm)
            depth_8bit = (depth_clipped * (255.0 / max_depth_mm)).astype(np.uint8)
            depth_heatmap = cv2.applyColorMap(depth_8bit, cv2.COLORMAP_JET)
            depth_heatmap[depth_raw == 0] = [0, 0, 0]

            # Inyectar esteganografía LSB
            inyectar_lsb(color_image, frame_id, timestamp_ns)
            inyectar_lsb(depth_heatmap, frame_id, timestamp_ns)
            inyectar_lsb(ir_left_image, frame_id, timestamp_ns)
            inyectar_lsb(ir_right_image, frame_id, timestamp_ns)

            # Escribir a RTSP
            try:
                proceso_color.stdin.write(color_image.tobytes())
                proceso_depth.stdin.write(depth_heatmap.tobytes())
                proceso_ir1.stdin.write(ir_left_image.tobytes())
                proceso_ir2.stdin.write(ir_right_image.tobytes())
            except (BrokenPipeError, OSError) as e:
                print(f"  ✗ Error escribiendo a FFmpeg RTSP: {e}")
                break

            # Enviar a grabación de rango
            if grabador_rango is not None:
                grabador_rango.agregar_frame(
                    frame_id, color_image, depth_raw,
                    ir_left_image, ir_right_image, timestamp_ns
                )
                if frame_id >= grabador_rango.fin:
                    print(f"\n  ✓ Rango de fotogramas finalizado ({grabador_rango.inicio} a {grabador_rango.fin}).")
                    grabador_rango.detener()
                    grabador_rango = None

            frame_id += 1

            # Log cada 150 frames
            if (frame_id - 1) % 150 == 0:
                segundos = time.time() - tiempo_inicio
                fps_actual = (frame_id - 1) / segundos if segundos > 0 else 0.0
                ts_str = time.strftime("%H:%M:%S", time.localtime(timestamp_ns / 1e9))
                ts_ms = int((timestamp_ns % 1_000_000_000) / 1_000_000)
                print(f"  📹 FID: {frame_id - 1} | TS: {ts_str}.{ts_ms:03d} | "
                      f"FPS: {fps_actual:.1f} | Tiempo: {segundos:.0f}s")

            # Verificar que los FFmpeg RTSP sigan vivos
            if (proceso_color.poll() is not None or
                proceso_depth.poll() is not None or
                proceso_ir1.poll() is not None or
                proceso_ir2.poll() is not None):
                print("  ✗ Uno de los procesos FFmpeg RTSP se detuvo.")
                break

    except KeyboardInterrupt:
        print("\n\n  ⏹ Detenido por el usuario (Ctrl+C).")

    except Exception as e:
        print(f"\n  ✗ Error inesperado: {e}")
        import traceback
        traceback.print_exc()

    finally:
        print("\n  Liberando recursos ...")

        if 'grabador_rango' in locals() and grabador_rango is not None:
            grabador_rango.detener()

        if pipeline_started and pipeline is not None:
            try:
                pipeline.stop()
                print("  ✓ Pipeline RealSense detenido")
            except Exception:
                pass

        # Cerrar procesos FFmpeg RTSP
        for proc, name in [(proceso_color, "color"), (proceso_depth, "depth"),
                           (proceso_ir1, "ir1"), (proceso_ir2, "ir2")]:
            if proc and proc.stdin:
                try:
                    proc.stdin.close()
                except Exception:
                    pass

        for proc, name in [(proceso_color, "color"), (proceso_depth, "depth"),
                           (proceso_ir1, "ir1"), (proceso_ir2, "ir2")]:
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                    print(f"  ✓ FFmpeg ({name}) detenido")
                except Exception:
                    proc.kill()
                    print(f"  ✓ FFmpeg ({name}) forzado")

        # Detener MediaMTX
        if proceso_mediamtx and proceso_mediamtx.poll() is None:
            try:
                proceso_mediamtx.terminate()
                proceso_mediamtx.wait(timeout=5)
                print("  ✓ MediaMTX detenido")
            except Exception:
                proceso_mediamtx.kill()

        total_frames = frame_id - 1
        if total_frames > 0:
            dt_total = time.time() - tiempo_inicio
            print(f"\n  Resumen: {total_frames} frames en {dt_total:.1f}s "
                  f"({total_frames / dt_total:.1f} FPS promedio)")

        print("\n  Emisor finalizado.\n")


# ═══════════════════════════════════════════════════════════════════════════
# PUNTO DE ENTRADA
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Emisor RTSP v3 con LSB para Windows.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos de uso:
  python emisor.py                          # Cámara 0, puerto 8554
  python emisor.py --cam 1                  # Usar segunda cámara RealSense
  python emisor.py --puerto 9554            # Usar puerto alternativo
  python emisor.py --calidad 4000           # Mayor calidad de vídeo
  python emisor.py --listar-camaras         # Ver cámaras RealSense detectadas
  python emisor.py --grabar-rango 150 450   # Grabar frames 150 a 450 sin pérdidas
        """
    )

    parser.add_argument("--puerto", type=int, default=PUERTO_RTSP_DEFECTO,
                        help=f"Puerto TCP del servidor RTSP (por defecto: {PUERTO_RTSP_DEFECTO})")
    parser.add_argument("--cam", type=int, default=0,
                        help="Índice de la cámara RealSense a usar (por defecto: 0)")
    parser.add_argument("--calidad", type=int, default=2000,
                        help="Bitrate de vídeo en kbps (por defecto: 2000)")
    parser.add_argument("--grabar-rango", type=int, nargs=2, metavar=("INICIO", "FIN"), default=None,
                        help="Grabar un rango de fotogramas sin pérdidas (ej. --grabar-rango 100 500)")
    parser.add_argument("--dir-salida", type=str, default=None,
                        help="Directorio de salida para la grabación de rango sin pérdidas")
    parser.add_argument("--listar-camaras", action="store_true",
                        help="Listar cámaras Intel RealSense conectadas y salir")

    args = parser.parse_args()

    if args.listar_camaras:
        print("Buscando cámaras Intel RealSense ...")
        listar_camaras_realsense()
        sys.exit(0)

    iniciar_emisor(
        indice_camara=args.cam,
        puerto=args.puerto,
        bitrate_kbps=args.calidad,
        rango_grabacion=args.grabar_rango,
        dir_salida=args.dir_salida
    )
