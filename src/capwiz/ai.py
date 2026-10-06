"""Claude is the editor.

Every editorial choice is made by Claude and comes back with a reason:
  * look_at_clips — looks at frames of every clip (and its transcript) to tell
                  talking takes from b-roll from unusable footage;
  * plan_brief  — reads the script (stage directions included), what was said
                  and how the template's own video is built, and decides how
                  this video must be; every later step follows it;
  * plan_edit   — what to keep / discard (behind the scenes, retakes, repeats,
                  off-topic, fillers, silences) and the pause threshold;
  * plan_covers — where b-roll covers the talking footage;
  * plan_montage — an images-only sequence when nobody speaks to the audience;
  * plan_texts  — intro title + emphasis overlays;
  * plan_sounds — which sound effects to use, where, and which to leave out;
  * review      — a final critique of the montage.

There is no heuristic fallback: without a working Claude connection the
pipeline stops (`require_ai`). Two ways to reach Claude, tried in order:

1. **`claude` CLI** (Claude Code) — uses the user's own login.
2. **Anthropic SDK** — needs `ANTHROPIC_API_KEY`.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .edit import EditPlan, EditRange, GWord
from .models import SourceClip, TimelineSegment


class AIUnavailable(RuntimeError):
    pass


# Aliases understood by the CLI → full ids for the SDK.
MODEL_IDS = {
    "sonnet": "claude-sonnet-5-5",
    "opus": "claude-opus-5-5",
    "haiku": "claude-haiku-4-5-20251001",
}
DEFAULT_MODEL = "sonnet"

EDITOR_SYSTEM = (
    "Sei un montatore video professionista specializzato in reel social brevi "
    "in italiano (Instagram, TikTok). Prendi decisioni editoriali precise e le "
    "motivi sempre in modo concreto, citando cosa viene detto. Rispondi sempre "
    "e solo con JSON valido, senza testo prima o dopo."
)
REVIEW_SYSTEM = (
    "Sei un montatore video esperto di reel social in italiano. Dai giudizi "
    "sintetici, concreti e utili a migliorare il montaggio."
)


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

_CLAUDE_CLI_FALLBACKS = (
    Path.home() / ".claude/local/claude",
    Path("/usr/local/bin/claude"),
    Path("/opt/homebrew/bin/claude"),
)


def _find_claude_cli() -> str | None:
    found = shutil.which("claude")
    if found:
        return found
    for p in _CLAUDE_CLI_FALLBACKS:
        if p.exists() and os.access(p, os.X_OK):
            return str(p)
    return None


def _clean_env() -> dict[str, str]:
    """Environment for spawning `claude` without inheriting a host session.

    When this app is launched from a terminal inside the Claude desktop app,
    variables like CLAUDECODE / CLAUDE_CODE_* / ANTHROPIC_BASE_URL leak in and
    make the nested CLI act as a child session: it ignores the Keychain login
    and reports "Not logged in". Stripping them lets it use the user's own login.
    """
    env = dict(os.environ)
    hosted = "CLAUDECODE" in env
    for key in list(env):
        if key.startswith("CLAUDE") or key in ("AI_AGENT", "BAGGAGE"):
            env.pop(key, None)
    if hosted:
        env.pop("ANTHROPIC_BASE_URL", None)   # host's proxy, not the user's
    return env


def _call_claude_cli(prompt: str, system: str, model: str, cli: str,
                     images: list[Path] | None = None, timeout: int = 600) -> str:
    """One-shot call through the Claude Code CLI.

    `--system-prompt` replaces Claude Code's software-engineering prompt (which
    made some models refuse a video-editing task) and `--tools ""` turns it into
    a plain text completion. The prompt goes through stdin so long transcripts
    don't hit argv limits.

    With `images`, the only tool enabled is Read and the working directory is
    the folder holding them, so Claude can look at the frames and nothing else.
    """
    cwd = tempfile.gettempdir()
    if images:
        cwd = str(images[0].parent)
        prompt += ("\n\nIMMAGINI — leggile tutte con lo strumento Read prima di rispondere:\n"
                   + "\n".join(str(p) for p in images))
    args = [cli, "-p", "--model", model, "--system-prompt", system,
            "--tools", "Read" if images else "",
            "--no-session-persistence", "--strict-mcp-config"]
    try:
        result = subprocess.run(
            args, input=prompt, capture_output=True, text=True, timeout=timeout,
            check=True, env=_clean_env(), cwd=cwd,
        )
        return result.stdout
    except subprocess.TimeoutExpired as e:
        raise AIUnavailable(f"Claude non ha risposto entro {timeout}s") from e
    except subprocess.CalledProcessError as e:
        # "Not logged in · Please run /login" arrives on STDOUT with exit 1, and
        # harmless warnings (e.g. about stdin) can sit on STDERR: show both,
        # minus the warnings, so a warning never hides the real cause.
        lines = [ln for ln in f"{e.stdout or ''}\n{e.stderr or ''}".splitlines()
                 if ln.strip() and not ln.lstrip().lower().startswith("warning")]
        detail = " · ".join(lines) or "errore sconosciuto"
        if "logged in" in detail.lower() or "/login" in detail.lower():
            detail = ("Claude CLI non autenticato: apri un terminale, lancia `claude` "
                      "ed esegui `/login` una volta (oppure imposta ANTHROPIC_API_KEY).")
        raise AIUnavailable(detail[:300]) from e


def _call_anthropic_sdk(prompt: str, system: str, model: str,
                        images: list[Path] | None = None) -> str:
    try:
        from anthropic import Anthropic
    except ImportError as e:
        raise AIUnavailable("Pacchetto `anthropic` non installato (pip install anthropic).") from e
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise AIUnavailable("ANTHROPIC_API_KEY non impostata.")
    content: list[dict] = []
    for p in images or []:
        content.append({"type": "text", "text": f"Immagine {p.name}:"})
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/jpeg",
            "data": base64.standard_b64encode(p.read_bytes()).decode()}})
    content.append({"type": "text", "text": prompt})
    msg = Anthropic().messages.create(
        model=MODEL_IDS.get(model, model), max_tokens=16000, system=system,
        messages=[{"role": "user", "content": content}],
    )
    return "".join(b.text for b in msg.content if hasattr(b, "text"))


def _call_claude(prompt: str, system: str = EDITOR_SYSTEM, model: str = DEFAULT_MODEL,
                 images: list[Path] | None = None) -> str:
    cli = _find_claude_cli()
    if cli:
        return _call_claude_cli(prompt, system, model, cli, images)
    return _call_anthropic_sdk(prompt, system, model, images)


def ai_status() -> dict:
    """How (if at all) Claude is reachable — cheap, doesn't call the model."""
    cli = _find_claude_cli()
    key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    method = "claude-cli" if cli else ("anthropic-key" if key else None)
    return {"available": bool(cli or key), "method": method,
            "claude_cli": cli, "anthropic_key": key}


def ai_ping(model: str = DEFAULT_MODEL) -> dict:
    """Fire a trivial request to verify Claude actually answers."""
    st = ai_status()
    if not st["available"]:
        return {"ok": False, "method": None,
                "message": "Claude non è collegato: né il CLI `claude` né ANTHROPIC_API_KEY sono disponibili."}
    try:
        reply = _call_claude('Rispondi con questo JSON: {"ok": true}',
                             model=model).strip()
        return {"ok": True, "method": st["method"], "message": reply[:120] or "OK"}
    except AIUnavailable as e:
        return {"ok": False, "method": st["method"], "message": str(e)}


def require_ai(model: str, log: Callable[[str], None]) -> None:
    """Stop the pipeline unless Claude answers. No Claude, no montage."""
    st = ai_status()
    via = ("Claude Code CLI" if st["method"] == "claude-cli"
           else "API key Anthropic" if st["method"] else None)
    if not via:
        raise AIUnavailable(
            "L'AI è obbligatoria e Claude non è collegato. Installa il CLI "
            "(npm install -g @anthropic-ai/claude-code) e fai `claude` → `/login`, "
            "oppure imposta ANTHROPIC_API_KEY.")
    t0 = time.time()
    res = ai_ping(model)
    if not res["ok"]:
        raise AIUnavailable(f"L'AI è obbligatoria ma Claude non risponde: {res['message']}")
    log(f"✓ Claude collegato via {via} · modello {model} · risposta in {time.time() - t0:.1f}s")


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(raw: str) -> dict:
    m = _JSON_RE.search(raw or "")
    if not m:
        raise ValueError(f"nessun JSON nella risposta: {raw[:160]!r}")
    return json.loads(m.group(0))


def _ask_json(prompt: str, model: str, log: Callable[[str], None], what: str,
              images: list[Path] | None = None) -> dict:
    """Call Claude and parse its JSON; retry once if the answer isn't valid JSON."""
    last_err = ""
    for attempt in (1, 2):
        t0 = time.time()
        raw = _call_claude(prompt if attempt == 1 else
                           prompt + "\n\nATTENZIONE: la risposta precedente non era JSON "
                           "valido. Rispondi SOLO con l'oggetto JSON richiesto.",
                           model=model, images=images)
        try:
            data = _extract_json(raw)
            log(f"  ⓘ Claude ha risposto ({what}) in {time.time() - t0:.1f}s")
            return data
        except (ValueError, json.JSONDecodeError) as e:
            last_err = str(e)
            log(f"  ⚠ risposta di Claude non valida ({what}), riprovo… [{last_err[:80]}]")
    raise AIUnavailable(f"Claude non ha restituito JSON valido per {what}: {last_err}")


def _str(v) -> str:
    return str(v or "").strip()


def _float(v, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ===========================================================================
# 0. SGUARDO — look at every clip: talking head, b-roll or unusable?
# ===========================================================================

_LOOK_PROMPT = """Devi capire cosa contiene ogni clip del girato, guardandola e \
ascoltandola. Per ogni clip hai un'immagine con i fotogrammi in ordine di tempo (da \
sinistra a destra, riga per riga dall'alto), la trascrizione dell'audio e, quando cambia, \
la LUMINOSITÀ misurata sul video al decimo di secondo. Un salto di luminosità può essere \
una luce accesa o spenta, una tapparella o una porta che si apre, oppure solo qualcuno \
che passa davanti all'obiettivo: capiscilo dai fotogrammi. Quando è l'azione della \
ripresa, quei secondi ti dicono esattamente dove accade.
{script}
{clips}

Per ogni clip indica:
- "shows": cosa SUCCEDE nella ripresa, in una frase concreta. Descrivi l'azione, non la \
tecnica: una stanza che passa dal buio alla luce è una tapparella che si alza o una luce \
che viene accesa, non «l'esposizione che cambia»;
- "meaning": a quale punto del copione corrisponde questa ripresa, con le parole del \
copione (per esempio «tapparelle su»). Ogni ripresa è stata girata per uno scopo: \
cercalo nel copione. Stringa vuota solo se non c'è un copione o la ripresa non \
corrisponde a nulla;
- "kind":
    "parlato" se c'è una persona che parla al pubblico (in camera, o un presentatore in scena);
    "broll"   se sono immagini di copertura: ambienti, dettagli, esterni, azioni, senza \
qualcuno che parla al pubblico (le voci che si sentono sono di chi sta riprendendo);
    "scarto"  solo se le IMMAGINI sono inutilizzabili: coperte, molto mosse, ripresa \
partita per sbaglio. Una ripresa che parte buia NON è uno scarto se il buio fa parte \
dell'azione. Le voci fuori campo NON rendono una clip uno scarto: l'audio del b-roll \
viene comunque silenziato, quindi giudica soltanto ciò che si vede. Anche un dettaglio \
breve o un gesto (una mano che apre una porta, che preme un pulsante) è b-roll;
- "best_from" / "best_to": solo per i broll, il tratto da usare, in secondi dentro la \
clip (al massimo 5 secondi). Deve contenere il momento in cui l'azione SI VEDE ACCADERE: \
se il senso della ripresa è un cambiamento (una tapparella che sale, una luce che si \
accende, un oggetto che viene posato), il tratto giusto è quello del cambiamento — tra \
l'ultimo fotogramma «prima» e il primo «dopo», o intorno al secondo indicato dalla \
luminosità — anche se parte buio. Un passaggio dal buio alla luce in una ripresa di luci, \
tapparelle o finestre È l'azione: va messo al centro del tratto, mai evitato come un \
difetto di esposizione. Non scegliere il tratto finale solo perché è più luminoso o più \
stabile: lì l'azione è già finita. Solo se nella ripresa non succede nulla scegli il \
tratto più bello e stabile;
- "why": perché l'hai classificata così e perché hai scelto quel tratto.

Rispondi con questo JSON:
{{"clips": [{{"n": 1, "shows": "...", "meaning": "...", "kind": "broll", "best_from": 1.0, "best_to": 4.0, "why": "..."}}]}}"""

KIND_LABEL = {"parlato": "PARLATO", "broll": "B-ROLL", "scarto": "SCARTO"}


@dataclass
class ClipLook:
    shows: str = ""
    meaning: str = ""            # what the shot is for, in the script's own words
    light: str = ""              # measured brightness changes ("sale di colpo a 2.4s")
    kind: str = "broll"          # parlato | broll | scarto
    best_from: float = 0.0
    best_to: float = 0.0
    why: str = ""


def look_at_clips(clips: list[SourceClip], strips: list[tuple[Path | None, list[float]]],
                  model: str, log: Callable[[str], None],
                  script: str | None = None, lights: list[str] | None = None) -> list[ClipLook]:
    """One ClipLook per clip, in order. With a script, each shot is read for the
    role the script gives it. Clips whose frames couldn't be extracted are
    classified from the audio alone."""
    blocks, images = [], []
    for i, (clip, (strip, times)) in enumerate(zip(clips, strips), 1):
        heard = " ".join(w.text for w in clip.words)
        lines = [f"CLIP {i} · {clip.path.name} · durata {clip.duration:.1f}s"]
        if strip:
            images.append(strip)
            lines.append(f"  immagine: {strip.name} · fotogrammi ai secondi "
                         + ", ".join(f"{t:.1f}" for t in times))
        else:
            lines.append("  immagine: non disponibile")
        if lights and lights[i - 1]:
            lines.append(f"  luminosità: {lights[i - 1]}")
        lines.append(f"  audio: «{heard[:600]}»" if heard else "  audio: nessun parlato")
        blocks.append("\n".join(lines))

    script_txt = ""
    if script:
        script_txt = ("\nCOPIONE DEL VIDEO — spiega a cosa servono le riprese. Leggilo prima di "
                      "guardare le clip e interpreta ogni ripresa per il ruolo che ha qui:\n"
                      + script.strip()[:8000] + "\n")
    data = _ask_json(_LOOK_PROMPT.format(clips="\n\n".join(blocks), script=script_txt),
                     model, log, "sguardo alle clip", images=images or None)
    by_n = {int(_float(it.get("n"), 0)): it for it in data.get("clips") or []
            if isinstance(it, dict)}
    looks = []
    for i, clip in enumerate(clips, 1):
        it = by_n.get(i, {})
        kind = _str(it.get("kind")).lower()
        if kind not in KIND_LABEL:
            kind = "parlato" if clip.words else "broll"
        start = max(0.0, min(_float(it.get("best_from"), 0.0), clip.duration))
        end = max(start, min(_float(it.get("best_to"), clip.duration), clip.duration))
        if end - start < 0.5:                       # no usable hint → whole clip
            start, end = 0.0, clip.duration
        looks.append(ClipLook(shows=_str(it.get("shows")),
                              meaning=_str(it.get("meaning")) if script else "",
                              light=lights[i - 1] if lights else "", kind=kind,
                              best_from=start, best_to=end, why=_str(it.get("why"))))
    return looks


# ===========================================================================
# 0b. REGIA — how the video should be built, read from the script and the words
# ===========================================================================

_BRIEF_PROMPT = """Prima di toccare il montaggio devi capire COME deve essere costruito \
questo video. Parti dai testi: {sources}

{script}{footage}{reference}
Scrivi la regia del video:
- "based_on": da cosa l'hai ricavata soprattutto: "copione", "parlato" oppure "immagini";
- "format": che tipo di video è, in una riga;
- "tone": tono e ritmo;
- "structure": le parti del video in ordine (hook, sviluppo…, call to action), ognuna con \
cosa deve contenere, citando le frasi del copione o del parlato a cui corrisponde;
- "directions": le INDICAZIONI DI REGIA scritte nel copione — tra parentesi, in maiuscolo, \
note a margine, titoli di sezione. Sono VINCOLANTI e vanno riportate TUTTE, una per una, \
senza saltarne: quali riprese (b-roll) e in che ordine, il ritmo dei tagli, la musica, i \
suoni, i testi a schermo, gli stacchi. Per ognuna: "quote" = le parole esatte, "area" = \
broll | ritmo | musica | suoni | testi | taglio, "do" = cosa va fatto in pratica, con le \
clip del girato che servono. Non inventarne: se non ce ne sono, lista vuota. L'unica cosa \
da ignorare è la DURATA TOTALE del video scritta nel copione (spesso non viene \
rispettata): non usarla mai come obiettivo. La durata dei singoli tagli invece è ritmo, \
e conta;
- "cut_seconds": se il copione indica quanto dura ogni taglio della sequenza di immagini \
(per esempio «CUT da 0,5s»), quel numero in secondi; altrimenti null;
- "music": la musica che il copione chiede (per esempio «audio del trend»), con le sue \
parole; stringa vuota se non ne parla;
- "missing": ciò che il copione chiede e che NON c'è nel materiale: una ripresa prevista \
che nel girato manca, una musica o un suono che il template non ha. Per ognuna "what" e \
"why". Servirà a dire all'utente cosa aggiungere a mano; lista vuota se c'è tutto;
- "caption": la didascalia del post, se il copione la riporta (non va nel video); \
altrimenti stringa vuota;
- "guide": una consegna pratica per ogni fase del montaggio — "cuts" (cosa è essenziale e \
non va perso, cosa è accessorio), "broll" (quali immagini servono e su quali frasi), \
"texts" (quali testi a schermo e in che momenti), "sounds" (dove un suono ha senso e \
dove no);
- "why": perché questa è la regia giusta per questo materiale.

Rispondi con questo JSON:
{{
  "based_on": "copione",
  "format": "...",
  "tone": "...",
  "structure": [{{"part": "hook", "what": "..."}}],
  "directions": [{{"quote": "...", "area": "broll", "do": "..."}}],
  "cut_seconds": null,
  "music": "",
  "missing": [{{"what": "...", "why": "..."}}],
  "caption": "",
  "guide": {{"cuts": "...", "broll": "...", "texts": "...", "sounds": "..."}},
  "why": "..."
}}"""

BRIEF_SOURCE = {"copione": "dal copione", "parlato": "dal parlato del girato",
                "immagini": "dalle immagini del girato"}
_STEP_AREA = {"cuts": "taglio", "broll": "broll", "texts": "testi", "sounds": "suoni"}
DIRECTION_AREAS = ("broll", "ritmo", "musica", "suoni", "testi", "taglio")


@dataclass
class Brief:
    """The direction of the video, decided before any cut and followed by every step."""
    based_on: str = "parlato"
    format: str = ""
    tone: str = ""
    structure: list[tuple[str, str]] = field(default_factory=list)          # part, what
    directions: list[tuple[str, str, str]] = field(default_factory=list)    # quote, area, do
    cut_seconds: float | None = None       # length of each cut, when the script states it
    music: str = ""                        # the music the script asks for
    missing: list[tuple[str, str]] = field(default_factory=list)            # what, why
    caption: str = ""                      # the post's caption (not part of the video)
    guide: dict[str, str] = field(default_factory=dict)                     # cuts/broll/texts/sounds
    why: str = ""

    def for_step(self, step: str) -> str:
        """The brief as a prompt block for one step (cuts, broll, texts, sounds, review)."""
        lines = [f"REGIA DEL VIDEO — l'hai decisa tu all'inizio partendo "
                 f"{BRIEF_SOURCE.get(self.based_on, 'dai testi')}. Seguila:"]
        if self.format:
            lines.append(f"- Formato: {self.format}")
        if self.tone:
            lines.append(f"- Tono: {self.tone}")
        if self.structure:
            lines.append("- Struttura: " + " → ".join(
                f"{i}. {part}: {what}" for i, (part, what) in enumerate(self.structure, 1)))
        if self.guide.get(step):
            lines.append(f"- Consegna per questa fase: {self.guide[step]}")
        if self.cut_seconds:
            lines.append(f"- RITMO indicato dal copione: ogni taglio della sequenza di immagini "
                         f"dura {self.cut_seconds:g} secondi.")
        if self.music:
            lines.append(f"- MUSICA indicata dal copione: {self.music}")
        if self.missing:
            lines.append("- Già segnalato all'utente come mancante nel materiale: "
                         + "; ".join(what for what, _ in self.missing))
        if self.directions:
            # every step sees every direction: a rhythm note matters to the b-roll,
            # a sound cue to the texts, and so on
            lines.append("- Indicazioni del copione, VINCOLANTI (rispettale tutte; se una non "
                         "si può rispettare, dillo nel motivo):")
            lines += [f"    · [{d_area}] «{quote}»: {do}" for quote, d_area, do in self.directions]
        if step == "cuts":
            lines.append("La regia serve a riconoscere le parti del video e ciò che è "
                         "essenziale; i criteri di scarto restano quelli indicati sopra.")
        return "\n".join(lines) + "\n"


def _reference_text(bp: dict) -> str:
    """The template's own video, described for the brief."""
    def span(rows, limit):
        return "\n".join(f"[{a:5.1f}–{b:5.1f}] {text}" for a, b, text in rows[:limit]) or "(nessuno)"

    pace = (f"{bp['shots']} inquadrature in {bp['duration']:.0f}s (una ogni "
            f"{bp['duration'] / bp['shots']:.1f}s)" if bp.get("shots") and bp.get("duration")
            else "non rilevabile")
    sounds = "\n".join(f"[{start:5.1f}] «{name}» ({role}{', ' + format(dur, '.0f') + 's' if role == 'musica' else ''})"
                       for start, dur, name, role in bp.get("sounds", [])[:40]) or "(nessuno)"
    return (
        f"VIDEO DI RIFERIMENTO — il template «{bp['name']}», durata {bp['duration']:.0f}s. È un "
        f"ALTRO video dello stesso formato: da qui prendi solo COME è costruito (ritmo, quando "
        f"compaiono i testi a schermo, come sono usati i suoni), mai il contenuto.\n"
        f"Ritmo: {pace}; {bp.get('overlays', 0)} clip o immagini sovrapposte.\n"
        f"Cosa veniva detto (i suoi sottotitoli):\n{span(bp.get('captions', []), 80)}\n"
        f"Testi a schermo:\n{span(bp.get('texts', []), 40)}\n"
        f"Suoni e musica:\n{sounds}\n\n")


def plan_brief(clips: list[SourceClip], looks: list[ClipLook], *, name: str,
               script: str | None, blueprint: dict | None, model: str,
               log: Callable[[str], None]) -> Brief:
    """Read the script (stage directions included), what was actually said and
    how the template's own video is built, and decide how this video must be."""
    spoken = any(c.words and look.kind != "scarto" for c, look in zip(clips, looks))
    if script:
        sources = ("il copione è la fonte principale — dice cosa va detto e spesso contiene "
                   "indicazioni di regia — e il parlato del girato ti dice cosa è stato "
                   "registrato davvero.")
    elif spoken:
        sources = ("non c'è un copione, quindi ricava la struttura da ciò che viene detto "
                   "nel girato.")
    else:
        sources = ("non c'è né copione né parlato rivolto al pubblico, quindi ricava la "
                   "struttura da ciò che mostrano le immagini.")
    if blueprint:
        sources += " Il video di riferimento ti mostra come sono costruiti i video di questo formato."

    blocks, budget = [], 16000
    for i, (c, look) in enumerate(zip(clips, looks), 1):
        heard = " ".join(w.text for w in c.words)[:min(5000, max(300, budget))]
        budget -= len(heard)
        blocks.append(f"CLIP {i} · {c.path.name} · {c.duration:.1f}s · {KIND_LABEL[look.kind]} — "
                      f"si vede: {look.shows or '—'}"
                      + (f" (nel copione: {look.meaning})" if look.meaning else "") + "\n"
                      + (f"  «{heard}»" if heard else "  (nessun parlato)"))
    prompt = _BRIEF_PROMPT.format(
        sources=sources,
        script=(f"COPIONE (progetto «{name}»):\n{script.strip()[:8000]}\n\n" if script else ""),
        footage="GIRATO — cosa si vede e cosa viene detto in ogni clip:\n" + "\n".join(blocks) + "\n\n",
        reference=_reference_text(blueprint) if blueprint else "",
    )
    data = _ask_json(prompt, model, log, "regia")

    based_on = _str(data.get("based_on")).lower()
    guide = data.get("guide") if isinstance(data.get("guide"), dict) else {}
    cut = _float(data.get("cut_seconds"), 0.0)
    brief = Brief(
        based_on=based_on if based_on in BRIEF_SOURCE
        else "copione" if script else "parlato" if spoken else "immagini",
        format=_str(data.get("format")), tone=_str(data.get("tone")),
        cut_seconds=cut if script and 0.2 <= cut <= 10 else None,
        music=_str(data.get("music")) if script else "",
        caption=_str(data.get("caption")) if script else "",
        guide={k: _str(guide.get(k)) for k in _STEP_AREA if _str(guide.get(k))},
        why=_str(data.get("why")),
    )
    for it in data.get("structure") or []:
        if isinstance(it, dict) and _str(it.get("what")):
            brief.structure.append((_str(it.get("part")) or "parte", _str(it.get("what"))))
    for it in (data.get("directions") or []) if script else []:
        if isinstance(it, dict) and _str(it.get("quote")) and _str(it.get("do")):
            area = _str(it.get("area")).lower().replace("-", "").replace(" ", "")
            brief.directions.append((_str(it.get("quote")),
                                     area if area in DIRECTION_AREAS else "taglio",
                                     _str(it.get("do"))))
    for it in data.get("missing") or []:
        if isinstance(it, dict) and _str(it.get("what")):
            brief.missing.append((_str(it.get("what")), _str(it.get("why"))))
    return brief


# ===========================================================================
# 1. MONTAGGIO — what to keep and what to throw away
# ===========================================================================

PACING_HINTS = {
    "normal": ("naturale: lascia respirare le frasi", 0.45),
    "fast": ("veloce e serrato, da reel", 0.30),
    "aggressive": ("aggressivo: zero respiri, massima densità", 0.18),
}

_EDIT_PROMPT = """Devi montare un reel partendo dal girato grezzo. Qui sotto trovi la \
trascrizione parola per parola di TUTTE le clip del progetto, in ordine. Ogni parola ha \
un indice globale tra parentesi quadre e l'istante in secondi dentro la sua clip. Le \
righe con ⏸ indicano silenzi.

OBIETTIVO: un reel {target}, ritmo {pacing}.

DEVI DECIDERE TU, parola per parola, cosa entra nel montaggio. Scarta:
- DIETRO LE QUINTE: tutto ciò che non è rivolto al pubblico — indicazioni a chi riprende \
("vai", "parti", "3 2 1", "va bene così?", "aspetta", "rifacciamo", "ok stop"), prove, \
commenti, risate fuori contesto, chiacchiere prima o dopo la ripresa.
- FUORI TEMA: frasi che non c'entrano con l'argomento del video.
- RETAKE / RIPETIZIONI / FALSE PARTENZE: se una frase viene detta più volte (anche in \
clip diverse), tieni SOLO la versione migliore (completa, fluida, di solito l'ultima) e \
scarta le altre, indicando nel motivo quale versione hai tenuto.
- ERRORI: frasi impappinate, interrotte a metà.
{fillers}
- SILENZI: i silenzi tra i blocchi tenuti vengono eliminati automaticamente. Dentro un \
blocco tenuto decidi tu la soglia `max_pause`: ogni pausa più lunga verrà tagliata.
{script}{brief}
COSA SI VEDE: accanto a ogni clip trovi cosa mostrano le immagini. Se in una clip non \
c'è nessuno che parla al pubblico (b-roll), le voci sono quasi sempre di chi sta \
riprendendo: scartale come dietro le quinte, salvo che sia chiaramente una narrazione \
rivolta al pubblico. Se in tutto il girato nessuno parla al pubblico, lascia `keep` \
vuoto: il video verrà montato solo con le immagini.

REGOLE:
- Tieni frasi complete, mai a metà parola o a metà concetto.
- Mantieni l'ordine cronologico, non riordinare.
- OGNI indice deve comparire in un intervallo di `keep` oppure di `discard`.
- Gli intervalli `from`–`to` sono inclusivi e non si sovrappongono.
- Il motivo (`why`) deve essere concreto e citare cosa viene detto.

Ruoli per `keep`: hook, contenuto, prova, cta, transizione.
Tipi per `discard`: dietro_le_quinte, fuori_tema, retake, ripetizione, falsa_partenza, \
filler, errore, silenzio, troppo_lungo.

TRASCRIZIONE:
{transcript}

Rispondi con questo JSON:
{{
  "topic": "di cosa parla il video, in una riga",
  "summary": "2-3 frasi: come hai montato e cosa hai tolto",
  "max_pause": {default_pause},
  "max_pause_why": "perché questa soglia per questo parlante e questo ritmo",
  "keep": [{{"from": 0, "to": 15, "role": "hook", "why": "..."}}],
  "discard": [{{"from": 16, "to": 30, "type": "dietro_le_quinte", "why": "..."}}]
}}"""


def _edit_transcript(clips: list[SourceClip], gwords: list[GWord],
                     looks: list[ClipLook] | None = None) -> str:
    lines: list[str] = []
    for ci, clip in enumerate(clips):
        cw = [gw for gw in gwords if gw.clip_idx == ci]
        lines.append(f"=== CLIP {ci + 1} · {clip.path.name} · durata {clip.duration:.1f}s ===")
        if looks:
            lines.append(f"   SI VEDE ({KIND_LABEL[looks[ci].kind]}): {looks[ci].shows}"
                         + (f" — nel copione: {looks[ci].meaning}" if looks[ci].meaning else ""))
        if not cw:
            lines.append("   (nessun parlato)")
            continue
        if cw[0].word.start > 0.5:
            lines.append(f"   ⏸ silenzio iniziale {cw[0].word.start:.1f}s")
        prev = None
        for gw in cw:
            if prev is not None:
                gap = gw.word.start - prev.word.end
                if gap >= 0.3:
                    lines.append(f"   ⏸ pausa {gap:.2f}s")
            lines.append(f"[{gw.gid}] {gw.word.start:.2f} {gw.word.text}")
            prev = gw
        tail = clip.duration - cw[-1].word.end
        if tail > 0.5:
            lines.append(f"   ⏸ silenzio finale {tail:.1f}s")
    return "\n".join(lines)


def plan_edit(
    clips: list[SourceClip],
    gwords: list[GWord],
    *,
    target_duration: float | None,
    pacing: str,
    drop_fillers: bool,
    aggressive_fillers: bool,
    script: str | None,
    model: str,
    log: Callable[[str], None],
    looks: list[ClipLook] | None = None,
    brief: Brief | None = None,
) -> EditPlan:
    pace_txt, default_pause = PACING_HINTS.get(pacing, PACING_HINTS["fast"])
    if drop_fillers:
        fillers = ("- FILLER: intercalari che non aggiungono nulla (ehm, uhm, ah"
                   + (", cioè, tipo, diciamo, insomma, praticamente" if aggressive_fillers else "")
                   + ").")
    else:
        fillers = "- FILLER: lascia gli intercalari se non disturbano (preferenza dell'utente)."
    script_txt = ""
    if script:
        script_txt = ("\nCOPIONE DI RIFERIMENTO (ciò che il parlante doveva dire — ciò che è "
                      "fuori copione è candidato allo scarto, salvo che migliori il video; se "
                      "indica una durata ignorala, non tagliare contenuto per rientrarci):\n"
                      + script.strip()[:8000] + "\n")
    prompt = _EDIT_PROMPT.format(
        target=f"di circa {int(target_duration)} secondi" if target_duration
        else "della durata giusta per il contenuto (in genere 20-60 secondi)",
        pacing=pace_txt, fillers=fillers, script=script_txt,
        brief="\n" + brief.for_step("cuts") if brief else "",
        transcript=_edit_transcript(clips, gwords, looks), default_pause=default_pause,
    )
    log(f"  Claude sta analizzando {len(gwords)} parole su {len(clips)} clip "
        f"(può richiedere 1-2 minuti)…")
    data = _ask_json(prompt, model, log, "montaggio")

    def ranges(items, key: str) -> list[EditRange]:
        out = []
        for it in items or []:
            if not isinstance(it, dict):
                continue
            try:
                s, e = int(it.get("from")), int(it.get("to"))
            except (TypeError, ValueError):
                continue
            if e >= s:
                out.append(EditRange(s, e, _str(it.get(key)).lower() or "?", _str(it.get("why"))))
        return out

    return EditPlan(
        topic=_str(data.get("topic")),
        summary=_str(data.get("summary")),
        max_pause=min(1.0, max(0.12, _float(data.get("max_pause"), default_pause))),
        max_pause_why=_str(data.get("max_pause_why")),
        keeps=ranges(data.get("keep"), "role"),
        discards=ranges(data.get("discard"), "type"),
    )


# ===========================================================================
# 1b. B-ROLL — cover shots over the speech, or an images-only montage
# ===========================================================================

@dataclass
class BRoll:
    """A clip available as b-roll, with what the AI saw in it."""
    clip: SourceClip
    look: ClipLook


def _broll_catalog(broll: list[BRoll]) -> str:
    return "\n".join(
        f"- B{i} · {b.clip.path.name} · durata {b.clip.duration:.1f}s · mostra: {b.look.shows} "
        + (f"· nel copione: {b.look.meaning} " if b.look.meaning else "")
        + (f"· luminosità misurata: {b.look.light} " if b.look.light else "")
        + f"· tratto in cui succede {b.look.best_from:.1f}–{b.look.best_to:.1f}s"
        for i, b in enumerate(broll, 1)
    )


def _pick_broll(ref, broll: list[BRoll]) -> BRoll | None:
    m = re.search(r"\d+", _str(ref))
    if m and 1 <= int(m.group()) <= len(broll):
        return broll[int(m.group()) - 1]
    return next((b for b in broll if b.clip.path.name.lower() == _str(ref).lower()), None)


def _stretch(it: dict, b: BRoll, lo: float, hi: float,
             exact: float | None = None) -> tuple[float, float]:
    """Clamp the AI's from/to to the clip and to a sane shot length. `exact` is
    the cut length the script dictates: the shot lasts exactly that."""
    d = b.clip.duration
    if exact:
        lo = hi = exact
    lo = min(lo, d)
    start = max(0.0, min(_float(it.get("from"), b.look.best_from), max(0.0, d - lo)))
    end = min(d, max(_float(it.get("to"), start + 2.5), start + lo))
    return start, min(end, start + hi)


def _blocks_text(timeline: list[TimelineSegment]) -> str:
    """The speech cut, one line per block the edit kept."""
    blocks: dict[int, list[TimelineSegment]] = {}
    for ts in timeline:
        blocks.setdefault(ts.keep.block, []).append(ts)
    return "\n".join(
        f"BLOCCO {n} [{segs[0].timeline_start:6.2f}–{segs[-1].timeline_end:6.2f}] «"
        + " ".join(w.text for ts in segs for w in ts.keep.words) + "»"
        for n, segs in blocks.items()
    )


_COVERS_PROMPT = """Questo è il parlato già montato (tempi in secondi, durata {total:.1f}s), \
diviso nei blocchi che hai tenuto. Argomento: {topic}

{timeline}

Riprese (b-roll) disponibili:
{catalog}
{brief}{target}
Il b-roll si può usare in due modi. Decidi tu, seguendo la regia:

A) INSERTO — l'immagine va DA SOLA, a tutto schermo, senza parlato: la reggono la musica \
e i testi a schermo. Serve quando una parte del video è fatta di sole immagini: \
l'apertura, una sequenza di preparazione, uno stacco tra due momenti, la chiusura.
- `before_block` = numero del blocco di parlato PRIMA del quale va l'inserto; usa \
{after_last} per metterlo dopo l'ultimo blocco;
- più inserti con lo stesso `before_block` vanno uno dopo l'altro nell'ordine in cui li scrivi;
- ogni inserto dura quanto serve al ritmo: da 0.4 secondi (sequenze a tagli rapidi) a 5. \
Se la regia indica la durata dei tagli, TUTTI gli inserti durano esattamente quella \
(un testo a schermo può restare sopra più tagli: non allungare un taglio per farlo leggere);
- con tagli rapidi puoi usare più volte la stessa ripresa in momenti diversi dell'azione \
(l'inizio, la metà, la fine) per dare ritmo, ma mai due tagli quasi uguali di fila;
- l'ordine delle riprese indicato dal copione è vincolante: non spostarle e non \
aggiungerne altre dopo l'ultima prevista;
- quando l'azione è un cambio di luce, metti il taglio a cavallo del secondo misurato \
(«luminosità misurata»), così l'accensione si vede dentro il taglio;
- se il video è tutto parlato e la regia non prevede parti di sole immagini, non usarne.

B) COPERTURA — l'immagine va SOPRA chi parla (l'audio del parlato continua):
- solo quando illustra o rafforza ciò che viene detto in quel momento, oppure per \
nascondere uno stacco brusco tra due frasi;
- non coprire i primi 2 secondi di parlato (nell'hook serve il volto) né la call to action;
- ogni copertura dura da 1.5 a 4 secondi; non si sovrappongono; tra una e l'altra lascia \
rivedere il volto; in totale non più di metà del parlato;
- `at` = secondo del PARLATO (i tempi dei blocchi qui sopra) in cui la copertura inizia.

Per entrambi `from`/`to` = tratto della clip da usare, in secondi dentro quella clip: deve \
mostrare l'azione che dà senso alla ripresa (il «tratto in cui succede»), non un momento \
in cui è già finita. Se una ripresa non serve a questo video, non usarla e spiega perché.

Rispondi con questo JSON:
{{
  "summary": "come hai usato il b-roll, in 1-2 frasi",
  "inserts": [{{"clip": "B1", "from": 3.0, "to": 4.0, "before_block": 1, "why": "..."}}],
  "covers": [{{"clip": "B2", "from": 3.0, "to": 5.5, "at": 12.3, "why": "..."}}],
  "not_used": [{{"clip": "B4", "why": "..."}}]
}}"""


@dataclass
class CoverPlan:
    summary: str = ""
    # b-roll shown alone between speech blocks: b, from, to, before_block, why
    inserts: list[tuple[BRoll, float, float, int, str]] = field(default_factory=list)
    # b-roll over the speech: b, from, to, at (speech-cut seconds), why
    covers: list[tuple[BRoll, float, float, float, str]] = field(default_factory=list)
    not_used: list[tuple[str, str]] = field(default_factory=list)


def plan_covers(timeline: list[TimelineSegment], broll: list[BRoll], *, topic: str,
                model: str, log: Callable[[str], None],
                brief: Brief | None = None,
                target_duration: float | None = None) -> CoverPlan:
    """Where the b-roll goes when someone speaks: alone between speech blocks
    (inserts) and/or over the speech (covers)."""
    total = timeline[-1].timeline_end
    last_block = max(ts.keep.block for ts in timeline)
    data = _ask_json(_COVERS_PROMPT.format(total=total, topic=topic or "—",
                                           timeline=_blocks_text(timeline),
                                           catalog=_broll_catalog(broll),
                                           after_last=last_block + 1,
                                           target=(
                                               f"\nDURATA chiesta dall'utente: circa {int(target_duration)} "
                                               f"secondi in tutto. Il parlato ne occupa {total:.1f}: se "
                                               f"manca tempo, riempilo con gli inserti (più tagli per "
                                               f"azione, in momenti diversi), senza ripetizioni.\n"
                                               if target_duration and target_duration > total + 1 else ""),
                                           brief="\n" + brief.for_step("broll") if brief else ""),
                     model, log, "b-roll")
    plan = CoverPlan(summary=_str(data.get("summary")))
    for it in data.get("inserts") or []:
        b = _pick_broll(it.get("clip"), broll) if isinstance(it, dict) else None
        if b is None:
            continue
        start, end = _stretch(it, b, lo=0.4, hi=8.0, exact=brief.cut_seconds if brief else None)
        if end - start >= 0.2:
            before = int(_float(it.get("before_block"), 1))
            plan.inserts.append((b, start, end, max(1, min(before, last_block + 1)),
                                 _str(it.get("why"))))
    busy_until = 0.0
    for it in sorted((c for c in data.get("covers") or [] if isinstance(c, dict)),
                     key=lambda c: _float(c.get("at"), 0.0)):
        b = _pick_broll(it.get("clip"), broll)
        if b is None:
            continue
        start, end = _stretch(it, b, lo=0.8, hi=6.0)
        at = max(busy_until, _float(it.get("at"), 0.0))
        if at + (end - start) > total:              # never spill past the speech
            end = start + (total - at)
        if end - start < 0.8:
            continue
        plan.covers.append((b, start, end, at, _str(it.get("why"))))
        busy_until = at + (end - start)
    for it in data.get("not_used") or []:
        if isinstance(it, dict):
            b = _pick_broll(it.get("clip"), broll)
            plan.not_used.append((b.clip.path.name if b else _str(it.get("clip")),
                                  _str(it.get("why"))))
    return plan


_MONTAGE_PROMPT = """In questo girato nessuno parla al pubblico: devi montare un reel \
fatto solo di immagini (con musica e testi). Progetto: «{name}».

Riprese disponibili:
{catalog}

OBIETTIVO: un reel {target}, ritmo {pacing}.
{brief}
Decidi la sequenza:
- un ordine logico e narrativo (per esempio: esterno → ingresso → interni → dettagli);
- per ogni inquadratura quale tratto della clip usare (quello in cui si vede accadere \
l'azione che le dà senso) e quanto farlo durare (di solito \
da 1.5 a 4 secondi; più lungo solo se il movimento lo merita; anche 0.4-0.8 secondi se la \
regia chiede una sequenza a tagli rapidi);
- puoi usare la stessa clip più volte con tratti diversi, oppure non usarla;
- apri con l'immagine più forte.

Rispondi con questo JSON:
{{
  "topic": "di cosa parla il video, in una riga",
  "summary": "come hai costruito la sequenza, in 1-2 frasi",
  "shots": [{{"clip": "B3", "from": 1.0, "to": 3.5, "why": "..."}}],
  "not_used": [{{"clip": "B2", "why": "..."}}]
}}"""


@dataclass
class MontagePlan:
    topic: str = ""
    summary: str = ""
    shots: list[tuple[BRoll, float, float, str]] = field(default_factory=list)   # b, from, to, why
    not_used: list[tuple[str, str]] = field(default_factory=list)


def plan_montage(broll: list[BRoll], *, name: str, target_duration: float | None,
                 pacing: str, model: str, log: Callable[[str], None],
                 brief: Brief | None = None) -> MontagePlan:
    pace_txt, _ = PACING_HINTS.get(pacing, PACING_HINTS["fast"])
    data = _ask_json(_MONTAGE_PROMPT.format(
        name=name, catalog=_broll_catalog(broll), pacing=pace_txt,
        brief="\n" + brief.for_step("broll") if brief else "",
        target=f"di circa {int(target_duration)} secondi" if target_duration
        else "della durata giusta per le immagini che hai (in genere 15-40 secondi)"),
        model, log, "montaggio visivo")
    plan = MontagePlan(topic=_str(data.get("topic")), summary=_str(data.get("summary")))
    for it in data.get("shots") or []:
        b = _pick_broll(it.get("clip"), broll) if isinstance(it, dict) else None
        if b is None:
            continue
        start, end = _stretch(it, b, lo=0.4, hi=8.0, exact=brief.cut_seconds if brief else None)
        if end - start >= 0.2:
            plan.shots.append((b, start, end, _str(it.get("why"))))
    for it in data.get("not_used") or []:
        if isinstance(it, dict):
            b = _pick_broll(it.get("clip"), broll)
            plan.not_used.append((b.clip.path.name if b else _str(it.get("clip")),
                                  _str(it.get("why"))))
    return plan


# ===========================================================================
# 2. TESTI — intro title + emphasis overlays
# ===========================================================================

def _timeline_text(timeline: list[TimelineSegment]) -> str:
    """One line per segment: the words spoken, or what a b-roll shot shows."""
    mixed = any(ts.keep.muted for ts in timeline) and any(ts.keep.words for ts in timeline)
    return "\n".join(
        f"[{ts.timeline_start:6.2f}–{ts.timeline_end:6.2f}] "
        + (("(chi parla è in video) " if mixed else "") + " ".join(w.text for w in ts.keep.words)
           if ts.keep.words else f"(solo immagine, nessun parlato) {ts.keep.note}")
        for ts in timeline
    )


_TEXTS_PROMPT = """Questo è il reel già montato (tempi in secondi sulla timeline finale). \
Progetto: «{name}». Argomento: {topic}

{timeline}
{brief}
Decidi i testi a schermo:
{intro_rule}
2. `emphasis`: al massimo {count} testi di enfasi (max 4 parole, IN MAIUSCOLO; un testo \
indicato dalla regia va scritto com'è) solo nei momenti che lo meritano davvero (un numero, una rivelazione, un concetto chiave, la CTA). \
Se non serve, mettine meno. `at` = secondo in cui deve comparire. Dove il reel è fatto \
di sole immagini, i testi sono ciò che porta il messaggio: usali per dire cosa si sta \
vedendo, senza inventare fatti che non conosci. Tutto ciò che viene DETTO ha già i suoi \
sottotitoli: non riscrivere a schermo una frase del parlato, sarebbe un doppione. Se il \
copione indica quali testi vanno a schermo, usa quelli e NON aggiungerne altri di tua \
iniziativa.

Rispondi con questo JSON:
{{
  "intro_title": "HOOK",
  "intro_why": "...",
  "emphasis": [{{"phrase": "FRASE", "at": 4.2, "why": "..."}}]
}}"""


@dataclass
class TextPlan:
    intro_title: str | None = None
    intro_why: str = ""
    emphasis: list[tuple[str, float, float, str]] = field(default_factory=list)


def plan_texts(timeline: list[TimelineSegment], *, name: str, topic: str, want_intro: bool,
               count: int, model: str, log: Callable[[str], None],
               display_dur: float = 1.8, brief: Brief | None = None) -> TextPlan:
    intro_rule = ("1. `intro_title`: titolo d'apertura di 1-4 parole IN MAIUSCOLO, un hook "
                  "comprensibile da solo. Se la regia indica un testo d'apertura preciso, usa "
                  "quello così com'è, anche se è più lungo." if want_intro
                  else "1. `intro_title`: lascialo vuoto, il titolo lo ha già scelto l'utente.")
    prompt = _TEXTS_PROMPT.format(name=name, topic=topic or "—",
                                  timeline=_timeline_text(timeline),
                                  intro_rule=intro_rule, count=max(0, count),
                                  brief="\n" + brief.for_step("texts") if brief else "")
    data = _ask_json(prompt, model, log, "testi")
    total = timeline[-1].timeline_end if timeline else 0.0
    plan = TextPlan(
        intro_title=(_str(data.get("intro_title")).upper() or None) if want_intro else None,
        intro_why=_str(data.get("intro_why")),
    )
    for it in (data.get("emphasis") or [])[: max(0, count)]:
        if not isinstance(it, dict):
            continue
        phrase = _str(it.get("phrase")).upper()
        if not phrase:
            continue
        start = max(0.0, min(_float(it.get("at"), 0.0), max(0.0, total - 0.5)))
        plan.emphasis.append((phrase, start, min(total, start + display_dur), _str(it.get("why"))))
    return plan


# ===========================================================================
# 3. SUONI — which sound effects matter, and where
# ===========================================================================

_SOUNDS_PROMPT = """Questo è il reel montato (tempi in secondi sulla timeline finale, \
durata totale {total:.1f}s). Argomento: {topic}

{timeline}

Punti di taglio netti (cambi di scena reali):
{cuts}

Suoni disponibili nel template:
{sounds}

Musica disponibile nel template:
{music}
{brief}
Decidi TU il ruolo di ogni suono e usalo SOLO quando ha una funzione precisa:
- suoni di transizione (whoosh, swish, riser…) solo su cambi di scena reali, non su ogni taglio;
- suoni di enfasi (ding, pop, notifica, cassa, rivelazione…) solo per sottolineare un \
numero, una rivelazione, una CTA;
- niente suoni sopra parole importanti se coprirebbero il parlato; mai due suoni troppo \
vicini; meglio pochi e mirati che tanti;
- `at` = secondo in cui il suono deve INIZIARE (per una transizione, poco prima del taglio);
- puoi usare lo stesso suono più volte, e puoi decidere di non usarne alcuni;
- la musica: decidi se tenerla come sottofondo per tutto il reel. Se la regia indica una \
musica precisa (per esempio «audio del trend», un brano, un genere) e quella del template \
non lo è, NON tenerla (`keep`: false): meglio nessuna musica che una sbagliata, la \
aggiungerà l'utente;
- se il copione chiede un suono che tra questi non c'è, usa il più simile solo se regge \
davvero la stessa funzione, e dillo nel motivo;
- `todo`: SOLO per musica e suoni, ciò che resta da fare A MANO in CapCut perché il \
copione sia rispettato (al massimo 3 voci brevi, senza ripetere ciò che la regia segnala \
già come mancante); lista vuota se non manca nulla.

Usa esattamente i nomi dei suoni indicati. Rispondi con questo JSON:
{{
  "summary": "come hai usato i suoni, in 1-2 frasi",
  "music": [{{"name": "...", "keep": true, "why": "..."}}],
  "placements": [{{"sound": "...", "at": 5.1, "why": "..."}}],
  "not_used": [{{"sound": "...", "why": "..."}}],
  "todo": ["..."]
}}"""


@dataclass
class SoundPlan:
    summary: str = ""
    music: list[tuple[str, bool, str]] = field(default_factory=list)
    placements: list[tuple[str, float, str]] = field(default_factory=list)
    not_used: list[tuple[str, str]] = field(default_factory=list)
    todo: list[str] = field(default_factory=list)      # left to do by hand in CapCut


def scene_cuts(timeline: list[TimelineSegment], min_jump: float = 1.0) -> list[str]:
    """Describe the real cut points (clip change or a content jump)."""
    out = []
    for a, b in zip(timeline, timeline[1:]):
        if a.keep.source.path != b.keep.source.path:
            out.append(f"- @ {b.timeline_start:.2f}s — cambio clip "
                       f"({a.keep.source.path.name} → {b.keep.source.path.name})")
        elif b.keep.src_start - a.keep.src_end >= min_jump:
            out.append(f"- @ {b.timeline_start:.2f}s — salto di contenuto "
                       f"(tolti {b.keep.src_start - a.keep.src_end:.1f}s di girato)")
    return out


def plan_sounds(timeline: list[TimelineSegment], inventory: dict, *, topic: str,
                model: str, log: Callable[[str], None],
                brief: Brief | None = None) -> SoundPlan:
    total = timeline[-1].timeline_end if timeline else 0.0
    cuts = scene_cuts(timeline)
    sounds = "\n".join(
        f'- "{s["name"]}" · {s["duration"]:.1f}s · tipo probabile: {s["category"]} · '
        f'presente {s["count"]} volte nel template'
        for s in inventory.get("sounds", [])
    ) or "- (nessuno)"
    music = "\n".join(f'- "{m["name"]}" · {m["duration"]:.1f}s'
                      for m in inventory.get("music", [])) or "- (nessuna)"
    prompt = _SOUNDS_PROMPT.format(total=total, topic=topic or "—",
                                   timeline=_timeline_text(timeline),
                                   cuts="\n".join(cuts) or "- nessun cambio di scena netto",
                                   sounds=sounds, music=music,
                                   brief="\n" + brief.for_step("sounds") if brief else "")
    data = _ask_json(prompt, model, log, "suoni")
    plan = SoundPlan(summary=_str(data.get("summary")))
    for it in data.get("music") or []:
        if isinstance(it, dict) and _str(it.get("name")):
            plan.music.append((_str(it.get("name")), bool(it.get("keep", True)), _str(it.get("why"))))
    for it in data.get("placements") or []:
        if isinstance(it, dict) and _str(it.get("sound")):
            at = max(0.0, min(_float(it.get("at"), 0.0), total))
            plan.placements.append((_str(it.get("sound")), at, _str(it.get("why"))))
    plan.placements.sort(key=lambda p: p[1])
    for it in data.get("not_used") or []:
        if isinstance(it, dict) and _str(it.get("sound")):
            plan.not_used.append((_str(it.get("sound")), _str(it.get("why"))))
    plan.todo = [_str(t) for t in data.get("todo") or [] if _str(t)][:3]
    return plan


# ===========================================================================
# 4. RECENSIONE — critique of the final montage
# ===========================================================================

_REVIEW_PROMPT = """Valuta questo reel montato (tempi in secondi, durata {total:.1f}s):

{timeline}

Elementi già aggiunti al montaggio:
{extras}
{brief}
Considera hook dei primi 2 secondi, ritmo, struttura, frasi mozze o ripetizioni rimaste, \
efficacia della CTA, e quanto il montato rispetta la regia se è indicata. Non segnalare come mancante ciò che è già elencato qui sopra. \
Rispondi in italiano, massimo 200 parole, in questo formato:

VOTO: X/10

PUNTI FORTI:
- …

DA MIGLIORARE:
- …

SUGGERIMENTO CONCRETO (una sola azione, la più importante):
…"""


def review(timeline: list[TimelineSegment], model: str, extras: list[str] | None = None,
           brief: Brief | None = None) -> str:
    """`extras` lists what was already added on top of the cut (texts, sounds,
    music, b-roll covers) so the critique doesn't call them missing."""
    if not timeline:
        return ""
    prompt = _REVIEW_PROMPT.format(total=timeline[-1].timeline_end,
                                   timeline=_timeline_text(timeline),
                                   extras="\n".join(f"- {e}" for e in extras or [])
                                   or "- nessuno",
                                   brief="\n" + brief.for_step("review") if brief else "")
    return _call_claude(prompt, system=REVIEW_SYSTEM, model=model).strip()
