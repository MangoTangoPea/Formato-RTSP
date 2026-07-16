#!/usr/bin/env python3
"""
Receptor RTSP Multicanal - Ubuntu/Linux.

Se conecta a 4 streams RTSP del emisor RealSense D435, extrae metadatos
LSB (Frame ID + Timestamp) y muestra sincronía en tiempo real.

Controles:
  M       -> Mosaico (4 vistas combinadas)
  1       -> Solo Color (RGB 1920x1080)
  2       -> Solo Infrarrojo 1 (Left)
  3       -> Solo Profundidad (Depth heatmap)
  4       -> Solo Infrarrojo 2 (Right)
  H       -> Mostrar/ocultar HUD
  F       -> Pantalla completa on/off
  R       -> Iniciar/detener grabación
  Q / ESC -> Salir

Uso:
    python3 receptor.py <IP_DEL_EMISOR>
    python3 receptor.py <IP> <PUERTO_BASE>
    python3 receptor.py --sin-hud <IP>
"""

import sys
import time
import argparse
import os
import threading
import subprocess
import datetime

import cv2
import numpy as np

from lsb_steganography import extraer_lsb, formatear_timestamp_ns
from utils import buscar_ffmpeg, asegurar_carpeta_grabaciones


# ===========================================================================
# CONSTANTES
# ===========================================================================

PUERTO_RTSP_DEFECTO = 8554
NOMBRE_VENTANA = "Receptor RTSP - RealSense D435 (Ubuntu - LSB)"

MOSAICO_ANCHO = 1920
MOSAICO_ALTO = 1440

CANALES_INFO = {
    "color": {"titulo": "Color (RGB)",      "color": (0, 200, 100), "res": "1920x1080"},
    "depth": {"titulo": "Profundidad",      "color": (0, 140, 255), "res": "1280x720"},
    "ir1":   {"titulo": "Infrarrojo 1 (L)", "color": (200, 200, 0), "res": "1280x720"},
    "ir2":   {"titulo": "Infrarrojo 2 (R)", "color": (200, 100, 200), "res": "1280x720"},
}


# ===========================================================================
# LECTOR RTSP (hilo dedicado por canal)
# ===========================================================================

class LectorRTSP:
    """
    Lector asíncrono de un flujo RTSP con hilo dedicado.
    Mantiene solo el frame más reciente. Reconexión automática con backoff.
    Extrae metadatos LSB de cada frame.
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

        # Metadatos LSB
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
        """Bucle interno: conecta, lee frames, extrae LSB, reconecta."""
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"

        cap = None
        backoff = 0.5

        while self.corriendo:
            if cap is None or not cap.isOpened():
                with self.lock:
                    self.conectado = False
                cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
                if cap.isOpened():
                    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                    with self.lock:
                        self.conectado = True
                    backoff = 0.5
                else:
                    time.sleep(min(backoff, 5.0))
                    backoff = min(backoff * 1.5, 5.0)
                    continue

            ret, frame = cap.read()
            if not ret or frame is None:
                cap.release()
                cap = None
                with self.lock:
                    self.conectado = False
                time.sleep(0.3)
                continue

            fid, ts = extraer_lsb(frame)

            with self.lock:
                self.frame = frame
                self.frames_recibidos += 1
                self._contador_fps += 1
                self.frame_id = fid
                self.timestamp_ns = ts

            ahora = time.time()
            dt = ahora - self._t_fps
            if dt >= 1.0:
                with self.lock:
                    self.fps = self._contador_fps / dt
                    self._contador_fps = 0
                    self._t_fps = ahora

        if cap and cap.isOpened():
            cap.release()

    def obtener_frame(self):
        """Retorna el frame más reciente o None."""
        with self.lock:
            return self.frame.copy() if self.frame is not None else None

    def obtener_estado(self):
        """Retorna (conectado, fps, total_frames)."""
        with self.lock:
            return self.conectado, self.fps, self.frames_recibidos

    def obtener_metadatos(self):
        """Retorna (frame_id, timestamp_ns) extraídos por LSB."""
        with self.lock:
            return self.frame_id, self.timestamp_ns

    def detener(self):
        """Detiene el hilo de lectura."""
        self.corriendo = False
        if self.hilo:
            self.hilo.join(timeout=2.0)


# ===========================================================================
# GRABADOR MKV (toggle con tecla R)
# ===========================================================================

class GrabadorMKV:
    """
    Grabador de mosaico en MKV usando FFmpeg.
    Se inicia/detiene con toggle (tecla R).
    Los archivos se guardan automáticamente en grabaciones/.
    """

    def __init__(self, ruta_ffmpeg):
        self.ruta_ffmpeg = ruta_ffmpeg
        self.proceso = None
        self.grabando = False
        self.ruta_archivo = None
        self.frames_grabados = 0
        self.t_inicio = None

    def toggle(self):
        """
        Alterna entre grabar y no grabar.

        Returns
        -------
        bool
            True si ahora está grabando, False si se detuvo.
        """
        if self.grabando:
            self.detener()
            return False
        else:
            return self.iniciar()

    def iniciar(self):
        """
        Inicia la grabación. Genera nombre automático con timestamp.

        Returns
        -------
        bool
            True si se inició correctamente.
        """
        dir_grab = asegurar_carpeta_grabaciones()
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.ruta_archivo = os.path.join(dir_grab, f"grabacion_{timestamp}.mkv")

        cmd = [
            self.ruta_ffmpeg,
            "-y",
            "-f", "rawvideo",
            "-pix_fmt", "bgr24",
            "-s", f"{MOSAICO_ANCHO}x{MOSAICO_ALTO}",
            "-r", "30",
            "-i", "-",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-preset", "ultrafast",
            "-crf", "18",
            "-metadata", "title=RealSense D435 Mosaico (Receptor Ubuntu)",
            "-metadata", f"comment=Grabacion {timestamp}",
            "-f", "matroska",
            self.ruta_archivo
        ]

        try:
            self.proceso = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE
            )
        except Exception as e:
            print(f"  [X] Error al iniciar grabación: {e}")
            return False

        time.sleep(0.3)
        if self.proceso.poll() is not None:
            stderr_out = self.proceso.stderr.read().decode(errors="replace")[:500]
            print(f"  [X] FFmpeg de grabación terminó: {stderr_out}")
            return False

        self.grabando = True
        self.frames_grabados = 0
        self.t_inicio = time.time()
        print(f"  [REC] GRABACIÓN INICIADA -> {self.ruta_archivo}")
        return True

    def escribir_frame(self, mosaico):
        """
        Escribe un frame de mosaico al archivo MKV.

        Parameters
        ----------
        mosaico : np.ndarray
            Frame BGR de 1920x1440.
        """
        if not self.grabando or self.proceso is None:
            return

        try:
            self.proceso.stdin.write(mosaico.tobytes())
            self.frames_grabados += 1
        except (BrokenPipeError, OSError) as e:
            print(f"  [!] Error de grabación: {e}")
            self.grabando = False

    def detener(self):
        """Detiene la grabación y cierra el archivo MKV."""
        if self.proceso is None:
            self.grabando = False
            return

        try:
            if self.proceso.stdin:
                self.proceso.stdin.close()
        except Exception:
            pass

        try:
            self.proceso.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proceso.kill()
        except Exception:
            pass

        duracion = time.time() - self.t_inicio if self.t_inicio else 0
        print(f"  [STOP] GRABACIÓN DETENIDA -> {self.ruta_archivo}")
        print(f"    {self.frames_grabados} frames, {duracion:.1f}s")

        self.proceso = None
        self.grabando = False
        self.ruta_archivo = None

    @property
    def info_grabacion(self):
        """Retorna texto con info de la grabación actual."""
        if not self.grabando:
            return ""
        duracion = time.time() - self.t_inicio if self.t_inicio else 0
        return f"REC {duracion:.0f}s ({self.frames_grabados} frames)"


# ===========================================================================
# FUNCIONES DE RENDERIZADO
# ===========================================================================

def crear_placeholder(ancho, alto, texto, subtexto=""):
    """Crea un frame negro con texto centrado (para canales sin datos)."""
    img = np.zeros((alto, ancho, 3), dtype=np.uint8)
    fuente = cv2.FONT_HERSHEY_SIMPLEX

    tam = cv2.getTextSize(texto, fuente, 0.6, 1)[0]
    x = (ancho - tam[0]) // 2
    y = (alto + tam[1]) // 2 - 10
    cv2.putText(img, texto, (x, y), fuente, 0.6, (80, 80, 180), 1, cv2.LINE_AA)

    if subtexto:
        tam2 = cv2.getTextSize(subtexto, fuente, 0.4, 1)[0]
        x2 = (ancho - tam2[0]) // 2
        cv2.putText(img, subtexto, (x2, y + 25), fuente, 0.4, (100, 100, 100), 1, cv2.LINE_AA)

    return img


def dibujar_hud(frame, titulo, fps, total_frames, segundos,
                color_tema=(0, 200, 100), frame_id=None, timestamp_ns=None):
    """Dibuja HUD semitransparente con metadatos LSB."""
    alto, ancho = frame.shape[:2]

    if ancho >= 1200:
        bw, bh, escala, salto = 380, 100, 0.42, 19
    elif ancho >= 800:
        bw, bh, escala, salto = 300, 88, 0.38, 17
    else:
        bw, bh, escala, salto = 240, 76, 0.34, 15

    overlay = frame.copy()
    cv2.rectangle(overlay, (8, 8), (8 + bw, 8 + bh), (20, 20, 20), -1)
    cv2.addWeighted(overlay, 0.65, frame, 0.35, 0, frame)

    fuente = cv2.FONT_HERSHEY_SIMPLEX
    x0, y0 = 16, 8 + salto

    cv2.putText(frame, titulo, (x0, y0), fuente, escala, (255, 255, 255), 1, cv2.LINE_AA)

    if frame_id is not None:
        ts_str = formatear_timestamp_ns(timestamp_ns)
        cv2.putText(frame, f"FID: {frame_id}  TS: {ts_str}",
                    (x0, y0 + salto), fuente, escala * 0.9, color_tema, 1, cv2.LINE_AA)
    else:
        cv2.putText(frame, "FID: ---  TS: --:--:--.---",
                    (x0, y0 + salto), fuente, escala * 0.9, (100, 100, 100), 1, cv2.LINE_AA)

    latencia_str = ""
    if timestamp_ns is not None and timestamp_ns > 0:
        latencia_ms = (time.time_ns() - timestamp_ns) / 1_000_000
        if 0 < latencia_ms < 60_000:
            latencia_str = f"  Lat: {latencia_ms:.0f}ms"
    cv2.putText(frame, f"FPS: {fps:.1f}{latencia_str}",
                (x0, y0 + 2 * salto), fuente, escala * 0.9, color_tema, 1, cv2.LINE_AA)

    cv2.putText(frame, f"Recibidos: {total_frames}  Tiempo: {int(segundos)}s",
                (x0, y0 + 3 * salto), fuente, escala * 0.85, (150, 150, 150), 1, cv2.LINE_AA)


def dibujar_indicador_rec(canvas, grabador):
    """
    Dibuja el indicador visual [REC] REC parpadeante cuando se está grabando.

    El círculo rojo parpadea cada 0.5 segundos para dar feedback visual claro.
    """
    if not grabador.grabando:
        return

    alto, ancho = canvas.shape[:2]
    fuente = cv2.FONT_HERSHEY_SIMPLEX

    # Parpadeo: visible 0.5s, invisible 0.5s
    parpadeo = int(time.time() * 2) % 2 == 0

    # Fondo semitransparente para el indicador
    x_rec = ancho - 220
    y_rec = 12

    overlay = canvas.copy()
    cv2.rectangle(overlay, (x_rec - 8, y_rec - 4), (ancho - 10, y_rec + 50), (15, 15, 15), -1)
    cv2.addWeighted(overlay, 0.7, canvas, 0.3, 0, canvas)

    if parpadeo:
        # Círculo rojo
        cv2.circle(canvas, (x_rec + 12, y_rec + 16), 10, (0, 0, 255), -1)

    # Texto REC
    cv2.putText(canvas, "REC", (x_rec + 28, y_rec + 22),
                fuente, 0.65, (0, 0, 255), 2, cv2.LINE_AA)

    # Info de duración
    info = grabador.info_grabacion
    if info:
        # Quitar "REC " del inicio porque ya lo mostramos como texto grande
        duracion_txt = info.replace("REC ", "")
        cv2.putText(canvas, duracion_txt, (x_rec, y_rec + 44),
                    fuente, 0.38, (180, 180, 180), 1, cv2.LINE_AA)


def verificar_sincronia(metadatos):
    """
    Compara los Frame IDs de los 4 canales para verificar sincronía.

    Returns
    -------
    tuple (bool, int, str)
        (sincronizado, delta_max, texto_estado)
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
                         sync_ok=True, grabando=False):
    """Dibuja barra de estado inferior con controles y sincronía."""
    alto, ancho = canvas.shape[:2]
    bar_h = 28

    overlay = canvas.copy()
    cv2.rectangle(overlay, (0, alto - bar_h), (ancho, alto), (15, 15, 15), -1)
    cv2.addWeighted(overlay, 0.7, canvas, 0.3, 0, canvas)

    fuente = cv2.FONT_HERSHEY_SIMPLEX
    escala = 0.4
    y_txt = alto - 8

    # Controles
    controles = "[M]osaico [1]Color [2]IR1 [3]Depth [4]IR2 [H]UD [F]ull [R]EC [Q]Salir"
    cv2.putText(canvas, controles, (10, y_txt), fuente, escala, (150, 150, 150), 1, cv2.LINE_AA)

    # Info derecha
    color_sync = (0, 200, 100) if sync_ok else (0, 100, 255)
    rec_txt = " | [REC] REC" if grabando else ""
    info = f"{texto_sync}  |  {modo.upper()}  |  {int(segundos)}s  |  {fps_total:.0f} fps{rec_txt}"
    tam = cv2.getTextSize(info, fuente, escala, 1)[0]
    cv2.putText(canvas, info, (ancho - tam[0] - 10, y_txt), fuente, escala, color_sync, 1, cv2.LINE_AA)


def construir_mosaico(color_img, depth_color, ir1_img, ir2_img):
    """Fusiona los 4 frames en un mosaico de 1920x1440."""
    ir1_small = cv2.resize(ir1_img, (640, 360), interpolation=cv2.INTER_LINEAR)
    depth_small = cv2.resize(depth_color, (640, 360), interpolation=cv2.INTER_LINEAR)
    ir2_small = cv2.resize(ir2_img, (640, 360), interpolation=cv2.INTER_LINEAR)

    if ir1_small.ndim == 2:
        ir1_small = cv2.cvtColor(ir1_small, cv2.COLOR_GRAY2BGR)
    if ir2_small.ndim == 2:
        ir2_small = cv2.cvtColor(ir2_small, cv2.COLOR_GRAY2BGR)

    fila_inferior = np.hstack([ir1_small, depth_small, ir2_small])
    mosaico = np.vstack([color_img, fila_inferior])
    return mosaico


# ===========================================================================
# FUNCIÓN PRINCIPAL
# ===========================================================================

def iniciar_receptor(ip, puerto, mostrar_hud=True):
    """
    Receptor RTSP: se conecta a 4 streams del emisor RealSense,
    extrae metadatos LSB y muestra sincronía. Soporta grabación MKV
    interactiva con tecla R.
    """
    urls = {
        "color": f"rtsp://{ip}:{puerto}/stream",
        "depth": f"rtsp://{ip}:{puerto + 1}/stream",
        "ir1":   f"rtsp://{ip}:{puerto + 2}/stream",
        "ir2":   f"rtsp://{ip}:{puerto + 3}/stream",
    }

    print("\n" + "=" * 62)
    print("  RECEPTOR RTSP - RealSense D435 - Ubuntu (LSB)")
    print("=" * 62)
    for nombre, url in urls.items():
        print(f"  {nombre:<6} -> {url}")
    print("-" * 62)
    print("  Controles: [M]osaico [1]Color [2]IR1 [3]Depth [4]IR2")
    print("             [H]UD on/off  [F]ullscreen  [R]EC  [Q]Salir")
    print("=" * 62)

    # Buscar FFmpeg para grabación (no bloquea si no existe)
    ruta_ffmpeg, origen = buscar_ffmpeg()
    if ruta_ffmpeg:
        print(f"\n  FFmpeg disponible para grabación: {ruta_ffmpeg}")
    else:
        print("\n  [!] FFmpeg no encontrado. Grabación MKV deshabilitada.")

    # Crear grabador
    grabador = GrabadorMKV(ruta_ffmpeg) if ruta_ffmpeg else None

    # Crear lectores RTSP
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
    ultimo_log = 0

    try:
        while True:
            ahora = time.time()
            dt = ahora - t_inicio

            # Obtener frames y metadatos
            frames = {}
            estados = {}
            metadatos = {}
            for nombre, lector in lectores.items():
                frames[nombre] = lector.obtener_frame()
                estados[nombre] = lector.obtener_estado()
                metadatos[nombre] = lector.obtener_metadatos()

            # Verificar sincronía
            sync_ok, sync_delta, sync_texto = verificar_sincronia(metadatos)

            # Log cada 5 segundos
            if ahora - ultimo_log > 5:
                for nombre, (conn, fps, total) in estados.items():
                    fid, ts = metadatos[nombre]
                    if conn:
                        ts_str = formatear_timestamp_ns(ts)
                        fid_str = str(fid) if fid is not None else "---"
                        print(f"  [{nombre:<6}] [OK] FID: {fid_str:>8} | TS: {ts_str} | "
                              f"FPS: {fps:.1f} | Frames: {total}")
                    elif total == 0:
                        print(f"  [{nombre:<6}] [~] esperando ...")
                if any(s[0] for s in estados.values()):
                    print(f"  [{sync_texto}]")
                ultimo_log = ahora

            # Preparar frames con placeholders
            f_color = frames["color"]
            f_depth = frames["depth"]
            f_ir1 = frames["ir1"]
            f_ir2 = frames["ir2"]

            if f_color is None:
                f_color = crear_placeholder(1920, 1080, "Esperando Color (RGB) ...", urls["color"])
            if f_depth is None:
                f_depth = crear_placeholder(1280, 720, "Esperando Profundidad ...", urls["depth"])
            if f_ir1 is None:
                f_ir1 = crear_placeholder(1280, 720, "Esperando Infrarrojo 1 ...", urls["ir1"])
            if f_ir2 is None:
                f_ir2 = crear_placeholder(1280, 720, "Esperando Infrarrojo 2 ...", urls["ir2"])

            # Convertir grayscale a BGR
            if f_ir1.ndim == 2:
                f_ir1 = cv2.cvtColor(f_ir1, cv2.COLOR_GRAY2BGR)
            elif len(f_ir1.shape) > 2 and f_ir1.shape[2] == 1:
                f_ir1 = cv2.cvtColor(f_ir1, cv2.COLOR_GRAY2BGR)
            if f_ir2.ndim == 2:
                f_ir2 = cv2.cvtColor(f_ir2, cv2.COLOR_GRAY2BGR)
            elif len(f_ir2.shape) > 2 and f_ir2.shape[2] == 1:
                f_ir2 = cv2.cvtColor(f_ir2, cv2.COLOR_GRAY2BGR)

            # -- Grabación: escribir mosaico LIMPIO (sin HUD) --------
            if grabador and grabador.grabando:
                canales_listos = all(frames[n] is not None for n in ["color", "depth", "ir1", "ir2"])
                if canales_listos:
                    mosaico_limpio = construir_mosaico(
                        f_color.copy(), f_depth.copy(),
                        f_ir1.copy(), f_ir2.copy()
                    )
                    grabador.escribir_frame(mosaico_limpio)

            # -- Componer vista según modo ---------------------------
            canvas = None

            if modo == "mosaico":
                ir1_small = cv2.resize(f_ir1, (640, 360), interpolation=cv2.INTER_LINEAR)
                depth_small = cv2.resize(f_depth, (640, 360), interpolation=cv2.INTER_LINEAR)
                ir2_small = cv2.resize(f_ir2, (640, 360), interpolation=cv2.INTER_LINEAR)

                if hud_visible:
                    _, fps_c, tot_c = estados["color"]
                    _, fps_d, tot_d = estados["depth"]
                    _, fps_1, tot_1 = estados["ir1"]
                    _, fps_2, tot_2 = estados["ir2"]

                    fid_c, ts_c = metadatos["color"]
                    fid_d, ts_d = metadatos["depth"]
                    fid_1, ts_1 = metadatos["ir1"]
                    fid_2, ts_2 = metadatos["ir2"]

                    dibujar_hud(f_color, "Color (RGB) 1920x1080", fps_c, tot_c, dt,
                                CANALES_INFO["color"]["color"], fid_c, ts_c)
                    dibujar_hud(ir1_small, "IR1 (Left)", fps_1, tot_1, dt,
                                CANALES_INFO["ir1"]["color"], fid_1, ts_1)
                    dibujar_hud(depth_small, "Profundidad", fps_d, tot_d, dt,
                                CANALES_INFO["depth"]["color"], fid_d, ts_d)
                    dibujar_hud(ir2_small, "IR2 (Right)", fps_2, tot_2, dt,
                                CANALES_INFO["ir2"]["color"], fid_2, ts_2)

                fila_inferior = np.hstack([ir1_small, depth_small, ir2_small])
                canvas = np.vstack([f_color, fila_inferior])

            elif modo in ("color", "depth", "ir1", "ir2"):
                mapa_frames = {"color": f_color, "depth": f_depth, "ir1": f_ir1, "ir2": f_ir2}
                canvas = mapa_frames[modo]

                if hud_visible:
                    info = CANALES_INFO[modo]
                    conn, fps, total = estados[modo]
                    fid, ts = metadatos[modo]
                    dibujar_hud(canvas, f"{info['titulo']} - {info['res']}",
                                fps, total, dt, info["color"], fid, ts)

            if canvas is not None:
                # Indicador REC parpadeante
                if grabador:
                    dibujar_indicador_rec(canvas, grabador)

                # Barra de estado inferior
                grabando = grabador.grabando if grabador else False
                dibujar_barra_estado(canvas, modo, dt,
                                     sum(s[1] for s in estados.values()) / 4,
                                     sync_texto, sync_ok, grabando)

                cv2.imshow(NOMBRE_VENTANA, canvas)

            # -- Entrada de teclado ----------------------------------
            tecla = cv2.waitKey(10) & 0xFF

            if tecla == ord('q') or tecla == ord('Q') or tecla == 27:
                print("\n  [STOP] Receptor detenido por el usuario.")
                break
            elif tecla == ord('m') or tecla == ord('M'):
                modo = "mosaico"
                print("  -> Vista: Mosaico (4 canales)")
            elif tecla == ord('1'):
                modo = "color"
                print("  -> Vista: Color (RGB 1920x1080)")
            elif tecla == ord('2'):
                modo = "ir1"
                print("  -> Vista: Infrarrojo 1 (Left)")
            elif tecla == ord('3'):
                modo = "depth"
                print("  -> Vista: Profundidad (Depth)")
            elif tecla == ord('4'):
                modo = "ir2"
                print("  -> Vista: Infrarrojo 2 (Right)")
            elif tecla == ord('h') or tecla == ord('H'):
                hud_visible = not hud_visible
                print(f"  -> HUD: {'visible' if hud_visible else 'oculto'}")
            elif tecla == ord('f') or tecla == ord('F'):
                pantalla_completa = not pantalla_completa
                if pantalla_completa:
                    cv2.setWindowProperty(NOMBRE_VENTANA, cv2.WND_PROP_FULLSCREEN,
                                          cv2.WINDOW_FULLSCREEN)
                    print("  -> Pantalla completa activada")
                else:
                    cv2.setWindowProperty(NOMBRE_VENTANA, cv2.WND_PROP_FULLSCREEN,
                                          cv2.WINDOW_NORMAL)
                    print("  -> Pantalla completa desactivada")
            elif tecla == ord('r') or tecla == ord('R'):
                if grabador:
                    resultado = grabador.toggle()
                    # El mensaje ya se imprime dentro de toggle()
                else:
                    print("  [!] Grabación no disponible (FFmpeg no encontrado)")

            # Detectar cierre de ventana
            try:
                if cv2.getWindowProperty(NOMBRE_VENTANA, cv2.WND_PROP_VISIBLE) < 1:
                    print("\n  [STOP] Ventana cerrada.")
                    break
            except cv2.error:
                break

    except KeyboardInterrupt:
        print("\n\n  [STOP] Detenido por teclado (Ctrl+C).")

    finally:
        print("\n  Deteniendo lectores ...")
        for nombre, lector in lectores.items():
            lector.detener()
            print(f"  [OK] {nombre} detenido")
        cv2.destroyAllWindows()

        # Detener grabación si está activa
        if grabador and grabador.grabando:
            grabador.detener()

        print("  [OK] Recursos liberados.\n")


# ===========================================================================
# PUNTO DE ENTRADA
# ===========================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Receptor RTSP para Intel RealSense D435 - Ubuntu con extracción LSB.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  python3 receptor.py 192.168.1.42             # IP del emisor
  python3 receptor.py 192.168.1.42 9554        # Puerto base personalizado
  python3 receptor.py --sin-hud 192.168.1.42   # Sin overlay de info
  python3 receptor.py 127.0.0.1                # Prueba local

Controles en la ventana:
  M -> Mosaico (4 vistas)    1 -> Color    2 -> IR1
  3 -> Depth                 4 -> IR2      H -> HUD on/off
  F -> Fullscreen on/off     R -> Grabar/Parar    Q / ESC -> Salir

Grabación:
  Presiona R para iniciar. Presiona R de nuevo para parar.
  Los archivos se guardan en grabaciones/ con nombre automático.
        """
    )

    parser.add_argument("destino", nargs="?", default=None,
                        help="Dirección IP del emisor")
    parser.add_argument("puerto", nargs="?", type=int, default=PUERTO_RTSP_DEFECTO,
                        help=f"Puerto RTSP base (defecto: {PUERTO_RTSP_DEFECTO})")
    parser.add_argument("--sin-hud", action="store_true",
                        help="No mostrar información de estado sobre el vídeo")

    args = parser.parse_args()

    ip = "127.0.0.1"
    puerto = args.puerto

    if args.destino is not None:
        ip = args.destino

    if ip == "127.0.0.1" and args.destino is None:
        print("  [!] No se especificó IP del emisor, usando 127.0.0.1 (loopback)")
        print("  Uso: python3 receptor.py <IP_DEL_EMISOR>")

    iniciar_receptor(ip, puerto, mostrar_hud=not args.sin_hud)
