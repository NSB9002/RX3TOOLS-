# rx3tool V3: modelos y texturas RX3, probado en Termux 

Herramienta creada para facilitar la edición de archivos `.RX3` de FIFA. Script de Python (solo libreria estandar) para FIFA `.rx3`. No necesita Blender.


Normalmente los archivos `.RX3` se usan en FIFA 14, FIFA 15, FIFA 16 y otros FIFA que usan este formato, para guardar modelos 3D y texturas.

## ¿Para qué se creó esta herramienta?

Se creó para facilitar la edición de archivos `.RX3`, como modelos y texturas, sin necesidad de tener un ordenador.

El fin es que aquellas personas que no tienen computadora puedan hacerlo desde su teléfono, usando Termux o otra app.

Pero también se puede usar en un ordenador, sea Windows, Linux o Mac, que tenga Python. No solo funciona en Termux, funciona en cualquier sitio donde se pueda ejecutar comandos de Python.

## ¿Qué se puede hacer?

### 1. Modelos 3D
Se puede exportar un archivo `.RX3` a `.OBJ`.

Dentro de un `.RX3` pueden venir modelos 3D como:
- Caras
- Guantes
- Pelos
- Y otros modelos del juego

Una vez exportado a `.OBJ`, se puede editar.

Para editar el modelo `.OBJ` en el móvil se pueden usar herramientas como:
- Prisma 3D
- Nomad Sculpt
- Y otras aplicaciones de modelado 3D para móvil

Después de editarlo, se vuelve a importar a `.RX3`.

### 2. Texturas
Las texturas se exportan en `.PNG` y `.DDS` y se pueden editar.

Para editar las texturas en el móvil se pueden usar aplicaciones como:
- IbisPaint
- PixelLab / PicsArt
- Y otras aplicaciones de edición de imágenes

Después de editarlas, se vuelven a importar a `.RX3`.

## ¿Dónde funciona?

- En Termux o cualquier app con Python , desde el teléfono
- En Windows, con Python
- En Linux, con Python
- En Mac, con Python
- En cualquier sitio donde se pueda ejecutar Python, como Replit o Google Colab

## Que se arreglo respecto a V2 (probado con `head.rx3` y `face.rx3` reales)

| Problema | Causa | Arreglo |
|---|---|---|
| Textura: "formato id 3 no soportado" | `face.rx3` es RGBA de 32 bits sin comprimir (id 3) y V2 solo conocia DXT1/3/5 y ATI2 | id 3 soportado: exporta DDS+PNG e importa PNG/DDS. Un id desconocido ya no se salta en silencio: se intenta deducir por tamano o se explica |
| Modelo importa pero no se ve en el juego | 1) El RX3 guarda en la seccion `objetos` el tamano y numero de indices de cada malla y V2 no la actualizaba. 2) V2 escribia el tamano de indice (2/4 bytes) en el byte 11 de la cabecera y el archivo lo tiene en el byte 8 | Se actualiza `objetos` y se conserva la cabecera original |
| Cualquier edicion cambiaba la estructura | V2 siempre reconstruia los buffers (3158 -> 3045 vertices) | **Modo "en sitio"**: si el OBJ tiene la misma topologia, solo se tocan posiciones y UV y el archivo queda con la misma estructura |

Prueba de que V2 dejaba el archivo roto: `python rx3tool.py check` sobre la salida de V2 dice
`'objetos' dice 36064 bytes/18021 indices pero el buffer tiene 34608/17289`.

Comprobado en este equipo:
- Exportar y volver a importar sin cambios (modelo, PNG y DDS) da un archivo **identico byte a byte** al original.
- Mover vertices cambia solo bytes dentro de los buffers de vertices.
- Reconstruccion con caras quitadas/anadidas, e indices de 4 bytes (mas de 65535 vertices): todo verificado.
- Importar PNG funciona con y sin Pillow.

No he podido probar en el juego (no tengo el juego), asi que sigue valiendo: **haz copia del RX3 original**.

## Instalacion en Termux

```
pkg update
pkg install python
termux-setup-storage
cp ~/storage/downloads/rx3tool.py -d ~/
python rx3tool.py menu
```

## Comandos

```
python rx3tool.py info head.rx3
python rx3tool.py check head.rx3                      # comprueba que el RX3 es coherente

python rx3tool.py export-model head.rx3               # -> head_out/head.obj
python rx3tool.py export-model head.rx3 --split       # un OBJ por malla
python rx3tool.py import-model head.rx3 head_out/head.obj    # -> head_mod.rx3

python rx3tool.py export-tex face.rx3                 # -> face_out/face_tex000.dds y .png
python rx3tool.py import-tex face.rx3 0 nueva.png     # textura 0 -> face_mod.rx3
python rx3tool.py import-tex face.rx3 0 nueva.dds
```

El original nunca se modifica: se crea `<nombre>_mod.rx3` (o el que indiques con `-o`).
Para aplicar varias cosas seguidas usa `-o`:

```
python rx3tool.py import-model head.rx3 editado.obj -o head_nuevo.rx3
```

Cada importacion termina con una linea `Verificacion: OK`. Si dice PROBLEMAS, no metas ese archivo al juego y copia el texto.

## Editar el modelo sin que falle en el juego

1. Exporta con los valores por defecto (escala 0.01, vertices soldados).
2. **Mueve vertices** (no anadas ni quites caras) y guarda como OBJ con UV.
   Asi la importacion sale `EN SITIO` y el archivo conserva exactamente la misma estructura,
   normales, tangentes y huesos. Es lo mas seguro para el juego.
3. Si cambias la topologia (anades/quitas caras o el programa reordena vertices) el script avisa
   y **reconstruye** los buffers. Ahora lo hace correctamente, pero los vertices nuevos copian
   normales, tangentes y huesos del vertice original mas cercano.
4. Usa la misma `--scale` al exportar e importar. Si no coincide, el script lo detecta y se detiene
   (`--force` para saltarlo).
5. Si tu editor renombra el objeto, indica la malla: `import-model head.rx3 editado.obj --mesh 0`.
6. `--rebuild` fuerza la reconstruccion aunque la topologia coincida (solo para pruebas).

## Texturas

- Soporta DXT1, DXT3, DXT5, ATI2 (solo DDS) y **RGBA de 32 bits (id 3)**.
- Si importas PNG se mantiene el formato original de esa textura (`--format dxt1|dxt3|dxt5|rgba` para forzar otro).
- Si el original tiene mipmaps y la imagen tiene el mismo tamano, se generan los mismos mipmaps.
- Usa el mismo tamano y formato que el original si el juego es estricto: el script avisa cuando cambian.
- DDS de 32 bits sin comprimir (A8R8G8B8, tambien 24 bits) se aceptan.
- Orden de bytes de las texturas de 32 bits: `rgba` (comprobado en `face.rx3`). Si en otro archivo los colores salen
  intercambiados, usa `--order bgra` (o `argb`, `abgr`) al exportar e importar.

## Limitaciones

- Normales, tangentes y pesos de huesos no se recalculan.
- Mallas con indices en tira de triangulos (trofeos) no estan soportadas.
- Las bounding boxes solo se amplian y solo si la estructura se reconoce (`head.rx3` no tiene).
- La salida va sin comprimir. Si el juego necesita el archivo comprimido o dentro de un `.big`/`.ebo`, hay que
  repaquetarlo.
- Si la importacion de modelo sigue sin verse en el juego con la verificacion en OK, envia `info` del RX3 original y
  del modificado: lo siguiente a revisar seria algo propio de esa version del juego.
