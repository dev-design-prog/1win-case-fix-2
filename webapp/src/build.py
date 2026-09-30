#!/usr/bin/env python3
"""
Сборка мини-аппа:  python webapp/src/build.py
Берёт webapp/src/template.html (читаемая вёрстка, стили и скрипты) и подставляет тяжёлые
картинки/анимации из webapp/src/blobs/*.txt вместо меток {{BLOB:имя}} -> webapp/index.html.
Править интерфейс нужно в template.html, потом запускать build.py.
"""
import re, pathlib, json, sys
here = pathlib.Path(__file__).parent
tpl = (here / "template.html").read_text(encoding="utf-8")
sys.path.insert(0, str(here.parent.parent / "bot"))
import cases_config
_cat = cases_config.public_catalog()
_cat["upgrade_chances"] = {"1.5": 50, "2.0": 35, "3.0": 25, "5.0": 15, "10.0": 10}
tpl = tpl.replace("{{CATALOG_JSON}}", json.dumps(_cat, ensure_ascii=False))
out = re.sub(r"\{\{BLOB:(\w+)\}\}", lambda m: (here / "blobs" / f"{m.group(1)}.txt").read_text(), tpl)
(here.parent / "index.html").write_text(out, encoding="utf-8")
print(f"index.html: {len(out) / 1e6:.1f} MB")
