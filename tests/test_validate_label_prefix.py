"""validate_rules refuses an add_label/remove_label the labeling worker would
drop (cortex-gvzf)."""

from cortex_utils.triage_config import validate_rules
from cortex_utils.triage_config.models import (
    Action,
    ChainConfig,
    LLMConfig,
    MatchCondition,
    Rule,
    RulesConfig,
)


def _config(prefix: str, action: Action, routed: Action | None = None) -> RulesConfig:
    rules = [Rule(match=MatchCondition(), action=action)]
    if routed:
        rules.append(
            Rule(llm=LLMConfig(model="m", prompt_version="v1"), routes={"x": routed})
        )
    return RulesConfig(label_prefix=prefix, chains={"main": ChainConfig(rules=rules)})


def test_an_add_label_outside_the_prefix_is_refused() -> None:
    errors = validate_rules(_config("Cortex", Action(add_label="Kickstarter/Creators")))
    assert len(errors) == 1 and "Kickstarter/Creators" in errors[0]
    assert "add_label" in errors[0]


def test_each_name_in_a_list_and_remove_label_are_checked() -> None:
    action = Action(add_label=["Cortex/A", "B"], remove_label=["Cortex/C", "D/E"])
    errors = validate_rules(_config("Cortex", action))
    assert sorted(e.split("'")[3] for e in errors) == ["B", "D/E"]


def test_a_route_add_label_is_checked() -> None:
    errors = validate_rules(_config("Cortex", Action(label="L"), Action(add_label="X/Y")))
    assert len(errors) == 1 and "route 'x'" in errors[0]


def test_names_under_the_prefix_pass() -> None:
    action = Action(add_label=["Cortex/Kickstarter/{{project}}"], remove_label="Cortex/Old")
    assert validate_rules(_config("Cortex", action)) == []


def test_a_prefix_without_its_slash_does_not_count() -> None:
    assert validate_rules(_config("Cortex", Action(add_label="CortexX/A"))) != []


def test_an_empty_prefix_requires_nothing() -> None:
    """valid's config: label_prefix is ''; the worker then manages every label."""
    action = Action(label="Jira/Board", add_label="Jira/X", remove_label=["Y"])
    assert validate_rules(_config("", action)) == []


def test_null_add_and_remove_labels_pass() -> None:
    assert validate_rules(_config("Cortex", Action(label="L", add_label=None))) == []


def test_label_itself_is_not_checked() -> None:
    """The matcher prefixes `label`, so an unprefixed one is correct."""
    assert validate_rules(_config("Cortex", Action(label="Commercial/Retail"))) == []
