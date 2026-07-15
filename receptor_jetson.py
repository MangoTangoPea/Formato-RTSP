#!/usr/bin/env python3
"""
Receptor RTSP Multicanal — Jetson Orin Nano (ARM64).

Se conecta a 4 streams RTSP del emisor Jetson (Color, Depth, IR1, IR2),
extrae los metadatos LSB ocultos (Frame ID + Timestamp) y muestra
la sincronía entre los canales en el HUD.

Mosaico simétrico 2×2 de 2560×1440:
  ┌─────────────┬──────────────┐
  │    Color     │    Depth     │
  │  1280×720    │  1280×720    │
  ├─────────────┼──────────────┤
  │     IR1      │     IR2      │
  │  1280×720    │  1280×720    │
  └─────────────┴──────────────┘

Controles de teclado:
  m  → Mosaico (4 vistas combinadas)
  1  → Solo Color (RGB)
  2  → Solo Infrarrojo 1 (Left)
  3  → Solo Profundidad (Depth heatmap)
  4  → Solo Infrarrojo 2 (Right)
  h  → Mostrar/ocultar HUD
  f  → Pantalla completa on/off
  q / ESC → Salir

Uso:
    python3 receptor_jetson.py <IP_DEL_EMISOR>
    python3 receptor_jetson.py <IP> <PUERTO>
    python3 receptor_jetson.py --sin-hud <IP>
    python3 receptor_jetson.py --grabar <IP>
"""

import sys
import time
import argparse
import os
import threading
import struct
import datetime
import subprocess
import shutil

# ─── Verificar dependencias ──────────────────────────────────────────────
try:
    import cv2
except ImportError:
    print("Error: opencv-python no está instalado.")
    print("  Instalar con: pip install opencv-python")
    print("  Si falta libGL: sudo apt install libgl1 libglib2.0-0")
    sys.exit(1)

try:
    import numpy as np
except ImportError:
    print("Error: numpy no está instalado.")
    print("  Instalar con: pip install numpy")
    sys.exit(1)


# ═════════════════════════════════════════════════════════════════════════
# CONSTANTES Y CONFIGURACIÓN
# ═════════════════════════════════════════════════════════════════════════

PUERTO_RTSP_DEFECTO = 8554
NOMBRE_VENTANA = "Receptor RTSP — RealSense D435 (Jetson · LSB)"

# ─── Dimensiones del mosaico 2×2 simétrico ──────────────────────────────
# Todos los cuadrantes son de 1280×720, formando un grid de 2 columnas × 2 filas.
# 2 columnas × 1280 = 2560 de ancho
# 2 filas    × 720  = 1440 de alto
CUADRANTE_ANCHO = 1280
CUADRANTE_ALTO = 720
MOSAICO_ANCHO = CUADRANTE_ANCHO * 2   # = 2560
MOSAICO_ALTO = CUADRANTE_ALTO * 2     # = 1440

# ─── Información de cada canal para el HUD ──────────────────────────────
CANALES_INFO = {
    "color": {"titulo": "Color (RGB)",      "color": (0, 200, 100), "res": "1280×720"},
    "depth": {"titulo": "Profundidad",      "color": (0, 140, 255), "res": "1280×720"},
    "ir1":   {"titulo": "Infrarrojo 1 (L)", "color": (200, 200, 0), "res": "1280×720"},
    "ir2":   {"titulo": "Infrarrojo 2 (R)", "color": (200, 100, 200), "res": "1280×720"},
}


# ═════════════════════════════════════════════════════════════════════════
# ESTEGANOGRAFÍA LSB — EXTRACCIÓN
# ═════════════════════════════════════════════════════════════════════════
#
# Lee los 128 bits ocultos en la primera fila de cada frame.
# Debe usar los MISMOS parámetros que el emisor:
#   BITS_POR_BLOQUE = 8  →  1024 píxeles en fila 0

BITS_POR_BLOQUE = 8       # Cada bit se repitió 8 veces al inyectar
TOTAL_BITS = 128           # 64 bits Frame ID + 64 bits Timestamp
PIXELES_LSB = TOTAL_BITS * BITS_POR_BLOQUE  # = 1024


def extraer_lsb(frame):
    """
    Lee los 128 bits escondidos en la primera fila de la imagen.
    Usa votación por mayoría en cada bloque de 8 píxeles.

    Devuelve (frame_id, timestamp_ns) o (None, None) si no se puede leer.
    """
    try:
        ancho = frame.shape[1] if frame.ndim >= 2 else 0
        if ancho < PIXELES_LSB:
            return None, None

        # Leer la primera fila del canal correcto
        if frame.ndim == 3:
            fila = frame[0, :PIXELES_LSB, 0]   # Canal Azul en BGR
        else:
            fila = frame[0, :PIXELES_LSB]       # Grayscale

        # Extraer LSBs, agrupar en bloques de 8, votar por mayoría
        lsbs = (fila & np.uint8(1)).reshape(TOTAL_BITS, BITS_POR_BLOQUE)
        bits = (lsbs.sum(axis=1) > BITS_POR_BLOQUE // 2).astype(np.uint8)

        # Reconstruir los 16 bytes y desempaquetar
        datos = np.packbits(bits)
        frame_id, timestamp_ns = struct.unpack('>QQ', datos.tobytes())
        return frame_id, timestamp_ns
    except Exception:
        return None, None


def formatear_timestamp_ns(timestamp_ns):
    """Convierte un timestamp en nanosegundos a texto: HH:MM:SS.mmm"""
    if timestamp_ns is None or timestamp_ns == 0:
        return "--:--:--.---"
    try:
        ts_sec = timestamp_ns / 1e9
        dt = datetime.datetime.fromtimestamp(ts_sec)
        ms = int((timestamp_ns % 1_000_000_000) / 1_000_000)
        return dt.strftime("%H:%M:%S") + f".{ms:03d}"
    except (OSError, ValueError, OverflowError):
        return "--:--:--.---"


# ═════════════════════════════════════════════════════════════════════════
# BÚSQUEDA DE FFMPEG
# ═════════════════════════════════════════════════════════════════════════

def buscar_ffmpeg():
    """
    Busca FFmpeg en el sistema. Primero el de apt, luego imageio-ffmpeg.
    Devuelve (ruta, origen) o (None, None).
    """
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


# ═════════════════════════════════════════════════════════════════════════
# MOSAICO Y GRABACIÓN MKV
# ═════════════════════════════════════════════════════════════════════════

def construir_mosaico(color_img, depth_color, ir1_img, ir2_img, cv2_mod):
    """
    Fusiona los 4 frames en un mosaico 2×2 simétrico de 2560×1440.

    Layout:
      ┌─────────────┬──────────────┐
      │    Color     │    Depth     │
      │  1280×720    │  1280×720    │
      ├─────────────┼──────────────┤
      │     IR1      │     IR2      │
      │  1280×720    │  1280×720    │
      └─────────────┴──────────────┘

    Todos los cuadrantes son del MISMO tamaño: sin deformaciones.
    """
    # Redimensionar cada frame a exactamente 1280×720
    c = cv2_mod.resize(color_img, (CUADRANTE_ANCHO, CUADRANTE_ALTO),
                       interpolation=cv2_mod.INTER_LINEAR)
    d = cv2_mod.resize(depth_color, (CUADRANTE_ANCHO, CUADRANTE_ALTO),
                       interpolation=cv2_mod.INTER_LINEAR)
    i1 = cv2_mod.resize(ir1_img, (CUADRANTE_ANCHO, CUADRANTE_ALTO),
                        interpolation=cv2_mod.INTER_LINEAR)
    i2 = cv2_mod.resize(ir2_img, (CUADRANTE_ANCHO, CUADRANTE_ALTO),
                        interpolation=cv2_mod.INTER_LINEAR)

    # Convertir blanco y negro a color si es necesario
    if i1.ndim == 2:
        i1 = cv2_mod.cvtColor(i1, cv2_mod.COLOR_GRAY2BGR)
    if i2.ndim == 2:
        i2 = cv2_mod.cvtColor(i2, cv2_mod.COLOR_GRAY2BGR)

    # Armar el grid 2×2
    fila_superior = np.hstack([c, d])
    fila_inferior = np.hstack([i1, i2])
    return np.vstack([fila_superior, fila_inferior])


def crear_grabacion_mosaico_mkv(ruta_ffmpeg, ruta_mkv, fps=15):
    """
    Crea el pipeline de grabación local MKV con el mosaico 2×2.
    Recibe frames crudos BGR24 de 2560×1440 por stdin y graba en Matroska.

    ¿Por qué MKV y no MP4?
    MKV (Matroska) escribe incrementalmente: si la Jetson se apaga de golpe,
    el video grabado hasta ese momento NO se corrompe. MP4 sí se corrubiría
    porque necesita escribir un índice al final del archivo.
    """
    cmd = [
        ruta_ffmpeg,
        "-y",
        "-f", "rawvideo",
        "-pix_fmt", "bgr24",
        "-s", f"{MOSAICO_ANCHO}x{MOSAICO_ALTO}",
        "-r", str(fps),
        "-i", "-",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-preset", "ultrafast",
        "-crf", "18",
        "-metadata", "title=RealSense D435 Mosaico 2x2 (Receptor Jetson)",
        "-metadata", f"comment=Layout: Color/Depth/IR1/IR2 {CUADRANTE_ANCHO}x{CUADRANTE_ALTO} cada uno",
        "-f", "matroska",
        ruta_mkv
    ]

    print(f"  → Lanzando FFmpeg de grabación MKV "
          f"(mosaico {MOSAICO_ANCHO}×{MOSAICO_ALTO}) ...")
    try:
        proceso = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE
        )
    except Exception as e:
        print(f"  ✗ Error al lanzar FFmpeg de grabación: {e}")
        return None

    time.sleep(0.3)
    if proceso.poll() is not None:
        stderr_out = proceso.stderr.read().decode(errors="replace")[:500]
        print(f"  ✗ FFmpeg de grabación terminó inesperadamente:")
        print(f"    {stderr_out}")
        return None

    print(f"  ✓ Pipeline de grabación MKV activo → {ruta_mkv}")
    return proceso


# ═════════════════════════════════════════════════════════════════════════
# LECTOR DE STREAM RTSP (un hilo por canal)
# ═════════════════════════════════════════════════════════════════════════

class LectorRTSP:
    """
    Lee un stream RTSP en un hilo dedicado.
    Solo guarda el frame más reciente (no acumula buffer).
    Se reconecta automáticamente si se cae la conexión.
    Extrae los metadatos LSB de cada frame.
    """

    def __init__(self, url, nombre):
        self.url = url
        self.nombre = nombre
        self.frame = None
        self.conectado = False
        self.corriendo = False
        self.hilo = None
        self.lock = threading.Lock()

        # Estadísticas
        self.frames_recibidos = 0
        self.fps = 0.0
        self._t_fps = time.time()
        self._contador_fps = 0

        # Metadatos extraídos por LSB
        self.frame_id = None
        self.timestamp_ns = None

    def iniciar(self):
        """Arranca el hilo de lectura."""
        self.corriendo = True
        self.hilo = threading.Thread(
            target=self._bucle_lectura,
            name=f"Lector-{self.nombre}",
            daemon=True
        )
        self.hilo.start()

    def _bucle_lectura(self):
        """
        Hilo interno: se conecta al stream RTSP, lee frames en bucle,
        extrae LSB, y se reconecta si la conexión se cae.
        """
        # Forzar TCP para el transporte RTSP (más estable que UDP)
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"

        cap = None
        backoff = 0.5  # Tiempo de espera entre reintentos (va subiendo)

        while self.corriendo:
            # ─── Conectar ───────────────────────────────────────────────
            if cap is None or not cap.isOpened():
                with self.lock:
                    self.conectado = False
                cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
                if cap.isOpened():
                    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                    with self.lock:
                        self.conectado = True
                    backoff = 0.5  # Resetear el backoff al conectar
                else:
                    # Esperar un poco más cada vez que falla
                    time.sleep(min(backoff, 5.0))
                    backoff = min(backoff * 1.5, 5.0)
                    continue

            # ─── Leer frame ─────────────────────────────────────────────
            ret, frame = cap.read()
            if not ret or frame is None:
                cap.release()
                cap = None
                with self.lock:
                    self.conectado = False
                time.sleep(0.3)
                continue

            # Extraer metadatos LSB del frame recibido
            fid, ts = extraer_lsb(frame)

            with self.lock:
                self.frame = frame
                self.frames_recibidos += 1
                self._contador_fps += 1
                self.frame_id = fid
                self.timestamp_ns = ts

            # Calcular FPS cada segundo
            ahora = time.time()
            dt = ahora - self._t_fps
            if dt >= 1.0:
                with self.lock:
                    self.fps = self._contador_fps / dt
                    self._contador_fps = 0
                    self._t_fps = ahora

        # Limpiar al salir
        if cap and cap.isOpened():
            cap.release()

    def obtener_frame(self):
        """Devuelve una copia del frame más reciente, o None."""
        with self.lock:
            return self.frame.copy() if self.frame is not None else None

    def obtener_estado(self):
        """Devuelve (conectado, fps, total_frames)."""
        with self.lock:
            return self.conectado, self.fps, self.frames_recibidos

    def obtener_metadatos(self):
        """Devuelve (frame_id, timestamp_ns) del último frame."""
        with self.lock:
            return self.frame_id, self.timestamp_ns

    def detener(self):
        """Para el hilo de lectura."""
        self.corriendo = False
        if self.hilo:
            self.hilo.join(timeout=2.0)


# ═════════════════════════════════════════════════════════════════════════
# FUNCIONES DE RENDERIZADO (HUD, placeholders, barra de estado)
# ═════════════════════════════════════════════════════════════════════════

def crear_placeholder(ancho, alto, texto, subtexto=""):
    """Crea una imagen negra con texto centrado (para canales sin datos)."""
    img = np.zeros((alto, ancho, 3), dtype=np.uint8)
    fuente = cv2.FONT_HERSHEY_SIMPLEX

    # Texto principal centrado
    tam = cv2.getTextSize(texto, fuente, 0.6, 1)[0]
    x = (ancho - tam[0]) // 2
    y = (alto + tam[1]) // 2 - 10
    cv2.putText(img, texto, (x, y), fuente, 0.6, (80, 80, 180), 1, cv2.LINE_AA)

    # Subtexto más pequeño debajo
    if subtexto:
        tam2 = cv2.getTextSize(subtexto, fuente, 0.4, 1)[0]
        x2 = (ancho - tam2[0]) // 2
        cv2.putText(img, subtexto, (x2, y + 25), fuente, 0.4,
                    (100, 100, 100), 1, cv2.LINE_AA)

    return img


def dibujar_hud(frame, titulo, fps, total_frames, segundos,
                color_tema=(0, 200, 100), frame_id=None, timestamp_ns=None):
    """
    Dibuja un recuadro semitransparente con información del canal:
    - Nombre del canal
    - Frame ID y Timestamp (extraídos por LSB)
    - FPS y latencia
    - Total de frames recibidos
    """
    alto, ancho = frame.shape[:2]

    # Adaptar el tamaño del HUD al tamaño del frame
    if ancho >= 1200:
        bw, bh, escala, salto = 380, 100, 0.42, 19
    elif ancho >= 800:
        bw, bh, escala, salto = 300, 88, 0.38, 17
    else:
        bw, bh, escala, salto = 240, 76, 0.34, 15

    # Fondo semitransparente
    overlay = frame.copy()
    cv2.rectangle(overlay, (8, 8), (8 + bw, 8 + bh), (20, 20, 20), -1)
    cv2.addWeighted(overlay, 0.65, frame, 0.35, 0, frame)

    fuente = cv2.FONT_HERSHEY_SIMPLEX
    x0, y0 = 16, 8 + salto

    # Línea 1: Nombre del canal
    cv2.putText(frame, titulo, (x0, y0), fuente, escala,
                (255, 255, 255), 1, cv2.LINE_AA)

    # Línea 2: Frame ID y Timestamp (de LSB)
    if frame_id is not None:
        ts_str = formatear_timestamp_ns(timestamp_ns)
        cv2.putText(frame, f"FID: {frame_id}  TS: {ts_str}",
                    (x0, y0 + salto), fuente, escala * 0.9,
                    color_tema, 1, cv2.LINE_AA)
    else:
        cv2.putText(frame, f"FID: ---  TS: --:--:--.---",
                    (x0, y0 + salto), fuente, escala * 0.9,
                    (100, 100, 100), 1, cv2.LINE_AA)

    # Línea 3: FPS y latencia
    latencia_str = ""
    if timestamp_ns is not None and timestamp_ns > 0:
        latencia_ms = (time.time_ns() - timestamp_ns) / 1_000_000
        if 0 < latencia_ms < 60_000:
            latencia_str = f"  Lat: {latencia_ms:.0f}ms"
    cv2.putText(frame, f"FPS: {fps:.1f}{latencia_str}",
                (x0, y0 + 2 * salto), fuente, escala * 0.9,
                color_tema, 1, cv2.LINE_AA)

    # Línea 4: Frames recibidos y tiempo
    cv2.putText(frame, f"Recibidos: {total_frames}  Tiempo: {int(segundos)}s",
                (x0, y0 + 3 * salto), fuente, escala * 0.85,
                (150, 150, 150), 1, cv2.LINE_AA)


def verificar_sincronia(metadatos):
    """
    Compara los Frame IDs de los 4 canales para ver si están sincronizados.

    Devuelve (sincronizado, delta_max, texto_estado).
    """
    fids = []
    for nombre, (fid, ts) in metadatos.items():
        if fid is not None:
            fids.append(fid)

    if len(fids) < 2:
        return True, 0, "SYNC: ? (esperando canales)"

    delta = max(fids) - min(fids)
    n_canales = len(fids)

    if delta <= 1:
        return True, delta, f"SYNC: OK ({n_canales}/4 canales)"
    elif delta <= 3:
        return False, delta, f"SYNC: ~ D={delta} ({n_canales}/4)"
    else:
        return False, delta, f"SYNC: DESYNC D={delta} ({n_canales}/4)"


def dibujar_barra_estado(canvas, modo, segundos, fps_total, texto_sync="",
                         sync_ok=True):
    """Dibuja una barra de estado en la parte inferior con controles y sincronía."""
    alto, ancho = canvas.shape[:2]
    bar_h = 28

    overlay = canvas.copy()
    cv2.rectangle(overlay, (0, alto - bar_h), (ancho, alto), (15, 15, 15), -1)
    cv2.addWeighted(overlay, 0.7, canvas, 0.3, 0, canvas)

    fuente = cv2.FONT_HERSHEY_SIMPLEX
    escala = 0.4
    y_txt = alto - 8

    # Izquierda: controles de teclado
    controles = ("[M] Mosaico  [1] Color  [2] IR1  [3] Depth  [4] IR2  "
                 "[H] HUD  [F] Full  [Q] Salir")
    cv2.putText(canvas, controles, (10, y_txt), fuente, escala,
                (150, 150, 150), 1, cv2.LINE_AA)

    # Derecha: estado de sincronía
    color_sync = (0, 200, 100) if sync_ok else (0, 100, 255)
    info = (f"{texto_sync}  |  {modo.upper()}  |  "
            f"{int(segundos)}s  |  {fps_total:.0f} fps")
    tam = cv2.getTextSize(info, fuente, escala, 1)[0]
    cv2.putText(canvas, info, (ancho - tam[0] - 10, y_txt), fuente, escala,
                color_sync, 1, cv2.LINE_AA)


# ═════════════════════════════════════════════════════════════════════════
# FUNCIÓN PRINCIPAL DEL RECEPTOR
# ═════════════════════════════════════════════════════════════════════════

def iniciar_receptor(ip, puerto, mostrar_hud=True, ruta_grabacion=None):
    """
    Receptor RTSP: se conecta a los 4 streams del emisor,
    extrae metadatos LSB, muestra sincronía en el HUD,
    y opcionalmente graba un mosaico 2×2 en MKV.
    """
    urls = {
        "color": f"rtsp://{ip}:{puerto}/color",
        "depth": f"rtsp://{ip}:{puerto}/depth",
        "ir1":   f"rtsp://{ip}:{puerto}/ir1",
        "ir2":   f"rtsp://{ip}:{puerto}/ir2",
    }

    print("\n" + "═" * 62)
    print("  RECEPTOR RTSP — RealSense D435 · Jetson Orin Nano (LSB)")
    print("═" * 62)
    for nombre, url in urls.items():
        print(f"  {nombre:<6} → {url}")
    print("─" * 62)
    print(f"  Mosaico 2×2: {MOSAICO_ANCHO}×{MOSAICO_ALTO} "
          f"(cuadrantes de {CUADRANTE_ANCHO}×{CUADRANTE_ALTO})")
    print(f"  Extracción LSB: {TOTAL_BITS} bits × {BITS_POR_BLOQUE}px/bit "
          f"(votación por mayoría)")
    print("  Controles: [M]osaico [1]Color [2]IR1 [3]Depth [4]IR2")
    print("             [H]UD on/off  [F]ullscreen  [Q]Salir")
    print("═" * 62)

    # Crear un lector por cada canal (cada uno corre en su propio hilo)
    lectores = {}
    for nombre, url in urls.items():
        lectores[nombre] = LectorRTSP(url, nombre)
        lectores[nombre].iniciar()

    print("\n  Conectando a los flujos RTSP ...\n")

    # Crear ventana OpenCV
    flags = cv2.WINDOW_NORMAL
    try:
        flags |= cv2.WINDOW_GUI_NORMAL
    except AttributeError:
        pass

    cv2.namedWindow(NOMBRE_VENTANA, flags)
    cv2.resizeWindow(NOMBRE_VENTANA, 1440, 1080)

    # Estado de la interfaz
    modo = "mosaico"
    hud_visible = mostrar_hud
    pantalla_completa = False
    t_inicio = time.time()
    ultimo_log_conexion = 0

    # Estado de grabación MKV
    grabando = False
    proceso_grab = None
    grabacion_pendiente = False
    ruta_ffmpeg = None

    if ruta_grabacion:
        print(f"\n  [Grabación] Grabación local MKV programada ...")
        print(f"    → Destino: {os.path.abspath(ruta_grabacion)}")
        ruta_ffmpeg, origen = buscar_ffmpeg()
        if ruta_ffmpeg is None:
            print("    ⚠ Error: no se encontró un ejecutable de FFmpeg válido.")
        else:
            print(f"    ✓ FFmpeg encontrado: {ruta_ffmpeg} ({origen})")
            grabacion_pendiente = True

    try:
        while True:
            ahora = time.time()
            dt = ahora - t_inicio

            # ─── Obtener frames y metadatos de cada canal ──────────────
            frames = {}
            estados = {}
            metadatos = {}
            for nombre, lector in lectores.items():
                frames[nombre] = lector.obtener_frame()
                estados[nombre] = lector.obtener_estado()
                metadatos[nombre] = lector.obtener_metadatos()

            # Verificar sincronía entre los 4 canales
            sync_ok, sync_delta, sync_texto = verificar_sincronia(metadatos)

            # Log de estado de conexión cada 5 segundos
            if ahora - ultimo_log_conexion > 5:
                for nombre, (conn, fps, total) in estados.items():
                    fid, ts = metadatos[nombre]
                    if conn:
                        ts_str = formatear_timestamp_ns(ts)
                        fid_str = str(fid) if fid is not None else "---"
                        print(f"  [{nombre:<6}] ✓ FID: {fid_str:>8} | "
                              f"TS: {ts_str} | FPS: {fps:.1f} | Frames: {total}")
                    elif total == 0:
                        print(f"  [{nombre:<6}] ⟳ esperando ...")
                if any(s[0] for s in estados.values()):
                    print(f"  [{sync_texto}]")
                ultimo_log_conexion = ahora

            # ─── Preparar frames (placeholders si no hay datos) ────────
            f_color = frames["color"]
            f_depth = frames["depth"]
            f_ir1 = frames["ir1"]
            f_ir2 = frames["ir2"]

            if f_color is None:
                f_color = crear_placeholder(CUADRANTE_ANCHO, CUADRANTE_ALTO,
                                            "Esperando Color (RGB) ...",
                                            urls["color"])
            if f_depth is None:
                f_depth = crear_placeholder(CUADRANTE_ANCHO, CUADRANTE_ALTO,
                                            "Esperando Profundidad ...",
                                            urls["depth"])
            if f_ir1 is None:
                f_ir1 = crear_placeholder(CUADRANTE_ANCHO, CUADRANTE_ALTO,
                                          "Esperando Infrarrojo 1 ...",
                                          urls["ir1"])
            if f_ir2 is None:
                f_ir2 = crear_placeholder(CUADRANTE_ANCHO, CUADRANTE_ALTO,
                                          "Esperando Infrarrojo 2 ...",
                                          urls["ir2"])

            # Convertir grayscale a BGR si viene en blanco y negro
            if len(f_ir1.shape) == 2:
                f_ir1 = cv2.cvtColor(f_ir1, cv2.COLOR_GRAY2BGR)
            elif f_ir1.shape[2] == 1:
                f_ir1 = cv2.cvtColor(f_ir1, cv2.COLOR_GRAY2BGR)
            if len(f_ir2.shape) == 2:
                f_ir2 = cv2.cvtColor(f_ir2, cv2.COLOR_GRAY2BGR)
            elif f_ir2.shape[2] == 1:
                f_ir2 = cv2.cvtColor(f_ir2, cv2.COLOR_GRAY2BGR)

            # ─── Grabación MKV (antes de dibujar el HUD) ───────────────
            if (grabacion_pendiente or grabando) and ruta_ffmpeg:
                canales_listos = all(
                    lectores[n].obtener_frame() is not None
                    for n in ["color", "depth", "ir1", "ir2"]
                )
                if canales_listos:
                    if grabacion_pendiente and not grabando:
                        proceso_grab = crear_grabacion_mosaico_mkv(
                            ruta_ffmpeg, ruta_grabacion, fps=FPS_CAPTURA)
                        if proceso_grab is not None:
                            grabando = True
                            grabacion_pendiente = False
                            print(f"  🔴 GRABACIÓN MOSAICO INICIADA "
                                  f"→ {os.path.abspath(ruta_grabacion)}")
                        else:
                            grabando = False
                            grabacion_pendiente = False

                    if grabando:
                        try:
                            mosaico_grab = construir_mosaico(
                                f_color.copy(), f_depth.copy(),
                                f_ir1.copy(), f_ir2.copy(), cv2)
                            proceso_grab.stdin.write(mosaico_grab.tobytes())
                        except (BrokenPipeError, OSError) as e:
                            print(f"  ⚠ Error al escribir en grabación MKV: {e}")
                            grabando = False

            # ─── Componer la vista según el modo ───────────────────────
            canvas = None

            if modo == "mosaico":
                # Redimensionar cada frame al tamaño del cuadrante
                c = cv2.resize(f_color, (CUADRANTE_ANCHO, CUADRANTE_ALTO),
                               interpolation=cv2.INTER_LINEAR)
                d = cv2.resize(f_depth, (CUADRANTE_ANCHO, CUADRANTE_ALTO),
                               interpolation=cv2.INTER_LINEAR)
                i1 = cv2.resize(f_ir1, (CUADRANTE_ANCHO, CUADRANTE_ALTO),
                                interpolation=cv2.INTER_LINEAR)
                i2 = cv2.resize(f_ir2, (CUADRANTE_ANCHO, CUADRANTE_ALTO),
                                interpolation=cv2.INTER_LINEAR)

                if hud_visible:
                    _, fps_c, tot_c = estados["color"]
                    _, fps_d, tot_d = estados["depth"]
                    _, fps_1, tot_1 = estados["ir1"]
                    _, fps_2, tot_2 = estados["ir2"]

                    fid_c, ts_c = metadatos["color"]
                    fid_d, ts_d = metadatos["depth"]
                    fid_1, ts_1 = metadatos["ir1"]
                    fid_2, ts_2 = metadatos["ir2"]

                    dibujar_hud(c, "Color (RGB) 1280×720", fps_c, tot_c, dt,
                                CANALES_INFO["color"]["color"], fid_c, ts_c)
                    dibujar_hud(d, "Profundidad 1280×720", fps_d, tot_d, dt,
                                CANALES_INFO["depth"]["color"], fid_d, ts_d)
                    dibujar_hud(i1, "IR1 (Left) 1280×720", fps_1, tot_1, dt,
                                CANALES_INFO["ir1"]["color"], fid_1, ts_1)
                    dibujar_hud(i2, "IR2 (Right) 1280×720", fps_2, tot_2, dt,
                                CANALES_INFO["ir2"]["color"], fid_2, ts_2)

                # Armar el mosaico 2×2
                fila_superior = np.hstack([c, d])
                fila_inferior = np.hstack([i1, i2])
                canvas = np.vstack([fila_superior, fila_inferior])

            elif modo in ("color", "depth", "ir1", "ir2"):
                mapa_frames = {
                    "color": f_color, "depth": f_depth,
                    "ir1": f_ir1, "ir2": f_ir2
                }
                canvas = mapa_frames[modo]

                if hud_visible:
                    info = CANALES_INFO[modo]
                    conn, fps, total = estados[modo]
                    fid, ts = metadatos[modo]
                    dibujar_hud(canvas, f"{info['titulo']} — {info['res']}",
                                fps, total, dt, info["color"], fid, ts)

            if canvas is not None:
                # Barra de estado inferior
                dibujar_barra_estado(canvas, modo, dt,
                                     sum(s[1] for s in estados.values()) / 4,
                                     sync_texto, sync_ok)

                cv2.imshow(NOMBRE_VENTANA, canvas)

            # ─── Entrada de teclado ────────────────────────────────────
            tecla = cv2.waitKey(10) & 0xFF

            if tecla == ord('q') or tecla == ord('Q') or tecla == 27:
                print("\n  ⏹ Receptor detenido por el usuario.")
                break
            elif tecla == ord('m') or tecla == ord('M'):
                modo = "mosaico"
                print("  → Vista: Mosaico 2×2 (4 canales)")
            elif tecla == ord('1'):
                modo = "color"
                print("  → Vista: Color (RGB 1280×720)")
            elif tecla == ord('2'):
                modo = "ir1"
                print("  → Vista: Infrarrojo 1 (Left)")
            elif tecla == ord('3'):
                modo = "depth"
                print("  → Vista: Profundidad (Depth)")
            elif tecla == ord('4'):
                modo = "ir2"
                print("  → Vista: Infrarrojo 2 (Right)")
            elif tecla == ord('h') or tecla == ord('H'):
                hud_visible = not hud_visible
                print(f"  → HUD: {'visible' if hud_visible else 'oculto'}")
            elif tecla == ord('f') or tecla == ord('F'):
                pantalla_completa = not pantalla_completa
                if pantalla_completa:
                    cv2.setWindowProperty(NOMBRE_VENTANA,
                                          cv2.WND_PROP_FULLSCREEN,
                                          cv2.WINDOW_FULLSCREEN)
                    print("  → Pantalla completa activada")
                else:
                    cv2.setWindowProperty(NOMBRE_VENTANA,
                                          cv2.WND_PROP_FULLSCREEN,
                                          cv2.WINDOW_NORMAL)
                    print("  → Pantalla completa desactivada")

            # Detectar si cerraron la ventana con el botón X
            try:
                if cv2.getWindowProperty(NOMBRE_VENTANA, cv2.WND_PROP_VISIBLE) < 1:
                    print("\n  ⏹ Ventana cerrada.")
                    break
            except cv2.error:
                break

    except KeyboardInterrupt:
        print("\n\n  ⏹ Detenido por teclado (Ctrl+C).")

    finally:
        print("\n  Deteniendo lectores ...")
        for nombre, lector in lectores.items():
            lector.detener()
            print(f"  ✓ {nombre} detenido")
        cv2.destroyAllWindows()

        # Detener grabación MKV si estaba activa
        if proceso_grab:
            try:
                if proceso_grab.stdin:
                    proceso_grab.stdin.close()
            except Exception:
                pass
            try:
                proceso_grab.wait(timeout=5)
                print(f"  ✓ Grabación MKV finalizada → {ruta_grabacion}")
            except subprocess.TimeoutExpired:
                proceso_grab.kill()
                print("  ⚠ FFmpeg de grabación forzado a detener")
            except Exception:
                pass

        print("  ✓ Recursos liberados.\n")


# ═════════════════════════════════════════════════════════════════════════
# CONSTANTE DE FPS PARA GRABACIÓN
# ═════════════════════════════════════════════════════════════════════════
# Debe coincidir con los FPS del emisor. Si cambias los FPS del emisor,
# cambia este valor también.
FPS_CAPTURA = 15


# ═════════════════════════════════════════════════════════════════════════
# PUNTO DE ENTRADA
# ═════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Receptor RTSP para RealSense D435 — Jetson Orin Nano (ARM64).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  python3 receptor_jetson.py 192.168.1.42             # IP del emisor
  python3 receptor_jetson.py 192.168.1.42 9554        # Puerto personalizado
  python3 receptor_jetson.py rtsp://192.168.1.42:8554/color   # URL directa
  python3 receptor_jetson.py --sin-hud 192.168.1.42   # Sin overlay de info
  python3 receptor_jetson.py --grabar 192.168.1.42    # Graba en grabacion.mkv
  python3 receptor_jetson.py 127.0.0.1                # Prueba local

Controles en la ventana:
  M → Mosaico 2×2 (4 vistas)  1 → Color    2 → IR1
  3 → Depth                   4 → IR2      H → HUD on/off
  F → Fullscreen on/off       Q / ESC → Salir

HUD muestra: Frame ID real (LSB), Timestamp del emisor, Latencia,
             y estado de sincronía entre los 4 canales.
        """
    )

    parser.add_argument("destino", nargs="?", default=None,
                        help="IP del emisor o URL RTSP completa")
    parser.add_argument("puerto", nargs="?", type=int,
                        default=PUERTO_RTSP_DEFECTO,
                        help=f"Puerto RTSP (defecto: {PUERTO_RTSP_DEFECTO})")
    parser.add_argument("--sin-hud", action="store_true",
                        help="No mostrar información de estado sobre el vídeo")
    parser.add_argument("--grabar", nargs="?", const="grabacion.mkv",
                        default=None, metavar="RUTA.mkv",
                        help="Grabar mosaico en archivo MKV (defecto: grabacion.mkv)")

    args = parser.parse_args()

    # Parsear destino (IP o URL RTSP)
    ip = "127.0.0.1"
    puerto = args.puerto

    if args.destino is not None:
        if args.destino.startswith("rtsp://"):
            sin_proto = args.destino[7:]
            if "/" in sin_proto:
                host_port = sin_proto.split("/")[0]
            else:
                host_port = sin_proto

            if ":" in host_port:
                partes = host_port.split(":")
                ip = partes[0]
                try:
                    puerto = int(partes[1])
                except ValueError:
                    pass
            else:
                ip = host_port
        else:
            ip = args.destino

    if ip == "127.0.0.1" and args.destino is None:
        print("  ⚠ No se especificó IP del emisor, usando 127.0.0.1 (loopback)")
        print("  Uso: python3 receptor_jetson.py <IP_DEL_EMISOR>")

    iniciar_receptor(ip, puerto,
                     mostrar_hud=not args.sin_hud,
                     ruta_grabacion=args.grabar)
