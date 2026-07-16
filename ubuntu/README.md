# Emisor/Receptor RTSP — Intel RealSense D435 · Ubuntu

Sistema de transmisión RTSP multicanal para la cámara Intel RealSense D435
con esteganografía LSB para sincronía de frames.

## Arquitectura

```
Emisor (Ubuntu + RealSense D435)          Receptor (Ubuntu/Windows)
┌─────────────────────────────┐           ┌──────────────────────────┐
│  RealSenseCamera            │           │  4 × LectorRTSP (hilos) │
│  ├─ Color   1920×1080       │  RTSP/TCP │  ├─ Color               │
│  ├─ Depth   1280×720   ────────────────►│  ├─ Depth               │
│  ├─ IR1     1280×720        │  4 puertos│  ├─ IR1                 │
│  └─ IR2     1280×720        │           │  └─ IR2                 │
│                             │           │                          │
│  LSB: Frame ID + Timestamp  │           │  Extracción LSB          │
│  → 128 bits en fila 0       │           │  → Verificación sincronía│
│                             │           │                          │
│  4 × FFmpeg RTSP Server     │           │  Mosaico + HUD + REC    │
└─────────────────────────────┘           └──────────────────────────┘
```

## Estructura de Archivos

```
ubuntu/
├── README.md                  ← Este archivo
├── requirements.txt           ← Dependencias pip
├── verificar_sistema.py       ← Diagnóstico (ejecutar 1 sola vez)
├── realsense_camera.py        ← Clase RealSenseCamera (módulo)
├── lsb_steganography.py       ← Esteganografía LSB (módulo)
├── rtsp_server.py             ← Servidor RTSP FFmpeg (módulo)
├── utils.py                   ← Utilidades compartidas (módulo)
├── emisor.py                  ← Script principal del EMISOR
├── receptor.py                ← Script principal del RECEPTOR
└── grabaciones/               ← Grabaciones MKV (automático)
    └── grabacion_20260715_211500.mkv
```

## Instalación

### 1. Paquetes del sistema

```bash
sudo apt update
sudo apt install ffmpeg python3-venv python3-pip libgl1 libglib2.0-0
```

### 2. Dependencias Python

```bash
cd ubuntu/
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Reglas udev para RealSense (permisos USB)

```bash
wget https://raw.githubusercontent.com/IntelRealSense/librealsense/master/config/99-realsense-libusb.rules
sudo cp 99-realsense-libusb.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

### 4. Verificar instalación (una sola vez)

```bash
python3 verificar_sistema.py
```

Si todo sale con ✓, estás listo.

## Uso

### Emisor (máquina con la cámara RealSense)

```bash
# Uso básico
python3 emisor.py

# Segunda cámara
python3 emisor.py --cam 1

# Puerto alternativo
python3 emisor.py --puerto 9554

# Mayor calidad
python3 emisor.py --calidad 4000

# Ver cámaras conectadas
python3 emisor.py --listar-camaras

# Grabar rango de frames sin pérdidas
python3 emisor.py --grabar-rango 150 450
```

### Receptor (cualquier máquina en la red)

```bash
# Conectar al emisor
python3 receptor.py 192.168.1.42

# Puerto personalizado
python3 receptor.py 192.168.1.42 9554

# Sin HUD
python3 receptor.py --sin-hud 192.168.1.42

# Prueba local (loopback)
python3 receptor.py 127.0.0.1
```

### Controles del Receptor

| Tecla   | Acción                           |
|---------|----------------------------------|
| `M`     | Vista mosaico (4 canales)        |
| `1`     | Solo Color (RGB 1920×1080)       |
| `2`     | Solo Infrarrojo 1 (Left)         |
| `3`     | Solo Profundidad (Depth)         |
| `4`     | Solo Infrarrojo 2 (Right)        |
| `H`     | Mostrar/ocultar HUD              |
| `F`     | Pantalla completa on/off         |
| **`R`** | **Iniciar/detener grabación**    |
| `Q`/ESC | Salir                            |

### Grabación

- **Presiona `R`** → Aparece 🔴 **REC** parpadeante, empieza a grabar
- **Presiona `R` otra vez** → Desaparece el indicador, se detiene la grabación
- Los archivos se guardan automáticamente en `grabaciones/` con nombre:
  `grabacion_YYYYMMDD_HHMMSS.mkv`

## Puertos RTSP

El emisor usa 4 puertos consecutivos:

| Canal   | Puerto         | Resolución |
|---------|---------------|------------|
| Color   | base (8554)   | 1920×1080  |
| Depth   | base+1 (8555) | 1280×720   |
| IR1     | base+2 (8556) | 1280×720   |
| IR2     | base+3 (8557) | 1280×720   |

## Solución de Problemas

### La cámara no se conecta

El módulo `realsense_camera.py` incluye un **hardware reset** automático
que resuelve la mayoría de problemas de conexión. Si aún falla:

1. Verifica que esté en un puerto **USB 3.0** (azul)
2. Cierra cualquier otro programa que use la cámara
3. Ejecuta `python3 verificar_sistema.py`
4. Verifica las reglas udev (sección de instalación)

### FFmpeg no encontrado

```bash
sudo apt install ffmpeg
```

### Error de permisos USB

```bash
# Instalar reglas udev
wget https://raw.githubusercontent.com/IntelRealSense/librealsense/master/config/99-realsense-libusb.rules
sudo cp 99-realsense-libusb.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger

# Reconectar la cámara después de esto
```

### Puerto en uso

```bash
# Ver qué proceso usa el puerto
sudo lsof -i :8554

# O usar otro puerto
python3 emisor.py --puerto 9554
```

---

**GIGSEEA — UTP · 2025**
