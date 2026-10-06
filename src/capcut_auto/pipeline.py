"""End-to-end pipeline, callable from CLI, GUI and the desktop backend.

Claude is the editor. It first *looks at* every clip (frames + transcript) to
tell talking takes from b-roll, then writes the *brief*: reading the script
(stage directions included), what was actually said and how the template's own
video is built, it decides how this video must be. Every later step follows it:
  * if someone speaks to the audience, the speech drives the reel (what to keep,
    which silences and off-topic/behind-the-scenes lines to cut) and the b-roll
    is laid over the sentences it illustrates;
  * if nobody does, the reel is an images-only montage of the b-roll.
Sounds and texts are the AI's call too. Every choice is written to the log with
its reason and saved as a Markdown report. Without a working Claude connection
the pipeline refuses to run.
"""
from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .ai import (BRIEF_SOURCE, DEFAULT_MODEL, KIND_LABEL, AIUnavailable, BRoll, SoundPlan,
                 look_at_clips, plan_brief, plan_covers, plan_edit, plan_montage,
                 plan_sounds, plan_texts, require_ai, review)
from .cuts import PACING_BY_NAME, PACING_FAST, Pacing
from .draft import CAPCUT_PROJECTS_DIR, build_draft
from .edit import annotated_transcript, build_global_words, materialize, normalize_plan
from .emphasis import Emphasis
from .models import CoverShot, KeepInterval, TimelineSegment
from .probe import probe
from .report import Report
from .stabilize import stabilize
from .subtitles import build_subtitles
from .template import (build_from_template, template_blueprint, template_sound_inventory,
                       template_subtitle_look)
from .transcribe import transcribe
from .vision import make_strip


VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}


def _is_video(p: Path) -> bool:
    # Skip macOS AppleDouble sidecars (e.g. "._C0867.MP4") that share a video
    # suffix but hold no media — ffprobe exits non-zero on them.
    if p.name.startswith("._"):
        return False
    return p.suffix.lower() in VIDEO_EXTS


def gather_inputs(input_path: Path) -> list[Path]:
    if input_path.is_file():
        return [input_path]
    if input_path.is_dir():
        files = sorted(p for p in input_path.iterdir() if _is_video(p))
        if not files:
            raise ValueError(f"Nessun video trovato in {input_path}")
        return files
    raise ValueError(f"Percorso inesistente: {input_path}")


def _resolve_pacing(pacing: str | Pacing) -> Pacing:
    if isinstance(pacing, Pacing):
        return pacing
    return PACING_BY_NAME.get(pacing, PACING_FAST)


def _read_docx(path: Path) -> str:
    """Extract plain text from a .docx without external dependencies.

    A .docx is a zip; the body lives in word/document.xml. We turn each
    paragraph (</w:p>) into a newline, tabs into \\t, strip every tag, then
    unescape XML entities. Quote characters survive, so the screenplay-style
    quote extraction downstream works.
    """
    import html
    import re
    import zipfile

    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", errors="replace")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab/>", "\t", xml)
    xml = re.sub(r"<w:br/>", "\n", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    return html.unescape(text)


def read_script(path: Path) -> str:
    """Read a script from .txt/.md or .docx.

    .docx is unzipped and stripped to plain text. Plain-text files are decoded
    tolerantly: UTF-8 (with/without BOM), Windows-1252, Mac Roman, then Latin-1
    as a never-fails fallback — because TextEdit/Word/Pages often save as Mac
    Roman or Windows-1252, which break a plain read_text().
    """
    path = Path(path)
    if path.suffix.lower() == ".docx":
        return _read_docx(path)
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp1252", "mac-roman", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


@dataclass
class PipelineResult:
    draft_path: Path
    report_path: Path


def run_pipeline(
    input_path: Path,
    name: str,
    template: str | None = None,
    script: str | None = None,
    model: str = "large-v3",
    language: str = "it",
    pacing: str | Pacing = "fast",
    stabilize_clips: bool = False,
    redistribute_sfx_enabled: bool = True,
    add_emphasis: bool = True,
    emphasis_count: int = 4,
    drop_fillers: bool = True,
    aggressive_fillers: bool = False,
    intro_title: str | None = None,
    clear_template_texts: bool = False,
    ai_model: str = DEFAULT_MODEL,
    use_ai_review: bool = True,
    ai_target_duration: float | None = None,
    subtitle_max_words: int = 4,
    subtitle_max_duration: float = 1.4,
    subtitle_like_template: bool = True,
    fmt: str = "vertical",
    projects_dir: Path | None = None,
    log: Callable[[str], None] = print,
) -> PipelineResult:
    """Run the whole pipeline. `redistribute_sfx_enabled` = let the AI manage
    the template's sounds (off keeps the template audio untouched).
    `subtitle_like_template` = write the subtitles the way the template's own
    are written (block length, letter case, punctuation); off uses
    `subtitle_max_words` / `subtitle_max_duration`."""
    rep = Report(name, log)
    try:
        out = _run(rep, input_path, name, template, script, model, language,
                   _resolve_pacing(pacing), stabilize_clips, redistribute_sfx_enabled,
                   add_emphasis, emphasis_count, drop_fillers, aggressive_fillers,
                   intro_title, clear_template_texts, ai_model, use_ai_review,
                   ai_target_duration, subtitle_max_words, subtitle_max_duration,
                   subtitle_like_template, fmt, projects_dir or CAPCUT_PROJECTS_DIR)
    except Exception as e:
        rep("")
        rep(f"✘ INTERROTTO: {e}")
        log(f"📄 Report (parziale): {rep.save()}")
        if isinstance(e, AIUnavailable):
            raise ValueError(str(e)) from e
        raise
    report_path = rep.save()
    log(f"📄 Report completo delle decisioni: {report_path}")
    return PipelineResult(draft_path=out, report_path=report_path)


def _run(rep: Report, input_path, name, template, script, model, language,
         pacing: Pacing, stabilize_clips, ai_sounds, add_emphasis, emphasis_count,
         drop_fillers, aggressive_fillers, intro_title, clear_template_texts,
         ai_model, use_ai_review, target_duration, subtitle_max_words,
         subtitle_max_duration, subtitle_like_template, fmt, projects_dir: Path) -> Path:
    inputs = gather_inputs(input_path)
    if template and not (projects_dir / template / "draft_info.json").exists():
        raise ValueError(f"Il template «{template}» non esiste più nella cartella progetti di "
                         f"CapCut ({projects_dir}). Scegline un altro.")

    # 0 — no Claude, no montage
    rep.section("0 · CONTROLLO AI")
    require_ai(ai_model, rep)
    rep(f"Progetto «{name}» · {len(inputs)} clip · trascrizione Whisper {model} · lingua {language}")
    rep(f"Preferenze passate all'AI: ritmo {pacing.name} · durata "
        f"{f'~{int(target_duration)}s' if target_duration else 'libera'} · filler "
        f"{'da togliere' + (' (anche cioè/tipo/diciamo)' if aggressive_fillers else '') if drop_fillers else 'da lasciare'}"
        f" · copione {'sì' if script else 'no'} · template {template or 'nessuno'}")

    # 1 — transcription of every clip (silent ones too: they may be b-roll)
    rep.section("1 · TRASCRIZIONE")
    if model == "large-v3" and len(inputs) >= 8:
        rep(f"ⓘ {len(inputs)} clip con large-v3: trascrizione lenta, "
            f"per cartelle con molti file valuta 'medium'.")
    clips = []
    for idx, p in enumerate(inputs, 1):
        path = stabilize(p, log=rep) if stabilize_clips else p
        c = probe(path)
        rep(f"• [{idx}/{len(inputs)}] {path.name} ({c.duration:.1f}s)…")
        transcribe(c, model_name=model, language=language)
        rep(f"    → {len(c.words)} parole, da {c.words[0].start:.1f}s a {c.words[-1].end:.1f}s"
            if c.words else "    → nessun parlato")
        clips.append(c)

    # 2 — the AI looks at every clip: talking take, b-roll or unusable?
    rep.section("2 · L'AI GUARDA LE CLIP")
    strip_dir = Path(tempfile.mkdtemp(prefix="capcut_auto_frames_"))
    try:
        strips = [make_strip(c, strip_dir, f"clip{i:02d}") for i, c in enumerate(clips, 1)]
        looks = look_at_clips(clips, strips, ai_model, rep)
    finally:
        shutil.rmtree(strip_dir, ignore_errors=True)
    for c, look in zip(clips, looks):
        best = (f" · tratto migliore {look.best_from:.1f}–{look.best_to:.1f}s"
                if look.kind == "broll" else "")
        rep(f"👁 {c.path.name} → {KIND_LABEL[look.kind]}: {look.shows or '—'}{best}")
        rep(f"      perché: {look.why or '—'}")

    # 3 — the brief: how this video must be built, read from the texts
    rep.section("3 · REGIA: COME DEV'ESSERE IL VIDEO (AI)")
    blueprint = template_blueprint(template, projects_dir) if template else None
    read = []
    if script:
        read.append(f"copione ({len(script.split())} parole)")
    said = sum(len(c.words) for c in clips)
    read.append(f"parlato del girato ({said} parole)" if said else "girato senza parlato")
    if blueprint:
        read.append(f"template «{template}» ({len(blueprint['captions'])} sottotitoli, "
                    f"{len(blueprint['texts'])} testi a schermo, {len(blueprint['sounds'])} suoni)")
    rep("Testi letti dall'AI: " + " · ".join(read))
    brief = plan_brief(clips, looks, name=name, script=script, blueprint=blueprint,
                       model=ai_model, log=rep)
    rep(f"Regia ricavata soprattutto {BRIEF_SOURCE[brief.based_on]}")
    rep(f"Formato: {brief.format or '—'}")
    rep(f"Tono: {brief.tone or '—'}")
    if brief.structure:
        rep("Struttura:")
        for n, (part, what) in enumerate(brief.structure, 1):
            rep(f"   {n}. {part.upper()} — {what}")
    if brief.directions:
        rep("Indicazioni di regia trovate nel copione:")
        for quote, area, do in brief.directions:
            rep(f"   ✎ «{quote}» → [{area}] {do}")
    elif script:
        rep("Nel copione non ci sono indicazioni di regia esplicite.")
    labels = {"cuts": "tagli", "broll": "b-roll", "texts": "testi a schermo", "sounds": "suoni"}
    if brief.guide:
        rep("Consegne per le fasi successive:")
        for key, label in labels.items():
            if brief.guide.get(key):
                rep(f"   · {label}: {brief.guide[key]}")
    rep(f"      perché: {brief.why or '—'}")

    # 4 — the speech, if anyone talks to the audience
    rep.section("4 · MONTAGGIO DEL PARLATO (AI)")
    timeline: list[TimelineSegment] = []
    topic = ""
    spoken = [(c, look) for c, look in zip(clips, looks) if c.words and look.kind != "scarto"]
    if spoken:
        sclips = [c for c, _ in spoken]
        gwords = build_global_words(sclips)
        plan = plan_edit(sclips, gwords, target_duration=target_duration, pacing=pacing.name,
                         drop_fillers=drop_fillers, aggressive_fillers=aggressive_fillers,
                         script=script, model=ai_model, log=rep,
                         looks=[look for _, look in spoken], brief=brief)
        topic = plan.topic
        rep(f"Argomento: {plan.topic or '—'}")
        rep(f"Come l'ha montato: {plan.summary or '—'}")
        rep(f"Soglia silenzi dentro le frasi: {plan.max_pause:.2f}s — {plan.max_pause_why or '—'}")
        rep("")
        blocks = normalize_plan(plan, len(gwords))
        timeline, stats = materialize(plan, blocks, gwords, sclips,
                                      pacing.head_pad, pacing.tail_pad, rep)
        rep.appendix(annotated_transcript(plan, blocks, gwords, sclips))
        rep("")
        if timeline:
            speech_dropped = sum(stats.discarded.values())
            other_silence = max(0.0, stats.raw_duration - stats.kept_duration
                                - speech_dropped - stats.silence_cut)
            rep(f"Riepilogo: girato parlato {stats.raw_duration:.1f}s → reel "
                f"{stats.kept_duration:.1f}s ({len(timeline)} spezzoni)")
            for label, secs in sorted(stats.discarded.items(), key=lambda kv: -kv[1]):
                rep(f"   − {secs:5.1f}s  {label}")
            rep(f"   − {stats.silence_cut:5.1f}s  silenzi dentro le frasi ({stats.silence_cuts} tagli)")
            rep(f"   − {other_silence:5.1f}s  silenzi tra i blocchi e a inizio/fine clip")
        else:
            rep("Nessuno parla al pubblico in questo girato: si passa al montaggio visivo.")
    else:
        rep("Nessuna clip contiene parlato: si passa al montaggio visivo.")

    used = {ts.keep.source.path for ts in timeline}
    broll = [BRoll(c, look) for c, look in zip(clips, looks)
             if look.kind == "broll" and c.path not in used]

    # 5 — the b-roll: over the speech, or as the whole reel
    covers: list[CoverShot] = []
    extras: list[str] = []          # what sits on top of the cut, for the final review
    if timeline:
        rep.section("5 · B-ROLL SOPRA IL PARLATO (AI)")
        if broll:
            cp = plan_covers(timeline, broll, topic=topic, model=ai_model, log=rep,
                             brief=brief)
            rep(f"Come l'ha usato: {cp.summary or '—'}")
            for b, start, end, at, why in cp.covers:
                rep(f"🎞 {b.clip.path.name} {start:.1f}–{end:.1f}s sopra il parlato "
                    f"@ {at:.2f}s ({end - start:.1f}s) — perché: {why or '—'}")
                covers.append(CoverShot(source=b.clip, src_start=start, src_end=end,
                                        timeline_start=at, note=b.look.shows))
                extras.append(f"copertura b-roll @ {at:.1f}s per {end - start:.1f}s: {b.look.shows}")
            for cname, why in cp.not_used:
                rep(f"✘ {cname} non usata — perché: {why or '—'}")
        else:
            rep("In questa cartella non ci sono riprese di copertura.")
    elif broll:
        rep.section("5 · MONTAGGIO SOLO VISIVO (AI)")
        mp = plan_montage(broll, name=name, target_duration=target_duration,
                          pacing=pacing.name, model=ai_model, log=rep, brief=brief)
        topic = mp.topic
        rep(f"Argomento: {mp.topic or '—'}")
        rep(f"Come l'ha costruito: {mp.summary or '—'}")
        rep("")
        cursor = 0.0
        for n, (b, start, end, why) in enumerate(mp.shots, 1):
            rep(f"✔ INQUADRATURA #{n}  [{b.clip.path.name} {start:.1f}–{end:.1f}s · "
                f"{end - start:.1f}s] {b.look.shows}")
            rep(f"      perché: {why or '—'}")
            k = KeepInterval(source=b.clip, src_start=start, src_end=end,
                             muted=True, note=b.look.shows)
            timeline.append(TimelineSegment(keep=k, timeline_start=cursor,
                                            timeline_end=cursor + k.duration))
            cursor += k.duration
        for cname, why in mp.not_used:
            rep(f"✘ {cname} non usata — perché: {why or '—'}")
        if not timeline:
            raise ValueError("L'AI non ha scelto nessuna inquadratura dal girato.")
        rep("")
        rep(f"Riepilogo: {len(timeline)} inquadrature, reel di {cursor:.1f}s. "
            f"L'audio originale delle clip è silenziato (voci di chi riprendeva).")
    else:
        raise ValueError("Nel girato non c'è né parlato rivolto al pubblico né riprese "
                         "utilizzabili: non c'è niente da montare.")

    # 6 — subtitles
    rep.section("6 · SOTTOTITOLI")
    look = template_subtitle_look(template, projects_dir) if template else None
    like_template = bool(look and subtitle_like_template)
    case = {"upper": "TUTTO MAIUSCOLO", "lower": "tutto minuscolo",
            "asis": "maiuscole e minuscole normali"}[look.case] if look else ""
    punct = (f"solo «{look.punctuation}»" if look.punctuation else "tolta") if look else ""
    if like_template:
        subs = build_subtitles(timeline, max_chars=look.max_chars, max_duration=look.max_duration,
                               phrases=True, case=look.case, punctuation=look.punctuation)
    else:
        # sized by hand; letter case and punctuation still follow the template's
        subs = build_subtitles(timeline, max_words=subtitle_max_words,
                               max_duration=subtitle_max_duration,
                               case=look.case if look else "asis",
                               punctuation=look.punctuation if look else None)
    if subs and like_template:
        rep(f"{len(subs)} blocchi di sottotitoli, scritti come i {look.count} del template "
            f"«{template}»: una frase per blocco, fino a {look.max_chars} caratteri e "
            f"{look.max_duration:.1f}s (nel template di solito ~{look.typical_chars} caratteri), "
            f"{case}, punteggiatura {punct}.")
    elif subs:
        why = ("lunghezza scelta da te" if not subtitle_like_template or not template
               else "il template ha troppo pochi sottotitoli per prenderli a modello")
        rep(f"{len(subs)} blocchi di sottotitoli (max {subtitle_max_words} parole, "
            f"max {subtitle_max_duration:.1f}s ciascuno — {why})"
            + (f"; come nel template: {case}, punteggiatura {punct}." if look else "."))
    if subs:
        for sub in subs[:4]:
            rep(f"   [{sub.timeline_start:6.2f}–{sub.timeline_end:6.2f}] {sub.text}")
        if len(subs) > 4:
            rep(f"   … e altri {len(subs) - 4}")
        extras.append("sottotitoli su tutto il parlato")
    else:
        rep("Nessun sottotitolo: nel reel non c'è parlato.")

    emphasis: list[Emphasis] = []
    final_intro = intro_title
    sound_plan = None
    if template:
        # 7 — on-screen texts
        rep.section("7 · TESTI A SCHERMO (AI)")
        tp = plan_texts(timeline, name=name, topic=topic, want_intro=not intro_title,
                        count=emphasis_count if add_emphasis else 0, model=ai_model, log=rep,
                        brief=brief)
        if intro_title:
            rep(f"Titolo iniziale: «{intro_title}» — scelto da te")
        elif tp.intro_title:
            final_intro = tp.intro_title
            rep(f"Titolo iniziale: «{tp.intro_title}» — perché: {tp.intro_why or '—'}")
        for phrase, start, end, why in tp.emphasis:
            rep(f"✚ testo «{phrase}» @ {start:.1f}s — perché: {why or '—'}")
            extras.append(f"testo a schermo «{phrase}» @ {start:.1f}s")
            emphasis.append(Emphasis(text=phrase, timeline_start=start, timeline_end=end, score=99))
        if not tp.emphasis:
            rep("Nessun testo di enfasi: l'AI non ha trovato momenti che lo meritino.")

        # 8 — sounds
        rep.section("8 · SUONI (AI)")
        if ai_sounds:
            inventory = template_sound_inventory(template, projects_dir)
            if inventory["sounds"] or inventory["music"]:
                sound_plan = plan_sounds(timeline, inventory, topic=topic,
                                         model=ai_model, log=rep, brief=brief)
                rep(f"Come li ha usati: {sound_plan.summary or '—'}")
                for mname, keep, why in sound_plan.music:
                    rep(f"♪ musica «{mname}»: {'tenuta come sottofondo' if keep else 'rimossa'}"
                        f" — perché: {why or '—'}")
                for sname, at, why in sound_plan.placements:
                    rep(f"🔊 «{sname}» @ {at:.2f}s — perché: {why or '—'}")
                    extras.append(f"suono «{sname}» @ {at:.1f}s")
                extras += [f"musica di sottofondo «{m}»" for m, keep, _ in sound_plan.music if keep]
                for sname, why in sound_plan.not_used:
                    rep(f"🔇 «{sname}» non usato — perché: {why or '—'}")
            else:
                sound_plan = SoundPlan()      # still applied: clears the old project's audio
                rep("Il template non contiene suoni né musica utilizzabili.")
        else:
            rep("Gestione suoni con AI disattivata: audio del template lasciato com'è.")
    else:
        rep("")
        rep("ⓘ Nessun template: testi di enfasi e suoni non vengono aggiunti.")

    has_music = bool(sound_plan and any(keep for _, keep, _ in sound_plan.music))
    if all(ts.keep.muted for ts in timeline) and not has_music:
        rep("⚠ Il reel non ha audio: le riprese sono silenziate e "
            + ("il template non ha una musica utilizzabile" if template else "non c'è un template")
            + ". Aggiungi una musica in CapCut o scegli un template che ne abbia una.")

    # 9 — write the CapCut draft
    rep.section("9 · DRAFT CAPCUT")
    if template:
        out = build_from_template(
            template_name=template, out_name=name, timeline=timeline, subtitles=subs,
            emphasis=emphasis or None, intro_title=final_intro,
            clear_template_texts_enabled=clear_template_texts,
            sound_plan=sound_plan, covers=covers, projects_dir=projects_dir, log=rep,
        )
        if clear_template_texts:
            rep("Testi residui del template rimossi (restano solo le emoji decorative).")
    else:
        canvas = (1080, 1920) if fmt == "vertical" else (1920, 1080)
        out = build_draft(name=name, timeline=timeline, subtitles=subs,
                          canvas_w=canvas[0], canvas_h=canvas[1], projects_dir=projects_dir,
                          covers=covers)
    if covers:
        rep(f"{len(covers)} coperture b-roll su una traccia video sopra il parlato (mute).")
    rep(f"✓ Draft scritto: {out}")

    # 10 — critique
    if use_ai_review:
        rep.section("10 · RECENSIONE DELL'AI")
        if final_intro:
            extras.insert(0, f"titolo iniziale a schermo «{final_intro}»")
        for line in review(timeline, ai_model, extras, brief=brief).splitlines():
            rep("  " + line)

    return out
