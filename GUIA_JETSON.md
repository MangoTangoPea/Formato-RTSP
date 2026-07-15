# Guía de Instalación — Jetson Orin Nano para RTSP RealSense D435

Esta guía explica cómo preparar una NVIDIA Jetson Orin Nano Developer Kit
para ejecutar el sistema de transmisión RTSP con la cámara Intel RealSense D435
conectada por USB 2.0.

---

## 1. Requisitos previos

- **Jetson Orin Nano Developer Kit** con JetPack 5.x o 6.x instalado
- **Intel RealSense D435** conectada por cable USB
- Conexión a internet (para descargar dependencias)
- Terminal abierta en la Jetson

Verificar que la arquitectura es ARM64:
```bash
uname -m
# Debe decir: aarch64
```

---

## 2. Instalar dependencias del sistema

```bash
sudo apt update
sudo apt install -y \
    python3-pip \
    python3-dev \
    python3-numpy \
    python3-opencv \
    ffmpeg \
    git \
    cmake \
    build-essential \
    libssl-dev \
    libusb-1.0-0-dev \
    pkg-config \
    libgtk-3-dev \
    libglfw3-dev \
    libgl1-mesa-dev \
    libglu1-mesa-dev
```

---

## 3. Compilar librealsense desde fuentes (ARM64)

**¿Por qué compilar?** El paquete `pip install pyrealsense2` solo funciona
en x86_64. En la Jetson (ARM64) hay que compilar la librería desde el código
fuente para que funcione con la cámara.

### 3.1 Descargar el código fuente

```bash
cd ~
git clone https://github.com/IntelRealSense/librealsense.git
cd librealsense
```

### 3.2 Instalar las reglas udev (ANTES de compilar)

```bash
sudo cp config/99-realsense-libusb.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger
```

> Esto permite que la cámara se use sin ser root.

### 3.3 Compilar con CMake

```bash
mkdir build && cd build

cmake ../ \
    -DFORCE_LIBUVC=true \
    -DBUILD_PYTHON_BINDINGS=ON \
    -DPYTHON_EXECUTABLE=$(which python3) \
    -DCMAKE_BUILD_TYPE=Release \
    -DBUILD_EXAMPLES=false \
    -DBUILD_GRAPHICAL_EXAMPLES=false

make -j$(nproc)
```

> **Nota:** `-DFORCE_LIBUVC=true` es necesario porque el kernel de JetPack
> no siempre incluye los parches de Intel para el driver UVC nativo.
>
> La compilación toma entre 15 y 40 minutos dependiendo del modo de
> energía de la Jetson.

### 3.4 Instalar en el sistema

```bash
sudo make install
```

### 3.5 Hacer que Python encuentre pyrealsense2

Copiar la librería compilada al lugar donde Python la busca:

```bash
# Buscar dónde quedó el .so compilado
find ~/librealsense/build -name "pyrealsense2*.so" -type f

# Copiar al site-packages de Python
# (ajustar la versión de Python si no es 3.10)
sudo cp ~/librealsense/build/wrappers/python/pyrealsense2*.so \
    /usr/lib/python3/dist-packages/

# Si usas un entorno virtual, copiar ahí en vez de al sistema:
# cp ~/librealsense/build/wrappers/python/pyrealsense2*.so \
#    /ruta/a/tu/venv/lib/python3.*/site-packages/
```

### 3.6 Verificar la instalación

```bash
python3 -c "import pyrealsense2 as rs; print('OK:', rs)"
```

Si dice `OK:` seguido de la referencia al módulo, la instalación fue exitosa.

---

## 4. Desactivar el autosuspend USB (CRÍTICO)

El kernel de Linux puede "dormir" los puertos USB para ahorrar energía.
Si esto pasa con la RealSense, la cámara pierde alimentación y el programa
se cae con un timeout.

### 4.1 Desactivar temporalmente (se pierde al reiniciar)

```bash
echo -1 | sudo tee /sys/module/usbcore/parameters/autosuspend_delay_ms
```

### 4.2 Desactivar permanentemente (sobrevive reinicios)

Editar el archivo de configuración de GRUB:

```bash
sudo nano /etc/default/grub
```

Buscar la línea que empieza con `GRUB_CMDLINE_LINUX_DEFAULT` y agregar
`usbcore.autosuspend=-1` al final (dentro de las comillas):

```
GRUB_CMDLINE_LINUX_DEFAULT="quiet splash usbcore.autosuspend=-1"
```

Guardar y aplicar los cambios:

```bash
sudo update-grub
sudo reboot
```

### 4.3 Verificar después del reinicio

```bash
cat /sys/module/usbcore/parameters/autosuspend_delay_ms
# Debe decir: -1
```

---

## 5. Verificar que la cámara es reconocida

### 5.1 Con lsusb

```bash
lsusb | grep -i intel
```

Debe aparecer algo como:
```
Bus 001 Device 003: ID 8086:0b3a Intel Corp. Intel(R) RealSense(TM) Depth Camera 435
```

### 5.2 Con el diagnóstico del emisor

```bash
python3 emisor_jetson.py --diagnostico
```

Esto verifica todo de una vez: FFmpeg, OpenCV, numpy, pyrealsense2,
reglas udev, autosuspend USB, puerto RTSP, y MediaMTX.

### 5.3 Listar cámaras

```bash
python3 emisor_jetson.py --listar-camaras
```

Muestra la cámara con su número de serie y tipo de USB (2.x o 3.x).

---

## 6. Ejecutar el sistema

### En la Jetson (emisor):

```bash
python3 emisor_jetson.py
```

Opciones útiles:
```bash
python3 emisor_jetson.py --puerto 9554       # Puerto diferente
python3 emisor_jetson.py --calidad 2000      # Más bitrate
python3 emisor_jetson.py --cam 1             # Segunda cámara
python3 emisor_jetson.py --grabar-rango 100 500  # Grabar frames sin pérdidas
```

### En otra máquina (receptor):

```bash
python3 receptor_jetson.py <IP_DE_LA_JETSON>
```

Opciones útiles:
```bash
python3 receptor_jetson.py --grabar 192.168.1.42      # Graba mosaico en MKV
python3 receptor_jetson.py --sin-hud 192.168.1.42     # Sin overlay
```

También puedes ver cualquier canal individualmente con VLC:
```bash
vlc rtsp://<IP_DE_LA_JETSON>:8554/color
vlc rtsp://<IP_DE_LA_JETSON>:8554/depth
```

---

## 7. Solución de problemas frecuentes

### "RuntimeError" o timeout al iniciar la cámara

1. Desconectar y reconectar el cable USB
2. Verificar autosuspend: `cat /sys/module/usbcore/parameters/autosuspend_delay_ms`
3. Si dice un número positivo, desactivar con el paso 4
4. Probar un cable USB más corto (máximo 1 metro para USB 2.0)

### "Permission denied" al abrir la cámara

```bash
# Instalar las reglas udev
sudo cp ~/librealsense/config/99-realsense-libusb.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger
# Desconectar y reconectar la cámara
```

### "BrokenPipeError" en FFmpeg

Esto pasa cuando FFmpeg no puede procesar los frames a tiempo.
El emisor Jetson ya está configurado a 15 FPS para evitar esto.
Si sigue pasando:
- Reducir la calidad: `--calidad 800`
- Verificar que no haya otros procesos pesados corriendo

### "ModuleNotFoundError: No module named 'pyrealsense2'"

La librería no se compiló o no se copió al lugar correcto.
Repetir el paso 3.5 y verificar con el paso 3.6.

---

## Resumen de la configuración

| Parámetro | Valor | Razón |
|---|---|---|
| Resolución | 1280×720 | Mínimo 1024 px para LSB, máximo viable en USB 2.0 |
| FPS | 15 | Reduce tráfico USB a la mitad vs 30 FPS |
| Codificación | libx264 ultrafast | Mínima carga de CPU ARM64 |
| Mosaico receptor | 2560×1440 | 4 cuadrantes de 1280×720, simétrico |
| LSB | 1024 px en fila 0 | 128 bits × 8 px/bit, votación por mayoría |
| Autosuspend USB | -1 (desactivado) | Evita que el kernel duerma la cámara |
