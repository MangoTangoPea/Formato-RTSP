#!/usr/bin/env python3
"""Receptor: se conecta al emisor y recibe vídeo por TCP.

Uso:
    python receptor.py [IP_DEL_EMISOR] [PUERTO]

Ejemplo:
    python receptor.py 192.168.0.9 9999
"""
import socket
import struct
import cv2
import numpy as np
import time
import sys

# Dirección del emisor a la que conectarse
IP_EMISOR = '127.0.0.1'
PUERTO_EMISOR = 9999
RETARDO_RECONEXION = 2  # segundos a esperar antes de reintentar

def recibir_todo(socket_conexion, cantidad):
    """Recibir exactamente 'cantidad' bytes desde el socket"""
    buffer = b''
    while len(buffer) < cantidad:
        parte = socket_conexion.recv(cantidad - len(buffer))
        if not parte:
            # Si recv devuelve vacío, la conexión se cerró
            return None
        buffer += parte
    return buffer

def iniciar_receptor(ip_emisor=IP_EMISOR, puerto_emisor=PUERTO_EMISOR):
    """Conectar al emisor y recibir frames de video"""
    while True:
        socket_receptor = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            print(f"Intentando conectar a {ip_emisor}:{puerto_emisor} ...")
            socket_receptor.connect((ip_emisor, puerto_emisor))
            print("Conectado al emisor. Recibiendo video...")
            
            while True:
                # Primero recibir 4 bytes con el tamaño del siguiente fotograma
                datos_tamaño = recibir_todo(socket_receptor, 4)
                if not datos_tamaño:
                    # Conexión cerrada por el emisor
                    print("Conexión perdida (no se recibió el tamaño).")
                    break
                
                # Desempaquetar tamaño (big-endian unsigned int)
                tamaño_fotograma = struct.unpack('!I', datos_tamaño)[0]
                
                # Recibir el bloque de bytes del fotograma según el tamaño
                datos_fotograma = recibir_todo(socket_receptor, tamaño_fotograma)
                if not datos_fotograma:
                    print("Conexión perdida (datos incompletos).")
                    break

                # Convertir bytes recibidos a imagen OpenCV
                datos_numpy = np.frombuffer(datos_fotograma, dtype=np.uint8)
                fotograma = cv2.imdecode(datos_numpy, cv2.IMREAD_COLOR)
                if fotograma is None:
                    # Si la decodificación falla, saltar este fotograma
                    continue

                # Voltear la imagen (rotación 180 grados)
                fotograma = cv2.flip(fotograma, -1)

                # Mostrar el fotograma en una ventana
                cv2.imshow('Video en directo (presione q para salir)', fotograma)
                # Salir si el usuario presiona 'q'
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    print("Usuario solicitó salir.")
                    socket_receptor.close()
                    cv2.destroyAllWindows()
                    return
        
        except ConnectionRefusedError:
            # No hay emisor escuchando en esa IP/puerto
            print(f"No se pudo conectar a {ip_emisor}:{puerto_emisor}. Reintentando en {RETARDO_RECONEXION}s...")
            time.sleep(RETARDO_RECONEXION)
        
        except KeyboardInterrupt:
            # Permitir salir con Ctrl+C
            print("\nReceptor detenido por el usuario.")
            try:
                socket_receptor.close()
            except Exception:
                pass
            cv2.destroyAllWindows()
            return
        
        except Exception as e:
            # Capturar otras excepciones y reintentar
            print(f"Error de conexión/recepción: {e}. Reintentando en {RETARDO_RECONEXION}s...")
            try:
                socket_receptor.close()
            except Exception:
                pass
            time.sleep(RETARDO_RECONEXION)


if __name__ == '__main__':
    # Permitir pasar IP y puerto por línea de comandos
    if len(sys.argv) >= 2:
        IP_EMISOR = sys.argv[1]
    if len(sys.argv) >= 3:
        PUERTO_EMISOR = int(sys.argv[2])
    iniciar_receptor(IP_EMISOR, PUERTO_EMISOR)
