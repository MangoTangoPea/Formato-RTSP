#!/usr/bin/env python3
"""
Emisor RTSP para Intel RealSense D435 - Ubuntu/Linux.

Captura 4 canales de la RealSense D435, inyecta metadatos LSB
(Frame ID + Timestamp) y los publica como streams RTSP independientes
usando FFmpeg como servidor directo.

Streams RTSP:
  rtsp://<IP>:8554/stream   - RGB 1920x1080
  rtsp://<IP>:8555/stream   - Profundidad (heatmap JET) 1280x720
  rtsp://<IP>:8556/stream   - Infrarrojo izquierdo 1280x720
  rtsp://<IP>:8557/stream   - Infrarrojo derecho 1280x720

Uso:
    python3 emisor.py                            # Cámara 0, puerto 8554
    python3 emisor.py --cam 1                    # Segunda cámara
    python3 emisor.py --puerto 9554              # Puerto alternativo
    python3 emisor.py --calidad 4000             # Mayor calidad
    python3 emisor.py --listar-camaras           # Ver cámaras
    python3 emisor.py --grabar-rango 150 450     # Grabar rango sin pérdidas
"""

import sys
import os
import signal
import time
import argparse
import queue
import threading
import datetime

import numpy as np
import cv2

from realsense_camera import RealSenseCamera
from rtsp_server import RTSPServer
from lsb_steganography import inyectar_lsb
from utils import buscar_ffmpeg, obtener_ip_local, verificar_puerto_disponible, asegurar_carpeta_grabaciones


# ===========================================================================
# CONSTANTES
# ===========================================================================

PUERTO_RTSP_DEFECTO = 8554

# Flag global para cierre limpio con señales POSIX
_cerrando = False


# ===========================================================================
# GRABADOR DE RANGO SIN PÉRDIDAS (asíncrono)
# ===========================================================================

class GrabadorRango:
    """
    Graba un rango específico de frames sin pérdidas (PNG + CSV de metadatos).
    La escritura se hace en un hilo dedicado para no bloquear la captura.
    Los frames se guardan automáticamente en la carpeta grabaciones/.
    """

    def __init__(self, dir_salida, frame_inicio, frame_fin):
        self.dir_salida = dir_salida
        self.inicio = frame_inicio
        self.fin = frame_fin
        self.cola = queue.Queue()
        self.corriendo = True
        self.hilo = threading.Thread(target=self._bucle_guardado, name="GrabadorRango", daemon=True)

        # Crear estructura de carpetas
        for subdir in ["color", "depth", "ir1", "ir2"]:
            os.makedirs(os.path.join(self.dir_salida, subdir), exist_ok=True)

        # CSV de metadatos
        self.csv_path = os.path.join(self.dir_salida, "metadata.csv")
        with open(self.csv_path, "w", encoding="utf-8") as f:
            f.write("frame_id,timestamp_ns,timestamp_utc\n")

        self.hilo.start()

    def agregar_frame(self, frame_id, color, depth_raw, ir1, ir2, timestamp_ns):
        """Encola un frame para guardado si está dentro del rango."""
        if self.inicio <= frame_id <= self.fin:
            self.cola.put((frame_id, color.copy(), depth_raw.copy(),
                           ir1.copy(), ir2.copy(), timestamp_ns))

    def _bucle_guardado(self):
        """Hilo de escritura: desencola frames y los guarda."""
        while self.corriendo or not self.cola.empty():
            try:
                item = self.cola.get(timeout=0.2)
            except queue.Empty:
                continue

            frame_id, color, depth_raw, ir1, ir2, timestamp_ns = item
            filename = f"{frame_id:08d}.png"

            cv2.imwrite(os.path.join(self.dir_salida, "color", filename), color)
            cv2.imwrite(os.path.join(self.dir_salida, "depth", filename), depth_raw)
            cv2.imwrite(os.path.join(self.dir_salida, "ir1", filename), ir1)
            cv2.imwrite(os.path.join(self.dir_salida, "ir2", filename), ir2)

            try:
                dt_utc = datetime.datetime.fromtimestamp(timestamp_ns / 1e9, datetime.timezone.utc)
                fecha_utc = dt_utc.isoformat()
            except Exception:
                fecha_utc = "unknown"

            with open(self.csv_path, "a", encoding="utf-8") as f:
                f.write(f"{frame_id},{timestamp_ns},{fecha_utc}\n")

            self.cola.task_done()

    def detener(self):
        """Detiene el hilo de escritura y espera a que termine."""
        self.corriendo = False
        if self.hilo.is_alive():
            self.hilo.join(timeout=15.0)


# ===========================================================================
# FUNCIÓN PRINCIPAL
# ===========================================================================

def iniciar_emisor(indice_camara=0, puerto=PUERTO_RTSP_DEFECTO,
                   bitrate_kbps=2000, rango_grabacion=None):
    """
    Emisor RTSP: captura 4 canales de RealSense D435, inyecta metadatos
    LSB y los publica como streams RTSP.
    """
    global _cerrando
    camara = None
    servidor = RTSPServer()

    # Señales POSIX para cierre limpio
    def manejar_senal(signum, frame):
        global _cerrando
        nombre = signal.Signals(signum).name
        print(f"\n  [STOP] Señal {nombre} recibida. Cerrando ...")
        _cerrando = True

    signal.signal(signal.SIGINT, manejar_senal)
    signal.signal(signal.SIGTERM, manejar_senal)

    try:
        print("\n" + "=" * 62)
        print("  EMISOR RTSP - Intel RealSense D435 - Ubuntu")
        print("  Esteganografía LSB activa - FFmpeg RTSP Server directo")
        print("=" * 62)

        # -- PASO 1: FFmpeg ------------------------------------------
        print("\n[1/4] Buscando FFmpeg ...")
        ruta_ffmpeg, origen = buscar_ffmpeg()
        if ruta_ffmpeg is None:
            print("  [X] FFmpeg no encontrado. Instalar: sudo apt install ffmpeg")
            sys.exit(1)
        print(f"  [OK] FFmpeg: {ruta_ffmpeg}")

        # -- PASO 2: Cámara RealSense --------------------------------
        print(f"\n[2/4] Abriendo cámara RealSense (índice {indice_camara}) ...")
        camara = RealSenseCamera(indice_camara=indice_camara)
        print(f"  -> Dispositivo: {camara.nombre} (S/N: {camara.serial})")
        camara.start(hacer_reset=True)

        # -- PASO 3: Servidores RTSP ---------------------------------
        print(f"\n[3/4] Iniciando 4 servidores RTSP ...")

        # Verificar puertos
        for i, nombre in enumerate(["color", "depth", "ir1", "ir2"]):
            p = puerto + i
            if not verificar_puerto_disponible(p):
                print(f"  [X] Puerto {p} ({nombre}) ya está en uso.")
                print(f"    Usa --puerto OTRO o: sudo lsof -i :{p}")
                sys.exit(1)

        servidor.iniciar(ruta_ffmpeg, puerto, bitrate_kbps)
        time.sleep(2)  # Dar tiempo a FFmpeg para abrir sockets

        # -- PASO 4: Grabación de rango (opcional) -------------------
        grabador_rango = None
        if rango_grabacion is not None:
            frame_inicio, frame_fin = rango_grabacion
            dir_grab = asegurar_carpeta_grabaciones()
            dir_rango = os.path.join(dir_grab, f"rango_{frame_inicio}_{frame_fin}")
            print(f"\n[4/4] Grabación de rango [{frame_inicio}-{frame_fin}] -> {dir_rango}")
            grabador_rango = GrabadorRango(dir_rango, frame_inicio, frame_fin)
        else:
            print(f"\n[4/4] Grabación de rango: desactivada")

        # -- Banner de conexión --------------------------------------
        ip_local = obtener_ip_local()
        puertos = servidor.puertos

        print("\n" + "=" * 62)
        print("  [OK] TRANSMISIÓN ACTIVA - 4 Canales RTSP + LSB")
        print("-" * 62)
        print(f"  Color (RGB):     rtsp://{ip_local}:{puertos['color']}/stream")
        print(f"  Profundidad:     rtsp://{ip_local}:{puertos['depth']}/stream")
        print(f"  Infrarrojo 1:    rtsp://{ip_local}:{puertos['ir1']}/stream")
        print(f"  Infrarrojo 2:    rtsp://{ip_local}:{puertos['ir2']}/stream")
        print("-" * 62)
        print(f"  Receptor:  python3 receptor.py {ip_local}")
        print(f"  VLC:       vlc rtsp://{ip_local}:{puertos['color']}/stream")
        print("=" * 62)
        print("\n  Presiona Ctrl+C para detener.\n")

        # -- Bucle principal -----------------------------------------
        frame_id = 1
        t_inicio = time.time()

        while not _cerrando:
            datos = camara.capture_frame()
            if datos is None:
                continue

            timestamp_ns = time.time_ns()

            # Inyectar LSB en los 4 canales
            inyectar_lsb(datos['color'], frame_id, timestamp_ns)
            inyectar_lsb(datos['depth_color'], frame_id, timestamp_ns)
            inyectar_lsb(datos['ir1'], frame_id, timestamp_ns)
            inyectar_lsb(datos['ir2'], frame_id, timestamp_ns)

            # Enviar a RTSP
            try:
                servidor.enviar_frames(
                    datos['color'], datos['depth_color'],
                    datos['ir1'], datos['ir2']
                )
            except (BrokenPipeError, OSError) as e:
                print(f"  [X] Error escribiendo a FFmpeg: {e}")
                break

            # Grabación de rango
            if grabador_rango is not None:
                grabador_rango.agregar_frame(
                    frame_id, datos['color'], datos['depth_raw'],
                    datos['ir1'], datos['ir2'], timestamp_ns
                )
                if frame_id >= grabador_rango.fin:
                    print(f"\n  [OK] Rango finalizado ({grabador_rango.inicio}-{grabador_rango.fin})")
                    grabador_rango.detener()
                    grabador_rango = None

            frame_id += 1

            # Log cada ~5 segundos
            if (frame_id - 1) % 150 == 0:
                dt = time.time() - t_inicio
                fps_actual = (frame_id - 1) / dt if dt > 0 else 0
                ts_str = time.strftime("%H:%M:%S", time.localtime(timestamp_ns / 1e9))
                ts_ms = int((timestamp_ns % 1_000_000_000) / 1_000_000)
                print(f"  [>>] FID: {frame_id - 1} | TS: {ts_str}.{ts_ms:03d} | "
                      f"FPS: {fps_actual:.1f} | Tiempo: {dt:.0f}s")

            # Verificar salud de FFmpeg
            canal_muerto = servidor.verificar_salud()
            if canal_muerto:
                print(f"  [X] FFmpeg ({canal_muerto}) se detuvo inesperadamente.")
                break

    except KeyboardInterrupt:
        print("\n\n  [STOP] Detenido por el usuario (Ctrl+C).")

    except Exception as e:
        print(f"\n  [X] Error inesperado: {e}")
        import traceback
        traceback.print_exc()

    finally:
        print("\n  Liberando recursos ...")

        if 'grabador_rango' in locals() and grabador_rango is not None:
            grabador_rango.detener()
            print("  [OK] Grabador de rango finalizado.")

        if camara:
            camara.stop()

        servidor.detener()

        total_frames = frame_id - 1 if 'frame_id' in locals() else 0
        if total_frames > 0:
            dt_total = time.time() - t_inicio
            print(f"\n  Resumen: {total_frames} frames en {dt_total:.1f}s "
                  f"({total_frames / dt_total:.1f} FPS promedio)")

        print("\n  Emisor finalizado.\n")


# ===========================================================================
# PUNTO DE ENTRADA
# ===========================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Emisor RTSP para Intel RealSense D435 - Ubuntu.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  python3 emisor.py                         # Cámara 0, puerto 8554
  python3 emisor.py --cam 1                 # Segunda cámara
  python3 emisor.py --puerto 9554           # Puerto alternativo
  python3 emisor.py --calidad 4000          # Mayor calidad
  python3 emisor.py --listar-camaras        # Ver cámaras conectadas
  python3 emisor.py --grabar-rango 150 450  # Grabar rango sin pérdidas

Puertos RTSP (base + offset):
  Color:  base     (ej. 8554)
  Depth:  base + 1 (ej. 8555)
  IR1:    base + 2 (ej. 8556)
  IR2:    base + 3 (ej. 8557)
        """
    )

    parser.add_argument("--puerto", type=int, default=PUERTO_RTSP_DEFECTO,
                        help=f"Puerto RTSP base (defecto: {PUERTO_RTSP_DEFECTO})")
    parser.add_argument("--cam", type=int, default=0,
                        help="Índice de la cámara RealSense (defecto: 0)")
    parser.add_argument("--calidad", type=int, default=2000,
                        help="Bitrate total en kbps (defecto: 2000)")
    parser.add_argument("--grabar-rango", type=int, nargs=2, metavar=("INICIO", "FIN"),
                        default=None,
                        help="Grabar rango de frames sin pérdidas (ej. --grabar-rango 100 500)")
    parser.add_argument("--listar-camaras", action="store_true",
                        help="Listar cámaras RealSense y salir")

    args = parser.parse_args()

    if args.listar_camaras:
        RealSenseCamera.listar_camaras()
        sys.exit(0)

    iniciar_emisor(
        indice_camara=args.cam,
        puerto=args.puerto,
        bitrate_kbps=args.calidad,
        rango_grabacion=args.grabar_rango,
    )
