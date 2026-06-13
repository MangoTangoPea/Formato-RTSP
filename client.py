#!/usr/bin/env python3
"""Cliente que recibe frames JPEG por TCP y los muestra.

Uso:
    python client.py --host 192.168.1.2 --port 8000
"""
import socket
import struct
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


def receive_and_show(host: str, port: int) -> None:
    while True:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
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

                cv2.imshow('Cliente - stream', frame)
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    sock.close()
                    cv2.destroyAllWindows()
                    return

        except Exception as e:
            print('Error / reconectando:', e)
            try:
                sock.close()
            except Exception:
                pass
            time.sleep(1)


def main():
    p = argparse.ArgumentParser(description='Cliente de vídeo TCP')
    p.add_argument('--host', required=True, help='IP del servidor')
    p.add_argument('--port', type=int, default=8000, help='Puerto (por defecto 8000)')
    args = p.parse_args()

    receive_and_show(args.host, args.port)


if __name__ == '__main__':
    main()
