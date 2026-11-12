# AGENTS.md

Coding rules for this repository. They apply to humans and AI agents alike.

## Tooling

- All commands MUST use `uv` (e.g. `uv run pytest`, `uv run python media_renamer.py --check`, `uv add --dev <package>`). Never call `python`, `pip` or `pytest` directly.

## Testing

- Every function MUST be unit tested with `pytest`.
- Tests live in `tests/` and mirror the module they cover (`tests/test_<module>.py`).

## Language

- Documentation files MUST be written in English.
- All identifiers MUST be in English: functions, classes, variables, constants, file names, etc.
- Comments MUST be in English.

## Code style

- Leave exactly 2 blank lines between top-level functions.
- Every function MUST have a **Sphinx** docstring written in English.

### Standard function format

```python
def sample_function(param1: str) -> Dict[str, Any]:
    """
    Description of the function.

    :param param1: Description of the parameter.
    :return: Description of the returned value. Omit this line if the function returns nothing.
    :raises ValueError: Description of the possible error. Omit this line if the function raises no exception.
    """
    pass
```
