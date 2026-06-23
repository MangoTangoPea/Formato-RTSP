#!/usr/bin/env python3
"""
Receptor RTSP Multicanal: se conecta a 4 flujos RTSP independientes de la
cámara Intel RealSense D435 (Color, IR1, IR2, Depth) usando hilos dedicados
y los muestra en tiempo real con controles interactivos en OpenCV.

Arquitectura de recepción:
──────────────────────────
  ┌─────────────────┐      RTSP/RTP (/color) ┌───────────────────┐
  │                 │ ─────────────────────► │ Reader-Color      │ ──┐
  │                 │      RTSP/RTP (/depth) ├───────────────────┤   │
  │  RealSense      │ ─────────────────────► │ Reader-Depth      │ ──┼─► [Bucle Principal]
  │  D435 (emisor)  │      RTSP/RTP (/ir1)   ├───────────────────┤   │   - Composición local
  │                 │ ─────────────────────► │ Reader-IR1        │ ──┤   - Render OSD nítido
  │                 │      RTSP/RTP (/ir2)   ├───────────────────┤   │   - Cambio de vistas
  │                 │ ─────────────────────► │ Reader-IR2        │ ──┘
  └─────────────────┘                        └───────────────────┘

Teclas de control en la ventana:
  - 'm' o 'M': Modo Mosaico (RGB superior, IR1/Depth/IR2 inferiores).
  - '1': Vista exclusiva RGB (resolución nativa 1920x1080).
  - '2': Vista exclusiva IR1 (resolución nativa 1280x720).
  - '3': Vista exclusiva Profundidad (resolución nativa 1280x720).
  - '4': Vista exclusiva IR2 (resolución nativa 1280x720).
  - 'q' o 'Q' o ESC: Salir.
"""

import sys
import time
import argparse
import os
import threading

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
NOMBRE_VENTANA = "Receptor RTSP — RealSense D435"


# ═══════════════════════════════════════════════════════════════════════════
# CLASE LECTORA MULTIHILO
# ═══════════════════════════════════════════════════════════════════════════

class RTSPStreamReader:
    """
    Lector de flujo RTSP que se ejecuta en un hilo secundario de forma asíncrona.
    Mantiene siempre el fotograma más reciente en memoria, descartando los
    anteriores para evitar la acumulación de búfer y la latencia.
    """
    def __init__(self, url, nombre):
        self.url = url
        self.nombre = nombre
        self.cap = None
        self.frame = None
        self.running = False
        self.thread = None
        self.lock = threading.Lock()
        
        # Estadísticas de canal
        self.fotogramas_recibidos = 0
        self.fps = 0.0
        self.ultimo_tiempo_fps = time.time()
        self.conectado = False
        
    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._update, name=f"Reader-{self.nombre}", daemon=True)
        self.thread.start()
        
    def _update(self):
        # Forzar transporte TCP
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
        
        while self.running:
            if self.cap is None or not self.cap.isOpened():
                self.conectado = False
                self.cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
                if self.cap.isOpened():
                    self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                    self.conectado = True
                else:
                    time.sleep(1)
                    continue
                    
            ret, frame = self.cap.read()
            if not ret or frame is None:
                # Reintentar conexión en la siguiente iteración
                self.cap.release()
                self.cap = None
                self.conectado = False
                continue
                
            with self.lock:
                self.frame = frame
                self.fotogramas_recibidos += 1
                
            # Calcular FPS del canal cada 30 frames
            if self.fotogramas_recibidos % 30 == 0:
                ahora = time.time()
                delta = ahora - self.ultimo_tiempo_fps
                if delta > 0:
                    self.fps = 30 / delta
                self.ultimo_tiempo_fps = ahora
                
    def get_frame(self):
        with self.lock:
            return self.frame.copy() if self.frame is not None else None
            
    def get_info(self):
        with self.lock:
            return self.conectado, self.fps, self.fotogramas_recibidos
            
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
    """
    Genera un frame negro con un texto descriptivo centrado.
    """
    img = np.zeros((alto, ancho, 3), dtype=np.uint8)
    fuente = cv2.FONT_HERSHEY_SIMPLEX
    escala = 0.6
    grosor = 2
    
    # Dibujar mensaje centrado
    t_size = cv2.getTextSize(texto, fuente, escala, grosor)[0]
    tx = (ancho - t_size[0]) // 2
    ty = (alto + t_size[1]) // 2
    
    cv2.putText(img, texto, (tx, ty), fuente, escala, (0, 0, 180), grosor, cv2.LINE_AA)
    return img


def dibujar_hud_panel(frame, titulo, fps, frames, segundos, resolucion, color_tema=(0, 255, 0), posicion="top-left"):
    """
    Dibuja una barra de estado OSD semitransparente sobre un panel de vídeo.
    Se adapta dinámicamente al tamaño del frame.
    """
    alto, ancho = frame.shape[:2]
    
    # Adaptar tamaños de caja y letra según la resolución del panel
    if ancho >= 1000:
        box_ancho = 340
        box_alto = 80
        escala = 0.5
        grosor = 1
        salto = 22
    else:
        box_ancho = 250
        box_alto = 66
        escala = 0.4
        grosor = 1
        salto = 18

    # Calcular coordenadas de la caja
    if posicion == "bottom-right":
        box_x2 = ancho - 10
        box_y2 = alto - 10
        box_x1 = box_x2 - box_ancho
        box_y1 = box_y2 - box_alto
    else: # top-left
        box_x1 = 10
        box_y1 = 10
        box_x2 = box_x1 + box_ancho
        box_y2 = box_y1 + box_alto

    # Dibujar fondo de la barra de estado
    overlay = frame.copy()
    cv2.rectangle(overlay, (box_x1, box_y1), (box_x2, box_y2), (30, 30, 30), -1)
    cv2.addWeighted(overlay, 0.6, frame, 0.4, 0, frame)

    fuente = cv2.FONT_HERSHEY_SIMPLEX
    color_titulo = (0, 0, 255)   # Rojo

    # Línea 1: Título
    cv2.putText(frame, titulo, (box_x1 + 10, box_y1 + salto),
                fuente, escala, color_titulo, grosor, cv2.LINE_AA)
    # Línea 2: FPS y frames
    cv2.putText(frame, f"FPS: {fps:.1f} | Frames: {frames}", (box_x1 + 10, box_y1 + salto + salto),
                fuente, escala, color_tema, grosor, cv2.LINE_AA)
    # Línea 3: Tiempo y resolución
    cv2.putText(frame, f"Tiempo: {int(segundos)}s | {resolucion}", (box_x1 + 10, box_y1 + salto + 2 * salto),
                fuente, escala, color_tema, grosor, cv2.LINE_AA)


# ═══════════════════════════════════════════════════════════════════════════
# FUNCIÓN PRINCIPAL DEL RECEPTOR
# ═══════════════════════════════════════════════════════════════════════════

def iniciar_receptor(url_base_ip, puerto, mostrar_hud_info=True):
    """
    Inicializa los hilos de recepción y coordina el bucle de visualización.
    """
    url_color = f"rtsp://{url_base_ip}:{puerto}/color"
    url_depth = f"rtsp://{url_base_ip}:{puerto}/depth"
    url_ir1 = f"rtsp://{url_base_ip}:{puerto}/ir1"
    url_ir2 = f"rtsp://{url_base_ip}:{puerto}/ir2"

    print("\n" + "═" * 60)
    print("  RECEPTOR RTSP MULTICANAL — RealSense D435")
    print("═" * 60)
    print(f"  Color (RGB):   {url_color}")
    print(f"  Depth Map:     {url_depth}")
    print(f"  Infrared 1:    {url_ir1}")
    print(f"  Infrared 2:    {url_ir2}")
    print(f"  Ventana:       Redimensionable (cv2.WINDOW_NORMAL)")
    print("═" * 60)
    print("\n  Atajos de teclado en la ventana de vídeo:")
    print("    [m] Modo Mosaico (Completo 4 streams)")
    print("    [1] Vista exclusiva RGB (1920x1080)")
    print("    [2] Vista exclusiva Infrarrojo 1 (1280x720)")
    print("    [3] Vista exclusiva Profundidad (1280x720)")
    print("    [4] Vista exclusiva Infrarrojo 2 (1280x720)")
    print("    [q] Salir\n")

    # Iniciar los hilos lectores de flujos
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
    modo_vista = "mosaico"  # mosaico, color, ir1, depth, ir2
    tiempo_inicio = time.time()

    try:
        while True:
            ahora = time.time()
            segundos_transcurridos = ahora - tiempo_inicio

            # ─── 1. Obtener frames más recientes ───
            frame_color = reader_color.get_frame()
            frame_depth = reader_depth.get_frame()
            frame_ir1 = reader_ir1.get_frame()
            frame_ir2 = reader_ir2.get_frame()

            # Obtener estados de conexión e info
            con_color, fps_color, f_color = reader_color.get_info()
            con_depth, fps_depth, f_depth = reader_depth.get_info()
            con_ir1, fps_ir1, f_ir1 = reader_ir1.get_info()
            con_ir2, fps_ir2, f_ir2 = reader_ir2.get_info()

            # Rellenar con placeholders si los flujos no están disponibles
            if frame_color is None:
                frame_color = generar_placeholder(1920, 1080, "Esperando canal Color (RGB) ...")
            if frame_depth is None:
                frame_depth = generar_placeholder(1280, 720, "Esperando canal Profundidad ...")
            if frame_ir1 is None:
                frame_ir1 = generar_placeholder(1280, 720, "Esperando canal Infrarrojo 1 ...")
            if frame_ir2 is None:
                frame_ir2 = generar_placeholder(1280, 720, "Esperando canal Infrarrojo 2 ...")

            # Garantizar que todos los frames tengan 3 canales para poder mezclar y dibujar OSD en color
            if len(frame_ir1.shape) == 2 or frame_ir1.shape[2] == 1:
                frame_ir1 = cv2.cvtColor(frame_ir1, cv2.COLOR_GRAY2BGR)
            if len(frame_ir2.shape) == 2 or frame_ir2.shape[2] == 1:
                frame_ir2 = cv2.cvtColor(frame_ir2, cv2.COLOR_GRAY2BGR)

            # ─── 2. Componer la visualización elegida ───
            canvas_mostrar = None
            titulo_ventana = NOMBRE_VENTANA

            if modo_vista == "mosaico":
                titulo_ventana = f"{NOMBRE_VENTANA} — Modo Mosaico [m]"
                
                # Redimensionar paneles inferiores a 640x360 con Lanczos4 para el mosaico
                ir1_resized = cv2.resize(frame_ir1, (640, 360), interpolation=cv2.INTER_LANCZOS4)
                depth_resized = cv2.resize(frame_depth, (640, 360), interpolation=cv2.INTER_LANCZOS4)
                ir2_resized = cv2.resize(frame_ir2, (640, 360), interpolation=cv2.INTER_LANCZOS4)

                # Dibujar HUDs locales en cada panel
                if mostrar_hud_info:
                    dibujar_hud_panel(frame_color, "Color (RGB)", fps_color, f_color, segundos_transcurridos, "1920x1080", (0, 255, 0), "bottom-right")
                    dibujar_hud_panel(ir1_resized, "IR1 (Left)", fps_ir1, f_ir1, segundos_transcurridos, "1280x720", (0, 255, 0), "top-left")
                    dibujar_hud_panel(depth_resized, "Profundidad", fps_depth, f_depth, segundos_transcurridos, "1280x720", (0, 255, 0), "top-left")
                    dibujar_hud_panel(ir2_resized, "IR2 (Right)", fps_ir2, f_ir2, segundos_transcurridos, "1280x720", (0, 255, 0), "top-left")

                # Ensamblar fila inferior y lienzo final
                bottom_row = np.hstack([ir1_resized, depth_resized, ir2_resized])
                canvas_mostrar = np.vstack([frame_color, bottom_row])

            elif modo_vista == "color":
                titulo_ventana = f"{NOMBRE_VENTANA} — Color (RGB 1920x1080) [1]"
                if mostrar_hud_info:
                    dibujar_hud_panel(frame_color, "Color (RGB) - Resolucion Nativa", fps_color, f_color, segundos_transcurridos, "1920x1080", (0, 255, 0), "bottom-right")
                canvas_mostrar = frame_color

            elif modo_vista == "ir1":
                titulo_ventana = f"{NOMBRE_VENTANA} — Infrarrojo 1 (Left 1280x720) [2]"
                if mostrar_hud_info:
                    dibujar_hud_panel(frame_ir1, "IR1 (Izquierdo) - Resolucion Nativa", fps_ir1, f_ir1, segundos_transcurridos, "1280x720", (0, 255, 0), "top-left")
                canvas_mostrar = frame_ir1

            elif modo_vista == "depth":
                titulo_ventana = f"{NOMBRE_VENTANA} — Profundidad (Depth 1280x720) [3]"
                if mostrar_hud_info:
                    dibujar_hud_panel(frame_depth, "Profundidad (JET Heatmap) - Resolucion Nativa", fps_depth, f_depth, segundos_transcurridos, "1280x720", (0, 255, 0), "top-left")
                canvas_mostrar = frame_depth

            elif modo_vista == "ir2":
                titulo_ventana = f"{NOMBRE_VENTANA} — Infrarrojo 2 (Right 1280x720) [4]"
                if mostrar_hud_info:
                    dibujar_hud_panel(frame_ir2, "IR2 (Derecho) - Resolucion Nativa", fps_ir2, f_ir2, segundos_transcurridos, "1280x720", (0, 255, 0), "top-left")
                canvas_mostrar = frame_ir2

            # Mostrar el lienzo y actualizar título
            if canvas_mostrar is not None:
                cv2.imshow(NOMBRE_VENTANA, canvas_mostrar)
                try:
                    cv2.setWindowTitle(NOMBRE_VENTANA, titulo_ventana)
                except Exception:
                    pass

            # ─── 3. Gestionar entrada de teclado ───
            tecla = cv2.waitKey(10) & 0xFF  # Pequeño delay de 10ms
            if tecla == ord('q') or tecla == ord('Q') or tecla == 27:  # ESC o Q
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

            # Cerrar el bucle si el usuario cerró la ventana manualmente desde la X
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
        description="Receptor RTSP: recibe y muestra el mosaico de 4 streams de una Intel RealSense D435.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos de uso:
  python receptor.py rtsp://192.168.1.42:8554/camara      # URL completa
  python receptor.py 192.168.1.42                          # IP con puerto/ruta por defecto
  python receptor.py 192.168.1.42 8554                     # IP y puerto
  python receptor.py --sin-hud rtsp://192.168.1.42:8554/camara  # Sin overlay de info

Notas:
  - Asegúrate de que el emisor esté corriendo antes de iniciar el receptor.
  - Ambas máquinas deben estar en la misma red local.
  - Presiona 'q' en la ventana del vídeo para cerrar el receptor.
  - El mosaico recibido contiene: RGB + IR1 + Depth (heatmap) + IR2.
  - La ventana es redimensionable (arrastrar bordes para ajustar tamaño).
        """
    )

    parser.add_argument(
        "destino",
        nargs="?",
        default=None,
        help="URL RTSP completa (rtsp://IP:puerto/ruta) o dirección IP del emisor"
    )
    parser.add_argument(
        "puerto",
        nargs="?",
        type=int,
        default=PUERTO_RTSP_DEFECTO,
        help=f"Puerto del servidor RTSP (por defecto: {PUERTO_RTSP_DEFECTO})"
    )
    parser.add_argument(
        "--sin-hud",
        action="store_true",
        help="No mostrar información de estado (HUD) sobre el vídeo"
    )

    args = parser.parse_args()

    # Resolver IP de destino a partir de los argumentos
    ip_destino = "127.0.0.1"
    puerto_destino = args.puerto

    if args.destino is not None:
        if args.destino.startswith("rtsp://"):
            # Extraer IP y puerto de la URL completa rtsp://IP:puerto/ruta
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

    # Iniciar el receptor
    iniciar_receptor(ip_destino, puerto_destino, mostrar_hud_info=not args.sin_hud)
