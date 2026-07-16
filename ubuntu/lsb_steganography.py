#!/usr/bin/env python3
"""
Esteganografía LSB (Least Significant Bit) para frames de video.

Inyecta y extrae 128 bits de metadatos (64-bit Frame ID + 64-bit Timestamp)
en la primera fila de cada frame usando el bit menos significativo.

Esquema de codificación
-----------------------
Los 128 bits de payload se empaquetan en big-endian como 16 bytes contiguos
usando struct.pack('>QQ', frame_id, timestamp_ns).

Cada bit lógico se almacena en BITS_POR_BLOQUE píxeles consecutivos para
redundancia contra la compresión H.264. En la extracción se aplica
"majority voting" sobre cada bloque.

Impacto visual
--------------
Solo se modifica el LSB de cada píxel: cambio máximo de +-1 en rango 0..255,
perturbación del 0.39%, completamente imperceptible al ojo humano.

Canal de inyección
------------------
- Imágenes BGR (Color, Depth heatmap): LSB del canal Azul [0].
- Imágenes monocromáticas (IR1, IR2): LSB del píxel directamente.
"""

import struct
import numpy as np


# ===========================================================================
# CONFIGURACIÓN
# ===========================================================================

BITS_POR_BLOQUE = 1       # Redundancia por bit (1 = sin redundancia)
TOTAL_BITS = 128          # 64 bits Frame ID + 64 bits Timestamp
PIXELES_LSB = TOTAL_BITS * BITS_POR_BLOQUE  # Píxeles necesarios en fila 0

# Configuración para extracción post-compresión (mayor redundancia)
BITS_POR_BLOQUE_RECEPCION = 8
PIXELES_LSB_RECEPCION = TOTAL_BITS * BITS_POR_BLOQUE_RECEPCION  # 1024


# ===========================================================================
# INYECCIÓN (lado emisor, antes de comprimir)
# ===========================================================================

def inyectar_lsb(frame, frame_id, timestamp_ns):
    """
    Inyecta 128 bits de metadatos en la primera fila (fila 0) del frame.

    El payload consta de:
      - Bits [0..63]:   Frame ID (entero secuencial de 64 bits, inicia en 1)
      - Bits [64..127]: Timestamp del computador emisor (nanosegundos)

    Cada bit lógico se replica en BITS_POR_BLOQUE píxeles consecutivos.

    Parameters
    ----------
    frame : np.ndarray
        Imagen (HxWx3 para BGR, HxW para grayscale). Se modifica in-place.
    frame_id : int
        Identificador secuencial del frame (64 bits).
    timestamp_ns : int
        Timestamp del sistema emisor en nanosegundos.

    Returns
    -------
    np.ndarray
        El mismo frame (modificado in-place).
    """
    ancho = frame.shape[1]

    if ancho < PIXELES_LSB:
        return frame

    # Empaquetar payload a 16 bytes big-endian
    datos = struct.pack('>QQ',
                        frame_id & 0xFFFFFFFFFFFFFFFF,
                        timestamp_ns & 0xFFFFFFFFFFFFFFFF)

    # Desempaquetar a 128 bits individuales y expandir con redundancia
    bits_arr = np.unpackbits(np.frombuffer(datos, dtype=np.uint8))
    mascara = np.repeat(bits_arr, BITS_POR_BLOQUE)

    # Obtener referencia directa a la primera fila (sin copia)
    if frame.ndim == 3:
        fila = frame[0, :PIXELES_LSB, 0]   # Canal Azul en BGR
    else:
        fila = frame[0, :PIXELES_LSB]       # Grayscale directo

    # Aplicar inyección LSB: limpiar LSB (AND 0xFE) + establecer nuevo (OR máscara)
    fila[:] = (fila & np.uint8(0xFE)) | mascara.astype(fila.dtype)
    return frame


# ===========================================================================
# EXTRACCIÓN (lado receptor, después de descomprimir)
# ===========================================================================

def extraer_lsb(frame):
    """
    Extrae 128 bits de metadatos LSB de la primera fila del frame
    usando majority voting sobre bloques de BITS_POR_BLOQUE_RECEPCION píxeles.

    El proceso es el inverso de inyectar_lsb():
      1. Lee los LSBs de la fila 0
      2. Los agrupa en 128 bloques
      3. Aplica majority voting
      4. Empaqueta los 128 bits y los desempaqueta con struct

    Parameters
    ----------
    frame : np.ndarray
        Imagen recibida (puede tener artefactos de compresión H.264).

    Returns
    -------
    tuple (int, int) or (None, None)
        (frame_id, timestamp_ns) o (None, None) si no se puede extraer.
    """
    try:
        ancho = frame.shape[1] if frame.ndim >= 2 else 0
        if ancho < PIXELES_LSB_RECEPCION:
            return None, None

        if frame.ndim == 3:
            fila = frame[0, :PIXELES_LSB_RECEPCION, 0]   # Canal Azul
        else:
            fila = frame[0, :PIXELES_LSB_RECEPCION]

        # Extraer LSBs y agrupar en bloques
        lsbs = (fila & np.uint8(1)).reshape(TOTAL_BITS, BITS_POR_BLOQUE_RECEPCION)

        # Majority voting: si mayoría de LSBs en el bloque son 1, el bit es 1
        bits = (lsbs.sum(axis=1) > BITS_POR_BLOQUE_RECEPCION // 2).astype(np.uint8)

        # Empaquetar bits -> bytes -> struct
        datos = np.packbits(bits)
        frame_id, timestamp_ns = struct.unpack('>QQ', datos.tobytes())
        return frame_id, timestamp_ns
    except Exception:
        return None, None


def formatear_timestamp_ns(timestamp_ns):
    """
    Formatea un timestamp en nanosegundos como HH:MM:SS.mmm

    Parameters
    ----------
    timestamp_ns : int or None
        Timestamp en nanosegundos.

    Returns
    -------
    str
        Timestamp formateado o '--:--:--.---' si no es válido.
    """
    if timestamp_ns is None or timestamp_ns == 0:
        return "--:--:--.---"
    try:
        import datetime
        ts_sec = timestamp_ns / 1e9
        dt = datetime.datetime.fromtimestamp(ts_sec)
        ms = int((timestamp_ns % 1_000_000_000) / 1_000_000)
        return dt.strftime("%H:%M:%S") + f".{ms:03d}"
    except (OSError, ValueError, OverflowError):
        return "--:--:--.---"
