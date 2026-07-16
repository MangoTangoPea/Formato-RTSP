#!/usr/bin/env python3
"""
Clase para interfaz con la cámara Intel RealSense D435.

Basada en el patrón de Daniel Zapata - GIGSEEA (UTP) 2025.
Incluye hardware_reset() para resolver problemas de conexión USB.

Módulo independiente: se puede usar en cualquier proyecto que necesite
capturar frames de una RealSense D435.
"""

import time
import numpy as np
import pyrealsense2 as rs
import cv2


class RealSenseCamera:
    """
    Interfaz con la Intel RealSense D435.

    Resuelve el problema de conexión donde la cámara no responde
    a menos que se abra primero el RealSense Viewer, mediante un
    hardware_reset() previo al inicio del pipeline.

    Attributes
    ----------
    pipeline : rs.pipeline
        Pipeline de captura de RealSense.
    config : rs.config
        Configuración de streams.
    activo : bool
        True si el pipeline está corriendo.

    Streams configurados
    --------------------
    - Color:       1920x1080 @ 30fps (BGR8)
    - Depth:       1280x720  @ 30fps (Z16)
    - Infrared 1:  1280x720  @ 30fps (Y8)
    - Infrared 2:  1280x720  @ 30fps (Y8)
    """

    def __init__(self, indice_camara=0):
        """
        Inicializa la cámara RealSense.

        Parameters
        ----------
        indice_camara : int
            Índice del dispositivo RealSense (0 para la primera cámara).

        Raises
        ------
        RuntimeError
            Si no se detectan cámaras RealSense.
        IndexError
            Si el índice está fuera de rango.
        """
        self._indice = indice_camara
        self.pipeline = None
        self.config = None
        self.activo = False
        self._serial = None
        self._nombre = None

        # Verificar que la cámara existe
        ctx = rs.context()
        dispositivos = ctx.query_devices()

        if len(dispositivos) == 0:
            raise RuntimeError(
                "No se detectaron cámaras Intel RealSense.\n"
                "  * Verifica que esté conectada a un puerto USB 3.0 (azul)\n"
                "  * Ejecuta: python3 verificar_sistema.py"
            )

        if indice_camara >= len(dispositivos):
            raise IndexError(
                f"Índice {indice_camara} fuera de rango. "
                f"Hay {len(dispositivos)} cámara(s) disponible(s)."
            )

        self._serial = dispositivos[indice_camara].get_info(rs.camera_info.serial_number)
        self._nombre = dispositivos[indice_camara].get_info(rs.camera_info.name)

    @property
    def serial(self):
        """Número de serie de la cámara."""
        return self._serial

    @property
    def nombre(self):
        """Nombre del dispositivo (ej: 'Intel RealSense D435')."""
        return self._nombre

    def hardware_reset(self):
        """
        Ejecuta un hardware reset del dispositivo USB.

        Esto es lo que hace internamente el RealSense Viewer y por eso
        la cámara funciona después de abrirlo. Sin este paso, el dispositivo
        puede quedar en un estado inconsistente y rechazar la configuración
        de streams.

        Después del reset, espera a que el dispositivo se re-enumere en
        el bus USB antes de retornar.
        """
        print(f"  -> Hardware reset de {self._nombre} (S/N: {self._serial}) ...")

        ctx = rs.context()
        dispositivos = ctx.query_devices()

        for dev in dispositivos:
            if dev.get_info(rs.camera_info.serial_number) == self._serial:
                dev.hardware_reset()
                break

        # Esperar a que el dispositivo se re-enumere en USB
        print("  -> Esperando re-enumeración USB ...")
        self._esperar_dispositivo(timeout=10)
        print("  [OK] Dispositivo listo.")

    def _esperar_dispositivo(self, timeout=10):
        """
        Espera activa hasta que el dispositivo vuelva a aparecer en el
        bus USB después de un hardware reset.

        Parameters
        ----------
        timeout : int
            Segundos máximos de espera.

        Raises
        ------
        TimeoutError
            Si el dispositivo no reaparece en el tiempo dado.
        """
        t_inicio = time.time()

        while time.time() - t_inicio < timeout:
            time.sleep(0.5)
            ctx = rs.context()
            for dev in ctx.query_devices():
                try:
                    if dev.get_info(rs.camera_info.serial_number) == self._serial:
                        # Dar un momento extra para estabilizarse
                        time.sleep(1.0)
                        return
                except Exception:
                    continue

        raise TimeoutError(
            f"La cámara {self._serial} no reapareció después del reset "
            f"({timeout}s). Verifica la conexión USB."
        )

    def start(self, hacer_reset=True):
        """
        Inicia el pipeline de captura con hardware reset previo.

        Parameters
        ----------
        hacer_reset : bool
            Si True, ejecuta hardware_reset() antes de iniciar.
            Poner en False solo si la cámara ya está estable.

        Raises
        ------
        RuntimeError
            Si no se puede iniciar la cámara después de 3 intentos.
        """
        if hacer_reset:
            self.hardware_reset()

        # Crear pipeline y configuración desde cero
        self.pipeline = rs.pipeline()
        self.config = rs.config()
        self.config.enable_device(self._serial)

        # Configurar los 4 streams a resolución nativa de la D435
        self.config.enable_stream(rs.stream.color, 1920, 1080, rs.format.bgr8, 30)
        self.config.enable_stream(rs.stream.depth, 1280, 720, rs.format.z16, 30)
        self.config.enable_stream(rs.stream.infrared, 1, 1280, 720, rs.format.y8, 30)
        self.config.enable_stream(rs.stream.infrared, 2, 1280, 720, rs.format.y8, 30)

        # Intentar iniciar con reintentos
        ultimo_error = None
        for intento in range(3):
            try:
                self.pipeline.start(self.config)
                self.activo = True
                print(f"  [OK] Pipeline iniciado (Color 1920x1080, Depth/IR 1280x720 @ 30fps)")
                return
            except RuntimeError as e:
                ultimo_error = e
                espera = (intento + 1) * 2
                print(f"  [!] Intento {intento + 1}/3 falló: {e}")
                print(f"    Reintentando en {espera}s ...")
                time.sleep(espera)

        raise RuntimeError(
            f"No se pudo iniciar la cámara después de 3 intentos.\n"
            f"  Último error: {ultimo_error}\n"
            f"  * Verifica que esté en un puerto USB 3.0 (azul)\n"
            f"  * Cierra cualquier otro programa que use la cámara\n"
            f"  * Ejecuta: python3 verificar_sistema.py"
        )

    def stop(self):
        """
        Detiene el pipeline y libera el dispositivo USB.

        Incluye 'del self.pipeline' explícito para forzar la liberación
        del handle USB, como en el patrón original de Daniel Zapata.
        """
        if self.activo and self.pipeline:
            try:
                self.pipeline.stop()
            except Exception:
                pass
            del self.pipeline
            self.pipeline = None
            self.activo = False
            print("  [OK] Cámara RealSense detenida correctamente.")

    def capture_frame(self, timeout_ms=5000):
        """
        Captura un frameset completo de la cámara.

        Parameters
        ----------
        timeout_ms : int
            Timeout en milisegundos para esperar el frameset.

        Returns
        -------
        dict or None
            Diccionario con los frames capturados:
            {
                'color':        np.ndarray (1920x1080x3, BGR),
                'depth_raw':    np.ndarray (1280x720, uint16 Z16),
                'depth_color':  np.ndarray (1280x720x3, BGR heatmap JET),
                'ir1':          np.ndarray (1280x720, uint8),
                'ir2':          np.ndarray (1280x720, uint8),
            }
            Retorna None si no se pudo capturar.
        """
        if not self.activo:
            return None

        try:
            frameset = self.pipeline.wait_for_frames(timeout_ms=timeout_ms)
        except RuntimeError:
            return None

        fc = frameset.get_color_frame()
        fd = frameset.get_depth_frame()
        fi1 = frameset.get_infrared_frame(1)
        fi2 = frameset.get_infrared_frame(2)

        if not fc or not fd or not fi1 or not fi2:
            return None

        # Convertir a arrays NumPy (copias escribibles)
        color_img = np.array(fc.get_data())
        depth_raw = np.asanyarray(fd.get_data())
        ir1_img = np.array(fi1.get_data())
        ir2_img = np.array(fi2.get_data())

        # Generar heatmap JET de profundidad
        depth_clipped = np.clip(depth_raw, 0, 4000)
        depth_8bit = (depth_clipped * (255.0 / 4000.0)).astype(np.uint8)
        depth_color = cv2.applyColorMap(depth_8bit, cv2.COLORMAP_JET)
        depth_color[depth_raw == 0] = [0, 0, 0]  # Negro donde no hay dato

        return {
            'color': color_img,
            'depth_raw': depth_raw,
            'depth_color': depth_color,
            'ir1': ir1_img,
            'ir2': ir2_img,
        }

    @staticmethod
    def listar_camaras():
        """
        Lista todas las cámaras RealSense conectadas.

        Returns
        -------
        list[dict]
            Lista de diccionarios con info de cada cámara:
            [{'indice': 0, 'nombre': '...', 'serie': '...', 'usb': '3.2'}]
        """
        ctx = rs.context()
        dispositivos = ctx.query_devices()

        if len(dispositivos) == 0:
            print("\n  [X] No se detectaron cámaras Intel RealSense.")
            print("    Ejecuta: python3 verificar_sistema.py")
            return []

        print(f"\n  Cámaras Intel RealSense detectadas: {len(dispositivos)}")
        print("  " + "-" * 60)
        print(f"  {'Idx':<5} {'Nombre':<30} {'No Serie':<15} {'USB'}")
        print("  " + "-" * 60)

        lista = []
        for i, dev in enumerate(dispositivos):
            nombre = dev.get_info(rs.camera_info.name)
            serie = dev.get_info(rs.camera_info.serial_number)
            try:
                usb_tipo = dev.get_info(rs.camera_info.usb_type_descriptor)
            except Exception:
                usb_tipo = "?"
            print(f"  {i:<5} {nombre:<30} {serie:<15} {usb_tipo}")
            lista.append({"indice": i, "nombre": nombre, "serie": serie, "usb": usb_tipo})

        print("  " + "-" * 60)
        return lista


# ===========================================================================
# Uso directo: muestra video de la cámara
# ===========================================================================

if __name__ == "__main__":
    def procesar_frame(color_image, depth_image):
        """Ejemplo de función de procesamiento."""
        cv2.putText(color_image, "Press 'q' to exit", (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2, cv2.LINE_AA)

    cam = RealSenseCamera(indice_camara=0)
    try:
        cam.start()
        while True:
            datos = cam.capture_frame()
            if datos is None:
                continue

            procesar_frame(datos['color'], datos['depth_raw'])
            cv2.imshow("RealSense Video", datos['color'])

            if cv2.waitKey(1) & 0xFF == ord("q"):
                break
    finally:
        cam.stop()
        cv2.destroyAllWindows()
