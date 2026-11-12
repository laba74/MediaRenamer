"""Unit tests for the ``media_renamer`` module."""

from media_renamer import main


def test_main_returns_zero_without_arguments() -> None:
    """
    Check that ``main`` succeeds when called with no arguments.
    """
    assert main([]) == 0
