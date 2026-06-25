# Demo RTSP — Transmisión de Mosaico RealSense D435

Sistema de transmisión de vídeo en tiempo real en red local utilizando el protocolo **RTSP**. El emisor captura streams simultáneos (RGB, Infrarrojos y Profundidad) de una cámara **Intel RealSense D435**, compone un mosaico de alta resolución (1920x1440) con OSD y lo transmite de forma fluida.

---

## 🚀 Guías de Instalación y Ejecución

Dependiendo de tu sistema operativo, elige la guía correspondiente:

### Opción A: Ejecutar en Windows Nativo (Recomendado)
Windows tiene acceso nativo a la cámara por USB, por lo que es la opción más rápida y estable.

1. **Clonar e instalar dependencias:**
   Abre PowerShell y ejecuta:
   ```powershell
   cd "C:\ruta\al\proyecto"
   python -m venv venv
   .\venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```
2. **Ejecutar el Emisor:**
   ```powershell
   python emisor.py
   ```
   *(Anota la URL RTSP y la IP que aparece en la consola).*
3. **Ejecutar el Receptor:**
   Abre otro PowerShell (o en otra computadora de tu red) y ejecuta:
   ```powershell
   python receptor.py 127.0.0.1  # Usa la IP real si es en otra computadora
   ```

---

### Opción B: Ejecutar en Ubuntu Nativo (Raspberry Pi, Jetson, PC)
En un sistema Linux nativo, el kernel ya incluye los controladores de video necesarios.

1. **Clonar e instalar dependencias:**
   ```bash
   sudo apt update && sudo apt install python3-venv python3-pip libgl1 libglib2.0-0
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
2. **Dar permisos USB a la cámara (Solo la primera vez):**
   ```bash
   wget https://raw.githubusercontent.com/IntelRealSense/librealsense/master/config/99-realsense-libusb.rules
   sudo cp 99-realsense-libusb.rules /etc/udev/rules.d/
   sudo udevadm control --reload-rules && sudo udevadm trigger
   ```
3. **Ejecutar el Emisor:**
   Abre una terminal y ejecuta:
   ```bash
   cd /ruta/al/proyecto
   source .venv/bin/activate
   python3 emisor_ubuntu.py
   ```
4. **Ejecutar el Receptor:**
   En otra terminal o en otra computadora, ejecuta:
   ```bash
   cd /ruta/al/proyecto
   source .venv/bin/activate
   python3 receptor_ubuntu.py 127.0.0.1  # Usa la IP real si es en otra computadora
   ```

---

### Opción C: Ejecutar en WSL2 (Desarrollo avanzado)
WSL2 no tiene drivers de video USB. Requiere puentear el USB desde Windows y compilar una librería especial. Sigue estos 6 pasos exactos:

**Paso 1: Vincular la cámara a WSL**
En un **PowerShell de Windows como Administrador**, instala la herramienta y vincula la cámara:
```powershell
winget install --interactive --exact dorssel.usbipd-win
usbipd list
usbipd bind --busid <TU_BUSID>    # (ej: 2-1)
usbipd attach --wsl --busid <TU_BUSID>
```

**Paso 2: Compilar librealsense en Ubuntu (WSL)**
Abre tu terminal de Ubuntu y ejecuta el script de instalación (tomará 15-30 minutos):
```bash
cd /mnt/c/Users/Lenovo/Documents/GitHub/Formato-RTSP
bash install_realsense.sh
```

**Paso 3: Ejecutar el Emisor (con superpermisos)**
No cierres esta terminal mientras uses la cámara:
```bash
sudo /mnt/c/Users/Lenovo/Documents/GitHub/Formato-RTSP/.venvv/bin/python3 emisor_ubuntu.py
```

**Paso 4: Ejecutar el Receptor (en paralelo)**
Abre una **NUEVA** pestaña de Ubuntu (WSL) y ejecuta:
```bash
cd /mnt/c/Users/Lenovo/Documents/GitHub/Formato-RTSP
source .venvv/bin/activate
python3 receptor_ubuntu.py 127.0.0.1
```
*(Si WSL no abre la ventana gráfica, corre `python receptor.py 127.0.0.1` desde un PowerShell de Windows).*

**Paso 5: Limpiar almacenamiento (Opcional)**
La compilación dejó 2GB de basura. Bórrala en Ubuntu con:
```bash
rm -rf ~/librealsense
```

**Paso 6: Devolver la cámara a Windows**
En PowerShell como Administrador:
```powershell
usbipd detach --busid <TU_BUSID>
usbipd unbind --busid <TU_BUSID>
# Para borrar el programa si ya no lo usas: winget uninstall dorssel.usbipd-win
```

---

## 📡 Ejecutar el receptor en OTRA computadora
El protocolo RTSP está diseñado para redes. Para ver la cámara desde otra computadora física conectada al mismo WiFi o cable:

1. Descubre la IP de la computadora que tiene la cámara conectada (En Windows abre PowerShell y escribe `ipconfig` -> busca Dirección IPv4, ej: `192.168.1.42`).
2. Ve a la **segunda computadora** (la que va a recibir el video), abre la terminal, activa el entorno y ejecuta el script apuntando a esa IP.

**Si el receptor es Windows:**
```powershell
cd "C:\ruta\al\proyecto"
.\venv\Scripts\Activate.ps1
python receptor.py 192.168.1.42
```

**Si el receptor es Ubuntu/Linux:**
```bash
cd /ruta/al/proyecto
source .venv/bin/activate
python3 receptor_ubuntu.py 192.168.1.42
```

*(Nota: Si usas el emisor dentro de WSL, Windows bloqueará la conexión a otras computadoras físicas. Para transmitir a otra PC, es altamente recomendado usar el emisor en Windows Nativo u Ubuntu Nativo).*

---

## 🛠️ Resolución de Problemas

- **"No se detectaron cámaras"**: Asegúrate de que no haya otro programa usando la cámara (Zoom, OBS). En WSL, verifica que hayas hecho el `usbipd attach`.
- **"MediaMTX terminó inesperadamente"**: El puerto `8554` ya está en uso. Usa `python emisor.py --puerto 9554` y conéctate con `python receptor.py <IP> 9554`.
- **"Video lento o entrecortado"**: Reduce la calidad con `python emisor.py --calidad 1000`. Usa WiFi 5GHz o cable Ethernet.

---

## ⚙️ Explicación Técnica Detallada

1. **Multiplexado con MediaMTX**: El emisor envía el video **una sola vez** al servidor local. MediaMTX se encarga de retransmitirlo a todos los receptores (clientes) que se conecten, evitando saturar el hardware de la cámara.
2. **Procesamiento de Imágenes**: 
   - La profundidad se convierte a un mapa de calor usando `cv2.COLORMAP_JET`.
   - Las resoluciones nativas (RGB 1080p y Depth/IR 720p) se redimensionan y concatenan matemáticamente usando `numpy` en un lienzo de `1920x1440`.
3. **Compresión H.264 (FFmpeg)**: El lienzo en crudo pasa por una tubería (pipe) hacia FFmpeg, configurado con el preset `ultrafast` y `zerolatency` para empujar los paquetes vía RTP/TCP.

### Estructura del proyecto
```text
Demo RTSP/
├── emisor.py              # Emisor para Windows nativo
├── emisor_ubuntu.py       # Emisor para Linux/WSL
├── receptor.py            # Receptor para Windows nativo
├── receptor_ubuntu.py     # Receptor para Linux/WSL
├── install_realsense.sh   # Script de compilación USB para WSL
├── requirements.txt       # Dependencias de Python (opencv-python, numpy, etc.)
└── .venv/                 # Entorno virtual de Python
```
