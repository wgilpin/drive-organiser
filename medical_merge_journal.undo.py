# Reverses the merge recorded in medical_merge_journal.json
import json, shutil, pathlib
for r in reversed(json.loads(pathlib.Path('medical_merge_journal.json').read_text())):
    dst = pathlib.Path(r['src']); dst.parent.mkdir(parents=True, exist_ok=True)
    if pathlib.Path(r['dst']).exists() and not dst.exists():
        shutil.move(r['dst'], r['src'])
