"""Production stdio tool visibility and legacy schemas; does not contact Blender."""
from __future__ import annotations
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
from stdio_pipe_transport import mcp_stdio_session
from blender_mcp.tool_profiles import ALGORITHM_REVIEW_TOOL_NAMES, KNOWN_TOOL_NAMES, resolve_profile

RUNTIME = ROOT / '.runtime' / 'algorithm_review_profiles'

async def verify():
    RUNTIME.mkdir(parents=True, exist_ok=True)
    baseline = json.loads((ROOT/'.runtime/algorithm_review_baseline_tools.json').read_text(encoding='utf-8'))
    report = {'kind':'real_server_stdio_no_blender','profiles':{},'ok':False}
    all_schemas = None
    os.environ.pop("BLENDER_MCP_LOCAL_DOMAINS", None)
    os.environ.pop("BLENDER_MCP_VIEW_DOMAINS", None)
    for profile in ('all','algorithm_review','core','outliner','geometry_nodes','algorithm_reproduction'):
        env = dict(os.environ, BLENDER_MCP_PROFILE=profile, BLENDER_MCP_PORT='1',
                   BLENDER_MCP_TOKEN_FILE=str(RUNTIME/'fixture_token'),
                   BLENDER_MCP_AUDIT_DIR=str(RUNTIME/'audit'))
        async with mcp_stdio_session(sys.executable,[str(ROOT/'server.py')],env=env,cwd=ROOT,
                                    stderr_path=RUNTIME/(profile+'.stderr.log')) as (session, process):
            listed = await asyncio.wait_for(session.list_tools(),20)
            tools = {t.name:t for t in listed.tools}
            expected = KNOWN_TOOL_NAMES if profile=='all' else resolve_profile(profile).tool_names
            assert set(tools)==set(expected),(profile,sorted(set(tools)^set(expected)))
            schemas = {n:{'inputSchema':t.inputSchema,'outputSchema':t.outputSchema} for n,t in tools.items()}
            if profile=='all':
                assert len(baseline)==76
                assert all(schemas[n]==v for n,v in baseline.items()), 'legacy schema changed'
                all_schemas = schemas
            else:
                assert all(s==all_schemas[n] for n,s in schemas.items()), 'profile changed schema'
            view_reply = await session.call_tool("blender_tool_domain_state", {})
            view = view_reply.structuredContent or json.loads(next(c.text for c in view_reply.content if hasattr(c, "text")))
            assert view["all_tools_active"] is True
            assert view["available_tool_count"] == 93
            assert set(view["view_tool_names"]) == resolve_profile(profile).view_tool_names
            assert set(view["selected_domains"]) == set(resolve_profile(profile).domains)
            assert not (await session.call_tool('blender_environment',{})).isError
            report['profiles'][profile]={'count':len(tools),'names':sorted(tools),
                                         "directory_view_count": view["view_tool_count"], "all_tools_active": True,
                                         'pid':process.pid if process else None}
    report.update(ok=True,original_schemas_preserved=len(baseline))
    (RUNTIME/'report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
    print(json.dumps({'ok':True,'counts':{n:p['count'] for n,p in report['profiles'].items()},
                      'original_schemas_preserved':len(baseline)}))

if __name__=='__main__':
    asyncio.run(verify())
