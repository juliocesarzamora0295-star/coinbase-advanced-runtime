# Contributing to Fortress v4

Thank you for your interest in contributing to Fortress v4!

## Development Setup

1. Fork the repository
2. Clone your fork:
   ```bash
   git clone https://github.com/YOUR_USERNAME/fortress_v4.git
   cd fortress_v4
   ```

3. Create a virtual environment:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   ```

4. Install in development mode:
   ```bash
   pip install -e ".[dev]"
   ```

5. Run tests to verify setup:
   ```bash
   pytest tests/unit/ -v
   ```

## Development Workflow

### Branch Naming

- `feat/description` - New features
- `fix/description` - Bug fixes
- `docs/description` - Documentation updates
- `refactor/description` - Code refactoring
- `test/description` - Test additions/improvements

### Before Committing

1. **Run tests:**
   ```bash
   pytest tests/unit/ -v
   ```

2. **Check formatting:**
   ```bash
   black src/ tests/
   ruff check src/ tests/
   ```

3. **Type checking:**
   ```bash
   mypy src/
   ```

4. **Verify syntax:**
   ```bash
   python -m compileall -q src tests
   ```

### Commit Messages

Follow conventional commits format:

```
<type>(<scope>): <description>

[optional body]

[optional footer]
```

Types:
- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation changes
- `style`: Code style changes (formatting, etc.)
- `refactor`: Code refactoring
- `test`: Test additions/changes
- `chore`: Build process or auxiliary tool changes

Example:
```
feat(risk): add fail-closed validation for risk inputs

- Block trading if day_pnl_pct is unavailable
- Block trading if drawdown_pct is unavailable
- Block trading if orders_last_minute is unavailable
```

## Code Standards

### Python Style

- Follow PEP 8
- Line length: 100 characters
- Use type hints
- Document public functions with docstrings

### Testing

- All new code must have unit tests
- Maintain test coverage above 80%
- Use pytest fixtures for common setup

### Risk-Critical Code

For any code related to:
- Order execution
- Risk evaluation
- Position sizing
- PnL calculation

Additional requirements:
- Must use `Decimal` for financial calculations
- Must have fail-closed behavior
- Must be reviewed by at least one maintainer

## Pull Request Process

1. Update CHANGELOG.md with your changes
2. Ensure all CI checks pass
3. Request review from maintainers
4. Address review feedback
5. Squash commits if requested

## Questions?

Open an issue for:
- Bug reports
- Feature requests
- Documentation improvements
- General questions

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
