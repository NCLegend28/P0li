# Setup CI/CD Pipeline

Set up a GitHub Actions CI/CD pipeline for a Python project.

## Instructions

1. Analyze the project structure:
   - Check for `pyproject.toml`, `setup.py`, or `requirements.txt`
   - Identify the Python version requirement
   - Find dev dependencies (pytest, ruff, mypy, black, etc.)
   - Locate test directory

2. Create `.github/workflows/ci.yml` with these jobs:

   **lint** - Code quality checks
   - Use ruff, flake8, or black based on project config
   
   **typecheck** - Static type analysis
   - Use mypy if configured in project
   
   **test** - Run test suite
   - Use pytest or unittest as configured
   - Run on push to main and pull requests
   
   **secrets-scan** - Prevent credential leaks
   - Use TruffleHog or similar scanner
   - Scan only verified secrets to reduce noise

3. Triggers:
   - Run on push to `main`
   - Run on pull requests to `main`

4. Best practices:
   - Use `actions/checkout@v4` and `actions/setup-python@v5`
   - Cache pip dependencies if builds are slow
   - Run jobs in parallel where possible
   - Keep workflows minimal and focused

## Example CI workflow

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install ruff
      - run: ruff check .

  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -e ".[dev]"
      - run: pytest -v

  secrets-scan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: trufflesecurity/trufflehog@main
        with:
          extra_args: --only-verified
```

## Notes

- Adjust Python version based on `requires-python` in pyproject.toml
- Add coverage reporting if project uses it
- Consider adding dependency caching for faster builds
- For monorepos, use path filters to run only relevant jobs
