# Releasing `gann-sdk` to PyPI

## Automatic publishing (GitHub Actions)

This repo includes an automatic publish workflow at
`.github/workflows/publish-on-version-bump.yml`.

How it works:

- Triggers on pushes to `main` when `__init__.py` or `pyproject.toml` changes.
- Reads `__version__` from `__init__.py`.
- Publishes only if version changed vs previous commit.
- Skips if that version already exists on PyPI.

Required one-time setup:

- Add repository secret `PYPI_API_TOKEN` with a valid PyPI API token.

## 1) Bump version

Update `__version__` in `__init__.py`.

## 2) Build artifacts

```bash
python -m pip install --upgrade build twine
python -m build
```

Artifacts are created in `dist/`.

## 3) Validate artifacts

```bash
python -m twine check dist/*
```

## 4) Upload to TestPyPI (recommended first)

```bash
python -m twine upload --repository testpypi dist/*
```

Install check:

```bash
python -m pip install -i https://test.pypi.org/simple/ gann-sdk
```

## 5) Upload to PyPI

```bash
python -m twine upload dist/*
```

Use API token auth:

- username: `__token__`
- password: your PyPI token (or set `TWINE_USERNAME` / `TWINE_PASSWORD`)