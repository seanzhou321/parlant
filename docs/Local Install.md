# Local Installation Guide

### 1. Build the Package
```
pip install build
python -m build
```

This will create a dist/ directory containing your package distributions (both wheel and source)

### 2. Install Locally for Testing

You have several options:

#### Option A: Install the built package

```
pip install dist/my_package-0.1.0-py3-none-any.whl
```

#### Option B: Install in development mode (easiest for testing)

```
pip install -e .
```

This creates an "editable" install where changes to your source code will be immediately available without reinstalling.

#### Option C: Install from a local directory

If you want to test installation from a local directory:

```
pip install /path/to/my_package
```
