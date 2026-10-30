"""Fetch LINE Seed Sans Extra Bold (Latin) once, for embedding in the explorer page.

The game sets its card chrome in LINE Seed Sans Extra Bold -- named exactly, because the only
Latin face in the build is the TextMeshPro atlas `LINESeedSans_A_XBd SDF Atlas`. An SDF atlas is
not a font, so there is nothing to extract from the game and no `*.ttf/otf/ttc` anywhere in the
install. The face has to come from LINE.

LINE Seed is published by LY Corporation under the **SIL Open Font License 1.1**, which permits
embedding and redistribution with the licence attached. That is why this is allowed where
shipping a game asset would not be: it is not a game asset.

Only the `latin` subset at weight 800 is fetched -- the page's LINE Seed text is generation
labels, "Lv" and digits. Fontsource already splits by unicode range, so this needs no subsetting
step and therefore no `fonttools` dependency, which is not on the project's approved list.

    python fetch_font.py            # downloads if missing
    python fetch_font.py --force    # re-downloads

Writes fonts/<name>.woff2 plus fonts/SOURCE.txt recording where it came from and its sha256.
Nothing else in the build touches the network; if the file is absent the page simply falls back
to the next font in its stack.
"""
import hashlib, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(HERE, "fonts")
NAME = "line-seed-sans-xbd-latin"
# Fontsource mirrors the upstream OFL release and serves per-subset woff2 over jsDelivr.
URL = "https://cdn.jsdelivr.net/fontsource/fonts/line-seed-jp@latest/latin-800-normal.woff2"
LICENCE = "SIL Open Font License 1.1 -- LINE Seed, (c) LY Corporation"


def main():
    dst = os.path.join(FONT_DIR, NAME + ".woff2")
    if os.path.exists(dst) and "--force" not in sys.argv:
        print(f"ya esta: {dst} ({os.path.getsize(dst)} B) -- --force para volver a bajarlo")
        return 0

    import requests
    print(f"GET {URL}")
    r = requests.get(URL, timeout=30)
    r.raise_for_status()
    body = r.content
    # woff2 files start with the ASCII signature 'wOF2'; anything else is an error page.
    if body[:4] != b"wOF2":
        print(f"la respuesta no es woff2 (empieza por {body[:8]!r}), no se guarda")
        return 1

    os.makedirs(FONT_DIR, exist_ok=True)
    with open(dst, "wb") as fh:
        fh.write(body)
    digest = hashlib.sha256(body).hexdigest()
    with open(os.path.join(FONT_DIR, "SOURCE.txt"), "w", encoding="utf-8") as fh:
        fh.write(f"{NAME}.woff2\n  url    {URL}\n  sha256 {digest}\n  bytes  {len(body)}\n"
                 f"  licence {LICENCE}\n")
    print(f"{dst}  {len(body)} B  sha256 {digest[:16]}...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
