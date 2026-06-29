import pathlib
import sys

# Allow running the suite without installing the package (src layout).
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))
