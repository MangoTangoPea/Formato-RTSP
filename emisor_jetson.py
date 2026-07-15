#!/usr/bin/env python3
"""
Emisor RTSP para Intel RealSense D435 — Jetson Orin Nano (ARM64).

Adaptado para funcionar con USB 2.0 en la Jetson:
  - Todos los flujos a 1280×720 @ 15 FPS (reduce el tráfico USB a la mitad)
  - Esteganografía LSB con 8 píxeles por bit (1024 px en fila 0)
  - Codificación libx264 ultrafast optimizada para CPU ARM64

Publica 4 streams RTSP independientes:
  rtsp://<IP>:8554/color    — RGB 1280×720
  rtsp://<IP>:8554/depth    — Profundidad con heatmap JET 1280×720
  rtsp://<IP>:8554/ir1      — Infrarrojo izquierdo 1280×720
  rtsp://<IP>:8554/ir2      — Infrarrojo derecho 1280×720

Uso:
    python3 emisor_jetson.py [--puerto PUERTO] [--cam INDICE] [--calidad KBPS]
    python3 emisor_jetson.py --listar-camaras
    python3 emisor_jetson.py --diagnostico
    python3 emisor_jetson.py --grabar-rango 100 500
"""

import subprocess
import sys
import os
import signal
import time
import argparse
import tarfile
import stat
import socket
import shutil
import urllib.request
import glob
import struct
import queue
import threading
import datetime

# ─── Importar numpy (se necesita para la esteganografía LSB) ────────────
try:
    import numpy as np
except ImportError:
    np = None


# ═════════════════════════════════════════════════════════════════════════
# CONSTANTES GLOBALES
# ═════════════════════════════════════════════════════════════════════════

PUERTO_RTSP_DEFECTO = 8554
DIR_BASE = os.path.dirname(os.path.abspath(__file__))
DIR_MEDIAMTX = os.path.join(DIR_BASE, "mediamtx_linux")
MEDIAMTX_VERSION = "v1.12.2"

# ─── Resolución y FPS adaptados para USB 2.0 en Jetson ─────────────────
# La D435 en USB 2.0 no aguanta 1920×1080 @ 30fps sin timeouts.
# A 1280×720 @ 15fps el tráfico USB baja a la mitad y funciona estable.
# El ancho de 1280 es suficiente para los 1024 píxeles de la esteganografía.
ANCHO_CAPTURA = 1280
ALTO_CAPTURA = 720
FPS_CAPTURA = 15

# ─── Flag para cierre limpio ───────────────────────────────────────────
# Se pone en True cuando el usuario presiona Ctrl+C o llega SIGTERM.
_cerrando = False


# ═════════════════════════════════════════════════════════════════════════
# ESTEGANOGRAFÍA LSB (Least Significant Bit)
# ═════════════════════════════════════════════════════════════════════════
#
# ¿Qué hace?
#   Esconde 128 bits de datos (Frame ID + Timestamp) en la primera fila
#   de cada imagen, modificando solo el bit menos significativo de cada
#   píxel. El cambio es de ±1 en un rango de 0..255: invisible al ojo.
#
# ¿Cómo funciona?
#   1. Se empaquetan 128 bits (64 bits de Frame ID + 64 bits de Timestamp)
#   2. Cada bit se repite 8 veces seguidas para crear redundancia
#   3. Total: 128 bits × 8 repeticiones = 1024 píxeles ocupados
#   4. Al extraer, se usa "votación por mayoría" en cada bloque de 8
#      para reconstruir el bit original (sobrevive a compresión H.264)
#
# ¿Por qué 1024 píxeles?
#   Porque necesitamos 128 bits × 8 px/bit = 1024 px.
#   Por eso la resolución mínima es 1280 de ancho (caben los 1024).

BITS_POR_BLOQUE = 8       # Cada bit se repite 8 veces para redundancia
TOTAL_BITS = 128           # 64 bits de Frame ID + 64 bits de Timestamp
PIXELES_LSB = TOTAL_BITS * BITS_POR_BLOQUE  # = 1024 píxeles en fila 0


def inyectar_lsb(frame, frame_id, timestamp_ns):
    """
    Esconde el Frame ID y el Timestamp en la primera fila de la imagen.

    Funciona con imágenes a color (BGR) y en blanco y negro (grayscale).
    En color solo toca el canal Azul para minimizar el impacto visual.

    Modifica la imagen directamente (in-place) y la devuelve.
    """
    ancho = frame.shape[1]

    # Si la imagen es más angosta que 1024px, no se puede inyectar
    if ancho < PIXELES_LSB:
        return frame

    # Empaquetar Frame ID y Timestamp como 16 bytes (128 bits)
    datos = struct.pack('>QQ',
                        frame_id & 0xFFFFFFFFFFFFFFFF,
                        timestamp_ns & 0xFFFFFFFFFFFFFFFF)

    # Convertir los 16 bytes a 128 bits individuales y repetir cada uno 8 veces
    bits_arr = np.unpackbits(np.frombuffer(datos, dtype=np.uint8))
    mascara = np.repeat(bits_arr, BITS_POR_BLOQUE)

    # Obtener la primera fila de la imagen
    if frame.ndim == 3:
        # Imagen a color (BGR): solo modificamos el canal Azul (índice 0)
        fila = frame[0, :PIXELES_LSB, 0]
    else:
        # Imagen en blanco y negro: modificamos directamente
        fila = frame[0, :PIXELES_LSB]

    # Limpiar el bit menos significativo actual y poner el nuevo
    # AND 0xFE borra el último bit, OR con la máscara pone el valor nuevo
    fila[:] = (fila & np.uint8(0xFE)) | mascara.astype(fila.dtype)
    return frame


def extraer_lsb(frame):
    """
    Lee los 128 bits escondidos en la primera fila de la imagen.
    Usa votación por mayoría en cada bloque de 8 píxeles.

    Devuelve (frame_id, timestamp_ns) o (None, None) si no se puede leer.
    """
    ancho = frame.shape[1] if frame.ndim >= 2 else 0
    if ancho < PIXELES_LSB:
        return None, None

    # Leer la primera fila del canal correcto
    if frame.ndim == 3:
        fila = frame[0, :PIXELES_LSB, 0]   # Canal Azul
    else:
        fila = frame[0, :PIXELES_LSB]       # Grayscale

    # Extraer el bit menos significativo de cada píxel
    # Agrupar en bloques de 8 y aplicar votación por mayoría
    lsbs = (fila & np.uint8(1)).reshape(TOTAL_BITS, BITS_POR_BLOQUE)
    bits = (lsbs.sum(axis=1) > BITS_POR_BLOQUE // 2).astype(np.uint8)

    # Reconstruir los 16 bytes y desempaquetar
    datos = np.packbits(bits)
    frame_id, timestamp_ns = struct.unpack('>QQ', datos.tobytes())
    return frame_id, timestamp_ns


# ═════════════════════════════════════════════════════════════════════════
# GRABADOR DE RANGO SIN PÉRDIDAS
# ═════════════════════════════════════════════════════════════════════════
#
# Graba un rango de frames (ej: del 100 al 500) como imágenes PNG
# sin pérdidas, con profundidad en 16 bits nativos y un CSV de metadatos.
# La escritura a disco va en un hilo aparte para no trabar la transmisión.

class GrabadorRango:
    """
    Graba un rango específico de frames como PNG sin pérdidas.
    Guarda: color, depth (16 bits), ir1, ir2, y un CSV con metadatos.
    Todo en un hilo separado para no bloquear la transmisión.
    """
    def __init__(self, dir_salida, frame_inicio, frame_fin):
        self.dir_salida = dir_salida
        self.inicio = frame_inicio
        self.fin = frame_fin
        self.cola = queue.Queue()
        self.corriendo = True
        self.hilo = threading.Thread(target=self._bucle_guardado,
                                     name="GrabadorRango", daemon=True)

        # Crear carpetas para cada tipo de imagen
        for subdir in ["color", "depth", "ir1", "ir2"]:
            os.makedirs(os.path.join(self.dir_salida, subdir), exist_ok=True)

        # Crear archivo CSV con encabezado
        self.csv_path = os.path.join(self.dir_salida, "metadata.csv")
        with open(self.csv_path, "w", encoding="utf-8") as f:
            f.write("frame_id,timestamp_ns,timestamp_utc\n")

        self.hilo.start()

    def agregar_frame(self, frame_id, color, depth_raw, ir1, ir2, timestamp_ns):
        """Si el frame está en el rango pedido, lo manda a guardar."""
        if self.inicio <= frame_id <= self.fin:
            # Mandamos copias para evitar problemas con el hilo principal
            self.cola.put((frame_id, color.copy(), depth_raw.copy(),
                           ir1.copy(), ir2.copy(), timestamp_ns))

    def _bucle_guardado(self):
        """Hilo que va sacando frames de la cola y los guarda en disco."""
        while self.corriendo or not self.cola.empty():
            try:
                item = self.cola.get(timeout=0.2)
            except queue.Empty:
                continue

            frame_id, color, depth_raw, ir1, ir2, timestamp_ns = item
            filename = f"{frame_id:08d}.png"

            # Guardar cada imagen como PNG (sin pérdida)
            cv2.imwrite(os.path.join(self.dir_salida, "color", filename), color)
            cv2.imwrite(os.path.join(self.dir_salida, "depth", filename), depth_raw)
            cv2.imwrite(os.path.join(self.dir_salida, "ir1", filename), ir1)
            cv2.imwrite(os.path.join(self.dir_salida, "ir2", filename), ir2)

            # Registrar metadatos en el CSV
            try:
                dt_utc = datetime.datetime.fromtimestamp(
                    timestamp_ns / 1e9, datetime.timezone.utc)
                fecha_utc = dt_utc.isoformat()
            except Exception:
                fecha_utc = "unknown"

            with open(self.csv_path, "a", encoding="utf-8") as f:
                f.write(f"{frame_id},{timestamp_ns},{fecha_utc}\n")

            self.cola.task_done()

    def detener(self):
        """Para el hilo de escritura y espera a que termine."""
        self.corriendo = False
        if self.hilo.is_alive():
            self.hilo.join(timeout=15.0)


# ═════════════════════════════════════════════════════════════════════════
# DETECCIÓN DE DEPENDENCIAS
# ═════════════════════════════════════════════════════════════════════════

def detectar_arquitectura():
    """
    Detecta si estamos en x86_64 o ARM64 para descargar el MediaMTX correcto.
    En la Jetson esto devuelve 'aarch64' → 'arm64v8'.
    """
    try:
        resultado = subprocess.run(["uname", "-m"], capture_output=True, text=True)
        arch = resultado.stdout.strip().lower()
    except Exception:
        arch = "x86_64"

    mapa = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64v8",
        "arm64": "arm64v8",
        "armv7l": "armv7",
        "armhf": "armv7",
    }
    return mapa.get(arch, "amd64"), arch


def buscar_ffmpeg():
    """
    Busca FFmpeg en el sistema. Primero busca el de apt (del sistema),
    y si no lo encuentra, intenta imageio-ffmpeg como alternativa.
    Devuelve (ruta, origen) o (None, None).
    """
    # Opción 1: FFmpeg instalado con apt
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

    # Opción 2: imageio-ffmpeg (si está instalado como paquete Python)
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
    """Intenta cargar pyrealsense2 y devuelve el módulo o None."""
    try:
        import pyrealsense2 as rs
        return rs
    except ImportError:
        return None


def verificar_opencv():
    """Intenta cargar OpenCV y devuelve el módulo o None."""
    try:
        import cv2
        return cv2
    except ImportError:
        return None


def verificar_numpy():
    """Intenta cargar numpy y devuelve el módulo o None."""
    global np
    try:
        import numpy as _np
        np = _np
        return _np
    except ImportError:
        return None


# ═════════════════════════════════════════════════════════════════════════
# DIAGNÓSTICO DEL SISTEMA
# ═════════════════════════════════════════════════════════════════════════

def verificar_reglas_udev():
    """
    Verifica que las reglas udev de Intel RealSense estén instaladas.
    Sin estas reglas, la cámara no se puede abrir sin ser root.
    """
    rutas_udev = [
        "/etc/udev/rules.d/99-realsense-libusb.rules",
        "/etc/udev/rules.d/99-realsense-d4xx.rules",
    ]
    for ruta in rutas_udev:
        if os.path.isfile(ruta):
            return True, ruta
    return False, None


def verificar_autosuspend_usb():
    """
    Verifica si el autosuspend USB está desactivado.
    En la Jetson, si está activado, el kernel puede 'dormir' el puerto USB
    y la cámara pierde alimentación → timeout → crash.
    """
    ruta = "/sys/module/usbcore/parameters/autosuspend_delay_ms"
    try:
        with open(ruta, "r") as f:
            valor = f.read().strip()
        # -1 significa desactivado, cualquier positivo es peligroso
        if valor == "-1":
            return True, valor
        else:
            return False, valor
    except FileNotFoundError:
        return True, "N/A"  # Si no existe el archivo, no aplica


def verificar_dispositivos_usb():
    """Busca cámaras Intel RealSense en el bus USB con lsusb."""
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
            lineas_realsense = [
                l.strip() for l in res.stdout.split("\n")
                if "Intel Corp" in l and ("RealSense" in l or "D4" in l or "D5" in l)
            ]
        return lineas_realsense
    except Exception:
        return []


def verificar_puerto_disponible(puerto):
    """Verifica si un puerto TCP está libre (no lo está usando otro programa)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        result = s.connect_ex(("127.0.0.1", puerto))
        s.close()
        return result != 0  # True si está libre
    except Exception:
        return True


def ejecutar_diagnostico():
    """Ejecuta un chequeo completo del sistema y muestra los resultados."""
    print("\n" + "═" * 60)
    print("  DIAGNÓSTICO DEL SISTEMA — Emisor RTSP Jetson Orin Nano")
    print("═" * 60)

    # 1. Arquitectura
    arch_mtx, arch_raw = detectar_arquitectura()
    print(f"\n  [CPU] Arquitectura: {arch_raw} → MediaMTX: {arch_mtx}")
    if arch_raw in ("aarch64", "arm64"):
        print("  [✓] Plataforma ARM64 detectada (Jetson)")
    else:
        print("  [⚠] Esta no parece ser una Jetson (se esperaba aarch64)")

    # 2. FFmpeg
    ruta_ff, origen_ff = buscar_ffmpeg()
    if ruta_ff:
        print(f"  [✓] FFmpeg encontrado: {ruta_ff}")
        print(f"      Origen: {origen_ff}")
    else:
        print("  [✗] FFmpeg NO encontrado")
        print("      Instalar con: sudo apt install ffmpeg")

    # 3. Python / OpenCV / numpy
    cv2 = verificar_opencv()
    _np = verificar_numpy()
    rs = verificar_pyrealsense2()

    print(f"  [{'✓' if cv2 else '✗'}] OpenCV: {'v' + cv2.__version__ if cv2 else 'NO instalado'}")
    print(f"  [{'✓' if _np else '✗'}] NumPy: {'v' + _np.__version__ if _np else 'NO instalado'}")
    print(f"  [{'✓' if rs else '✗'}] pyrealsense2: {'disponible' if rs else 'NO instalado'}")

    if not rs:
        print("      En Jetson ARM64, pyrealsense2 se instala compilando desde fuentes.")
        print("      Ver GUIA_JETSON.md para las instrucciones paso a paso.")

    # 4. Reglas udev
    udev_ok, udev_ruta = verificar_reglas_udev()
    if udev_ok:
        print(f"  [✓] Reglas udev: {udev_ruta}")
    else:
        print("  [⚠] Reglas udev de RealSense no encontradas")
        print("      Sin ellas necesitas 'sudo' para acceder a la cámara.")
        print("      Ver GUIA_JETSON.md → sección 'Reglas udev'")

    # 5. Autosuspend USB (crítico en Jetson)
    autosuspend_ok, autosuspend_val = verificar_autosuspend_usb()
    if autosuspend_ok:
        print(f"  [✓] USB autosuspend desactivado ({autosuspend_val})")
    else:
        print(f"  [✗] USB autosuspend ACTIVADO (valor: {autosuspend_val}ms)")
        print("      ¡PELIGRO! El kernel puede suspender el puerto USB")
        print("      y la cámara perderá alimentación.")
        print("      Desactivar con:")
        print("        echo -1 | sudo tee /sys/module/usbcore/parameters/autosuspend_delay_ms")
        print("      Ver GUIA_JETSON.md para hacerlo permanente.")

    # 6. Dispositivos USB
    dispositivos = verificar_dispositivos_usb()
    if dispositivos:
        print(f"  [✓] Dispositivo(s) RealSense en USB: {len(dispositivos)}")
        for d in dispositivos:
            print(f"      → {d}")
    else:
        print("  [⚠] No se detectaron dispositivos RealSense en el bus USB")
        print("      Verifica que la cámara esté conectada.")

    # 7. Puerto RTSP
    puerto_libre = verificar_puerto_disponible(PUERTO_RTSP_DEFECTO)
    if puerto_libre:
        print(f"  [✓] Puerto {PUERTO_RTSP_DEFECTO} disponible")
    else:
        print(f"  [✗] Puerto {PUERTO_RTSP_DEFECTO} EN USO")
        print(f"      Usa --puerto OTRO_PUERTO o mata el proceso que lo ocupa")

    # 8. MediaMTX
    exe_mtx = os.path.join(DIR_MEDIAMTX, "mediamtx")
    if os.path.isfile(exe_mtx) and os.access(exe_mtx, os.X_OK):
        print(f"  [✓] MediaMTX instalado: {exe_mtx}")
    elif os.path.isfile(exe_mtx):
        print(f"  [⚠] MediaMTX existe pero sin permiso de ejecución: {exe_mtx}")
        print(f"      Ejecutar: chmod +x {exe_mtx}")
    else:
        print(f"  [i] MediaMTX no descargado aún (se descargará al ejecutar el emisor)")

    print("\n" + "═" * 60)
    print(f"  Configuración Jetson: {ANCHO_CAPTURA}×{ALTO_CAPTURA} @ {FPS_CAPTURA} FPS")
    print(f"  LSB: {TOTAL_BITS} bits × {BITS_POR_BLOQUE} px/bit = {PIXELES_LSB} px")
    print("═" * 60 + "\n")


# ═════════════════════════════════════════════════════════════════════════
# MEDIAMTX (servidor RTSP)
# ═════════════════════════════════════════════════════════════════════════

def descargar_mediamtx():
    """
    Descarga el servidor MediaMTX para la arquitectura actual.
    MediaMTX es un servidor RTSP ligero que recibe los streams de FFmpeg
    y los publica para que el receptor los consuma.
    """
    exe_path = os.path.join(DIR_MEDIAMTX, "mediamtx")

    if os.path.isfile(exe_path) and os.access(exe_path, os.X_OK):
        print(f"  ✓ MediaMTX ya instalado: {exe_path}")
        return exe_path

    arch_mtx, arch_raw = detectar_arquitectura()
    url = (
        f"https://github.com/bluenviron/mediamtx/releases/download/"
        f"{MEDIAMTX_VERSION}/mediamtx_{MEDIAMTX_VERSION}_linux_{arch_mtx}.tar.gz"
    )

    print(f"  ↓ Descargando MediaMTX {MEDIAMTX_VERSION} (linux/{arch_mtx}) ...")
    os.makedirs(DIR_MEDIAMTX, exist_ok=True)
    tar_path = os.path.join(DIR_MEDIAMTX, "mediamtx.tar.gz")

    try:
        urllib.request.urlretrieve(url, tar_path)
    except Exception as e:
        print(f"  ✗ Error al descargar MediaMTX: {e}")
        print(f"    URL: {url}")
        print(f"    Verifica tu conexión a internet.")
        sys.exit(1)

    print("  ↓ Extrayendo ...")
    try:
        with tarfile.open(tar_path, "r:gz") as tar:
            tar.extractall(DIR_MEDIAMTX)
    except tarfile.TarError as e:
        print(f"  ✗ Error al extraer: {e}")
        os.remove(tar_path)
        sys.exit(1)

    os.remove(tar_path)

    if not os.path.isfile(exe_path):
        print("  ✗ Binario 'mediamtx' no encontrado tras la extracción.")
        sys.exit(1)

    # Darle permiso de ejecución
    os.chmod(exe_path,
             os.stat(exe_path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    print(f"  ✓ MediaMTX instalado: {exe_path}")
    return exe_path


def iniciar_mediamtx(puerto):
    """Arranca el servidor MediaMTX en el puerto indicado."""
    exe_path = descargar_mediamtx()

    # Verificar que el puerto no esté ocupado
    if not verificar_puerto_disponible(puerto):
        print(f"  ✗ El puerto {puerto} ya está en uso.")
        print(f"    Usa --puerto OTRO o mata el proceso que lo ocupa:")
        print(f"    sudo lsof -i :{puerto}")
        sys.exit(1)

    entorno = os.environ.copy()
    entorno["MTX_RTSPADDRESS"] = f":{puerto}"

    print(f"  → Iniciando MediaMTX en :{puerto} ...")
    try:
        proceso = subprocess.Popen(
            [exe_path],
            cwd=DIR_MEDIAMTX,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=entorno
        )
    except PermissionError:
        print(f"  ✗ Sin permisos de ejecución.")
        print(f"    Ejecuta: chmod +x {exe_path}")
        sys.exit(1)
    except Exception as e:
        print(f"  ✗ Error al iniciar MediaMTX: {e}")
        sys.exit(1)

    # Esperar 2 segundos y verificar que no se haya muerto
    time.sleep(2)
    if proceso.poll() is not None:
        salida = proceso.stdout.read().decode(errors="replace")[:500]
        print(f"  ✗ MediaMTX terminó inesperadamente:")
        print(f"    {salida}")
        sys.exit(1)

    print(f"  ✓ MediaMTX corriendo (PID {proceso.pid})")
    return proceso


# ═════════════════════════════════════════════════════════════════════════
# CÁMARA REALSENSE
# ═════════════════════════════════════════════════════════════════════════

def listar_camaras(rs):
    """Muestra todas las cámaras RealSense conectadas con sus datos."""
    ctx = rs.context()
    dispositivos = ctx.query_devices()

    if len(dispositivos) == 0:
        print("\n  ✗ No se detectaron cámaras Intel RealSense.")
        print("    1. Verifica que la cámara esté conectada")
        print("    2. Ejecuta: python3 emisor_jetson.py --diagnostico")
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
    """Obtiene la IP de la máquina en la red local."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ═════════════════════════════════════════════════════════════════════════
# PROCESOS FFMPEG — CODIFICACIÓN Y ENVÍO RTSP
# ═════════════════════════════════════════════════════════════════════════

def crear_ffmpeg(ruta_ffmpeg, url_rtsp, ancho, alto, pix_fmt, fps, bitrate_kbps):
    """
    Lanza un proceso FFmpeg que lee frames crudos por stdin,
    los codifica en H.264 y los publica como stream RTSP.

    Cada canal (Color, Depth, IR1, IR2) tiene su propio FFmpeg.
    Usa libx264 con preset ultrafast para no cargar la CPU ARM64.
    """
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
        "-preset", "ultrafast",             # Mínima carga de CPU
        "-tune", "zerolatency",             # Optimizado para streaming en vivo
        "-b:v", f"{bitrate_kbps}k",
        "-maxrate", f"{bitrate_kbps}k",
        "-bufsize", f"{bitrate_kbps * 2}k",
        "-g", str(fps * 2),                 # Keyframe cada 2 segundos
        "-f", "rtsp",
        "-rtsp_transport", "tcp",
        url_rtsp,
    ]
    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


# ═════════════════════════════════════════════════════════════════════════
# FUNCIÓN PRINCIPAL DEL EMISOR
# ═════════════════════════════════════════════════════════════════════════

def iniciar_emisor(indice_camara=0, puerto=PUERTO_RTSP_DEFECTO,
                   bitrate_kbps=1500,
                   rango_grabacion=None, dir_salida=None):
    """
    Captura 4 canales de la RealSense D435 a 1280×720 @ 15 FPS,
    inyecta esteganografía LSB (Frame ID + Timestamp) en cada uno,
    y los publica como streams RTSP independientes.
    """
    global _cerrando
    proceso_mediamtx = None
    procesos_ff = {}         # {"color": Popen, "depth": Popen, ...}
    pipeline = None
    pipeline_activo = False

    # ─── Registrar señales para cierre limpio ───────────────────────────
    # Ctrl+C envía SIGINT, 'kill <PID>' envía SIGTERM.
    # Ambas activan el flag _cerrando para salir del bucle ordenadamente.
    def manejar_senal(signum, frame):
        global _cerrando
        nombre = signal.Signals(signum).name
        print(f"\n  ⏹ Señal {nombre} recibida. Cerrando ...")
        _cerrando = True

    signal.signal(signal.SIGINT, manejar_senal)
    signal.signal(signal.SIGTERM, manejar_senal)

    try:
        print("\n" + "═" * 62)
        print("  EMISOR RTSP — Intel RealSense D435 · Jetson Orin Nano")
        print(f"  Resolución: {ANCHO_CAPTURA}×{ALTO_CAPTURA} @ {FPS_CAPTURA} FPS · USB 2.0")
        print(f"  Esteganografía LSB: {PIXELES_LSB} px en fila 0")
        print("═" * 62)

        # ──────────────────────────────────────────────────────────────
        # PASO 1: Verificar FFmpeg
        # ──────────────────────────────────────────────────────────────
        print("\n[1/6] Buscando FFmpeg ...")
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
        print("\n[2/6] Verificando dependencias Python ...")

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
            print("    En Jetson ARM64, compilar desde fuentes.")
            print("    Ver GUIA_JETSON.md para instrucciones.")
            sys.exit(1)
        print(f"  ✓ pyrealsense2 disponible")

        # Verificar reglas udev
        udev_ok, udev_ruta = verificar_reglas_udev()
        if not udev_ok:
            print("  ⚠ Reglas udev de RealSense no encontradas.")
            print("    Si la cámara no abre, ver GUIA_JETSON.md → 'Reglas udev'")
        else:
            print(f"  ✓ Reglas udev: {udev_ruta}")

        # Verificar autosuspend USB (solo en Jetson)
        autosuspend_ok, autosuspend_val = verificar_autosuspend_usb()
        if not autosuspend_ok:
            print(f"  ⚠ USB autosuspend activado ({autosuspend_val}ms)")
            print("    Riesgo de timeout en la cámara. Desactivar con:")
            print("    echo -1 | sudo tee /sys/module/usbcore/parameters/autosuspend_delay_ms")

        # ──────────────────────────────────────────────────────────────
        # PASO 3: Iniciar MediaMTX
        # ──────────────────────────────────────────────────────────────
        print(f"\n[3/6] Preparando servidor RTSP (MediaMTX) ...")
        proceso_mediamtx = iniciar_mediamtx(puerto)

        # ──────────────────────────────────────────────────────────────
        # PASO 4: Abrir la cámara RealSense D435
        # ──────────────────────────────────────────────────────────────
        print(f"\n[4/6] Abriendo cámara Intel RealSense (índice {indice_camara}) ...")

        ctx = rs.context()
        dispositivos = ctx.query_devices()

        if len(dispositivos) == 0:
            print("  ✗ No se detectaron cámaras RealSense.")
            print("    Ejecuta: python3 emisor_jetson.py --diagnostico")
            sys.exit(1)

        if indice_camara >= len(dispositivos):
            print(f"  ✗ Índice {indice_camara} fuera de rango "
                  f"({len(dispositivos)} cámara(s) disponible(s))")
            sys.exit(1)

        serial = dispositivos[indice_camara].get_info(rs.camera_info.serial_number)
        nombre_cam = dispositivos[indice_camara].get_info(rs.camera_info.name)
        try:
            usb_tipo = dispositivos[indice_camara].get_info(
                rs.camera_info.usb_type_descriptor)
        except Exception:
            usb_tipo = "?"
        print(f"  → Dispositivo: {nombre_cam} (S/N: {serial}, USB: {usb_tipo})")

        pipeline = rs.pipeline()
        config = rs.config()
        config.enable_device(serial)

        # ─── Configurar los 4 flujos a 1280×720 @ 15 FPS ───────────────
        # Esta configuración está pensada para USB 2.0 en la Jetson:
        #   - 15 FPS en vez de 30 → reduce el tráfico USB a la mitad
        #   - 1280 px de ancho → suficiente para los 1024 px de LSB
        #   - Color a 720p en vez de 1080p → menos datos por el bus USB
        config.enable_stream(rs.stream.color, ANCHO_CAPTURA, ALTO_CAPTURA,
                             rs.format.bgr8, FPS_CAPTURA)
        config.enable_stream(rs.stream.infrared, 1, ANCHO_CAPTURA, ALTO_CAPTURA,
                             rs.format.y8, FPS_CAPTURA)
        config.enable_stream(rs.stream.infrared, 2, ANCHO_CAPTURA, ALTO_CAPTURA,
                             rs.format.y8, FPS_CAPTURA)
        config.enable_stream(rs.stream.depth, ANCHO_CAPTURA, ALTO_CAPTURA,
                             rs.format.z16, FPS_CAPTURA)

        print("  → Iniciando pipeline ...")
        try:
            pipeline.start(config)
            pipeline_activo = True
        except RuntimeError as e:
            print(f"  ✗ Error al iniciar la cámara: {e}")
            print("    • Verifica que la cámara esté bien conectada")
            print("    • Cierra cualquier otro programa que la use")
            print("    • Si el error persiste, desconecta y reconecta la cámara")
            print("    • Ejecuta: python3 emisor_jetson.py --diagnostico")
            sys.exit(1)

        print(f"  ✓ Pipeline iniciado "
              f"({ANCHO_CAPTURA}×{ALTO_CAPTURA} × 4 canales @ {FPS_CAPTURA} FPS)")

        # ──────────────────────────────────────────────────────────────
        # PASO 5: Lanzar 4 FFmpeg → RTSP
        # ──────────────────────────────────────────────────────────────
        print(f"\n[5/6] Iniciando transmisión de 4 canales RTSP ...")

        # Repartir el bitrate entre los 4 canales proporcionalmente
        bitrates = {
            "color": max(200, int(bitrate_kbps * 0.55)),
            "depth": max(200, int(bitrate_kbps * 0.25)),
            "ir1":   max(100, int(bitrate_kbps * 0.10)),
            "ir2":   max(100, int(bitrate_kbps * 0.10)),
        }

        # Los 4 canales van a 1280×720 (en el emisor original, color iba a 1920×1080)
        canales = {
            "color": {"ancho": ANCHO_CAPTURA, "alto": ALTO_CAPTURA,
                      "pix": "bgr24", "br": bitrates["color"]},
            "depth": {"ancho": ANCHO_CAPTURA, "alto": ALTO_CAPTURA,
                      "pix": "bgr24", "br": bitrates["depth"]},
            "ir1":   {"ancho": ANCHO_CAPTURA, "alto": ALTO_CAPTURA,
                      "pix": "gray",  "br": bitrates["ir1"]},
            "ir2":   {"ancho": ANCHO_CAPTURA, "alto": ALTO_CAPTURA,
                      "pix": "gray",  "br": bitrates["ir2"]},
        }

        for nombre, cfg in canales.items():
            url = f"rtsp://127.0.0.1:{puerto}/{nombre}"
            print(f"  → {nombre:<6} {cfg['ancho']}×{cfg['alto']} "
                  f"@ {cfg['br']}kbps → {url}")
            procesos_ff[nombre] = crear_ffmpeg(
                ruta_ffmpeg, url,
                cfg["ancho"], cfg["alto"], cfg["pix"],
                FPS_CAPTURA, cfg["br"]
            )

        time.sleep(1)

        # Verificar que todos los FFmpeg arrancaron bien
        for nombre, proc in procesos_ff.items():
            if proc.poll() is not None:
                stderr = proc.stderr.read().decode(errors="replace")[:300]
                print(f"  ✗ FFmpeg ({nombre}) falló al iniciar:")
                print(f"    {stderr}")
                sys.exit(1)

        # ──────────────────────────────────────────────────────────────
        # PASO 6: Grabación MKV (desactivada en el emisor)
        # ──────────────────────────────────────────────────────────────
        print(f"\n[6/6] Grabación local MKV en emisor: desactivada "
              f"(se realiza en el receptor)")

        # ─── Grabación de rango de frames sin pérdidas (opcional) ───
        grabador_rango = None
        if rango_grabacion is not None:
            try:
                frame_inicio, frame_fin = rango_grabacion
                if dir_salida is None:
                    dir_salida = f"rango_grabacion_{frame_inicio}_{frame_fin}"
                dir_salida_abs = os.path.abspath(dir_salida)
                print(f"\n[6.5] Preparando grabación de rango sin pérdidas ...")
                print(f"  → Rango: {frame_inicio} a {frame_fin}")
                print(f"  → Directorio de salida: {dir_salida_abs}")
                grabador_rango = GrabadorRango(dir_salida_abs, frame_inicio, frame_fin)
            except Exception as e:
                print(f"  ⚠ No se pudo iniciar el grabador de rango: {e}")

        # ──────────────────────────────────────────────────────────────
        # Banner con las URLs de conexión
        # ──────────────────────────────────────────────────────────────
        ip_local = obtener_ip_local()

        print("\n" + "═" * 62)
        print("  ✓ TRANSMISIÓN ACTIVA — 4 Canales RTSP + LSB")
        print("─" * 62)
        print(f"  Color (RGB):     rtsp://{ip_local}:{puerto}/color")
        print(f"  Profundidad:     rtsp://{ip_local}:{puerto}/depth")
        print(f"  Infrarrojo 1:    rtsp://{ip_local}:{puerto}/ir1")
        print(f"  Infrarrojo 2:    rtsp://{ip_local}:{puerto}/ir2")
        print("─" * 62)
        print(f"  Resolución: {ANCHO_CAPTURA}×{ALTO_CAPTURA} @ {FPS_CAPTURA} FPS")
        print(f"  LSB:  {TOTAL_BITS} bits × {BITS_POR_BLOQUE}px/bit "
              f"= {PIXELES_LSB}px en fila 0")
        if grabador_rango is not None:
            print(f"  🔴 GRABANDO RANGO [{frame_inicio} - {frame_fin}] "
                  f"→ {os.path.abspath(dir_salida)}")
        print("─" * 62)
        print(f"  Receptor:  python3 receptor_jetson.py {ip_local}")
        print(f"  VLC:       vlc rtsp://{ip_local}:{puerto}/color")
        print("═" * 62)
        print("\n  Presiona Ctrl+C para detener.\n")

        # ─── Bucle principal: capturar → inyectar LSB → enviar ──────
        # El Frame ID empieza en 1 (no en 0)
        frame_id = 1
        t_inicio = time.time()

        while not _cerrando:
            # Capturar un set completo de frames de la cámara
            try:
                frameset = pipeline.wait_for_frames(timeout_ms=5000)
            except RuntimeError:
                print("  ⚠ Timeout esperando frames de la cámara ...")
                continue

            # Marcar el momento exacto de la captura (para medir latencia)
            timestamp_ns = time.time_ns()

            # Extraer los 4 frames individuales
            fc = frameset.get_color_frame()
            fd = frameset.get_depth_frame()
            fi1 = frameset.get_infrared_frame(1)
            fi2 = frameset.get_infrared_frame(2)

            # Si algún canal no dio frame, saltar
            if not fc or not fd or not fi1 or not fi2:
                continue

            # Convertir a arrays NumPy (copias escribibles)
            color_img = np.array(fc.get_data())          # 1280×720 BGR
            depth_raw = np.asanyarray(fd.get_data())      # 1280×720 Z16
            ir1_img = np.array(fi1.get_data())            # 1280×720 gray
            ir2_img = np.array(fi2.get_data())            # 1280×720 gray

            # Convertir profundidad a mapa de calor visual (heatmap JET)
            # Se limita a 4 metros para maximizar el contraste de colores
            depth_clipped = np.clip(depth_raw, 0, 4000)
            depth_8bit = (depth_clipped * (255.0 / 4000.0)).astype(np.uint8)
            depth_color = cv2.applyColorMap(depth_8bit, cv2.COLORMAP_JET)
            depth_color[depth_raw == 0] = [0, 0, 0]  # Negro donde no hay dato

            # Inyectar esteganografía LSB en los 4 frames
            # El MISMO frame_id y timestamp van en los 4 canales
            # para que el receptor verifique que están sincronizados
            inyectar_lsb(color_img, frame_id, timestamp_ns)
            inyectar_lsb(depth_color, frame_id, timestamp_ns)
            inyectar_lsb(ir1_img, frame_id, timestamp_ns)
            inyectar_lsb(ir2_img, frame_id, timestamp_ns)

            # Enviar los frames a los 4 FFmpeg (que los publican por RTSP)
            try:
                procesos_ff["color"].stdin.write(color_img.tobytes())
                procesos_ff["depth"].stdin.write(depth_color.tobytes())
                procesos_ff["ir1"].stdin.write(ir1_img.tobytes())
                procesos_ff["ir2"].stdin.write(ir2_img.tobytes())
            except (BrokenPipeError, OSError) as e:
                print(f"  ✗ Error escribiendo a FFmpeg RTSP: {e}")
                break

            # Enviar a grabación de rango si está activa
            if grabador_rango is not None:
                grabador_rango.agregar_frame(
                    frame_id, color_img, depth_raw,
                    ir1_img, ir2_img, timestamp_ns
                )
                if frame_id >= grabador_rango.fin:
                    print(f"\n  ✓ Rango de fotogramas finalizado "
                          f"({grabador_rango.inicio} a {grabador_rango.fin}).")
                    grabador_rango.detener()
                    print("  ✓ Rango guardado con éxito.")
                    grabador_rango = None

            # Avanzar el contador de frames
            frame_id += 1

            # Log de estado cada ~5 segundos (75 frames @ 15fps)
            if (frame_id - 1) % 75 == 0:
                dt = time.time() - t_inicio
                fps_actual = (frame_id - 1) / dt if dt > 0 else 0
                ts_str = time.strftime("%H:%M:%S",
                                       time.localtime(timestamp_ns / 1e9))
                ts_ms = int((timestamp_ns % 1_000_000_000) / 1_000_000)
                print(f"  📹 FID: {frame_id - 1} | TS: {ts_str}.{ts_ms:03d} | "
                      f"FPS: {fps_actual:.1f} | Tiempo: {dt:.0f}s")

            # Verificar que los FFmpeg sigan vivos
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
        # ─── Liberar todo ordenadamente ─────────────────────────────────
        print("\n  Liberando recursos ...")

        # Detener grabador de rango si sigue activo
        if 'grabador_rango' in locals() and grabador_rango is not None:
            print("\n  Deteniendo grabador de rango ...")
            grabador_rango.detener()
            print("  ✓ Grabador de rango finalizado.")

        # Detener pipeline de la cámara
        if pipeline_activo and pipeline:
            try:
                pipeline.stop()
                print("  ✓ Pipeline RealSense detenido")
            except Exception:
                pass

        # Cerrar los 4 FFmpeg
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

        # Detener MediaMTX
        if proceso_mediamtx and proceso_mediamtx.poll() is None:
            try:
                proceso_mediamtx.terminate()
                proceso_mediamtx.wait(timeout=5)
                print("  ✓ MediaMTX detenido")
            except Exception:
                proceso_mediamtx.kill()
                print("  ✓ MediaMTX forzado")

        # Resumen final
        total_frames = frame_id - 1
        if total_frames > 0:
            dt_total = time.time() - t_inicio
            print(f"\n  Resumen: {total_frames} frames en {dt_total:.1f}s "
                  f"({total_frames / dt_total:.1f} FPS promedio)")

        print("\n  Emisor finalizado.\n")


# ═════════════════════════════════════════════════════════════════════════
# PUNTO DE ENTRADA
# ═════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Emisor RTSP para Intel RealSense D435 — Jetson Orin Nano (ARM64).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  python3 emisor_jetson.py                         # Cámara 0, puerto 8554
  python3 emisor_jetson.py --cam 1                 # Segunda cámara
  python3 emisor_jetson.py --puerto 9554           # Puerto alternativo
  python3 emisor_jetson.py --calidad 2000          # Mayor calidad
  python3 emisor_jetson.py --listar-camaras        # Ver cámaras conectadas
  python3 emisor_jetson.py --diagnostico           # Diagnóstico del sistema
  python3 emisor_jetson.py --grabar-rango 150 450  # Grabar rango sin pérdidas
        """
    )

    parser.add_argument("--puerto", type=int, default=PUERTO_RTSP_DEFECTO,
                        help=f"Puerto RTSP (defecto: {PUERTO_RTSP_DEFECTO})")
    parser.add_argument("--cam", type=int, default=0,
                        help="Índice de la cámara RealSense (defecto: 0)")
    parser.add_argument("--calidad", type=int, default=1500,
                        help="Bitrate total en kbps (defecto: 1500)")
    parser.add_argument("--grabar-rango", type=int, nargs=2,
                        metavar=("INICIO", "FIN"), default=None,
                        help="Grabar un rango de fotogramas sin pérdidas")
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
            print("    Ejecuta: python3 emisor_jetson.py --diagnostico")
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
