#!/usr/bin/env python3
"""
Aplicación Principal Emisora: emisor_app.py
Captura, inyecta esteganografía LSB y publica 4 streams RTSP en MediaMTX.
"""

import sys
import os
import time
import signal
import argparse
import numpy as np
import cv2

# Importar módulos locales
from camara_realsense import ControladorRealSense
from servidor_rtsp import AdministradorRTSP
from esteganografia import inyectar_lsb

# Flag global para interrupción POSIX
_cerrando = False

def manejar_senal(signum, frame):
    global _cerrando
    nombre = signal.Signals(signum).name
    print(f"\n  ⏹ Señal {nombre} recibida. Deteniendo emisor de forma limpia...")
    _cerrando = True

def main():
    global _cerrando
    
    # Registrar señales de parada
    signal.signal(signal.SIGINT, manejar_senal)
    signal.signal(signal.SIGTERM, manejar_senal)

    parser = argparse.ArgumentParser(
        description="Emisor Modular RTSP para Intel RealSense D435 con Esteganografía LSB y MediaMTX."
    )
    parser.add_argument("--puerto", type=int, default=8554, help="Puerto RTSP de MediaMTX (defecto: 8554)")
    parser.add_argument("--cam", type=int, default=0, help="Índice de la cámara RealSense (defecto: 0)")
    parser.add_argument("--calidad", type=int, default=2000, help="Bitrate global total en kbps (defecto: 2000)")
    args = parser.parse_args()

    print("\n" + "═" * 65)
    print("  EMISOR RTSP MODULAR — Intel RealSense D435")
    print("  Esteganografía LSB (8 px/bit) · Servidor MediaMTX")
    print("═" * 65)

    camara = None
    rtsp = None

    try:
        # 1. Inicializar y resetear la cámara por hardware
        camara = ControladorRealSense(indice_camara=args.cam)
        camara.realizar_hardware_reset()
        camara.conectar()

        # 2. Inicializar Servidor MediaMTX
        rtsp = AdministradorRTSP(puerto_rtsp=args.puerto)
        rtsp.iniciar_servidor()

        # Configurar resoluciones y bitrates en base a la velocidad USB detectada por el módulo de la cámara
        # El módulo de cámara configura automáticamente las resoluciones adecuadas
        if camara.usb_tipo.startswith("2"):
            ancho_color, alto_color = 1280, 720
            ancho_depth, alto_depth = 1280, 720
            fps = 15
        else:
            ancho_color, alto_color = 1920, 1080
            ancho_depth, alto_depth = 1280, 720
            fps = 30

        # Repartición proporcional del bitrate global
        bitrate_color = max(200, int(args.calidad * 0.55))
        bitrate_depth = max(200, int(args.calidad * 0.25))
        bitrate_ir1   = max(100, int(args.calidad * 0.10))
        bitrate_ir2   = max(100, int(args.calidad * 0.10))

        # 3. Lanzar publicadores FFmpeg
        print("\n  → Levantando publicadores FFmpeg en modo push...")
        rtsp.iniciar_publicador("color", ancho_color, alto_color, "bgr24", fps, bitrate_color)
        rtsp.iniciar_publicador("depth", ancho_depth, alto_depth, "bgr24", fps, bitrate_depth)
        rtsp.iniciar_publicador("ir1", ancho_depth, alto_depth, "gray", fps, bitrate_ir1)
        rtsp.iniciar_publicador("ir2", ancho_depth, alto_depth, "gray", fps, bitrate_ir2)

        # Esperar un momento a que inicien los publicadores y validar que sigan activos
        time.sleep(1.5)
        if not rtsp.verificar_publicadores():
            raise RuntimeError("Uno o más publicadores FFmpeg fallaron al inicializar.")

        # Obtener IP local para el banner informativo
        ip_local = "127.0.0.1"
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip_local = s.getsockname()[0]
            s.close()
        except Exception:
            pass

        print("\n" + "═" * 65)
        print("  ✓ TRANSMISIÓN ACTIVA — 4 Canales RTSP")
        print("─" * 65)
        print(f"  Color (RGB):     rtsp://{ip_local}:{args.puerto}/color")
        print(f"  Profundidad:     rtsp://{ip_local}:{args.puerto}/depth")
        print(f"  Infrarrojo 1:    rtsp://{ip_local}:{args.puerto}/ir1")
        print(f"  Infrarrojo 2:    rtsp://{ip_local}:{args.puerto}/ir2")
        print("─" * 65)
        print("  Receptor: python3 receptor_app.py " + ip_local)
        print("  VLC:      vlc rtsp://" + ip_local + f":{args.puerto}/color")
        print("═" * 65 + "\n")
        print("  Presione Ctrl+C para detener.\n")

        # 4. Bucle principal de captura y transmisión
        frame_id = 1
        tiempo_inicial = time.time()

        while not _cerrando:
            # Capturar frameset síncrono
            color_f, depth_f, ir1_f, ir2_f, timestamp_ns = camara.obtener_frameset(timeout_ms=3000)

            if color_f is None:
                # Timeout o error temporal
                continue

            # Convertir frames a arrays de NumPy escribibles
            color_img = np.array(color_f.get_data())     # BGR
            depth_raw = np.asanyarray(depth_f.get_data()) # Z16 (16-bit)
            ir1_img   = np.array(ir1_f.get_data())       # Grayscale
            ir2_img   = np.array(ir2_f.get_data())       # Grayscale

            # Convertir Profundidad Z16 (profundidad cruda en mm) a mapa de calor de visualización BGR
            # Limitamos la distancia máxima a 4 metros (4000 mm) para un contraste adecuado
            depth_clipped = np.clip(depth_raw, 0, 4000)
            depth_8bit    = (depth_clipped * (255.0 / 4000.0)).astype(np.uint8)
            depth_color   = cv2.applyColorMap(depth_8bit, cv2.COLORMAP_JET)
            depth_color[depth_raw == 0] = [0, 0, 0]  # Píxeles sin datos en negro

            # Inyectar esteganografía LSB en los 4 canales
            # El mismo frame_id y timestamp_ns en los 4 canales garantiza la coherencia temporal
            inyectar_lsb(color_img,   frame_id, timestamp_ns)
            inyectar_lsb(depth_color, frame_id, timestamp_ns)
            inyectar_lsb(ir1_img,     frame_id, timestamp_ns)
            inyectar_lsb(ir2_img,     frame_id, timestamp_ns)

            # Enviar a los codificadores FFmpeg
            ok_c = rtsp.enviar_frame("color", color_img.tobytes())
            ok_d = rtsp.enviar_frame("depth", depth_color.tobytes())
            ok_i1 = rtsp.enviar_frame("ir1", ir1_img.tobytes())
            ok_i2 = rtsp.enviar_frame("ir2", ir2_img.tobytes())

            if not (ok_c and ok_d and ok_i1 and ok_i2):
                print("  ⚠ Error al enviar frames a uno de los publicadores FFmpeg. ¿FFmpeg se ha cerrado?")
                # Comprobar estado real de los subprocesos
                if not rtsp.verificar_publicadores():
                    break

            # Logging periódico cada 150 frames (~5 segundos a 30 FPS)
            if frame_id % 150 == 0:
                elapsed = time.time() - tiempo_inicial
                fps_actual = frame_id / elapsed if elapsed > 0 else 0
                t_struct = time.localtime(timestamp_ns / 1e9)
                t_str = time.strftime("%H:%M:%S", t_struct)
                t_ms = int((timestamp_ns % 1_000_000_000) / 1_000_000)
                print(f"  📹 FID: {frame_id:<6} | TS: {t_str}.{t_ms:03d} | FPS: {fps_actual:.1f} | Tiempo: {int(elapsed)}s")

            frame_id += 1

    except KeyboardInterrupt:
        print("\n  ⏹ Detenido por solicitud del usuario.")
    except Exception as e:
        print(f"\n  ✗ Error crítico inesperado: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # Liberación ordenada de recursos
        print("\n  → Liberando todos los recursos...")
        if camara:
            camara.desconectar()
        if rtsp:
            rtsp.detener_todo()
        print("  ✓ Finalizado.")

if __name__ == "__main__":
    import socket
    main()
