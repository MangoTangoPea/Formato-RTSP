"""
Módulo esteganografia.py
Inyección y extracción de metadatos síncronos (Frame ID + Timestamp)
en la fila 0 utilizando Esteganografía LSB (Least Significant Bit).
Redundancia de 8 píxeles por bit (majority voting) para soportar compresión H.264.
"""

import struct
import numpy as np

# Configuración estructural
BITS_POR_BLOQUE = 8       # 8 píxeles por bit lógico
TOTAL_BITS      = 128     # 64 bits para Frame ID + 64 bits para Timestamp
PIXELES_LSB     = TOTAL_BITS * BITS_POR_BLOQUE  # = 1024 píxeles requeridos de ancho mínimo

def inyectar_lsb(frame, frame_id, timestamp_ns):
    """
    Inyecta 128 bits de datos (Frame ID y Timestamp) en la primera fila (fila 0) del frame.
    Modifica el frame in-place.
    
    Formatos soportados:
      - 3 canales (Color, BGR): Modifica solo el canal azul [0] (menos perceptible visualmente).
      - 1 canal (Grayscale): Modifica directamente el píxel.
    """
    if frame is None:
        return frame

    alto, ancho = frame.shape[:2]
    if ancho < PIXELES_LSB:
        # No hay suficiente espacio horizontal para inyectar 1024 px
        return frame

    # Empaquetar datos: 16 bytes (128 bits) -> Big Endian (Q: 64-bit unsigned int)
    datos_empaquetados = struct.pack('>QQ', 
                                     frame_id & 0xFFFFFFFFFFFFFFFF, 
                                     timestamp_ns & 0xFFFFFFFFFFFFFFFF)
    
    # Convertir bytes a array de bits (128 elementos de valor 0 o 1)
    bits = np.unpackbits(np.frombuffer(datos_empaquetados, dtype=np.uint8))
    
    # Replicar cada bit 8 veces seguidas para redundancia (majority voting)
    mascara = np.repeat(bits, BITS_POR_BLOQUE)
    
    # Modificar el bit menos significativo (LSB) de la primera fila
    if frame.ndim == 3:
        # Canal Azul [BGR -> B = 0]
        fila = frame[0, :PIXELES_LSB, 0]
    else:
        # Grayscale directo
        fila = frame[0, :PIXELES_LSB]
        
    # AND 0xFE limpia el LSB actual (lo pone a 0)
    # OR mascara pone el bit correspondiente (0 o 1)
    fila[:] = (fila & np.uint8(0xFE)) | mascara.astype(fila.dtype)
    return frame

def extraer_lsb(frame):
    """
    Extrae Frame ID y Timestamp de la primera fila del frame usando majority voting.
    Retorna (frame_id, timestamp_ns) o (None, None) si falla la extracción o el ancho es insuficiente.
    """
    if frame is None:
        return None, None

    alto, ancho = frame.shape[:2]
    if ancho < PIXELES_LSB:
        return None, None

    # Extraer el canal correspondiente de la primera fila
    if frame.ndim == 3:
        fila = frame[0, :PIXELES_LSB, 0]
    else:
        fila = frame[0, :PIXELES_LSB]

    # Extraer el bit menos significativo de cada píxel
    lsbs = (fila & np.uint8(1)).reshape(TOTAL_BITS, BITS_POR_BLOQUE)
    
    # Majority voting: si la suma de los 8 bits es >= 5, el bit lógico es 1. De lo contrario, es 0.
    bits = (lsbs.sum(axis=1) >= (BITS_POR_BLOQUE // 2 + 1)).astype(np.uint8)
    
    # Reconstruir bytes
    datos_bytes = np.packbits(bits).tobytes()
    
    try:
        frame_id, timestamp_ns = struct.unpack('>QQ', datos_bytes)
        return frame_id, timestamp_ns
    except Exception:
        return None, None
