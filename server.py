#!/usr/bin/env python3
"""Servidor simple que envía frames JPEG por TCP.

Uso:
    python server.py --host 0.0.0.0 --port 8000

Conecta con el cliente en la otra máquina: el cliente recibirá y mostrará vídeo.
"""
import socket
import struct
import argparse
import ssl
import socket
import struct
import cv2
import time
import sys

# Configuración de red
HOST = '0.0.0.0'  # Escuchar en todas las interfaces
PORT = 9999       # Puerto a usar (puede cambiarse)

def start_server(host=HOST, port=PORT):
    # Crear objeto de captura de video (cámara por defecto)
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("Error: no se pudo abrir la cámara.")
        return

    # Crear socket TCP y preparar para aceptar conexiones
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind((host, port))
    server_socket.listen(1)
    print(f"Escuchando en {host}:{port} ...")

    try:
        while True:
            # Esperar a que un cliente se conecte
            conn, addr = server_socket.accept()
            print(f"Conexión desde {addr}")
            try:
                # Bucle principal: capturar y enviar marcos continuamente
                while True:
                    ret, frame = cap.read()
                    if not ret:
                        # Si falla la lectura, esperar un momento y reintentar
                        time.sleep(0.1)
                        continue

                    # Codificar el marco a JPEG para reducir tamaño
                    # El tercer parámetro es la calidad JPEG (0-100)
                    encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), 80]
                    result, encoded_image = cv2.imencode('.jpg', frame, encode_param)
                    if not result:
                        # Si la codificación falla, saltar este marco
                        continue

                    data = encoded_image.tobytes()
                    # Enviar primero 4 bytes con el tamaño del bloque (big-endian)
                    size = struct.pack('!I', len(data))
                    try:
                        # Enviar tamaño + datos del marco
                        conn.sendall(size + data)
                    except (BrokenPipeError, ConnectionResetError):
                        # El cliente se desconectó inesperadamente
                        print("Cliente desconectado.")
                        break
                    # Pequeña pausa opcional para evitar saturar la red/cpu
                    time.sleep(0.01)
            except Exception as e:
                # Cualquier excepción del bucle de conexión se registra
                print(f"Error durante la transmisión: {e}")
            finally:
                # Cerrar la conexión actual y volver a esperar otra
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                conn.close()
                print("Conexión cerrada, esperando nuevo cliente...")
    except KeyboardInterrupt:
        # Permitir terminar el servidor con Ctrl+C
        print("\nServidor detenido por el usuario.")
    finally:
        # Liberar recursos: cámara y socket del servidor
        cap.release()
        server_socket.close()
        print("Recursos liberados, servidor finalizado.")

if __name__ == '__main__':
    # Permitir pasar host y puerto por argumentos opcionales
    if len(sys.argv) >= 2:
        HOST = sys.argv[1]
    if len(sys.argv) >= 3:
        PORT = int(sys.argv[2])
    start_server(HOST, PORT)
