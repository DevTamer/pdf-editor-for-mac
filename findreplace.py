"""Find and replace for PDF Editor.

Finds text in the document's runs (see textedit: a run is one line of
same-style text) and turns replacements into ordinary edits, so a
replacement keeps the line's font, size, colour and position, and is
verified and undoable exactly like typing it in.

Matching ignores the differences text extraction makes visible but a
reader doesn't: ligatures ("ﬁ" matches "fi"), no-break spaces (match a
space) and soft hyphens (ignored). Case-insensitive unless asked
otherwise.

Limits: a match must lie within one run, so text that crosses a line
break, or a change of style in the middle (a bold word inside a regular
sentence), isn't found as one match.

No Tk here.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

import pymupdf

from textedit import EditPlan, TextRun, page_runs, plan_edit, removal_rect

# What extraction shows -> what a search should see.
_FOLD = {
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi",
    "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st",
    " ": " ", " ": " ", " ": " ",
    "­": "",
}


def fold(text: str) -> Tuple[str, List[int]]:
    """``text`` as searched, plus, for each of its characters, the index
    in ``text`` it came from."""
    out, origin = [], []
    for i, ch in enumerate(text):
        for c in _FOLD.get(ch, ch):
            out.append(c)
            origin.append(i)
    return "".join(out), origin


def find_in_text(text: str, query: str, match_case: bool = False,
                 whole_word: bool = False) -> List[Tuple[int, int]]:
    """(start, end) of each non-overlapping match of ``query`` in
    ``text``, as indices into ``text``."""
    folded_query, _ = fold(query)
    if not folded_query:
        return []
    folded, origin = fold(text)
    pattern = re.escape(folded_query)
    if whole_word:
        pattern = rf"(?<!\w){pattern}(?!\w)"
    flags = 0 if match_case else re.IGNORECASE
    spans = []
    for m in re.finditer(pattern, folded, flags):
        if m.end() == m.start():
            continue
        start, end = origin[m.start()], origin[m.end() - 1] + 1
        # A ligature only partly matched ("ﬁ" for a query ending in "f")
        # can't be replaced on its own: skip it.
        if (m.start() > 0 and origin[m.start() - 1] == start) or (
                m.end() < len(origin) and origin[m.end()] == end - 1):
            continue
        spans.append((start, end))
    return spans


@dataclass(frozen=True)
class Match:
    """One occurrence of the search text."""

    run: TextRun
    start: int                                  # indices into run.text
    end: int
    # PDF points, for highlighting. None until worked out (match_rects):
    # that needs per-character positions, which are slow to get for a
    # whole document.
    rect: Optional[Tuple[float, float, float, float]] = None

    @property
    def order(self) -> tuple:
        """Sorts matches in reading order."""
        return (self.page_num, round(self.run.origin[1], 1), self.run.origin[0],
                self.start)

    @property
    def page_num(self) -> int:
        return self.run.page_num

    @property
    def text(self) -> str:
        return self.run.text[self.start:self.end]


def _char_boxes(page: pymupdf.Page) -> Dict[tuple, List[Tuple[float, ...]]]:
    """Each span's character boxes, keyed like TextRun pieces are."""
    boxes = {}
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                text = "".join(c["c"] for c in span["chars"])
                key = (text, tuple(round(v, 1) for v in span["bbox"]))
                boxes[key] = [tuple(c["bbox"]) for c in span["chars"]]
    return boxes


def _match_rect(run: TextRun, start: int, end: int, boxes) -> Tuple[float, ...]:
    """The box around characters start..end of ``run``."""
    picked = []
    offset = 0
    for piece in run.pieces:
        chars = boxes.get((piece.text, tuple(round(v, 1) for v in piece.bbox)))
        for i in range(len(piece.text)):
            if start <= offset + i < end and chars and i < len(chars):
                picked.append(chars[i])
        offset += len(piece.text)
    if not picked:
        return run.bbox                           # fall back to the whole line
    return (min(b[0] for b in picked), min(b[1] for b in picked),
            max(b[2] for b in picked), max(b[3] for b in picked))


def match_rects(page: pymupdf.Page, matches: List[Match]) -> List[Match]:
    """``matches`` (all on ``page``) with their highlight boxes."""
    boxes = _char_boxes(page)
    return [Match(m.run, m.start, m.end, _match_rect(m.run, m.start, m.end, boxes))
            for m in matches]


def find_on_page(page: pymupdf.Page, page_num: int, query: str,
                 match_case: bool = False, whole_word: bool = False,
                 runs: Optional[List[TextRun]] = None,
                 rects: bool = True) -> List[Match]:
    if runs is None:
        runs = page_runs(page, page_num)
    found = [Match(run, s, e) for run in runs
             for s, e in find_in_text(run.text, query, match_case, whole_word)]
    return match_rects(page, found) if (rects and found) else found


def might_contain(page_text: str, query: str) -> bool:
    """A quick check on a page's plain text: False only if no line of the
    page can match ``query`` (each of its words must be there somewhere)."""
    folded_text = fold(page_text)[0].lower()
    return all(word in folded_text for word in fold(query)[0].lower().split())


def find_all(doc: pymupdf.Document, query: str, match_case: bool = False,
             whole_word: bool = False, rects: bool = True) -> List[Match]:
    """Every match in the document, in page and reading order."""
    matches = []
    for page_num in range(len(doc)):
        page = doc[page_num]
        if might_contain(page.get_text(), query):
            matches.extend(find_on_page(page, page_num, query, match_case,
                                        whole_word, rects=rects))
    return matches


def replaced_text(run: TextRun, spans: Iterable[Tuple[int, int]],
                  replacement: str) -> str:
    """``run.text`` with each (start, end) span replaced."""
    text, last = [], 0
    for start, end in sorted(spans):
        text.append(run.text[last:start])
        text.append(replacement)
        last = end
    text.append(run.text[last:])
    return "".join(text)


def plan_replace(doc: pymupdf.Document, match: Match, replacement: str) -> EditPlan:
    """The edit that replaces one match."""
    page = doc[match.page_num]
    return plan_edit(doc, page, match.run,
                     replaced_text(match.run, [(match.start, match.end)], replacement))


@dataclass
class ReplaceAllPlan:
    """Everything decided about a Replace All before anything changes."""

    query: str
    replacement: str
    plans: List[EditPlan] = field(default_factory=list)      # one per line
    counts: List[int] = field(default_factory=list)          # matches per plan
    skipped: List[Tuple[Match, str]] = field(default_factory=list)  # (match, why)

    @property
    def count(self) -> int:
        return sum(self.counts)

    @property
    def pages(self) -> List[int]:
        return sorted({p.run.page_num for p in self.plans})

    @property
    def warnings(self) -> List[str]:
        """The edits' warnings, each said once."""
        return list(dict.fromkeys(w for p in self.plans for w in p.warnings))


def plan_replace_all(doc: pymupdf.Document, matches: List[Match],
                     replacement: str, query: str = "") -> ReplaceAllPlan:
    """One edit per line that has matches. Lines whose edit can't be made
    (rotated page, a comment over them...) are listed as skipped."""
    result = ReplaceAllPlan(query=query, replacement=replacement)
    # Grouped by the run itself, not its text and position: text drawn
    # twice in one place is two runs, each replaced once.
    by_run: Dict[int, List[Match]] = {}
    for match in matches:
        by_run.setdefault(id(match.run), []).append(match)
    runs_by_page: Dict[int, List[TextRun]] = {}
    planned: Dict[int, List[Tuple[EditPlan, List[Match]]]] = {}
    for group in by_run.values():
        run = group[0].run
        page = doc[run.page_num]
        if run.page_num not in runs_by_page:
            runs_by_page[run.page_num] = page_runs(page, run.page_num)
        new_text = replaced_text(run, [(m.start, m.end) for m in group], replacement)
        plan = plan_edit(doc, page, run, new_text, runs_by_page[run.page_num])
        if plan.is_noop:
            continue
        if not plan.can_apply:
            result.skipped.extend((m, plan.errors[0]) for m in group)
            continue
        planned.setdefault(run.page_num, []).append((plan, group))

    for page_num, entries in sorted(planned.items()):
        # A line whose removal would also take characters of text that
        # isn't being replaced would fail verification and with it the
        # whole Replace All: leave it out now, and say why.
        hits = _bands_hitting_other_text(doc[page_num], runs_by_page[page_num],
                                         [plan for plan, _g in entries])
        for plan, group in entries:
            if id(plan) in hits:
                result.skipped.extend((m, OVERLAP_ERROR) for m in group)
            else:
                result.plans.append(plan)
                result.counts.append(len(group))
    return result


OVERLAP_ERROR = ("This line overlaps other text, so it can't be changed "
                 "without touching that text.")


def _bands_hitting_other_text(page: pymupdf.Page, runs: List[TextRun],
                              plans: List[EditPlan]) -> set:
    """ids of the plans whose removal band touches a character of a run
    that no plan replaces."""
    replaced = [p.run for p in plans]
    others = [r for r in runs if not any(r.same_as(p) for p in replaced)]
    if not others:
        return set()
    boxes = _char_boxes(page)
    other_chars = []
    for run in others:
        for piece in run.pieces:
            chars = boxes.get((piece.text, tuple(round(v, 1) for v in piece.bbox)), ())
            other_chars.extend(pymupdf.Rect(b) for b, c in zip(chars, piece.text)
                               if not c.isspace())
    hits = set()
    for plan in plans:
        band = removal_rect(plan.run)
        if any(band.intersects(c) for c in other_chars):
            hits.add(id(plan))
    return hits


def skipped_summary(plan: ReplaceAllPlan) -> List[str]:
    """Why matches were skipped, one line per reason, with counts."""
    reasons = Counter(why for _m, why in plan.skipped)
    return [f"{n} × {why}" for why, n in reasons.items()]
