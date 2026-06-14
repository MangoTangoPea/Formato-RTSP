# Demo RTSP (Emisor/Receptor local)

Proyecto mínimo para transmitir vídeo desde una cámara (emisor) a un receptor en la red local.

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

## ¿Qué es una dirección IP? (Explicación simple)

Imagina que tienes dos casas y quieres enviar una carta de una a otra. Necesitas saber la dirección de la otra casa, ¿verdad?

**Las computadoras es lo mismo:** cada computadora en una red tiene una "dirección" llamada **dirección IP**. Esta dirección permite que una computadora encuentre y se comunique con otra.

**Ejemplo de dirección IP:** `192.168.1.42`

Una dirección IP es un número de 4 partes separadas por puntos. Cada parte es un número entre 0 y 255.

### ¿Cómo encontrar la dirección IP del emisor?

En la máquina donde está el **emisor** (la que tiene la cámara), abre PowerShell y ejecuta:

```powershell
ipconfig
```

Verás algo como esto:

```
Adaptador de Ethernet:
   Dirección IPv4. . . . . . . . . . : 192.168.1.42
   Máscara de subred . . . . . . . . : 255.255.255.0
   Puerta de enlace predeterminada . : 192.168.1.1
```

**Busca la línea "Dirección IPv4"** y copia ese número (en este ejemplo `192.168.1.42`).

**Nota importante:**
- Si tu computadora está conectada por **WiFi**, busca bajo la sección "Adaptador inalámbrico".
- Si está conectada por **cable Ethernet**, busca bajo esa sección.
- El número que ves **cambia solo si reinicia la computadora o desconectas de la red**. Normalmente es el mismo cada vez.

### Verificar que ambas máquinas están en la misma red

Para que el receptor pueda encontrar al emisor, **ambas máquinas deben estar en la misma red local (WiFi o cable)**. Usa este comando en el **receptor** para verificar que ve al emisor:

```powershell
ping 192.168.1.42
```

Reemplaza `192.168.0.9` con la IP real del emisor.

Si ves mensajes como `Reply from 192.168.0.9`, significa que están conectadas. ✓

Si ves `Timed out` o `no se alcanzó el host`, significa que no se ven. En ese caso:
1. Verifica que ambas máquinas estén en la misma red WiFi o cable.
2. Desactiva el firewall de Windows temporalmente (Control Panel > Windows Defender Firewall > Turn off).

## Cómo ejecutar

### Paso 1: Encuentra la IP del emisor

En la máquina con la cámara, abre PowerShell y escribe:

```powershell
ipconfig
```

Busca la línea **"Dirección IPv4"** y copia ese número. Por ejemplo: `192.168.0.9`

### Paso 2: Ejecuta el emisor

En la máquina con la cámara, en PowerShell, ejecuta:

```powershell
python emisor.py 0.0.0.0 9999
```

Deberías ver un mensaje como:
```
✓ Cámara abierta (índice 0)
Escuchando en 0.0.0.0:9999 ...
```

**Si ves un error de cámara**, es normal si no tienes cámara conectada. Para hacer pruebas, usa:
```powershell
python emisor.py 0.0.0.0 9999 --test
```

### Paso 3: Ejecuta el receptor

En la otra máquina, abre PowerShell, ve a la carpeta del proyecto e ejecuta:

```powershell
cd "C:\ruta\a\tu\proyecto"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python receptor.py 192.168.0.9 9999
```

Reemplaza `192.168.0.9` con la IP que encontraste en el Paso 1.

Deberías ver un mensaje:
```
Intentando conectar a 192.168.0.9:9999 ...
Conectado al emisor. Recibiendo video...
```

Y una ventana mostrará el vídeo en tiempo real.

### Detener los programas

- **Receptor:** presiona `q` en la ventana del vídeo.
- **Emisor:** presiona `Ctrl+C` en PowerShell.

## Resolución de problemas comunes

### "Se produjo un error durante el intento de conexión"

**Causa:** El receptor no puede alcanzar al emisor.

**Soluciones:**
1. Verifica que el emisor esté ejecutándose (deberías ver "Escuchando en 0.0.0.0:9999").
2. Verifica que ambas máquinas estén en la **misma red WiFi o cable**.
3. Verifica que usaste la **IP correcta** del emisor en el receptor.
4. Prueba hacer ping:
   ```powershell
   ping 192.168.0.9
   ```
   Si ves "Timed out", hay un problema de red.

### "No se pudo abrir la cámara"

Soluciones:
1. Asegúrate de que la cámara esté conectada y no en uso por otro programa.
2. Intenta con otro índice:
   ```powershell
   python emisor.py 0.0.0.0 9999 --cam 1
   ```
3. Usa modo de prueba (genera frames de prueba sin necesidad de cámara):
   ```powershell
   python emisor.py 0.0.0.0 9999 --test
   ```

### El vídeo es muy lento o entrecortado

Opciones para mejorar:
- Reduce la calidad JPEG (menos bytes = más rápido).
- Usa una red más rápida (WiFi 5GHz en lugar de 2.4GHz).
- Acerca las máquinas (menos interferencias).
- Reduce la resolución de la cámara.

Nota: los scripts incluidos en este repositorio aceptan argumentos posicionales simples.

- Ejecutar el emisor (máquina con la cámara):

```powershell
python emisor.py [HOST] [PUERTO]
# Ejemplo: python emisor.py 0.0.0.0 9999
```

- Ejecutar el receptor (máquina que verá el vídeo):

```powershell
python receptor.py [IP_DEL_EMISOR] [PUERTO]
# Ejemplo: python receptor.py 192.168.0.9 9999
```

- Para salir del receptor: presionar `q` en la ventana del stream.
- Para detener el emisor: usar `Ctrl+C` en la terminal donde se ejecuta.

## Explicación general (visión rápida)

El sistema sigue un patrón emisor-receptor simple sobre TCP:

- El `emisor` captura marcos de la cámara con OpenCV, los codifica como JPEG y envía cada marco como un bloque de bytes precedido por su tamaño (4 bytes, entero sin signo en big-endian).
- El `receptor` se conecta por TCP, lee primero los 4 bytes que indican el tamaño del siguiente marco, luego recibe exactamente esa cantidad de bytes, decodifica el JPEG a una imagen OpenCV y la muestra en una ventana en tiempo real.

Ambos scripts incluyen manejo básico de excepciones para reconexión y para liberar recursos correctamente.

## Explicación detallada de `emisor.py`

1) Imports y configuración

- `socket`, `struct`: manejo de sockets TCP y empaquetado/desempaquetado de enteros (tamaño de frame).
- `cv2` (OpenCV): captura y codificación de imágenes.
- `time`, `sys`: utilidades (pausas y lectura de argumentos).

2) Configuración de red

- `HOST` y `PUERTO`: valores por defecto. `HOST='0.0.0.0'` permite aceptar conexiones desde cualquier interfaz.

3) Apertura de la cámara

- `cv2.VideoCapture(0)` abre la cámara por defecto. Si hay varias cámaras, cambiar el índice `0` por `1`, etc.

4) Bucle de aceptación de receptores

- El emisor crea un socket TCP y llama a `listen(1)` para aceptar una conexión entrante a la vez.
- `accept()` devuelve una tupla `(conexion, direccion)` donde `conexion` es un socket nuevo para comunicarse con ese receptor.

5) Captura, codificación y envío de fotogramas

- Cada iteración del bucle principal captura un fotograma con `camara.read()`.
- Se codifica con `cv2.imencode('.jpg', fotograma, [cv2.IMWRITE_JPEG_QUALITY, 80])` para obtener bytes JPEG comprimidos. Ajustar la calidad reduce o aumenta el tamaño.
- Antes de enviar los bytes, se envían 4 bytes con `struct.pack('!I', len(datos))` que indican el tamaño del bloque. `!I` significa entero sin signo (4 bytes) en orden de bytes big-endian.
- Se llama a `conexion.sendall(tamaño + datos)` para enviar primero el tamaño y luego los datos del fotograma. `sendall` garantiza que se intente enviar todo el buffer.

6) Manejo de desconexiones y excepciones

- Si el receptor se desconecta inesperadamente (`BrokenPipeError`, `ConnectionResetError`) el emisor detecta la excepción y vuelve a esperar otra conexión.
- `try/finally` asegura que la conexión se cierre y que la cámara y el socket del emisor se liberen al detenerse.

## Explicación detallada de `receptor.py`

1) Imports y configuración

- `socket`, `struct`: manejo de conexión y lectura del tamaño del frame.
- `cv2`, `numpy`: decodificación de los bytes JPEG y visualización.
- `time`, `sys`: utilidades para reintentos y argumentos.

2) Función `recibir_todo(socket_conexion, cantidad)`

- `recv` puede devolver menos bytes de los solicitados. `recibir_todo` llama repetidamente hasta leer exactamente `cantidad` bytes o detectar que la conexión se cerró.

3) Conexión al emisor

- El receptor intenta conectar con `socket_receptor.connect((ip_emisor, puerto_emisor))`. Si no hay emisor, atrapa `ConnectionRefusedError` y vuelve a intentar después de un retardo.

4) Recepción y reconstrucción de fotogramas

- Lee primero 4 bytes: `datos_tamaño = recibir_todo(socket_conexion, 4)` y desempaqueta con `struct.unpack('!I', datos_tamaño)[0]` para obtener el tamaño del fotograma.
- Luego llama a `recibir_todo(socket_conexion, tamaño_fotograma)` para recibir exactamente los bytes JPEG.
- Convierte los bytes a un arreglo NumPy (`np.frombuffer`) y decodifica con `cv2.imdecode` para obtener la imagen en BGR lista para mostrar.

5) Visualización y control

- Los fotogramas se muestran con `cv2.imshow` y `cv2.waitKey(1)` para refrescar la ventana.
- Presionar `q` cierra el receptor limpiamente.

6) Reintentos y manejo de errores

- Si la conexión se pierde o los datos vienen incompletos, el receptor cierra el socket y entra en un bucle que intenta reconectar cada `RETARDO_RECONEXION` segundos.

## Consejos prácticos y consideraciones de red

- Asegúrate de que el firewall permita conexiones entrantes en el puerto elegido en la máquina emisor.
- Usa la IP LAN (por ejemplo `192.168.0.9`) del emisor cuando ejecutes el receptor desde otra máquina en la misma red.
- Si necesitas mejor rendimiento o menor latencia, reduce la calidad JPEG (por ejemplo 60) o emplea compresión/streaming optimizado.
- Para entornos reales y producción considera protocolos diseñados para vídeo (RTSP, WebRTC) y cifrado.

## Notas finales

Estos scripts son un ejemplo didáctico y funcional para redes locales. Sirven como base para experimentar y extender con features como múltiples receptores, autenticación, cifrado TLS, o transmisión con menor latencia.

