"""Tests for Block 1: Rename context_router.py -> context_manager.py.

Covers:
- File exists at new path, absent at old path
- hooks.json references context_manager.py (not context_router.py)
- Zero remaining references to context_router across the entire plugin
- All imports updated to use context_manager
- _read_catalog_or_scan() stub method with fail-open fallback signature (Decision 8)
- No functional behavior change from the rename
- Internal self-references updated to context_manager
- Public interface preserved (same functions, same signatures)

Tests are written against behavioral contracts from the spec. All tests
should FAIL until the implementation is complete.
"""

import importlib
import importlib.util
import inspect
import json
import os
import re
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from conftest import PLUGIN_ROOT, SCRIPTS_DIR, HOOKS_JSON, parse_hooks

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CONTEXT_MANAGER_PATH = SCRIPTS_DIR / "context_manager.py"
CONTEXT_ROUTER_PATH = SCRIPTS_DIR / "context_router.py"


def _make_memory_dir(tmp_path: Path, files: dict[str, str] | None = None) -> Path:
    """Create a temporary memory directory with optional files."""
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    if files:
        for name, content in files.items():
            (mem_dir / name).write_text(content)
    return mem_dir


def _import_context_manager():
    """Dynamically import context_manager module."""
    from multiplai_core.paths import _reset_cache
    _reset_cache()
    spec = importlib.util.spec_from_file_location("context_manager", CONTEXT_MANAGER_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot import context_manager from {CONTEXT_MANAGER_PATH}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_EXCLUDE_DIRS = {"__pycache__", ".git", "node_modules", "data", "venv",
                  ".pytest_cache", ".venv", "specs"}


def _all_plugin_files(suffix: str) -> list[Path]:
    """Collect every file with *suffix* (e.g. ".py", ".md") in the plugin,
    skipping directories that aren't part of the source tree."""
    result = []
    for root, dirs, files in os.walk(PLUGIN_ROOT):
        dirs[:] = [d for d in dirs if d not in _EXCLUDE_DIRS]
        for f in files:
            if f.endswith(suffix):
                result.append(Path(root) / f)
    return result


def _all_plugin_py_files() -> list[Path]:
    """Collect every .py file in the plugin (scripts/, tests/, etc.)."""
    return _all_plugin_files(".py")


def _all_plugin_md_files() -> list[Path]:
    """Collect every .md file in the plugin (skills/, root docs)."""
    return _all_plugin_files(".md")


# ===========================================================================
# 1. FILE EXISTS AT NEW PATH, ABSENT AT OLD PATH
# ===========================================================================


class TestFileRename:
    """The file context_router.py must be renamed to context_manager.py."""

    def test_context_manager_exists(self):
        """WHEN the plugin repository is checked after the rename
        THEN scripts/context_manager.py exists."""
        assert CONTEXT_MANAGER_PATH.is_file(), \
            f"scripts/context_manager.py must exist at {CONTEXT_MANAGER_PATH}"

    def test_context_router_does_not_exist(self):
        """WHEN the plugin repository is checked after the rename
        THEN scripts/context_router.py does NOT exist."""
        assert not CONTEXT_ROUTER_PATH.is_file(), \
            f"scripts/context_router.py must be removed after rename"

    def test_context_manager_is_python(self):
        """WHEN scripts/context_manager.py is checked
        THEN it is a valid Python file that compiles without errors."""
        import py_compile
        assert CONTEXT_MANAGER_PATH.is_file(), "context_manager.py must exist"
        py_compile.compile(str(CONTEXT_MANAGER_PATH), doraise=True)


# ===========================================================================
# 2. HOOKS.JSON UPDATED
# ===========================================================================


class TestHooksJsonUpdated:
    """All references to context_router.py in hooks.json must be updated."""

    @pytest.fixture(autouse=True)
    def load_hooks(self):
        self.hooks = parse_hooks()

    def test_hooks_json_references_context_manager(self):
        """WHEN hooks/hooks.json is parsed after the rename
        THEN a UserPromptSubmit hook references scripts/context_manager.py."""
        user_prompt_hooks = [
            h for h in self.hooks if h["event"] == "UserPromptSubmit"
        ]
        assert len(user_prompt_hooks) > 0, "No UserPromptSubmit hook found"
        assert any(h["script"] == "scripts/context_manager.py"
                   for h in user_prompt_hooks), \
            "hooks.json must have a UserPromptSubmit hook -> scripts/context_manager.py"

    def test_no_stale_context_router_in_hooks_json(self):
        """WHEN hooks/hooks.json is searched for 'context_router'
        THEN zero matches are found (no command string, no script path)."""
        hooks_text = HOOKS_JSON.read_text()
        assert "context_router" not in hooks_text
        assert "context-router" not in hooks_text
        for h in self.hooks:
            assert "context_router" not in h["command"]
            assert "context-router" not in h["command"]
            assert h["script"] != "scripts/context_router.py"

    def test_hook_script_path_exists(self):
        """WHEN the context_manager hook entry references a script
        THEN that script file exists in the plugin directory."""
        for h in self.hooks:
            if h["event"] == "UserPromptSubmit" and h["script"] == "scripts/context_manager.py":
                script_path = PLUGIN_ROOT / h["script"]
                assert script_path.is_file(), f"Hook script not found: {script_path}"
                return
        pytest.fail("context_manager hook not found in hooks.json")

    def test_hook_timeout_preserved(self):
        """WHEN the context_manager hook entry is inspected
        THEN its timeout is 30 (seconds, official schema) — headroom for
        the opt-in inline llm router (router timeout 25s + margin)."""
        for h in self.hooks:
            if h["event"] == "UserPromptSubmit" and h["script"] == "scripts/context_manager.py":
                assert h["timeout"] == 30, \
                    f"Expected 30s timeout, got {h['timeout']}"
                return
        pytest.fail("context_manager hook not found in hooks.json")


# ===========================================================================
# 3. ZERO REMAINING REFERENCES ACROSS PLUGIN (GREP AUDIT)
# ===========================================================================


class TestZeroContextRouterReferences:
    """No file in the plugin may reference 'context_router' after the rename,
    except for specs/archive docs and comments explicitly documenting the rename history."""

    # Files that are allowed to mention context_router (specs/design docs, archives,
    # and this test file itself which tests the rename)
    EXEMPT_PATHS = {"specs/", "build-progress.md", "tests/test_context_manager_rename.py"}

    def _is_exempt(self, filepath: Path) -> bool:
        rel = str(filepath.relative_to(PLUGIN_ROOT))
        return any(rel.startswith(e) for e in self.EXEMPT_PATHS)

    def test_no_context_router_in_python_files(self):
        """WHEN all .py files under the plugin directory are searched
        THEN zero matches for 'context_router' are found."""
        violations = []
        for py_file in _all_plugin_py_files():
            if self._is_exempt(py_file):
                continue
            text = py_file.read_text()
            if "context_router" in text:
                rel = py_file.relative_to(PLUGIN_ROOT)
                # Find specific line numbers
                for line_no, line in enumerate(text.splitlines(), 1):
                    if "context_router" in line:
                        violations.append(f"  {rel}:{line_no}: {line.strip()[:80]}")
        assert len(violations) == 0, \
            f"'context_router' found in Python files:\n" + "\n".join(violations)

    def test_no_context_router_in_hooks_json(self):
        """WHEN hooks/hooks.json is searched for 'context_router'
        THEN zero matches are found."""
        text = HOOKS_JSON.read_text()
        assert "context_router" not in text, \
            "hooks.json still contains 'context_router'"

    def test_no_context_router_in_plugin_json(self):
        """WHEN plugin.json is searched for 'context_router'
        THEN zero matches are found."""
        text = (PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text()
        assert "context_router" not in text, \
            "plugin.json still contains 'context_router'"

    def test_no_context_router_in_skill_files(self):
        """WHEN all .md files under skills/ are searched for 'context_router'
        THEN zero matches are found."""
        skills_dir = PLUGIN_ROOT / "skills"
        if not skills_dir.exists():
            return
        violations = []
        for md_file in skills_dir.glob("*.md"):
            text = md_file.read_text()
            if "context_router" in text:
                violations.append(md_file.name)
        assert len(violations) == 0, \
            f"'context_router' found in skill files: {violations}"

    def test_no_context_router_in_markdown_docs(self):
        """WHEN all non-spec markdown files are searched for 'context_router'
        THEN zero matches are found."""
        violations = []
        for md_file in _all_plugin_md_files():
            if self._is_exempt(md_file):
                continue
            text = md_file.read_text()
            if "context_router" in text:
                rel = md_file.relative_to(PLUGIN_ROOT)
                violations.append(str(rel))
        assert len(violations) == 0, \
            f"'context_router' found in docs: {violations}"

    def test_no_context_router_in_test_files(self):
        """WHEN all test files under tests/ are searched for 'context_router'
        THEN zero matches are found (excluding this test file and comments)."""
        violations = []
        tests_dir = PLUGIN_ROOT / "tests"
        for test_file in tests_dir.glob("test_*.py"):
            # This test file itself is allowed to reference context_router
            if test_file.name == "test_context_manager_rename.py":
                continue
            text = test_file.read_text()
            if "context_router" in text:
                for line_no, line in enumerate(text.splitlines(), 1):
                    stripped = line.strip()
                    # Allow comments that document the rename
                    if stripped.startswith("#"):
                        continue
                    if "context_router" in line:
                        rel = test_file.relative_to(PLUGIN_ROOT)
                        violations.append(f"  {rel}:{line_no}: {stripped[:80]}")
        assert len(violations) == 0, \
            f"'context_router' found in test files:\n" + "\n".join(violations)


# ===========================================================================
# 4. IMPORTS RESOLVE AFTER RENAME
# ===========================================================================


class TestImportsResolve:
    """Any Python import that previously imported from context_router must now
    import from context_manager."""

    def test_context_manager_importable(self):
        """WHEN context_manager is imported
        THEN it loads without ModuleNotFoundError."""
        assert CONTEXT_MANAGER_PATH.is_file(), "context_manager.py must exist"
        mod = _import_context_manager()
        assert mod is not None

    def test_context_manager_exposes_main(self):
        """WHEN context_manager is imported
        THEN it exposes a main() function."""
        mod = _import_context_manager()
        assert hasattr(mod, "main"), "context_manager must expose main()"
        assert callable(mod.main)

    def test_context_manager_exposes_ranking(self):
        """WHEN context_manager is imported
        THEN it exposes _rank_memory_files."""
        mod = _import_context_manager()
        assert hasattr(mod, "_rank_memory_files"), \
            "context_manager must expose _rank_memory_files"

    def test_context_manager_exposes_read_top(self):
        """WHEN context_manager is imported
        THEN it exposes _read_top_memory_files."""
        mod = _import_context_manager()
        assert hasattr(mod, "_read_top_memory_files"), \
            "context_manager must expose _read_top_memory_files"

    def test_context_manager_exposes_ranked_file(self):
        """WHEN context_manager is imported
        THEN it exposes the RankedFile dataclass."""
        mod = _import_context_manager()
        assert hasattr(mod, "RankedFile"), \
            "context_manager must expose RankedFile dataclass"


# ===========================================================================
# 5. INTERNAL SELF-REFERENCES UPDATED
# ===========================================================================


class TestSelfReferencesUpdated:
    """If context_router.py contained self-referential strings (logging,
    __name__ comparisons, error messages), they must be updated."""

    def test_no_hardcoded_context_router_in_module(self):
        """WHEN scripts/context_manager.py is searched for 'context_router'
        THEN zero matches are found."""
        assert CONTEXT_MANAGER_PATH.is_file(), "context_manager.py must exist"
        text = CONTEXT_MANAGER_PATH.read_text()
        assert "context_router" not in text, \
            "context_manager.py must not contain any 'context_router' string"

    def test_logger_name_is_context_manager(self):
        """WHEN scripts/context_manager.py creates a logger
        THEN the logger name is 'context_manager' not 'context_router'."""
        assert CONTEXT_MANAGER_PATH.is_file(), "context_manager.py must exist"
        text = CONTEXT_MANAGER_PATH.read_text()
        # The original had: setup_logging("context_router")
        assert re.search(r"""setup_logging\(\s*["']context_manager["']""", text), \
            "Logger must use 'context_manager' as name"
        assert "context_router" not in re.findall(
            r"""setup_logging\(\s*["'](\w+)["']""", text)

    def test_docstring_updated(self):
        """WHEN the module docstring of context_manager.py is read
        THEN it references 'context manager' not 'context router'."""
        assert CONTEXT_MANAGER_PATH.is_file(), "context_manager.py must exist"
        text = CONTEXT_MANAGER_PATH.read_text()
        # Original docstring said "Context router hook"
        assert "context_router" not in text.split('"""')[1] if '"""' in text else True, \
            "Module docstring must not reference 'context_router'"


# ===========================================================================
# 6. NO FUNCTIONAL BEHAVIOR CHANGE
# ===========================================================================


class TestNoFunctionalChange:
    """The rename is strictly a refactor. No business logic changes."""

    def test_public_interface_matches_original(self):
        """WHEN the set of public functions in context_manager.py is checked
        THEN it contains all functions that were in context_router.py."""
        assert CONTEXT_MANAGER_PATH.is_file(), "context_manager.py must exist"
        mod = _import_context_manager()
        expected_names = [
            "main",
            "RankedFile",
            "_iter_markdown_files",
            "_rank_memory_files",
            "_read_top_memory_files",
        ]
        for name in expected_names:
            assert hasattr(mod, name), \
                f"context_manager.py must expose '{name}' from original context_router.py"

    def test_ranking_still_works(self, tmp_path):
        """WHEN context_manager._rank_memory_files is called with memory files
        THEN it returns ranked results just like the original."""
        mem_dir = tmp_path / "memory"
        mem_dir.mkdir()
        (mem_dir / "recent.md").write_text("# Recent file\nContent here.")
        (mem_dir / "old.md").write_text("# Old file\nContent here too.")
        os.utime(mem_dir / "old.md",
                 (time.time() - 86400 * 30, time.time() - 86400 * 30))

        mod = _import_context_manager()
        ranked = mod._rank_memory_files(mem_dir)
        assert isinstance(ranked, list)
        assert len(ranked) == 2

    def test_read_top_memory_files_returns_contents(self, tmp_path):
        """WHEN context_manager._read_top_memory_files is called
        THEN it returns a dict of filename->content."""
        mem_dir = tmp_path / "memory"
        mem_dir.mkdir()
        (mem_dir / "me.md").write_text("# About Me\nI am a developer.")

        mod = _import_context_manager()
        result = mod._read_top_memory_files(mem_dir)
        assert isinstance(result, dict)
        assert "me.md" in result
        assert "About Me" in result["me.md"]

    def test_read_top_memory_files_still_works(self, tmp_path):
        """WHEN context_manager._read_top_memory_files is called
        THEN it returns at most max_files entries."""
        mem_dir = tmp_path / "memory"
        mem_dir.mkdir()
        for i in range(10):
            (mem_dir / f"mem-{i:02d}.md").write_text(f"# Memory {i}\nContent.")

        mod = _import_context_manager()
        result = mod._read_top_memory_files(mem_dir, max_files=3)
        assert isinstance(result, dict)
        assert len(result) <= 3

    def test_empty_memory_dir_returns_empty_dict(self, tmp_path):
        """WHEN _read_top_memory_files is called on empty dir
        THEN it returns empty dict."""
        mem_dir = tmp_path / "empty_memory"
        mem_dir.mkdir()

        mod = _import_context_manager()
        result = mod._read_top_memory_files(mem_dir)
        assert result == {}

    def test_missing_memory_dir_returns_empty_dict(self, tmp_path):
        """WHEN _read_top_memory_files is called on nonexistent dir
        THEN it returns empty dict without raising."""
        nonexistent = tmp_path / "does_not_exist"

        mod = _import_context_manager()
        result = mod._read_top_memory_files(nonexistent)
        assert result == {}

    def test_main_entrypoint_guard_present(self):
        """WHEN context_manager.py is inspected
        THEN it has if __name__ == '__main__' guard."""
        text = CONTEXT_MANAGER_PATH.read_text()
        assert "__name__" in text and "__main__" in text, \
            "context_manager.py must have __name__ == '__main__' guard"


# ===========================================================================
# 7. VENV GUARD AND PATH SETUP PRESERVED
# ===========================================================================


class TestVenvGuardPreserved:
    """Post-uv-migration: context_manager.py runs via `uv run --project`
    against the shared workspace; the old venv guard preamble is gone, PEP 723
    inline metadata was retired on 2026-08-04, and core imports resolve
    through multiplai_core."""

    def test_has_no_pep723_header(self):
        """WHEN context_manager.py is inspected
        THEN it carries no PEP 723 inline-metadata header — its dependencies
        come from scripts/pyproject.toml, resolved once from uv.lock."""
        text = CONTEXT_MANAGER_PATH.read_text()
        assert "# /// script" not in text, \
            "context_manager.py carries a PEP 723 header again"

    def test_no_venv_guard(self):
        """WHEN context_manager.py is inspected
        THEN the retired venv guard preamble is gone."""
        text = CONTEXT_MANAGER_PATH.read_text()
        assert "venv_guard" not in text and "ensure_venv_python" not in text, \
            "context_manager.py must not reference the retired venv guard"

    def test_uses_core_paths(self):
        """WHEN context_manager.py is inspected
        THEN it imports from multiplai_core.paths."""
        text = CONTEXT_MANAGER_PATH.read_text()
        assert "from multiplai_core.paths" in text or "multiplai_core.paths" in text, \
            "context_manager.py must import from multiplai_core.paths"

    def test_delegates_llm_routing_to_router(self):
        """WHEN context_manager.py is inspected
        THEN it reaches the model client through the router abstraction
        (``create_router`` in ``lib.memory_router``), not by importing the
        model client directly — the LLM call only fires on the ``llm``
        router path, inside memory_router, never on the hot hook path here."""
        text = CONTEXT_MANAGER_PATH.read_text()
        assert "create_router" in text or "memory_router" in text, \
            "context_manager.py must delegate routing to lib.memory_router"


# ===========================================================================
# 8. _read_catalog_or_scan() STUB (Decision 8)
# ===========================================================================


class TestReadCatalogOrScanStub:
    """context_manager.py must include a _read_catalog_or_scan() stub method
    with fail-open fallback signature per Decision 8.

    The stub is the foundation for catalog-first read paths added in later blocks.
    """

    def test_read_catalog_or_scan_exists(self):
        """WHEN context_manager.py is imported
        THEN it has a _read_catalog_or_scan function or method."""
        mod = _import_context_manager()
        assert hasattr(mod, "_read_catalog_or_scan"), \
            "context_manager.py must expose _read_catalog_or_scan()"

    def test_read_catalog_or_scan_is_callable(self):
        """WHEN _read_catalog_or_scan is accessed
        THEN it is callable."""
        mod = _import_context_manager()
        assert callable(mod._read_catalog_or_scan), \
            "_read_catalog_or_scan must be callable"

    def test_read_catalog_or_scan_accepts_catalog_type(self):
        """WHEN _read_catalog_or_scan signature is inspected
        THEN it accepts a catalog_type parameter (string)."""
        mod = _import_context_manager()
        sig = inspect.signature(mod._read_catalog_or_scan)
        params = list(sig.parameters.keys())
        # Should have catalog_type as first param (after self if it's a method)
        assert "catalog_type" in params, \
            "_read_catalog_or_scan must accept catalog_type parameter"

    def test_read_catalog_or_scan_returns_list(self):
        """WHEN _read_catalog_or_scan is called with a catalog type
        THEN it returns a list (of ContextEntry or equivalent)."""
        mod = _import_context_manager()
        # The stub should return a list, possibly empty
        result = mod._read_catalog_or_scan("memory")
        assert isinstance(result, list), \
            "_read_catalog_or_scan must return a list"

    def test_read_catalog_or_scan_fallback_on_missing_catalog(self):
        """WHEN _read_catalog_or_scan is called and no catalog file exists
        THEN it falls back gracefully (returns list, no exception)."""
        mod = _import_context_manager()
        # With no catalog present, should fall back without error
        result = mod._read_catalog_or_scan("memory")
        assert isinstance(result, list), \
            "_read_catalog_or_scan must return a list on fallback"

    def test_read_catalog_or_scan_fallback_on_invalid_type(self):
        """WHEN _read_catalog_or_scan is called with an unknown catalog type
        THEN it handles it gracefully (returns empty list or raises clear error)."""
        mod = _import_context_manager()
        # Should not crash on an unknown type
        try:
            result = mod._read_catalog_or_scan("nonexistent_type")
            assert isinstance(result, list)
        except (ValueError, KeyError):
            pass  # Also acceptable to raise a clear error

    def test_read_catalog_or_scan_docstring(self):
        """WHEN _read_catalog_or_scan's docstring is inspected
        THEN it documents the fail-open fallback behavior."""
        mod = _import_context_manager()
        doc = mod._read_catalog_or_scan.__doc__
        assert doc is not None, "_read_catalog_or_scan must have a docstring"
        doc_lower = doc.lower()
        assert "catalog" in doc_lower, \
            "Docstring must mention catalog"
        assert "fall" in doc_lower or "fallback" in doc_lower or "scan" in doc_lower, \
            "Docstring must mention fallback or scanning behavior"


# ===========================================================================
# 9. TEST FILE REFERENCES UPDATED
# ===========================================================================


class TestTestFileReferencesUpdated:
    """Test files that previously referenced context_router must be updated
    or replaced to reference context_manager."""

    def test_no_test_context_router_hook_file(self):
        """WHEN the tests directory is checked
        THEN test_context_router_hook.py either doesn't exist or has been
        renamed/updated to test_context_manager_hook.py."""
        old_test = PLUGIN_ROOT / "tests" / "test_context_router_hook.py"
        if old_test.exists():
            text = old_test.read_text()
            # If it still exists, it must not have non-comment references
            non_comment_refs = []
            for line_no, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if "context_router" in line and "context_manager" not in line:
                    non_comment_refs.append(f"  line {line_no}: {stripped[:80]}")
            assert len(non_comment_refs) == 0, \
                f"test_context_router_hook.py has active context_router references:\n" + \
                "\n".join(non_comment_refs)

    def test_test_script_port_updated(self):
        """WHEN tests/test_script_port.py is checked
        THEN EXPECTED_SCRIPTS list references context_manager.py not context_router.py."""
        test_file = PLUGIN_ROOT / "tests" / "test_script_port.py"
        if test_file.exists():
            text = test_file.read_text()
            # The EXPECTED_SCRIPTS list must reference the new name
            if "context_router" in text:
                # Find non-comment, non-string-literal references
                for line_no, line in enumerate(text.splitlines(), 1):
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        continue
                    if "context_router" in line:
                        pytest.fail(
                            f"test_script_port.py:{line_no} still references "
                            f"'context_router': {stripped[:80]}"
                        )

    def test_integration_wiring_updated(self):
        """WHEN tests/test_integration_wiring.py is checked
        THEN it references context_manager.py not context_router.py in
        lifecycle tests and script invocations."""
        test_file = PLUGIN_ROOT / "tests" / "test_integration_wiring.py"
        if test_file.exists():
            text = test_file.read_text()
            # Find non-comment code references to context_router
            for line_no, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if "context_router" in line:
                    pytest.fail(
                        f"test_integration_wiring.py:{line_no} still references "
                        f"'context_router': {stripped[:80]}"
                    )


# ===========================================================================
# 10. HOOK EXECUTION WORKS AFTER RENAME
# ===========================================================================


class TestHookExecutionAfterRename:
    """The hook must still execute correctly after the rename."""

    def test_hook_script_path_matches_actual_file(self):
        """WHEN hooks.json declares UserPromptSubmit scripts
        THEN each path resolves to an actual file, and the renamed
        context_manager.py is among them (checkpoint_nudge.py also runs
        on this event since the checkpoint feature)."""
        ups = [h for h in parse_hooks() if h["event"] == "UserPromptSubmit"]
        for h in ups:
            script_path = PLUGIN_ROOT / h["script"]
            assert script_path.is_file(), \
                f"Hook script does not exist: {h['script']}"
        assert any("context_manager" in h["script"] for h in ups), \
            "UserPromptSubmit hooks must include context_manager"

    def test_stdin_json_accepted(self, tmp_path, monkeypatch, reset_paths_cache):
        """WHEN context_manager.py receives valid JSON on stdin
        THEN it processes without crashing (validates basic execution)."""
        import subprocess

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        (memory_dir / "me.md").write_text("# About Me\nDeveloper.")

        env = os.environ.copy()
        for k in list(env):
            if k.startswith("CLAUDE_PLUGIN"):
                del env[k]
        env["CLAUDE_PLUGIN_ROOT"] = str(PLUGIN_ROOT)
        env["CLAUDE_PLUGIN_DATA"] = str(data_dir)
        env["CLAUDE_PLUGIN_OPTION_MEMORY_DIR"] = str(memory_dir)

        input_data = json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "test"})
        result = subprocess.run(
            [sys.executable, str(CONTEXT_MANAGER_PATH)],
            input=input_data,
            capture_output=True, text=True,
            env=env,
            timeout=15,
        )
        # Allow venv-related failures but not crashes
        if result.returncode != 0:
            assert "Traceback" not in result.stderr or "venv" in result.stderr.lower(), \
                f"context_manager.py crashed: {result.stderr[:500]}"


# ===========================================================================
# 11. SCORING CONSTANTS AND ALGORITHM PRESERVED
# ===========================================================================


class TestScoringPreserved:
    """The metadata-first ranking algorithm and constants must be unchanged."""

    def test_recency_weight_preserved(self):
        """WHEN context_manager.py is inspected
        THEN _RECENCY_WEIGHT is 0.7."""
        mod = _import_context_manager()
        assert hasattr(mod, "_RECENCY_WEIGHT")
        assert mod._RECENCY_WEIGHT == 0.7

    def test_size_weight_preserved(self):
        """WHEN context_manager.py is inspected
        THEN _SIZE_WEIGHT is 0.3."""
        mod = _import_context_manager()
        assert hasattr(mod, "_SIZE_WEIGHT")
        assert mod._SIZE_WEIGHT == 0.3

    def test_recency_decay_days_preserved(self):
        """WHEN context_manager.py is inspected
        THEN _RECENCY_DECAY_DAYS is 60."""
        mod = _import_context_manager()
        assert hasattr(mod, "_RECENCY_DECAY_DAYS")
        assert mod._RECENCY_DECAY_DAYS == 60

    def test_size_norm_bytes_preserved(self):
        """WHEN context_manager.py is inspected
        THEN _SIZE_NORM_BYTES is 10_000."""
        mod = _import_context_manager()
        assert hasattr(mod, "_SIZE_NORM_BYTES")
        assert mod._SIZE_NORM_BYTES == 10_000
