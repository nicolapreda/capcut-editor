"""CLI entrypoint: scan input, transcribe, cut, generate CapCut draft."""
from __future__ import annotations

from pathlib import Path

import click

from .draft import CAPCUT_PROJECTS_DIR
from .pipeline import run_pipeline


@click.command()
@click.argument("input_path", type=click.Path(exists=True, path_type=Path))
@click.option("--name", "-n", required=True, help="Nome del draft mostrato in CapCut.")
@click.option("--template", "-t", default=None,
              help="Nome di un draft CapCut esistente da usare come template.")
@click.option("--script", "-s", type=click.Path(exists=True, path_type=Path),
              help="Script opzionale: tiene solo le parti che matchano le frasi nel file.")
@click.option("--pacing", type=click.Choice(["normal", "fast", "aggressive"]),
              default="fast", help="Quanto stretti i tagli (default: fast).")
@click.option("--stabilize", "stabilize_clips", is_flag=True,
              help="Attiva la stabilizzazione integrata di CapCut sulle clip.")
@click.option("--no-sfx-redistribute", "redistribute_sfx_enabled", flag_value=False,
              default=True, help="NON ridistribuire gli SFX sui nuovi tagli.")
@click.option("--no-emphasis", "add_emphasis", flag_value=False, default=True,
              help="NON aggiungere testo di enfasi automatico.")
@click.option("--emphasis-count", default=4, help="Quanti overlay di enfasi al massimo.")
@click.option("--model", "-m", default="small",
              help="Modello Whisper: tiny|base|small|medium|large-v3.")
@click.option("--language", "-l", default="it", help="Codice lingua (default: it)")
@click.option("--subtitle-max-words", default=4, help="Parole per blocco sottotitolo.")
@click.option("--subtitle-max-duration", default=1.4, help="Durata massima blocco sottotitolo (s).")
@click.option("--format", "fmt", type=click.Choice(["vertical", "horizontal"]), default="vertical")
@click.option("--projects-dir", type=click.Path(path_type=Path), default=None,
              help=f"Directory progetti CapCut (default: {CAPCUT_PROJECTS_DIR})")
def main(input_path, name, template, script, pacing, stabilize_clips,
         redistribute_sfx_enabled, add_emphasis, emphasis_count,
         model, language, subtitle_max_words, subtitle_max_duration,
         fmt, projects_dir):
    """Genera un draft CapCut da INPUT_PATH (file video o cartella di video)."""
    script_text = script.read_text() if script else None
    try:
        run_pipeline(
            input_path=input_path, name=name, template=template,
            script=script_text, pacing=pacing,
            stabilize_clips=stabilize_clips,
            redistribute_sfx_enabled=redistribute_sfx_enabled,
            add_emphasis=add_emphasis, emphasis_count=emphasis_count,
            model=model, language=language,
            subtitle_max_words=subtitle_max_words,
            subtitle_max_duration=subtitle_max_duration,
            fmt=fmt, projects_dir=projects_dir,
            log=click.echo,
        )
    except ValueError as e:
        raise click.ClickException(str(e))


if __name__ == "__main__":
    main()
