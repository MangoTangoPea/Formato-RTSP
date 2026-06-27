#!/usr/bin/env python3
"""
Emisor RTSP para Intel RealSense D435 — Ubuntu/Linux Nativo (v2).

Rediseñado desde cero para funcionar de forma nativa en Linux.
A diferencia de la v1 (calco de Windows), esta versión:
  - Usa FFmpeg del sistema (apt install ffmpeg) en lugar de imageio-ffmpeg
  - Valida permisos USB y reglas udev antes de abrir la cámara
  - Usa señales POSIX para un cierre limpio
  - Descarga MediaMTX con verificación robusta de arquitectura

Publica 4 streams RTSP independientes:
  rtsp://<IP>:8554/color    — RGB 1920x1080
  rtsp://<IP>:8554/depth    — Profundidad con heatmap JET 1280x720
  rtsp://<IP>:8554/ir1      — Infrarrojo izquierdo 1280x720
  rtsp://<IP>:8554/ir2      — Infrarrojo derecho 1280x720

Uso:
    python3 emisor_ubuntu.py [--puerto PUERTO] [--cam INDICE] [--calidad KBPS]
    python3 emisor_ubuntu.py --listar-camaras
    python3 emisor_ubuntu.py --diagnostico
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


# ═══════════════════════════════════════════════════════════════════════════
# CONSTANTES
# ═══════════════════════════════════════════════════════════════════════════

PUERTO_RTSP_DEFECTO = 8554
DIR_BASE = os.path.dirname(os.path.abspath(__file__))
DIR_MEDIAMTX = os.path.join(DIR_BASE, "mediamtx_linux")
MEDIAMTX_VERSION = "v1.12.2"

# Flag global para cierre limpio con señales POSIX
_cerrando = False


# ═══════════════════════════════════════════════════════════════════════════
# DETECCIÓN DE DEPENDENCIAS (Linux-native)
# ═══════════════════════════════════════════════════════════════════════════

def detectar_arquitectura():
    """Detecta la arquitectura del CPU para descargar el binario correcto de MediaMTX."""
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
                # Extraer versión de la primera línea
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
    try:
        import numpy as np
        return np
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

    # 1. Arquitectura
    arch_mtx, arch_raw = detectar_arquitectura()
    print(f"\n  [CPU] Arquitectura: {arch_raw} → MediaMTX: {arch_mtx}")

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
    np = verificar_numpy()
    rs = verificar_pyrealsense2()

    print(f"  [{'✓' if cv2 else '✗'}] OpenCV: {'v' + cv2.__version__ if cv2 else 'NO instalado'}")
    print(f"  [{'✓' if np else '✗'}] NumPy: {'v' + np.__version__ if np else 'NO instalado'}")
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

    # 4. Reglas udev
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

    # 5. Dispositivos USB
    dispositivos = verificar_dispositivos_usb()
    if dispositivos:
        print(f"  [✓] Dispositivo(s) RealSense en USB: {len(dispositivos)}")
        for d in dispositivos:
            print(f"      → {d}")
    else:
        print("  [⚠] No se detectaron dispositivos RealSense en el bus USB")
        print("      Verifica que la cámara esté conectada a un puerto USB 3.0")

    # 6. Puerto RTSP
    puerto_libre = verificar_puerto_disponible(PUERTO_RTSP_DEFECTO)
    if puerto_libre:
        print(f"  [✓] Puerto {PUERTO_RTSP_DEFECTO} disponible")
    else:
        print(f"  [✗] Puerto {PUERTO_RTSP_DEFECTO} EN USO")
        print(f"      Usa --puerto OTRO_PUERTO o mata el proceso que lo ocupa")

    # 7. MediaMTX
    exe_mtx = os.path.join(DIR_MEDIAMTX, "mediamtx")
    if os.path.isfile(exe_mtx) and os.access(exe_mtx, os.X_OK):
        print(f"  [✓] MediaMTX instalado: {exe_mtx}")
    elif os.path.isfile(exe_mtx):
        print(f"  [⚠] MediaMTX existe pero sin permiso de ejecución: {exe_mtx}")
        print(f"      Ejecutar: chmod +x {exe_mtx}")
    else:
        print(f"  [i] MediaMTX no descargado aún (se descargará al ejecutar el emisor)")

    print("\n" + "═" * 60 + "\n")


# ═══════════════════════════════════════════════════════════════════════════
# MEDIAMTX
# ═══════════════════════════════════════════════════════════════════════════

def descargar_mediamtx():
    """Descarga y extrae MediaMTX para Linux. Retorna ruta al ejecutable."""
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

    # Asegurar permisos de ejecución
    os.chmod(exe_path, os.stat(exe_path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    print(f"  ✓ MediaMTX instalado: {exe_path}")
    return exe_path


def iniciar_mediamtx(puerto):
    """Inicia MediaMTX y retorna el proceso."""
    exe_path = descargar_mediamtx()

    # Verificar que el puerto esté libre
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

    # Esperar a que arranque (verificar que no muera de inmediato)
    time.sleep(2)
    if proceso.poll() is not None:
        salida = proceso.stdout.read().decode(errors="replace")[:500]
        print(f"  ✗ MediaMTX terminó inesperadamente:")
        print(f"    {salida}")
        sys.exit(1)

    print(f"  ✓ MediaMTX corriendo (PID {proceso.pid})")
    return proceso


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
# PROCESOS FFMPEG
# ═══════════════════════════════════════════════════════════════════════════

def crear_ffmpeg(ruta_ffmpeg, url_rtsp, ancho, alto, pix_fmt, fps, bitrate_kbps):
    """Lanza un subproceso FFmpeg para codificar rawvideo → H.264 → RTSP."""
    cmd = [
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
        "-b:v", f"{bitrate_kbps}k",
        "-maxrate", f"{bitrate_kbps}k",
        "-bufsize", f"{bitrate_kbps * 2}k",
        "-g", str(fps * 2),
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


# ═══════════════════════════════════════════════════════════════════════════
# FUNCIÓN PRINCIPAL DEL EMISOR
# ═══════════════════════════════════════════════════════════════════════════

def iniciar_emisor(indice_camara=0, puerto=PUERTO_RTSP_DEFECTO, bitrate_kbps=2000):
    """
    Emisor RTSP v2: captura 4 canales de RealSense D435 y los publica
    como streams RTSP independientes a través de MediaMTX.
    """
    global _cerrando
    proceso_mediamtx = None
    procesos_ff = {}  # {"color": Popen, "depth": Popen, ...}
    pipeline = None
    pipeline_activo = False

    # ─── Registrar señales POSIX para cierre limpio ─────────────────────
    def manejar_senal(signum, frame):
        global _cerrando
        nombre = signal.Signals(signum).name
        print(f"\n  ⏹ Señal {nombre} recibida. Cerrando ...")
        _cerrando = True

    signal.signal(signal.SIGINT, manejar_senal)
    signal.signal(signal.SIGTERM, manejar_senal)

    try:
        print("\n" + "═" * 62)
        print("  EMISOR RTSP — Intel RealSense D435 · Ubuntu Nativo (v2)")
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

        np = verificar_numpy()
        if np is None:
            print("  ✗ numpy no instalado.")
            print("    Instalar: pip install numpy")
            sys.exit(1)
        print(f"  ✓ NumPy {np.__version__}")

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
        # PASO 3: Iniciar MediaMTX
        # ──────────────────────────────────────────────────────────────
        print(f"\n[3/5] Preparando servidor RTSP (MediaMTX) ...")
        proceso_mediamtx = iniciar_mediamtx(puerto)

        # ──────────────────────────────────────────────────────────────
        # PASO 4: Abrir la cámara RealSense D435
        # ──────────────────────────────────────────────────────────────
        print(f"\n[4/5] Abriendo cámara Intel RealSense (índice {indice_camara}) ...")

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

        # Configurar streams a resolución nativa
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
            print("    • Si estás en WSL, necesitas usbipd-win (ver README)")
            sys.exit(1)

        print(f"  ✓ Pipeline iniciado (Color 1920×1080, Depth/IR 1280×720 @ 30fps)")

        # ──────────────────────────────────────────────────────────────
        # PASO 5: Lanzar 4 FFmpeg → RTSP
        # ──────────────────────────────────────────────────────────────
        print(f"\n[5/5] Iniciando transmisión de 4 canales RTSP ...")

        # Distribución del bitrate
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
            url = f"rtsp://127.0.0.1:{puerto}/{nombre}"
            print(f"  → {nombre:<6} {cfg['ancho']}×{cfg['alto']} @ {cfg['br']}kbps → {url}")
            procesos_ff[nombre] = crear_ffmpeg(
                ruta_ffmpeg, url,
                cfg["ancho"], cfg["alto"], cfg["pix"], 30, cfg["br"]
            )

        time.sleep(1)

        # Verificar que todos arrancaron
        for nombre, proc in procesos_ff.items():
            if proc.poll() is not None:
                stderr = proc.stderr.read().decode(errors="replace")[:300]
                print(f"  ✗ FFmpeg ({nombre}) falló al iniciar:")
                print(f"    {stderr}")
                sys.exit(1)

        ip_local = obtener_ip_local()

        print("\n" + "═" * 62)
        print("  ✓ TRANSMISIÓN ACTIVA — 4 Canales RTSP")
        print("─" * 62)
        print(f"  Color (RGB):     rtsp://{ip_local}:{puerto}/color")
        print(f"  Profundidad:     rtsp://{ip_local}:{puerto}/depth")
        print(f"  Infrarrojo 1:    rtsp://{ip_local}:{puerto}/ir1")
        print(f"  Infrarrojo 2:    rtsp://{ip_local}:{puerto}/ir2")
        print("─" * 62)
        print(f"  Receptor:  python3 receptor_ubuntu.py {ip_local}")
        print(f"  VLC:       vlc rtsp://{ip_local}:{puerto}/color")
        print("═" * 62)
        print("\n  Presiona Ctrl+C para detener.\n")

        # ─── Bucle principal ────────────────────────────────────────────
        frames_enviados = 0
        t_inicio = time.time()

        while not _cerrando:
            try:
                frameset = pipeline.wait_for_frames(timeout_ms=5000)
            except RuntimeError:
                print("  ⚠ Timeout esperando frames de la cámara ...")
                continue

            fc = frameset.get_color_frame()
            fd = frameset.get_depth_frame()
            fi1 = frameset.get_infrared_frame(1)
            fi2 = frameset.get_infrared_frame(2)

            if not fc or not fd or not fi1 or not fi2:
                continue

            # Convertir a numpy
            color_img = np.asanyarray(fc.get_data())       # 1920×1080 BGR
            depth_raw = np.asanyarray(fd.get_data())        # 1280×720 Z16
            ir1_img = np.asanyarray(fi1.get_data())         # 1280×720 gray
            ir2_img = np.asanyarray(fi2.get_data())         # 1280×720 gray

            # Procesar profundidad → heatmap JET
            depth_clipped = np.clip(depth_raw, 0, 4000)
            depth_8bit = (depth_clipped * (255.0 / 4000.0)).astype(np.uint8)
            depth_color = cv2.applyColorMap(depth_8bit, cv2.COLORMAP_JET)
            depth_color[depth_raw == 0] = [0, 0, 0]  # negro donde no hay dato

            # Enviar cada canal a su FFmpeg
            try:
                procesos_ff["color"].stdin.write(color_img.tobytes())
                procesos_ff["depth"].stdin.write(depth_color.tobytes())
                procesos_ff["ir1"].stdin.write(ir1_img.tobytes())
                procesos_ff["ir2"].stdin.write(ir2_img.tobytes())
            except (BrokenPipeError, OSError) as e:
                print(f"  ✗ Error escribiendo a FFmpeg: {e}")
                break

            frames_enviados += 1

            # Log cada 150 frames (~5 segundos)
            if frames_enviados % 150 == 0:
                dt = time.time() - t_inicio
                fps = frames_enviados / dt if dt > 0 else 0
                print(f"  📹 Frames: {frames_enviados} | FPS: {fps:.1f} | Tiempo: {dt:.0f}s")

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
        print("\n  Liberando recursos ...")

        # Detener pipeline
        if pipeline_activo and pipeline:
            try:
                pipeline.stop()
                print("  ✓ Pipeline RealSense detenido")
            except Exception:
                pass

        # Cerrar FFmpeg
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

        print("\n  Emisor finalizado.\n")


# ═══════════════════════════════════════════════════════════════════════════
# PUNTO DE ENTRADA
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Emisor RTSP v2 para Intel RealSense D435 — Ubuntu/Linux nativo.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  python3 emisor_ubuntu.py                    # Cámara 0, puerto 8554
  python3 emisor_ubuntu.py --cam 1            # Segunda cámara
  python3 emisor_ubuntu.py --puerto 9554      # Puerto alternativo
  python3 emisor_ubuntu.py --calidad 4000     # Mayor calidad
  python3 emisor_ubuntu.py --listar-camaras   # Ver cámaras conectadas
  python3 emisor_ubuntu.py --diagnostico      # Diagnóstico completo del sistema
        """
    )

    parser.add_argument("--puerto", type=int, default=PUERTO_RTSP_DEFECTO,
                        help=f"Puerto RTSP (defecto: {PUERTO_RTSP_DEFECTO})")
    parser.add_argument("--cam", type=int, default=0,
                        help="Índice de la cámara RealSense (defecto: 0)")
    parser.add_argument("--calidad", type=int, default=2000,
                        help="Bitrate total en kbps (defecto: 2000)")
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
        bitrate_kbps=args.calidad
    )
