#!/usr/bin/env python3
"""
Servidor RTSP usando FFmpeg como backend.

Encapsula la creación y gestión de 4 procesos FFmpeg independientes,
cada uno actuando como servidor RTSP directo (sin MediaMTX) para un
canal de la cámara RealSense D435:

  Puerto base     -> Color (RGB) 1920x1080
  Puerto base + 1 -> Profundidad (heatmap JET) 1280x720
  Puerto base + 2 -> Infrarrojo 1 (Left) 1280x720
  Puerto base + 3 -> Infrarrojo 2 (Right) 1280x720
"""

import subprocess


class RTSPServer:
    """
    Servidor RTSP de 4 canales usando FFmpeg como backend.

    Cada canal es un proceso FFmpeg independiente que abre un socket TCP
    y espera conexiones RTSP entrantes. El receptor se conecta directamente.

    Attributes
    ----------
    procesos : dict
        Diccionario {nombre: subprocess.Popen} de los 4 FFmpeg.
    puertos : dict
        Diccionario {nombre: puerto} de los 4 canales.
    """

    # Configuración de cada canal: resolución, formato de píxel FFmpeg
    CANALES = {
        "color": {"ancho": 1920, "alto": 1080, "pix_fmt": "bgr24"},
        "depth": {"ancho": 1280, "alto": 720,  "pix_fmt": "bgr24"},
        "ir1":   {"ancho": 1280, "alto": 720,  "pix_fmt": "gray"},
        "ir2":   {"ancho": 1280, "alto": 720,  "pix_fmt": "gray"},
    }

    # Distribución proporcional del bitrate total
    PESO_BITRATE = {
        "color": 0.55,
        "depth": 0.25,
        "ir1":   0.10,
        "ir2":   0.10,
    }

    def __init__(self):
        self.procesos = {}
        self.puertos = {}

    def iniciar(self, ruta_ffmpeg, puerto_base, bitrate_total_kbps=2000, fps=30):
        """
        Lanza los 4 procesos FFmpeg como servidores RTSP.

        Parameters
        ----------
        ruta_ffmpeg : str
            Ruta al ejecutable de FFmpeg.
        puerto_base : int
            Puerto RTSP para el canal color. Los demás son base+1, +2, +3.
        bitrate_total_kbps : int
            Bitrate total en kbps, se distribuye proporcionalmente.
        fps : int
            Frames por segundo del stream.
        """
        self.puertos = {
            "color": puerto_base,
            "depth": puerto_base + 1,
            "ir1":   puerto_base + 2,
            "ir2":   puerto_base + 3,
        }

        for nombre, cfg in self.CANALES.items():
            puerto = self.puertos[nombre]
            peso = self.PESO_BITRATE[nombre]
            bitrate = max(100, int(bitrate_total_kbps * peso))

            print(f"  -> {nombre:<6} {cfg['ancho']}x{cfg['alto']} @ {bitrate}kbps -> puerto {puerto}")

            self.procesos[nombre] = self._crear_ffmpeg(
                ruta_ffmpeg, puerto,
                cfg["ancho"], cfg["alto"], cfg["pix_fmt"],
                fps, bitrate
            )

    def _crear_ffmpeg(self, ruta_ffmpeg, puerto, ancho, alto, pix_fmt, fps, bitrate_kbps):
        """
        Lanza un subproceso FFmpeg como servidor RTSP directo.

        FFmpeg abre un socket TCP en el puerto indicado y espera conexiones
        RTSP entrantes.
        """
        url_listen = f"rtsp://0.0.0.0:{puerto}/stream"
        cmd = [
            ruta_ffmpeg,
            "-y",
            "-f", "rawvideo",
            "-vcodec", "rawvideo",
            "-pix_fmt", pix_fmt,
            "-s", f"{ancho}x{alto}",
            "-r", str(fps),
            "-i", "-",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-preset", "ultrafast",
            "-tune", "zerolatency",
            "-b:v", f"{bitrate_kbps}k",
            "-maxrate", f"{bitrate_kbps}k",
            "-bufsize", f"{bitrate_kbps * 2}k",
            "-g", str(fps * 2),
            "-f", "rtsp",
            "-rtsp_transport", "tcp",
            "-rtsp_flags", "listen",
            url_listen,
        ]
        return subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

    def enviar_frames(self, color, depth_color, ir1, ir2):
        """
        Envía un set de frames a los 4 servidores RTSP.

        Parameters
        ----------
        color : np.ndarray
            Frame BGR 1920x1080.
        depth_color : np.ndarray
            Frame BGR 1280x720 (heatmap de profundidad).
        ir1 : np.ndarray
            Frame grayscale 1280x720.
        ir2 : np.ndarray
            Frame grayscale 1280x720.

        Raises
        ------
        BrokenPipeError, OSError
            Si algún proceso FFmpeg se cerró inesperadamente.
        """
        self.procesos["color"].stdin.write(color.tobytes())
        self.procesos["depth"].stdin.write(depth_color.tobytes())
        self.procesos["ir1"].stdin.write(ir1.tobytes())
        self.procesos["ir2"].stdin.write(ir2.tobytes())

    def verificar_salud(self):
        """
        Verifica que los 4 FFmpeg sigan vivos.

        Returns
        -------
        str or None
            Nombre del canal muerto, o None si todos están bien.
        """
        for nombre, proc in self.procesos.items():
            if proc.poll() is not None:
                return nombre
        return None

    def detener(self):
        """Detiene todos los procesos FFmpeg de forma ordenada."""
        # Cerrar stdin de todos
        for nombre, proc in self.procesos.items():
            if proc and proc.stdin:
                try:
                    proc.stdin.close()
                except Exception:
                    pass

        # Terminar procesos
        for nombre, proc in self.procesos.items():
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                    print(f"  [OK] FFmpeg ({nombre}) detenido")
                except Exception:
                    proc.kill()
                    print(f"  [OK] FFmpeg ({nombre}) forzado")

        self.procesos.clear()
