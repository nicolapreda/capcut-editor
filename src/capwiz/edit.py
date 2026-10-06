"""Turn the AI's edit decisions into timeline segments — and explain each step.

The AI works on a single *global* transcript that spans every clip of the
project (so it can spot a retake in clip 3 of a line already said in clip 1).
Every word gets a global index (gid). The AI answers with ranges of gids to
KEEP (with a role) and to DISCARD (with a type), each with a reason.

This module:
  * builds that global word list,
  * normalizes the plan (clamps, overlaps, words the AI forgot to classify),
  * materializes the kept ranges into KeepIntervals, cutting the pauses inside
    them that are longer than the threshold the AI chose,
  * logs every block (kept / discarded) and every silence cut, in order.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .models import KeepInterval, SourceClip, TimelineSegment, Word


@dataclass
class GWord:
    gid: int
    clip_idx: int
    word: Word


def build_global_words(clips: list[SourceClip]) -> list[GWord]:
    out: list[GWord] = []
    for ci, clip in enumerate(clips):
        for w in clip.words:
            out.append(GWord(gid=len(out), clip_idx=ci, word=w))
    return out


# Labels the AI may use (anything else is kept verbatim in the log).
KEEP_ROLES = {
    "hook": "HOOK", "contenuto": "CONTENUTO", "prova": "PROVA / CREDIBILITÀ",
    "cta": "CALL TO ACTION", "transizione": "TRANSIZIONE",
}
DISCARD_TYPES = {
    "dietro_le_quinte": "DIETRO LE QUINTE", "fuori_tema": "FUORI TEMA",
    "retake": "RETAKE (rifatta meglio dopo)", "ripetizione": "RIPETIZIONE",
    "falsa_partenza": "FALSA PARTENZA", "filler": "FILLER", "errore": "ERRORE / IMPAPPINATO",
    "silenzio": "SILENZIO", "troppo_lungo": "TAGLIATO PER DURATA",
    "non_classificato": "NON CLASSIFICATO DALL'AI",
}


@dataclass
class EditRange:
    start: int          # gid, inclusive
    end: int            # gid, inclusive
    label: str          # role (keep) or type (discard)
    why: str


@dataclass
class EditPlan:
    summary: str = ""
    topic: str = ""
    max_pause: float = 0.35
    max_pause_why: str = ""
    keeps: list[EditRange] = field(default_factory=list)
    discards: list[EditRange] = field(default_factory=list)


@dataclass
class Block:
    """A run of consecutive words sharing the same decision."""
    kind: str           # keep | discard
    ref: int            # index into plan.keeps / plan.discards (-1 = implicit)
    gids: list[int]


def normalize_plan(plan: EditPlan, n_words: int) -> list[Block]:
    """Clamp ranges, resolve overlaps, and label every word exactly once.

    Keep wins over discard when the AI marked a word as both. Words the AI
    didn't classify become an implicit discard ("non classificato") so nothing
    disappears silently.
    """
    def clamp(r: EditRange) -> EditRange | None:
        s, e = max(0, r.start), min(n_words - 1, r.end)
        return EditRange(s, e, r.label, r.why) if s <= e else None

    plan.keeps = sorted(filter(None, (clamp(r) for r in plan.keeps)), key=lambda r: r.start)
    plan.discards = [r for r in (clamp(r) for r in plan.discards) if r]

    labels: list[tuple[str, int] | None] = [None] * n_words
    for i, r in enumerate(plan.keeps):
        for g in range(r.start, r.end + 1):
            if labels[g] is None:
                labels[g] = ("keep", i)
    for i, r in enumerate(plan.discards):
        for g in range(r.start, r.end + 1):
            if labels[g] is None:
                labels[g] = ("discard", i)

    blocks: list[Block] = []
    for g, lab in enumerate(labels):
        lab = lab or ("discard", -1)
        if blocks and (blocks[-1].kind, blocks[-1].ref) == lab:
            blocks[-1].gids.append(g)
        else:
            blocks.append(Block(kind=lab[0], ref=lab[1], gids=[g]))
    return blocks


@dataclass
class EditStats:
    raw_duration: float = 0.0
    kept_duration: float = 0.0
    discarded: dict[str, float] = field(default_factory=dict)   # label → seconds of speech
    silence_cut: float = 0.0                                     # pauses removed inside kept blocks
    silence_cuts: int = 0


def _quote(words: list[Word], limit: int = 140) -> str:
    txt = " ".join(w.text for w in words)
    return txt if len(txt) <= limit else txt[: limit - 1] + "…"


def materialize(
    plan: EditPlan,
    blocks: list[Block],
    gwords: list[GWord],
    clips: list[SourceClip],
    head_pad: float,
    tail_pad: float,
    log: Callable[[str], None],
) -> tuple[list[TimelineSegment], EditStats]:
    """Log each decision in chronological order and build the timeline."""
    stats = EditStats(raw_duration=sum(c.duration for c in clips))
    timeline: list[TimelineSegment] = []
    cursor = 0.0
    keep_no = 0

    for b in blocks:
        ws = [gwords[g] for g in b.gids]
        by_clip: dict[int, list[Word]] = {}
        for gw in ws:
            by_clip.setdefault(gw.clip_idx, []).append(gw.word)
        where = " + ".join(
            f"{clips[ci].path.name} {cw[0].start:.2f}s–{cw[-1].end:.2f}s"
            for ci, cw in by_clip.items()
        )
        span = sum(cw[-1].end - cw[0].start for cw in by_clip.values())
        words_only = [gw.word for gw in ws]

        if b.kind == "discard":
            r = plan.discards[b.ref] if b.ref >= 0 else EditRange(0, 0, "non_classificato",
                                                                  "l'AI non ha incluso queste parole nel montaggio")
            label = DISCARD_TYPES.get(r.label, r.label.upper())
            stats.discarded[label] = stats.discarded.get(label, 0.0) + span
            log(f"✘ SCARTA  [{where} · {span:.1f}s] {label}")
            log(f"      «{_quote(words_only)}»")
            log(f"      perché: {r.why or '—'}")
            continue

        r = plan.keeps[b.ref]
        keep_no += 1
        label = KEEP_ROLES.get(r.label, r.label.upper())
        log(f"✔ TIENI #{keep_no}  [{where} · {span:.1f}s] {label}")
        log(f"      «{_quote(words_only)}»")
        log(f"      perché: {r.why or '—'}")

        # split each clip's words at pauses longer than the AI's threshold
        for ci, cw in by_clip.items():
            clip = clips[ci]
            group: list[Word] = [cw[0]]
            groups: list[list[Word]] = []
            for w in cw[1:]:
                gap = w.start - group[-1].end
                if gap > plan.max_pause:
                    log(f"      ✂ silenzio {gap:.2f}s tagliato dentro #{keep_no} "
                        f"({clip.path.name} @ {group[-1].end:.2f}s) — oltre la soglia "
                        f"{plan.max_pause:.2f}s decisa dall'AI")
                    stats.silence_cut += gap
                    stats.silence_cuts += 1
                    groups.append(group)
                    group = [w]
                else:
                    group.append(w)
            groups.append(group)

            for gws in groups:
                start = max(0.0, gws[0].start - head_pad)
                end = min(clip.duration, gws[-1].end + tail_pad)
                if end - start < 0.08:
                    continue
                k = KeepInterval(source=clip, src_start=start, src_end=end, words=list(gws),
                                 block=keep_no)
                timeline.append(TimelineSegment(keep=k, timeline_start=cursor,
                                                timeline_end=cursor + k.duration))
                cursor += k.duration

    stats.kept_duration = cursor
    return timeline, stats


def annotated_transcript(plan: EditPlan, blocks: list[Block], gwords: list[GWord],
                         clips: list[SourceClip]) -> str:
    """Markdown appendix: the full transcript with every decision marked."""
    md = ["## Trascrizione completa annotata", "",
          "Ogni blocco è marcato con la decisione dell'AI.", ""]
    keep_no = 0
    last_clip = -1
    for b in blocks:
        ci = gwords[b.gids[0]].clip_idx
        if ci != last_clip:
            md += ["", f"### {clips[ci].path.name}", ""]
            last_clip = ci
        text = " ".join(gwords[g].word.text for g in b.gids)
        if b.kind == "keep":
            keep_no += 1
            r = plan.keeps[b.ref]
            md.append(f"- ✔ **#{keep_no} {KEEP_ROLES.get(r.label, r.label)}** — {text}  \n"
                      f"  _perché:_ {r.why}")
        else:
            r = plan.discards[b.ref] if b.ref >= 0 else None
            lab = DISCARD_TYPES.get(r.label, r.label) if r else DISCARD_TYPES["non_classificato"]
            md.append(f"- ✘ ~~{text}~~ — **{lab}**  \n  _perché:_ {r.why if r else '—'}")
    return "\n".join(md)
