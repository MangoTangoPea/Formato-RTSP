#!/bin/bash

echo "========================================================"
echo " INICIANDO COMPILACION DE LIBREALSENSE PARA WSL (LIBUVC)"
echo "========================================================"
echo ""
echo "[1/7] Desinstalando versión de pip..."
cd /mnt/c/Users/Lenovo/Documents/GitHub/Formato-RTSP
source .venvv/bin/activate
pip uninstall -y pyrealsense2

echo ""
echo "[2/7] Instalando dependencias de Ubuntu..."
sudo apt-get update
sudo apt-get install -y git cmake libssl-dev freeglut3-dev libusb-1.0-0-dev pkg-config libgtk-3-dev build-essential python3-dev python3-setuptools

echo ""
echo "[3/7] Descargando código fuente..."
cd ~
rm -rf librealsense
git clone https://github.com/IntelRealSense/librealsense.git
cd librealsense

echo ""
echo "[4/7] Configurando compilación con CMake..."
mkdir -p build && cd build
cmake ../ -DFORCE_LIBUVC=true -DBUILD_PYTHON_BINDINGS=bool:true -DPYTHON_EXECUTABLE=/mnt/c/Users/Lenovo/Documents/GitHub/Formato-RTSP/.venvv/bin/python3 -DCMAKE_BUILD_TYPE=release -DBUILD_EXAMPLES=false -DBUILD_GRAPHICAL_EXAMPLES=false

echo ""
echo "[5/7] Compilando (Esto tomará entre 15 y 30 minutos)..."
make -j$(nproc)

echo ""
echo "[6/7] Instalando en el sistema..."
sudo make install

echo ""
echo "[7/7] Copiando librerías al entorno virtual..."
cp wrappers/python/pyrealsense2*.so /mnt/c/Users/Lenovo/Documents/GitHub/Formato-RTSP/.venvv/lib/python3.10/site-packages/
cp wrappers/python/pybackend2*.so /mnt/c/Users/Lenovo/Documents/GitHub/Formato-RTSP/.venvv/lib/python3.10/site-packages/

echo ""
echo "========================================================"
echo " COMPILACION FINALIZADA CON EXITO"
echo "========================================================"
echo "Ya puedes ejecutar: sudo /mnt/c/Users/Lenovo/Documents/GitHub/Formato-RTSP/.venvv/bin/python3 emisor_ubuntu.py"
