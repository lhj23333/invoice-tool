"""Keep all synthetic fixtures and their cleanup inside this project."""
from pathlib import Path
import tempfile

TEST_TEMP = Path(__file__).resolve().parent.parent / ".test-tmp"
TEST_TEMP.mkdir(exist_ok=True)
tempfile.tempdir = str(TEST_TEMP)
