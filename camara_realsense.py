"""
Módulo camara_realsense.py
Control y captura robusta de la cámara Intel RealSense D435.
"""

import time
import sys

try:
    import pyrealsense2 as rs
except ImportError:
    rs = None

class ControladorRealSense:
    def __init__(self, indice_camara=0):
        self.indice_camara = indice_camara
        self.pipeline = None
        self.config = None
        self.perfil = None
        self.dispositivo = None
        self.serial = None
        self.nombre = None
        self.usb_tipo = "3.0"  # Por defecto asumimos alta velocidad
        self.activo = False

        if rs is None:
            raise ImportError(
                "pyrealsense2 no está instalado en este entorno. "
                "Instálelo con 'pip install pyrealsense2' o compílelo si está en ARM64/Jetson."
            )

    def realizar_hardware_reset(self):
        """
        Realiza un hardware reset en la cámara indicada para liberar sesiones anteriores.
        Emula el comportamiento del RealSense Viewer.
        """
        print(f"  → Iniciando re-enumeración por hardware (reset) en cámara {self.indice_camara} ...")
        ctx = rs.context()
        dispositivos = ctx.query_devices()
        
        if len(dispositivos) == 0:
            print("  ⚠ No se detectó ninguna cámara RealSense para reiniciar.")
            return False

        if self.indice_camara >= len(dispositivos):
            print(f"  ⚠ Índice de cámara {self.indice_camara} fuera de rango. Total detectadas: {len(dispositivos)}")
            return False

        try:
            dev = dispositivos[self.indice_camara]
            self.nombre = dev.get_info(rs.camera_info.name)
            self.serial = dev.get_info(rs.camera_info.serial_number)
            dev.hardware_reset()
            print("  ✓ Hardware reset enviado. Esperando 3.0 segundos para re-enumeración USB...")
            time.sleep(3.0)
            return True
        except Exception as e:
            print(f"  ⚠ Hardware reset falló: {e}")
            return False

    def conectar(self):
        """
        Busca el dispositivo tras el reset, autodetecta la velocidad USB,
        configura las resoluciones idóneas y levanta el pipeline con hasta 3 reintentos.
        """
        self.activo = False
        ctx = rs.context()
        dispositivos = ctx.query_devices()

        if len(dispositivos) == 0:
            raise RuntimeError("No se encontró ningún dispositivo Intel RealSense. Verifique la conexión USB.")

        # Buscar el dispositivo por el serial previamente conocido o por índice
        dev = None
        if self.serial:
            for d in dispositivos:
                try:
                    if d.get_info(rs.camera_info.serial_number) == self.serial:
                        dev = d
                        break
                except Exception:
                    pass
        
        if dev is None:
            if self.indice_camara < len(dispositivos):
                dev = dispositivos[self.indice_camara]
            else:
                raise RuntimeError(f"No se encontró el dispositivo en el índice {self.indice_camara}.")

        self.nombre = dev.get_info(rs.camera_info.name)
        self.serial = dev.get_info(rs.camera_info.serial_number)
        
        try:
            self.usb_tipo = dev.get_info(rs.camera_info.usb_type_descriptor)
        except Exception:
            self.usb_tipo = "3.0"  # Fallback

        print(f"  ✓ Dispositivo detectado: {self.nombre} (S/N: {self.serial}, USB: {self.usb_tipo})")

        # Configurar resoluciones y FPS basados en la velocidad del puerto USB
        # Si está en puerto USB 2.x, 1920x1080 @ 30 FPS saturará el ancho de banda
        if self.usb_tipo.startswith("2"):
            print("  ⚠ ADVERTENCIA: Cámara conectada a puerto USB 2.x.")
            print("    Configurando flujos a 1280×720 @ 15 FPS para evitar timeouts de bus.")
            ancho_color, alto_color, fps = 1280, 720, 15
            ancho_depth, alto_depth = 1280, 720
        else:
            print("  ✓ Conectado a USB 3.x. Configurando Color a 1920×1080 y Depth/IR a 1280×720 @ 30 FPS.")
            ancho_color, alto_color, fps = 1920, 1080, 30
            ancho_depth, alto_depth = 1280, 720

        # Intentos de arranque
        MAX_INTENTOS = 3
        for intento in range(1, MAX_INTENTOS + 1):
            print(f"  → Iniciando pipeline (Intento {intento}/{MAX_INTENTOS}) ...")
            try:
                self.pipeline = rs.pipeline()
                self.config = rs.config()
                self.config.enable_device(self.serial)

                # Habilitar los 4 canales de vídeo de forma síncrona
                self.config.enable_stream(rs.stream.color, ancho_color, alto_color, rs.format.bgr8, fps)
                self.config.enable_stream(rs.stream.depth, ancho_depth, alto_depth, rs.format.z16, fps)
                self.config.enable_stream(rs.stream.infrared, 1, ancho_depth, alto_depth, rs.format.y8, fps)
                self.config.enable_stream(rs.stream.infrared, 2, ancho_depth, alto_depth, rs.format.y8, fps)

                self.perfil = self.pipeline.start(self.config)
                self.activo = True
                print(f"  ✓ Pipeline iniciado con éxito ({ancho_color}x{alto_color} Color, {ancho_depth}x{alto_depth} Depth/IR @ {fps}fps)")
                return True
            except Exception as e:
                print(f"  ✗ Intento {intento} falló: {e}")
                self.pipeline = None
                self.config = None
                if intento < MAX_INTENTOS:
                    tiempo_espera = 2.0 * intento
                    print(f"    Esperando {tiempo_espera}s antes de reintentar...")
                    time.sleep(tiempo_espera)

        raise RuntimeError(f"No se pudo iniciar el pipeline de RealSense tras {MAX_INTENTOS} intentos.")

    def obtener_frameset(self, timeout_ms=5000):
        """
        Captura un frameset síncrono.
        Retorna (color_frame, depth_frame, ir1_frame, ir2_frame, timestamp_ns)
        o (None, None, None, None, None) si falla o hay timeout.
        """
        if not self.activo or not self.pipeline:
            return None, None, None, None, None

        try:
            frameset = self.pipeline.wait_for_frames(timeout_ms=timeout_ms)
            # Timestamp del reloj local tomado en el momento exacto en que llega el frameset
            timestamp_ns = time.time_ns()

            color_f = frameset.get_color_frame()
            depth_f = frameset.get_depth_frame()
            ir1_f   = frameset.get_infrared_frame(1)
            ir2_f   = frameset.get_infrared_frame(2)

            if not color_f or not depth_f or not ir1_f or not ir2_f:
                return None, None, None, None, None

            return color_f, depth_f, ir1_f, ir2_f, timestamp_ns
        except Exception:
            return None, None, None, None, None

    def desconectar(self):
        """Detiene de forma segura el pipeline de la cámara."""
        self.activo = False
        if self.pipeline:
            try:
                self.pipeline.stop()
                print("  ✓ Pipeline RealSense detenido limpiamente.")
            except Exception:
                pass
            self.pipeline = None
            self.config = None
            self.perfil = None

    def listar_dispositivos(self):
        """Lista los dispositivos RealSense conectados."""
        if rs is None:
            return []
        ctx = rs.context()
        return [d.get_info(rs.camera_info.name) for d in ctx.query_devices()]
