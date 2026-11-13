#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rx3tool V3 - Extrae e importa modelos y texturas de archivos FIFA .rx3
(sin Blender, funciona en Termux con Python 3).

Basado en el formato que usa el addon "FIFA 15 3D IE v0.66.2" (arti-10).

  info          muestra secciones, mallas y texturas
  check         comprueba que el RX3 es coherente (tamanos, indices, texturas)
  export-model  RX3 -> OBJ
  import-model  OBJ -> RX3 (en sitio si la topologia no cambia; si no, reconstruye)
  export-tex    RX3 -> DDS + PNG
  import-tex    DDS/PNG -> RX3 (reemplaza una textura)
  menu          menu interactivo (comodo en el telefono)

Solo usa la libreria estandar. Pillow es opcional (pkg install python-pillow):
acelera el redimensionado; sin el, los PNG se leen con un lector propio.

Cambios de V3 respecto a V2 (comprobados con head.rx3 / face.rx3 reales):
  * Texturas id 3 (RGBA de 32 bits sin comprimir) soportadas: exportar e importar.
  * Al importar un modelo se actualiza la sección 'objetos' (tamano y numero de
    indices por malla) y la cabecera de indices se escribe como el original.
    Antes el juego recibia un modelo "vacio" y no se veia.
  * Importacion "en sitio": si el OBJ tiene la misma topologia que la exportacion,
    solo se tocan posiciones/UV y el archivo conserva EXACTAMENTE la misma
    estructura (lo mas seguro para el juego).
  * Se verifica el RX3 resultante antes de darlo por bueno.
"""
import argparse
import glob
import math
import os
import re
import struct
import sys
import zlib

__version__ = '3.0'

# ---------------------------------------------------------------- constantes
H_DESCR = 3263271920     # descripcion del layout de vertices
H_VERT = 5798561         # buffer de vertices
H_IDX = 5798132          # buffer de indices
H_OBJ = 582139446        # 'objetos': tamano/numero de indices por malla
H_TEX = 2047566042       # una textura
H_TEXTAB = 1808827868    # tabla de texturas (w/h/mips/formato)
H_GROUP = 2116321516     # grupos (contienen bounding boxes)
H_NAMES = {
    H_DESCR: 'descr_malla', H_VERT: 'vertices', H_IDX: 'indices',
    H_OBJ: 'objetos', H_TEX: 'textura', H_TEXTAB: 'tabla_texturas', H_GROUP: 'grupo',
    123459928: 'material', 4034198449: 'colision', 3751472158: 'huesos',
    1285267122: 'props/nombres', 685399266: 'prop_pos',
    230948820: 'nombre_grupo',
}
# id de formato de textura en el RX3 -> (nombre, bytes por bloque 4x4; 0 = sin comprimir 32 bits)
ID_RAW32 = 3
ID_L8 = 100              # id interno: 8 bits (solo se exporta, formato adivinado por tamano)
TEX_FMT = {0: ('DXT1', 8), 1: ('DXT3', 16), 2: ('DXT5', 16), ID_RAW32: ('RGBA8', 0),
           7: ('ATI2', 16)}
FOURCC_TO_ID = {'DXT1': 0, 'DXT3': 1, 'DXT5': 2, 'ATI2': 7, 'BC5U': 7}
FORMAT_CHOICES = {'dxt1': 0, 'dxt3': 1, 'dxt5': 2, 'rgba': ID_RAW32}
# orden de bytes de los pixeles de las texturas sin comprimir (comprobado en face.rx3)
RAW_ORDER = 'rgba'


class RX3Error(Exception):
    pass


def r16(n):
    return (n + 15) // 16 * 16


def warn(msg):
    print('  [aviso] ' + msg)


# ------------------------------------------------------------- contenedor RX3
def descomprimir_chunkzip(raw):
    """Replica la logica del addon para archivos 'chunkzip'."""
    pos = 20
    sec_num = struct.unpack_from('>I', raw, pos)[0]
    pos += 12
    out = bytearray()
    for _ in range(sec_num):
        if pos % 4:
            pos += 4 - pos % 4
        sec_len = 0
        while sec_len == 0:
            if pos + 4 > len(raw):
                raise RX3Error('chunkzip corrupto (fin inesperado)')
            sec_len = struct.unpack_from('>I', raw, pos)[0]
            pos += 4
        pos += 4
        chunk = raw[pos:pos + sec_len]
        pos += sec_len
        try:
            out += zlib.decompress(chunk, -15)
        except zlib.error:
            raise RX3Error('chunkzip corrupto (zlib)')
    return bytes(out)


class Section:
    def __init__(self, h, off, size, extra):
        self.hash, self.orig_off, self.size, self.extra = h, off, size, extra
        self.data = b''
        self.gap = b''
        self.offset = off

    @property
    def name(self):
        return H_NAMES.get(self.hash, 'desconocida_%d' % self.hash)


class RX3:
    def __init__(self, raw):
        self.compressed = raw[:8] == b'chunkzip'
        if self.compressed:
            raw = descomprimir_chunkzip(raw)
        if raw[:3].lower() != b'rx3':
            raise RX3Error('No parece un RX3 (cabecera: %r)' % raw[:8])
        self.E = {b'l': '<', b'b': '>'}.get(raw[3:4])
        if self.E is None:
            raise RX3Error('Endianness desconocida: %r' % raw[3:4])
        E = self.E
        self.raw_len = len(raw)
        self.total_field = struct.unpack_from(E + 'I', raw, 8)[0]
        n = struct.unpack_from(E + 'I', raw, 12)[0]
        if n == 0 or n > 20000 or 16 + 16 * n > len(raw):
            raise RX3Error('Numero de secciones invalido: %d' % n)
        self.header16 = bytearray(raw[:16])
        self.secs = []
        for i in range(n):
            h, o, s, x = struct.unpack_from(E + '4I', raw, 16 + 16 * i)
            if o + s > len(raw):
                raise RX3Error('Seccion %d fuera del archivo' % i)
            self.secs.append(Section(h, o, s, x))
        order = sorted(range(n), key=lambda i: self.secs[i].orig_off)
        table_end = 16 + 16 * n
        first = self.secs[order[0]].orig_off
        if first < table_end:
            raise RX3Error('Primera seccion solapa con la tabla')
        self.pre = raw[table_end:first]
        self.tail = b''
        for k, idx in enumerate(order):
            s = self.secs[idx]
            end = s.orig_off + s.size
            nxt = self.secs[order[k + 1]].orig_off if k + 1 < n else None
            s.data = raw[s.orig_off:end]
            if nxt is None:
                self.tail = raw[end:]
            else:
                if nxt < end:
                    raise RX3Error('Secciones solapadas (no soportado)')
                s.gap = raw[end:nxt]
        self.order = order

    @classmethod
    def open(cls, path):
        with open(path, 'rb') as f:
            return cls(f.read())

    def build(self):
        E = self.E
        n = len(self.secs)
        out = bytearray(16 + 16 * n)
        out += self.pre
        for idx in self.order:
            s = self.secs[idx]
            s.offset = len(out)
            out += s.data
            out += s.gap
        out += self.tail
        hdr = bytearray(self.header16)
        new_total = self.total_field + (len(out) - self.raw_len)
        struct.pack_into(E + 'I', hdr, 8, new_total)
        out[0:16] = hdr
        for i, s in enumerate(self.secs):
            struct.pack_into(E + '4I', out, 16 + 16 * i, s.hash, s.offset,
                             len(s.data), s.extra)
        return bytes(out)

    def set_data(self, sec, data):
        pad = r16(len(data)) - len(data)
        sec.data = bytes(data) + b'\x00' * pad
        sec.size = len(sec.data)
        # el hueco original (relleno) se conserva; no afecta a los offsets

    def by_hash(self, h):
        return [s for s in self.secs if s.hash == h]


# ------------------------------------------------------------------- mallas
class Mesh:
    def __init__(self, rx, k, sd, sv, si):
        E = rx.E
        self.rx, self.k, self.sd, self.sv, self.si = rx, k, sd, sv, si
        # --- descripcion
        length = struct.unpack_from(E + 'i', sd.data, 4)[0]
        txt = sd.data[16:16 + max(length, 0)]
        toks, cur = [], bytearray()
        for b in txt + b'\x00':
            if b in (0, 32):
                if cur:
                    toks.append(cur.decode('ascii', 'replace'))
                    cur = bytearray()
            else:
                cur.append(b)
        self.descr_text = ' '.join(toks)
        self.elems = {}      # ('p',0) -> (offset, tipo)
        for t in toks:
            f = t.split(':')
            if len(f) < 5:
                warn('elemento de descripcion raro: %r' % t)
                continue
            m = re.match(r'([a-z])(\d*)$', f[0])
            if not m:
                continue
            try:
                off = int(f[1], 16)
            except ValueError:
                continue
            self.elems[(m.group(1), int(m.group(2) or 0))] = (off, f[4])
        # --- vertices (los datos de vertice van siempre en little-endian)
        _, self.vc, self.stride, self.vflag = struct.unpack_from(E + '4I', sv.data, 0)
        if 16 + self.vc * self.stride > len(sv.data):
            raise RX3Error('Malla %d: buffer de vertices incompleto' % k)
        self.vraw = bytearray(sv.data[16:16 + self.vc * self.stride])
        # --- indices
        self.old_vsize = len(sv.data)
        self.old_isize = len(si.data)
        self.icount = struct.unpack_from(E + 'I', si.data, 4)[0]
        # tamano de indice: byte 8 de la cabecera (asi esta en los RX3 reales)
        self.isz_off = 8 if si.data[8] else 11
        self.isz = si.data[self.isz_off]
        if self.isz not in (2, 4):
            raise RX3Error('Malla %d: tamano de indice raro (%d)' % (k, self.isz))
        if 16 + self.icount * self.isz > len(si.data):
            raise RX3Error('Malla %d: buffer de indices incompleto' % k)
        fmt = E + ('HHH' if self.isz == 2 else 'III')
        tris = []
        bad = 0
        pos = 16
        for _ in range(self.icount // 3):
            a, b, c = struct.unpack_from(fmt, si.data, pos)
            pos += 3 * self.isz
            if a == b or b == c or a == c:
                continue
            if max(a, b, c) >= self.vc:
                bad += 1
                continue
            tris.append((a, b, c))
        self.tris = tris
        self.bad_tris = bad
        if self.icount % 3:
            warn('malla %d: indices no multiplo de 3 (tira de triangulos?). '
                 'No soportado, se ignora el resto.' % k)

    @property
    def has_pos(self):
        return ('p', 0) in self.elems and self.elems[('p', 0)][1] in ('3f32', '4f16')

    def pos(self, i):
        off, t = self.elems[('p', 0)]
        o = i * self.stride + off
        if t == '3f32':
            return struct.unpack_from('<3f', self.vraw, o)
        return struct.unpack_from('<3e', self.vraw, o)

    def set_pos(self, i, p):
        off, t = self.elems[('p', 0)]
        o = i * self.stride + off
        if t == '3f32':
            struct.pack_into('<3f', self.vraw, o, *p)
        else:
            struct.pack_into('<3e', self.vraw, o, *[clamp_half(c) for c in p])

    def uv(self, i, n=0):
        e = self.elems.get(('t', n))
        if not e:
            return (0.0, 0.0)
        o = i * self.stride + e[0]
        if e[1] == '2f32':
            return struct.unpack_from('<2f', self.vraw, o)
        if e[1] == '2f16':
            return struct.unpack_from('<2e', self.vraw, o)
        return (0.0, 0.0)

    def set_uv(self, i, uv, n=0):
        e = self.elems.get(('t', n))
        if not e:
            return
        o = i * self.stride + e[0]
        if e[1] == '2f32':
            struct.pack_into('<2f', self.vraw, o, *uv)
        elif e[1] == '2f16':
            struct.pack_into('<2e', self.vraw, o, *[clamp_half(c) for c in uv])

    def aabb(self):
        ps = [self.pos(i) for i in range(self.vc)]
        return (tuple(min(p[a] for p in ps) for a in range(3)),
                tuple(max(p[a] for p in ps) for a in range(3)))

    def store_vraw(self):
        """Vuelca los vertices editados en la seccion (mismo tamano)."""
        d = bytearray(self.sv.data)
        d[16:16 + len(self.vraw)] = self.vraw
        self.sv.data = bytes(d)


def clamp_half(x):
    return max(-65504.0, min(65504.0, x))


def get_meshes(rx):
    de, ve, ie = rx.by_hash(H_DESCR), rx.by_hash(H_VERT), rx.by_hash(H_IDX)
    if not (len(de) == len(ve) == len(ie)):
        warn('descr/vertices/indices no coinciden (%d/%d/%d); uso el minimo'
             % (len(de), len(ve), len(ie)))
    return [Mesh(rx, k, de[k], ve[k], ie[k]) for k in range(min(len(de), len(ve), len(ie)))]


# ------------------------------------------------------- export modelo (OBJ)
def mesh_geometry(m, scale, weld):
    """Devuelve (verts, uvs, faces, corner, face_src).
    corner:   indice de vertice RX3 -> (indice v, indice vt) en el OBJ (locales)
    face_src: triangulo original (indices RX3) de cada cara exportada"""
    verts, vkey = [], {}
    uvs, ukey = [], {}
    corner = {}
    for tri in m.tris:
        for i in tri:
            if i in corner:
                continue
            p = m.pos(i)
            u, v = m.uv(i)
            key = (round(p[0], 3), round(p[1], 3), round(p[2], 3)) if weld else i
            if key not in vkey:
                vkey[key] = len(verts)
                verts.append((p[0] * scale, p[1] * scale, p[2] * scale))
            ukey_ = (round(u, 6), round(v, 6)) if weld else i
            if ukey_ not in ukey:
                ukey[ukey_] = len(uvs)
                uvs.append((u, 1.0 - v))
            corner[i] = (vkey[key], ukey[ukey_])
    faces, face_src = [], []
    for tri in m.tris:
        cs = [corner[i] for i in tri]
        if len({c[0] for c in cs}) < 3:
            continue            # triangulo degenerado tras soldar
        faces.append(cs)
        face_src.append(tri)
    return verts, uvs, faces, corner, face_src


def write_obj(path, geos):
    vo = to = 0
    with open(path, 'w') as f:
        f.write('# exportado con rx3tool.py\n')
        for name, verts, uvs, faces in geos:
            f.write('o %s\n' % name)
            for v in verts:
                f.write('v %.6f %.6f %.6f\n' % v)
            for t in uvs:
                f.write('vt %.6f %.6f\n' % t)
            for cs in faces:
                f.write('f ' + ' '.join('%d/%d' % (vo + c[0] + 1, to + c[1] + 1)
                                        for c in cs) + '\n')
            vo += len(verts)
            to += len(uvs)


def cmd_export_model(a):
    rx = RX3.open(a.file)
    meshes = get_meshes(rx)
    base = os.path.splitext(os.path.basename(a.file))[0]
    outdir = a.out or (base + '_out')
    os.makedirs(outdir, exist_ok=True)
    geos = []
    for m in meshes:
        if a.mesh is not None and m.k != a.mesh:
            continue
        if not m.has_pos:
            warn('malla %d: sin posiciones soportadas (descr: %s)' % (m.k, m.descr_text))
            continue
        v, t, f, _, _ = mesh_geometry(m, a.scale, not a.no_weld)
        geos.append(('mesh_%03d' % m.k, v, t, f))
        print('malla %d: %d vertices RX3 -> %d v / %d vt / %d caras'
              % (m.k, m.vc, len(v), len(t), len(f)))
    if not geos:
        raise RX3Error('No hay mallas para exportar')
    if a.split:
        for g in geos:
            p = os.path.join(outdir, '%s_%s.obj' % (base, g[0]))
            write_obj(p, [g])
            print('->', p)
    else:
        p = os.path.join(outdir, base + '.obj')
        write_obj(p, geos)
        print('->', p)


# ------------------------------------------------------- import modelo (OBJ)
def obj_float(value):
    """Convierte numeros OBJ aceptando coma o punto decimal."""
    value = value.strip()

    if ',' in value and '.' not in value:
        value = value.replace(',', '.')

    try:
        return float(value)
    except ValueError:
        raise RX3Error(
            "Numero invalido en OBJ: %r. "
            "Se esperaba un decimal con punto o coma." % value
        )


def parse_obj(path):
    v, vt, buckets, order = [], [], {}, []
    cur = None

    def bucket(name):
        if name not in buckets:
            buckets[name] = []
            order.append(name)
        return buckets[name]

    with open(path, 'r', errors='replace') as f:
        for line_num, line in enumerate(f, 1):
            p = line.split()

            if not p:
                continue

            c = p[0]

            try:
                if c == 'v' and len(p) >= 4:
                    v.append((
                        obj_float(p[1]),
                        obj_float(p[2]),
                        obj_float(p[3])
                    ))

                elif c == 'vt' and len(p) >= 3:
                    vt.append((
                        obj_float(p[1]),
                        obj_float(p[2])
                    ))

                elif c in ('o', 'g'):
                    name = ' '.join(p[1:]) or 'sin_nombre'

                    if cur is not None and not buckets[cur] and cur not in ('__inicio__',):
                        buckets[name] = buckets.pop(cur)
                        order[order.index(cur)] = name
                        cur = name
                    else:
                        cur = name
                        bucket(cur)

                elif c == 'f' and len(p) >= 4:
                    if cur is None:
                        cur = '__inicio__'
                        bucket(cur)

                    poly = []

                    for tok in p[1:]:
                        f_ = tok.split('/')

                        vi = int(f_[0])
                        vi = vi - 1 if vi > 0 else len(v) + vi

                        ti = None

                        if len(f_) > 1 and f_[1]:
                            ti = int(f_[1])
                            ti = ti - 1 if ti > 0 else len(vt) + ti

                        poly.append((vi, ti))

                    buckets[cur].append(poly)

            except (ValueError, IndexError) as e:
                raise RX3Error(
                    "Error leyendo OBJ en linea %d: %s"
                    % (line_num, e)
                )

    return v, vt, [
        (n, buckets[n])
        for n in order
        if buckets[n]
    ]


class NearestVertex:
    """Busca el vertice original mas cercano (para heredar normales, huesos...)."""

    def __init__(self, pts):
        self.pts = pts
        mn = [min(p[a] for p in pts) for a in range(3)]
        mx = [max(p[a] for p in pts) for a in range(3)]
        ext = max(mx[a] - mn[a] for a in range(3)) or 1.0
        self.cell = ext / max(1.0, len(pts) ** (1 / 3.0))
        self.grid = {}
        for i, p in enumerate(pts):
            self.grid.setdefault(self._c(p), []).append(i)

    def _c(self, p):
        c = self.cell
        return (int(math.floor(p[0] / c)), int(math.floor(p[1] / c)),
                int(math.floor(p[2] / c)))

    def query(self, p, uv, uvs):
        cx, cy, cz = self._c(p)
        best, bd, r = None, 1e30, 0
        while r < 8:          # vertices lejanos: se cae a la busqueda directa (rapida)
            rng = range(-r, r + 1)
            for dx in rng:
                for dy in rng:
                    for dz in rng:
                        if max(abs(dx), abs(dy), abs(dz)) != r:
                            continue
                        for i in self.grid.get((cx + dx, cy + dy, cz + dz), ()):
                            q = self.pts[i]
                            d = ((q[0] - p[0]) ** 2 + (q[1] - p[1]) ** 2 +
                                 (q[2] - p[2]) ** 2)
                            w = uvs[i]
                            d += 1e-6 * ((w[0] - uv[0]) ** 2 + (w[1] - uv[1]) ** 2)
                            if d < bd:
                                bd, best = d, i
            if best is not None and bd <= (r * self.cell) ** 2:
                break
            r += 1
        if best is None:
            best = min(range(len(self.pts)),
                       key=lambda i: sum((self.pts[i][a] - p[a]) ** 2 for a in range(3)))
        return best


def diag(pts):
    if not pts:
        return 0.0
    mn = [min(p[a] for p in pts) for a in range(3)]
    mx = [max(p[a] for p in pts) for a in range(3)]
    return math.sqrt(sum((mx[a] - mn[a]) ** 2 for a in range(3)))


def patch_refs(rx, skip, old, new, old_isz=None, new_isz=None, prefer=None):
    """Busca en las DEMAS secciones el par de dwords `old` (p. ej. tamano e
    indices de un buffer) y lo cambia por `new`. Asi se mantiene al dia la
    seccion 'objetos' (y cualquier otra copia) sin depender de su posicion."""
    E = rx.E
    pat, rep = struct.pack(E + '2I', *old), struct.pack(E + '2I', *new)
    n = 0
    for s in rx.secs:
        if s is skip:
            continue
        d = bytearray(s.data)
        hits, pos = [], 0
        while True:
            j = d.find(pat, pos)
            if j < 0:
                break
            if j % 4 == 0:
                hits.append(j)
            pos = j + 1
        if not hits:
            continue
        if len(hits) > 1:
            pick = [j for j in hits if prefer is not None and j == 16 + 16 * prefer]
            if not pick:
                warn("seccion '%s': hay %d entradas iguales; se cambia la primera"
                     % (s.name, len(hits)))
                pick = hits[:1]
            hits = pick
        for j in hits:
            d[j:j + 8] = rep
            if old_isz is not None and j + 8 < len(d) and d[j + 8] == old_isz:
                d[j + 8] = new_isz
            n += 1
            print("  seccion '%s' actualizada (entrada en offset %d)" % (s.name, j))
        s.data = bytes(d)
    return n


def rebuild_mesh(rx, m, polys, v, vt, scale):
    """Crea nuevas secciones de vertices/indices para la malla m."""
    E = rx.E
    if not m.has_pos:
        raise RX3Error('malla %d sin posiciones soportadas' % m.k)
    if m.vc == 0:
        raise RX3Error('malla %d original sin vertices; no hay plantilla' % m.k)
    keymap, newv, tris = {}, [], []

    def corner(c):
        i = keymap.get(c)
        if i is None:
            i = len(newv)
            keymap[c] = i
            if c[0] < 0 or c[0] >= len(v):
                raise RX3Error('indice v fuera de rango en el OBJ')
            p = v[c[0]]
            uv = vt[c[1]] if (c[1] is not None and 0 <= c[1] < len(vt)) else (0.0, 0.0)
            newv.append(((p[0] / scale, p[1] / scale, p[2] / scale), (uv[0], 1.0 - uv[1])))
        return i
    for poly in polys:
        ids = [corner(c) for c in poly]
        for j in range(1, len(ids) - 1):
            t = (ids[0], ids[j], ids[j + 1])
            if len(set(t)) == 3:
                tris.append(t)
    if not tris:
        raise RX3Error('el OBJ no tiene triangulos validos para la malla %d' % m.k)
    nv = len(newv)
    if nv > 0xFFFFFFFF:
        raise RX3Error('demasiados vertices')

    opos = [m.pos(i) for i in range(m.vc)]
    ouv = [m.uv(i) for i in range(m.vc)]
    if abs(diag([p for p, _ in newv]) / (diag(opos) or 1.0) - 1.0) > 0.5:
        warn('el tamano del modelo cambia mucho respecto al original; '
             '¿usaste la misma --scale al exportar e importar?')
    nn = NearestVertex(opos)
    stride = m.stride
    buf = bytearray(nv * stride)
    tmp = Mesh.__new__(Mesh)          # plantilla de escritura de un solo registro
    tmp.elems, tmp.stride = m.elems, stride
    for i, (p, uv) in enumerate(newv):
        src = nn.query(p, uv, ouv)
        tmp.vraw = bytearray(m.vraw[src * stride:(src + 1) * stride])
        tmp.set_pos(0, p)
        tmp.set_uv(0, uv)
        buf[i * stride:(i + 1) * stride] = tmp.vraw
    # --- secciones nuevas (se conservan los bytes de cabecera originales)
    old_v = (m.old_vsize, m.vc)
    old_i = (m.old_isize, m.icount)
    isz = 2 if nv <= 65535 else 4
    fmt = E + ('HHH' if isz == 2 else 'III')
    idata = b''.join(struct.pack(fmt, *t) for t in tris)

    vhdr = bytearray(m.sv.data[:16])
    struct.pack_into(E + '2I', vhdr, 0, r16(16 + nv * stride), nv)
    rx.set_data(m.sv, bytes(vhdr) + bytes(buf))

    ihdr = bytearray(m.si.data[:16])
    struct.pack_into(E + '2I', ihdr, 0, r16(16 + len(idata)), len(tris) * 3)
    ihdr[m.isz_off] = isz
    rx.set_data(m.si, bytes(ihdr) + idata)

    new_v = (len(m.sv.data), nv)
    new_i = (len(m.si.data), len(tris) * 3)
    if new_i != old_i or isz != m.isz:
        if not patch_refs(rx, m.si, old_i, new_i, m.isz, isz, prefer=m.k):
            warn('no encontre en el archivo la copia del tamano/numero de indices; '
                 'si el modelo no se ve en el juego, enviame la salida de "check".')
    if new_v != old_v:
        patch_refs(rx, m.sv, old_v, new_v, prefer=m.k)
    aabb = (tuple(min(p[a] for p, _ in newv) for a in range(3)),
            tuple(max(p[a] for p, _ in newv) for a in range(3)))
    print('malla %d: %d -> %d vertices, %d -> %d triangulos (indices de %d bytes)'
          % (m.k, m.vc, nv, len(m.tris), len(tris), isz))
    return aabb


def inplace_import(m, polys, v, vt, scale, force):
    """Si el OBJ tiene la misma topologia que la exportacion de esta malla, solo
    se actualizan posiciones y UV de los vertices originales (normales, tangentes,
    huesos, indices y tamanos quedan intactos). Devuelve None si no encaja."""
    if any(len(p) != 3 for p in polys):
        return None
    found = None
    for weld in (True, False):
        verts, uvs, faces, corner, face_src = mesh_geometry(m, scale, weld)
        if len(faces) != len(polys) or not faces:
            continue
        base = min(vi for p in polys for vi, _ in p)
        if all(polys[k][j][0] - base == faces[k][j][0]
               for k in range(len(faces)) for j in range(3)):
            found = (verts, uvs, faces, corner, face_src, base)
            break
    if not found:
        return None
    verts, uvs, faces, corner, face_src, base = found
    if base + len(verts) > len(v):
        return None
    objv = v[base:base + len(verts)]
    ratio = diag(objv) / (diag(verts) or 1.0)
    if not (0.5 <= ratio <= 2.0):
        msg = ('el tamano del OBJ es %.2f veces el esperado: ¿--scale distinto al de la '
               'exportacion?' % ratio)
        if not force:
            raise RX3Error('malla %d: %s (usa --force si es a proposito)' % (m.k, msg))
        warn(msg)
    tol = 1.5e-6        # el OBJ se escribe con 6 decimales
    nvpos = nuv = conflicts = 0
    for i, (vi, _) in corner.items():
        pv, ev = objv[vi], verts[vi]
        if max(abs(pv[0] - ev[0]), abs(pv[1] - ev[1]), abs(pv[2] - ev[2])) > tol:
            m.set_pos(i, (pv[0] / scale, pv[1] / scale, pv[2] / scale))
            nvpos += 1
    seen = {}
    for k, poly in enumerate(polys):
        for j in range(3):
            ti = poly[j][1]
            if ti is None or not (0 <= ti < len(vt)):
                continue
            i = face_src[k][j]
            u, w = vt[ti]
            eu, ew = uvs[corner[i][1]]
            if abs(u - eu) <= tol and abs(w - ew) <= tol:
                continue
            if i in seen:
                if abs(seen[i][0] - u) > tol or abs(seen[i][1] - w) > tol:
                    conflicts += 1
                continue
            seen[i] = (u, w)
            m.set_uv(i, (u, 1.0 - w))
            nuv += 1
    if conflicts:
        warn('malla %d: %d vertices piden UV distintas en caras distintas (costuras '
             'nuevas); se usa la primera. Para cambiar costuras hay que reconstruir '
             '(--rebuild).' % (m.k, conflicts))
    m.store_vraw()
    print('malla %d: EN SITIO, %d vertices movidos, %d UV cambiadas '
          '(estructura del archivo intacta)' % (m.k, nvpos, nuv))
    return True


def update_bboxes(rx, changed):
    """changed: {mesh_idx: (aabb_original, aabb_nuevo)}. Solo AMPLIA cajas y solo
    si la estructura se verifica contra la caja original."""
    E = rx.E
    for s in rx.by_hash(H_GROUP):
        d = bytearray(s.data)
        if len(d) < 120:
            continue
        cnt = struct.unpack_from(E + 'I', d, 112)[0]
        if cnt > 10000 or 120 + cnt * 40 > len(d):
            continue
        ents, ok = [], True
        for j in range(cnt):
            o = 120 + j * 40
            mn = struct.unpack_from(E + '3f', d, o)
            mx = struct.unpack_from(E + '3f', d, o + 16)
            idx = struct.unpack_from(E + 'I', d, o + 32)[0]
            if idx in changed:
                (omn, omx), _ = changed[idx]
                ext = max(omx[a] - omn[a] for a in range(3)) or 1.0
                tol = 1.0 + 0.02 * ext
                if not all(mn[a] <= omn[a] + tol and mx[a] >= omx[a] - tol and
                           (mx[a] - mn[a]) <= 3 * (omx[a] - omn[a]) + 2 * tol
                           for a in range(3)):
                    ok = False
                    break
                ents.append((o, mn, mx, idx))
        if not ok or not ents:
            continue
        gmn = list(struct.unpack_from(E + '3f', d, 80))
        gmx = list(struct.unpack_from(E + '3f', d, 96))
        for o, mn, mx, idx in ents:
            _, (nmn, nmx) = changed[idx]
            emn = [min(mn[a], nmn[a]) for a in range(3)]
            emx = [max(mx[a], nmx[a]) for a in range(3)]
            struct.pack_into(E + '3f', d, o, *emn)
            struct.pack_into(E + '3f', d, o + 16, *emx)
            gmn = [min(gmn[a], emn[a]) for a in range(3)]
            gmx = [max(gmx[a], emx[a]) for a in range(3)]
        struct.pack_into(E + '3f', d, 80, *gmn)
        struct.pack_into(E + '3f', d, 96, *gmx)
        s.data = bytes(d)
        print('bounding boxes del grupo actualizadas (%d entradas)' % len(ents))


def cmd_import_model(a):
    rx = RX3.open(a.file)
    meshes = get_meshes(rx)
    v, vt, buckets = parse_obj(a.obj)
    if not buckets:
        raise RX3Error('el OBJ no contiene caras')
    assign = {}
    if a.mesh is not None:
        if a.mesh >= len(meshes):
            raise RX3Error('la malla %d no existe (hay %d)' % (a.mesh, len(meshes)))
        assign[a.mesh] = [p for _, polys in buckets for p in polys]
    else:
        for pos, (name, polys) in enumerate(buckets):
            mt = re.search(r'mesh[_\-]?0*(\d+)', name, re.I)
            idx = int(mt.group(1)) if mt else (pos if len(buckets) == len(meshes) else None)
            if idx is None or idx >= len(meshes):
                raise RX3Error("No se a que malla corresponde el objeto '%s'. "
                               "Usa --mesh N o nombralo mesh_000, mesh_001..." % name)
            assign.setdefault(idx, []).extend(polys)
    changed = {}
    for idx, polys in sorted(assign.items()):
        m = meshes[idx]
        if not m.has_pos:
            raise RX3Error('malla %d sin posiciones soportadas' % idx)
        old = m.aabb() if m.vc else ((0, 0, 0), (0, 0, 0))
        if not a.rebuild and inplace_import(m, polys, v, vt, a.scale, a.force):
            changed[idx] = (old, m.aabb())
            continue
        if not a.rebuild:
            print('malla %d: la topologia no coincide con la exportacion '
                  '(caras/vertices distintos) -> se reconstruyen los buffers' % idx)
        changed[idx] = (old, rebuild_mesh(rx, m, polys, v, vt, a.scale))
    if not a.no_bbox:
        update_bboxes(rx, changed)
    out = a.out or os.path.splitext(a.file)[0] + '_mod.rx3'
    data = rx.build()
    problems = verify(RX3(data))
    with open(out, 'wb') as f:
        f.write(data)
    print('->', out, '(el original no se toca)')
    report_problems(problems)
    if rx.compressed:
        warn('el original estaba comprimido (chunkzip); la salida va SIN comprimir')


# ----------------------------------------------------------------- texturas
def tex_parse(rx, sec):
    E = rx.E
    d = sec.data
    ident = d[5]
    w = struct.unpack_from(E + 'H', d, 8)[0]
    h = struct.unpack_from(E + 'H', d, 10)[0]
    mips = struct.unpack_from(E + 'H', d, 14)[0]
    chunks, p = [], 16
    for _ in range(mips):
        if p + 16 > len(d):
            break
        _, _, size, _ = struct.unpack_from(E + '4I', d, p)
        p += 16
        if p + size > len(d):
            raise RX3Error('textura truncada')
        chunks.append(d[p:p + size])
        p += size
    return ident, w, h, mips, chunks


def effective_ident(ident, w, h, chunks):
    """Id de formato que se usara. Si el id es desconocido se intenta adivinar
    por el tamano de los datos (solo 32 bits y 8 bits)."""
    if ident in TEX_FMT:
        return ident
    if chunks and w and h:
        if len(chunks[0]) == w * h * 4:
            return ID_RAW32
        if len(chunks[0]) == w * h:
            return ID_L8
    return None


def mip_dims(w, h, n, raw):
    out, mw, mh = [], w, h
    lim = 1 if raw else 4
    for _ in range(n):
        out.append((mw, mh))
        mw, mh = max(lim, mw // 2), max(lim, mh // 2)
    return out


def swizzle(data, src, dst):
    """Reordena los canales de pixeles de 32 bits entre dos ordenes ('rgba', 'bgra'...)."""
    if src == dst:
        return bytes(data)
    out = bytearray(len(data))
    for k, ch in enumerate(dst):
        out[k::4] = data[src.index(ch)::4]
    return bytes(out)


def dds_header(w, h, mips, ident, linear):
    flags = 0x1 | 0x2 | 0x4 | 0x1000
    caps = 0x1000
    if mips > 1:
        flags |= 0x20000
        caps |= 0x8 | 0x400000
    if ident == ID_RAW32:       # BGRA de 32 bits, lo que entienden todos los programas
        flags |= 0x8
        pf = struct.pack('<2I', 32, 0x41) + b'\x00' * 4 + struct.pack(
            '<5I', 32, 0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)
    elif ident == ID_L8:
        flags |= 0x8
        pf = struct.pack('<2I', 32, 0x20000) + b'\x00' * 4 + struct.pack(
            '<5I', 8, 0xFF, 0, 0, 0)
    else:
        flags |= 0x80000
        pf = struct.pack('<2I', 32, 4) + TEX_FMT[ident][0].encode() + b'\x00' * 20
    return (b'DDS ' + struct.pack('<7I', 124, flags, h, w, linear, 0, mips) +
            b'\x00' * 44 + pf + struct.pack('<5I', caps, 0, 0, 0, 0))


def read_dds(path):
    """Devuelve (ident, w, h, chunks). Si es sin comprimir se devuelve ident=3 con
    los pixeles ya en orden RGBA (se reordena luego al escribir en el RX3)."""
    with open(path, 'rb') as f:
        d = f.read()
    if d[:4] != b'DDS ' or len(d) < 128:
        raise RX3Error('%s no es un DDS valido' % path)
    h, w = struct.unpack_from('<2I', d, 12)
    mips = struct.unpack_from('<I', d, 28)[0] or 1
    pf_flags = struct.unpack_from('<I', d, 80)[0]
    fourcc = d[84:88].decode('ascii', 'replace')
    bits = struct.unpack_from('<I', d, 88)[0]
    mk = struct.unpack_from('<4I', d, 92)       # R, G, B, A
    if pf_flags & 0x4:                           # comprimido
        if fourcc == 'DX10':
            raise RX3Error('DDS con cabecera DX10 no soportado; guardalo como DXT1/DXT5')
        if fourcc not in FOURCC_TO_ID:
            raise RX3Error('formato DDS %r no soportado (usa DXT1, DXT3, DXT5 o ATI2)'
                           % fourcc)
        ident = FOURCC_TO_ID[fourcc]
        blk = TEX_FMT[ident][1]
        chunks, p, mw, mh = [], 128, w, h
        for _ in range(mips):
            size = max(1, (mw + 3) // 4) * max(1, (mh + 3) // 4) * blk
            if p + size > len(d):
                raise RX3Error('DDS truncado')
            chunks.append(d[p:p + size])
            p += size
            mw, mh = max(mw // 2, 4), max(mh // 2, 4)
        return ident, w, h, chunks
    if pf_flags & 0x40 and bits in (24, 32):    # RGB / RGBA sin comprimir
        bpp = bits // 8
        pos = []
        for m_ in mk:
            if m_ == 0:
                pos.append(None)
            elif m_ in (0xFF, 0xFF00, 0xFF0000, 0xFF000000):
                pos.append((m_.bit_length() - 1) // 8)
            else:
                raise RX3Error('DDS sin comprimir con mascaras raras; guardalo como '
                               'A8R8G8B8 (32 bits) o DXT5')
        if pos[0] is None or pos[1] is None or pos[2] is None:
            raise RX3Error('DDS sin canales RGB')
        chunks, p = [], 128
        for mw, mh in mip_dims(w, h, mips, True):
            size = mw * mh * bpp
            if p + size > len(d):
                raise RX3Error('DDS truncado')
            px, n = d[p:p + size], mw * mh
            out = bytearray(n * 4)
            for k in range(4):
                if pos[k] is None:
                    out[k::4] = b'\xff' * n          # sin alfa -> opaco
                else:
                    out[k::4] = px[pos[k]::bpp]
            chunks.append(bytes(out))
            p += size
        return ID_RAW32, w, h, chunks
    raise RX3Error('DDS no soportado (usa DXT1/DXT3/DXT5/ATI2 o A8R8G8B8 de 32 bits)')


# ---- decodificacion DXT -> RGBA
def _from565(c):
    r, g, b = (c >> 11) & 31, (c >> 5) & 63, c & 31
    return ((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2))


def _color_palette(c0, c1, four):
    a, b = _from565(c0), _from565(c1)
    p = [a + (255,), b + (255,)]
    if four or c0 > c1:
        p.append(tuple((2 * a[i] + b[i]) // 3 for i in range(3)) + (255,))
        p.append(tuple((a[i] + 2 * b[i]) // 3 for i in range(3)) + (255,))
    else:
        p.append(tuple((a[i] + b[i]) // 2 for i in range(3)) + (255,))
        p.append((0, 0, 0, 0))
    return p


def _bc4_palette(a0, a1):
    if a0 > a1:
        return [a0, a1] + [((7 - i) * a0 + i * a1) // 7 for i in range(1, 7)]
    return [a0, a1] + [((5 - i) * a0 + i * a1) // 5 for i in range(1, 5)] + [0, 255]


def decode_dxt(data, w, h, ident):
    out = bytearray(w * h * 4)
    blk = TEX_FMT[ident][1]
    bw, bh = (w + 3) // 4, (h + 3) // 4
    pos = 0
    for by in range(bh):
        for bx in range(bw):
            b = data[pos:pos + blk]
            pos += blk
            px = [None] * 16
            if ident == 7:        # ATI2 / BC5: dos bloques BC4 (R y G)
                chans = []
                for k in range(2):
                    sub = b[k * 8:k * 8 + 8]
                    pal = _bc4_palette(sub[0], sub[1])
                    bits = int.from_bytes(sub[2:8], 'little')
                    chans.append([pal[(bits >> (3 * i)) & 7] for i in range(16)])
                for i in range(16):
                    x = chans[0][i] / 127.5 - 1.0
                    y = chans[1][i] / 127.5 - 1.0
                    z = math.sqrt(max(0.0, 1.0 - x * x - y * y))
                    px[i] = (chans[0][i], chans[1][i], int((z * 0.5 + 0.5) * 255), 255)
            else:
                cb = b[8:16] if ident != 0 else b[0:8]
                c0, c1, bits = struct.unpack('<HHI', cb)
                pal = _color_palette(c0, c1, ident != 0)
                for i in range(16):
                    px[i] = pal[(bits >> (2 * i)) & 3]
                if ident == 1:
                    ab = int.from_bytes(b[0:8], 'little')
                    px = [px[i][:3] + (((ab >> (4 * i)) & 15) * 17,) for i in range(16)]
                elif ident == 2:
                    ap = _bc4_palette(b[0], b[1])
                    ab = int.from_bytes(b[2:8], 'little')
                    px = [px[i][:3] + (ap[(ab >> (3 * i)) & 7],) for i in range(16)]
            for py in range(4):
                y = by * 4 + py
                if y >= h:
                    break
                row = bytearray()
                for qx in range(4):
                    if bx * 4 + qx >= w:
                        break
                    row += bytes(px[py * 4 + qx])
                o = (y * w + bx * 4) * 4
                out[o:o + len(row)] = row
    return bytes(out)


def decode_tex(chunk, w, h, ident, order):
    """Un nivel de la textura -> RGBA (bytes)."""
    if ident == ID_RAW32:
        return swizzle(chunk[:w * h * 4], order, 'rgba')
    if ident == ID_L8:
        out = bytearray(w * h * 4)
        g = bytes(chunk[:w * h])
        out[0::4] = g
        out[1::4] = g
        out[2::4] = g
        out[3::4] = b'\xff' * (w * h)
        return bytes(out)
    return decode_dxt(chunk, w, h, ident)


def write_png(path, w, h, rgba):
    raw = bytearray()
    st = w * 4
    for y in range(h):
        raw.append(0)
        raw += rgba[y * st:(y + 1) * st]

    def chunk(t, d):
        c = struct.pack('>I', len(d)) + t + d
        return c + struct.pack('>I', zlib.crc32(t + d) & 0xffffffff)
    with open(path, 'wb') as f:
        f.write(b'\x89PNG\r\n\x1a\n' +
                chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0)) +
                chunk(b'IDAT', zlib.compress(bytes(raw), 6)) + chunk(b'IEND', b''))


def read_png(path):
    """Lector PNG minimo (sin Pillow): 8/16 bits, gris/RGB/paleta/RGBA, sin entrelazar."""
    with open(path, 'rb') as f:
        d = f.read()
    if d[:8] != b'\x89PNG\r\n\x1a\n':
        raise RX3Error('%s no es un PNG' % path)
    pos, idat, plte, trns = 8, b'', None, None
    w = h = depth = ctype = inter = None
    while pos + 8 <= len(d):
        ln, t = struct.unpack_from('>I4s', d, pos)
        body = d[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if t == b'IHDR':
            w, h, depth, ctype, _, _, inter = struct.unpack('>IIBBBBB', body)
        elif t == b'PLTE':
            plte = body
        elif t == b'tRNS':
            trns = body
        elif t == b'IDAT':
            idat += body
        elif t == b'IEND':
            break
    if w is None:
        raise RX3Error('PNG sin cabecera')
    if inter:
        raise RX3Error('PNG entrelazado no soportado sin Pillow; guardalo sin entrelazar')
    chans = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    bpp = max(1, chans * depth // 8)
    stride = (w * chans * depth + 7) // 8
    raw = zlib.decompress(idat)
    rows, prev = [], bytearray(stride)
    p = 0
    for _ in range(h):
        ft = raw[p]
        cur = bytearray(raw[p + 1:p + 1 + stride])
        p += 1 + stride
        if ft == 1:
            for i in range(bpp, stride):
                cur[i] = (cur[i] + cur[i - bpp]) & 255
        elif ft == 2:
            for i in range(stride):
                cur[i] = (cur[i] + prev[i]) & 255
        elif ft == 3:
            for i in range(stride):
                left = cur[i - bpp] if i >= bpp else 0
                cur[i] = (cur[i] + ((left + prev[i]) >> 1)) & 255
        elif ft == 4:
            for i in range(stride):
                a = cur[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                cur[i] = (cur[i] + pr) & 255
        rows.append(cur)
        prev = cur
    out = bytearray(w * h * 4)
    for y, row in enumerate(rows):
        if depth == 16:
            row = row[0::2]            # nos quedamos con el byte alto
        elif depth < 8:
            vals = []
            for byte in row:
                for s in range(8 - depth, -1, -depth):
                    vals.append((byte >> s) & ((1 << depth) - 1))
            row = bytearray(vals[:w * chans])
            if ctype == 0:
                row = bytearray(v * 255 // ((1 << depth) - 1) for v in row)
        o = y * w * 4
        if ctype == 6:
            out[o:o + w * 4] = row[:w * 4]
        elif ctype == 2:
            out[o:o + w * 4:4] = row[0::3]
            out[o + 1:o + w * 4:4] = row[1::3]
            out[o + 2:o + w * 4:4] = row[2::3]
            out[o + 3:o + w * 4:4] = b'\xff' * w
        elif ctype == 0:
            for k in range(3):
                out[o + k:o + w * 4:4] = row[:w]
            out[o + 3:o + w * 4:4] = b'\xff' * w
        elif ctype == 4:
            for k in range(3):
                out[o + k:o + w * 4:4] = row[0::2]
            out[o + 3:o + w * 4:4] = row[1::2]
        elif ctype == 3:
            if plte is None:
                raise RX3Error('PNG con paleta sin PLTE')
            for x in range(w):
                i = row[x]
                out[o + x * 4:o + x * 4 + 3] = plte[i * 3:i * 3 + 3]
                out[o + x * 4 + 3] = trns[i] if trns and i < len(trns) else 255
    return bytes(out), w, h


def load_image(path):
    """Imagen -> (RGBA bytes, ancho, alto). Usa Pillow si esta; si no, lector PNG propio."""
    try:
        from PIL import Image
    except ImportError:
        if path.lower().endswith('.png'):
            return read_png(path)
        raise RX3Error('Sin Pillow solo se pueden importar PNG o DDS '
                       '(pkg install python-pillow para JPG, BMP, etc.)')
    im = Image.open(path).convert('RGBA')
    return im.tobytes(), im.size[0], im.size[1]


def resize_rgba(rgba, w, h, nw, nh):
    """Reduccion por promedio de cajas (box). Pillow si esta; si no, codigo propio."""
    if (w, h) == (nw, nh):
        return rgba
    try:
        from PIL import Image
        box = getattr(getattr(Image, 'Resampling', Image), 'BOX')
        return Image.frombytes('RGBA', (w, h), rgba).resize((nw, nh), box).tobytes()
    except ImportError:
        pass
    out = bytearray(nw * nh * 4)
    if w % nw or h % nh:                      # vecino mas cercano
        for y in range(nh):
            sy = y * h // nh
            for x in range(nw):
                sx = x * w // nw
                out[(y * nw + x) * 4:(y * nw + x) * 4 + 4] = rgba[(sy * w + sx) * 4:(sy * w + sx) * 4 + 4]
        return bytes(out)
    fx, fy = w // nw, h // nh
    n = fx * fy
    for y in range(nh):
        for x in range(nw):
            acc = [0, 0, 0, 0]
            for yy in range(fy):
                base = ((y * fy + yy) * w + x * fx) * 4
                for xx in range(fx):
                    o = base + xx * 4
                    acc[0] += rgba[o]
                    acc[1] += rgba[o + 1]
                    acc[2] += rgba[o + 2]
                    acc[3] += rgba[o + 3]
            out[(y * nw + x) * 4:(y * nw + x) * 4 + 4] = bytes(c // n for c in acc)
    return bytes(out)


def cmd_export_tex(a):
    rx = RX3.open(a.file)
    base = os.path.splitext(os.path.basename(a.file))[0]
    outdir = a.out or (base + '_out')
    os.makedirs(outdir, exist_ok=True)
    texs = rx.by_hash(H_TEX)
    if not texs:
        raise RX3Error('El archivo no contiene texturas')
    done = 0
    for k, s in enumerate(texs):
        if a.index is not None and k != a.index:
            continue
        ident, w, h, mips, chunks = tex_parse(rx, s)
        eff = effective_ident(ident, w, h, chunks)
        if eff is None:
            warn('textura %d: formato id %d desconocido (%dx%d, %d bytes en el nivel 0); '
                 'se omite. Cabecera: %s' % (k, ident, w, h, len(chunks[0]) if chunks else 0,
                                           s.data[:16].hex()))
            continue
        if eff != ident:
            warn('textura %d: id %d desconocido; por el tamano se trata como %s'
                 % (k, ident, 'RGBA 32 bits' if eff == ID_RAW32 else 'gris 8 bits'))
        name = os.path.join(outdir, '%s_tex%03d' % (base, k))
        with open(name + '.dds', 'wb') as f:
            f.write(dds_header(w, h, len(chunks), eff, len(chunks[0])))
            for c in chunks:
                f.write(swizzle(c, a.order, 'bgra') if eff == ID_RAW32 else c)
        fname = ('id%d' % ident) if eff != ident else TEX_FMT[ident][0]
        msg = 'textura %d: %dx%d %s, %d mips -> %s.dds' % (k, w, h, fname, len(chunks), name)
        if not a.no_png:
            write_png(name + '.png', w, h, decode_tex(chunks[0], w, h, eff, a.order))
            msg += ' + .png'
        print(msg)
        done += 1
    if not done:
        raise RX3Error('no se exporto ninguna textura')


# ---- codificacion RGBA -> DXT
def _to565(r, g, b):
    return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)


def _enc_color(px, mode):
    """px: bytes de 16 pixeles RGBA. mode: 'dxt1a' (1 bit alfa) o 'four'."""
    r, g, b, al = px[0::4], px[1::4], px[2::4], px[3::4]
    if mode == 'dxt1a' and min(al) < 128:
        idx = [i for i in range(16) if al[i] >= 128]
        if not idx:
            return struct.pack('<HHI', 0, 0, 0xFFFFFFFF)
        rr, gg, bb = [r[i] for i in idx], [g[i] for i in idx], [b[i] for i in idx]
        c0 = _to565(max(rr), max(gg), max(bb))
        c1 = _to565(min(rr), min(gg), min(bb))
        if c0 > c1:
            c0, c1 = c1, c0           # c0 <= c1 -> modo de 3 colores + transparente
        pal = _color_palette(c0, c1, False)
        bits = 0
        for i in range(16):
            if al[i] < 128:
                q = 3
            else:
                q = min(range(3), key=lambda k: (pal[k][0] - r[i]) ** 2 +
                        (pal[k][1] - g[i]) ** 2 + (pal[k][2] - b[i]) ** 2)
            bits |= q << (2 * i)
        return struct.pack('<HHI', c0, c1, bits)
    mnr, mxr, mng, mxg, mnb, mxb = min(r), max(r), min(g), max(g), min(b), max(b)
    ir, ig, ib = (mxr - mnr) >> 4, (mxg - mng) >> 4, (mxb - mnb) >> 4
    c0 = _to565(mxr - ir, mxg - ig, mxb - ib)
    c1 = _to565(mnr + ir, mng + ig, mnb + ib)
    if c0 == c1:
        return struct.pack('<HHI', c0, c1, 0)
    if c0 < c1:
        c0, c1 = c1, c0
    a, bq = _from565(c0), _from565(c1)
    dr, dg, db = a[0] - bq[0], a[1] - bq[1], a[2] - bq[2]
    dd = float(dr * dr + dg * dg + db * db) or 1.0
    bits = 0
    mp = (1, 3, 2, 0)
    for i in range(16):
        t = ((r[i] - bq[0]) * dr + (g[i] - bq[1]) * dg + (b[i] - bq[2]) * db) / dd
        q = int(t * 3 + 0.5)
        q = 0 if q < 0 else 3 if q > 3 else q
        bits |= mp[q] << (2 * i)
    return struct.pack('<HHI', c0, c1, bits)


def _enc_bc4(vals):
    a0, a1 = max(vals), min(vals)
    if a0 == a1:
        return bytes([a0, a1]) + b'\x00' * 6
    mp = (1, 7, 6, 5, 4, 3, 2, 0)
    rng = float(a0 - a1)
    bits = 0
    for i, v in enumerate(vals):
        t = int((v - a1) / rng * 7 + 0.5)
        bits |= mp[t] << (3 * i)
    return bytes([a0, a1]) + bits.to_bytes(6, 'little')


def encode_dxt(rgba, w, h, ident, progress=False):
    out = bytearray()
    total = ((h + 3) // 4) or 1
    last = -1
    for by in range(0, h, 4):
        if progress:
            pct = (by // 4) * 100 // total
            if pct // 10 != last:
                last = pct // 10
                sys.stdout.write('\r  codificando DXT... %d%%' % pct)
                sys.stdout.flush()
        for bx in range(0, w, 4):
            rows = []
            for py in range(4):
                y = min(by + py, h - 1)
                o = (y * w + bx) * 4
                rows.append(rgba[o:o + 16])
            px = b''.join(rows)
            if ident == 0:
                out += _enc_color(px, 'dxt1a')
            elif ident == 1:
                al = px[3::4]
                bits = 0
                for i, v in enumerate(al):
                    bits |= min(15, (v + 8) >> 4) << (4 * i)
                out += bits.to_bytes(8, 'little') + _enc_color(px, 'four')
            elif ident == 2:
                out += _enc_bc4(px[3::4]) + _enc_color(px, 'four')
            else:
                raise RX3Error('no se puede codificar a ATI2 desde PNG; usa un DDS')
    if progress:
        sys.stdout.write('\r  codificando DXT... 100%\n')
    return bytes(out)


def build_texture_section(rx, old, ident, w, h, chunks):
    E = rx.E
    raw = ident == ID_RAW32
    blk = TEX_FMT[ident][1]
    body = b''
    for c, (mw, mh) in zip(chunks, mip_dims(w, h, len(chunks), raw)):
        if raw:
            pitch, rows = mw * 4, mh
        else:
            pitch, rows = ((mw + 3) // 4) * blk, (mh + 3) // 4
        body += struct.pack(E + '4I', pitch, rows, len(c), 0) + c
    hdr = bytearray(old.data[:16])
    struct.pack_into(E + 'I', hdr, 0, r16(16 + len(body)))
    hdr[5] = ident
    struct.pack_into(E + 'H', hdr, 8, w)
    struct.pack_into(E + 'H', hdr, 10, h)
    struct.pack_into(E + 'H', hdr, 14, len(chunks))
    return bytes(hdr) + body


def cmd_import_tex(a):
    rx = RX3.open(a.file)
    texs = rx.by_hash(H_TEX)
    if a.index < 0 or a.index >= len(texs):
        raise RX3Error('textura %d no existe (hay %d)' % (a.index, len(texs)))
    old = texs[a.index]
    o_id, ow, oh, omips, o_chunks = tex_parse(rx, old)
    o_eff = effective_ident(o_id, ow, oh, o_chunks)
    # --- origen
    s_ident, schunks = None, None
    if a.image.lower().endswith('.dds'):
        s_ident, w, h, schunks = read_dds(a.image)
        if s_ident == ID_RAW32:
            schunks = [swizzle(c, 'rgba', a.order) for c in schunks]   # al orden del RX3
    else:
        rgba, w, h = load_image(a.image)
    # --- formato destino
    if a.format:
        ident = FORMAT_CHOICES[a.format]
    elif o_eff in TEX_FMT:
        ident = o_eff
    else:
        raise RX3Error('la textura original tiene un formato (id %d) que no se puede '
                       'escribir; indica uno con --format dxt1|dxt3|dxt5|rgba' % o_id)
    if (ident, w, h) != (o_eff, ow, oh):
        warn('original: %dx%d %s -> nuevo: %dx%d %s. Si el juego espera el formato y el '
             'tamano originales, usa los mismos.'
             % (ow, oh, TEX_FMT.get(o_eff, ('id%d' % o_id,))[0], w, h, TEX_FMT[ident][0]))
    raw = ident == ID_RAW32
    if not raw and (w % 4 or h % 4):
        if schunks is not None:
            raise RX3Error('el DDS debe tener ancho y alto multiplos de 4')
        w2, h2 = max(4, w // 4 * 4), max(4, h // 4 * 4)
        warn('tamano no multiplo de 4; se redimensiona a %dx%d' % (w2, h2))
        rgba = resize_rgba(rgba, w, h, w2, h2)
        w, h = w2, h2
    # --- niveles
    if schunks is not None and s_ident == ident and not a.mips:
        chunks = schunks                      # el DDS ya viene en el formato correcto
    else:
        if schunks is not None:               # hay que pasar por RGBA
            if s_ident == 7 and ident != 7:
                warn('el DDS es ATI2 (normal map); se convierte a %s perdiendo calidad'
                     % TEX_FMT[ident][0])
            rgba = decode_tex(schunks[0], w, h, s_ident,
                              a.order if s_ident == ID_RAW32 else 'rgba')
        if ident == 7:
            raise RX3Error('ATI2 solo se puede importar desde un DDS ATI2')
        if ident == 0 and min(rgba[3::4]) < 255:
            warn('la imagen tiene transparencia y el formato es DXT1 (solo alfa de 1 bit); '
                 'usa --format dxt5 o rgba para conservarla')
        if a.mips:
            nmips = a.mips
        elif (w, h) == (ow, oh):
            nmips = omips
        elif raw:
            nmips = 1
        else:
            nmips = 10 if max(w, h) >= 2048 else 3 if max(w, h) >= 512 else 1
        chunks = []
        for i, (mw, mh) in enumerate(mip_dims(w, h, nmips, raw)):
            level = rgba if i == 0 else resize_rgba(rgba, w, h, mw, mh)
            if raw:
                chunks.append(swizzle(level, 'rgba', a.order))
            else:
                chunks.append(encode_dxt(level, mw, mh, ident, progress=(mw * mh >= 512 * 512)))
    rx.set_data(old, build_texture_section(rx, old, ident, w, h, chunks))
    # --- tabla de texturas
    E = rx.E
    for tab in rx.by_hash(H_TEXTAB):
        p = 16 + 16 * a.index
        if p + 16 <= len(tab.data):
            d = bytearray(tab.data)
            struct.pack_into(E + 'I', d, p, old.size)
            d[p + 5] = ident
            struct.pack_into(E + 'H', d, p + 8, w)
            struct.pack_into(E + 'H', d, p + 10, h)
            struct.pack_into(E + 'H', d, p + 14, len(chunks))
            tab.data = bytes(d)
        else:
            warn('tabla de texturas demasiado corta; no se actualizo')
    out = a.out or os.path.splitext(a.file)[0] + '_mod.rx3'
    data = rx.build()
    problems = verify(RX3(data))
    with open(out, 'wb') as f:
        f.write(data)
    print('textura %d reemplazada: %dx%d %s, %d mips -> %s'
          % (a.index, w, h, TEX_FMT[ident][0], len(chunks), out))
    report_problems(problems)


# ------------------------------------------------------------- verificacion
def verify(rx):
    """Lista de problemas de coherencia del RX3 (vacia = todo bien)."""
    E = rx.E
    pr = []
    # secciones: la primera dword de vertices/indices/texturas es su tamano
    for s in rx.secs:
        if s.hash in (H_VERT, H_IDX, H_TEX):
            if len(s.data) < 16:
                pr.append("seccion '%s' demasiado corta" % s.name)
            elif struct.unpack_from(E + 'I', s.data, 0)[0] != len(s.data):
                pr.append("seccion '%s': el tamano de su cabecera (%d) no coincide con el "
                          "real (%d)" % (s.name, struct.unpack_from(E + 'I', s.data, 0)[0],
                                         len(s.data)))
    try:
        meshes = get_meshes(rx)
    except RX3Error as e:
        return pr + [str(e)]
    objs = rx.by_hash(H_OBJ)
    for m in meshes:
        if m.bad_tris:
            pr.append('malla %d: %d triangulos con indices fuera de rango' % (m.k, m.bad_tris))
        if m.isz == 2 and m.vc > 65536:
            pr.append('malla %d: %d vertices no caben en indices de 2 bytes' % (m.k, m.vc))
        for s in objs:
            n = (len(s.data) - 16) // 16
            if m.k < n:
                sz, cnt = struct.unpack_from(E + '2I', s.data, 16 + 16 * m.k)
                if (sz, cnt) != (len(m.si.data), m.icount):
                    pr.append("malla %d: 'objetos' dice %d bytes/%d indices pero el buffer "
                              "tiene %d/%d" % (m.k, sz, cnt, len(m.si.data), m.icount))
                elif s.data[16 + 16 * m.k + 8] != m.isz:
                    pr.append("malla %d: 'objetos' dice indices de %d bytes y el buffer %d"
                              % (m.k, s.data[16 + 16 * m.k + 8], m.isz))
    texs = rx.by_hash(H_TEX)
    for k, s in enumerate(texs):
        try:
            ident, w, h, mips, chunks = tex_parse(rx, s)
        except RX3Error as e:
            pr.append('textura %d: %s' % (k, e))
            continue
        if len(chunks) != mips:
            pr.append('textura %d: declara %d mips pero hay %d' % (k, mips, len(chunks)))
        elif ident in TEX_FMT:
            for c, (mw, mh) in zip(chunks, mip_dims(w, h, mips, ident == ID_RAW32)):
                bpl = mw * mh * 4 if ident == ID_RAW32 else \
                    ((mw + 3) // 4) * ((mh + 3) // 4) * TEX_FMT[ident][1]
                if len(c) != bpl:
                    pr.append('textura %d: un mip de %dx%d mide %d bytes y se esperaban %d'
                              % (k, mw, mh, len(c), bpl))
                    break
        for tab in rx.by_hash(H_TEXTAB):
            p = 16 + 16 * k
            if p + 16 <= len(tab.data):
                tsz = struct.unpack_from(E + 'I', tab.data, p)[0]
                tw, th = struct.unpack_from(E + '2H', tab.data, p + 8)
                if tsz != len(s.data) or (tw, th) != (w, h) or tab.data[p + 5] != ident:
                    pr.append('textura %d: la tabla de texturas no coincide con la textura'
                              % k)
    return pr


def report_problems(pr):
    if not pr:
        print('Verificacion: OK (tamanos, indices y tablas coherentes)')
    else:
        print('Verificacion: PROBLEMAS')
        for p in pr:
            print('  -', p)


def cmd_check(a):
    rx = RX3.open(a.file)
    print('Archivo: %s' % a.file)
    report_problems(verify(rx))


# --------------------------------------------------------------------- info
def cmd_info(a):
    rx = RX3.open(a.file)
    print('Archivo: %s  (%s, %s, %d secciones%s)' % (
        a.file, 'RX3', 'little-endian' if rx.E == '<' else 'BIG-endian',
        len(rx.secs), ', chunkzip descomprimido' if rx.compressed else ''))
    cnt = {}
    for s in rx.secs:
        cnt[s.name] = cnt.get(s.name, 0) + 1
    print('Secciones:', ', '.join('%s x%d' % kv for kv in cnt.items()))
    for m in get_meshes(rx):
        print('\nMalla %d: %d vertices, stride %d, %d triangulos, indices de %d bytes'
              % (m.k, m.vc, m.stride, len(m.tris), m.isz))
        print('  descr:', m.descr_text)
        if not m.has_pos:
            warn('sin posiciones soportadas')
    for k, s in enumerate(rx.by_hash(H_TEX)):
        ident, w, h, mips, chunks = tex_parse(rx, s)
        eff = effective_ident(ident, w, h, chunks)
        nm = TEX_FMT[ident][0] if ident in TEX_FMT else (
            'id%d (se tratara como %s)' % (ident, 'RGBA' if eff == ID_RAW32 else 'gris')
            if eff else 'id%d (DESCONOCIDO)' % ident)
        print('Textura %d: %dx%d %s, %d mips' % (k, w, h, nm, mips))
    print()
    report_problems(verify(rx))


# --------------------------------------------------------------------- menu
def ask(prompt, default=None):
    s = input(prompt + (' [%s]' % default if default else '') + ': ').strip()
    return s or default


def ask_path(prompt, exts=None):
    """Pide una ruta (acepta comillas y ~). Con Enter lista los archivos de la carpeta."""
    while True:
        s = input(prompt + ' (Enter = ver archivos): ').strip().strip('"\'')
        if not s:
            files = sorted(f for e in (exts or ['rx3']) for f in glob.glob('*.' + e))
            print('  ' + ('\n  '.join(files) if files else '(no hay archivos aqui)'))
            continue
        s = os.path.expanduser(s)
        if os.path.exists(s):
            return s
        print('  No existe: %s' % s)


def cmd_menu(_):
    cmds = [('Info / comprobar RX3', 'info'), ('Exportar modelos (OBJ)', 'xm'),
            ('Exportar texturas (DDS+PNG)', 'xt'), ('Importar modelo (OBJ)', 'im'),
            ('Importar textura (DDS/PNG)', 'it'), ('Salir', None)]
    while True:
        print('\n=== rx3tool V%s ===' % __version__)
        for i, (n, _) in enumerate(cmds, 1):
            print(' %d) %s' % (i, n))
        try:
            ch = int(ask('Opcion', '6'))
            code = cmds[ch - 1][1]
        except (ValueError, IndexError):
            continue
        if code is None:
            return
        try:
            f = ask_path('Archivo .rx3')
            ns = argparse.Namespace(file=f, out=None, mesh=None, scale=0.01,
                                    no_weld=False, split=False, no_png=False,
                                    index=None, no_bbox=False, format=None, mips=0,
                                    rebuild=False, force=False, order=RAW_ORDER)
            if code == 'info':
                cmd_info(ns)
            elif code == 'xm':
                cmd_export_model(ns)
            elif code == 'xt':
                cmd_export_tex(ns)
            elif code == 'im':
                ns.obj = ask_path('Archivo .obj', ['obj'])
                cmd_import_model(ns)
            elif code == 'it':
                ns.index = int(ask('Numero de textura', '0'))
                ns.image = ask_path('Imagen (.dds/.png)', ['dds', 'png'])
                cmd_import_tex(ns)
        except (RX3Error, OSError, ValueError, struct.error) as e:
            print('ERROR:', e)


def main(argv=None):
    ap = argparse.ArgumentParser(description='Herramienta RX3 para Termux (V%s)' % __version__)
    sub = ap.add_subparsers(dest='cmd')
    p = sub.add_parser('info'); p.add_argument('file'); p.set_defaults(fn=cmd_info)
    p = sub.add_parser('check', help='comprueba la coherencia del RX3')
    p.add_argument('file'); p.set_defaults(fn=cmd_check)
    p = sub.add_parser('export-model'); p.add_argument('file')
    p.add_argument('-o', '--out', help='carpeta de salida')
    p.add_argument('--mesh', type=int, help='exportar solo esta malla')
    p.add_argument('--scale', type=float, default=0.01, help='escala (defecto 0.01: cm->m)')
    p.add_argument('--no-weld', action='store_true', help='no soldar vertices duplicados')
    p.add_argument('--split', action='store_true', help='un OBJ por malla')
    p.set_defaults(fn=cmd_export_model)
    p = sub.add_parser('import-model'); p.add_argument('file'); p.add_argument('obj')
    p.add_argument('-o', '--out', help='RX3 de salida (defecto: <nombre>_mod.rx3)')
    p.add_argument('--mesh', type=int, help='malla destino (si el OBJ no usa mesh_000...)')
    p.add_argument('--scale', type=float, default=0.01, help='debe coincidir con la exportacion')
    p.add_argument('--no-bbox', action='store_true', help='no tocar bounding boxes')
    p.add_argument('--rebuild', action='store_true',
                   help='forzar la reconstruccion de buffers (por defecto solo si cambia la topologia)')
    p.add_argument('--force', action='store_true', help='ignorar el aviso de escala distinta')
    p.set_defaults(fn=cmd_import_model)
    p = sub.add_parser('export-tex'); p.add_argument('file')
    p.add_argument('-o', '--out'); p.add_argument('--index', type=int)
    p.add_argument('--no-png', action='store_true')
    p.add_argument('--order', default=RAW_ORDER, choices=['rgba', 'bgra', 'argb', 'abgr'],
                   help='orden de bytes de las texturas de 32 bits (defecto rgba)')
    p.set_defaults(fn=cmd_export_tex)
    p = sub.add_parser('import-tex'); p.add_argument('file')
    p.add_argument('index', type=int); p.add_argument('image')
    p.add_argument('-o', '--out')
    p.add_argument('--format', choices=sorted(FORMAT_CHOICES))
    p.add_argument('--mips', type=int, default=0)
    p.add_argument('--order', default=RAW_ORDER, choices=['rgba', 'bgra', 'argb', 'abgr'])
    p.set_defaults(fn=cmd_import_tex)
    p = sub.add_parser('menu'); p.set_defaults(fn=cmd_menu)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        a.fn(a)
    except (RX3Error, OSError, struct.error) as e:
        print('ERROR:', e)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
