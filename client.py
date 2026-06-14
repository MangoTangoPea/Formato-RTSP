#!/usr/bin/env python3
"""Cliente que recibe frames JPEG por TCP y los muestra.

Uso:
    python client.py --host 192.168.1.2 --port 8000
"""
import socket
import struct
import ssl
import argparse
import numpy as np
import cv2
import time


def recvall(sock: socket.socket, n: int) -> bytes:
    data = bytearray()
    while len(data) < n:
        packet = sock.recv(n - len(data))
        if not packet:
            return b''
        data.extend(packet)
    return bytes(data)


def receive_and_show(host: str, port: int, secure: bool, cafile: str) -> None:
    while True:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if secure:
                context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
                if cafile:
                    context.load_verify_locations(cafile)
                else:
                    context.check_hostname = False
                    context.verify_mode = ssl.CERT_NONE
                    print('Advertencia: TLS activo pero sin verificación de certificado (usar --cafile para verificar).')
                sock = context.wrap_socket(sock, server_hostname=host)
            print(f"Conectando a {host}:{port} ...")
            sock.connect((host, port))
            print('Conectado.')

            while True:
                # Leer longitud
                packed_len = recvall(sock, 4)
                if not packed_len:
                    print('Conexión cerrada por el servidor')
                    break

                msg_len = struct.unpack('!I', packed_len)[0]
                data = recvall(sock, msg_len)
                if not data:
                    print('No se recibieron datos')
                    break

                # Decodificar JPEG y mostrar
                npdata = np.frombuffer(data, dtype=np.uint8)
                frame = cv2.imdecode(npdata, cv2.IMREAD_COLOR)
                if frame is None:
                    continue

                import socket
                import struct
                import cv2
                import numpy as np
                import time
                import sys

                # Dirección del servidor a la que conectarse
                # Usar: python client.py <IP_DEL_SERVIDOR> <PUERTO>
                SERVER_IP = '127.0.0.1'
                SERVER_PORT = 9999
                RECONNECT_DELAY = 2  # segundos a esperar antes de reintentar

                def recvall(sock, count):
                    # Recibir exactamente 'count' bytes desde el socket
                    buf = b''
                    while len(buf) < count:
                        part = sock.recv(count - len(buf))
                        if not part:
                            # Si recv devuelve vacío, la conexión se cerró
                            return None
                        buf += part
                    return buf

                def start_client(server_ip=SERVER_IP, server_port=SERVER_PORT):
                    while True:
                        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        try:
                            print(f"Intentando conectar a {server_ip}:{server_port} ...")
                            sock.connect((server_ip, server_port))
                            print("Conectado al servidor. Recibiendo video...")
                            while True:
                                # Primero recibir 4 bytes con el tamaño del siguiente marco
                                size_data = recvall(sock, 4)
                                if not size_data:
                                    # Conexión cerrada por el servidor
                                    print("Conexión perdida (no se recibió el tamaño).")
                                    break
                                # Desempaquetar tamaño (big-endian unsigned int)
                                frame_size = struct.unpack('!I', size_data)[0]
                                # Recibir el bloque de bytes del marco según el tamaño
                                frame_data = recvall(sock, frame_size)
                                if not frame_data:
                                    print("Conexión perdida (datos incompletos).")
                                    break

                                # Convertir bytes recibidos a imagen OpenCV
                                np_data = np.frombuffer(frame_data, dtype=np.uint8)
                                frame = cv2.imdecode(np_data, cv2.IMREAD_COLOR)
                                if frame is None:
                                    # Si la decodificación falla, saltar este marco
                                    continue

                                # Mostrar el marco en una ventana
                                cv2.imshow('Stream (presione q para salir)', frame)
                                # Salir si el usuario presiona 'q'
                                if cv2.waitKey(1) & 0xFF == ord('q'):
                                    print("Usuario solicitó salir.")
                                    sock.close()
                                    cv2.destroyAllWindows()
                                    return
                        except ConnectionRefusedError:
                            # No hay servidor escuchando en esa IP/puerto
                            print(f"No se pudo conectar a {server_ip}:{server_port}. Reintentando en {RECONNECT_DELAY}s...")
                            time.sleep(RECONNECT_DELAY)
                        except KeyboardInterrupt:
                            # Permitir salir con Ctrl+C
                            print("\nCliente detenido por el usuario.")
                            try:
                                sock.close()
                            except Exception:
                                pass
                            cv2.destroyAllWindows()
                            return
                        except Exception as e:
                            # Capturar otras excepciones y reintentar
                            print(f"Error de conexión/recepción: {e}. Reintentando en {RECONNECT_DELAY}s...")
                            try:
                                sock.close()
                            except Exception:
                                pass
                            time.sleep(RECONNECT_DELAY)

                if __name__ == '__main__':
                    # Permitir pasar IP y puerto por línea de comandos
                    if len(sys.argv) >= 2:
                        SERVER_IP = sys.argv[1]
                    if len(sys.argv) >= 3:
                        SERVER_PORT = int(sys.argv[2])
                    start_client(SERVER_IP, SERVER_PORT)
