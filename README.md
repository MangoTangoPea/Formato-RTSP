# Demo RTSP (Cliente/Servidor local)

Código mínimo para transmitir vídeo desde una cámara (servidor) a un cliente en la red local.

## Requisitos

- Python 3.8+

## Preparar entorno virtual

Crea y activa un entorno virtual antes de instalar las dependencias:

```powershell
cd "C:\ruta\a\tu\proyecto"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Después instala las dependencias:

```bash
pip install -r requirements.txt
```

Cuando termines, desactiva el entorno virtual:

```powershell
Deactivate
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

### Conexión segura (opcional)

Para iniciar una conexión segura usa `--secure` en ambos lados. En el servidor:

```bash
python server.py --host 0.0.0.0 --port 8000 --secure --certfile cert.pem --keyfile key.pem
```

Y en el cliente:

```bash
python client.py --host 192.168.x.y --port 8000 --secure --cafile cert.pem
```

Para quitar la conexión segura, simplemente omite la opción `--secure` en el servidor y en el cliente.

- Pulsa `q` en la ventana del cliente para salir.
- Ajusta `--cam` en el servidor si hay varias cámaras, y `--quality` para cambiar la compresión JPEG.

## Notas

- Este ejemplo usa TCP y envía frames JPEG en bruto. No es optimizado ni cifrado.
- Para producción, considerar protocolos como RTSP, WebRTC o añadir compresión/seguridad.
