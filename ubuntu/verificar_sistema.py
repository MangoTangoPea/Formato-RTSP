#!/usr/bin/env python3
"""
Diagnóstico completo del sistema para RTSP con RealSense D435.

Ejecutar UNA SOLA VEZ después de instalar las dependencias para verificar
que todo está configurado correctamente. No es necesario ejecutarlo en
cada sesión de trabajo.

Uso:
    python3 verificar_sistema.py
"""

import os
import sys
import socket
import shutil
import subprocess


PUERTO_RTSP_DEFECTO = 8554


def verificar_python():
    """Muestra la versión de Python."""
    version = sys.version.split()[0]
    print(f"  [[OK]] Python {version}")
    print(f"      Ejecutable: {sys.executable}")
    return True


def verificar_opencv():
    """Verifica que OpenCV esté instalado."""
    try:
        import cv2
        print(f"  [[OK]] OpenCV {cv2.__version__}")
        return True
    except ImportError:
        print("  [[X]] OpenCV NO instalado")
        print("      Instalar con: pip install opencv-python")
        print("      Si falta libGL: sudo apt install libgl1 libglib2.0-0")
        return False


def verificar_numpy():
    """Verifica que NumPy esté instalado."""
    try:
        import numpy as np
        print(f"  [[OK]] NumPy {np.__version__}")
        return True
    except ImportError:
        print("  [[X]] NumPy NO instalado")
        print("      Instalar con: pip install numpy")
        return False


def verificar_pyrealsense2():
    """Verifica que pyrealsense2 esté instalado."""
    try:
        import pyrealsense2 as rs
        print(f"  [[OK]] pyrealsense2 disponible")
        return True
    except ImportError:
        print("  [[X]] pyrealsense2 NO instalado")
        print("      Opción A (pip): pip install pyrealsense2")
        print("      Opción B (repo Intel):")
        print("        sudo mkdir -p /etc/apt/keyrings")
        print("        curl -sSf https://librealsense.intel.com/Debian/librealsense.pgp \\")
        print("          | sudo tee /etc/apt/keyrings/librealsense.pgp > /dev/null")
        print('        echo "deb [signed-by=/etc/apt/keyrings/librealsense.pgp] \\')
        print("          https://librealsense.intel.com/Debian/apt-repo `lsb_release -cs` main\" \\")
        print("          | sudo tee /etc/apt/sources.list.d/librealsense.list")
        print("        sudo apt update")
        print("        sudo apt install librealsense2-dkms librealsense2-utils")
        print("        pip install pyrealsense2")
        return False


def verificar_ffmpeg():
    """Verifica que FFmpeg esté disponible en el sistema."""
    ruta = shutil.which("ffmpeg")
    if ruta:
        try:
            res = subprocess.run(
                [ruta, "-version"],
                capture_output=True, text=True, timeout=5
            )
            if res.returncode == 0:
                version = res.stdout.split("\n")[0] if res.stdout else "desconocida"
                print(f"  [[OK]] FFmpeg: {ruta}")
                print(f"      Versión: {version}")
                return True
        except Exception:
            pass

    print("  [[X]] FFmpeg NO encontrado")
    print("      Instalar con: sudo apt install ffmpeg")
    return False


def verificar_reglas_udev():
    """Verifica si las reglas udev de Intel RealSense están instaladas."""
    rutas_udev = [
        "/etc/udev/rules.d/99-realsense-libusb.rules",
        "/etc/udev/rules.d/99-realsense-d4xx.rules",
    ]
    for ruta in rutas_udev:
        if os.path.isfile(ruta):
            print(f"  [[OK]] Reglas udev: {ruta}")
            return True

    print("  [[!]] Reglas udev de RealSense no encontradas")
    print("      Esto puede causar errores de permisos USB.")
    print("      Instalar con:")
    print("        wget https://raw.githubusercontent.com/IntelRealSense/librealsense/master/config/99-realsense-libusb.rules")
    print("        sudo cp 99-realsense-libusb.rules /etc/udev/rules.d/")
    print("        sudo udevadm control --reload-rules && sudo udevadm trigger")
    return False


def verificar_dispositivos_usb():
    """Busca dispositivos Intel RealSense en el bus USB."""
    try:
        res = subprocess.run(
            ["lsusb"], capture_output=True, text=True, timeout=5
        )
        lineas = [
            l.strip() for l in res.stdout.split("\n")
            if "8086" in l and ("0b07" in l.lower() or "0ad" in l.lower()
                                or "realsense" in l.lower() or "0b3a" in l.lower()
                                or "0b5c" in l.lower() or "0b64" in l.lower())
        ]
        if not lineas:
            lineas = [
                l.strip() for l in res.stdout.split("\n")
                if "Intel Corp" in l and ("RealSense" in l or "D4" in l or "D5" in l)
            ]

        if lineas:
            print(f"  [[OK]] Dispositivo(s) RealSense en USB: {len(lineas)}")
            for d in lineas:
                print(f"      -> {d}")
            return True
        else:
            print("  [[!]] No se detectaron dispositivos RealSense en USB")
            print("      Verifica que la cámara esté conectada a un puerto USB 3.0")
            return False
    except FileNotFoundError:
        print("  [[!]] 'lsusb' no disponible (no es Linux o falta usbutils)")
        return False
    except Exception:
        print("  [[!]] Error al verificar dispositivos USB")
        return False


def verificar_camaras_realsense():
    """Intenta detectar cámaras usando pyrealsense2."""
    try:
        import pyrealsense2 as rs
        ctx = rs.context()
        dispositivos = ctx.query_devices()

        if len(dispositivos) > 0:
            print(f"  [[OK]] Cámaras RealSense detectadas: {len(dispositivos)}")
            for i, dev in enumerate(dispositivos):
                nombre = dev.get_info(rs.camera_info.name)
                serie = dev.get_info(rs.camera_info.serial_number)
                try:
                    usb = dev.get_info(rs.camera_info.usb_type_descriptor)
                except Exception:
                    usb = "?"
                print(f"      [{i}] {nombre} (S/N: {serie}, USB: {usb})")
            return True
        else:
            print("  [[!]] pyrealsense2 cargado pero no se detectan cámaras")
            print("      Verifica la conexión USB y las reglas udev")
            return False
    except ImportError:
        print("  [-] Omitido (pyrealsense2 no disponible)")
        return False
    except Exception as e:
        print(f"  [[!]] Error al detectar cámaras: {e}")
        return False


def verificar_puertos(puerto_base=PUERTO_RTSP_DEFECTO):
    """Verifica que los 4 puertos RTSP estén disponibles."""
    nombres = ["color", "depth", "ir1", "ir2"]
    todos_ok = True
    for i, nombre in enumerate(nombres):
        p = puerto_base + i
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1)
            result = s.connect_ex(("127.0.0.1", p))
            s.close()
            libre = result != 0
        except Exception:
            libre = True

        if libre:
            print(f"  [[OK]] Puerto {p} ({nombre}) disponible")
        else:
            print(f"  [[X]] Puerto {p} ({nombre}) EN USO")
            print(f"      Liberar con: sudo lsof -i :{p}")
            todos_ok = False

    return todos_ok


def main():
    """Ejecuta el diagnóstico completo."""
    print("\n" + "=" * 60)
    print("  DIAGNÓSTICO DEL SISTEMA - RTSP RealSense D435")
    print("=" * 60)

    print("\n-- Entorno Python --")
    verificar_python()

    print("\n-- Dependencias Python --")
    ok_cv2 = verificar_opencv()
    ok_np = verificar_numpy()
    ok_rs = verificar_pyrealsense2()

    print("\n-- Software del Sistema --")
    ok_ff = verificar_ffmpeg()

    print("\n-- Hardware y Permisos --")
    verificar_reglas_udev()
    verificar_dispositivos_usb()
    verificar_camaras_realsense()

    print("\n-- Puertos RTSP --")
    verificar_puertos()

    # Resumen
    print("\n" + "=" * 60)
    todo_ok = ok_cv2 and ok_np and ok_rs and ok_ff
    if todo_ok:
        print("  [OK] Sistema listo. Puedes ejecutar:")
        print("    python3 emisor.py")
        print("    python3 receptor.py <IP_DEL_EMISOR>")
    else:
        print("  [!] Hay dependencias faltantes. Revisa los mensajes arriba.")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
