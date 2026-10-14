#!/usr/bin/env python3
"""Static software comparisons; does not emulate native Blockbench or game materials."""
import argparse,json,math
from pathlib import Path
import numpy as np
from PIL import Image,ImageDraw
from bb_texture_optimize import Asset,OptimizationError,compare_non_uv
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('original',type=Path);parser.add_argument('optimized',type=Path);parser.add_argument('--out',required=True,type=Path)
args=parser.parse_args()
old_asset=Asset(args.original);new_asset=Asset(args.optimized)
old_asset.ensure_supported_optimization();new_asset.ensure_supported_optimization()
if any(r['crop'].getextrema()[3][0]<255 for asset in (old_asset,new_asset) for r in asset.faces):raise OptimizationError('Software preview does not support transparent UV regions; use native preview')
if len(old_asset.images)!=1 or len(new_asset.images)!=1:raise OptimizationError('Software preview supports one color texture per model')
if not compare_non_uv(old_asset.model,new_asset.model):raise OptimizationError('Software comparison needs unchanged geometry and animation data')
if any(e.get('rescale') for e in old_asset.model['elements']):raise OptimizationError('Rescaled cubes need native preview')
original=old_asset.model;optimized=new_asset.model;OUT=args.out
OUT.mkdir(parents=True,exist_ok=True)
old_by_id={r['id']:r for r in old_asset.faces};new_by_id={r['id']:r for r in new_asset.faces}
if old_by_id.keys()!=new_by_id.keys():raise OptimizationError('Both models must have the same active faces')
# Orthographic software renderer. Both versions use the same visible-face and geometry buffers.
def rotate_matrix(degrees):
    ax,ay,az=np.deg2rad(degrees)
    rx=np.array([[1,0,0],[0,math.cos(ax),-math.sin(ax)],[0,math.sin(ax),math.cos(ax)]])
    ry=np.array([[math.cos(ay),0,math.sin(ay)],[0,1,0],[-math.sin(ay),0,math.cos(ay)]])
    rz=np.array([[math.cos(az),-math.sin(az),0],[math.sin(az),math.cos(az),0],[0,0,1]])
    return rz@ry@rx

def around(origin,degrees):
    rot=rotate_matrix(degrees); a=np.eye(4); a[:3,:3]=rot; o=np.array(origin); a[:3,3]=o-rot@o; return a

parents={}; group_by_id={g['uuid']:g for g in original.get('groups',[])}
def visit(items,parent=None):
    for item in items:
        if isinstance(item,str): parents[item]=parent
        else:
            parents[item['uuid']]=parent; group_by_id.setdefault(item['uuid'],item); visit(item.get('children',[]),item['uuid'])
visit(original['outliner'])
group_matrices={}
def gmatrix(gid):
    if gid is None: return np.eye(4)
    if gid not in group_matrices:
        g=group_by_id[gid]
        group_matrices[gid]=gmatrix(parents.get(gid))@around(g.get('origin',[0,0,0]),g.get('rotation',[0,0,0]))
    return group_matrices[gid]
face_defs={'north':[(0,1,0),(1,1,0),(1,0,0),(0,0,0)],'south':[(1,1,1),(0,1,1),(0,0,1),(1,0,1)],'east':[(1,1,0),(1,1,1),(1,0,1),(1,0,0)],'west':[(0,1,1),(0,1,0),(0,0,0),(0,0,1)],'up':[(0,1,1),(1,1,1),(1,1,0),(0,1,0)],'down':[(0,0,0),(1,0,0),(1,0,1),(0,0,1)]}
geometry=[]; face_sources=[]
for i,e in enumerate(original['elements']):
    transform=gmatrix(parents.get(e['uuid']))@around(e.get('origin',[0,0,0]),e.get('rotation',[0,0,0]))
    lo=np.array(e['from'])-float(e.get('inflate',0)); hi=np.array(e['to'])+float(e.get('inflate',0))
    for direction,f in e['faces'].items():
        if f.get('texture') is None or f.get('texture') is False: continue
        pts=np.array([lo+(hi-lo)*np.array(c) for c in face_defs[direction]])
        pts=(np.c_[pts,np.ones(4)]@transform.T)[:,:3]
        norm=np.cross(pts[1]-pts[0],pts[2]-pts[0]); length=np.linalg.norm(norm)
        if length<1e-10: continue
        geometry.append((pts,norm/length)); face_sources.append((i,direction))

old_uv=np.array([old_by_id[original['elements'][i]['uuid']+':'+d]['pixels'] for i,d in face_sources],dtype=np.float32)
new_uv=np.array([new_by_id[optimized['elements'][i]['uuid']+':'+d]['pixels'] for i,d in face_sources],dtype=np.float32)
rotations=np.array([original['elements'][i]['faces'][d].get('rotation',0) for i,d in face_sources])
light=np.array([-.4,.9,-.7]);light/=np.linalg.norm(light)


def render_pair(name,view,width=1000,height=760):
    view=np.array(view,dtype=float);view/=np.linalg.norm(view)
    right=np.cross([0,1,0],view);right/=np.linalg.norm(right);up=np.cross(view,right)
    allpts=np.concatenate([q[0] for q in geometry]); proj=np.c_[allpts@right,allpts@up]
    pmin=proj.min(axis=0);pmax=proj.max(axis=0); scale_view=min((width-90)/(pmax[0]-pmin[0]),(height-110)/(pmax[1]-pmin[1]))
    center=(pmin+pmax)/2
    depth=np.full((height,width),-np.inf,dtype=np.float32)
    ids=np.full((height,width),-1,dtype=np.int32); us=np.zeros((height,width),dtype=np.float32);vs=np.zeros_like(us)
    coords=np.array([[0,0],[1,0],[1,1],[0,1]],dtype=float)
    for fi,(pts,norm) in enumerate(geometry):
        if norm@view<=0: continue
        xy=np.c_[(pts@right-center[0])*scale_view+width/2,-(pts@up-center[1])*scale_view+height/2+18]
        zs=pts@view
        for indices in [(0,1,2),(0,2,3)]:
            pp=xy[list(indices)]; zz=zs[list(indices)]; uv=coords[list(indices)]
            xmin=max(0,int(math.floor(pp[:,0].min())));xmax=min(width-1,int(math.ceil(pp[:,0].max())))
            ymin=max(0,int(math.floor(pp[:,1].min())));ymax=min(height-1,int(math.ceil(pp[:,1].max())))
            if xmax<xmin or ymax<ymin:continue
            den=(pp[1,1]-pp[2,1])*(pp[0,0]-pp[2,0])+(pp[2,0]-pp[1,0])*(pp[0,1]-pp[2,1])
            if abs(den)<1e-8:continue
            yy,xx=np.mgrid[ymin:ymax+1,xmin:xmax+1];xx=xx+.5;yy=yy+.5
            a=((pp[1,1]-pp[2,1])*(xx-pp[2,0])+(pp[2,0]-pp[1,0])*(yy-pp[2,1]))/den
            b=((pp[2,1]-pp[0,1])*(xx-pp[2,0])+(pp[0,0]-pp[2,0])*(yy-pp[2,1]))/den;c=1-a-b
            z=a*zz[0]+b*zz[1]+c*zz[2]
            target=depth[ymin:ymax+1,xmin:xmax+1]; mask=(a>=-1e-6)&(b>=-1e-6)&(c>=-1e-6)&(z>=target)
            if not mask.any():continue
            target[mask]=z[mask];ids[ymin:ymax+1,xmin:xmax+1][mask]=fi
            us[ymin:ymax+1,xmin:xmax+1][mask]=(a*uv[0,0]+b*uv[1,0]+c*uv[2,0])[mask]
            vs[ymin:ymax+1,xmin:xmax+1][mask]=(a*uv[0,1]+b*uv[1,1]+c*uv[2,1])[mask]
    visible=ids>=0; fids=ids[visible]; u=us[visible].copy();v=vs[visible].copy()
    for angle in (90,180,270):
        mask=rotations[fids]==angle
        if angle==90:u[mask],v[mask]=v[mask].copy(),1-u[mask].copy()
        elif angle==180:u[mask],v[mask]=1-u[mask],1-v[mask]
        else:u[mask],v[mask]=1-v[mask].copy(),u[mask].copy()
    shading=np.array([.70+.30*max(0,n@light) for p,n in geometry])[fids]
    def color_pass(bitmap,uvs):
        ar=np.asarray(bitmap); rect=uvs[fids]
        xx=rect[:,0]+u*(rect[:,2]-rect[:,0]); yy=rect[:,1]+v*(rect[:,3]-rect[:,1])
        xx=np.clip(np.floor(xx).astype(int),0,ar.shape[1]-1);yy=np.clip(np.floor(yy).astype(int),0,ar.shape[0]-1)
        rgb=(ar[yy,xx,:3].astype(float)*shading[:,None]).astype(np.uint8)
        out=np.empty((height,width,3),dtype=np.uint8);out[:]=[34,39,43];out[visible]=rgb
        return Image.fromarray(out),rgb
    before,brgb=color_pass(old_asset.images[0],old_uv);after,argb=color_pass(new_asset.images[0],new_uv)
    before.save(OUT/(name+'_original.png'));after.save(OUT/(name+'_optimized.png'))
    sheet=Image.new('RGB',(width*2,height+40),(25,29,33));sheet.paste(before,(0,40));sheet.paste(after,(width,40));d=ImageDraw.Draw(sheet)
    d.text((24,13),'ORIGINAL '+str(old_asset.images[0].width)+' x '+str(old_asset.images[0].height),fill=(226,230,233));d.text((width+24,13),'OPTIMIZED '+str(new_asset.images[0].width)+' x '+str(new_asset.images[0].height),fill=(226,230,233))
    sheet.save(OUT/(name+'_comparison.png'))
    return {'view':name,'visible_pixels':int(visible.sum()),'mean_absolute_rgb_difference':round(float(np.mean(np.abs(brgb.astype(float)-argb.astype(float)))),3)}

metrics=[]
for name,view in [('perspective',[1,.65,-1.15]),('side',[1,.20,-.08]),('front',[0,.16,-1])]:
    metrics.append(render_pair(name,view))
    print('Rendered '+name,flush=True)


(OUT/'comparison_report.json').write_text(json.dumps({'renderer':'software orthographic, simplified lighting','native_validation':False,'views':metrics},indent=2),encoding='utf-8')
