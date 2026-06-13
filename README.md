# Demo RTSP (Cliente/Servidor local)

Código mínimo para transmitir vídeo desde una cámara (servidor) a un cliente en la red local.

## Requisitos

- Python 3.8+
- Instalar dependencias:

```bash
pip install -r requirements.txt
```

## Uso

1. Ejecutar el servidor en la máquina que tiene la cámara:

```bash
python server.py --host 0.0.0.0 --port 8000
```

2. Ejecutar el cliente desde la otra máquina (usar la IP del servidor):

```bash
python client.py --host 192.168.x.y --port 8000
```

- Pulsa `q` en la ventana del cliente para salir.
- Ajusta `--cam` en el servidor si hay varias cámaras, y `--quality` para cambiar la compresión JPEG.

## Notas

- Este ejemplo usa TCP y envía frames JPEG en bruto. No es optimizado ni cifrado.
- Para producción, considerar protocolos como RTSP, WebRTC o añadir compresión/seguridad.
