# Contributing to the Vidaa TV Home Assistant Integration

Thank you for your interest in contributing to this project! This document provides guidelines for contributions.

This repository is the Home Assistant integration (`vidaa_tv`). The underlying protocol
library lives in the separate [`pyvidaa`](https://github.com/warrenrees/pyvidaa) repository —
protocol/transport changes belong there.

## Getting Started

### Development Setup

1. Clone the repository:
   ```bash
   git clone https://github.com/warrenrees/ha_vidaatv.git
   cd ha_vidaatv
   ```

2. Create a virtual environment:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate  # Linux/macOS
   # or: .venv\Scripts\activate  # Windows
   ```

3. Install test dependencies:
   ```bash
   pip install -r requirements-test.txt
   ```
   This pulls in `pytest-homeassistant-custom-component`, the `pyvidaa` library, and
   `async-upnp-client` (needed by Home Assistant's SSDP component).

### Running Tests

```bash
pytest
```

## Code Style

- Follow [PEP 8](https://peps.python.org/pep-0008/) style guidelines
- Use type hints for function parameters and return values
- Include docstrings for all public functions and classes
- Use `logging` instead of `print()` statements

### Docstrings

Use Google-style docstrings:

```python
def my_function(param: str) -> bool:
    """Brief description of function.

    Args:
        param: Description of parameter.

    Returns:
        Description of return value.

    Raises:
        ValueError: When param is invalid.
    """
```

## Submitting Changes

### Pull Requests

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Make your changes
4. Commit with descriptive messages
5. Push to your fork
6. Open a Pull Request

### Commit Messages

- Use clear, descriptive commit messages
- Start with a verb in present tense (e.g., "Add", "Fix", "Update")
- Reference issues when applicable (e.g., "Fix #123")

Example:
```
Add support for new TV model authentication

- Implement protocol version 3300 detection
- Add fallback for legacy authentication
- Update documentation
```

## Reporting Issues

When reporting bugs, please include:

- Home Assistant version
- Integration version
- TV model (if known)
- Steps to reproduce the issue
- Error messages or logs
- Expected vs actual behavior

## Feature Requests

Feature requests are welcome! Please describe:

- The use case for the feature
- How it should work
- Any implementation suggestions

## Protocol / Library Changes

If a change requires new protocol behavior (authentication, transport, discovery, key codes),
it belongs in the [`pyvidaa`](https://github.com/warrenrees/pyvidaa) library and must be
released to PyPI, then pinned in `custom_components/vidaa_tv/manifest.json`.

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
