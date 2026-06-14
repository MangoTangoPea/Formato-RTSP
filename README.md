# Demo RTSP (Cliente/Servidor local)

Proyecto mínimo para transmitir vídeo desde una cámara (servidor) a un cliente en la red local.

## Requisitos

- Python 3.8+
- dependencias listadas en `requirements.txt` (`opencv-python`, `numpy`)

## Preparar entorno virtual (Windows PowerShell)

```powershell
cd "C:\Users\Luis Fdo\Documents\GitHub\Demo RTSP"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Para salir del entorno virtual:

```powershell
Deactivate
```

## Cómo ejecutar

Nota: los scripts incluidos en este repositorio aceptan argumentos posicionales simples.

- Ejecutar el servidor (máquina con la cámara):

```powershell
python server.py [HOST] [PORT]
# Ejemplo: python server.py 0.0.0.0 9999
```

- Ejecutar el cliente (máquina que verá el vídeo):

```powershell
python client.py [IP_DEL_SERVIDOR] [PUERTO]
# Ejemplo: python client.py 192.168.1.42 9999
```

- Para salir del cliente: presionar `q` en la ventana del stream.
- Para detener el servidor: usar `Ctrl+C` en la terminal donde se ejecuta.

## Explicación general (visión rápida)

El sistema sigue un patrón cliente-servidor simple sobre TCP:

- El `servidor` captura marcos de la cámara con OpenCV, los codifica como JPEG y envía cada marco como un bloque de bytes precedido por su tamaño (4 bytes, entero sin signo en big-endian).
- El `cliente` se conecta por TCP, lee primero los 4 bytes que indican el tamaño del siguiente marco, luego recibe exactamente esa cantidad de bytes, decodifica el JPEG a una imagen OpenCV y la muestra en una ventana en tiempo real.

Ambos scripts incluyen manejo básico de excepciones para reconexión y para liberar recursos correctamente.

## Explicación detallada de `server.py`

1) Imports y configuración

- `socket`, `struct`: manejo de sockets TCP y empaquetado/desempaquetado de enteros (tamaño de frame).
- `cv2` (OpenCV): captura y codificación de imágenes.
- `time`, `sys`: utilidades (pausas y lectura de argumentos).

2) Configuración de red

- `HOST` y `PORT`: valores por defecto. `HOST='0.0.0.0'` permite aceptar conexiones desde cualquier interfaz.

3) Apertura de la cámara

- `cv2.VideoCapture(0)` abre la cámara por defecto. Si hay varias cámaras, cambiar el índice `0` por `1`, etc.

4) Bucle de aceptación de clientes

- El servidor crea un socket TCP y llama a `listen(1)` para aceptar una conexión entrante a la vez.
- `accept()` devuelve una tupla `(conn, addr)` donde `conn` es un socket nuevo para comunicarse con ese cliente.

5) Captura, codificación y envío de frames

- Cada iteración del bucle principal captura un frame con `cap.read()`.
- Se codifica con `cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])` para obtener bytes JPEG comprimidos. Ajustar la calidad reduce o aumenta el tamaño.
- Antes de enviar los bytes, se envían 4 bytes con `struct.pack('!I', len(data))` que indican el tamaño del bloque. `!I` significa entero sin signo (4 bytes) en orden de bytes big-endian.
- Se llama a `conn.sendall(size + data)` para enviar primero el tamaño y luego los datos del frame. `sendall` garantiza que se intente enviar todo el buffer.

6) Manejo de desconexiones y excepciones

- Si el cliente se desconecta inesperadamente (`BrokenPipeError`, `ConnectionResetError`) el servidor detecta la excepción y vuelve a esperar otra conexión.
- `try/finally` asegura que la conexión se cierre y que la cámara y el socket del servidor se liberen al detenerse.

## Explicación detallada de `client.py`

1) Imports y configuración

- `socket`, `struct`: manejo de conexión y lectura del tamaño del frame.
- `cv2`, `numpy`: decodificación de los bytes JPEG y visualización.
- `time`, `sys`: utilidades para reintentos y argumentos.

2) Función `recvall(sock, count)`

- `recv` puede devolver menos bytes de los solicitados. `recvall` llama repetidamente hasta leer exactamente `count` bytes o detectar que la conexión se cerró.

3) Conexión al servidor

- El cliente intenta conectar con `sock.connect((server_ip, server_port))`. Si no hay servidor, atrapa `ConnectionRefusedError` y vuelve a intentar después de un retardo.

4) Recepción y reconstrucción de frames

- Lee primero 4 bytes: `size_data = recvall(sock, 4)` y desempaqueta con `struct.unpack('!I', size_data)[0]` para obtener el tamaño del marco.
- Luego llama a `recvall(sock, frame_size)` para recibir exactamente los bytes JPEG.
- Convierte los bytes a un arreglo NumPy (`np.frombuffer`) y decodifica con `cv2.imdecode` para obtener la imagen en BGR lista para mostrar.

5) Visualización y control

- Los frames se muestran con `cv2.imshow` y `cv2.waitKey(1)` para refrescar la ventana.
- Presionar `q` cierra el cliente limpiamente.

6) Reintentos y manejo de errores

- Si la conexión se pierde o los datos vienen incompletos, el cliente cierra el socket y entra en un bucle que intenta reconectar cada `RECONNECT_DELAY` segundos.

## Consejos prácticos y consideraciones de red

- Asegúrate de que el firewall permita conexiones entrantes en el puerto elegido en la máquina servidor.
- Usa la IP LAN (por ejemplo `192.168.1.42`) del servidor cuando ejecutes el cliente desde otra máquina en la misma red.
- Si necesitas mejor rendimiento o menor latencia, reduce la calidad JPEG (por ejemplo 60) o emplea compresión/streaming optimizado.
- Para entornos reales y producción considera protocolos diseñados para vídeo (RTSP, WebRTC) y cifrado.

## Notas finales

Estos scripts son un ejemplo didáctico y funcional para redes locales. Sirven como base para experimentar y extender con features como múltiples clientes, autenticación, cifrado TLS, o transmisión con menor latencia.

---
Si quieres, actualizo el `requirements.txt` o añado un ejemplo de `systemd`/servicio para ejecutar el servidor automáticamente.
