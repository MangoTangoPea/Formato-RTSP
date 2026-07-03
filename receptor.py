#!/usr/bin/env python3
"""
Receptor RTSP Multicanal — Windows (v3).

Rediseñado con extracción de esteganografía LSB para verificación de sincronía.
Se conecta a 4 flujos RTSP independientes de la cámara Intel RealSense D435
(Color, IR1, IR2, Depth) y extrae los metadatos ocultos (Frame ID + Timestamp)
para mostrar la sincronía real entre canales en el HUD.

Teclas de control en la ventana:
  m/M → Modo Mosaico (RGB superior, IR1/Depth/IR2 inferiores)
  1   → Vista exclusiva RGB (resolución nativa 1920x1080)
  2   → Vista exclusiva IR1 (resolución nativa 1280x720)
  3   → Vista exclusiva Profundidad (resolución nativa 1280x720)
  4   → Vista exclusiva IR2 (resolución nativa 1280x720)
  h/H → HUD on/off
  f/F → Pantalla completa on/off
  q/Q/ESC → Salir

Uso:
    python receptor.py <IP_DEL_EMISOR>
    python receptor.py <IP> <PUERTO>
    python receptor.py rtsp://<IP>:<PUERTO>/color
    python receptor.py --sin-hud <IP>
"""

import sys
import time
import argparse
import os
import threading
import struct
import datetime

# ─── Configurar la codificación de la consola para Unicode en Windows ─────
if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

# ─── Intentar importar librerías necesarias ──────────────────────────────
try:
    import cv2
    import numpy as np
except ImportError:
    print("Error: opencv-python y numpy son obligatorios.")
    print("  Instálalo con:  pip install -r requirements.txt")
    sys.exit(1)


# ═══════════════════════════════════════════════════════════════════════════
# CONSTANTES Y CONFIGURACIÓN
# ═══════════════════════════════════════════════════════════════════════════

PUERTO_RTSP_DEFECTO = 8554
NOMBRE_VENTANA = "Receptor RTSP — RealSense D435 (v3 · LSB)"


# ═══════════════════════════════════════════════════════════════════════════
# ESTEGANOGRAFÍA LSB — EXTRACCIÓN
# ═══════════════════════════════════════════════════════════════════════════

BITS_POR_BLOQUE = 8
TOTAL_BITS = 128
PIXELES_LSB = TOTAL_BITS * BITS_POR_BLOQUE  # 1024


def extraer_lsb(frame):
    """
    Extrae 128 bits de metadatos LSB de la primera fila del frame
    usando majority voting sobre bloques de BITS_POR_BLOQUE píxeles.

    Retorna (frame_id, timestamp_ns) o (None, None) si no es posible.
    """
    try:
        ancho = frame.shape[1] if frame.ndim >= 2 else 0
        if ancho < PIXELES_LSB:
            return None, None

        if frame.ndim == 3:
            fila = frame[0, :PIXELES_LSB, 0]   # Canal Azul en formato BGR
        else:
            fila = frame[0, :PIXELES_LSB]

        # Extraer el bit menos significativo
        lsbs = (fila & np.uint8(1)).reshape(TOTAL_BITS, BITS_POR_BLOQUE)
        # Majority voting: si más de la mitad de los bits de redundancia son 1, el bit se considera 1
        bits = (lsbs.sum(axis=1) > BITS_POR_BLOQUE // 2).astype(np.uint8)

        datos = np.packbits(bits)
        frame_id, timestamp_ns = struct.unpack('>QQ', datos.tobytes())
        return frame_id, timestamp_ns
    except Exception as e:
        # En caso de corrupción extrema de frames o errores de decodificación de red, retornamos None
        return None, None


def formatear_timestamp_ns(timestamp_ns):
    """Formatea un timestamp en nanosegundos como HH:MM:SS.mmm"""
    if timestamp_ns is None or timestamp_ns == 0:
        return "--:--:--.---"
    try:
        ts_sec = timestamp_ns / 1e9
        dt = datetime.datetime.fromtimestamp(ts_sec)
        ms = int((timestamp_ns % 1_000_000_000) / 1_000_000)
        return dt.strftime("%H:%M:%S") + f".{ms:03d}"
    except (OSError, ValueError, OverflowError):
        return "--:--:--.---"


# ═══════════════════════════════════════════════════════════════════════════
# CLASE LECTORA MULTIHILO
# ═══════════════════════════════════════════════════════════════════════════

class RTSPStreamReader:
    """
    Lector de flujo RTSP que se ejecuta en un hilo secundario.
    Mantiene siempre el fotograma más reciente y extrae metadatos LSB
    (Frame ID + Timestamp) de cada frame recibido.
    """
    def __init__(self, url, nombre):
        self.url = url
        self.nombre = nombre
        self.cap = None
        self.frame = None
        self.running = False
        self.thread = None
        self.lock = threading.Lock()

        # Estadísticas
        self.fotogramas_recibidos = 0
        self.fps = 0.0
        self.ultimo_tiempo_fps = time.time()
        self._contador_fps = 0
        self.conectado = False

        # Metadatos LSB
        self.frame_id = None
        self.timestamp_ns = None

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._update,
                                       name=f"Reader-{self.nombre}", daemon=True)
        self.thread.start()

    def _update(self):
        # Forzar transporte TCP
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
        backoff = 0.5

        while self.running:
            if self.cap is None or not self.cap.isOpened():
                self.conectado = False
                self.cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
                if self.cap.isOpened():
                    self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                    self.conectado = True
                    backoff = 0.5
                else:
                    time.sleep(min(backoff, 5.0))
                    backoff = min(backoff * 1.5, 5.0)
                    continue

            ret, frame = self.cap.read()
            if not ret or frame is None:
                self.cap.release()
                self.cap = None
                self.conectado = False
                time.sleep(0.3)
                continue

            # Extraer metadatos LSB
            fid, ts = extraer_lsb(frame)

            with self.lock:
                self.frame = frame
                self.fotogramas_recibidos += 1
                self._contador_fps += 1
                self.frame_id = fid
                self.timestamp_ns = ts

            # Calcular FPS cada segundo
            ahora = time.time()
            dt = ahora - self.ultimo_tiempo_fps
            if dt >= 1.0:
                with self.lock:
                    self.fps = self._contador_fps / dt
                    self._contador_fps = 0
                    self.ultimo_tiempo_fps = ahora

        if self.cap and self.cap.isOpened():
            self.cap.release()

    def get_frame(self):
        with self.lock:
            return self.frame.copy() if self.frame is not None else None

    def get_info(self):
        with self.lock:
            return self.conectado, self.fps, self.fotogramas_recibidos

    def get_metadatos(self):
        """Retorna (frame_id, timestamp_ns) extraídos por LSB."""
        with self.lock:
            return self.frame_id, self.timestamp_ns

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=1.0)
        if self.cap:
            self.cap.release()


# ═══════════════════════════════════════════════════════════════════════════
# FUNCIONES AUXILIARES DE RENDERIZADO
# ═══════════════════════════════════════════════════════════════════════════

def generar_placeholder(ancho, alto, texto):
    """Genera un frame negro con un texto descriptivo centrado."""
    img = np.zeros((alto, ancho, 3), dtype=np.uint8)
    fuente = cv2.FONT_HERSHEY_SIMPLEX
    escala = 0.6
    grosor = 2

    t_size = cv2.getTextSize(texto, fuente, escala, grosor)[0]
    tx = (ancho - t_size[0]) // 2
    ty = (alto + t_size[1]) // 2

    cv2.putText(img, texto, (tx, ty), fuente, escala, (0, 0, 180), grosor, cv2.LINE_AA)
    return img


def dibujar_hud_panel(frame, titulo, fps, frames_count, segundos, resolucion,
                      color_tema=(0, 255, 0), posicion="top-left",
                      frame_id=None, timestamp_ns=None):
    """
    Dibuja una barra de estado OSD semitransparente con metadatos LSB reales.
    """
    alto, ancho = frame.shape[:2]

    # Adaptar tamaños
    if ancho >= 1000:
        box_ancho, box_alto = 380, 105
        escala, grosor, salto = 0.45, 1, 20
    else:
        box_ancho, box_alto = 290, 88
        escala, grosor, salto = 0.38, 1, 17

    # Coordenadas de la caja
    if posicion == "bottom-right":
        box_x2 = ancho - 10
        box_y2 = alto - 10
        box_x1 = box_x2 - box_ancho
        box_y1 = box_y2 - box_alto
    else:
        box_x1 = 10
        box_y1 = 10
        box_x2 = box_x1 + box_ancho
        box_y2 = box_y1 + box_alto

    # Fondo semitransparente
    overlay = frame.copy()
    cv2.rectangle(overlay, (box_x1, box_y1), (box_x2, box_y2), (30, 30, 30), -1)
    cv2.addWeighted(overlay, 0.6, frame, 0.4, 0, frame)

    fuente = cv2.FONT_HERSHEY_SIMPLEX
    color_titulo = (0, 0, 255)

    # Línea 1: Título
    cv2.putText(frame, titulo, (box_x1 + 10, box_y1 + salto),
                fuente, escala, color_titulo, grosor, cv2.LINE_AA)

    # Línea 2: Frame ID + Timestamp (de LSB)
    if frame_id is not None:
        ts_str = formatear_timestamp_ns(timestamp_ns)
        cv2.putText(frame, f"FID: {frame_id}  TS: {ts_str}",
                    (box_x1 + 10, box_y1 + salto + salto),
                    fuente, escala * 0.9, color_tema, grosor, cv2.LINE_AA)
    else:
        cv2.putText(frame, "FID: ---  TS: --:--:--.---",
                    (box_x1 + 10, box_y1 + salto + salto),
                    fuente, escala * 0.9, (100, 100, 100), grosor, cv2.LINE_AA)

    # Línea 3: FPS + Latencia
    latencia_str = ""
    if timestamp_ns is not None and timestamp_ns > 0:
        latencia_ms = (time.time_ns() - timestamp_ns) / 1_000_000
        if 0 < latencia_ms < 60_000:
            latencia_str = f"  Lat: {latencia_ms:.0f}ms"
    cv2.putText(frame, f"FPS: {fps:.1f}{latencia_str}",
                (box_x1 + 10, box_y1 + salto + 2 * salto),
                fuente, escala * 0.9, color_tema, grosor, cv2.LINE_AA)

    # Línea 4: Frames recibidos + resolución
    cv2.putText(frame, f"Recv: {frames_count} | {resolucion} | {int(segundos)}s",
                (box_x1 + 10, box_y1 + salto + 3 * salto),
                fuente, escala * 0.85, (150, 150, 150), grosor, cv2.LINE_AA)


def verificar_sincronia(metadatos):
    """
    Compara los Frame IDs de los 4 canales para verificar sincronía.

    Returns:
        (sincronizado: bool, delta_max: int, texto: str)
    """
    fids = []
    for nombre, (fid, ts) in metadatos.items():
        if fid is not None:
            fids.append(fid)

    if len(fids) < 2:
        return True, 0, "SYNC: ? (esperando)"

    delta = max(fids) - min(fids)
    n = len(fids)

    if delta <= 1:
        return True, delta, f"SYNC: OK ({n}/4)"
    elif delta <= 3:
        return False, delta, f"SYNC: ~ D={delta} ({n}/4)"
    else:
        return False, delta, f"SYNC: DESYNC D={delta} ({n}/4)"


def letterbox(frame, ancho_ventana, alto_ventana):
    """Escala el frame para que quepa en la ventana sin deformarse."""
    if ancho_ventana <= 0 or alto_ventana <= 0:
        return frame

    alto_frame, ancho_frame = frame.shape[:2]
    escala = min(ancho_ventana / ancho_frame, alto_ventana / alto_frame)

    nuevo_ancho = int(ancho_frame * escala)
    nuevo_alto = int(alto_frame * escala)

    frame_escalado = cv2.resize(frame, (nuevo_ancho, nuevo_alto),
                                interpolation=cv2.INTER_LINEAR)

    lienzo = np.zeros((alto_ventana, ancho_ventana, 3), dtype=np.uint8)
    x_offset = (ancho_ventana - nuevo_ancho) // 2
    y_offset = (alto_ventana - nuevo_alto) // 2
    lienzo[y_offset:y_offset + nuevo_alto, x_offset:x_offset + nuevo_ancho] = frame_escalado

    return lienzo


# ═══════════════════════════════════════════════════════════════════════════
# FUNCIÓN PRINCIPAL DEL RECEPTOR
# ═══════════════════════════════════════════════════════════════════════════

def iniciar_receptor(url_base_ip, puerto, mostrar_hud_info=True):
    """
    Receptor RTSP v3 con extracción LSB para Windows.
    Se conecta a 4 streams independientes, extrae metadatos LSB ocultos
    y muestra sincronía real en el HUD.
    """
    url_color = f"rtsp://{url_base_ip}:{puerto}/color"
    url_depth = f"rtsp://{url_base_ip}:{puerto}/depth"
    url_ir1 = f"rtsp://{url_base_ip}:{puerto}/ir1"
    url_ir2 = f"rtsp://{url_base_ip}:{puerto}/ir2"

    print("\n" + "═" * 62)
    print("  RECEPTOR RTSP MULTICANAL — RealSense D435 (v3 · LSB)")
    print("═" * 62)
    print(f"  Color (RGB):   {url_color}")
    print(f"  Depth Map:     {url_depth}")
    print(f"  Infrared 1:    {url_ir1}")
    print(f"  Infrared 2:    {url_ir2}")
    print("─" * 62)
    print("  Extracción LSB: 128 bits × 8px/bit (majority voting)")
    print("═" * 62)
    print("\n  Atajos de teclado en la ventana de vídeo:")
    print("    [m] Modo Mosaico (4 streams)   [h] HUD on/off")
    print("    [1] Color  [2] IR1  [3] Depth  [4] IR2")
    print("    [f] Pantalla completa          [q] Salir\n")

    # Iniciar lectores
    reader_color = RTSPStreamReader(url_color, "Color")
    reader_depth = RTSPStreamReader(url_depth, "Depth")
    reader_ir1 = RTSPStreamReader(url_ir1, "IR1")
    reader_ir2 = RTSPStreamReader(url_ir2, "IR2")

    reader_color.start()
    reader_depth.start()
    reader_ir1.start()
    reader_ir2.start()

    cv2.namedWindow(NOMBRE_VENTANA, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(NOMBRE_VENTANA, 1440, 1080)

    # Estado de la UI
    modo_vista = "mosaico"
    hud_visible = mostrar_hud_info
    pantalla_completa = False
    tiempo_inicio = time.time()
    ultimo_log_conexion = 0

    try:
        while True:
            ahora = time.time()
            segundos = ahora - tiempo_inicio

            # Obtener frames e info
            frame_color = reader_color.get_frame()
            frame_depth = reader_depth.get_frame()
            frame_ir1 = reader_ir1.get_frame()
            frame_ir2 = reader_ir2.get_frame()

            con_color, fps_color, f_color = reader_color.get_info()
            con_depth, fps_depth, f_depth = reader_depth.get_info()
            con_ir1, fps_ir1, f_ir1 = reader_ir1.get_info()
            con_ir2, fps_ir2, f_ir2 = reader_ir2.get_info()

            # Obtener metadatos LSB
            metadatos = {
                "color": reader_color.get_metadatos(),
                "depth": reader_depth.get_metadatos(),
                "ir1":   reader_ir1.get_metadatos(),
                "ir2":   reader_ir2.get_metadatos(),
            }

            sync_ok, sync_delta, sync_texto = verificar_sincronia(metadatos)

            # Log de conexión cada 5 segundos
            if ahora - ultimo_log_conexion > 5:
                lectores_info = [
                    ("color", con_color, fps_color, f_color),
                    ("depth", con_depth, fps_depth, f_depth),
                    ("ir1", con_ir1, fps_ir1, f_ir1),
                    ("ir2", con_ir2, fps_ir2, f_ir2),
                ]
                for nombre, conn, fps, total in lectores_info:
                    fid, ts = metadatos[nombre]
                    if conn:
                        fid_str = str(fid) if fid is not None else "---"
                        ts_str = formatear_timestamp_ns(ts)
                        print(f"  [{nombre:<6}] ✓ FID: {fid_str:>8} | TS: {ts_str} | "
                              f"FPS: {fps:.1f} | Frames: {total}")
                    elif total == 0:
                        print(f"  [{nombre:<6}] ⟳ esperando ...")
                if any([con_color, con_depth, con_ir1, con_ir2]):
                    print(f"  [{sync_texto}]")
                ultimo_log_conexion = ahora

            # Placeholders
            if frame_color is None:
                frame_color = generar_placeholder(1920, 1080, "Esperando canal Color (RGB) ...")
            if frame_depth is None:
                frame_depth = generar_placeholder(1280, 720, "Esperando canal Profundidad ...")
            if frame_ir1 is None:
                frame_ir1 = generar_placeholder(1280, 720, "Esperando canal Infrarrojo 1 ...")
            if frame_ir2 is None:
                frame_ir2 = generar_placeholder(1280, 720, "Esperando canal Infrarrojo 2 ...")

            # Convertir grayscale a BGR
            if len(frame_ir1.shape) == 2 or frame_ir1.shape[2] == 1:
                frame_ir1 = cv2.cvtColor(frame_ir1, cv2.COLOR_GRAY2BGR)
            if len(frame_ir2.shape) == 2 or frame_ir2.shape[2] == 1:
                frame_ir2 = cv2.cvtColor(frame_ir2, cv2.COLOR_GRAY2BGR)

            # ─── Componer visualización ───
            canvas_mostrar = None
            titulo_ventana = NOMBRE_VENTANA

            fid_c, ts_c = metadatos["color"]
            fid_d, ts_d = metadatos["depth"]
            fid_1, ts_1 = metadatos["ir1"]
            fid_2, ts_2 = metadatos["ir2"]

            if modo_vista == "mosaico":
                titulo_ventana = f"{NOMBRE_VENTANA} — Mosaico [m]"

                ir1_resized = cv2.resize(frame_ir1, (640, 360), interpolation=cv2.INTER_LANCZOS4)
                depth_resized = cv2.resize(frame_depth, (640, 360), interpolation=cv2.INTER_LANCZOS4)
                ir2_resized = cv2.resize(frame_ir2, (640, 360), interpolation=cv2.INTER_LANCZOS4)

                if hud_visible:
                    dibujar_hud_panel(frame_color, "Color (RGB)", fps_color, f_color,
                                     segundos, "1920x1080", (0, 255, 0), "bottom-right",
                                     fid_c, ts_c)
                    dibujar_hud_panel(ir1_resized, "IR1 (Left)", fps_ir1, f_ir1,
                                     segundos, "1280x720", (0, 255, 0), "top-left",
                                     fid_1, ts_1)
                    dibujar_hud_panel(depth_resized, "Profundidad", fps_depth, f_depth,
                                     segundos, "1280x720", (0, 255, 0), "top-left",
                                     fid_d, ts_d)
                    dibujar_hud_panel(ir2_resized, "IR2 (Right)", fps_ir2, f_ir2,
                                     segundos, "1280x720", (0, 255, 0), "top-left",
                                     fid_2, ts_2)

                bottom_row = np.hstack([ir1_resized, depth_resized, ir2_resized])
                canvas_mostrar = np.vstack([frame_color, bottom_row])

            elif modo_vista == "color":
                titulo_ventana = f"{NOMBRE_VENTANA} — Color (RGB 1920x1080) [1]"
                if hud_visible:
                    dibujar_hud_panel(frame_color, "Color (RGB) - Nativa", fps_color, f_color,
                                     segundos, "1920x1080", (0, 255, 0), "bottom-right",
                                     fid_c, ts_c)
                canvas_mostrar = frame_color

            elif modo_vista == "ir1":
                titulo_ventana = f"{NOMBRE_VENTANA} — IR1 (Left 1280x720) [2]"
                if hud_visible:
                    dibujar_hud_panel(frame_ir1, "IR1 (Izquierdo) - Nativa", fps_ir1, f_ir1,
                                     segundos, "1280x720", (0, 255, 0), "top-left",
                                     fid_1, ts_1)
                canvas_mostrar = frame_ir1

            elif modo_vista == "depth":
                titulo_ventana = f"{NOMBRE_VENTANA} — Profundidad (Depth 1280x720) [3]"
                if hud_visible:
                    dibujar_hud_panel(frame_depth, "Profundidad (JET) - Nativa", fps_depth, f_depth,
                                     segundos, "1280x720", (0, 255, 0), "top-left",
                                     fid_d, ts_d)
                canvas_mostrar = frame_depth

            elif modo_vista == "ir2":
                titulo_ventana = f"{NOMBRE_VENTANA} — IR2 (Right 1280x720) [4]"
                if hud_visible:
                    dibujar_hud_panel(frame_ir2, "IR2 (Derecho) - Nativa", fps_ir2, f_ir2,
                                     segundos, "1280x720", (0, 255, 0), "top-left",
                                     fid_2, ts_2)
                canvas_mostrar = frame_ir2

            # Mostrar
            if canvas_mostrar is not None:
                # Barra de sincronía inferior
                bar_h = 26
                alto_c, ancho_c = canvas_mostrar.shape[:2]
                overlay = canvas_mostrar.copy()
                cv2.rectangle(overlay, (0, alto_c - bar_h), (ancho_c, alto_c), (15, 15, 15), -1)
                cv2.addWeighted(overlay, 0.7, canvas_mostrar, 0.3, 0, canvas_mostrar)

                fuente = cv2.FONT_HERSHEY_SIMPLEX
                y_bar = alto_c - 7

                # Izquierda: controles
                cv2.putText(canvas_mostrar,
                            "[M] Mosaico [1-4] Canal [H] HUD [F] Full [Q] Salir",
                            (10, y_bar), fuente, 0.38, (150, 150, 150), 1, cv2.LINE_AA)

                # Derecha: sincronía
                color_sync = (0, 200, 100) if sync_ok else (0, 100, 255)
                fps_prom = (fps_color + fps_depth + fps_ir1 + fps_ir2) / 4
                info_txt = f"{sync_texto}  |  {modo_vista.upper()}  |  {fps_prom:.0f} fps"
                tam = cv2.getTextSize(info_txt, fuente, 0.38, 1)[0]
                cv2.putText(canvas_mostrar, info_txt,
                            (ancho_c - tam[0] - 10, y_bar),
                            fuente, 0.38, color_sync, 1, cv2.LINE_AA)

                # Letterbox
                try:
                    ancho_win = int(cv2.getWindowImageRect(NOMBRE_VENTANA)[2])
                    alto_win = int(cv2.getWindowImageRect(NOMBRE_VENTANA)[3])
                    if ancho_win > 0 and alto_win > 0:
                        canvas_mostrar = letterbox(canvas_mostrar, ancho_win, alto_win)
                except Exception:
                    pass

                cv2.imshow(NOMBRE_VENTANA, canvas_mostrar)
                try:
                    cv2.setWindowTitle(NOMBRE_VENTANA, titulo_ventana)
                except Exception:
                    pass

            # ─── Entrada de teclado ───
            tecla = cv2.waitKey(10) & 0xFF
            if tecla == ord('q') or tecla == ord('Q') or tecla == 27:
                print("\n  ⏹ Receptor detenido por el usuario.")
                break
            elif tecla == ord('m') or tecla == ord('M'):
                modo_vista = "mosaico"
                print("  → Vista cambiada a Mosaico")
            elif tecla == ord('1'):
                modo_vista = "color"
                print("  → Vista exclusiva: Color (RGB 1920x1080)")
            elif tecla == ord('2'):
                modo_vista = "ir1"
                print("  → Vista exclusiva: Infrarrojo 1 (Left 1280x720)")
            elif tecla == ord('3'):
                modo_vista = "depth"
                print("  → Vista exclusiva: Profundidad (Depth 1280x720)")
            elif tecla == ord('4'):
                modo_vista = "ir2"
                print("  → Vista exclusiva: Infrarrojo 2 (Right 1280x720)")
            elif tecla == ord('h') or tecla == ord('H'):
                hud_visible = not hud_visible
                print(f"  → HUD: {'visible' if hud_visible else 'oculto'}")
            elif tecla == ord('f') or tecla == ord('F'):
                pantalla_completa = not pantalla_completa
                if pantalla_completa:
                    cv2.setWindowProperty(NOMBRE_VENTANA, cv2.WND_PROP_FULLSCREEN,
                                          cv2.WINDOW_FULLSCREEN)
                    print("  → Pantalla completa activada")
                else:
                    cv2.setWindowProperty(NOMBRE_VENTANA, cv2.WND_PROP_FULLSCREEN,
                                          cv2.WINDOW_NORMAL)
                    print("  → Pantalla completa desactivada")

            # Cierre por botón X
            if cv2.getWindowProperty(NOMBRE_VENTANA, cv2.WND_PROP_VISIBLE) < 1:
                print("\n  ⏹ Ventana cerrada por el usuario.")
                break

    except KeyboardInterrupt:
        print("\n\n  ⏹ Receptor detenido por teclado (Ctrl+C).")

    finally:
        print("\n  Deteniendo hilos de recepción...")
        reader_color.stop()
        reader_depth.stop()
        reader_ir1.stop()
        reader_ir2.stop()
        cv2.destroyAllWindows()
        print("  ✓ Hilos detenidos y recursos liberados.")
        print("\n  Receptor finalizado.\n")


# ═══════════════════════════════════════════════════════════════════════════
# PUNTO DE ENTRADA
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Receptor RTSP v3 con extracción LSB para RealSense D435 — Windows.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos de uso:
  python receptor.py 192.168.1.42                         # IP del emisor
  python receptor.py 192.168.1.42 8554                    # IP y puerto
  python receptor.py rtsp://192.168.1.42:8554/color       # URL directa
  python receptor.py --sin-hud 192.168.1.42               # Sin overlay

Controles en la ventana:
  M → Mosaico    1 → Color   2 → IR1    3 → Depth   4 → IR2
  H → HUD on/off  F → Fullscreen  Q / ESC → Salir

HUD muestra: Frame ID real (LSB), Timestamp del emisor, Latencia,
             y estado de sincronía entre los 4 canales.
        """
    )

    parser.add_argument("destino", nargs="?", default=None,
                        help="URL RTSP completa o dirección IP del emisor")
    parser.add_argument("puerto", nargs="?", type=int, default=PUERTO_RTSP_DEFECTO,
                        help=f"Puerto del servidor RTSP (por defecto: {PUERTO_RTSP_DEFECTO})")
    parser.add_argument("--sin-hud", action="store_true",
                        help="No mostrar información de estado (HUD) sobre el vídeo")

    args = parser.parse_args()

    ip_destino = "127.0.0.1"
    puerto_destino = args.puerto

    if args.destino is not None:
        if args.destino.startswith("rtsp://"):
            sin_protocolo = args.destino[7:]
            if "/" in sin_protocolo:
                host_puerto = sin_protocolo.split("/")[0]
            else:
                host_puerto = sin_protocolo

            if ":" in host_puerto:
                parts = host_puerto.split(":")
                ip_destino = parts[0]
                try:
                    puerto_destino = int(parts[1])
                except ValueError:
                    pass
            else:
                ip_destino = host_puerto
        else:
            ip_destino = args.destino

    iniciar_receptor(ip_destino, puerto_destino, mostrar_hud_info=not args.sin_hud)
