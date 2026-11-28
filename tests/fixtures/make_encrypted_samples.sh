#!/bin/bash
# Regenerates the encrypted sample PDFs used by tests/test_encryption.py:
# the same two-page document, protected by an owner password ("owner")
# with no user password, in each standard encryption scheme, written by
# qpdf with compressed object streams (as many real-world PDFs are).
# PyMuPDF can't write encrypted files with object streams reliably, which
# is why these are made with qpdf and committed. Needs: brew install qpdf
set -e
cd "$(dirname "$0")"
PY="${PYTHON:-../../venv/bin/python}"
"$PY" - <<'PYEOF'
import pymupdf
doc = pymupdf.open()
for i in range(2):
    page = doc.new_page(width=400, height=300)
    page.insert_text((50, 80), f"Confidential page {i + 1}", fontname="helv", fontsize=14)
    page.insert_text((50, 110), "Keep this line", fontname="helv", fontsize=12)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 24, 24), False)
    pix.set_rect(pix.irect, (40, 120, 200))
    page.insert_image(pymupdf.Rect(250, 150, 330, 230), pixmap=pix)
doc.set_metadata({"title": "Encrypted sample", "author": "Test",
                  "modDate": "D:20240102120000Z"})
doc.save("encrypted_plain.tmp.pdf")
PYEOF
for spec in "rc4_40:40" "rc4_128:128 --use-aes=n" "aes_128:128 --use-aes=y" "aes_256:256"; do
    name="${spec%%:*}"; args="${spec#*:}"; bits="${args%% *}"; rest="${args#$bits}"
    if [ "$bits" = 40 ]; then perms="--print=y --modify=n --extract=n"
    else perms="--print=full --modify=none --extract=n"; fi
    # shellcheck disable=SC2086
    qpdf --allow-weak-crypto --object-streams=generate \
        --encrypt "" owner "$bits" $perms $rest -- \
        encrypted_plain.tmp.pdf "encrypted_${name}.pdf"
done
rm encrypted_plain.tmp.pdf
