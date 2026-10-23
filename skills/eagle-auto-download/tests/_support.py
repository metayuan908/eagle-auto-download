"""Run the same regression suite from the project or an installed portable Skill."""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT if (ROOT / 'SKILL.md').is_file() else ROOT / 'skills/eagle-auto-download'
WORK = Path(tempfile.gettempdir()) if SKILL == ROOT else ROOT / 'work'
WORK.mkdir(parents=True, exist_ok=True)
CONFIG = SKILL / 'references/config.example.json'
sys.path.insert(0, str(SKILL / 'scripts'))
