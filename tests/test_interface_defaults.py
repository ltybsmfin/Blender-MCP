"""Omitted interface defaults must retain Blender's native typed defaults."""
import unittest
from mcp.server.fastmcp import FastMCP
from blender_mcp.models import InterfaceSocketSpec
from blender_mcp.tools.geometry_nodes import register


class InterfaceDefaultTests(unittest.TestCase):
    def forwarded(self, entries):
        class Bridge:
            def call(self, command, params, timeout=None):
                return params
        mcp = FastMCP('interface-default-test')
        register(mcp, Bridge())
        tool = mcp._tool_manager.get_tool('blender_create_node_group')
        return tool.fn(name='Fixture', interface=[InterfaceSocketSpec(**e) for e in entries])['interface']

    def test_omitted_menu_and_numeric_defaults_are_not_sent_as_null(self):
        for entry in self.forwarded([{'name': 'Choice', 'socket_type': 'NodeSocketMenu'},
                                    {'name': 'Width', 'socket_type': 'NodeSocketFloat'}]):
            self.assertNotIn('default_value', entry)

    def test_explicit_empty_datablock_and_numeric_defaults_remain_explicit(self):
        entries = self.forwarded([{'name': 'Object', 'socket_type': 'NodeSocketObject', 'default_value': None},
                                  {'name': 'Width', 'socket_type': 'NodeSocketFloat', 'default_value': 2}])
        self.assertIn('default_value', entries[0])
        self.assertIsNone(entries[0]['default_value'])
        self.assertEqual(entries[1]['default_value'], 2)
