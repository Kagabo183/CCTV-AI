"""zoom.py <frame stem> x1 y1 x2 y2 [x1 y1 x2 y2 ...] -> bench/review/zoom_<stem>_<n>.jpg (4x, grid every 10 px)"""
import json
import sys
from pathlib import Path

import cv2

B = Path(__file__).parent
stem, vals = sys.argv[1], list(map(int, sys.argv[2:]))
img = cv2.imread(str(B / "frames" / f"{stem}.png"))
cands = json.loads((B / "gt" / f"{stem}.candidates.json").read_text())
for n in range(len(vals) // 4):
    x1, y1, x2, y2 = vals[n * 4 : n * 4 + 4]
    s = 4 if img.shape[0] <= 720 else 2
    crop = cv2.resize(img[y1:y2, x1:x2], None, fx=s, fy=s, interpolation=cv2.INTER_CUBIC)
    for gx in range(x1 - x1 % 10 + 10, x2, 10):
        big = gx % 50 == 0
        cv2.line(crop, ((gx - x1) * s, 0), ((gx - x1) * s, 10 if big else 4), (255, 255, 255), 1)
        if big:
            cv2.putText(crop, str(gx), ((gx - x1) * s + 2, 20), cv2.FONT_HERSHEY_PLAIN, 1, (255, 255, 255), 1)
    for gy in range(y1 - y1 % 10 + 10, y2, 10):
        big = gy % 50 == 0
        cv2.line(crop, (0, (gy - y1) * s), (10 if big else 4, (gy - y1) * s), (255, 255, 255), 1)
        if big:
            cv2.putText(crop, str(gy), (12, (gy - y1) * s + 5), cv2.FONT_HERSHEY_PLAIN, 1, (255, 255, 255), 1)
    for c in cands:
        bx1, by1, bx2, by2 = c["box"]
        if bx2 < x1 or bx1 > x2 or by2 < y1 or by1 > y2:
            continue
        p1 = (int((bx1 - x1) * s), int((by1 - y1) * s))
        cv2.rectangle(crop, p1, (int((bx2 - x1) * s), int((by2 - y1) * s)), (60, 220, 60) if c["group"] == "person" else (255, 160, 40), 1)
        cv2.putText(crop, f'{c["id"]} {c["conf"]}', (p1[0] + 2, p1[1] + 14), cv2.FONT_HERSHEY_PLAIN, 1, (60, 220, 60), 1)
    cv2.imwrite(str(B / "review" / f"zoom_{stem}_{n}.jpg"), crop, [cv2.IMWRITE_JPEG_QUALITY, 93])
print(json.dumps([c for c in cands], separators=(",", ":")))
