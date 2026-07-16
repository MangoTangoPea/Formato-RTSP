#!/usr/bin/env python3
"""
Utilidades compartidas entre emisor y receptor.

Funciones de red, puertos y sistema de archivos usadas
durante la ejecución normal (no diagnóstico).
"""

import os
import socket
import shutil
import subprocess


# ===========================================================================
# DIRECTORIO DE GRABACIONES
# ===========================================================================

# Ruta relativa al directorio del script
DIR_GRABACIONES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "grabaciones")


def asegurar_carpeta_grabaciones():
    """
    Crea la carpeta 'grabaciones/' si no existe.

    Returns
    -------
    str
        Ruta absoluta a la carpeta de grabaciones.
    """
    os.makedirs(DIR_GRABACIONES, exist_ok=True)
    return DIR_GRABACIONES


# ===========================================================================
# DETECCIÓN DE FFMPEG
# ===========================================================================

def buscar_ffmpeg():
    """
    Busca FFmpeg en el sistema. Prioriza el binario del sistema (apt),
    luego intenta imageio-ffmpeg como fallback.

    Returns
    -------
    tuple (str, str) or (None, None)
        (ruta_ejecutable, descripción_origen) o (None, None) si no existe.
    """
    # Opción 1: FFmpeg del sistema
    ruta_sistema = shutil.which("ffmpeg")
    if ruta_sistema:
        try:
            res = subprocess.run(
                [ruta_sistema, "-version"],
                capture_output=True, text=True, timeout=5
            )
            if res.returncode == 0:
                version = res.stdout.split("\n")[0] if res.stdout else "desconocida"
                return ruta_sistema, f"sistema ({version})"
        except Exception:
            pass

    # Opción 2: imageio-ffmpeg (fallback)
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


# ===========================================================================
# RED
# ===========================================================================

def obtener_ip_local():
    """
    Obtiene la IP local de la máquina en la red LAN.

    Returns
    -------
    str
        Dirección IP local (ej: '192.168.1.42') o '127.0.0.1' si falla.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def verificar_puerto_disponible(puerto):
    """
    Verifica si un puerto TCP está disponible.

    Parameters
    ----------
    puerto : int
        Número de puerto a verificar.

    Returns
    -------
    bool
        True si el puerto está libre, False si está en uso.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        result = s.connect_ex(("127.0.0.1", puerto))
        s.close()
        return result != 0
    except Exception:
        return True
