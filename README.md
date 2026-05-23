# capcut-auto

Generatore automatico di progetti CapCut per video social brevi.
Da una cartella di video genera un draft già pronto con:

- silenzi tagliati e ritmo serrato (3 preset di pacing)
- sottotitoli word-level chunkati
- musica/SFX/stile sottotitoli ereditati da un draft template (es. *CASA RIFUGIO*)
- SFX di transizione (whoosh/swish/riser) ridistribuiti sui nuovi punti di stacco
- musica di sottofondo allungata sulla nuova durata
- testi di enfasi automatici sui numeri / parole impattanti del parlato
- stabilizzazione opzionale (attiva la funzione integrata di CapCut, nessun re-encode)

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

Serve `ffmpeg`/`ffprobe` nel PATH (`brew install ffmpeg`).

## GUI

```bash
.venv/bin/capcut-auto-gui
```

Finestra a 4 tab:

| Tab | Cosa contiene |
|---|---|
| **Input** | Cartella o file video, nome progetto, template CapCut (default *CASA RIFUGIO*), script opz. |
| **Tagli e ritmo** | Pacing (normal/fast/aggressive), parole per sottotitolo, durata max sottotitolo |
| **Stile & effetti** | Switch: ridistribuisci SFX, aggiungi testi di enfasi (+ quantità), stabilizzazione |
| **Output** | Modello Whisper, lingua, formato verticale/orizzontale |

## CLI

```bash
# minimo
.venv/bin/capcut-auto /path/cartella --name "Reel 23"

# con template CASA RIFUGIO + pacing serrato
.venv/bin/capcut-auto /path/cartella --name "Reel 23" \
  --template "CASA RIFUGIO" --pacing fast

# tutto attivo: stabilizzazione CapCut, ridistribuisci SFX, enfasi, modello migliore
.venv/bin/capcut-auto /path/cartella --name "Reel 23" \
  --template "CASA RIFUGIO" --pacing aggressive \
  --stabilize --model medium

# disabilita features specifiche
.venv/bin/capcut-auto /path/video.mov --name "Reel" \
  --template "CASA RIFUGIO" --no-emphasis --no-sfx-redistribute
```

### Opzioni principali

- `--template, -t` — nome di un draft CapCut esistente. Musica, SFX, effetti, transizioni e stile sottotitoli sono ereditati. Il software sostituisce solo la pista video principale e quella dei sottotitoli e ridistribuisce gli SFX sui nuovi tagli.
- `--pacing` — `normal` / `fast` (default) / `aggressive`. Quanto stretti i tagli:
  - normal: silenzi ≥ 0.45s tagliati, pad 0.07s
  - fast: silenzi ≥ 0.25s tagliati, pad 0.04s
  - aggressive: silenzi ≥ 0.12s tagliati, no pad finale
- `--stabilize` — attiva la stabilizzazione integrata di CapCut sulle clip (la calcola CapCut all'apertura del progetto, niente re-encode).
- `--no-sfx-redistribute` — NON spostare gli SFX di transizione sui nuovi tagli.
- `--no-emphasis` — NON aggiungere testi di enfasi automatici.
- `--emphasis-count` — quanti testi di enfasi al massimo (default 4).
- `--subtitle-max-words` — parole per blocco sottotitolo (default 4).
- `--subtitle-max-duration` — durata massima blocco (default 1.4s).
- `--model` — Whisper: `tiny|base|small|medium|large-v3`.
- `--language` — codice lingua (default `it`).
- `--script` — file di testo: tiene solo le parti del parlato che matchano lo script.

Tutto il resto del template (overlay video, filtri, regolazioni colore, text overlay non-sottotitoli) viene preservato; ciò che cade oltre la nuova durata del video viene troncato/scartato.

## Architettura

```
src/capcut_auto/
├── cli.py          # entry point CLI (click)
├── gui.py          # entry point GUI (customtkinter)
├── pipeline.py     # orchestrator: probe → transcribe → cut → subtitle → emphasis → draft
├── probe.py        # ffprobe wrapper
├── transcribe.py   # faster-whisper, word-level timestamps
├── cuts.py         # pacing presets + silence-cut + script-driven cuts
├── subtitles.py    # word chunking
├── emphasis.py     # detect emphatic moments (numbers, keywords, !)
├── draft.py        # CapCut draft writer (from scratch)
├── template.py     # CapCut draft writer (template-based) + SFX redistribution
└── models.py       # dataclasses
```

## Note di fragilità

Il formato `.draft` di CapCut non è ufficiale. Il template-mode è meno fragile
del from-scratch: clona lo schema da un draft esistente prodotto dalla tua
versione di CapCut. Se aggiorni CapCut e qualcosa rompe, di solito basta
ricreare il draft template nella nuova versione di CapCut e rilanciare.
