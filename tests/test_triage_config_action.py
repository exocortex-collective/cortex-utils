"""Action fields survive YAML load and the JSONB model_dump round-trip."""

from cortex_utils.triage_config import load_rules_from_string
from cortex_utils.triage_config.models import Action

YAML = """
version: 1
chains:
  main:
    rules:
      - match: {from_glob: "*@example.com"}
        action: {trash: true}
"""


def test_trash_survives_yaml_load_and_storage() -> None:
    action = load_rules_from_string(YAML).chains["main"].rules[0].action
    assert action is not None and action.trash is True
    assert Action.model_validate(action.model_dump()).trash is True


def test_trash_defaults_to_unset() -> None:
    assert Action().trash is None
