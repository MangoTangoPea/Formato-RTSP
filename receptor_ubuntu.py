#!/usr/bin/env python3
"""
Receptor RTSP Multicanal — Ubuntu/Linux Nativo (v2).

Rediseñado desde cero para funcionar de forma nativa en Linux.
Se conecta a 4 streams RTSP independientes del emisor RealSense D435
(Color, Depth, IR1, IR2) y ofrece múltiples modos de visualización.

Controles de teclado:
  m  → Mosaico (4 vistas combinadas)
  1  → Solo Color (RGB 1920x1080)
  2  → Solo Infrarrojo 1 (Left)
  3  → Solo Profundidad (Depth heatmap)
  4  → Solo Infrarrojo 2 (Right)
  h  → Mostrar/ocultar HUD (información en pantalla)
  f  → Pantalla completa on/off
  q / ESC → Salir

Uso:
    python3 receptor_ubuntu.py <IP_DEL_EMISOR>
    python3 receptor_ubuntu.py <IP> <PUERTO>
    python3 receptor_ubuntu.py rtsp://<IP>:<PUERTO>/color
    python3 receptor_ubuntu.py --sin-hud <IP>
"""

import sys
import time
import argparse
import os
import threading

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


# ═══════════════════════════════════════════════════════════════════════════
# CONSTANTES
# ═══════════════════════════════════════════════════════════════════════════

PUERTO_RTSP_DEFECTO = 8554
NOMBRE_VENTANA = "Receptor RTSP — RealSense D435 (Ubuntu v2)"

# Nombres y colores temáticos para cada canal
CANALES_INFO = {
    "color": {"titulo": "Color (RGB)",     "color": (0, 200, 100), "res": "1920×1080"},
    "depth": {"titulo": "Profundidad",     "color": (0, 140, 255), "res": "1280×720"},
    "ir1":   {"titulo": "Infrarrojo 1 (L)","color": (200, 200, 0), "res": "1280×720"},
    "ir2":   {"titulo": "Infrarrojo 2 (R)","color": (200, 100, 200),"res": "1280×720"},
}


# ═══════════════════════════════════════════════════════════════════════════
# LECTOR DE STREAM RTSP (hilo dedicado)
# ═══════════════════════════════════════════════════════════════════════════

class LectorRTSP:
    """
    Lector asíncrono de un flujo RTSP usando un hilo dedicado.
    Mantiene solo el frame más reciente para evitar acumulación de buffer.
    Implementa reconexión automática con backoff.
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
        """Bucle interno del hilo: conecta, lee frames, reconecta si falla."""
        # Forzar transporte TCP para OpenCV
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"

        cap = None
        backoff = 0.5  # segundos de espera entre reintentos

        while self.corriendo:
            # Intentar conexión
            if cap is None or not cap.isOpened():
                with self.lock:
                    self.conectado = False
                cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
                if cap.isOpened():
                    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                    with self.lock:
                        self.conectado = True
                    backoff = 0.5  # reset backoff
                else:
                    time.sleep(min(backoff, 5.0))
                    backoff = min(backoff * 1.5, 5.0)
                    continue

            # Leer frame
            ret, frame = cap.read()
            if not ret or frame is None:
                cap.release()
                cap = None
                with self.lock:
                    self.conectado = False
                time.sleep(0.3)
                continue

            with self.lock:
                self.frame = frame
                self.frames_recibidos += 1
                self._contador_fps += 1

            # Calcular FPS cada segundo
            ahora = time.time()
            dt = ahora - self._t_fps
            if dt >= 1.0:
                with self.lock:
                    self.fps = self._contador_fps / dt
                    self._contador_fps = 0
                    self._t_fps = ahora

        # Limpiar al salir del bucle
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

    def detener(self):
        """Detiene el hilo de lectura."""
        self.corriendo = False
        if self.hilo:
            self.hilo.join(timeout=2.0)


# ═══════════════════════════════════════════════════════════════════════════
# FUNCIONES DE RENDERIZADO
# ═══════════════════════════════════════════════════════════════════════════

def crear_placeholder(ancho, alto, texto, subtexto=""):
    """Crea un frame negro con texto centrado (para canales sin datos)."""
    img = np.zeros((alto, ancho, 3), dtype=np.uint8)
    fuente = cv2.FONT_HERSHEY_SIMPLEX

    # Texto principal
    tam = cv2.getTextSize(texto, fuente, 0.6, 1)[0]
    x = (ancho - tam[0]) // 2
    y = (alto + tam[1]) // 2 - 10
    cv2.putText(img, texto, (x, y), fuente, 0.6, (80, 80, 180), 1, cv2.LINE_AA)

    # Subtexto
    if subtexto:
        tam2 = cv2.getTextSize(subtexto, fuente, 0.4, 1)[0]
        x2 = (ancho - tam2[0]) // 2
        cv2.putText(img, subtexto, (x2, y + 25), fuente, 0.4, (100, 100, 100), 1, cv2.LINE_AA)

    return img


def dibujar_hud(frame, titulo, fps, total_frames, segundos, color_tema=(0, 200, 100)):
    """
    Dibuja un HUD semitransparente en la esquina superior izquierda del frame.
    """
    alto, ancho = frame.shape[:2]

    # Dimensiones adaptativas
    if ancho >= 1200:
        bw, bh, escala, salto = 320, 72, 0.45, 20
    elif ancho >= 800:
        bw, bh, escala, salto = 260, 64, 0.4, 18
    else:
        bw, bh, escala, salto = 200, 56, 0.35, 16

    # Fondo semitransparente
    overlay = frame.copy()
    cv2.rectangle(overlay, (8, 8), (8 + bw, 8 + bh), (20, 20, 20), -1)
    cv2.addWeighted(overlay, 0.65, frame, 0.35, 0, frame)

    fuente = cv2.FONT_HERSHEY_SIMPLEX
    x0, y0 = 16, 8 + salto

    # Línea 1: Título del canal
    cv2.putText(frame, titulo, (x0, y0), fuente, escala, (255, 255, 255), 1, cv2.LINE_AA)

    # Línea 2: FPS y frames
    cv2.putText(frame, f"FPS: {fps:.1f}  |  Frames: {total_frames}",
                (x0, y0 + salto), fuente, escala * 0.9, color_tema, 1, cv2.LINE_AA)

    # Línea 3: Tiempo
    cv2.putText(frame, f"Tiempo: {int(segundos)}s",
                (x0, y0 + 2 * salto), fuente, escala * 0.9, color_tema, 1, cv2.LINE_AA)


def dibujar_barra_estado(canvas, modo, segundos, fps_total):
    """Dibuja una barra de estado en la parte inferior del canvas."""
    alto, ancho = canvas.shape[:2]
    bar_h = 28

    overlay = canvas.copy()
    cv2.rectangle(overlay, (0, alto - bar_h), (ancho, alto), (15, 15, 15), -1)
    cv2.addWeighted(overlay, 0.7, canvas, 0.3, 0, canvas)

    fuente = cv2.FONT_HERSHEY_SIMPLEX
    escala = 0.4
    y_txt = alto - 8

    # Izquierda: controles
    controles = "[M] Mosaico  [1] Color  [2] IR1  [3] Depth  [4] IR2  [H] HUD  [F] Fullscreen  [Q] Salir"
    cv2.putText(canvas, controles, (10, y_txt), fuente, escala, (150, 150, 150), 1, cv2.LINE_AA)

    # Derecha: info
    info = f"Vista: {modo.upper()}  |  {int(segundos)}s  |  {fps_total:.0f} fps"
    tam = cv2.getTextSize(info, fuente, escala, 1)[0]
    cv2.putText(canvas, info, (ancho - tam[0] - 10, y_txt), fuente, escala, (0, 200, 100), 1, cv2.LINE_AA)


# ═══════════════════════════════════════════════════════════════════════════
# FUNCIÓN PRINCIPAL DEL RECEPTOR
# ═══════════════════════════════════════════════════════════════════════════

def iniciar_receptor(ip, puerto, mostrar_hud=True):
    """
    Receptor RTSP v2: se conecta a 4 streams independientes del emisor
    RealSense y ofrece visualización flexible con mosaico y vistas individuales.
    """
    urls = {
        "color": f"rtsp://{ip}:{puerto}/color",
        "depth": f"rtsp://{ip}:{puerto}/depth",
        "ir1":   f"rtsp://{ip}:{puerto}/ir1",
        "ir2":   f"rtsp://{ip}:{puerto}/ir2",
    }

    print("\n" + "═" * 62)
    print("  RECEPTOR RTSP — RealSense D435 · Ubuntu Nativo (v2)")
    print("═" * 62)
    for nombre, url in urls.items():
        print(f"  {nombre:<6} → {url}")
    print("─" * 62)
    print("  Controles: [M]osaico [1]Color [2]IR1 [3]Depth [4]IR2")
    print("             [H]UD on/off  [F]ullscreen  [Q]Salir")
    print("═" * 62)

    # Crear lectores
    lectores = {}
    for nombre, url in urls.items():
        lectores[nombre] = LectorRTSP(url, nombre)
        lectores[nombre].iniciar()

    print("\n  Conectando a los flujos RTSP ...\n")

    # Crear ventana OpenCV con soporte para GUI nativa de Linux
    flags = cv2.WINDOW_NORMAL
    try:
        flags |= cv2.WINDOW_GUI_NORMAL  # Oculta toolbar de Qt/GTK en Linux
    except AttributeError:
        pass  # No disponible en todas las compilaciones de OpenCV

    cv2.namedWindow(NOMBRE_VENTANA, flags)
    cv2.resizeWindow(NOMBRE_VENTANA, 1440, 1080)

    # Estado de la interfaz
    modo = "mosaico"  # mosaico, color, ir1, depth, ir2
    hud_visible = mostrar_hud
    pantalla_completa = False
    t_inicio = time.time()
    ultimo_log_conexion = 0

    try:
        while True:
            ahora = time.time()
            dt = ahora - t_inicio

            # ─── Obtener frames de cada canal ──────────────────────────
            frames = {}
            estados = {}
            for nombre, lector in lectores.items():
                frames[nombre] = lector.obtener_frame()
                estados[nombre] = lector.obtener_estado()

            # Log de estado de conexión cada 5 segundos
            if ahora - ultimo_log_conexion > 5:
                for nombre, (conn, fps, total) in estados.items():
                    estado = "✓ conectado" if conn else "⟳ esperando"
                    if conn:
                        print(f"  [{nombre:<6}] {estado} | FPS: {fps:.1f} | Frames: {total}")
                    elif total == 0:
                        print(f"  [{nombre:<6}] {estado} ...")
                ultimo_log_conexion = ahora

            # ─── Preparar frames (con placeholders si no hay datos) ────
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

            # Convertir grayscale a BGR si es necesario
            if len(f_ir1.shape) == 2:
                f_ir1 = cv2.cvtColor(f_ir1, cv2.COLOR_GRAY2BGR)
            elif f_ir1.shape[2] == 1:
                f_ir1 = cv2.cvtColor(f_ir1, cv2.COLOR_GRAY2BGR)
            if len(f_ir2.shape) == 2:
                f_ir2 = cv2.cvtColor(f_ir2, cv2.COLOR_GRAY2BGR)
            elif f_ir2.shape[2] == 1:
                f_ir2 = cv2.cvtColor(f_ir2, cv2.COLOR_GRAY2BGR)

            # ─── Componer la vista según el modo seleccionado ──────────
            canvas = None

            if modo == "mosaico":
                # Layout:
                #   ┌──────────────────────────────────────────┐
                #   │          Color (RGB) 1920×1080           │
                #   ├──────────────┬──────────────┬────────────┤
                #   │  IR1 640×360 │ Depth 640×360│ IR2 640×360│
                #   └──────────────┴──────────────┴────────────┘

                ir1_small = cv2.resize(f_ir1, (640, 360), interpolation=cv2.INTER_LINEAR)
                depth_small = cv2.resize(f_depth, (640, 360), interpolation=cv2.INTER_LINEAR)
                ir2_small = cv2.resize(f_ir2, (640, 360), interpolation=cv2.INTER_LINEAR)

                if hud_visible:
                    _, fps_c, tot_c = estados["color"]
                    _, fps_d, tot_d = estados["depth"]
                    _, fps_1, tot_1 = estados["ir1"]
                    _, fps_2, tot_2 = estados["ir2"]

                    dibujar_hud(f_color, "Color (RGB) 1920×1080", fps_c, tot_c, dt,
                                CANALES_INFO["color"]["color"])
                    dibujar_hud(ir1_small, "IR1 (Left)", fps_1, tot_1, dt,
                                CANALES_INFO["ir1"]["color"])
                    dibujar_hud(depth_small, "Profundidad", fps_d, tot_d, dt,
                                CANALES_INFO["depth"]["color"])
                    dibujar_hud(ir2_small, "IR2 (Right)", fps_2, tot_2, dt,
                                CANALES_INFO["ir2"]["color"])

                fila_inferior = np.hstack([ir1_small, depth_small, ir2_small])
                canvas = np.vstack([f_color, fila_inferior])

            elif modo in ("color", "depth", "ir1", "ir2"):
                mapa_frames = {"color": f_color, "depth": f_depth, "ir1": f_ir1, "ir2": f_ir2}
                canvas = mapa_frames[modo]

                if hud_visible:
                    info = CANALES_INFO[modo]
                    conn, fps, total = estados[modo]
                    dibujar_hud(canvas, f"{info['titulo']} — {info['res']}",
                                fps, total, dt, info["color"])

            if canvas is not None:
                # Barra de estado inferior
                dibujar_barra_estado(canvas, modo, dt,
                                     sum(s[1] for s in estados.values()) / 4)

                cv2.imshow(NOMBRE_VENTANA, canvas)

            # ─── Entrada de teclado ────────────────────────────────────
            tecla = cv2.waitKey(10) & 0xFF

            if tecla == ord('q') or tecla == ord('Q') or tecla == 27:
                print("\n  ⏹ Receptor detenido por el usuario.")
                break
            elif tecla == ord('m') or tecla == ord('M'):
                modo = "mosaico"
                print("  → Vista: Mosaico (4 canales)")
            elif tecla == ord('1'):
                modo = "color"
                print("  → Vista: Color (RGB 1920×1080)")
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
                    cv2.setWindowProperty(NOMBRE_VENTANA, cv2.WND_PROP_FULLSCREEN,
                                          cv2.WINDOW_FULLSCREEN)
                    print("  → Pantalla completa activada")
                else:
                    cv2.setWindowProperty(NOMBRE_VENTANA, cv2.WND_PROP_FULLSCREEN,
                                          cv2.WINDOW_NORMAL)
                    print("  → Pantalla completa desactivada")

            # Detectar cierre de la ventana con el botón X
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
        print("  ✓ Recursos liberados.\n")


# ═══════════════════════════════════════════════════════════════════════════
# PUNTO DE ENTRADA
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Receptor RTSP v2 para Intel RealSense D435 — Ubuntu/Linux nativo.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  python3 receptor_ubuntu.py 192.168.1.42             # IP del emisor
  python3 receptor_ubuntu.py 192.168.1.42 9554        # Puerto personalizado
  python3 receptor_ubuntu.py rtsp://192.168.1.42:8554/color   # URL directa
  python3 receptor_ubuntu.py --sin-hud 192.168.1.42   # Sin overlay de info
  python3 receptor_ubuntu.py 127.0.0.1                # Prueba local

Controles en la ventana:
  M → Mosaico (4 vistas)    1 → Color    2 → IR1
  3 → Depth                 4 → IR2      H → HUD on/off
  F → Fullscreen on/off     Q / ESC → Salir
        """
    )

    parser.add_argument("destino", nargs="?", default=None,
                        help="IP del emisor o URL RTSP completa")
    parser.add_argument("puerto", nargs="?", type=int, default=PUERTO_RTSP_DEFECTO,
                        help=f"Puerto RTSP (defecto: {PUERTO_RTSP_DEFECTO})")
    parser.add_argument("--sin-hud", action="store_true",
                        help="No mostrar información de estado sobre el vídeo")

    args = parser.parse_args()

    # Parsear destino
    ip = "127.0.0.1"
    puerto = args.puerto

    if args.destino is not None:
        if args.destino.startswith("rtsp://"):
            # Extraer IP y puerto de una URL RTSP
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
        print("  Uso: python3 receptor_ubuntu.py <IP_DEL_EMISOR>")

    iniciar_receptor(ip, puerto, mostrar_hud=not args.sin_hud)