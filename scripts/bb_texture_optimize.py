#!/usr/bin/env python3
"""Audit and repack static per-face cube textures without changing model geometry."""
import argparse
import base64
import copy
import hashlib
import io
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image


class OptimizationError(ValueError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def load_texture(texture, parent):
    source = texture.get('source', '')
    if source.startswith('data:image/'):
        try:
            payload = base64.b64decode(source.split(',', 1)[1], validate=True)
        except Exception as exc:
            raise OptimizationError('Invalid embedded texture image') from exc
        return Image.open(io.BytesIO(payload)).convert('RGBA')
    for key in ('relative_path', 'path'):
        value = texture.get(key)
        if value and '://' not in value:
            path = Path(value)
            if not path.is_absolute():
                path = parent / path
            if path.is_file():
                return Image.open(path).convert('RGBA')
    raise OptimizationError('Texture requires an embedded image or an existing local PNG')


def face_lengths(element, direction, rotation):
    dimensions = [abs(float(b) - float(a)) + 2 * float(element.get('inflate', 0))
                  for a, b in zip(element['from'], element['to'])]
    axes = {'north': (0, 1), 'south': (0, 1), 'east': (2, 1),
            'west': (2, 1), 'up': (0, 2), 'down': (0, 2)}
    if direction not in axes:
        raise OptimizationError('Unknown cube face direction: ' + direction)
    i, j = axes[direction]
    values = (dimensions[i], dimensions[j])
    return values[::-1] if rotation % 180 else values


class Asset:
    def __init__(self, path, uv_override=None):
        self.path = Path(path).resolve()
        self.raw = self.path.read_bytes()
        self.model = json.loads(self.raw.decode('utf-8-sig'))
        self.images = [load_texture(t, self.path.parent) for t in self.model.get('textures', [])]
        if not self.images:
            raise OptimizationError('Model has no readable textures')
        self.uv_sizes = []
        per_texture = self.model.get('meta', {}).get('model_format') in {'free', 'obj'}
        resolution = self.model.get('resolution', {})
        for t, image in zip(self.model['textures'], self.images):
            if uv_override:
                size = list(uv_override)
            elif per_texture:
                size = [t.get('uv_width', resolution.get('width', image.width)),
                        t.get('uv_height', resolution.get('height', image.height))]
            else:
                size = [resolution.get('width', t.get('uv_width', image.width)),
                        resolution.get('height', t.get('uv_height', image.height))]
            if any(not isinstance(v, (float, int)) or not math.isfinite(v) or v <= 0 for v in size):
                raise OptimizationError('UV logical dimensions must be positive finite numbers')
            self.uv_sizes.append(size)
        self.faces = []
        self.elements = {}
        for index, element in enumerate(self.model.get('elements', [])):
            if element.get('type', 'cube') != 'cube':
                raise OptimizationError('Helper supports cube models only; use native mesh UV tools')
            uid = element.get('uuid')
            if not uid or uid in self.elements:
                raise OptimizationError('Element UUIDs must be present and unique')
            self.elements[uid] = element
            for direction, face in element.get('faces', {}).items():
                reference = face.get('texture')
                if reference is None or reference is False:
                    continue
                if isinstance(reference, int) and not isinstance(reference, bool):
                    texture_index = reference
                else:
                    found = [i for i, t in enumerate(self.model['textures'])
                             if reference in (t.get('uuid'), t.get('id'))]
                    if len(found) != 1:
                        raise OptimizationError('Unknown or ambiguous face texture reference')
                    texture_index = found[0]
                if not 0 <= texture_index < len(self.images):
                    raise OptimizationError('Unknown face texture index')
                uv = face.get('uv')
                if not isinstance(uv, list) or len(uv) != 4 or any(not isinstance(v, (float, int)) or not math.isfinite(v) for v in uv):
                    raise OptimizationError('A cube face needs four finite UV coordinates')
                image = self.images[texture_index]
                uw, uh = self.uv_sizes[texture_index]
                pixels = [uv[0] * image.width / uw, uv[1] * image.height / uh,
                          uv[2] * image.width / uw, uv[3] * image.height / uh]
                xmin, xmax = sorted((pixels[0], pixels[2]))
                ymin, ymax = sorted((pixels[1], pixels[3]))
                if xmin < -1e-6 or ymin < -1e-6 or xmax > image.width + 1e-6 or ymax > image.height + 1e-6:
                    raise OptimizationError('UV wrapping or out-of-image coordinates require native handling')
                if xmax - xmin <= 1e-9 or ymax - ymin <= 1e-9:
                    raise OptimizationError('Collapsed active UV rectangle')
                box = (max(0, math.floor(xmin + 1e-9)), max(0, math.floor(ymin + 1e-9)),
                       min(image.width, math.ceil(xmax - 1e-9)), min(image.height, math.ceil(ymax - 1e-9)))
                crop = image.crop(box)
                rotation = face.get('rotation', 0)
                if rotation not in (0, 90, 180, 270):
                    raise OptimizationError('Unsupported face UV rotation')
                lengths = face_lengths(element, direction, rotation)
                self.faces.append({'id': uid + ':' + direction, 'index': index, 'direction': direction,
                                   'element_name': element.get('name', uid), 'texture': texture_index,
                                   'pixels': pixels, 'box': box, 'crop': crop, 'lengths': lengths,
                                   'key': (crop.width, crop.height, digest(crop.tobytes()))})
        if not self.faces:
            raise OptimizationError('Model has no active textured faces')

    def ensure_supported_optimization(self):
        default_box = self.model.get('meta', {}).get('box_uv', False)
        if any(e.get('box_uv', default_box) for e in self.model['elements']):
            raise OptimizationError('Box UV must first be converted and verified in a model copy')
        textures = self.model['textures']
        signatures = set()
        for texture in textures:
            if texture.get('layers_enabled'):
                raise OptimizationError('Flatten texture layers in a copy before using this helper')
            if texture.get('pbr_channel', 'color') != 'color' or texture.get('group'):
                raise OptimizationError('PBR or linked texture groups need coordinated native processing')
            if texture.get('frame_count', texture.get('frameCount', 1)) > 1 or texture.get('frame_order') or texture.get('frame_interpolate'):
                raise OptimizationError('Animated texture frames require a frame-aware native workflow')
            signatures.add((texture.get('render_mode', 'default'), texture.get('render_sides', 'auto'),
                            texture.get('force_translucent', False), texture.get('wrap_mode', 'limited')))
        if len(signatures) != 1:
            raise OptimizationError('Textures with different render or sampler settings cannot be combined')
        if self.model.get('texture_groups'):
            raise OptimizationError('PBR texture groups are outside this helper scope')

    def audit(self):
        try:
            self.ensure_supported_optimization()
            support = {'optimization_supported': True, 'reason': None}
        except OptimizationError as exc:
            support = {'optimization_supported': False, 'reason': str(exc)}
        default_box = self.model.get('meta', {}).get('box_uv', False)
        box_count = sum(bool(e.get('box_uv', default_box)) for e in self.model['elements'])
        unique = {r['key'] for r in self.faces}
        face_info = []
        for r in self.faces:
            x1, y1, x2, y2 = r['pixels']; lu, lv = r['lengths']
            face_info.append({'id': r['id'], 'element_name': r['element_name'], 'texture_index': r['texture'],
                              'pixel_box': list(r['box']), 'sampled_pixel_span': [abs(x2-x1), abs(y2-y1)],
                              'surface_lengths': [lu, lv],
                              'pixels_per_model_unit': [abs(x2-x1)/lu if lu > 0 else None,
                                                        abs(y2-y1)/lv if lv > 0 else None]})
        return {'source_sha256': digest(self.raw), 'format': self.model.get('meta', {}).get('model_format'),
                'elements': len(self.model['elements']), 'animations': len(self.model.get('animations', [])),
                'active_faces': len(self.faces), 'exact_unique_patches': len(unique),
                'exact_unique_pixels': sum(w*h for w, h, _ in unique),
                'support': support, 'uv_modes': {'box_uv_cubes': box_count, 'per_face_cubes': len(self.model['elements'])-box_count},
                'textures': [{'name': t.get('name'), 'bitmap_size': list(im.size), 'uv_size': list(size),
                              'pbr_channel': t.get('pbr_channel', 'color'), 'layers_enabled': bool(t.get('layers_enabled')),
                              'texture_animation_metadata': {k:t.get(k) for k in ('frame_count','frameCount','frame_order','frame_interpolate') if k in t}}
                             for t, im, size in zip(self.model['textures'], self.images, self.uv_sizes)],
                'faces': face_info}


class Packer:
    def __init__(self, size):
        self.free = [(0, 0, size, size)]

    def add(self, width, height):
        candidates = [(min(w-width, h-height), max(w-width, h-height), y, x)
                      for x, y, w, h in self.free if width <= w and height <= h]
        if not candidates:
            return None
        _, _, y, x = min(candidates)
        updated = []
        for fx, fy, fw, fh in self.free:
            if x >= fx+fw or x+width <= fx or y >= fy+fh or y+height <= fy:
                updated.append((fx, fy, fw, fh))
                continue
            if x > fx: updated.append((fx, fy, x-fx, fh))
            if x+width < fx+fw: updated.append((x+width, fy, fx+fw-x-width, fh))
            if y > fy: updated.append((fx, fy, fw, y-fy))
            if y+height < fy+fh: updated.append((fx, y+height, fw, fy+fh-y-height))
        updated = sorted(set(r for r in updated if r[2] > 0 and r[3] > 0))
        self.free = [a for j, a in enumerate(updated) if not any(j != k and b[0] <= a[0] and b[1] <= a[1]
                      and b[0]+b[2] >= a[0]+a[2] and b[1]+b[3] >= a[1]+a[3] for k, b in enumerate(updated))]
        return x, y


def pack_tiles(tiles, size, padding):
    area = sum((t['image'].width+2*padding)*(t['image'].height+2*padding) for t in tiles)
    if area > size*size:
        return None
    orders = [lambda t: (t['image'].width*t['image'].height, max(t['image'].size)),
              lambda t: (max(t['image'].size), t['image'].width*t['image'].height),
              lambda t: (t['image'].height, t['image'].width),
              lambda t: (sum(t['image'].size), t['image'].width*t['image'].height)]
    for order in orders:
        packer = Packer(size)
        positions = {}
        for tile in sorted(tiles, key=order, reverse=True):
            location = packer.add(tile['image'].width+2*padding, tile['image'].height+2*padding)
            if location is None:
                break
            positions[tile['key']] = (location[0]+padding, location[1]+padding)
        if len(positions) == len(tiles):
            return positions
    return None


def config_assignments(asset, config_path):
    if not config_path:
        raise OptimizationError('Shared mode requires an explicit material-group config')
    path = Path(config_path).resolve()
    config = read_json(path)
    definitions = config.get('groups', {})
    known_faces = {r['id'] for r in asset.faces}
    assigned = {}
    for uid, group in config.get('elements', {}).items():
        if uid not in asset.elements:
            raise OptimizationError('Unknown element UUID in config: ' + uid)
        for r in asset.faces:
            if r['id'].startswith(uid + ':'): assigned[r['id']] = group
    for face_id, group in config.get('faces', {}).items():
        if face_id not in known_faces:
            raise OptimizationError('Unknown active face in config: ' + face_id)
        assigned[face_id] = group
    for name in assigned.values():
        if name not in definitions:
            raise OptimizationError('Unknown material group: ' + str(name))
    for name, definition in definitions.items():
        if definition.get('strategy', 'exact') not in {'exact', 'field', 'patch'}:
            raise OptimizationError('Unknown group strategy: ' + name)
        if int(definition.get('variants', 1)) < 1:
            raise OptimizationError('Material variants must be positive')
        if float(definition.get('scale', 1)) <= 0:
            raise OptimizationError('Patch scale must be positive')
    return definitions, assigned, path.parent


def representative(records, target_size):
    candidates = {}
    for record in records:
        image = record['crop'].resize(target_size, Image.Resampling.NEAREST)
        candidates.setdefault(digest(image.tobytes()), image)
    values = list(candidates.values())
    average = np.mean([np.asarray(im, dtype=np.float32) for im in values], axis=0)
    return min(values, key=lambda im: float(np.mean((np.asarray(im, dtype=np.float32)-average)**2)))


def build_tiles(asset, definitions, assignments, config_parent, density):
    buckets = defaultdict(list)
    for record in asset.faces:
        group = assignments.get(record['id'])
        definition = definitions.get(group, {})
        strategy = definition.get('strategy', 'exact')
        if strategy == 'exact': key = ('exact',) + record['key']
        elif strategy == 'patch':
            if definition.get('source'): key = ('patch', group, 'external')
            else: key = ('patch', group, record['crop'].width, record['crop'].height)
        else:
            variants = int(definition.get('variants', 1))
            material_seed = digest((str(record['texture']) + str(record['pixels']) + group).encode())
            key = ('field', group, int(material_seed[:8], 16) % variants)
        buckets[key].append(record)
    tiles = []
    for key, records in buckets.items():
        strategy = key[0]
        definition = definitions.get(key[1], {}) if strategy != 'exact' else {}
        external = definition.get('source')
        if external:
            source = Path(external)
            if not source.is_absolute(): source = config_parent / source
            if not source.is_file(): raise OptimizationError('Group source PNG does not exist: ' + str(source))
            source_image = Image.open(source).convert('RGBA')
        else:
            source_image = None
        tile = {'key': key, 'records': records, 'strategy': strategy}
        if strategy == 'exact':
            tile['image'] = records[0]['crop']
        elif strategy == 'patch':
            if source_image is not None: tile['image'] = source_image
            else:
                factor = float(definition.get('scale', 1))
                size = (max(1, round(records[0]['crop'].width*factor)), max(1, round(records[0]['crop'].height*factor)))
                tile['image'] = representative(records, size)
            tile['external'] = source_image is not None
        else:
            d = float(definition.get('density', density))
            if d <= 0 or not math.isfinite(d): raise OptimizationError('Material density must be positive and finite')
            if any(min(r['lengths']) <= 0 for r in records): raise OptimizationError('Field mapping cannot target a zero-area face')
            width = max(1, math.ceil(max(r['lengths'][0] for r in records)*d))
            height = max(1, math.ceil(max(r['lengths'][1] for r in records)*d))
            # A group has the same tile dimensions across variants to keep density stable.
            siblings = [r for r in asset.faces if assignments.get(r['id']) == key[1]]
            width = max(1, math.ceil(max(r['lengths'][0] for r in siblings)*d))
            height = max(1, math.ceil(max(r['lengths'][1] for r in siblings)*d))
            ordered = sorted(siblings, key=lambda r: (-r['crop'].width*r['crop'].height, r['id']))
            distinct = list({r['key']: r for r in reversed(ordered)}.values())
            distinct.sort(key=lambda r: (-r['crop'].width*r['crop'].height, r['id']))
            exemplar = distinct[min(key[2], len(distinct)-1)]
            if source_image is None:
                source_image = exemplar['crop']
                pu = abs(exemplar['pixels'][2]-exemplar['pixels'][0])/exemplar['lengths'][0]
                pv = abs(exemplar['pixels'][3]-exemplar['pixels'][1])/exemplar['lengths'][1]
            else:
                supplied = definition.get('source_density')
                if not supplied or len(supplied) != 2 or min(supplied) <= 0:
                    raise OptimizationError('External field PNG needs source_density [x,y]')
                pu, pv = supplied
            array = np.asarray(source_image)
            trim = max(0, int(definition.get('trim_pixels', 1)))
            if trim and min(source_image.size) > 2*trim+2: array = array[trim:-trim, trim:-trim]
            xs = np.floor(np.arange(width)*pu/d).astype(int) % array.shape[1]
            ys = np.floor(np.arange(height)*pv/d).astype(int) % array.shape[0]
            tile['image'] = Image.fromarray(array[ys[:, None], xs[None, :]])
            tile['density'] = d
        tiles.append(tile)
    return tiles


def compare_non_uv(source, result):
    a, b = copy.deepcopy(source), copy.deepcopy(result)
    for obj in (a, b):
        obj.pop('name', None); obj.pop('resolution', None); obj.pop('textures', None)
        for element in obj['elements']:
            for face in element.get('faces', {}).values():
                face.pop('uv', None); face.pop('texture', None)
    return a == b


def optimize(asset, out, size, mode='exact', config=None, density='auto', padding=1):
    asset.ensure_supported_optimization()
    if size < 1 or padding < 0:
        raise OptimizationError('Size must be positive and padding must be nonnegative')
    out = Path(out).resolve()
    model_path = out / (asset.path.stem + '_optimized_' + str(size) + '.bbmodel')
    texture_path = out / ('atlas_' + str(size) + '.png')
    reports = [out/'optimization_report.json', out/'atlas_regions.json']
    if model_path == asset.path or any(p.exists() for p in [model_path, texture_path, *reports]):
        raise OptimizationError('Output already exists; choose a new output directory')
    if mode == 'shared': definitions, assignments, parent = config_assignments(asset, config)
    else: definitions, assignments, parent = {}, {}, asset.path.parent
    if density == 'auto':
        source_densities = [abs(r['pixels'][2]-r['pixels'][0])/r['lengths'][0] for r in asset.faces if r['lengths'][0] > 0]
        start = float(np.median(source_densities))
        candidates = [start*f for f in [1,.875,.75,.625,.5,.4375,.375,.35,.325,.3125,.3,.275,.25,.225,.2,.175,.15,.125,.1,.075,.0625]]
    else:
        candidates = [float(density)]
    attempts = []
    if mode == 'exact': candidates = [1]
    positions = None
    for d in candidates:
        tiles = build_tiles(asset, definitions, assignments, parent, d)
        area = sum((t['image'].width+2*padding)*(t['image'].height+2*padding) for t in tiles)
        positions = pack_tiles(tiles, size, padding)
        attempts.append({'density': round(d, 6), 'padded_pixels': area, 'packed': positions is not None})
        if positions is not None: break
    if positions is None:
        raise OptimizationError('Atlas capacity insufficient: preserve critical patches, choose a larger size or explicitly revise material groups')
    result = copy.deepcopy(asset.model)
    atlas = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    manifest = []
    face_regions = {}
    for number, tile in enumerate(tiles):
        x, y = positions[tile['key']]
        patch = tile['image']; w, h = patch.size
        if padding:
            padded = np.pad(np.asarray(patch), ((padding,padding),(padding,padding),(0,0)), mode='edge')
            atlas.paste(Image.fromarray(padded), (x-padding,y-padding))
        else: atlas.paste(patch, (x,y))
        rect = [x, y, x+w, y+h]
        manifest.append({'region': number, 'strategy': tile['strategy'], 'group': tile['key'][1] if tile['strategy'] != 'exact' else None,
                         'rect': rect, 'faces': [r['id'] for r in tile['records']]})
        for record in tile['records']:
            old = record['pixels']
            if tile['strategy'] == 'field':
                du = record['lengths'][0]*tile['density']; dv = record['lengths'][1]*tile['density']
                seed = int(digest(record['id'].encode())[:16],16)
                ox = (seed%997)/996*max(0,w-du); oy = ((seed//997)%991)/990*max(0,h-dv)
                new_uv = [x+ox, y+oy, x+ox+du, y+oy+dv]
                if old[0] > old[2]: new_uv[0],new_uv[2] = new_uv[2],new_uv[0]
                if old[1] > old[3]: new_uv[1],new_uv[3] = new_uv[3],new_uv[1]
            elif tile['strategy'] == 'patch' and tile.get('external'):
                new_uv = [x,y,x+w,y+h]
                if old[0] > old[2]: new_uv[0],new_uv[2] = new_uv[2],new_uv[0]
                if old[1] > old[3]: new_uv[1],new_uv[3] = new_uv[3],new_uv[1]
            else:
                bx,by,ex,ey = record['box']; fx=w/(ex-bx);fy=h/(ey-by)
                new_uv = [x+(old[0]-bx)*fx,y+(old[1]-by)*fy,x+(old[2]-bx)*fx,y+(old[3]-by)*fy]
            face = result['elements'][record['index']]['faces'][record['direction']]
            face['uv'] = [round(v, 8) for v in new_uv]; face['texture'] = 0
            face_regions[record['id']] = rect
    buf = io.BytesIO(); atlas.save(buf,format='PNG',optimize=True); png = buf.getvalue()
    texture = copy.deepcopy(asset.model['textures'][0])
    texture.update(name='atlas_'+str(size), path=texture_path.as_posix(), relative_path=texture_path.name,
                   width=size,height=size,uv_width=size,uv_height=size,
                   source='data:image/png;base64,'+base64.b64encode(png).decode('ascii'),internal=True,saved=True)
    result['textures'] = [texture];result['resolution'] = {'width':size,'height':size};result['name'] = model_path.stem
    valid = compare_non_uv(asset.model,result)
    for record in asset.faces:
        face = result['elements'][record['index']]['faces'][record['direction']]
        a,b,c,e = face['uv']; x,y,x2,y2 = face_regions[record['id']]
        valid = valid and x-1e-6 <= min(a,c) < max(a,c) <= x2+1e-6 and y-1e-6 <= min(b,e) < max(b,e) <= y2+1e-6
    if not valid: raise OptimizationError('Internal geometry or UV verification failed')
    # Packing is checked independently before writing any asset.
    for i,r in enumerate(manifest):
        ax,ay,bx,by=r['rect']
        for q in manifest[:i]:
            cx,cy,dx,dy=q['rect']
            if not (bx+padding<=cx-padding or dx+padding<=ax-padding or by+padding<=cy-padding or dy+padding<=ay-padding):
                raise OptimizationError('Packed regions overlap')
    if asset.path.read_bytes() != asset.raw: raise OptimizationError('Source model changed during optimization')
    report = {'mode':mode,'original_texture_sizes':[list(im.size) for im in asset.images], 'optimized_texture_size':[size,size],
              'active_faces':len(asset.faces),'exact_unique_patches':len({r['key'] for r in asset.faces}),
              'atlas_regions':len(manifest),'selected_density':round(d,6) if mode=='shared' else None,
              'padding':padding,'attempts':attempts,'source_sha256':digest(asset.raw),'png_bytes':len(png),
              'verification':{'source_unchanged':True,'non_uv_content_unchanged':True,'all_uvs_inside_regions':True,'padded_regions_nonoverlapping':True},
              'visual_validation':'pending native or clearly labeled software comparison',
              'limitations': ['Field and patch sharing can simplify original per-face noise and painted edges.'] if mode=='shared' else ['Exact mode preserves base-level texel samples; filtering and mipmaps still need runtime checks.']}
    out.mkdir(parents=True, exist_ok=True)
    texture_path.write_bytes(png)
    model_path.write_text(json.dumps(result,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
    (out/'atlas_regions.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    (out/'optimization_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    written = read_json(model_path)
    if not compare_non_uv(asset.model,written) or base64.b64decode(written['textures'][0]['source'].split(',',1)[1]) != texture_path.read_bytes():
        raise OptimizationError('Written deliverables failed round-trip validation')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['audit','optimize'])
    parser.add_argument('model',type=Path)
    parser.add_argument('--out',required=True,type=Path)
    parser.add_argument('--size',type=int,default=128)
    parser.add_argument('--mode',choices=['exact','shared'],default='exact')
    parser.add_argument('--config',type=Path)
    parser.add_argument('--density',default='auto',help='Field pixels per model unit, or auto; auto is a packing proposal, not a visual-quality guarantee')
    parser.add_argument('--padding',type=int,default=1)
    parser.add_argument('--uv-size',nargs=2,type=float,metavar=('WIDTH','HEIGHT'),help='Explicit logical UV dimensions for custom formats')
    args = parser.parse_args()
    try:
        asset = Asset(args.model,args.uv_size)
        if args.command == 'audit':
            if any((args.out/name).exists() for name in ('audit.json','face_assignments.json')):
                raise OptimizationError('Audit output already exists; choose a new output directory')
            args.out.mkdir(parents=True,exist_ok=True)
            report = asset.audit()
            (args.out/'audit.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            template = {'groups':{},'elements':{},'faces':{}}
            template['groups']['protected'] = {'strategy':'exact'}
            (args.out/'face_assignments.json').write_text(json.dumps(template,indent=2),encoding='utf-8')
            print(json.dumps({k:v for k,v in report.items() if k!='faces'},ensure_ascii=False,indent=2))
        else:
            print(json.dumps(optimize(asset,args.out,args.size,args.mode,args.config,args.density,args.padding),ensure_ascii=False,indent=2))
    except (OptimizationError,OSError,ValueError,KeyError) as exc:
        print('ERROR: '+str(exc),file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
