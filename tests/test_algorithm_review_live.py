"""Real named MCP fixture acceptance. Manual only; refuses missing bridge commands."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid
import base64

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from stdio_pipe_transport import mcp_stdio_session

REPORT = ROOT / '.runtime' / 'algorithm_review_live.json'
REQUIRED = {'properties_list_parameters', 'properties_set_parameters', 'model_inspect',
            'geometry_nodes_layout', 'geometry_nodes_reroute_insert',
            'geometry_nodes_reroute_move', 'geometry_nodes_reroute_delete',
            'viewport_capture_prepare', 'gn_editor_capture_prepare'}


async def verify():
    report = {'ok': False, 'checks': [], 'fixture_created': False,
              'kind': 'real_gui_blender_named_mcp', 'images': []}
    prefix = 'MCPReview_' + uuid.uuid4().hex[:8]
    scene, group = prefix + '_Scene', prefix + '_GN'
    a, b, modifier = prefix+'_A', prefix+'_B', prefix+'_Parameters'
    report.update(fixture_scene=scene, fixture_group=group, fixture_objects=[a,b])
    def record():
        REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    def check(label, condition):
        assert condition, label
        report['checks'].append(label)
        record()
    env = dict(os.environ, BLENDER_MCP_PROFILE='all',
               BLENDER_MCP_AUDIT_DIR=str(ROOT / '.runtime/review_live_audit'))
    record()
    async with mcp_stdio_session(sys.executable, [str(ROOT/'server.py')], env=env, cwd=ROOT,
                                stderr_path=ROOT/'.runtime/review_live.stderr.log') as (session, _):
        async def raw(tool_name, **args):
            return await asyncio.wait_for(session.call_tool('blender_'+tool_name, args), 85)
        async def call(tool_name, error=None, **args):
            r = await raw(tool_name, **args)
            text = '\n'.join(c.text for c in r.content if c.type=='text')
            if error:
                assert r.isError and error in text, (tool_name, text[:1500])
                return text
            assert not r.isError, (tool_name, text[:2000])
            return r.structuredContent or json.loads(text)
        async def capture(name, **args):
            r = await raw(name, **args)
            text = '\n'.join(c.text for c in r.content if c.type=='text')
            assert not r.isError, (name, text[:2000])
            images = [c for c in r.content if c.type=='image']
            check(name+' native MCP image', len(images)==1 and images[0].mimeType=='image/png')
            # The actual native MCP image is returned by the server; a file path
            # alone is never counted as image acceptance.
            metadata = json.loads(next(c.text for c in r.content if c.type=='text'))
            report['images'].append({'tool':name,'metadata':metadata})
            record()
            return metadata
        health = await call('health')
        report.update(pid=health['pid'], version=health['blender_version'],
                      missing_commands=sorted(REQUIRED-set(health['commands'])))
        record()
        if report['missing_commands']:
            raise RuntimeError('bridge_refresh_required; no fixture created')
        check('Blender 5.2.1 LTS GUI', health['blender_version']=='5.2.1 LTS' and not health['background'])
        original_file = Path(health['blend_filepath'])
        original_hash = hashlib.sha256(original_file.read_bytes()).hexdigest()
        inventory = await call('list_objects')
        baseline_objects = {r['name']:await call('object_info', name=r['name'], include_evaluated=False)
                            for r in inventory['objects']}
        groups = {m['node_group'] for o in baseline_objects.values() for m in o['modifiers'] if m.get('node_group')}
        baseline_groups = {g:await call('inspect_node_group', name=g) for g in groups}
        await call('create_case_scene', name=scene, activate=False)
        report['fixture_created']=True
        record()
        for name in (a,b):
            await call('outliner_object_create', scene=scene, path='', name=name, type='MESH', vertices=[])
        await call('create_node_group', name=group, interface=[{
            'name':'Columns','socket_type':'NodeSocketInt','default_value':4,'min_value':2,'max_value':20}])
        tree = await call('inspect_node_group', name=group)
        identifier = next(i['identifier'] for i in tree['interface']['inputs'] if i['name']=='Columns')
        input_node = next(n for n in tree['nodes'] if n['bl_idname']=='NodeGroupInput')
        output_node = next(n for n in tree['nodes'] if n['bl_idname']=='NodeGroupOutput')
        g1=(await call('create_node', group=group, node_type='GeometryNodeMeshGrid', name=prefix+'_Grid1', location=[-200,120]))['node']
        g2=(await call('create_node', group=group, node_type='GeometryNodeMeshGrid', name=prefix+'_Grid2', location=[-200,-180]))['node']
        join=(await call('create_node', group=group, node_type='GeometryNodeJoinGeometry', name=prefix+'_Join', location=[120,120]))['node']
        def socket(node, direction, name):
            return next(s['identifier'] for s in node[direction] if s['name']==name)
        branch=[]
        multi=[]
        for node in (g1,g2):
            await call('set_socket_default', group=group, node=node['name'], socket=socket(node,'inputs','Vertices Y'), value=3)
            edge={'from_node':input_node['name'],'from_socket':identifier,
                  'to_node':node['name'],'to_socket':socket(node,'inputs','Vertices X')}
            await call('link_sockets',group=group,**edge)
            branch.append(edge)
            edge={'from_node':node['name'],'from_socket':socket(node,'outputs','Mesh'),
                  'to_node':join['name'],'to_socket':socket(join,'inputs','Geometry')}
            # Existing named tool uses Blender's links.new; multi-input adds
            # another source with replace=true (false rejects an occupied input).
            await call('link_sockets',group=group,replace=True,**edge)
            multi.append(edge)
        await call('link_sockets',group=group,from_node=join['name'],from_socket=socket(join,'outputs','Geometry'),
                   to_node=output_node['name'],to_socket=socket(output_node,'inputs','Geometry'))
        for name in (a,b):
            await call('attach_geometry_nodes_modifier',object=name,group=group,modifier_name=modifier,parameters={identifier:4})
        relative='MCP验收/'+prefix+'/fixture.blend'
        await call('save_scene',scene=scene,relative_path=relative,overwrite=False)
        report['fixture_blend']=str(ROOT.parent/'Project'/relative)
        check('dedicated fixture physically inside Project', Path(report['fixture_blend']).is_file())
        listed=await call('properties_list_parameters',object=a,modifier=modifier)
        parameter=next(p for p in listed['inputs'] if p['identifier']==identifier)
        check('parameter identifier type instance default', parameter['current']==4 and parameter['default']==4)
        report['parameter_before']=parameter
        before=await call('model_inspection_inspect',object=a,scene=scene,modifier=modifier)
        b_before=await call('properties_list_parameters',object=b,modifier=modifier)
        await call('properties_set_parameters',object=a,modifier=modifier,changes=[{'identifier':identifier,'value':6}])
        after=await call('model_inspection_inspect',object=a,scene=scene,modifier=modifier)
        check('actual mesh count updates 24 to 36 vertices', before['counts']['vertices']==24 and after['counts']['vertices']==36)
        check('shared default unchanged', (await call('inspect_node_group',name=group))['interface']['inputs'][0]['default_value']==4)
        check('other modifier instance unchanged', await call('properties_list_parameters',object=b,modifier=modifier)==b_before)
        stable=await call('properties_list_parameters',object=a,modifier=modifier)
        for changes, code in (([{'identifier':identifier,'value':7},{'identifier':'__missing__','value':2}], 'batch_precheck_failed'),
                              ([{'identifier':identifier,'value':'wrong'}], 'invalid_value'),
                              ([{'identifier':identifier,'value':1000}], 'value_out_of_range')):
            await call('properties_set_parameters',error=code,object=a,modifier=modifier,changes=changes)
            check('invalid batch leaves instance unchanged', await call('properties_list_parameters',object=a,modifier=modifier)==stable)
        report['evaluated_before']=before
        report['evaluated_after']=after
        await capture('model_inspection_capture_viewport',object=a,scene=scene,fit=True)
        await capture('geometry_nodes_capture_editor',group=group,object=a,modifier=modifier,fit='ALL')
        async def inspected_signature():
            result=await call('model_inspection_inspect',object=a,scene=scene)
            return {k:result[k] for k in ('counts','bounds','signature_sha256')}
        for label,edges,shared,count in [('single',branch[:1],False,1),('branch',branch,True,1),('multi',multi,False,2)]:
            signature=await inspected_signature()
            baseline=await call('geometry_nodes_layout',group=group)
            inserted=await call('geometry_nodes_reroute_insert',group=group,links=edges,shared_branch=shared)
            check(label+' insert count and semantic equality', inserted['created_count']==count and inserted['validation']['equivalent'])
            layout=await call('geometry_nodes_layout',group=group)
            names=[n['name'] for n in layout['nodes'] if n['bl_idname']=='NodeReroute']
            await call('geometry_nodes_reroute_move',group=group,positions=[{'node':name,'x':100+80*i,'y':-300} for i,name in enumerate(names)])
            check(label+' evaluated geometry unchanged', await inspected_signature()==signature)
            deleted=await call('geometry_nodes_reroute_delete',group=group,nodes=names)
            check(label+' delete semantic equality', deleted['validation']['equivalent'])
            check(label+' geometry restored',await inspected_signature()==signature)
            final=await call('geometry_nodes_layout',group=group)
            def endpoints(layout):
                return sorted((l['from_node'],l['from_socket'],l['to_node'],l['to_socket'],l.get('multi_input_order')) for l in layout['links'])
            check(label+' endpoints and multi-input order restored', endpoints(final)==endpoints(baseline))
        end=await call('health')
        check('original filepath and active scene preserved',end['blend_filepath']==health['blend_filepath'] and end['scene']==health['scene'])
        check('original saved total SHA256 unchanged',hashlib.sha256(original_file.read_bytes()).hexdigest()==original_hash)
        for name,previous in baseline_objects.items():
            check('original object preserved '+name,await call('object_info',name=name,include_evaluated=False)==previous)
        for name,previous in baseline_groups.items():
            check('original GN group preserved '+name,await call('inspect_node_group',name=name)==previous)
        report['ok']=True
        record()
        print(json.dumps({'ok':True,'checks':len(report['checks']),'images':len(report['images']),
                          'fixture_blend':report['fixture_blend']},ensure_ascii=False))


async def resume_existing():
    """Continue the last isolated fixture; never create another on screenshot retry."""
    report = json.loads(REPORT.read_text(encoding='utf-8'))
    group, scene = report['fixture_group'], report['fixture_scene']
    a, b = report['fixture_objects']
    prefix = group.removesuffix('_GN')
    assert prefix.startswith('MCPReview_') and Path(report['fixture_blend']).is_file()
    modifier=prefix+'_Parameters'
    def record():
        REPORT.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    def check(label, condition):
        assert condition, label
        report['checks'].append(label)
        record()
    env=dict(os.environ,BLENDER_MCP_PROFILE='all',BLENDER_MCP_AUDIT_DIR=str(ROOT/'.runtime/review_live_audit'))
    async with mcp_stdio_session(sys.executable,[str(ROOT/'server.py')],env=env,cwd=ROOT,
                                stderr_path=ROOT/'.runtime/review_live_resume.stderr.log') as (session,_):
        async def raw(tool_name, **args):
            r=await asyncio.wait_for(session.call_tool('blender_'+tool_name,args),85)
            text='\n'.join(c.text for c in r.content if c.type=='text')
            assert not r.isError,(tool_name,text[:2200])
            return r
        async def call(tool_name, **args):
            r=await raw(tool_name,**args)
            return r.structuredContent or json.loads(next(c.text for c in r.content if c.type=='text'))
        async def signature():
            s=await call('model_inspection_inspect',object=a,scene=scene)
            return {k:s[k] for k in ('counts','bounds','signature_sha256')}
        def image_evidence(result, label):
            images = [content for content in result.content if content.type == 'image']
            check(label+' real MCP image', len(images) == 1 and images[0].mimeType == 'image/png')
            texts = [json.loads(content.text) for content in result.content if content.type == 'text']
            metadata = next(data for data in texts if data.get('mcp_image_content'))
            pixels = base64.b64decode(images[0].data, validate=True)
            check(label+' received pixels match published PNG',
                  hashlib.sha256(pixels).hexdigest() == metadata['sha256']
                  and Path(metadata['path']).read_bytes() == pixels)
            check(label+' actual editor framebuffer and UI restored',
                  metadata['image_source'] in ('bpy.types.Window.screenshot', 'bpy.ops.screen.screenshot_area')
                  and metadata['draw_evidence']['match']['matches']
                  and metadata['restored'] and not metadata['saved_blend'])
            report['images'].append({'tool':label, 'metadata':metadata,
                                     'mcp_image_sha256':hashlib.sha256(pixels).hexdigest()})
            record()
            return metadata
        if '--assets-only' in sys.argv:
            baseline=json.loads((ROOT/'.runtime/algorithm_review_asset_baseline.json').read_text(encoding='utf-8'))
            health=await call('health')
            check('original filepath active scene preserved',all(health[k]==baseline['health'][k] for k in ('blend_filepath','scene')))
            check('original total disk SHA256 unchanged',hashlib.sha256(Path(health['blend_filepath']).read_bytes()).hexdigest()==baseline['disk_sha256'])
            for name,previous in baseline['objects'].items():
                check('original object preserved '+name,await call('object_info',name=name,include_evaluated=False)==previous)
            for name,previous in baseline['groups'].items():
                check('original GN group preserved '+name,await call('inspect_node_group',name=name)==previous)
            report['asset_protection_ok']=True
            record()
            print(json.dumps({'asset_protection_ok':True,'objects':len(baseline['objects']),'groups':len(baseline['groups'])}))
            return
        health = await call('health')
        missing = sorted((REQUIRED | {'reload_review_commands'}) - set(health['commands']))
        report['refresh_gate'] = {'pid':health['pid'], 'missing_commands':missing,
                                  'test_mutations_performed':False}
        record()
        if missing:
            report['failure'] = {'code':'bridge_refresh_required', 'missing_commands':missing,
                                 'test_mutations_performed':False, 'saved_blend':False}
            record()
            print(json.dumps(report['failure']))
            return 2
        refreshed = await call('reload_review_bridge')
        if report.get('failure'):
            report['prior_failure'] = report.pop('failure')
        check('fixed review refresh preserves PID filepath and scene',
              all(refreshed[key] == health[key] for key in ('pid','blend_filepath','scene'))
              and refreshed['bridge_preserved'] and not refreshed['saved_blend'])
        session_baseline = {key:health[key] for key in ('pid','blend_filepath','scene')}
        report['resume_session_baseline'] = session_baseline
        layout=await call('geometry_nodes_layout',group=group)
        input_name=next(n['name'] for n in layout['nodes'] if n['bl_idname']=='NodeGroupInput')
        join_name=next(n['name'] for n in layout['nodes'] if n['bl_idname']=='GeometryNodeJoinGeometry')
        branch=[{k:l[k] for k in ('from_node','from_socket','to_node','to_socket')} for l in layout['links'] if l['from_node']==input_name]
        multi=[{k:l[k] for k in ('from_node','from_socket','to_node','to_socket','multi_input_sort_id')} for l in layout['links'] if l['to_node']==join_name]
        for label,edges,shared,count in [('single',branch[:1],False,1),('branch',branch,True,1),('multi',multi,False,2)]:
            before=await signature()
            baseline=await call('geometry_nodes_layout',group=group)
            inserted=await call('geometry_nodes_reroute_insert',group=group,links=edges,shared_branch=shared)
            check(label+' insert count and semantic equality',inserted['created_count']==count and inserted['validation']['equivalent'])
            updated=await call('geometry_nodes_layout',group=group)
            names=[n['name'] for n in updated['nodes'] if n['bl_idname']=='NodeReroute']
            await call('geometry_nodes_reroute_move',group=group,positions=[{'node':n,'x':100+80*i,'y':-300} for i,n in enumerate(names)])
            check(label+' evaluated geometry unchanged',await signature()==before)
            deleted=await call('geometry_nodes_reroute_delete',group=group,nodes=names)
            check(label+' delete semantic equality',deleted['validation']['equivalent'])
            check(label+' geometry restored',await signature()==before)
            final=await call('geometry_nodes_layout',group=group)
            def edges_of(data):
                return sorted((l['from_node'],l['from_socket'],l['to_node'],l['to_socket'],l.get('multi_input_order')) for l in data['links'])
            check(label+' endpoints and multi-input order restored',edges_of(final)==edges_of(baseline))
        report['topology_ok']=True
        record()
        if '--topology-only' in sys.argv:
            print(json.dumps({'topology_ok':True,'checks':len(report['checks'])}))
            return
        for tool_name,args in [('model_inspection_capture_viewport',dict(object=a,scene=scene,fit=True)),
                               ('geometry_nodes_capture_editor',dict(group=group,object=a,modifier=modifier,fit='ALL')),
                               ('geometry_nodes_capture_editor',dict(group=group,object=a,modifier=modifier,
                                                                     fit='SELECTED',nodes=[join_name],include_sidebar=True))]:
            r=await raw(tool_name,**args)
            metadata = image_evidence(r, tool_name)
            if tool_name == 'geometry_nodes_capture_editor':
                check('exact requested GN tree drawn', metadata['ui_state']['tree'] == group)
                check('structure includes coordinates links Frames and Reroutes',
                      bool(metadata['node_layout']['nodes']) and bool(metadata['node_layout']['links']))
        listed=await call('properties_list_parameters',object=a,modifier=modifier)
        parameter=next(p for p in listed['inputs'] if p['name']=='Columns')
        identifier=parameter['identifier']
        old_value=parameter['current']
        new_value=5 if old_value != 5 else 6
        before_other=await call('properties_list_parameters',object=b,modifier=modifier)
        r=await raw('properties_iterate_parameters',object=a,modifier=modifier,scene=scene,
                    changes=[{'identifier':identifier,'value':new_value}])
        texts=[json.loads(c.text) for c in r.content if c.type=='text']
        check('iteration diff summary and MCP image',texts[0]['parameters_committed'] and
              texts[0]['before']['counts']['vertices']==old_value*6 and
              texts[0]['after']['counts']['vertices']==new_value*6 and
              texts[0]['before']['signature_sha256'] != texts[0]['after']['signature_sha256'])
        image_evidence(r, 'properties_iterate_parameters_after')
        check('iteration preserves other modifier instance',
              await call('properties_list_parameters',object=b,modifier=modifier)==before_other)
        check('iteration preserves shared interface default',
              next(item for item in (await call('inspect_node_group',name=group))['interface']['inputs']
                   if item['identifier']==identifier)['default_value']==4)
        report['iteration']=texts[0]
        baseline=json.loads((ROOT/'.runtime/algorithm_review_asset_baseline.json').read_text(encoding='utf-8'))
        health=await call('health')
        check('resume PID filepath active scene preserved',all(health[k]==session_baseline[k]
                                                              for k in ('pid','blend_filepath','scene')))
        original_path = baseline['health']['blend_filepath']
        check('original total disk SHA256 unchanged',hashlib.sha256(Path(original_path).read_bytes()).hexdigest()==baseline['disk_sha256'])
        if health['blend_filepath'] == original_path:
            for name,previous in baseline['objects'].items():
                check('original object preserved '+name,await call('object_info',name=name,include_evaluated=False)==previous)
            for name,previous in baseline['groups'].items():
                check('original GN group preserved '+name,await call('inspect_node_group',name=name)==previous)
            report['original_runtime_assets_compared'] = True
        else:
            check('only the saved isolated fixture is loaded',health['blend_filepath']==report['fixture_blend'])
            report['original_runtime_assets_compared'] = False
            report['original_asset_evidence'] = 'original disk hash unchanged; original cases are not loaded in this restarted fixture-only session'
        report['ok']=True
        record()
        print(json.dumps({'ok':True,'checks':len(report['checks']),'images':len(report['images'])}))


if __name__=='__main__':
    outcome = asyncio.run(resume_existing() if any(s in sys.argv for s in ('--resume','--topology-only','--assets-only')) else verify())
    if isinstance(outcome, int):
        raise SystemExit(outcome)
