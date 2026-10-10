"""Manual GUI acceptance using named MCP tools in the existing isolated fixture.

No bpy, raw bridge requests, desktop input, original case edits or file saves.
"""
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from stdio_pipe_transport import mcp_stdio_session

REPORT = ROOT / '.runtime/algorithm_review_extended_live.json'


async def verify():
    fixture = json.loads((ROOT / '.runtime/algorithm_review_live.json').read_text(encoding='utf-8'))
    prefix = fixture['fixture_group'].removesuffix('_GN')
    scene = fixture['fixture_scene']
    group, obj, other, modifier = (prefix + suffix for suffix in ('_FieldGN', '_FieldA', '_FieldB', '_FieldParams'))
    report = {'ok': False, 'checks': [], 'images': [], 'group': group, 'objects': [obj, other]}
    def record():
        REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    def check(label, condition):
        assert condition, label
        report['checks'].append(label)
        record()
    env = dict(os.environ, BLENDER_MCP_PROFILE='all',
               BLENDER_MCP_AUDIT_DIR=str(ROOT / '.runtime/review_live_audit'))
    async with mcp_stdio_session(sys.executable, [str(ROOT / 'server.py')], env=env, cwd=ROOT,
                                stderr_path=ROOT / '.runtime/review_extended.stderr.log') as (session, _):
        async def raw(tool_name, **args):
            return await asyncio.wait_for(session.call_tool('blender_' + tool_name, args), 85)
        async def call(tool_name, error=None, **args):
            result = await raw(tool_name, **args)
            text = '\n'.join(c.text for c in result.content if c.type == 'text')
            if error:
                assert result.isError and error in text, (tool_name, text[:3000])
                report.setdefault('errors', []).append({'tool': tool_name, 'expected': error, 'text': text})
                record()
                return text
            assert not result.isError, (tool_name, text[:3000])
            return result.structuredContent or json.loads(text)
        async def capture(name, **args):
            result = await raw(name, **args)
            text = '\n'.join(c.text for c in result.content if c.type == 'text')
            assert not result.isError, (name, text[:3000])
            images = [c for c in result.content if c.type == 'image']
            check(name + ' actual MCP ImageContent', len(images) == 1)
            metadata = next(json.loads(c.text) for c in result.content if c.type == 'text'
                            and json.loads(c.text).get('mcp_image_content'))
            pixels = base64.b64decode(images[0].data, validate=True)
            check(name + ' exact image bytes and target draw',
                  pixels == Path(metadata['path']).read_bytes()
                  and hashlib.sha256(pixels).hexdigest() == metadata['sha256']
                  and metadata['draw_evidence']['match']['matches'] and metadata['restored'])
            report['images'].append({'tool': name, 'metadata': metadata})
            record()
            return metadata
        health = await call('health')
        check('only existing isolated fixture loaded', health['blend_filepath'] == fixture['fixture_blend']
              and health['scene'] == scene and prefix.startswith('MCPReview_'))
        report['health_before'] = health
        await call('reload_review_bridge')
        inventory = await call('scene_summary', scene=scene)
        # The first run creates this small typed test graph; retries reuse it.
        existing = await call('list_objects', scene=scene)
        if obj not in [o['name'] for o in existing['objects']]:
            await call('create_node_group', name=group, if_exists='reuse', interface=[
                {'name': 'Offset', 'socket_type': 'NodeSocketVector', 'default_value': [0, 0, 0]},
                {'name': 'Width', 'socket_type': 'NodeSocketFloat', 'default_value': 1, 'min_value': .1, 'max_value': 10},
                {'name': 'Enabled', 'socket_type': 'NodeSocketBool', 'default_value': True},
                {'name': 'Choice', 'socket_type': 'NodeSocketMenu'},
                {'name': 'ObjectRef', 'socket_type': 'NodeSocketObject'},
                {'name': 'CollectionRef', 'socket_type': 'NodeSocketCollection'},
                {'name': 'MaterialRef', 'socket_type': 'NodeSocketMaterial'},
                {'name': 'Text', 'socket_type': 'NodeSocketString', 'default_value': 'initial'},
                {'name': 'Colour', 'socket_type': 'NodeSocketColor', 'default_value': [1, 0, 0, 1]},
                {'name': 'Rotation', 'socket_type': 'NodeSocketRotation', 'default_value': [0, 0, 0]},
                {'name': 'Hidden', 'socket_type': 'NodeSocketFloat', 'default_value': 1, 'hide_in_modifier': True},
            ])
            tree = await call('inspect_node_group', name=group)
            inp = next(n for n in tree['nodes'] if n['bl_idname'] == 'NodeGroupInput')
            out = next(n for n in tree['nodes'] if n['bl_idname'] == 'NodeGroupOutput')
            async def node(name, node_type, location, **args):
                existing_node = next((n for n in tree['nodes'] if n['name'] == name), None)
                return existing_node or (await call('create_node', group=group, node_type=node_type, name=name, location=location, **args))['node']
            grid = await node('Grid', 'GeometryNodeMeshGrid', [-80, 180])
            position = await node('Set Position', 'GeometryNodeSetPosition', [220, 180])
            menu = await node('Menu Switch', 'GeometryNodeMenuSwitch', [-350, 300], settings={'data_type': 'FLOAT'})
            def sid(node, side, name):
                return next(s['identifier'] for s in node[side] if s['name'] == name)
            ids = {s['name']: s['identifier'] for s in tree['interface']['inputs']}
            for source, source_id, target, target_id in [
                (inp['name'], ids['Offset'], position['name'], sid(position, 'inputs', 'Offset')),
                (inp['name'], ids['Enabled'], position['name'], sid(position, 'inputs', 'Selection')),
                (inp['name'], ids['Width'], grid['name'], sid(grid, 'inputs', 'Size Y')),
                (inp['name'], ids['Choice'], menu['name'], sid(menu, 'inputs', 'Menu')),
                (menu['name'], menu['outputs'][0]['identifier'], grid['name'], sid(grid, 'inputs', 'Size X')),
                (grid['name'], sid(grid, 'outputs', 'Mesh'), position['name'], sid(position, 'inputs', 'Geometry')),
                (position['name'], sid(position, 'outputs', 'Geometry'), out['name'], sid(out, 'inputs', 'Geometry')),
            ]:
                await call('link_sockets', group=group, from_node=source, from_socket=source_id,
                           to_node=target, to_socket=target_id)
            values = [s for s in menu['inputs'] if s['type'] == 'VALUE']
            check('live Menu Switch has two entries', len(values) == 2)
            for socket, value in zip(values, [1, 3]):
                await call('set_socket_default', group=group, node=menu['name'], socket=socket['identifier'], value=value)
            await call('geometry_nodes_move_nodes', group=group, positions=[
                {'node': inp['name'], 'x': -660, 'y': 130}, {'node': out['name'], 'x': 520, 'y': 180}])
            for name in (obj, other):
                await call('outliner_object_create', scene=scene, path='', name=name, type='MESH', vertices=[])
                await call('attach_geometry_nodes_modifier', object=name, group=group, modifier_name=modifier)
        listed = await call('properties_list_parameters', object=obj, modifier=modifier)
        report['parameters'] = listed
        record()
        ids = {p['name']: p['identifier'] for p in listed['inputs']}
        async def get(name):
            return (await call('properties_get_parameter', object=obj, modifier=modifier, identifier=ids[name]))['parameter']
        async def set_value(name, value=None, **kwargs):
            return await call('properties_set_parameter', object=obj, modifier=modifier,
                              identifier=ids[name], value=value, **kwargs)
        async def inspect():
            return await call('model_inspection_inspect', object=obj, modifier=modifier, scene=scene)
        # Normalize only this fixture instance so the test is repeatable.
        await set_value('Offset', [0, 0, 0])
        await set_value('Width', 1)
        await set_value('Enabled', True)
        before_other = await call('properties_list_parameters', object=other, modifier=modifier)
        defaults = (await call('inspect_node_group', name=group))['interface']
        before = await inspect()
        await capture('model_inspection_capture_viewport', object=obj, scene=scene, fit=True)
        changed = await set_value('Offset', mode='ATTRIBUTE', attribute_name='position')
        field = await get('Offset')
        after = await inspect()
        report.update(field_write=changed, field_read=field, field_before=before, field_after=after)
        record()
        check('ATTRIBUTE position doubles real evaluated extent',
              after['bounds']['size'][0] == before['bounds']['size'][0] * 2)
        check('live field mode active and proven', field['attribute_mode']['verified'] and field['is_field'])
        await capture('model_inspection_capture_viewport', object=obj, scene=scene, fit=True)
        await set_value('Offset', [0, 0, 1])
        value = await get('Offset')
        after_value = await inspect()
        report.update(value_read=value, value_after=after_value)
        check('VALUE clears field selector and moves mesh in Z',
              not value['is_field'] and after_value['bounds']['min'][2] == 1)
        # Actual menu enum values/identifiers, not guessed positional indices.
        menu_param = await get('Choice')
        report['menu_before'] = menu_param
        items = menu_param['enum']['items']
        check('live modifier menu exposes reliable enum values', len(items) == 2)
        await set_value('Choice', items[0]['identifier'])
        menu_before = await inspect()
        await set_value('Choice', items[1]['identifier'])
        menu_after = await inspect()
        check('menu identifier maps to evaluated geometry',
              menu_before['bounds']['size'][0] == 1 and menu_after['bounds']['size'][0] == 3)
        await set_value('Choice', items[0]['value'])
        check('menu numeric RNA value roundtrip', (await get('Choice'))['current'] == items[0]['value'])
        for name, new in [('Text', 'verified'), ('Colour', [.25, .5, .75, 1]), ('Rotation', [.1, .2, .3])]:
            await set_value(name, new)
            actual = (await get(name))['current']
            check(name + ' live typed value roundtrip', actual == new if isinstance(new, str)
                  else all(abs(a - b) < 1e-6 for a, b in zip(actual, new)))
        await set_value('ObjectRef', {'id_type': 'OBJECT', 'id_name': fixture['fixture_objects'][0]})
        check('OBJECT pointer actual datablock readback', (await get('ObjectRef'))['current']['id_name'] == fixture['fixture_objects'][0])
        collection = prefix + '_ReferenceCollection'
        await call('outliner_ensure_path', scene=scene, path=collection)
        await set_value('CollectionRef', {'id_type': 'COLLECTION', 'id_name': collection})
        check('COLLECTION pointer actual datablock readback', (await get('CollectionRef'))['current']['id_name'] == collection)
        await set_value('MaterialRef', None)
        check('MATERIAL null pointer readback', (await get('MaterialRef'))['current'] is None)
        await call('properties_set_parameter', error='reference_type_mismatch', object=obj, modifier=modifier,
                   identifier=ids['MaterialRef'], value={'id_type': 'OBJECT', 'id_name': obj})
        await set_value('Enabled', False)
        check('bool selection input changes actual geometry', (await inspect())['bounds']['min'][2] == 0)
        await set_value('Enabled', True)
        await set_value('Width', 2)
        check('float width input changes actual geometry', (await inspect())['bounds']['size'][1] == 2)
        await set_value('Width', 1)
        stable = await call('properties_list_parameters', object=obj, modifier=modifier)
        for changes, error in [
            ([{'identifier': ids['Width'], 'value': 2}, {'identifier': ids['ObjectRef'], 'value': {'id_type': 'OBJECT', 'id_name': '__absent__'}}], 'batch_precheck_failed'),
            ([{'identifier': ids['Width'], 'value': 2}, {'identifier': ids['Offset'], 'value': [1, 2]}], 'batch_precheck_failed'),
            ([{'identifier': ids['Hidden'], 'value': 2}], 'hidden_input'),
            ([{'identifier': ids['Enabled'], 'value': 1}], 'invalid_value'),
            ([{'identifier': ids['Choice'], 'value': 10000}], 'value_out_of_range'),
        ]:
            await call('properties_set_parameters', error=error, object=obj, modifier=modifier, changes=changes)
            check(error + ' leaves entire instance unchanged',
                  await call('properties_list_parameters', object=obj, modifier=modifier) == stable)
        check('all typed writes preserve other instance and shared defaults',
              await call('properties_list_parameters', object=other, modifier=modifier) == before_other
              and (await call('inspect_node_group', name=group))['interface'] == defaults)
        for args, code in [({'object': '__absent__', 'modifier': modifier, 'identifier': ids['Width']}, 'object_not_found'),
                           ({'object': obj, 'modifier': '__absent__', 'identifier': ids['Width']}, 'modifier_not_found'),
                           ({'object': obj, 'modifier': modifier, 'identifier': '__absent__'}, 'input_not_found')]:
            await call('properties_get_parameter', error=code, **args)
        # A positive real GeometrySet instance must not be mislabeled empty mesh.
        instance_obj, instance_group = prefix + '_Instance', prefix + '_InstanceGN'
        existing = await call('list_objects', scene=scene)
        if instance_obj not in [item['name'] for item in existing['objects']]:
            await call('create_node_group', name=instance_group)
            tree = await call('inspect_node_group', name=instance_group)
            out = next(n for n in tree['nodes'] if n['bl_idname'] == 'NodeGroupOutput')
            node = (await call('create_node', group=instance_group, name='Object Info', node_type='GeometryNodeObjectInfo'))['node']
            await call('set_socket_default', group=instance_group, node=node['name'], socket='Object',
                       value={'id_type': 'OBJECT', 'id_name': obj})
            await call('set_socket_default', group=instance_group, node=node['name'], socket='As Instance', value=True)
            await call('link_sockets', group=instance_group, from_node=node['name'], from_socket='Geometry',
                       to_node=out['name'], to_socket=out['inputs'][0]['identifier'])
            await call('outliner_object_create', scene=scene, path='', name=instance_obj, type='MESH', vertices=[])
            await call('attach_geometry_nodes_modifier', object=instance_obj, group=instance_group, modifier_name='Instance')
        instances = await call('model_inspection_inspect', object=instance_obj, scene=scene)
        report['positive_instances'] = instances
        check('actual instance count bounds and cleanup', instances['instances']['instances'] == 1
              and not instances['instances']['errors'] and instances['bounds'] is not None
              and instances['mesh_cleanup']['to_mesh_calls'] == instances['mesh_cleanup']['to_mesh_clear_calls'])
        # Visible Frame/Reroute fixture, with explicitly bounded coordinates.
        base = fixture['fixture_group']
        layout = await call('geometry_nodes_layout', group=base)
        names = {n['bl_idname']: n['name'] for n in layout['nodes']}
        await call('geometry_nodes_move_nodes', group=base, positions=[
            {'node': names['NodeGroupInput'], 'x': -600, 'y': 120},
            {'node': names['NodeGroupOutput'], 'x': 500, 'y': 120}])
        frame = prefix + '_Frame'
        await call('node_frame_create', group=base, frame=frame, label='Two independent grids', location=[-250, 180], shrink=True)
        grids = [n['name'] for n in layout['nodes'] if n['bl_idname'] == 'GeometryNodeMeshGrid']
        await call('node_frame_parent', group=base, frame=frame, nodes=grids)
        edge = next(l for l in layout['links'] if l['from_node'] == names['NodeGroupInput'])
        inserted = await call('geometry_nodes_reroute_insert', group=base,
                              links=[{k: edge[k] for k in ('from_node', 'from_socket', 'to_node', 'to_socket')}], points=[[-350, 60]])
        try:
            all_image = await capture('geometry_nodes_capture_editor', group=base, fit='ALL', include_sidebar=False)
            selected = await capture('geometry_nodes_capture_editor', group=base, fit='SELECTED', nodes=[frame], include_sidebar=True)
            check('drawn structure reports Frame children and native Reroute',
                  any(n['parent'] == frame for n in all_image['node_layout']['nodes'])
                  and any(n['bl_idname'] == 'NodeReroute' for n in all_image['node_layout']['nodes']))
            report['layout_image'] = all_image['path']
            report['subset_image'] = selected['path']
        finally:
            layout = await call('geometry_nodes_layout', group=base)
            reroutes = [n['name'] for n in layout['nodes'] if n['bl_idname'] == 'NodeReroute']
            await call('geometry_nodes_reroute_delete', group=base, nodes=reroutes)
        original = json.loads((ROOT / '.runtime/algorithm_review_asset_baseline.json').read_text(encoding='utf-8'))
        check('original case file SHA256 unchanged', hashlib.sha256(Path(original['health']['blend_filepath']).read_bytes()).hexdigest() == original['disk_sha256'])
        final = await call('health')
        check('fixture PID path scene preserved without save', all(final[k] == health[k] for k in ('pid', 'blend_filepath', 'scene')))
        relative = 'MCP验收/' + prefix + '/extended_fixture_instances.blend'
        path = ROOT.parent / 'Project' / relative
        if not path.exists():
            await call('save_scene', scene=scene, relative_path=relative, overwrite=False)
        report['saved_fixture'] = str(path)
        report['ok'] = True
        record()
        print(json.dumps({'ok': True, 'checks': len(report['checks']), 'images': len(report['images'])}))


if __name__ == '__main__':
    asyncio.run(verify())
