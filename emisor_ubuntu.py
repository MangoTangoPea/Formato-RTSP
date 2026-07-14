#!/usr/bin/env python3
"""
Emisor RTSP para Intel RealSense D435 — Ubuntu/Linux Nativo (v4).

Arquitectura de grabación y streaming con esteganografía LSB:
  - Inyecta Frame ID (64 bits) + Timestamp (64 bits) en cada frame vía LSB
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

# ─── Importación soft de numpy ──────────────────────────────────────────
# Necesaria para las funciones LSB a nivel de módulo.
# El diagnóstico y listado de cámaras siguen funcionando sin numpy.
try:
    import numpy as np
except ImportError:
    np = None


# ═══════════════════════════════════════════════════════════════════════════
# CONSTANTES GLOBALES
# ═══════════════════════════════════════════════════════════════════════════

PUERTO_RTSP_DEFECTO = 8554

# ─── Flag global para cierre limpio con señales POSIX ───────────────────
# Se activa cuando se recibe SIGINT (Ctrl+C) o SIGTERM para que el bucle
# principal termine de forma ordenada.
_cerrando = False


# ═══════════════════════════════════════════════════════════════════════════
# ESTEGANOGRAFÍA LSB (Least Significant Bit)
# ═══════════════════════════════════════════════════════════════════════════
#
# Inyecta y extrae 128 bits de metadatos (64-bit Frame ID + 64-bit Timestamp)
# en la primera fila de cada frame usando el bit menos significativo.
#
# ── Esquema de codificación ──
# Los 128 bits de payload (Frame ID + Timestamp) se empaquetan en big-endian
# como 16 bytes contiguos usando struct.pack('>QQ', ...).
#
# Cada bit lógico se replica en BITS_POR_BLOQUE (8) píxeles consecutivos
# para crear redundancia que permita sobrevivir a la compresión H.264.
# En la extracción se aplica "majority voting" sobre cada bloque de 8 píxeles
# para reconstruir el bit original (si ≥5 de 8 LSBs son 1, el bit es 1).
#
# Total de píxeles utilizados: 128 × 8 = 1024 (caben en 1280px y 1920px).
#
# ── Impacto visual ──
# Solo se modifica el bit menos significativo de cada píxel. Esto produce
# un cambio máximo de ±1 en un rango de 0..255, es decir, una perturbación
# del 0.39% que es completamente imperceptible al ojo humano.
#
# ── Canal de inyección ──
# - En imágenes BGR (Color, Depth heatmap): se modifica el LSB del canal
#   Azul [0] únicamente, dejando intactos los canales Verde y Rojo.
# - En imágenes monocromáticas (IR1, IR2): se modifica el LSB del píxel
#   directamente (ya que solo hay un canal).

BITS_POR_BLOQUE = 1
TOTAL_BITS = 128
PIXELES_LSB = TOTAL_BITS * BITS_POR_BLOQUE  # 128 (píxeles del 0 al 127)


def inyectar_lsb(frame, frame_id, timestamp_ns):
    """
    Inyecta 128 bits de metadatos en la primera fila (fila 0) del frame.

    El payload consta de:
      - Bits [0..63]:   Frame ID (entero secuencial de 64 bits, inicia en 1)
      - Bits [64..127]: Timestamp del computador emisor (nanosegundos, time.time_ns())

    Cada bit lógico se replica en 8 píxeles consecutivos para redundancia.

    Comportamiento según tipo de imagen:
      - Frame BGR (3D, shape H×W×3): modifica únicamente el LSB del canal
        Azul (canal [0] en orden BGR de OpenCV).
      - Frame Grayscale (2D, shape H×W): modifica el LSB del píxel directamente.

    Modifica el frame in-place y lo retorna.

    Args:
        frame:        Imagen NumPy (H×W×3 para BGR, H×W para grayscale).
        frame_id:     Identificador secuencial del frame (entero 64 bits).
        timestamp_ns: Timestamp del sistema emisor en nanosegundos.

    Returns:
        El mismo frame (modificado in-place).
    """
    ancho = frame.shape[1]

    # Verificar que la imagen es lo suficientemente ancha para los 1024 píxeles LSB
    if ancho < PIXELES_LSB:
        return frame

    # ── Empaquetar payload a 16 bytes big-endian ────────────────────────
    # struct.pack('>QQ', ...) produce exactamente 16 bytes (128 bits).
    # La máscara & 0xFFFF...F asegura que valores negativos se trunquen
    # correctamente a 64 bits sin signo.
    datos = struct.pack('>QQ',
                        frame_id & 0xFFFFFFFFFFFFFFFF,
                        timestamp_ns & 0xFFFFFFFFFFFFFFFF)

    # ── Desempaquetar a 128 bits individuales y expandir con redundancia ─
    # np.unpackbits convierte los 16 bytes en un array de 128 bits (0 o 1).
    # np.repeat duplica cada bit 8 veces → array de 1024 valores.
    bits_arr = np.unpackbits(np.frombuffer(datos, dtype=np.uint8))
    mascara = np.repeat(bits_arr, BITS_POR_BLOQUE)

    # ── Obtener referencia directa a la primera fila (sin copia) ────────
    # Accedemos directamente al array para modificar in-place sin overhead.
    if frame.ndim == 3:
        # Imagen BGR: solo modificamos el canal Azul (índice 0)
        fila = frame[0, :PIXELES_LSB, 0]
    else:
        # Imagen grayscale: modificamos el píxel directamente
        fila = frame[0, :PIXELES_LSB]

    # ── Aplicar inyección LSB ───────────────────────────────────────────
    # Paso 1: Limpiar el LSB actual con AND 0xFE (11111110 en binario)
    # Paso 2: Establecer el nuevo LSB con OR de la máscara
    # Resultado: el LSB de cada píxel queda exactamente como el bit de datos
    fila[:] = (fila & np.uint8(0xFE)) | mascara.astype(fila.dtype)
    return frame


def extraer_lsb(frame):
    """
    Extrae 128 bits de metadatos LSB de la primera fila del frame
    usando majority voting sobre bloques de BITS_POR_BLOQUE píxeles.

    El proceso es el inverso de inyectar_lsb():
      1. Lee los 1024 LSBs de la fila 0
      2. Los agrupa en 128 bloques de 8 píxeles
      3. Aplica majority voting: si ≥5 de 8 LSBs son 1, el bit lógico es 1
      4. Empaqueta los 128 bits en 16 bytes y los desempaqueta con struct

    Retorna (frame_id, timestamp_ns) o (None, None) si no es posible extraer.
    """
    ancho = frame.shape[1] if frame.ndim >= 2 else 0
    if ancho < PIXELES_LSB:
        return None, None

    # Seleccionar la fila 0 del canal correcto
    if frame.ndim == 3:
        fila = frame[0, :PIXELES_LSB, 0]   # Canal Azul en BGR
    else:
        fila = frame[0, :PIXELES_LSB]       # Grayscale directo

    # ── Extraer LSBs ────────────────────────────────────────────────────
    # AND con 1 extrae solo el bit menos significativo de cada píxel.
    # Reshape agrupa los 1024 valores en 128 bloques de 8.
    lsbs = (fila & np.uint8(1)).reshape(TOTAL_BITS, BITS_POR_BLOQUE)

    # ── Majority voting ─────────────────────────────────────────────────
    # Sumamos los LSBs de cada bloque (resultado 0..8).
    # Si la suma es > 4 (es decir ≥5), el bit lógico original era 1.
    # Esto permite tolerar hasta 3 bits corrompidos por la compresión H.264.
    bits = (lsbs.sum(axis=1) > BITS_POR_BLOQUE // 2).astype(np.uint8)

    # ── Empaquetar bits → bytes → struct ────────────────────────────────
    datos = np.packbits(bits)
    frame_id, timestamp_ns = struct.unpack('>QQ', datos.tobytes())
    return frame_id, timestamp_ns


# ═══════════════════════════════════════════════════════════════════════════
# GRABACIÓN DE RANGO SIN PÉRDIDAS (Asíncrona)
# ═══════════════════════════════════════════════════════════════════════════
#
# Permite grabar un rango específico de frames (ej: frame 100 a 500) en
# formato PNG sin pérdidas, incluyendo la profundidad en 16 bits nativos.
# Los metadatos (Frame ID, timestamp) se registran en un CSV sincronizado.

class GrabadorRango:
    """
    Grabador en segundo plano para registrar un rango específico de frames sin pérdidas.
    Guarda las imágenes en carpetas individuales (PNG) y la profundidad en 16 bits nativos (Z16).
    Registra los metadatos de sincronía (Frame ID y timestamps) en un archivo CSV.

    La escritura a disco se realiza en un hilo dedicado para no bloquear el bucle
    principal de captura/transmisión. Los frames se encolan como copias independientes
    para evitar condiciones de carrera con el bucle principal.
    """
    def __init__(self, dir_salida, frame_inicio, frame_fin):
        self.dir_salida = dir_salida
        self.inicio = frame_inicio
        self.fin = frame_fin
        self.cola = queue.Queue()
        self.corriendo = True
        self.hilo = threading.Thread(target=self._bucle_guardado, name="GrabadorRango", daemon=True)

        # Crear estructura de carpetas para cada canal
        for subdir in ["color", "depth", "ir1", "ir2"]:
            os.makedirs(os.path.join(self.dir_salida, subdir), exist_ok=True)

        # Crear archivo de metadatos CSV con encabezado
        self.csv_path = os.path.join(self.dir_salida, "metadata.csv")
        with open(self.csv_path, "w", encoding="utf-8") as f:
            f.write("frame_id,timestamp_ns,timestamp_utc\n")

        self.hilo.start()

    def agregar_frame(self, frame_id, color, depth_raw, ir1, ir2, timestamp_ns):
        """Encola un frame para guardado si está dentro del rango solicitado."""
        if self.inicio <= frame_id <= self.fin:
            # Mandamos copias a la cola para evitar modificaciones concurrentes
            self.cola.put((frame_id, color.copy(), depth_raw.copy(), ir1.copy(), ir2.copy(), timestamp_ns))

    def _bucle_guardado(self):
        """Hilo de escritura: desencola frames y los guarda en PNG + CSV."""
        while self.corriendo or not self.cola.empty():
            try:
                item = self.cola.get(timeout=0.2)
            except queue.Empty:
                continue

            frame_id, color, depth_raw, ir1, ir2, timestamp_ns = item

            # Nombre del archivo con ceros a la izquierda (ej: 00000150.png)
            filename = f"{frame_id:08d}.png"

            # Guardar imágenes sin pérdida (PNG)
            # OpenCV detecta uint16 y guarda automáticamente como PNG de 16 bits
            cv2.imwrite(os.path.join(self.dir_salida, "color", filename), color)
            cv2.imwrite(os.path.join(self.dir_salida, "depth", filename), depth_raw)
            cv2.imwrite(os.path.join(self.dir_salida, "ir1", filename), ir1)
            cv2.imwrite(os.path.join(self.dir_salida, "ir2", filename), ir2)

            # Registrar metadatos en CSV
            try:
                dt_utc = datetime.datetime.fromtimestamp(timestamp_ns / 1e9, datetime.timezone.utc)
                fecha_utc = dt_utc.isoformat()
            except Exception:
                fecha_utc = "unknown"

            with open(self.csv_path, "a", encoding="utf-8") as f:
                f.write(f"{frame_id},{timestamp_ns},{fecha_utc}\n")

            self.cola.task_done()

    def detener(self):
        """Detiene el hilo de escritura y espera a que termine."""
        self.corriendo = False
        if self.hilo.is_alive():
            self.hilo.join(timeout=15.0)


# ═══════════════════════════════════════════════════════════════════════════
# DETECCIÓN DE DEPENDENCIAS (Linux-native)
# ═══════════════════════════════════════════════════════════════════════════

def buscar_ffmpeg():
    """
    Busca FFmpeg en el sistema. Prioriza el binario del sistema operativo
    instalado vía apt. Si no existe, intenta imageio-ffmpeg como fallback.
    Retorna (ruta, origen) o (None, None).
    """
    # Opción 1: FFmpeg del sistema (instalado con apt)
    ruta_sistema = shutil.which("ffmpeg")
    if ruta_sistema:
        try:
            res = subprocess.run(
                [ruta_sistema, "-version"],
                capture_output=True, text=True, timeout=5
            )
            if res.returncode == 0:
                version_linea = res.stdout.split("\n")[0] if res.stdout else "desconocida"
                return ruta_sistema, f"sistema ({version_linea})"
        except Exception:
            pass

    # Opción 2: Fallback a imageio-ffmpeg (si está instalado)
    try:
        import imageio_ffmpeg
        ruta_iio = imageio_ffmpeg.get_ffmpeg_exe()
        res = subprocess.run(
            [ruta_iio, "-version"],
            capture_output=True, text=True, timeout=5
        )
        if res.returncode == 0:
            return ruta_iio, "imageio-ffmpeg (fallback)"
    except Exception:
        pass

    return None, None


def verificar_pyrealsense2():
    """
    Intenta importar pyrealsense2 y retorna el módulo o None.
    Soporta tanto la instalación vía pip como la compilación desde fuente.
    """
    try:
        import pyrealsense2 as rs
        return rs
    except ImportError:
        return None


def verificar_opencv():
    """Intenta importar OpenCV y retorna el módulo o None."""
    try:
        import cv2
        return cv2
    except ImportError:
        return None


def verificar_numpy():
    """Intenta importar numpy y retorna el módulo o None."""
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
    rutas_udev = [
        "/etc/udev/rules.d/99-realsense-libusb.rules",
        "/etc/udev/rules.d/99-realsense-d4xx.rules",
    ]
    for ruta in rutas_udev:
        if os.path.isfile(ruta):
            return True, ruta
    return False, None


def verificar_dispositivos_usb():
    """Busca dispositivos Intel RealSense en el bus USB."""
    try:
        res = subprocess.run(
            ["lsusb"], capture_output=True, text=True, timeout=5
        )
        lineas_realsense = [
            l.strip() for l in res.stdout.split("\n")
            if "8086" in l and ("0b07" in l.lower() or "0ad" in l.lower()
                                or "realsense" in l.lower() or "0b3a" in l.lower()
                                or "0b5c" in l.lower() or "0b64" in l.lower())
        ]
        if not lineas_realsense:
            # Búsqueda más amplia por Intel Corp.
            lineas_realsense = [
                l.strip() for l in res.stdout.split("\n")
                if "Intel Corp" in l and ("RealSense" in l or "D4" in l or "D5" in l)
            ]
        return lineas_realsense
    except Exception:
        return []


def verificar_puerto_disponible(puerto):
    """Verifica si un puerto TCP está disponible."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        result = s.connect_ex(("127.0.0.1", puerto))
        s.close()
        return result != 0  # True si está libre
    except Exception:
        return True


def ejecutar_diagnostico():
    """Ejecuta un diagnóstico completo del sistema para RTSP con RealSense."""
    print("\n" + "═" * 60)
    print("  DIAGNÓSTICO DEL SISTEMA — Emisor RTSP Ubuntu")
    print("═" * 60)

    # 1. FFmpeg
    ruta_ff, origen_ff = buscar_ffmpeg()
    if ruta_ff:
        print(f"\n  [✓] FFmpeg encontrado: {ruta_ff}")
        print(f"      Origen: {origen_ff}")
    else:
        print("\n  [✗] FFmpeg NO encontrado")
        print("      Instalar con: sudo apt install ffmpeg")

    # 2. Python / OpenCV / numpy
    cv2 = verificar_opencv()
    _np = verificar_numpy()
    rs = verificar_pyrealsense2()

    print(f"  [{'✓' if cv2 else '✗'}] OpenCV: {'v' + cv2.__version__ if cv2 else 'NO instalado'}")
    print(f"  [{'✓' if _np else '✗'}] NumPy: {'v' + _np.__version__ if _np else 'NO instalado'}")
    print(f"  [{'✓' if rs else '✗'}] pyrealsense2: {'disponible' if rs else 'NO instalado'}")

    if not rs:
        print("      Para instalar pyrealsense2 en Ubuntu:")
        print("        Opción A (pip): pip install pyrealsense2")
        print("        Opción B (repo Intel):")
        print("          sudo mkdir -p /etc/apt/keyrings")
        print("          curl -sSf https://librealsense.intel.com/Debian/librealsense.pgp \\")
        print("            | sudo tee /etc/apt/keyrings/librealsense.pgp > /dev/null")
        print("          echo \"deb [signed-by=/etc/apt/keyrings/librealsense.pgp] \\")
        print("            https://librealsense.intel.com/Debian/apt-repo `lsb_release -cs` main\" \\")
        print("            | sudo tee /etc/apt/sources.list.d/librealsense.list")
        print("          sudo apt update")
        print("          sudo apt install librealsense2-dkms librealsense2-utils")
        print("          pip install pyrealsense2")

    # 3. Reglas udev
    udev_ok, udev_ruta = verificar_reglas_udev()
    if udev_ok:
        print(f"  [✓] Reglas udev: {udev_ruta}")
    else:
        print("  [⚠] Reglas udev de RealSense no encontradas")
        print("      Esto puede causar errores de permisos USB.")
        print("      Instalar con:")
        print("        wget https://raw.githubusercontent.com/IntelRealSense/librealsense/master/config/99-realsense-libusb.rules")
        print("        sudo cp 99-realsense-libusb.rules /etc/udev/rules.d/")
        print("        sudo udevadm control --reload-rules && sudo udevadm trigger")

    # 4. Dispositivos USB
    dispositivos = verificar_dispositivos_usb()
    if dispositivos:
        print(f"  [✓] Dispositivo(s) RealSense en USB: {len(dispositivos)}")
        for d in dispositivos:
            print(f"      → {d}")
    else:
        print("  [⚠] No se detectaron dispositivos RealSense en el bus USB")
        print("      Verifica que la cámara esté conectada a un puerto USB 3.0")

    # 5. Puertos RTSP (4 puertos consecutivos)
    puerto_base = PUERTO_RTSP_DEFECTO
    for i, nombre in enumerate(["color", "depth", "ir1", "ir2"]):
        p = puerto_base + i
        libre = verificar_puerto_disponible(p)
        if libre:
            print(f"  [✓] Puerto {p} ({nombre}) disponible")
        else:
            print(f"  [✗] Puerto {p} ({nombre}) EN USO")
            print(f"      Usa --puerto OTRO_PUERTO o mata el proceso que lo ocupa")

    print("\n" + "═" * 60 + "\n")


# ═══════════════════════════════════════════════════════════════════════════
# CÁMARA REALSENSE
# ═══════════════════════════════════════════════════════════════════════════

def listar_camaras(rs):
    """Lista cámaras RealSense conectadas."""
    ctx = rs.context()
    dispositivos = ctx.query_devices()

    if len(dispositivos) == 0:
        print("\n  ✗ No se detectaron cámaras Intel RealSense.")
        print("    1. Verifica que la cámara esté conectada a USB 3.0 (puerto azul)")
        print("    2. Ejecuta: python3 emisor_ubuntu.py --diagnostico")
        return []

    print(f"\n  Cámaras Intel RealSense detectadas: {len(dispositivos)}")
    print("  " + "─" * 60)
    print(f"  {'Idx':<5} {'Nombre':<30} {'Nº Serie':<15} {'USB'}")
    print("  " + "─" * 60)

    lista = []
    for i, dev in enumerate(dispositivos):
        nombre = dev.get_info(rs.camera_info.name)
        serie = dev.get_info(rs.camera_info.serial_number)
        try:
            usb_tipo = dev.get_info(rs.camera_info.usb_type_descriptor)
        except Exception:
            usb_tipo = "?"
        print(f"  {i:<5} {nombre:<30} {serie:<15} {usb_tipo}")
        lista.append({"indice": i, "nombre": nombre, "serie": serie, "usb": usb_tipo})

    print("  " + "─" * 60)
    return lista


def obtener_ip_local():
    """Obtiene la IP local de la máquina en la red LAN."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ═══════════════════════════════════════════════════════════════════════════
# PROCESOS FFMPEG — RTSP SERVER DIRECTO
# ═══════════════════════════════════════════════════════════════════════════

def crear_ffmpeg(ruta_ffmpeg, puerto_listen, ancho, alto, pix_fmt, fps, bitrate_kbps):
    """
    Lanza un subproceso FFmpeg como servidor RTSP directo (sin MediaMTX).

    FFmpeg abre un socket TCP en el puerto indicado y espera conexiones
    RTSP entrantes. El receptor se conecta directamente a este puerto.
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
        "-i", "-",                          # Leer desde stdin (pipe)
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
                   bitrate_kbps=2000,
                   rango_grabacion=None, dir_salida=None):
    """
    Emisor RTSP v4: captura 4 canales de RealSense D435, inyecta metadatos
    LSB (Frame ID + Timestamp) y los publica como streams RTSP independientes
    usando FFmpeg como servidor RTSP directo (sin MediaMTX).

    Opcionalmente graba un rango de fotogramas sin pérdidas.
    """
    global _cerrando
    procesos_ff = {}         # {"color": Popen, "depth": Popen, ...}
    pipeline = None
    pipeline_activo = False

    # Puertos: base para color, base+1 depth, base+2 ir1, base+3 ir2
    puertos = {
        "color": puerto,
        "depth": puerto + 1,
        "ir1":   puerto + 2,
        "ir2":   puerto + 3,
    }

    # ─── Registrar señales POSIX para cierre limpio ─────────────────────
    # SIGINT se recibe con Ctrl+C. SIGTERM se recibe al hacer kill <PID>.
    # Ambas activan el flag _cerrando para que el bucle principal salga
    # de forma ordenada, liberando todos los recursos.
    def manejar_senal(signum, frame):
        global _cerrando
        nombre = signal.Signals(signum).name
        print(f"\n  ⏹ Señal {nombre} recibida. Cerrando ...")
        _cerrando = True

    signal.signal(signal.SIGINT, manejar_senal)
    signal.signal(signal.SIGTERM, manejar_senal)

    try:
        print("\n" + "═" * 62)
        print("  EMISOR RTSP — Intel RealSense D435 · Ubuntu Nativo (v4)")
        print("  Esteganografía LSB activa · FFmpeg RTSP Server directo")
        print("═" * 62)

        # ──────────────────────────────────────────────────────────────
        # PASO 1: Verificar FFmpeg
        # ──────────────────────────────────────────────────────────────
        print("\n[1/5] Buscando FFmpeg ...")
        ruta_ffmpeg, origen = buscar_ffmpeg()
        if ruta_ffmpeg is None:
            print("  ✗ FFmpeg no encontrado en el sistema.")
            print("    Instálalo con:  sudo apt install ffmpeg")
            sys.exit(1)
        print(f"  ✓ FFmpeg: {ruta_ffmpeg}")
        print(f"    Origen: {origen}")

        # ──────────────────────────────────────────────────────────────
        # PASO 2: Verificar dependencias Python
        # ──────────────────────────────────────────────────────────────
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
            print("  ✗ numpy no instalado.")
            print("    Instalar: pip install numpy")
            sys.exit(1)
        print(f"  ✓ NumPy {_np.__version__}")

        rs = verificar_pyrealsense2()
        if rs is None:
            print("  ✗ pyrealsense2 no disponible.")
            print("    Opción A:  pip install pyrealsense2")
            print("    Opción B:  Instalar desde el repo oficial de Intel")
            print("    Ejecuta:   python3 emisor_ubuntu.py --diagnostico")
            sys.exit(1)
        print(f"  ✓ pyrealsense2 disponible")

        # Verificar permisos USB / udev
        udev_ok, udev_ruta = verificar_reglas_udev()
        if not udev_ok:
            print("  ⚠ Reglas udev de RealSense no encontradas.")
            print("    Si la cámara no abre, ejecuta:")
            print("      wget https://raw.githubusercontent.com/IntelRealSense/librealsense/master/config/99-realsense-libusb.rules")
            print("      sudo cp 99-realsense-libusb.rules /etc/udev/rules.d/")
            print("      sudo udevadm control --reload-rules && sudo udevadm trigger")
        else:
            print(f"  ✓ Reglas udev: {udev_ruta}")

        # ──────────────────────────────────────────────────────────────
        # PASO 3: Abrir la cámara RealSense D435
        # ──────────────────────────────────────────────────────────────
        print(f"\n[3/5] Abriendo cámara Intel RealSense (índice {indice_camara}) ...")

        ctx = rs.context()
        dispositivos = ctx.query_devices()

        if len(dispositivos) == 0:
            print("  ✗ No se detectaron cámaras RealSense.")
            print("    Ejecuta: python3 emisor_ubuntu.py --diagnostico")
            sys.exit(1)

        if indice_camara >= len(dispositivos):
            print(f"  ✗ Índice {indice_camara} fuera de rango ({len(dispositivos)} cámara(s) disponible(s))")
            sys.exit(1)

        serial = dispositivos[indice_camara].get_info(rs.camera_info.serial_number)
        nombre_cam = dispositivos[indice_camara].get_info(rs.camera_info.name)
        print(f"  → Dispositivo: {nombre_cam} (S/N: {serial})")

        pipeline = rs.pipeline()
        config = rs.config()
        config.enable_device(serial)

        # Configurar streams a resolución nativa de la D435
        config.enable_stream(rs.stream.color, 1920, 1080, rs.format.bgr8, 30)
        config.enable_stream(rs.stream.infrared, 1, 1280, 720, rs.format.y8, 30)
        config.enable_stream(rs.stream.infrared, 2, 1280, 720, rs.format.y8, 30)
        config.enable_stream(rs.stream.depth, 1280, 720, rs.format.z16, 30)

        print("  → Iniciando pipeline ...")
        try:
            pipeline.start(config)
            pipeline_activo = True
        except RuntimeError as e:
            print(f"  ✗ Error al iniciar la cámara: {e}")
            print("    • Verifica que esté en un puerto USB 3.0 (azul)")
            print("    • Cierra cualquier otro programa que use la cámara")
            sys.exit(1)

        print(f"  ✓ Pipeline iniciado (Color 1920×1080, Depth/IR 1280×720 @ 30fps)")

        # ──────────────────────────────────────────────────────────────
        # PASO 4: Lanzar 4 FFmpeg como servidores RTSP
        # ──────────────────────────────────────────────────────────────
        print(f"\n[4/5] Iniciando 4 servidores RTSP (FFmpeg directo) ...")

        # Verificar que los 4 puertos estén libres
        for nombre, p in puertos.items():
            if not verificar_puerto_disponible(p):
                print(f"  ✗ Puerto {p} ({nombre}) ya está en uso.")
                print(f"    Usa --puerto OTRO o mata el proceso que lo ocupa:")
                print(f"    sudo lsof -i :{p}")
                sys.exit(1)

        # Distribución proporcional del bitrate entre los 4 canales
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

        # ──────────────────────────────────────────────────────────────
        # PASO 5: Grabación (desactivada en emisor)
        # ──────────────────────────────────────────────────────────────
        print(f"\n[5/5] Grabación local MKV en emisor: desactivada (se realiza en el receptor)")

        # ─── Paso 5.5: Iniciar grabación de rango de frames sin pérdidas ───
        grabador_rango = None
        if rango_grabacion is not None:
            try:
                frame_inicio, frame_fin = rango_grabacion
                if dir_salida is None:
                    dir_salida = f"rango_grabacion_{frame_inicio}_{frame_fin}"
                dir_salida_abs = os.path.abspath(dir_salida)
                print(f"\n[5.5] Preparando grabación de rango de frames sin pérdidas ...")
                print(f"  → Rango: {frame_inicio} a {frame_fin}")
                print(f"  → Directorio de salida: {dir_salida_abs}")
                grabador_rango = GrabadorRango(dir_salida_abs, frame_inicio, frame_fin)
            except Exception as e:
                print(f"  ⚠ No se pudo iniciar el grabador de rango sin pérdidas: {e}")

        # ──────────────────────────────────────────────────────────────
        # Banner final con URLs de conexión
        # ──────────────────────────────────────────────────────────────
        ip_local = obtener_ip_local()

        print("\n" + "═" * 62)
        print("  ✓ TRANSMISIÓN ACTIVA — 4 Canales RTSP + LSB")
        print("─" * 62)
        print(f"  Color (RGB):     rtsp://{ip_local}:{puertos['color']}/stream")
        print(f"  Profundidad:     rtsp://{ip_local}:{puertos['depth']}/stream")
        print(f"  Infrarrojo 1:    rtsp://{ip_local}:{puertos['ir1']}/stream")
        print(f"  Infrarrojo 2:    rtsp://{ip_local}:{puertos['ir2']}/stream")
        print("─" * 62)
        print(f"  LSB:  128 bits × {BITS_POR_BLOQUE}px/bit = {PIXELES_LSB}px en fila 0")
        if grabador_rango is not None:
            print(f"  🔴 GRABANDO RANGO [{frame_inicio} - {frame_fin}] → {os.path.abspath(dir_salida)}")
        print("─" * 62)
        print(f"  Receptor:  python3 receptor_ubuntu.py {ip_local}")
        print(f"  VLC:       vlc rtsp://{ip_local}:{puertos['color']}/stream")
        print("═" * 62)
        print("\n  Presiona Ctrl+C para detener.\n")

        # ─── Bucle principal de captura y transmisión ───────────────────
        # El Frame ID inicia en 1 (no 0) según requerimiento.
        frame_id = 1
        t_inicio = time.time()

        while not _cerrando:
            # ── Capturar frameset síncrono de la cámara ─────────────────
            # wait_for_frames() bloquea hasta que la cámara entrega un set
            # completo de frames de todos los streams configurados.
            try:
                frameset = pipeline.wait_for_frames(timeout_ms=5000)
            except RuntimeError:
                print("  ⚠ Timeout esperando frames de la cámara ...")
                continue

            # Capturar timestamp del sistema INMEDIATAMENTE después de
            # recibir el frameset. Este valor se inyectará en los 4 canales
            # vía LSB para que el receptor pueda auditar la sincronía.
            timestamp_ns = time.time_ns()

            # ── Extraer frames individuales del frameset ─────────────────
            fc = frameset.get_color_frame()
            fd = frameset.get_depth_frame()
            fi1 = frameset.get_infrared_frame(1)
            fi2 = frameset.get_infrared_frame(2)

            # Si algún canal no produjo frame, saltar esta iteración
            if not fc or not fd or not fi1 or not fi2:
                continue

            # ── Convertir a arrays NumPy ────────────────────────────────
            # np.array() crea una COPIA escribible del buffer del SDK.
            # Necesitamos copias porque inyectar_lsb() modifica in-place.
            color_img = np.array(fc.get_data())          # 1920×1080 BGR
            depth_raw = np.asanyarray(fd.get_data())      # 1280×720 Z16 (lectura para rango)
            ir1_img = np.array(fi1.get_data())            # 1280×720 gray
            ir2_img = np.array(fi2.get_data())            # 1280×720 gray

            # ── Procesar profundidad → heatmap JET ──────────────────────
            # Convertir datos de profundidad Z16 (0..65535 mm) a un mapa
            # de calor visual BGR usando la paleta JET de OpenCV.
            # Se limita a 4000mm (4 metros) para maximizar el contraste.
            depth_clipped = np.clip(depth_raw, 0, 4000)
            depth_8bit = (depth_clipped * (255.0 / 4000.0)).astype(np.uint8)
            depth_color = cv2.applyColorMap(depth_8bit, cv2.COLORMAP_JET)
            depth_color[depth_raw == 0] = [0, 0, 0]  # Negro donde no hay dato

            # ── Inyectar esteganografía LSB en los 4 frames ────────────
            # Se inyecta el MISMO frame_id y timestamp_ns en los 4 canales
            # para que en la extracción se pueda verificar la sincronía.
            inyectar_lsb(color_img, frame_id, timestamp_ns)
            inyectar_lsb(depth_color, frame_id, timestamp_ns)
            inyectar_lsb(ir1_img, frame_id, timestamp_ns)
            inyectar_lsb(ir2_img, frame_id, timestamp_ns)

            # ── Enviar a los 4 FFmpeg RTSP ──────────────────────────────
            try:
                procesos_ff["color"].stdin.write(color_img.tobytes())
                procesos_ff["depth"].stdin.write(depth_color.tobytes())
                procesos_ff["ir1"].stdin.write(ir1_img.tobytes())
                procesos_ff["ir2"].stdin.write(ir2_img.tobytes())
            except (BrokenPipeError, OSError) as e:
                print(f"  ✗ Error escribiendo a FFmpeg RTSP: {e}")
                break



            # ── Enviar a grabación de rango sin pérdidas ────────────────
            if grabador_rango is not None:
                grabador_rango.agregar_frame(
                    frame_id, color_img, depth_raw,
                    ir1_img, ir2_img, timestamp_ns
                )
                if frame_id >= grabador_rango.fin:
                    print(f"\n  ✓ Rango de fotogramas finalizado ({grabador_rango.inicio} a {grabador_rango.fin}). Finalizando escrituras...")
                    grabador_rango.detener()
                    print("  ✓ Rango guardado con éxito.")
                    grabador_rango = None

            # ── Avanzar el contador secuencial de frames ────────────────
            frame_id += 1

            # ── Log de estado cada 150 frames (~5 segundos a 30fps) ─────
            if (frame_id - 1) % 150 == 0:
                dt = time.time() - t_inicio
                fps_actual = (frame_id - 1) / dt if dt > 0 else 0
                ts_str = time.strftime("%H:%M:%S", time.localtime(timestamp_ns / 1e9))
                ts_ms = int((timestamp_ns % 1_000_000_000) / 1_000_000)
                print(f"  📹 FID: {frame_id - 1} | TS: {ts_str}.{ts_ms:03d} | "
                      f"FPS: {fps_actual:.1f} | Tiempo: {dt:.0f}s")

            # ── Verificar que los FFmpeg RTSP sigan vivos ───────────────
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
        # ── Liberación ordenada de todos los recursos ───────────────────
        print("\n  Liberando recursos ...")

        # Detener grabador de rango si sigue activo
        if 'grabador_rango' in locals() and grabador_rango is not None:
            print("\n  Deteniendo grabador de rango sin pérdidas...")
            grabador_rango.detener()
            print("  ✓ Grabador de rango finalizado.")

        # Detener pipeline de la cámara RealSense
        if pipeline_activo and pipeline:
            try:
                pipeline.stop()
                print("  ✓ Pipeline RealSense detenido")
            except Exception:
                pass

        # ── Cerrar los 4 FFmpeg RTSP ────────────────────────────────────
        for nombre, proc in procesos_ff.items():
            if proc and proc.stdin:
                try:
                    proc.stdin.close()
                except Exception:
                    pass

        for nombre, proc in procesos_ff.items():
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                    print(f"  ✓ FFmpeg ({nombre}) detenido")
                except Exception:
                    proc.kill()
                    print(f"  ✓ FFmpeg ({nombre}) forzado")

        # ── Resumen final ───────────────────────────────────────────────
        total_frames = frame_id - 1  # Restar 1 porque frame_id se incrementó antes de salir
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
        description="Emisor RTSP v4 para Intel RealSense D435 — Ubuntu/Linux nativo con LSB (FFmpeg RTSP Server directo).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  python3 emisor_ubuntu.py                         # Cámara 0, puerto base 8554
  python3 emisor_ubuntu.py --cam 1                 # Segunda cámara
  python3 emisor_ubuntu.py --puerto 9554           # Puerto base alternativo
  python3 emisor_ubuntu.py --calidad 4000          # Mayor calidad
  python3 emisor_ubuntu.py --listar-camaras        # Ver cámaras conectadas
  python3 emisor_ubuntu.py --diagnostico           # Diagnóstico del sistema
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
    parser.add_argument("--grabar-rango", type=int, nargs=2, metavar=("INICIO", "FIN"), default=None,
                        help="Grabar un rango de fotogramas sin pérdidas (ej. --grabar-rango 100 500)")
    parser.add_argument("--dir-salida", type=str, default=None,
                        help="Directorio de salida para la grabación de rango sin pérdidas")
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
        dir_salida=args.dir_salida
    )
