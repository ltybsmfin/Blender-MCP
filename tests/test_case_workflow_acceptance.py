"""Bounded MCP acceptance: unchanged originals, isolated consecutive cases, recovery.

Requires an already running bridge. Never starts/stops Blender; writes unique
case outputs in Project/算法复现 and leaves them for inspection.
"""
import asyncio, json, os, sys, time
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
root = Path(__file__).resolve().parents[1]
report = {'checks': {}, 'started_at': time.time()}
def data(result):
    if result.isError:
        raise RuntimeError('\n'.join(item.text for item in result.content if getattr(item, 'text', None)))
    return result.structuredContent or json.loads(next(item.text for item in result.content if getattr(item, 'text', None)))
async def main():
    async with asyncio.timeout(240):
        params = StdioServerParameters(command=sys.executable, args=[str(root / 'server.py')], env=dict(os.environ), cwd=str(root))
        with (root / '.runtime' / 'scene_acceptance_sdk.stderr.log').open('w', encoding='utf-8') as errors:
            async with stdio_client(params, errlog=errors) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    async def call(name, args=None):
                        return data(await asyncio.wait_for(session.call_tool(name,args or {}), timeout=60))
                    health = await call('blender_health')
                    original_scene = health['scene']
                    original_path = health['blend_filepath']
                    baseline_scenes = await call('blender_list_scenes', {'include_objects':True})
                    original_objects = sorted({name for scene in baseline_scenes['scenes'] for name in scene['object_names']})
                    object_before = {name: await call('blender_object_info', {'name':name}) for name in original_objects}
                    summary_before = await call('blender_scene_summary')
                    group_before = {group['name']: await call('blender_inspect_node_group', {'name':group['name']}) for group in summary_before['node_groups']}
                    report.update(original_scene=original_scene, original_path=original_path, baseline_scenes=baseline_scenes, object_before=object_before, group_before=group_before)
                    try:
                        process = await asyncio.create_subprocess_exec(sys.executable, str(root / 'tests' / 'test_scene_isolation.py'), '--live', cwd=str(root), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                        try:
                            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=180)
                        except TimeoutError:
                            process.terminate()  # Only this verification runner, never Blender.
                            await asyncio.wait_for(process.wait(), timeout=5)
                            raise
                        (root / '.runtime' / 'logs' / 'scene_acceptance_stdout.log').write_bytes(stdout)
                        (root / '.runtime' / 'logs' / 'scene_acceptance_stderr.log').write_bytes(stderr)
                        report['runner_exit_code'] = process.returncode
                        report['runner_summary'] = stdout.decode('utf-8', 'replace')[-2200:]
                    finally:
                        report['restored'] = await call('blender_switch_scene', {'name':original_scene})
                    if report.get('runner_exit_code') == 0:
                        suite = json.loads((root / '.runtime' / 'scene_isolation_report.json').read_text(encoding='utf-8'))
                        context = suite['live_run']['context']
                        case_a, case_b = context['case_scenes']
                        prefix = context['prefix']
                        directory = str(Path(context['relative_blend']).parent).replace('\\', '/')
                        # Independently save the next case, while original Scene is active.
                        saved_b = await call('blender_save_scene', {'scene':case_b, 'relative_path':directory+'/next_case.blend', 'compress':True})
                        inspected_b = await call('blender_inspect_blend_file', {'relative_path':directory+'/next_case.blend'})
                        report['next_case'] = {'save':saved_b, 'inspect':inspected_b}
                        report['checks']['next_case_file_excludes_prior_case'] = inspected_b['scenes']==[case_b] and inspected_b['objects']==[prefix+'_obj_ext'] and not inspected_b['inventories'].get('node_groups')
                        # Recover the deliberate cross-scene driver error using a named tool.
                        report['removed_external_driver'] = await call('blender_remove_driver', {'owner_kind':'object','owner_name':prefix+'_obj','data_path':'["driven_ext"]'})
                        recovered = await call('blender_save_scene', {'scene':case_a,'relative_path':directory+'/recovered.blend'})
                        inspect_recovered = await call('blender_inspect_blend_file', {'relative_path':directory+'/recovered.blend'})
                        report['recovered_case'] = {'save':recovered,'inspect':inspect_recovered}
                        report['checks']['save_error_is_recoverable'] = recovered['saved'] and inspect_recovered['scenes']==[case_a] and inspect_recovered['objects']==[prefix+'_obj']
                    object_after = {name: await call('blender_object_info', {'name':name}) for name in original_objects}
                    group_after = {name: await call('blender_inspect_node_group', {'name':name}) for name in group_before}
                    scenes_after = await call('blender_list_scenes', {'include_objects':True})
                    final_health = await call('blender_health')
                    before_rows = {row['name']:row for row in baseline_scenes['scenes']}
                    after_rows = {row['name']:row for row in scenes_after['scenes'] if row['name'] in before_rows}
                    report.update(object_after=object_after, group_after=group_after, scenes_after=scenes_after)
                    report['checks'].update(original_object_details_unchanged=object_before==object_after, original_node_graphs_unchanged=group_before==group_after, original_scene_rows_unchanged=before_rows==after_rows, original_mainfile_path_unchanged=original_path==final_health['blend_filepath'], original_scene_restored=original_scene==final_health['scene'], live_runner_passed=report.get('runner_exit_code')==0)
                    report['ok'] = all(report['checks'].values())
                    print(json.dumps({'ok': report['ok'], 'checks':report['checks'], 'report':str(root / '.runtime' / 'scene_acceptance_original_state.json')}, ensure_ascii=False))
try:
    asyncio.run(main())
finally:
    report['finished_at'] = time.time()
    (root / '.runtime' / 'scene_acceptance_original_state.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
if not report.get('ok'):
    raise SystemExit(1)
