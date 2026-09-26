"""Make a clean bill look like a bill somebody photographed on a desk.

    uv run python scripts/degrade_bills.py --src tests/fixtures/bill_pile \
        --out tests/fixtures/bill_pile_photo

The generated pile is rendered text: crisp glyphs, even lighting, dead straight.
Scoring against it measures the model's reading and not much else, and the
number it produces - a hundred percent - is not a number a firm should plan
around. Bills actually arrive as phone photographs: a few degrees of skew, a
shadow across one side, JPEG artefacts from being forwarded twice on WhatsApp.

So this makes the same bills harder in exactly those ways, keeping the truth
file beside each one. The difference between the two scores is what capture
conditions cost, which is the thing worth knowing before promising a firm
anything about a folder of scans.

Deliberately not: crumpling, tearing, handwriting over the total. Those produce
documents the reader should refuse, and it already has an attention list for
that; what is being measured here is the everyday bad photograph.
"""

from __future__ import annotations

import argparse
import random
import shutil
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter

#: How far a page slips when somebody photographs it on a desk.
MAX_SKEW_DEGREES = 2.2

#: WhatsApp re-encodes; twice through is normal by the time it reaches a firm.
JPEG_QUALITY = (38, 62)


def shadow(image: Image.Image, strength: float = 0.45) -> Image.Image:
    """A soft gradient across the page, the way a hand or a lamp falls on it."""
    width, height = image.size
    gradient = Image.linear_gradient("L")
    if random.random() < 0.5:
        # Rotate before the resize: a rotated 256x256 gradient resized to the
        # page fits, a gradient rotated after it is the page's size transposed.
        gradient = gradient.transpose(Image.Transpose.ROTATE_90)
    gradient = gradient.resize((width, height)).point(
        lambda v: int(255 - v * strength)
    )
    darkened = image.copy()
    darkened.putalpha(gradient)
    flat = Image.new("RGB", image.size, "white")
    flat.paste(darkened, mask=darkened.split()[3])
    return flat


def photograph(path: Path, out: Path, seed: int = 0) -> Path:
    """One bill, as if it had been photographed and forwarded."""
    random.seed(seed or hash(path.name) % 10_000)
    image = Image.open(path).convert("RGB")

    image = image.rotate(
        random.uniform(-MAX_SKEW_DEGREES, MAX_SKEW_DEGREES),
        resample=Image.Resampling.BICUBIC,
        expand=True,
        fillcolor=(252, 252, 250),
    )
    image = shadow(image, strength=random.uniform(0.25, 0.5))
    image = ImageEnhance.Contrast(image).enhance(random.uniform(0.82, 0.98))
    image = image.filter(ImageFilter.GaussianBlur(radius=random.uniform(0.3, 0.9)))

    # A phone photo is not the page's own resolution.
    scale = random.uniform(0.55, 0.8)
    image = image.resize(
        (int(image.width * scale), int(image.height * scale)),
        Image.Resampling.LANCZOS,
    )

    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out, quality=random.randint(*JPEG_QUALITY), subsampling=2)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", default="tests/fixtures/bill_pile")
    parser.add_argument("--out", default="tests/fixtures/bill_pile_photo")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    src, out = Path(args.src), Path(args.out)
    made = 0
    for image in sorted(src.glob("*.png")):
        target = out / f"{image.stem}.jpg"
        try:
            photograph(image, target, seed=args.seed)
        except OSError:
            # The pile deliberately contains one file that is not a readable
            # image. It is carried across untouched: a reader that refuses it is
            # part of what the score is measuring.
            out.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(image, out / image.name)
            target = out / image.name
        truth = image.with_suffix(".json")
        if truth.is_file():
            shutil.copyfile(truth, target.with_suffix(".json"))
        made += 1

    print(f"  {made} photographed bill(s) in {out}")
    print("  the truth files came across unchanged, so the two piles score alike")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
