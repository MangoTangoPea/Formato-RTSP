"""
Módulo servidor_rtsp.py
Gestión automática del servidor MediaMTX y de los publicadores FFmpeg.
"""

import os
import sys
import subprocess
import time
import socket
import shutil
import urllib.request
import tarfile
import stat
import glob

# Constantes de MediaMTX
MEDIAMTX_VERSION = "v1.12.2"
DIR_BASE = os.path.dirname(os.path.abspath(__file__))
DIR_MEDIAMTX = os.path.join(DIR_BASE, "mediamtx_linux")

def detectar_arquitectura():
    """Detecta la arquitectura del procesador (x86_64 o ARM64/aarch64)."""
    try:
        resultado = subprocess.run(["uname", "-m"], capture_output=True, text=True, timeout=5)
        arch = resultado.stdout.strip().lower()
    except Exception:
        arch = "x86_64"

    mapa = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64v8",
        "arm64": "arm64v8",
        "armv7l": "armv7",
        "armhf": "armv7",
    }
    return mapa.get(arch, "amd64"), arch

def buscar_ffmpeg():
    """Busca el ejecutable de FFmpeg en el sistema o via imageio-ffmpeg."""
    # Intentar el del sistema (instalado vía apt/pacman/etc)
    ruta = shutil.which("ffmpeg")
    if ruta:
        return ruta, "sistema"

    # Fallback a imageio-ffmpeg
    try:
        import imageio_ffmpeg
        ruta = imageio_ffmpeg.get_ffmpeg_exe()
        if ruta:
            return ruta, "imageio-ffmpeg"
    except ImportError:
        pass

    return None, None

def verificar_puerto_disponible(puerto):
    """Retorna True si el puerto TCP está libre en local."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        res = s.connect_ex(("127.0.0.1", puerto))
        s.close()
        return res != 0
    except Exception:
        return True

def descargar_mediamtx():
    """Descarga y extrae MediaMTX en mediamtx_linux/ si no está ya instalado."""
    exe_path = os.path.join(DIR_MEDIAMTX, "mediamtx")

    if os.path.isfile(exe_path) and os.access(exe_path, os.X_OK):
        return exe_path

    arch_mtx, arch_raw = detectar_arquitectura()
    url = (
        f"https://github.com/bluenviron/mediamtx/releases/download/"
        f"{MEDIAMTX_VERSION}/mediamtx_{MEDIAMTX_VERSION}_linux_{arch_mtx}.tar.gz"
    )

    print(f"  ↓ Descargando MediaMTX {MEDIAMTX_VERSION} para linux/{arch_raw} ...")
    os.makedirs(DIR_MEDIAMTX, exist_ok=True)
    tar_path = os.path.join(DIR_MEDIAMTX, "mediamtx.tar.gz")

    try:
        urllib.request.urlretrieve(url, tar_path)
    except Exception as e:
        print(f"  ✗ Error de red al descargar MediaMTX: {e}")
        print(f"    Intente descargar manualmente de: {url}")
        sys.exit(1)

    print("  ↓ Extrayendo binarios...")
    try:
        with tarfile.open(tar_path, "r:gz") as tar:
            tar.extractall(DIR_MEDIAMTX)
    except Exception as e:
        print(f"  ✗ Error al descomprimir MediaMTX: {e}")
        if os.path.exists(tar_path):
            os.remove(tar_path)
        sys.exit(1)

    if os.path.exists(tar_path):
        os.remove(tar_path)

    # Buscar el ejecutable en caso de que esté anidado
    if not os.path.isfile(exe_path):
        encontrados = glob.glob(os.path.join(DIR_MEDIAMTX, "**", "mediamtx"), recursive=True)
        if encontrados:
            shutil.copy2(encontrados[0], exe_path)
        else:
            print("  ✗ No se encontró el binario 'mediamtx' en el archivo extraído.")
            sys.exit(1)

    # Dar permisos de ejecución
    os.chmod(exe_path, os.stat(exe_path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    print(f"  ✓ MediaMTX instalado en: {exe_path}")
    return exe_path

def limpiar_procesos_anteriores():
    """Intenta matar cualquier proceso previo de mediamtx o ffmpeg para liberar puertos y recursos."""
    print("  → Limpiando procesos mediamtx y ffmpeg previos...")
    try:
        if sys.platform.startswith("linux"):
            subprocess.run(["killall", "-9", "mediamtx"], capture_output=True, timeout=5)
            subprocess.run(["killall", "-9", "ffmpeg"], capture_output=True, timeout=5)
    except Exception:
        pass

class AdministradorRTSP:
    def __init__(self, puerto_rtsp=8554):
        self.puerto_rtsp = puerto_rtsp
        self.proceso_mediamtx = None
        self.procesos_ff = {}
        self.log_files = {}  # Para guardar referencias de archivos de log y cerrarlos limpiamente
        self.ruta_ffmpeg = None
        self.activo = False

        # Asegurar directorio de logs
        self.dir_logs = os.path.join(DIR_BASE, "logs")
        os.makedirs(self.dir_logs, exist_ok=True)

    def iniciar_servidor(self):
        """Descarga e inicia MediaMTX en el puerto seleccionado."""
        # Limpieza previa de puertos y procesos colgados
        limpiar_procesos_anteriores()

        # Buscar FFmpeg primero
        self.ruta_ffmpeg, origen = buscar_ffmpeg()
        if not self.ruta_ffmpeg:
            raise RuntimeError("FFmpeg no está instalado en el sistema. Instálelo con: sudo apt install ffmpeg")
        print(f"  ✓ FFmpeg encontrado: {self.ruta_ffmpeg} ({origen})")

        # Iniciar MediaMTX
        exe_path = descargar_mediamtx()
        
        if not verificar_puerto_disponible(self.puerto_rtsp):
            raise RuntimeError(f"El puerto RTSP {self.puerto_rtsp} ya está siendo usado por otro proceso.")

        entorno = os.environ.copy()
        entorno["MTX_RTSPADDRESS"] = f":{self.puerto_rtsp}"

        print(f"  → Levantando servidor MediaMTX en el puerto {self.puerto_rtsp} ...")
        try:
            log_path = os.path.join(self.dir_logs, "mediamtx.log")
            log_file = open(log_path, "w", encoding="utf-8")
            self.log_files["mediamtx"] = log_file

            self.proceso_mediamtx = subprocess.Popen(
                [exe_path],
                cwd=DIR_MEDIAMTX,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env=entorno
            )
        except Exception as e:
            raise RuntimeError(f"Fallo al invocar el binario de MediaMTX: {e}")

        # Comprobar que no haya muerto inmediatamente
        time.sleep(2)
        if self.proceso_mediamtx.poll() is not None:
            try:
                with open(log_path, "r", encoding="utf-8") as lf:
                    salida = lf.read()[:400]
            except Exception:
                salida = "No se pudo leer el log de MediaMTX."
            raise RuntimeError(f"MediaMTX falló al iniciar. Log:\n{salida}")

        print(f"  ✓ MediaMTX corriendo correctamente (PID: {self.proceso_mediamtx.pid})")
        self.activo = True

    def iniciar_publicador(self, canal, ancho, alto, pix_fmt, fps, bitrate_kbps):
        """
        Lanza un proceso FFmpeg en modo PUSH.
        Toma frames raw desde stdin y los empuja a MediaMTX vía TCP.
        Redirige stderr a un archivo de log para evitar bloqueos por buffer.
        """
        if not self.activo:
            return False

        url_destino = f"rtsp://127.0.0.1:{self.puerto_rtsp}/{canal}"
        
        cmd = [
            self.ruta_ffmpeg,
            "-y",
            "-f", "rawvideo",
            "-vcodec", "rawvideo",
            "-pix_fmt", pix_fmt,
            "-s", f"{ancho}x{alto}",
            "-r", str(fps),
            "-i", "-",                          # Leer de stdin
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-preset", "ultrafast",             # Carga mínima de CPU
            "-tune", "zerolatency",             # Latencia ultra baja
            "-b:v", f"{bitrate_kbps}k",
            "-maxrate", f"{bitrate_kbps}k",
            "-bufsize", f"{bitrate_kbps * 2}k",
            "-g", str(fps * 2),                 # GOP de 2s
            "-an",                              # Desactivar audio explícitamente para evitar malas interpretaciones del códec
            "-flags:v", "+global_header",       # Escribir cabeceras globales H.264 (SPS/PPS) en el SDP
            "-f", "rtsp",
            "-rtsp_transport", "tcp",
            url_destino
        ]

        try:
            log_path = os.path.join(self.dir_logs, f"ffmpeg_{canal}.log")
            log_file = open(log_path, "w", encoding="utf-8")
            self.log_files[canal] = log_file

            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=log_file
            )
            self.procesos_ff[canal] = proc
            return True
        except Exception as e:
            print(f"  ✗ Error al levantar publicador FFmpeg para '{canal}': {e}")
            return False

    def enviar_frame(self, canal, frame_bytes):
        """Envía bytes de vídeo crudos al proceso FFmpeg del canal correspondiente."""
        proc = self.procesos_ff.get(canal)
        if not proc or proc.poll() is not None:
            return False

        try:
            proc.stdin.write(frame_bytes)
            proc.stdin.flush()
            return True
        except Exception:
            return False

    def verificar_publicadores(self):
        """Verifica si todos los publicadores FFmpeg siguen vivos."""
        for canal, proc in list(self.procesos_ff.items()):
            if proc.poll() is not None:
                log_path = os.path.join(self.dir_logs, f"ffmpeg_{canal}.log")
                print(f"  ✗ El publicador del canal '{canal}' se detuvo inesperadamente.")
                print(f"    Consulte el log detallado en: {log_path}")
                
                # Intentar leer las últimas líneas del log para mostrar al usuario
                try:
                    if os.path.exists(log_path):
                        with open(log_path, "r", encoding="utf-8") as lf:
                            lineas = lf.readlines()
                            if lineas:
                                print("    Últimas líneas del log:")
                                for l in lineas[-5:]:
                                    print(f"      {l.strip()}")
                except Exception:
                    pass
                return False
        return True

    def detener_todo(self):
        """Detiene MediaMTX y todos los procesos FFmpeg."""
        self.activo = False
        print("\n  → Cerrando publicadores FFmpeg...")
        for canal, proc in self.procesos_ff.items():
            if proc:
                if proc.stdin:
                    try:
                        proc.stdin.close()
                    except Exception:
                        pass
                if proc.poll() is None:
                    try:
                        proc.terminate()
                        proc.wait(timeout=2)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
        self.procesos_ff.clear()

        if self.proceso_mediamtx:
            print("  → Deteniendo servidor MediaMTX...")
            if self.proceso_mediamtx.poll() is None:
                try:
                    self.proceso_mediamtx.terminate()
                    self.proceso_mediamtx.wait(timeout=3)
                except Exception:
                    try:
                        self.proceso_mediamtx.kill()
                    except Exception:
                        pass
            self.proceso_mediamtx = None

        # Cerrar manejadores de archivos de log después de detener los procesos
        for canal, log_file in list(self.log_files.items()):
            try:
                log_file.close()
            except Exception:
                pass
        self.log_files.clear()

        print("  ✓ Todos los servicios RTSP y codificadores detenidos.")
