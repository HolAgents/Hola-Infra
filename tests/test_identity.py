"""Test identity resolution with CI-resume fields."""

from dispatcher.identity import (
    AgentIdentity,
    resolve,
    get_current_target,
)


def test_agent_identity_has_target_id():
    """AgentIdentity has target_id and identity_name fields."""
    ident = AgentIdentity(
        agent_id="test-01",
        agent_type="triage",
        display_name="Test",
        gh_user="test-bot",
        target_id="claude-code",
        identity_name="testagent001",
    )
    assert ident.target_id == "claude-code"
    assert ident.identity_name == "testagent001"


def test_agent_identity_defaults():
    """Default values: target_id='claude-code', identity_name=''."""
    ident = AgentIdentity(
        agent_id="test-02",
        agent_type="review",
        display_name="Reviewer",
        gh_user="r-bot",
    )
    assert ident.target_id == "claude-code"
    assert ident.identity_name == ""


def test_get_current_target_found():
    """identity_name in local registry → returns target_id."""
    target = get_current_target("holagent001")
    assert target == "claude-code"


def test_get_current_target_not_found():
    """identity_name not in registry → None."""
    target = get_current_target("nonexistent-identity")
    assert target is None


def test_resolve_returns_identity_with_target():
    """resolve() returns AgentIdentity with target_id populated."""
    ident = resolve("triage", "HolAgents/Hola-Infra")
    assert ident is not None
    assert ident.target_id == "claude-code"
    assert ident.identity_name == "holagent001"


def test_resolve_unknown_type():
    """resolve() unknown agent_type → None."""
    ident = resolve("no-such-type", "HolAgents/test")
    assert ident is None
