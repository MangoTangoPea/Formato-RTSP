#!/usr/bin/env python3
"""Servidor simple que envía frames JPEG por TCP.

Uso:
    python server.py --host 0.0.0.0 --port 8000

Conecta con el cliente en la otra máquina: el cliente recibirá y mostrará vídeo.
"""
import socket
import struct
import argparse
import cv2


def send_frames(host: str, port: int, cam: int, quality: int) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(1)
    print(f"Escuchando en {host}:{port} ...")

    conn, addr = sock.accept()
    print(f"Conexión entrante desde {addr}")

    cap = cv2.VideoCapture(cam)
    if not cap.isOpened():
        print("No se pudo abrir la cámara")
        conn.close()
        sock.close()
        return

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                break

            # Codificar frame como JPEG
            encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), quality]
            result, encoded = cv2.imencode('.jpg', frame, encode_param)
            if not result:
                continue

            data = encoded.tobytes()
            # Enviar longitud (4 bytes, network order) + datos
            length = struct.pack('!I', len(data))
            conn.sendall(length + data)

    except Exception as e:
        print('Error:', e)
    finally:
        cap.release()
        conn.close()
        sock.close()


def main():
    p = argparse.ArgumentParser(description='Servidor de vídeo TCP (envía frames JPEG)')
    p.add_argument('--host', default='0.0.0.0', help='IP para enlazar (por defecto 0.0.0.0)')
    p.add_argument('--port', type=int, default=8000, help='Puerto (por defecto 8000)')
    p.add_argument('--cam', type=int, default=0, help='Índice de la cámara (por defecto 0)')
    p.add_argument('--quality', type=int, default=80, help='Calidad JPEG 0-100 (por defecto 80)')
    args = p.parse_args()

    send_frames(args.host, args.port, args.cam, args.quality)


if __name__ == '__main__':
    main()
