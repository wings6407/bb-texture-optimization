import base64,copy,io,json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from PIL import Image
REPO=Path(__file__).resolve().parents[1]
SCRIPT=REPO/'scripts'/'bb_texture_optimize.py'

class TextureOptimizationTests(unittest.TestCase):
    def setUp(self):
        base=os.environ.get('BB_TEST_TMP')
        if base:Path(base).mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(prefix='bb-texture-test-',dir=base)
        self.root=Path(self.temp.name)
        self.image=Image.new('RGBA',(20,12),(18,22,26,255))
        for y in range(4):
            for x in range(4):
                color=(30+x*25,50+y*30,80+x*7+y*3,255 if x+y else 0)
                self.image.putpixel((x,y),color);self.image.putpixel((12+x,y),color)
        buf=io.BytesIO();self.image.save(buf,format='PNG')
        texture={'uuid':'texture-one','id':'0','name':'test','width':20,'height':12,'uv_width':10,'uv_height':6,'source':'data:image/png;base64,'+base64.b64encode(buf.getvalue()).decode(),'render_mode':'default','pbr_channel':'color','internal':True,'layers_enabled':False}
        self.model={'meta':{'format_version':'5.0','model_format':'bedrock','box_uv':False},'name':'fixture','model_identifier':'geometry.fixture','resolution':{'width':10,'height':6},'elements':[{'uuid':'cube-one','name':'panel','type':'cube','box_uv':False,'from':[0,0,0],'to':[2,1,2],'origin':[0,0,0],'rotation':[0,0,0],'faces':{'north':{'uv':[.25,.25,1.75,1.75],'texture':0,'rotation':90},'east':{'uv':[7.75,.25,6.25,1.75],'texture':0}}}],'groups':[{'uuid':'rig','name':'rig','origin':[0,0,0]}],'outliner':[{'uuid':'rig','children':['cube-one']}],'animations':[{'uuid':'anim','name':'animation.fixture.idle','length':1,'animators':{'rig':{'keyframes':[{'channel':'rotation','time':0,'data_points':[{'x':0,'y':0,'z':0}]}]}}}],'textures':[texture]}
        self.source=self.root/'fixture.bbmodel';self.write_source()
    def tearDown(self):self.temp.cleanup()
    def write_source(self):self.source.write_text(json.dumps(self.model),encoding='utf-8')
    def run_cli(self,*args,ok=True):
        self.assertTrue(SCRIPT.is_file(),'Reusable optimizer has not been implemented yet')
        env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'}
        p=subprocess.run([sys.executable,'-B',str(SCRIPT),*map(str,args)],capture_output=True,text=True,env=env)
        if ok:self.assertEqual(p.returncode,0,p.stderr+p.stdout)
        else:self.assertNotEqual(p.returncode,0)
        return p
    def read_output(self,out):
        model=json.loads(next(out.glob('*.bbmodel')).read_text('utf-8'))
        return model,Image.open(next(out.glob('*.png'))).convert('RGBA'),json.loads((out/'optimization_report.json').read_text('utf-8'))
    def sampled_pixel(self,im,face,u,v,logical):
        angle=face.get('rotation',0)
        if angle==90:u,v=v,1-u
        elif angle==180:u,v=1-u,1-v
        elif angle==270:u,v=1-v,u
        a,b,c,d=face['uv'];x=(a+u*(c-a))*im.width/logical[0];y=(b+v*(d-b))*im.height/logical[1]
        return im.getpixel((min(im.width-1,max(0,int(x))),min(im.height-1,max(0,int(y)))))
    def test_exact_reuse_preserves_fractional_uv_mirror_rotation_and_alpha(self):
        out=self.root/'exact';before=self.source.read_bytes()
        self.run_cli('optimize',self.source,'--out',out,'--size',8,'--mode','exact')
        result,im,report=self.read_output(out)
        self.assertEqual(im.size,(8,8));self.assertEqual(report['atlas_regions'],1)
        for name,face in self.model['elements'][0]['faces'].items():
            target=result['elements'][0]['faces'][name]
            for u,v in [(.11,.19),(.36,.71),(.83,.43),(.97,.92)]:self.assertEqual(self.sampled_pixel(self.image,face,u,v,(10,6)),self.sampled_pixel(im,target,u,v,(8,8)))
        self.assertEqual(before,self.source.read_bytes());self.assertEqual(result['animations'],self.model['animations']);self.assertEqual(result['groups'],self.model['groups']);self.assertEqual(result['outliner'],self.model['outliner'])
        self.assertEqual(result['elements'][0]['faces']['north']['rotation'],90)
    def test_audit_distinguishes_bitmap_and_logical_uv_dimensions(self):
        out=self.root/'audit';self.run_cli('audit',self.source,'--out',out)
        data=json.loads((out/'audit.json').read_text('utf-8'))
        self.assertEqual(data['textures'][0]['bitmap_size'],[20,12]);self.assertEqual(data['textures'][0]['uv_size'],[10,6]);self.assertEqual(data['active_faces'],2)
        self.assertEqual(data['exact_unique_patches'],1);self.assertTrue((out/'face_assignments.json').is_file())
    def test_insufficient_exact_capacity_does_not_write_an_optimized_asset(self):
        out=self.root/'too-small';p=self.run_cli('optimize',self.source,'--out',out,'--size',4,'--mode','exact',ok=False)
        self.assertIn('capacity',p.stderr.lower());self.assertFalse(list(out.glob('*.bbmodel')));self.assertFalse(list(out.glob('*.png')))
    def test_shared_fields_keep_surface_proportions_and_animation(self):
        self.model['elements'][0]['faces']['north']['uv']=[0,0,2,1];self.model['elements'][0]['faces']['north']['rotation']=0
        self.model['elements'][0]['faces']['east']['uv']=[0,0,2,1];self.write_source()
        config=self.root/'groups.json';config.write_text(json.dumps({'groups':{'metal':{'strategy':'field','variants':1}},'elements':{'cube-one':'metal'}}))
        out=self.root/'shared';self.run_cli('optimize',self.source,'--out',out,'--size',16,'--mode','shared','--config',config,'--density',2)
        result,im,report=self.read_output(out)
        for f in result['elements'][0]['faces'].values():self.assertAlmostEqual(abs(f['uv'][2]-f['uv'][0])/abs(f['uv'][3]-f['uv'][1]),2)
        self.assertEqual(result['animations'],self.model['animations']);self.assertTrue(report['verification']['non_uv_content_unchanged'])
    def test_unassigned_shared_faces_remain_exact_and_external_patch_is_resolved(self):
        replacement=Image.new('RGBA',(3,2),(245,175,20,255));replacement.save(self.root/'feature.png')
        config=self.root/'groups.json';config.write_text(json.dumps({'groups':{'feature':{'strategy':'patch','source':'feature.png'}},'faces':{'cube-one:north':'feature'}}))
        out=self.root/'mixed';self.run_cli('optimize',self.source,'--out',out,'--size',16,'--mode','shared','--config',config,'--density',1)
        result,im,report=self.read_output(out)
        self.assertEqual(self.sampled_pixel(im,result['elements'][0]['faces']['north'],.4,.6,(16,16)),(245,175,20,255))
        for u,v in [(.2,.3),(.8,.7)]:self.assertEqual(self.sampled_pixel(self.image,self.model['elements'][0]['faces']['east'],u,v,(10,6)),self.sampled_pixel(im,result['elements'][0]['faces']['east'],u,v,(16,16)))
    def test_box_uv_and_unknown_assignments_are_rejected(self):
        self.model['elements'][0]['box_uv']=True;self.write_source()
        out=self.root/'box';p=self.run_cli('optimize',self.source,'--out',out,'--size',16,ok=False);self.assertIn('box uv',p.stderr.lower())
        self.model['elements'][0]['box_uv']=False;self.write_source()
        config=self.root/'bad.json';config.write_text(json.dumps({'groups':{'metal':{'strategy':'field'}},'elements':{'missing-uuid':'metal'}}))
        p=self.run_cli('optimize',self.source,'--out',out,'--size',16,'--mode','shared','--config',config,ok=False);self.assertIn('unknown',p.stderr.lower())
    def test_active_layers_and_pbr_channels_are_rejected_before_mutation(self):
        self.model['textures'][0]['layers_enabled']=True;self.write_source()
        p=self.run_cli('optimize',self.source,'--out',self.root/'layers','--size',16,ok=False);self.assertIn('layer',p.stderr.lower())
        self.model['textures'][0]['layers_enabled']=False;self.model['textures'][0]['pbr_channel']='normal';self.write_source()
        p=self.run_cli('optimize',self.source,'--out',self.root/'normal','--size',16,ok=False);self.assertIn('pbr',p.stderr.lower())
    def test_optimized_model_contains_its_exported_png_and_relative_path(self):
        out=self.root/'self-contained';self.run_cli('optimize',self.source,'--out',out,'--size',8)
        result,im,report=self.read_output(out);t=result['textures'][0]
        self.assertEqual(base64.b64decode(t['source'].split(',',1)[1]),(out/t['relative_path']).read_bytes());self.assertEqual(t['uv_width'],8);self.assertEqual(t['width'],8)
        self.assertTrue(report['verification']['source_unchanged']);self.assertTrue(report['verification']['all_uvs_inside_regions'])
    def test_audit_template_allows_whole_element_assignment_without_hidden_face_overrides(self):
        audit=self.root/'audit-template';self.run_cli('audit',self.source,'--out',audit)
        config=audit/'face_assignments.json';data=json.loads(config.read_text('utf-8'))
        data['groups']['armor']={'strategy':'field','variants':1};data['elements']['cube-one']='armor'
        config.write_text(json.dumps(data),encoding='utf-8')
        out=self.root/'from-template';self.run_cli('optimize',self.source,'--out',out,'--size',16,'--mode','shared','--config',config,'--density',1)
        regions=json.loads((out/'atlas_regions.json').read_text('utf-8'))
        self.assertTrue(any(r['strategy']=='field' for r in regions),'Whole-element material assignment was silently shadowed')

    def test_audit_does_not_overwrite_a_manually_edited_assignment_config(self):
        out=self.root/'audit-edited';self.run_cli('audit',self.source,'--out',out)
        config=out/'face_assignments.json';sentinel=b'{"groups":{"artist-work":{"strategy":"exact"}}}'
        config.write_bytes(sentinel)
        p=self.run_cli('audit',self.source,'--out',out,ok=False)
        self.assertIn('exist',p.stderr.lower());self.assertEqual(config.read_bytes(),sentinel)

    def test_audit_reports_unsupported_optimization_mode_without_mutating_source(self):
        self.model['elements'][0]['box_uv']=True;self.write_source();before=self.source.read_bytes()
        out=self.root/'audit-box-mode';self.run_cli('audit',self.source,'--out',out)
        data=json.loads((out/'audit.json').read_text('utf-8'))
        self.assertFalse(data.get('support',{}).get('optimization_supported',True))
        self.assertIn('box uv',data['support']['reason'].lower());self.assertEqual(data['uv_modes']['box_uv_cubes'],1)
        self.assertEqual(before,self.source.read_bytes())

    def test_software_renderer_rejects_transparent_uv_regions(self):
        out=self.root/'transparent-optimized';self.run_cli('optimize',self.source,'--out',out,'--size',8)
        target=next(out.glob('*.bbmodel'));preview=self.root/'transparent-preview'
        p=subprocess.run([sys.executable,'-B',str(REPO/'scripts/render_compare.py'),str(self.source),str(target),'--out',str(preview)],capture_output=True,text=True)
        self.assertNotEqual(p.returncode,0,'Software renderer ignored alpha and claimed a valid comparison')
        self.assertIn('transparen',p.stderr.lower())
        self.assertFalse((preview/'comparison_report.json').exists())

    def test_repeated_run_is_deterministic_and_existing_outputs_are_not_overwritten(self):
        left=self.root/'left';right=self.root/'right'
        for out in (left,right):self.run_cli('optimize',self.source,'--out',out,'--size',8)
        lm,_,_=self.read_output(left);rm,_,_=self.read_output(right)
        self.assertEqual(lm['elements'],rm['elements']);self.assertEqual(next(left.glob('*.png')).read_bytes(),next(right.glob('*.png')).read_bytes())
        before=next(left.glob('*.bbmodel')).read_bytes();p=self.run_cli('optimize',self.source,'--out',left,'--size',8,ok=False);self.assertIn('exist',p.stderr.lower());self.assertEqual(before,next(left.glob('*.bbmodel')).read_bytes())

if __name__=='__main__':unittest.main(verbosity=2)
