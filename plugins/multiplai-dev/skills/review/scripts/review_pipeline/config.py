"""Run configuration: concurrency and the model/effort per stage.

Precedence, highest first (same order as buildme's config.py: the project's
own file beats multiplai.conf, which beats the default):

1. `review.yaml` in the output directory (`<out>/review.yaml`):
   `concurrency`, `finder_model`, `verifier_model`, `merger_model`, `effort`,
   `max_turns`.
2. `multiplai.conf` keys `review_finder_model`, `review_verifier_model`,
   `review_effort` (the merger uses the verifier's).
3. Default: no model and no effort passed, so every stage runs on the
   session's model. Verifiers still each get a fresh context.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from multiplai_core.env import load_multiplai_conf

from .budget import DEFAULT_MAX_USD

log = logging.getLogger(__name__)

DIMENSIONS: tuple[str, ...] = ("diff-bugs", "callers", "history", "conventions", "tests")
DEFAULT_CONCURRENCY = 4
KNOWN_EFFORTS = {"low", "medium", "high", "xhigh", "max"}


@dataclass
class ReviewConfig:
    concurrency: int = DEFAULT_CONCURRENCY
    finder_model: str | None = None
    verifier_model: str | None = None
    merger_model: str | None = None
    effort: str | None = None
    max_turns: int = 60
    max_cost_usd: float | None = DEFAULT_MAX_USD
    dimensions: tuple[str, ...] = field(default=DIMENSIONS)


def _conf_value(conf: dict, key: str) -> str | None:
    for k in (key, key.upper()):
        value = conf.get(k)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def load_config(out_dir: Path | None, *, max_cost_usd: float | None = DEFAULT_MAX_USD) -> ReviewConfig:
    cfg = ReviewConfig(max_cost_usd=max_cost_usd)
    try:
        conf = load_multiplai_conf()
    except Exception as e:  # a broken conf file degrades to defaults
        log.warning("Could not read multiplai.conf (%s); using defaults", e)
        conf = {}
    cfg.finder_model = _conf_value(conf, "review_finder_model")
    cfg.verifier_model = _conf_value(conf, "review_verifier_model")
    cfg.merger_model = cfg.verifier_model
    cfg.effort = _conf_value(conf, "review_effort")

    yaml_path = (out_dir / "review.yaml") if out_dir else None
    if yaml_path and yaml_path.is_file():
        try:
            data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            log.warning("Ignoring unparseable %s: %s", yaml_path, e)
            data = {}
        if not isinstance(data, dict):
            log.warning("Ignoring %s: expected a mapping", yaml_path)
            data = {}
        for key in ("finder_model", "verifier_model", "merger_model", "effort"):
            if data.get(key):
                setattr(cfg, key, str(data[key]))
        if data.get("verifier_model") and not data.get("merger_model"):
            cfg.merger_model = str(data["verifier_model"])
        for key in ("concurrency", "max_turns"):
            if data.get(key) is not None:
                try:
                    setattr(cfg, key, max(1, int(data[key])))
                except (TypeError, ValueError):
                    log.warning("Ignoring %s=%r in %s (want an integer)", key, data[key], yaml_path)

    if cfg.effort and cfg.effort.lower() not in KNOWN_EFFORTS:
        log.warning("Ignoring unknown effort %r (expected one of %s)", cfg.effort, ", ".join(sorted(KNOWN_EFFORTS)))
        cfg.effort = None
    return cfg
