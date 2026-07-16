#!/usr/bin/env python3
"""
Aplicación Principal Receptora: receptor_app.py
Conecta a 4 streams RTSP en MediaMTX, extrae esteganografía LSB,
renderiza un mosaico HUD interactivo y graba en caliente presionando la tecla 'r'.
"""

import sys
import os
import time
import argparse
import threading
import datetime
import subprocess
import shutil
import cv2
import numpy as np

# Importar módulo local de esteganografía
from esteganografia import extraer_lsb

# Constantes del Receptor
NOMBRE_VENTANA = "Receptor RTSP RealSense D435 — HUD & Grabacion en Caliente"
MOSAICO_ANCHO = 1920
MOSAICO_ALTO = 1440

class LectorCanalRTSP:
    """
    Clase para leer de forma asíncrona un flujo RTSP de OpenCV.
    Mantiene siempre el último frame capturado para evitar la acumulación de búferes
    y garantizar latencia ultra baja en la visualización.
    """
    def __init__(self, url, nombre):
        self.url = url
        self.nombre = nombre
        self.cap = None
        self.frame_actual = None
        self.frame_id = None
        self.timestamp_ns = None
        self.fps = 0.0
        self.total_frames = 0
        self.corriendo = False
        self.hilo = None
        self.bloqueo = threading.Lock()

    def iniciar(self):
        self.corriendo = True
        self.hilo = threading.Thread(target=self._bucle_lectura, name=f"Lector_{self.nombre}", daemon=True)
        self.hilo.start()

    def _bucle_lectura(self):
        print(f"  → Conectando a {self.nombre}: {self.url} ...")
        
        # Parámetros para forzar transporte TCP y bajar latencia
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
        self.cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
        
        if not self.cap.isOpened():
            print(f"  ✗ Falló conexión inicial a {self.nombre}. Reintentando en bucle...")

        ultimo_tiempo = time.time()
        contador_frames = 0

        while self.corriendo:
            if not self.cap or not self.cap.isOpened():
                time.sleep(1.0)
                self.cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
                continue

            ret, frame = self.cap.read()
            if not ret or frame is None:
                # Pérdida de conexión o frame vacío
                time.sleep(0.01)
                continue

            # Extracción inmediata de LSB
            fid, ts = extraer_lsb(frame)

            # Actualización segura de frame y metadatos
            with self.bloqueo:
                self.frame_actual = frame
                self.frame_id = fid
                self.timestamp_ns = ts
                self.total_frames += 1
                contador_frames += 1

            # Calcular FPS reales del canal
            ahora = time.time()
            dt = ahora - ultimo_tiempo
            if dt >= 2.0:
                self.fps = contador_frames / dt
                contador_frames = 0
                ultimo_tiempo = ahora

        if self.cap:
            self.cap.release()

    def obtener_datos(self):
        """Retorna copia del frame actual y metadatos asociados."""
        with self.bloqueo:
            if self.frame_actual is None:
                return None, None, None
            return self.frame_actual.copy(), self.frame_id, self.timestamp_ns

    def detener(self):
        self.corriendo = False
        if self.hilo:
            self.hilo.join(timeout=2.0)


def crear_placeholder(ancho, alto, texto, subtexto=""):
    """Genera un cuadro de placeholder negro para canales desconectados."""
    img = np.zeros((alto, ancho, 3), dtype=np.uint8)
    fuente = cv2.FONT_HERSHEY_SIMPLEX
    
    # Texto principal
    tam = cv2.getTextSize(texto, fuente, 0.7, 1)[0]
    x = (ancho - tam[0]) // 2
    y = (alto + tam[1]) // 2 - 10
    cv2.putText(img, texto, (x, y), fuente, 0.7, (100, 100, 100), 2, cv2.LINE_AA)
    
    # Subtexto
    if subtexto:
        tam2 = cv2.getTextSize(subtexto, fuente, 0.45, 1)[0]
        x2 = (ancho - tam2[0]) // 2
        cv2.putText(img, subtexto, (x2, y + 30), fuente, 0.45, (80, 80, 80), 1, cv2.LINE_AA)
        
    return img


def dibujar_hud(canvas, titulo, fps, total_frames, sincronia_ok, offset_ms, grabando):
    """
    Dibuja la barra de HUD semitransparente superior con información crítica de red y sincronía.
    """
    alto, ancho = canvas.shape[:2]
    
    # Altura del HUD
    hud_h = 45
    
    # Overlay semitransparente
    overlay = canvas.copy()
    cv2.rectangle(overlay, (0, 0), (ancho, hud_h), (15, 15, 15), -1)
    cv2.addWeighted(overlay, 0.75, canvas, 0.25, 0, canvas)
    
    fuente = cv2.FONT_HERSHEY_SIMPLEX
    escala = 0.45
    
    # 1. Título
    cv2.putText(canvas, titulo, (15, 27), fuente, escala, (220, 220, 220), 1, cv2.LINE_AA)
    
    # 2. Sincronía Física (LSB)
    if sincronia_ok:
        sync_txt = "SYNC: OK (Coherente)"
        sync_col = (50, 220, 50)  # Verde
    else:
        sync_txt = "SYNC: ERROR (Desfasado)"
        sync_col = (50, 50, 250)  # Rojo
    cv2.putText(canvas, sync_txt, (350, 27), fuente, escala, sync_col, 1, cv2.LINE_AA)

    # 3. Latencia promedio
    lat_txt = f"LATENCIA DE RED: {offset_ms:.1f} ms" if offset_ms >= 0 else "LATENCIA: ---"
    cv2.putText(canvas, lat_txt, (580, 27), fuente, escala, (200, 200, 50), 1, cv2.LINE_AA)

    # 4. Info de FPS y frames
    info_txt = f"VISTA: MOSAICO | FPS: {fps:.1f} | TOTAL FRAMES: {total_frames}"
    tam_info = cv2.getTextSize(info_txt, fuente, escala, 1)[0]
    cv2.putText(canvas, info_txt, (ancho - tam_info[0] - 150, 27), fuente, escala, (180, 180, 180), 1, cv2.LINE_AA)

    # 5. Indicador de grabación
    if grabando:
        # Hacer que el REC parpadee cada segundo
        if int(time.time() * 2) % 2 == 0:
            cv2.circle(canvas, (ancho - 110, 22), 6, (0, 0, 255), -1)  # Círculo rojo
        cv2.putText(canvas, "REC", (ancho - 95, 27), fuente, escala, (0, 0, 255), 2, cv2.LINE_AA)
    else:
        cv2.circle(canvas, (ancho - 110, 22), 6, (80, 80, 80), -1)  # Círculo gris
        cv2.putText(canvas, "STBY", (ancho - 95, 27), fuente, escala, (120, 120, 120), 1, cv2.LINE_AA)


def buscar_ffmpeg():
    """Busca FFmpeg para el proceso de grabación local."""
    ruta = shutil.which("ffmpeg")
    if ruta:
        return ruta
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser(description="Receptor OSD RTSP interactivo con grabación en caliente.")
    parser.add_argument("ip", help="Dirección IP del emisor RealSense")
    parser.add_argument("puerto", type=int, nargs="?", default=8554, help="Puerto RTSP (defecto: 8554)")
    args = parser.parse_args()

    # Construir las URLs RTSP de los 4 canales de MediaMTX
    urls = {
        "color": f"rtsp://{args.ip}:{args.puerto}/color",
        "depth": f"rtsp://{args.ip}:{args.puerto}/depth",
        "ir1":   f"rtsp://{args.ip}:{args.puerto}/ir1",
        "ir2":   f"rtsp://{args.ip}:{args.puerto}/ir2"
    }

    print("\n" + "═" * 65)
    print("  RECEPTOR RTSP MODULAR CON OSD")
    print("═" * 65)
    for k, v in urls.items():
        print(f"  {k:<6} : {v}")
    print("─" * 65)
    print("  CONTROLES DE TECLADO:")
    print("    [r] - Iniciar/Detener grabación local (en caliente)")
    print("    [1] - Canal Color (RGB 1080p)")
    print("    [2] - Canal Infrarrojo Izquierdo 1")
    print("    [3] - Canal Profundidad (Heatmap)")
    print("    [4] - Canal Infrarrojo Derecho 2")
    print("    [m] - Vista Mosaico de 4 canales")
    print("    [h] - Mostrar/ocultar barra HUD superior")
    print("    [f] - Pantalla completa")
    print("    [q] - Salir")
    print("═" * 65 + "\n")

    # Inicializar hilos de lectura
    lectores = {}
    for nombre, url in urls.items():
        lectores[nombre] = LectorCanalRTSP(url, nombre)
        lectores[nombre].iniciar()

    # OpenCV ventana interactiva
    cv2.namedWindow(NOMBRE_VENTANA, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(NOMBRE_VENTANA, 960, 720)

    # Estado de la UI
    vista_actual = "mosaico"
    hud_visible = True
    pantalla_completa = False
    
    # Grabación local en caliente
    grabando = False
    proc_grabacion = None
    ruta_ffmpeg = buscar_ffmpeg()
    archivo_mkv = ""

    total_frames = 0
    t_inicial = time.time()
    ultimo_tiempo = time.time()
    fps_receptor = 0.0

    try:
        while True:
            # 1. Leer frames y metadatos de los lectores asíncronos
            frames = {}
            fids = {}
            timestamps = {}

            for nombre, lector in lectores.items():
                img, fid, ts = lector.obtener_datos()
                frames[nombre] = img
                fids[nombre] = fid
                timestamps[nombre] = ts

            # 2. Reconstrucción del layout mosaico (1920x1440 total)
            # Si un canal no está listo, creamos un placeholder
            frame_c = frames["color"] if frames["color"] is not None else crear_placeholder(1920, 1080, "COLOR (RGB)", "Conectando...")
            frame_d = frames["depth"] if frames["depth"] is not None else crear_placeholder(1280, 720, "PROFUNDIDAD", "Conectando...")
            frame_i1 = frames["ir1"] if frames["ir1"] is not None else crear_placeholder(1280, 720, "INFRARROJO 1 (LEFT)", "Conectando...")
            frame_i2 = frames["ir2"] if frames["ir2"] is not None else crear_placeholder(1280, 720, "INFRARROJO 2 (RIGHT)", "Conectando...")

            # Redimensionar infrarrojos y profundidad a 640x360 para que quepan en la mitad inferior
            # Total ancho inferior: 640*3 = 1920. Alto: 360.
            # Color superior: escalado a 1920x1080.
            # Total: Alto = 1080 + 360 = 1440. Ancho = 1920.
            frame_d_resized = cv2.resize(frame_d, (640, 360), interpolation=cv2.INTER_LINEAR)
            frame_i1_resized = cv2.resize(frame_i1, (640, 360), interpolation=cv2.INTER_LINEAR)
            frame_i2_resized = cv2.resize(frame_i2, (640, 360), interpolation=cv2.INTER_LINEAR)

            # Si son grayscale, los convertimos a BGR para concatenar
            if frame_i1_resized.ndim == 2:
                frame_i1_resized = cv2.cvtColor(frame_i1_resized, cv2.COLOR_GRAY2BGR)
            if frame_i2_resized.ndim == 2:
                frame_i2_resized = cv2.cvtColor(frame_i2_resized, cv2.COLOR_GRAY2BGR)

            # Concatenación de la fila inferior: IR1, DEPTH, IR2
            fila_inferior = np.hstack([frame_i1_resized, frame_d_resized, frame_i2_resized])

            # Mosaico final
            mosaico = np.vstack([frame_c, fila_inferior])

            # 3. Cálculo de sincronía física por LSB (comparar Frame IDs)
            ids_validos = [v for v in fids.values() if v is not None]
            sincronia_ok = False
            if len(ids_validos) == 4:
                # Todos los Frame ID deben coincidir
                sincronia_ok = len(set(ids_validos)) == 1

            # 4. Latencia de red
            # Comparamos el timestamp del emisor (canal color) contra el time.time_ns() actual
            offset_ms = -1
            if timestamps["color"]:
                lat_ns = time.time_ns() - timestamps["color"]
                offset_ms = lat_ns / 1_000_000.0

            # 5. Dibujar HUD sobre el mosaico si está habilitado
            if hud_visible:
                dibujar_hud(mosaico, "REAL-TIME MOSAIC", fps_receptor, total_frames, sincronia_ok, offset_ms, grabando)

            # 6. Escribir al proceso de grabación de FFmpeg si la grabación está activa
            if grabando and proc_grabacion:
                try:
                    proc_grabacion.stdin.write(mosaico.tobytes())
                    proc_grabacion.stdin.flush()
                except Exception as e:
                    print(f"  ✗ Error al escribir en el archivo de grabación: {e}")
                    grabando = False
                    if proc_grabacion:
                        try:
                            proc_grabacion.stdin.close()
                            proc_grabacion.terminate()
                        except Exception:
                            pass
                        proc_grabacion = None

            # 7. Selección de la vista en base a controles de usuario
            if vista_actual == "1" and frames["color"] is not None:
                lienzo = frames["color"]
            elif vista_actual == "2" and frames["ir1"] is not None:
                lienzo = frames["ir1"]
            elif vista_actual == "3" and frames["depth"] is not None:
                lienzo = frames["depth"]
            elif vista_actual == "4" and frames["ir2"] is not None:
                lienzo = frames["ir2"]
            else:
                lienzo = mosaico

            # Mostrar renderizado
            cv2.imshow(NOMBRE_VENTANA, lienzo)

            # Control de FPS del receptor
            total_frames += 1
            ahora_rec = time.time()
            dt_rec = ahora_rec - ultimo_tiempo
            if dt_rec >= 2.0:
                fps_receptor = total_frames / (ahora_rec - t_inicial)
                ultimo_tiempo = ahora_rec

            # 8. Capturar eventos de teclado de OpenCV (Key Polling de 1ms)
            key = cv2.waitKey(1) & 0xFF
            
            if key == ord('q') or key == 27:  # ESC o 'q' para salir
                break
            elif key == ord('h'):  # Mostrar/Ocultar HUD
                hud_visible = not hud_visible
            elif key == ord('f'):  # Pantalla completa
                pantalla_completa = not pantalla_completa
                if pantalla_completa:
                    cv2.setWindowProperty(NOMBRE_VENTANA, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
                else:
                    cv2.setWindowProperty(NOMBRE_VENTANA, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_NORMAL)
            elif key in [ord('1'), ord('2'), ord('3'), ord('4')]:
                vista_actual = chr(key)
            elif key == ord('m'):
                vista_actual = "mosaico"
            elif key == ord('r'):  # Alternar Grabación en caliente
                if not grabando:
                    # Iniciar grabación
                    if not ruta_ffmpeg:
                        print("  ✗ FFmpeg no disponible en el sistema. No se puede iniciar la grabación.")
                        continue
                    
                    os.makedirs("grabaciones", exist_ok=True)
                    timestamp_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                    archivo_mkv = os.path.join("grabaciones", f"grabacion_{timestamp_str}.mkv")
                    
                    print(f"  🔴 Iniciando grabación del mosaico -> {archivo_mkv}")
                    
                    cmd_grab = [
                        ruta_ffmpeg,
                        "-y",
                        "-f", "rawvideo",
                        "-vcodec", "rawvideo",
                        "-pix_fmt", "bgr24",
                        "-s", f"{MOSAICO_ANCHO}x{MOSAICO_ALTO}",
                        "-r", "30",
                        "-i", "-",                      # Leer de stdin
                        "-c:v", "libx264",
                        "-pix_fmt", "yuv420p",
                        "-preset", "veryfast",
                        archivo_mkv
                    ]
                    try:
                        proc_grabacion = subprocess.Popen(
                            cmd_grab,
                            stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL
                        )
                        grabando = True
                    except Exception as e:
                        print(f"  ✗ Falló al arrancar FFmpeg para grabación: {e}")
                        grabando = False
                else:
                    # Detener grabación
                    print(f"  ⏹ Grabación detenida. Archivo guardado: {archivo_mkv}")
                    grabando = False
                    if proc_grabacion:
                        try:
                            proc_grabacion.stdin.close()
                            proc_grabacion.wait(timeout=5)
                        except Exception:
                            try:
                                proc_grabacion.kill()
                            except Exception:
                                pass
                        proc_grabacion = None

    except Exception as e:
        print(f"  ✗ Error inesperado en receptor: {e}")
    finally:
        print("\n  → Apagando lectores asíncronos...")
        for lector in lectores.values():
            lector.detener()

        # Detener grabación si seguía activa
        if grabando and proc_grabacion:
            try:
                proc_grabacion.stdin.close()
                proc_grabacion.wait(timeout=2)
            except Exception:
                pass
            print(f"  ✓ Grabación finalizada y guardada en {archivo_mkv}")

        cv2.destroyAllWindows()
        print("  ✓ Receptor cerrado.")

if __name__ == "__main__":
    main()
