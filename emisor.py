#!/usr/bin/env python3
"""Emisor: captura vídeo y lo envía por TCP a receptores.

Uso:
    python emisor.py [HOST] [PUERTO]

Ejemplo:
    python emisor.py 0.0.0.0 9999
"""
import socket
import struct
import cv2
import time
import sys

# Configuración de red
HOST = '0.0.0.0'  # Escuchar en todas las interfaces
PUERTO = 9999     # Puerto a usar (puede cambiarse)

def iniciar_emisor(host=HOST, puerto=PUERTO):
    # Crear objeto de captura de video (cámara por defecto)
    camara = cv2.VideoCapture(0)
    if not camara.isOpened():
        print("Error: no se pudo abrir la cámara.")
        return

    # Crear socket TCP y preparar para aceptar conexiones de receptores
    socket_emisor = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    socket_emisor.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    socket_emisor.bind((host, puerto))
    socket_emisor.listen(1)
    print(f"Escuchando en {host}:{puerto} ...")

    try:
        while True:
            # Esperar a que un receptor se conecte
            conexion, direccion = socket_emisor.accept()
            print(f"Receptor conectado desde {direccion}")
            try:
                # Bucle principal: capturar y enviar marcos continuamente
                while True:
                    ret, fotograma = camara.read()
                    if not ret:
                        # Si falla la lectura, esperar un momento y reintentar
                        time.sleep(0.1)
                        continue

                    # Codificar el fotograma a JPEG para reducir tamaño
                    # El tercer parámetro es la calidad JPEG (0-100)
                    parametros_codificacion = [int(cv2.IMWRITE_JPEG_QUALITY), 80]
                    resultado, imagen_codificada = cv2.imencode('.jpg', fotograma, parametros_codificacion)
                    if not resultado:
                        # Si la codificación falla, saltar este fotograma
                        continue

                    datos = imagen_codificada.tobytes()
                    # Enviar primero 4 bytes con el tamaño del bloque (big-endian)
                    tamaño = struct.pack('!I', len(datos))
                    try:
                        # Enviar tamaño + datos del fotograma
                        conexion.sendall(tamaño + datos)
                    except (BrokenPipeError, ConnectionResetError):
                        # El receptor se desconectó inesperadamente
                        print("Receptor desconectado.")
                        break
                    # Pequeña pausa opcional para evitar saturar la red/cpu
                    time.sleep(0.01)
            except Exception as e:
                # Cualquier excepción del bucle de conexión se registra
                print(f"Error durante la transmisión: {e}")
            finally:
                # Cerrar la conexión actual y volver a esperar otro receptor
                try:
                    conexion.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                conexion.close()
                print("Conexión cerrada, esperando nuevo receptor...")
    except KeyboardInterrupt:
        # Permitir terminar el emisor con Ctrl+C
        print("\nEmisor detenido por el usuario.")
    finally:
        # Liberar recursos: cámara y socket del emisor
        camara.release()
        socket_emisor.close()
        print("Recursos liberados, emisor finalizado.")

if __name__ == '__main__':
    # Permitir pasar host y puerto por argumentos opcionales
    if len(sys.argv) >= 2:
        HOST = sys.argv[1]
    if len(sys.argv) >= 3:
        PUERTO = int(sys.argv[2])
    iniciar_emisor(HOST, PUERTO)
