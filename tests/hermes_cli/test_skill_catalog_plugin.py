"""
Tests for skill-catalog-plugin — pre_llm_call catalog injection.
"""
import importlib.util
import json
import sys
import yaml
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_PLUGIN_DIR = _REPO_ROOT / "plugins" / "skill-catalog"
_spec = importlib.util.spec_from_file_location(
    "skill_catalog_plugin",
    _PLUGIN_DIR / "__init__.py",
)
_plugin = importlib.util.module_from_spec(_spec)
sys.modules["skill_catalog_plugin"] = _plugin
_spec.loader.exec_module(_plugin)

_inject_skill_catalog = _plugin._inject_skill_catalog
_load_catalog = _plugin._load_catalog
_score_skill = _plugin._score_skill
_format_inject_block = _plugin._format_inject_block
_read_skill_content = _plugin._read_skill_content
_MAX_INJECT_SKILLS = _plugin._MAX_INJECT_SKILLS


# ══════════════════════════════════════════════════════════════════════════════
# plugin identity
# ══════════════════════════════════════════════════════════════════════════════

def test_plugin_yaml_exists():
    yaml_path = _PLUGIN_DIR / "plugin.yaml"
    assert yaml_path.is_file(), "plugin.yaml must exist"
    raw = yaml.safe_load(yaml_path.read_text())
    assert raw["name"] == "skill-catalog"


def test_plugin_importable():
    assert _plugin is not None


def test_register_registers_pre_llm_call_hook():
    """register(ctx) must register pre_llm_call hook."""
    ctx = _FakeCtx()
    _plugin.register(ctx)
    assert ctx.hooks.get("pre_llm_call") is not None
    assert ctx.hooks["pre_llm_call"] is _inject_skill_catalog


# ══════════════════════════════════════════════════════════════════════════════
# keyword scoring
# ══════════════════════════════════════════════════════════════════════════════

def test_score_skill_hit():
    cfg = {"keywords": ["debug", "报错", "traceback"]}
    assert _score_skill(cfg, "帮我debug这个报错") >= 2


def test_score_skill_miss():
    cfg = {"keywords": ["deploy", "docker"]}
    assert _score_skill(cfg, "今天天气怎么样") == 0


def test_score_skill_empty_keywords():
    cfg = {"keywords": []}
    assert _score_skill(cfg, "debug something") == 0


def test_score_skill_case_insensitive():
    cfg = {"keywords": ["Debug"]}
    assert _score_skill(cfg, "DEBUG is broken") >= 1


# ══════════════════════════════════════════════════════════════════════════════
# inject block formatting
# ══════════════════════════════════════════════════════════════════════════════

def test_format_inject_block():
    skills = [{"name": "test-skill", "score": 5, "content": "TEST_CONTENT"}]
    block = _format_inject_block(skills)
    assert "[Skill Catalog Injected]" in block
    assert "test-skill" in block
    assert "score=5" in block
    assert "TEST_CONTENT" in block
    assert "[/Skill Catalog Injected]" in block


# ══════════════════════════════════════════════════════════════════════════════
# full injection: catalog hit / miss
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def tmp_catalog(tmp_path):
    """Create a minimal temp catalog and skill."""
    skill_dir = tmp_path / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Test Skill\nContent for testing.")

    catalog = {
        "settings": {"max_inject_per_round": 2},
        "catalog": {
            "debug-skill": {
                "tier": "S",
                "path": "test-skill/SKILL.md",
                "keywords": ["debug", "报错", "traceback"],
                "sticky": True,
            },
            "deploy-skill": {
                "tier": "A",
                "path": "test-skill/SKILL.md",
                "keywords": ["deploy", "docker", "k8s"],
            },
        },
    }
    catalog_path = tmp_path / "skills-catalog.yaml"
    catalog_path.write_text(yaml.dump(catalog))
    return tmp_path, catalog_path


def test_inject_hit(monkeypatch, tmp_catalog):
    tmp_path, catalog_path = tmp_catalog
    monkeypatch.setattr(_plugin, "_CATALOG_PATH", catalog_path)
    monkeypatch.setattr(_plugin, "_SKILLS_DIR", tmp_path / "skills")

    result = _inject_skill_catalog(user_message="帮我debug这个报错", session_id="t1")
    assert result is not None
    assert "context" in result
    assert "[Skill Catalog Injected]" in result["context"]
    assert "debug-skill" in result["context"]


def test_inject_miss(monkeypatch, tmp_catalog):
    tmp_path, catalog_path = tmp_catalog
    monkeypatch.setattr(_plugin, "_CATALOG_PATH", catalog_path)
    monkeypatch.setattr(_plugin, "_SKILLS_DIR", tmp_path / "skills")

    result = _inject_skill_catalog(user_message="今天天气不错", session_id="t2")
    assert result is None


def test_top_n_limit(monkeypatch, tmp_path):
    """Max 2 skills injected per round."""
    skill_dir = tmp_path / "skills" / "multi"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Shared\nContent.")

    catalog = {
        "settings": {"max_inject_per_round": 2},
        "catalog": {
            "s1": {"path": "multi/SKILL.md", "keywords": ["debug", "报错"]},
            "s2": {"path": "multi/SKILL.md", "keywords": ["debug", "docker"]},
            "s3": {"path": "multi/SKILL.md", "keywords": ["debug", "k8s"]},
            "s4": {"path": "multi/SKILL.md", "keywords": ["debug", "ci"]},
        },
    }
    cat_path = tmp_path / "skills-catalog.yaml"
    cat_path.write_text(yaml.dump(catalog))

    monkeypatch.setattr(_plugin, "_CATALOG_PATH", cat_path)
    monkeypatch.setattr(_plugin, "_SKILLS_DIR", tmp_path / "skills")

    result = _inject_skill_catalog(
        user_message="debug 报错 docker k8s ci", session_id="t3"
    )
    assert result is not None
    skills_injected = result["context"].count("--- skill:")
    assert skills_injected <= 2, f"Got {skills_injected}, max=2"


def test_exception_returns_none(monkeypatch):
    """Exception in injection must return None, not raise."""
    monkeypatch.setattr(_plugin, "_CATALOG_PATH", Path("/nonexistent/catalog.yaml"))
    result = _inject_skill_catalog(user_message="debug", session_id="t4")
    assert result is None


# ══════════════════════════════════════════════════════════════════════════════
# no external API calls
# ══════════════════════════════════════════════════════════════════════════════

def test_no_network_calls():
    """Plugin source must not import urllib.request or requests."""
    src = (_PLUGIN_DIR / "__init__.py").read_text()
    assert "urllib.request" not in src, "must not call external APIs"
    assert "import requests" not in src, "must not call external APIs"


# ══════════════════════════════════════════════════════════════════════════════
# no cross-contamination with hermes-memory
# ══════════════════════════════════════════════════════════════════════════════

def test_no_memory_references():
    """Skill catalog must not reference active_lessons or event_candidates."""
    src = (_PLUGIN_DIR / "__init__.py").read_text()
    assert "active_lessons" not in src, "must not touch memory system"
    assert "event_candidates" not in src, "must not touch memory system"
    assert "reflections" not in src, "must not touch memory system"


# ══════════════════════════════════════════════════════════════════════════════
# helpers
# ══════════════════════════════════════════════════════════════════════════════

class _FakeCtx:
    def __init__(self):
        self.hooks = {}

    def register_hook(self, hook_name, fn):
        self.hooks[hook_name] = fn
