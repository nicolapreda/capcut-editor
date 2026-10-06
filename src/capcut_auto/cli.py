"""CLI entrypoint: scan input, transcribe, cut, generate CapCut draft."""
from __future__ import annotations

from pathlib import Path

import click

from .draft import CAPCUT_PROJECTS_DIR
from .pipeline import read_script, run_pipeline


@click.command()
@click.argument("input_path", type=click.Path(exists=True, path_type=Path))
@click.option("--name", "-n", required=True, help="Nome del draft mostrato in CapCut.")
@click.option("--template", "-t", default=None,
              help="Nome di un draft CapCut esistente da usare come template.")
@click.option("--script", "-s", type=click.Path(exists=True, path_type=Path),
              help="Copione di riferimento: l'AI lo usa per capire cosa è fuori copione.")
@click.option("--pacing", type=click.Choice(["normal", "fast", "aggressive"]),
              default="fast", help="Ritmo desiderato, passato all'AI (default: fast).")
@click.option("--stabilize", "stabilize_clips", is_flag=True,
              help="Stabilizza i video con ffmpeg vidstab prima di importarli.")
@click.option("--no-ai-sounds", "redistribute_sfx_enabled", flag_value=False,
              default=True, help="NON far gestire i suoni del template all'AI (li lascia com'erano).")
@click.option("--no-emphasis", "add_emphasis", flag_value=False, default=True,
              help="NON aggiungere testi di enfasi.")
@click.option("--emphasis-count", default=4, help="Quanti testi di enfasi al massimo.")
@click.option("--no-fillers", "drop_fillers", flag_value=False, default=True,
              help="Chiedi all'AI di NON togliere gli intercalari (ehm, uhm…).")
@click.option("--aggressive-fillers", is_flag=True,
              help="Chiedi all'AI di togliere anche cioè/tipo/diciamo/insomma.")
@click.option("--intro-title", default=None,
              help="Titolo iniziale scelto da te (altrimenti lo sceglie l'AI).")
@click.option("--clear-template-texts", is_flag=True,
              help="Rimuove tutti i testi residui del template (3,2,1 vai, etichette mid-video…).")
@click.option("--ai-model", type=click.Choice(["sonnet", "opus", "haiku"]), default="sonnet",
              help="Modello Claude che fa il montaggio (default: sonnet).")
@click.option("--no-ai-review", "use_ai_review", flag_value=False, default=True,
              help="Salta la recensione finale del montato.")
@click.option("--ai-target-duration", type=float, default=None,
              help="Durata target in secondi che l'AI deve cercare di ottenere con i tagli.")
@click.option("--model", "-m", default="large-v3",
              help="Modello Whisper: tiny|base|small|medium|large-v3 (default: large-v3).")
@click.option("--language", "-l", default="it", help="Codice lingua (default: it)")
@click.option("--manual-subtitles", "subtitle_like_template", flag_value=False, default=True,
              help="NON scrivere i sottotitoli come quelli del template: usa le due opzioni qui sotto.")
@click.option("--subtitle-max-words", default=4,
              help="Parole per blocco sottotitolo (solo con --manual-subtitles o senza template).")
@click.option("--subtitle-max-duration", default=1.4,
              help="Durata massima blocco sottotitolo in secondi (come sopra).")
@click.option("--format", "fmt", type=click.Choice(["vertical", "horizontal"]), default="vertical")
@click.option("--projects-dir", type=click.Path(path_type=Path), default=None,
              help=f"Directory progetti CapCut (default: {CAPCUT_PROJECTS_DIR})")
def main(input_path, name, template, script, pacing, stabilize_clips,
         redistribute_sfx_enabled, add_emphasis, emphasis_count,
         drop_fillers, aggressive_fillers, intro_title, clear_template_texts,
         ai_model, use_ai_review, ai_target_duration,
         model, language, subtitle_like_template, subtitle_max_words,
         subtitle_max_duration, fmt, projects_dir):
    """Genera un draft CapCut da INPUT_PATH (file video o cartella di video).
    Il montaggio lo decide Claude: serve il CLI `claude` loggato o ANTHROPIC_API_KEY."""
    script_text = read_script(script) if script else None
    try:
        run_pipeline(
            input_path=input_path, name=name, template=template,
            script=script_text, pacing=pacing,
            stabilize_clips=stabilize_clips,
            redistribute_sfx_enabled=redistribute_sfx_enabled,
            add_emphasis=add_emphasis, emphasis_count=emphasis_count,
            drop_fillers=drop_fillers, aggressive_fillers=aggressive_fillers,
            intro_title=intro_title, clear_template_texts=clear_template_texts,
            ai_model=ai_model, use_ai_review=use_ai_review,
            ai_target_duration=ai_target_duration,
            model=model, language=language,
            subtitle_max_words=subtitle_max_words,
            subtitle_max_duration=subtitle_max_duration,
            subtitle_like_template=subtitle_like_template,
            fmt=fmt, projects_dir=projects_dir,
            log=click.echo,
        )
    except ValueError as e:
        raise click.ClickException(str(e))


if __name__ == "__main__":
    main()
