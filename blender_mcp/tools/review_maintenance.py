"""Fixed bridge maintenance, independent of the restricted review profile."""
from mcp.types import ToolAnnotations
from ._common import call


def register(mcp, bridge):
    @mcp.tool(name='blender_reload_review_bridge',
              description='Reload ONLY the five fixed local review bridge modules after a source fix. Takes no code, file or module argument; preserves the resident listener, token and project, never saves .blend. Requires its maintenance command already bootstrapped once. Not exposed by algorithm_review.',
              annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,
                                          idempotentHint=False,openWorldHint=False))
    def blender_reload_review_bridge() -> dict:
        return call(bridge,'reload_review_commands',{})
