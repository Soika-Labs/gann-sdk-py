# Releasing `gann-sdk` to PyPI

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