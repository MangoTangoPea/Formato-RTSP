#!/usr/bin/env python3
"""
Emisor RTSP para Intel RealSense D435 — Ubuntu/Linux Nativo (v5).

Arquitectura de grabación y streaming con esteganografía LSB:
  - Inyecta Frame ID (64 bits) + Timestamp (64 bits) en cada frame vía LSB
  - 8 píxeles por bit (1024 px totales) con majority voting para sobrevivir H.264
  - Hardware reset automático al iniciar para limpiar sesiones colgadas
  - Reintentos automáticos en caso de fallo de pipeline
  - Usa FFmpeg como servidor RTSP directo (sin MediaMTX)
  - Soporta grabación local de rango sin pérdidas (--grabar-rango)
  - Mantiene compatibilidad total con señales POSIX y cierre limpio

Publica 4 streams RTSP independientes (un puerto por canal):
  rtsp://<IP>:8554/stream   — RGB 1920x1080
  rtsp://<IP>:8555/stream   — Profundidad con heatmap JET 1280x720
  rtsp://<IP>:8556/stream   — Infrarrojo izquierdo 1280x720
  rtsp://<IP>:8557/stream   — Infrarrojo derecho 1280x720

Uso:
    python3 emisor_ubuntu.py [--puerto PUERTO] [--cam INDICE] [--calidad KBPS]
    python3 emisor_ubuntu.py --grabar-rango INICIO FIN
    python3 emisor_ubuntu.py --listar-camaras
    python3 emisor_ubuntu.py --diagnostico
"""

import subprocess
import sys
import os
import signal
import time
import argparse
import socket
import shutil
import struct
import queue
import threading
import datetime

# ─── Importación soft de numpy ──────────────────────────────────────────────
try:
    import numpy as np
except ImportError:
    np = None


# ═══════════════════════════════════════════════════════════════════════════
# CONSTANTES GLOBALES
# ═══════════════════════════════════════════════════════════════════════════

PUERTO_RTSP_DEFECTO = 8554

# ─── Flag global para cierre limpio con señales POSIX ───────────────────────
_cerrando = False


# ═══════════════════════════════════════════════════════════════════════════
# ESTEGANOGRAFÍA LSB (Least Significant Bit)
# ═══════════════════════════════════════════════════════════════════════════
#
# Inyecta 128 bits de metadatos (64-bit Frame ID + 64-bit Timestamp)
# en la primera fila de cada frame usando el bit menos significativo.
#
# ── Esquema de codificación ──
# Cada bit lógico se replica en BITS_POR_BLOQUE (8) píxeles consecutivos
# para crear redundancia que permite sobrevivir a la compresión H.264.
# En la extracción se aplica "majority voting": si ≥5 de 8 LSBs son 1,
# el bit reconstruido es 1. Tolera hasta 3 bits corrompidos por bloque.
#
# Total de píxeles: 128 bits × 8 px/bit = 1024 px (caben en 1280px y 1920px)
#
# ── Canal de inyección ──
# - BGR: solo el canal Azul [0] (el menos sensible al ojo humano)
# - Grayscale: el píxel directamente

BITS_POR_BLOQUE = 8       # ← CRÍTICO: debe coincidir con receptor_ubuntu.py
TOTAL_BITS      = 128
PIXELES_LSB     = TOTAL_BITS * BITS_POR_BLOQUE  # = 1024 píxeles


def inyectar_lsb(frame, frame_id, timestamp_ns):
    """
    Inyecta 128 bits de metadatos en la primera fila (fila 0) del frame.

    El payload consta de:
      - Bits [0..63]:   Frame ID (entero secuencial de 64 bits, inicia en 1)
      - Bits [64..127]: Timestamp del computador emisor (nanosegundos)

    Cada bit se replica 8 veces para redundancia ante H.264.

    Modifica el frame in-place y lo retorna.
    """
    ancho = frame.shape[1]
    if ancho < PIXELES_LSB:
        return frame

    # Empaquetar payload: 16 bytes big-endian (Frame ID + Timestamp)
    datos = struct.pack('>QQ',
                        frame_id     & 0xFFFFFFFFFFFFFFFF,
                        timestamp_ns & 0xFFFFFFFFFFFFFFFF)

    # 128 bits → repetir cada uno 8 veces → array de 1024 valores (0 ó 1)
    bits_arr = np.unpackbits(np.frombuffer(datos, dtype=np.uint8))
    mascara  = np.repeat(bits_arr, BITS_POR_BLOQUE)

    # Seleccionar canal a modificar
    if frame.ndim == 3:
        fila = frame[0, :PIXELES_LSB, 0]   # Canal Azul en BGR
    else:
        fila = frame[0, :PIXELES_LSB]       # Grayscale directo

    # Limpiar LSB actual (AND 0xFE) y poner el nuevo (OR mascara)
    fila[:] = (fila & np.uint8(0xFE)) | mascara.astype(fila.dtype)
    return frame


def extraer_lsb(frame):
    """
    Extrae 128 bits de metadatos LSB de la primera fila del frame.
    Usa majority voting sobre bloques de 8 px para tolerar artefactos H.264.
    Retorna (frame_id, timestamp_ns) o (None, None).
    """
    ancho = frame.shape[1] if frame.ndim >= 2 else 0
    if ancho < PIXELES_LSB:
        return None, None

    if frame.ndim == 3:
        fila = frame[0, :PIXELES_LSB, 0]
    else:
        fila = frame[0, :PIXELES_LSB]

    lsbs  = (fila & np.uint8(1)).reshape(TOTAL_BITS, BITS_POR_BLOQUE)
    bits  = (lsbs.sum(axis=1) > BITS_POR_BLOQUE // 2).astype(np.uint8)
    datos = np.packbits(bits)
    frame_id, timestamp_ns = struct.unpack('>QQ', datos.tobytes())
    return frame_id, timestamp_ns


# ═══════════════════════════════════════════════════════════════════════════
# GRABACIÓN DE RANGO SIN PÉRDIDAS (Asíncrona)
# ═══════════════════════════════════════════════════════════════════════════

class GrabadorRango:
    """
    Grabador en segundo plano para registrar un rango de frames sin pérdidas.
    Guarda las imágenes en carpetas individuales (PNG) con metadatos CSV.
    La escritura a disco se hace en hilo separado para no bloquear la captura.
    """
    def __init__(self, dir_salida, frame_inicio, frame_fin):
        self.dir_salida = dir_salida
        self.inicio = frame_inicio
        self.fin    = frame_fin
        self.cola      = queue.Queue()
        self.corriendo = True
        self.hilo = threading.Thread(target=self._bucle_guardado,
                                     name="GrabadorRango", daemon=True)

        for subdir in ["color", "depth", "ir1", "ir2"]:
            os.makedirs(os.path.join(self.dir_salida, subdir), exist_ok=True)

        self.csv_path = os.path.join(self.dir_salida, "metadata.csv")
        with open(self.csv_path, "w", encoding="utf-8") as f:
            f.write("frame_id,timestamp_ns,timestamp_utc\n")

        self.hilo.start()

    def agregar_frame(self, frame_id, color, depth_raw, ir1, ir2, timestamp_ns):
        """Encola un frame si está dentro del rango solicitado."""
        if self.inicio <= frame_id <= self.fin:
            self.cola.put((frame_id,
                           color.copy(), depth_raw.copy(),
                           ir1.copy(),   ir2.copy(),
                           timestamp_ns))

    def _bucle_guardado(self):
        """Hilo de escritura: desencola frames y los guarda en PNG + CSV."""
        import cv2 as _cv2
        while self.corriendo or not self.cola.empty():
            try:
                item = self.cola.get(timeout=0.2)
            except queue.Empty:
                continue

            frame_id, color, depth_raw, ir1, ir2, timestamp_ns = item
            filename = f"{frame_id:08d}.png"

            _cv2.imwrite(os.path.join(self.dir_salida, "color", filename), color)
            _cv2.imwrite(os.path.join(self.dir_salida, "depth", filename), depth_raw)
            _cv2.imwrite(os.path.join(self.dir_salida, "ir1",   filename), ir1)
            _cv2.imwrite(os.path.join(self.dir_salida, "ir2",   filename), ir2)

            try:
                dt_utc    = datetime.datetime.fromtimestamp(
                                timestamp_ns / 1e9, datetime.timezone.utc)
                fecha_utc = dt_utc.isoformat()
            except Exception:
                fecha_utc = "unknown"

            with open(self.csv_path, "a", encoding="utf-8") as f:
                f.write(f"{frame_id},{timestamp_ns},{fecha_utc}\n")

            self.cola.task_done()

    def detener(self):
        """Detiene el hilo y espera a que termine."""
        self.corriendo = False
        if self.hilo.is_alive():
            self.hilo.join(timeout=15.0)


# ═══════════════════════════════════════════════════════════════════════════
# DETECCIÓN DE DEPENDENCIAS
# ═══════════════════════════════════════════════════════════════════════════

def buscar_ffmpeg():
    """
    Busca FFmpeg en el sistema (apt primero, luego imageio-ffmpeg como fallback).
    Retorna (ruta, origen) o (None, None).
    """
    ruta_sistema = shutil.which("ffmpeg")
    if ruta_sistema:
        try:
            res = subprocess.run([ruta_sistema, "-version"],
                                 capture_output=True, text=True, timeout=5)
            if res.returncode == 0:
                version = res.stdout.split("\n")[0] if res.stdout else "?"
                return ruta_sistema, f"sistema ({version})"
        except Exception:
            pass

    try:
        import imageio_ffmpeg
        ruta_iio = imageio_ffmpeg.get_ffmpeg_exe()
        res = subprocess.run([ruta_iio, "-version"],
                             capture_output=True, text=True, timeout=5)
        if res.returncode == 0:
            return ruta_iio, "imageio-ffmpeg (fallback)"
    except Exception:
        pass

    return None, None


def verificar_pyrealsense2():
    """Importa pyrealsense2 y lo retorna, o None si no está instalado."""
    try:
        import pyrealsense2 as rs
        return rs
    except ImportError:
        return None


def verificar_opencv():
    """Importa cv2 y lo retorna, o None."""
    try:
        import cv2
        return cv2
    except ImportError:
        return None


def verificar_numpy():
    """Importa numpy, actualiza la variable global y lo retorna, o None."""
    global np
    try:
        import numpy as _np
        np = _np
        return _np
    except ImportError:
        return None


# ═══════════════════════════════════════════════════════════════════════════
# DIAGNÓSTICO DEL SISTEMA
# ═══════════════════════════════════════════════════════════════════════════

def verificar_reglas_udev():
    """Verifica si las reglas udev de Intel RealSense están instaladas."""
    rutas = [
        "/etc/udev/rules.d/99-realsense-libusb.rules",
        "/etc/udev/rules.d/99-realsense-d4xx.rules",
    ]
    for ruta in rutas:
        if os.path.isfile(ruta):
            return True, ruta
    return False, None


def verificar_dispositivos_usb():
    """Busca dispositivos Intel RealSense en el bus USB."""
    try:
        res = subprocess.run(["lsusb"], capture_output=True, text=True, timeout=5)
        lineas = [
            l.strip() for l in res.stdout.split("\n")
            if "8086" in l and any(x in l.lower()
                for x in ["0b07", "0ad", "realsense", "0b3a", "0b5c", "0b64"])
        ]
        if not lineas:
            lineas = [
                l.strip() for l in res.stdout.split("\n")
                if "Intel Corp" in l and any(x in l for x in ["RealSense", "D4", "D5"])
            ]
        return lineas
    except Exception:
        return []


def verificar_puerto_disponible(puerto):
    """Retorna True si el puerto TCP está libre."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        result = s.connect_ex(("127.0.0.1", puerto))
        s.close()
        return result != 0
    except Exception:
        return True


def ejecutar_diagnostico():
    """Diagnóstico completo del sistema para RTSP con RealSense en Ubuntu."""
    print("\n" + "═" * 60)
    print("  DIAGNÓSTICO DEL SISTEMA — Emisor RTSP Ubuntu")
    print("═" * 60)

    ruta_ff, origen_ff = buscar_ffmpeg()
    if ruta_ff:
        print(f"\n  [✓] FFmpeg: {ruta_ff}")
        print(f"      Origen: {origen_ff}")
    else:
        print("\n  [✗] FFmpeg NO encontrado")
        print("      Instalar con: sudo apt install ffmpeg")

    cv2 = verificar_opencv()
    _np = verificar_numpy()
    rs  = verificar_pyrealsense2()

    print(f"  [{'✓' if cv2 else '✗'}] OpenCV: {'v' + cv2.__version__ if cv2 else 'NO instalado'}")
    print(f"  [{'✓' if _np else '✗'}] NumPy:  {'v' + _np.__version__ if _np else 'NO instalado'}")
    print(f"  [{'✓' if rs  else '✗'}] pyrealsense2: {'disponible' if rs else 'NO instalado'}")

    if rs:
        # Enumerar cámaras con detalle de USB
        ctx         = rs.context()
        dispositivos = ctx.query_devices()
        print(f"\n  Cámaras RealSense detectadas: {len(dispositivos)}")
        for i, dev in enumerate(dispositivos):
            nombre = dev.get_info(rs.camera_info.name)
            serie  = dev.get_info(rs.camera_info.serial_number)
            try:
                usb = dev.get_info(rs.camera_info.usb_type_descriptor)
            except Exception:
                usb = "?"
            print(f"    [{i}] {nombre}  S/N:{serie}  USB:{usb}")
            if usb.startswith("2"):
                print("        ⚠ USB 2.x: usa solo 1280×720 @ 15fps para evitar errores")

    udev_ok, udev_ruta = verificar_reglas_udev()
    if udev_ok:
        print(f"\n  [✓] Reglas udev: {udev_ruta}")
    else:
        print("\n  [⚠] Reglas udev de RealSense NO encontradas")
        print("      Esto puede causar errores de permisos USB.")
        print("      Instalar con:")
        print("        wget https://raw.githubusercontent.com/IntelRealSense/"
              "librealsense/master/config/99-realsense-libusb.rules")
        print("        sudo cp 99-realsense-libusb.rules /etc/udev/rules.d/")
        print("        sudo udevadm control --reload-rules && sudo udevadm trigger")

    dispositivos_usb = verificar_dispositivos_usb()
    if dispositivos_usb:
        print(f"\n  [✓] Dispositivo(s) en USB: {len(dispositivos_usb)}")
        for d in dispositivos_usb:
            print(f"      → {d}")
    else:
        print("\n  [⚠] No se detectaron dispositivos RealSense en lsusb")

    puerto_base = PUERTO_RTSP_DEFECTO
    print()
    for i, nombre in enumerate(["color", "depth", "ir1", "ir2"]):
        p    = puerto_base + i
        libre = verificar_puerto_disponible(p)
        estado = "✓ libre" if libre else "✗ EN USO"
        print(f"  [{estado}] Puerto {p} ({nombre})")

    print("\n" + "═" * 60 + "\n")


# ═══════════════════════════════════════════════════════════════════════════
# CÁMARA REALSENSE — APERTURA ROBUSTA (como RealSense Viewer)
# ═══════════════════════════════════════════════════════════════════════════

def listar_camaras(rs):
    """Lista las cámaras RealSense conectadas con detalle USB."""
    ctx         = rs.context()
    dispositivos = ctx.query_devices()

    if len(dispositivos) == 0:
        print("\n  ✗ No se detectaron cámaras Intel RealSense.")
        print("    1. Verifica que la cámara esté conectada a USB 3.0 (puerto azul)")
        print("    2. Ejecuta: python3 emisor_ubuntu.py --diagnostico")
        return []

    print(f"\n  Cámaras Intel RealSense detectadas: {len(dispositivos)}")
    print("  " + "─" * 65)
    print(f"  {'Idx':<5} {'Nombre':<30} {'Nº Serie':<15} {'USB'}")
    print("  " + "─" * 65)

    lista = []
    for i, dev in enumerate(dispositivos):
        nombre = dev.get_info(rs.camera_info.name)
        serie  = dev.get_info(rs.camera_info.serial_number)
        try:
            usb = dev.get_info(rs.camera_info.usb_type_descriptor)
        except Exception:
            usb = "?"
        print(f"  {i:<5} {nombre:<30} {serie:<15} {usb}")
        lista.append({"indice": i, "nombre": nombre, "serie": serie, "usb": usb})

    print("  " + "─" * 65)
    return lista


def obtener_ip_local():
    """Obtiene la IP local LAN de la máquina."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def abrir_camara_robusto(rs, indice_camara):
    """
    Abre la cámara RealSense de manera robusta, igual que lo hace
    el RealSense Viewer:

      1. Realiza hardware_reset() para limpiar cualquier sesión anterior
         (cámara colgada de un proceso anterior o del propio Viewer).
      2. Espera 3 segundos para que el dispositivo re-enumere en USB.
      3. Vuelve a listar los dispositivos (el índice puede cambiar).
      4. Intenta pipeline.start() hasta 3 veces con pausa entre intentos.

    Retorna (pipeline, perfil) o lanza RuntimeError si todos los intentos fallan.
    """
    # ── Paso 1: Hardware reset ───────────────────────────────────────────
    print("  → Reseteando dispositivo para limpiar sesiones anteriores ...")
    try:
        ctx_reset    = rs.context()
        devs_reset   = ctx_reset.query_devices()
        if len(devs_reset) == 0:
            raise RuntimeError("No se detectó ninguna cámara RealSense.")

        if indice_camara >= len(devs_reset):
            raise RuntimeError(
                f"Índice {indice_camara} fuera de rango "
                f"({len(devs_reset)} cámara(s) disponible(s))."
            )

        dev_reset = devs_reset[indice_camara]
        nombre_cam = dev_reset.get_info(rs.camera_info.name)
        serial_cam = dev_reset.get_info(rs.camera_info.serial_number)
        try:
            usb_tipo = dev_reset.get_info(rs.camera_info.usb_type_descriptor)
        except Exception:
            usb_tipo = "?"

        print(f"  → Dispositivo: {nombre_cam}  S/N: {serial_cam}  USB: {usb_tipo}")

        dev_reset.hardware_reset()
        print("  → Hardware reset enviado. Esperando re-enumeración (3 s) ...")
        time.sleep(3.0)

    except Exception as e:
        print(f"  ⚠ Hardware reset falló: {e}")
        print("    Continuando sin reset (puede que la cámara ya esté libre) ...")
        # Usamos el serial que ya conocemos o seguimos con índice
        serial_cam = None
        nombre_cam = f"cámara índice {indice_camara}"
        usb_tipo   = "?"

    # ── Paso 2: Re-enumerar después del reset ────────────────────────────
    ctx2  = rs.context()
    devs2 = ctx2.query_devices()

    if len(devs2) == 0:
        raise RuntimeError(
            "La cámara no apareció después del reset. "
            "Verifica la conexión USB 3.0 y las reglas udev."
        )

    # Buscar el dispositivo por serial (si lo tenemos) o por índice
    dev_final = None
    if serial_cam:
        for dev in devs2:
            try:
                if dev.get_info(rs.camera_info.serial_number) == serial_cam:
                    dev_final = dev
                    break
            except Exception:
                pass

    if dev_final is None:
        if indice_camara < len(devs2):
            dev_final = devs2[indice_camara]
        else:
            raise RuntimeError(
                f"No se encontró el dispositivo (índice {indice_camara}) "
                f"tras el reset. Cámaras disponibles: {len(devs2)}."
            )

    serial_final = dev_final.get_info(rs.camera_info.serial_number)
    nombre_final = dev_final.get_info(rs.camera_info.name)
    try:
        usb_final = dev_final.get_info(rs.camera_info.usb_type_descriptor)
    except Exception:
        usb_final = "?"

    print(f"  ✓ Dispositivo listo: {nombre_final}  S/N: {serial_final}  USB: {usb_final}")

    # Advertencia si está en USB 2.x
    if usb_final.startswith("2"):
        print("  ⚠ ADVERTENCIA: Cámara en USB 2.x.")
        print("    Las resoluciones máximas pueden causar errores de bandwidth.")
        print("    Considera usar --usb2 para ajustar automáticamente.")

    # ── Paso 3: Configurar y arrancar el pipeline (con reintentos) ───────
    MAX_INTENTOS = 3
    for intento in range(1, MAX_INTENTOS + 1):
        print(f"  → Iniciando pipeline (intento {intento}/{MAX_INTENTOS}) ...")
        try:
            pipeline = rs.pipeline()
            config   = rs.config()
            config.enable_device(serial_final)

            # Streams a resolución nativa D435
            # Color: 1920×1080 necesita USB 3.x
            # Depth + IR: 1280×720 (≥ 1024 px para LSB)
            config.enable_stream(rs.stream.color,    1920, 1080, rs.format.bgr8, 30)
            config.enable_stream(rs.stream.depth,    1280,  720, rs.format.z16,  30)
            config.enable_stream(rs.stream.infrared, 1,
                                 1280,  720, rs.format.y8,  30)
            config.enable_stream(rs.stream.infrared, 2,
                                 1280,  720, rs.format.y8,  30)

            perfil = pipeline.start(config)
            print(f"  ✓ Pipeline iniciado (Color 1920×1080, Depth/IR 1280×720 @ 30fps)")
            return pipeline, perfil

        except RuntimeError as e:
            msg = str(e)
            print(f"  ✗ Intento {intento} falló: {msg}")

            if intento < MAX_INTENTOS:
                espera = 2 * intento
                print(f"    Esperando {espera} s antes de reintentar ...")
                time.sleep(espera)
            else:
                raise RuntimeError(
                    f"No se pudo iniciar la cámara tras {MAX_INTENTOS} intentos.\n"
                    f"Último error: {msg}\n"
                    f"Verifica:\n"
                    f"  • Puerto USB 3.0 (conector azul)\n"
                    f"  • Cable USB original Intel\n"
                    f"  • Reglas udev instaladas (ver --diagnostico)\n"
                    f"  • Firmware actualizado con realsense-viewer\n"
                    f"  • Ningún otro proceso usando la cámara (realsense-viewer, rs-enumerate-devices)"
                )


# ═══════════════════════════════════════════════════════════════════════════
# PROCESOS FFMPEG — RTSP SERVER DIRECTO
# ═══════════════════════════════════════════════════════════════════════════

def crear_ffmpeg(ruta_ffmpeg, puerto_listen, ancho, alto, pix_fmt, fps, bitrate_kbps):
    """
    Lanza un subproceso FFmpeg como servidor RTSP directo (sin MediaMTX).
    FFmpeg abre un socket TCP en el puerto indicado y espera conexiones RTSP.
    """
    url_listen = f"rtsp://0.0.0.0:{puerto_listen}/stream"
    cmd = [
        ruta_ffmpeg,
        "-y",
        "-f", "rawvideo",
        "-vcodec", "rawvideo",
        "-pix_fmt", pix_fmt,
        "-s", f"{ancho}x{alto}",
        "-r", str(fps),
        "-i", "-",                          # Leer desde stdin
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-preset", "ultrafast",             # Mínima latencia de codificación
        "-tune", "zerolatency",             # Optimizado para streaming en vivo
        "-b:v", f"{bitrate_kbps}k",
        "-maxrate", f"{bitrate_kbps}k",
        "-bufsize", f"{bitrate_kbps * 2}k",
        "-g", str(fps * 2),                 # GOP de 2 segundos
        "-f", "rtsp",
        "-rtsp_transport", "tcp",
        "-rtsp_flags", "listen",
        url_listen,
    ]
    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


# ═══════════════════════════════════════════════════════════════════════════
# FUNCIÓN PRINCIPAL DEL EMISOR
# ═══════════════════════════════════════════════════════════════════════════

def iniciar_emisor(indice_camara=0, puerto=PUERTO_RTSP_DEFECTO,
                   bitrate_kbps=2000, rango_grabacion=None, dir_salida=None):
    """
    Emisor RTSP v5: apertura robusta de la cámara, esteganografía LSB
    (BITS_POR_BLOQUE=8, compatible con receptor_ubuntu.py), FFmpeg RTSP directo.
    """
    global _cerrando
    procesos_ff    = {}
    pipeline       = None
    pipeline_activo = False

    puertos = {
        "color": puerto,
        "depth": puerto + 1,
        "ir1":   puerto + 2,
        "ir2":   puerto + 3,
    }

    # ─── Señales POSIX para cierre limpio ───────────────────────────────────
    def manejar_senal(signum, frame):
        global _cerrando
        nombre = signal.Signals(signum).name
        print(f"\n  ⏹ Señal {nombre} recibida. Cerrando ...")
        _cerrando = True

    signal.signal(signal.SIGINT,  manejar_senal)
    signal.signal(signal.SIGTERM, manejar_senal)

    try:
        print("\n" + "═" * 65)
        print("  EMISOR RTSP — Intel RealSense D435 · Ubuntu (v5)")
        print("  LSB: 8 px/bit · 1024 px totales · FFmpeg RTSP directo")
        print("═" * 65)

        # ── PASO 1: FFmpeg ───────────────────────────────────────────────
        print("\n[1/5] Buscando FFmpeg ...")
        ruta_ffmpeg, origen = buscar_ffmpeg()
        if ruta_ffmpeg is None:
            print("  ✗ FFmpeg no encontrado.  Instalar: sudo apt install ffmpeg")
            sys.exit(1)
        print(f"  ✓ FFmpeg: {ruta_ffmpeg}  ({origen})")

        # ── PASO 2: Dependencias Python ──────────────────────────────────
        print("\n[2/5] Verificando dependencias Python ...")
        cv2 = verificar_opencv()
        if cv2 is None:
            print("  ✗ opencv-python no instalado.")
            print("    Instalar: pip install opencv-python")
            print("    Si falta libGL: sudo apt install libgl1 libglib2.0-0")
            sys.exit(1)
        print(f"  ✓ OpenCV {cv2.__version__}")

        _np = verificar_numpy()
        if _np is None:
            print("  ✗ numpy no instalado.  Instalar: pip install numpy")
            sys.exit(1)
        print(f"  ✓ NumPy {_np.__version__}")

        rs = verificar_pyrealsense2()
        if rs is None:
            print("  ✗ pyrealsense2 no disponible.")
            print("    Instalar: pip install pyrealsense2")
            print("    O ver: python3 emisor_ubuntu.py --diagnostico")
            sys.exit(1)
        print("  ✓ pyrealsense2 disponible")

        udev_ok, udev_ruta = verificar_reglas_udev()
        if not udev_ok:
            print("  ⚠ Reglas udev de RealSense no encontradas.")
            print("    Si la cámara no abre ejecuta --diagnostico para ver cómo instalarlas.")
        else:
            print(f"  ✓ Reglas udev: {udev_ruta}")

        # ── PASO 3: Abrir la cámara (robusta) ───────────────────────────
        print(f"\n[3/5] Abriendo cámara Intel RealSense (índice {indice_camara}) ...")
        try:
            pipeline, perfil = abrir_camara_robusto(rs, indice_camara)
            pipeline_activo = True
        except RuntimeError as e:
            print(f"\n  ✗ {e}")
            sys.exit(1)

        # ── PASO 4: Lanzar 4 FFmpeg RTSP ────────────────────────────────
        print(f"\n[4/5] Iniciando 4 servidores RTSP (FFmpeg directo) ...")

        for nombre, p in puertos.items():
            if not verificar_puerto_disponible(p):
                print(f"  ✗ Puerto {p} ({nombre}) ya está en uso.")
                print(f"    Libera con: sudo fuser -k {p}/tcp")
                sys.exit(1)

        bitrates = {
            "color": max(200, int(bitrate_kbps * 0.55)),
            "depth": max(200, int(bitrate_kbps * 0.25)),
            "ir1":   max(100, int(bitrate_kbps * 0.10)),
            "ir2":   max(100, int(bitrate_kbps * 0.10)),
        }
        canales = {
            "color": {"ancho": 1920, "alto": 1080, "pix": "bgr24", "br": bitrates["color"]},
            "depth": {"ancho": 1280, "alto": 720,  "pix": "bgr24", "br": bitrates["depth"]},
            "ir1":   {"ancho": 1280, "alto": 720,  "pix": "gray",  "br": bitrates["ir1"]},
            "ir2":   {"ancho": 1280, "alto": 720,  "pix": "gray",  "br": bitrates["ir2"]},
        }

        for nombre, cfg in canales.items():
            p = puertos[nombre]
            print(f"  → {nombre:<6} {cfg['ancho']}×{cfg['alto']} @ {cfg['br']}kbps → puerto {p}")
            procesos_ff[nombre] = crear_ffmpeg(
                ruta_ffmpeg, p,
                cfg["ancho"], cfg["alto"], cfg["pix"], 30, cfg["br"]
            )

        # Dar tiempo a FFmpeg para abrir los sockets de escucha
        time.sleep(2)

        # ── PASO 5: Grabación de rango (opcional) ────────────────────────
        print("\n[5/5] Grabación local en emisor: solo si se usa --grabar-rango")
        grabador_rango = None
        if rango_grabacion is not None:
            try:
                frame_inicio, frame_fin = rango_grabacion
                if dir_salida is None:
                    dir_salida = f"rango_{frame_inicio}_{frame_fin}"
                dir_salida_abs = os.path.abspath(dir_salida)
                print(f"\n  🔴 Grabación de rango activada: [{frame_inicio} – {frame_fin}]")
                print(f"     Directorio: {dir_salida_abs}")
                grabador_rango = GrabadorRango(dir_salida_abs, frame_inicio, frame_fin)
            except Exception as e:
                print(f"  ⚠ No se pudo iniciar el grabador de rango: {e}")

        # ── Banner final con URLs ────────────────────────────────────────
        ip_local = obtener_ip_local()

        print("\n" + "═" * 65)
        print("  ✓ TRANSMISIÓN ACTIVA — 4 Canales RTSP con LSB")
        print("─" * 65)
        print(f"  Color (RGB):     rtsp://{ip_local}:{puertos['color']}/stream")
        print(f"  Profundidad:     rtsp://{ip_local}:{puertos['depth']}/stream")
        print(f"  Infrarrojo 1:    rtsp://{ip_local}:{puertos['ir1']}/stream")
        print(f"  Infrarrojo 2:    rtsp://{ip_local}:{puertos['ir2']}/stream")
        print("─" * 65)
        print(f"  LSB: {BITS_POR_BLOQUE} px/bit × {TOTAL_BITS} bits = {PIXELES_LSB} px en fila 0")
        if grabador_rango is not None:
            print(f"  🔴 GRABANDO [{frame_inicio} – {frame_fin}] → {dir_salida_abs}")
        print("─" * 65)
        print(f"  Receptor: python3 receptor_ubuntu.py {ip_local}")
        print(f"  VLC:      vlc rtsp://{ip_local}:{puertos['color']}/stream")
        print("═" * 65)
        print("\n  Presiona Ctrl+C para detener.\n")

        # ── Bucle principal de captura y transmisión ──────────────────────
        frame_id = 1
        t_inicio = time.time()

        while not _cerrando:
            # Capturar frameset síncrono (todos los 4 canales del mismo instante)
            try:
                frameset = pipeline.wait_for_frames(timeout_ms=5000)
            except RuntimeError:
                print("  ⚠ Timeout esperando frames de la cámara ...")
                continue

            # Timestamp capturado INMEDIATAMENTE después del frameset
            timestamp_ns = time.time_ns()

            # Extraer frames individuales
            fc  = frameset.get_color_frame()
            fd  = frameset.get_depth_frame()
            fi1 = frameset.get_infrared_frame(1)
            fi2 = frameset.get_infrared_frame(2)

            if not fc or not fd or not fi1 or not fi2:
                continue

            # Convertir a NumPy (copias escribibles para LSB in-place)
            color_img = np.array(fc.get_data())          # 1920×1080 BGR
            depth_raw = np.asanyarray(fd.get_data())      # 1280×720 Z16
            ir1_img   = np.array(fi1.get_data())          # 1280×720 gray
            ir2_img   = np.array(fi2.get_data())          # 1280×720 gray

            # Profundidad Z16 → heatmap JET BGR (para visualización)
            depth_clipped = np.clip(depth_raw, 0, 4000)
            depth_8bit    = (depth_clipped * (255.0 / 4000.0)).astype(np.uint8)
            depth_color   = cv2.applyColorMap(depth_8bit, cv2.COLORMAP_JET)
            depth_color[depth_raw == 0] = [0, 0, 0]  # Negro donde no hay dato

            # Inyectar esteganografía LSB en los 4 frames
            # (mismo frame_id y timestamp en los 4 canales → garantiza sincronía)
            inyectar_lsb(color_img,   frame_id, timestamp_ns)
            inyectar_lsb(depth_color, frame_id, timestamp_ns)
            inyectar_lsb(ir1_img,     frame_id, timestamp_ns)
            inyectar_lsb(ir2_img,     frame_id, timestamp_ns)

            # Enviar a los 4 FFmpeg RTSP
            try:
                procesos_ff["color"].stdin.write(color_img.tobytes())
                procesos_ff["depth"].stdin.write(depth_color.tobytes())
                procesos_ff["ir1"].stdin.write(ir1_img.tobytes())
                procesos_ff["ir2"].stdin.write(ir2_img.tobytes())
            except (BrokenPipeError, OSError) as e:
                print(f"  ✗ Error escribiendo a FFmpeg RTSP: {e}")
                break

            # Grabación de rango sin pérdidas
            if grabador_rango is not None:
                grabador_rango.agregar_frame(
                    frame_id, color_img, depth_raw,
                    ir1_img, ir2_img, timestamp_ns
                )
                if frame_id >= grabador_rango.fin:
                    print(f"\n  ✓ Rango [{grabador_rango.inicio}–{grabador_rango.fin}] completo.")
                    grabador_rango.detener()
                    print("  ✓ Grabación sin pérdidas finalizada.")
                    grabador_rango = None

            frame_id += 1

            # Log de estado cada ~5 segundos (150 frames a 30fps)
            if (frame_id - 1) % 150 == 0:
                dt         = time.time() - t_inicio
                fps_actual = (frame_id - 1) / dt if dt > 0 else 0
                ts_str     = time.strftime("%H:%M:%S", time.localtime(timestamp_ns / 1e9))
                ts_ms      = int((timestamp_ns % 1_000_000_000) / 1_000_000)
                print(f"  📹 FID: {frame_id - 1}  TS: {ts_str}.{ts_ms:03d}"
                      f"  FPS: {fps_actual:.1f}  Tiempo: {dt:.0f}s")

            # Vigilar que los FFmpeg sigan vivos
            for nombre, proc in procesos_ff.items():
                if proc.poll() is not None:
                    print(f"  ✗ FFmpeg ({nombre}) se detuvo inesperadamente.")
                    _cerrando = True
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
            print("  ✓ Grabador de rango finalizado.")

        if pipeline_activo and pipeline:
            try:
                pipeline.stop()
                print("  ✓ Pipeline RealSense detenido")
            except Exception:
                pass

        # Cerrar stdin de los 4 FFmpeg
        for nombre, proc in procesos_ff.items():
            if proc and proc.stdin:
                try:
                    proc.stdin.close()
                except Exception:
                    pass

        # Terminar procesos FFmpeg
        for nombre, proc in procesos_ff.items():
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                    print(f"  ✓ FFmpeg ({nombre}) detenido")
                except Exception:
                    proc.kill()
                    print(f"  ✓ FFmpeg ({nombre}) forzado")

        total_frames = frame_id - 1 if 'frame_id' in locals() else 0
        if total_frames > 0:
            dt_total = time.time() - t_inicio
            print(f"\n  Resumen: {total_frames} frames en {dt_total:.1f}s "
                  f"({total_frames / dt_total:.1f} FPS promedio)")

        print("\n  Emisor finalizado.\n")


# ═══════════════════════════════════════════════════════════════════════════
# PUNTO DE ENTRADA
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=(
            "Emisor RTSP v5 para Intel RealSense D435 — Ubuntu/Linux nativo.\n"
            "Apertura robusta (hardware reset + reintentos) + LSB 8px/bit."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  python3 emisor_ubuntu.py                          # Cámara 0, puerto 8554
  python3 emisor_ubuntu.py --cam 1                  # Segunda cámara
  python3 emisor_ubuntu.py --puerto 9554            # Puerto base alternativo
  python3 emisor_ubuntu.py --calidad 4000           # Mayor calidad
  python3 emisor_ubuntu.py --listar-camaras         # Ver cámaras conectadas
  python3 emisor_ubuntu.py --diagnostico            # Diagnóstico del sistema
  python3 emisor_ubuntu.py --grabar-rango 150 450   # Grabar rango sin pérdidas

Puertos RTSP (base + offset):
  Color:  base     (ej. 8554)
  Depth:  base + 1 (ej. 8555)
  IR1:    base + 2 (ej. 8556)
  IR2:    base + 3 (ej. 8557)
        """
    )

    parser.add_argument("--puerto", type=int, default=PUERTO_RTSP_DEFECTO,
                        help=f"Puerto RTSP base (defecto: {PUERTO_RTSP_DEFECTO})")
    parser.add_argument("--cam", type=int, default=0,
                        help="Índice de la cámara RealSense (defecto: 0)")
    parser.add_argument("--calidad", type=int, default=2000,
                        help="Bitrate total en kbps (defecto: 2000)")
    parser.add_argument("--grabar-rango", type=int, nargs=2,
                        metavar=("INICIO", "FIN"), default=None,
                        help="Grabar un rango de frames sin pérdidas")
    parser.add_argument("--dir-salida", type=str, default=None,
                        help="Directorio de salida para la grabación de rango")
    parser.add_argument("--listar-camaras", action="store_true",
                        help="Listar cámaras RealSense y salir")
    parser.add_argument("--diagnostico", action="store_true",
                        help="Ejecutar diagnóstico completo del sistema")

    args = parser.parse_args()

    if args.diagnostico:
        ejecutar_diagnostico()
        sys.exit(0)

    if args.listar_camaras:
        rs = verificar_pyrealsense2()
        if rs is None:
            print("  ✗ pyrealsense2 no está instalado.")
            print("    Ejecuta: python3 emisor_ubuntu.py --diagnostico")
            sys.exit(1)
        listar_camaras(rs)
        sys.exit(0)

    iniciar_emisor(
        indice_camara=args.cam,
        puerto=args.puerto,
        bitrate_kbps=args.calidad,
        rango_grabacion=args.grabar_rango,
        dir_salida=args.dir_salida,
    )
