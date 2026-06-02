"""Smoke test: both LangGraphs compile with the new llm_pick nodes."""
from polybot.scanner.graph import build_scanner_graph
from polybot.scanner.sports_graph import build_sports_scanner_graph


def test_weather_graph_includes_llm_pick():
    g = build_scanner_graph()
    nodes = list(g.get_graph().nodes)
    assert "llm_pick" in nodes
    assert "run_strategies" in nodes
    assert "monitor_positions" in nodes


def test_sports_graph_includes_llm_pick():
    g = build_sports_scanner_graph()
    nodes = list(g.get_graph().nodes)
    assert "llm_pick_sports" in nodes
    assert "run_us_direct_strategy" in nodes
    assert "fetch_live_states" in nodes


def test_llm_picker_disabled_passthrough():
    """When llm_picker_enabled=False (default), pick_opportunities returns input."""
    import asyncio
    from polybot.strategies.llm_picker import pick_opportunities

    result = asyncio.run(pick_opportunities([], scan_number=1))
    assert result == []
