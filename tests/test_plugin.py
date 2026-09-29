# -*- coding: utf-8 -*-
"""Integration tests that load the plugin the way QwenPaw would.

QwenPaw is not installed in this environment, so the QwenPaw and
AgentScope modules the plugin imports are stubbed out. This still
verifies the parts that actually break in practice:

* the plugin module imports cleanly under an isolated namespace,
* ``plugin.py`` exposes a module-level ``plugin`` object with ``register``,
* ``register()`` calls the expected PluginApi methods with valid arguments,
* each tool is an async callable returning a ``ToolChunk``,
* the tool bodies work end to end against a real temporary git repo.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# Minimal stubs for the host environment
# ---------------------------------------------------------------------------


def _install_stubs():
    """Register fake qwenpaw / agentscope modules if the real ones are absent."""

    if "qwenpaw.plugins.api" not in sys.modules:
        qwenpaw = types.ModuleType("qwenpaw")
        plugins = types.ModuleType("qwenpaw.plugins")
        api_mod = types.ModuleType("qwenpaw.plugins.api")

        class PluginApi:  # noqa: D401 - stub
            """Stub PluginApi."""

        api_mod.PluginApi = PluginApi
        api_mod.get_tool_config = lambda name: None
        plugins.api = api_mod
        qwenpaw.plugins = plugins
        sys.modules.setdefault("qwenpaw", qwenpaw)
        sys.modules.setdefault("qwenpaw.plugins", plugins)
        sys.modules.setdefault("qwenpaw.plugins.api", api_mod)

    if "qwenpaw.runtime.commands.control.base" not in sys.modules:
        runtime = types.ModuleType("qwenpaw.runtime")
        commands = types.ModuleType("qwenpaw.runtime.commands")
        control = types.ModuleType("qwenpaw.runtime.commands.control")
        base = types.ModuleType("qwenpaw.runtime.commands.control.base")

        class BaseControlCommandHandler:  # noqa: D401 - stub
            """Stub control-command base class."""

            command_name = ""
            help_text = ""

            async def handle(self, ctx, args: str):  # pragma: no cover
                raise NotImplementedError

        base.BaseControlCommandHandler = BaseControlCommandHandler
        control.base = base
        commands.control = control
        runtime.commands = commands
        sys.modules.setdefault("qwenpaw.runtime", runtime)
        sys.modules.setdefault("qwenpaw.runtime.commands", commands)
        sys.modules.setdefault("qwenpaw.runtime.commands.control", control)
        sys.modules.setdefault(
            "qwenpaw.runtime.commands.control.base", base,
        )

    if "agentscope.tool" not in sys.modules:
        agentscope = types.ModuleType("agentscope")
        message = types.ModuleType("agentscope.message")
        tool = types.ModuleType("agentscope.tool")

        class ToolResultState:  # noqa: D401 - stub
            SUCCESS = "success"
            ERROR = "error"

        class TextBlock:  # noqa: D401 - stub
            def __init__(self, **kw):
                self.__dict__.update(kw)

        class Msg:  # noqa: D401 - stub
            def __init__(self, **kw):
                self.__dict__.update(kw)

        class ToolChunk:  # noqa: D401 - stub
            def __init__(self, state=None, content=None, **kw):
                self.state = state
                self.content = content or []
                self.__dict__.update(kw)

        message.TextBlock = TextBlock
        message.ToolResultState = ToolResultState
        message.Msg = Msg
        tool.ToolChunk = ToolChunk
        agentscope.message = message
        agentscope.tool = tool
        sys.modules.setdefault("agentscope", agentscope)
        sys.modules.setdefault("agentscope.message", message)
        sys.modules.setdefault("agentscope.tool", tool)


_install_stubs()


def _load_plugin_module():
    """Import plugin.py the way an isolated plugin loader would."""
    name = "crc_plugin_under_test"
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(PLUGIN_DIR, "plugin.py"),
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class RecordingApi:
    """Stand-in PluginApi that records what the plugin registers."""

    def __init__(self):
        self.tools = []
        self.commands = []
        self.plugin_id = "code-review-copilot"

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)

    def register_control_command(self, **kwargs):
        self.commands.append(kwargs)

    def register_startup_hook(self, **kwargs):  # pragma: no cover
        pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestManifest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with open(
            os.path.join(PLUGIN_DIR, "plugin.json"), encoding="utf-8",
        ) as fh:
            cls.manifest = json.load(fh)

    def test_required_fields(self):
        for key in ("id", "version", "entry"):
            self.assertIn(key, self.manifest)

    def test_declares_version_constraint(self):
        # 43 marketplace plugins omit this and get filtered out on 2.x.
        self.assertIn("qwenpaw_version", self.manifest)
        self.assertIn("min", self.manifest["qwenpaw_version"])

    def test_zero_dependencies(self):
        self.assertEqual(self.manifest["dependencies"], [])

    def test_entry_points_at_existing_file(self):
        backend = self.manifest["entry"]["backend"]
        self.assertTrue(
            os.path.isfile(os.path.join(PLUGIN_DIR, backend)),
            f"entry backend {backend} does not exist",
        )

    def test_declared_tools_match_registered_names(self):
        declared = {t["name"] for t in self.manifest["meta"]["tools"]}
        module = _load_plugin_module()
        api = RecordingApi()
        module.plugin.register(api)
        registered = {t["tool_name"] for t in api.tools}
        self.assertEqual(declared, registered)


class TestRegistration(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.module = _load_plugin_module()

    def test_plugin_object_exposes_register(self):
        self.assertTrue(hasattr(self.module, "plugin"))
        self.assertTrue(callable(self.module.plugin.register))

    def test_registers_three_tools(self):
        api = RecordingApi()
        self.module.plugin.register(api)
        self.assertEqual(len(api.tools), 3)
        names = {t["tool_name"] for t in api.tools}
        self.assertEqual(
            names,
            {"review_git_diff", "review_rev_range", "review_context"},
        )

    def test_tools_declare_honest_governance_type(self):
        """Tools shell out to git, so they must declare shell governance."""
        api = RecordingApi()
        self.module.plugin.register(api)
        for tool in api.tools:
            self.assertEqual(tool["tool_type"], "shell", tool["tool_name"])
            self.assertEqual(tool["target_param"], "command")

    def test_every_tool_is_async_callable(self):
        api = RecordingApi()
        self.module.plugin.register(api)
        for tool in api.tools:
            self.assertTrue(
                asyncio.iscoroutinefunction(tool["tool_func"]),
                f"{tool['tool_name']} must be async",
            )

    def test_registers_slash_command(self):
        api = RecordingApi()
        self.module.plugin.register(api)
        self.assertEqual(len(api.commands), 1)
        handler = api.commands[0]["handler"]
        self.assertEqual(handler.command_name, "review")

    def test_registration_is_idempotent_across_calls(self):
        api1, api2 = RecordingApi(), RecordingApi()
        self.module.plugin.register(api1)
        self.module.plugin.register(api2)
        self.assertEqual(len(api1.tools), len(api2.tools))


class TestToolBehaviour(unittest.TestCase):
    """Drive the real tools against a real temporary repository."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = self._tmp.name
        self._git("init")
        self._git("config", "user.email", "t@example.com")
        self._git("config", "user.name", "Tester")
        self._git("checkout", "-b", "main")
        self._write("app.py", "def f(x):\n    return x\n")
        self._git("add", ".")
        self._git("commit", "-m", "init")

    def tearDown(self):
        self._tmp.cleanup()

    def _git(self, *args):
        subprocess.run(
            ["git", *args], cwd=self.repo, capture_output=True, check=True,
        )

    def _write(self, rel, text):
        path = os.path.join(self.repo, rel)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def _call(self, coro):
        return asyncio.run(coro)

    def test_review_git_diff_finds_problems(self):
        module = _load_plugin_module()
        self._write(
            "app.py",
            "API_KEY = 'sk-abcdefghijklmnop'\n"
            "def f(x):\n"
            "    return eval(x)\n",
        )
        chunk = self._call(
            module.review_git_diff(repo_dir=self.repo),
        )
        text = chunk.content[0].text
        self.assertIn("Critical", text)
        self.assertIn("app.py", text)

    def test_review_git_diff_clean_repo(self):
        module = _load_plugin_module()
        chunk = self._call(module.review_git_diff(repo_dir=self.repo))
        self.assertIn("未发现规则问题", chunk.content[0].text)

    def test_review_git_diff_on_non_repo_returns_error(self):
        module = _load_plugin_module()
        with tempfile.TemporaryDirectory() as plain:
            chunk = self._call(module.review_git_diff(repo_dir=plain))
        self.assertEqual(chunk.state, "error")
        self.assertIn("Not a git repository", chunk.content[0].text)

    def test_review_rev_range(self):
        module = _load_plugin_module()
        self._write("app.py", "def f(x):\n    return x\nimport pickle\n")
        self._git("add", ".")
        self._git("commit", "-m", "add pickle")
        chunk = self._call(
            module.review_rev_range(
                rev_range="HEAD~1..HEAD", repo_dir=self.repo,
            ),
        )
        self.assertNotEqual(chunk.state, "error")

    def test_review_rev_range_rejects_empty(self):
        module = _load_plugin_module()
        chunk = self._call(module.review_rev_range(rev_range="   "))
        self.assertEqual(chunk.state, "error")
        self.assertIn("must not be empty", chunk.content[0].text)

    def test_review_context_includes_added_lines(self):
        module = _load_plugin_module()
        self._write("app.py", "def f(x):\n    return x\n# UNIQUE_MARKER\n")
        chunk = self._call(
            module.review_context(repo_dir=self.repo),
        )
        text = chunk.content[0].text
        self.assertIn("UNIQUE_MARKER", text)
        self.assertIn("file:line", text)  # instructs the model how to report

    def test_ignore_patterns_are_honoured(self):
        module = _load_plugin_module()
        self._write("vendor/lib.py", "x = eval(y)\n")
        chunk = self._call(
            module.review_git_diff(repo_dir=self.repo, ignore="^vendor/"),
        )
        self.assertIn("未发现规则问题", chunk.content[0].text)

    def test_slash_command_handler_returns_message(self):
        module = _load_plugin_module()
        api = RecordingApi()
        module.plugin.register(api)
        handler = api.commands[0]["handler"]
        msg = self._call(handler.handle(None, ""))
        self.assertTrue(hasattr(msg, "content"))

    def test_default_repo_dir_falls_back_gracefully(self):
        """No repo_dir must not raise even when WORKING_DIR is absent."""
        module = _load_plugin_module()
        chunk = self._call(module.review_git_diff(repo_dir=""))
        self.assertIsNotNone(chunk)


if __name__ == "__main__":
    unittest.main(verbosity=2)
