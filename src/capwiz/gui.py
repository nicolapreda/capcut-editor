"""Modern Tk GUI (customtkinter): tabs for Input, Pacing, Style & Effects, Output."""
from __future__ import annotations

import queue
import threading
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from .cuts import PACING_BY_NAME
from .draft import CAPCUT_PROJECTS_DIR
from .pipeline import read_script, run_pipeline


ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# On macOS Retina, customtkinter's auto-scaling can offset click hit-targets
# from the visible widget by a few pixels, making everything feel "off". Pin
# both scales to 1.0 so click coordinates line up with what's drawn.
ctk.set_widget_scaling(1.0)
ctk.set_window_scaling(1.0)

# Larger defaults — bigger buttons, taller switches, taller sliders.
# On a trackpad this is the difference between fighting the UI and just using it.
BTN_H = 38
ENTRY_H = 34
SWITCH_W = 52
SWITCH_H = 26
SLIDER_H = 22


def _list_templates(projects_dir: Path) -> list[str]:
    if not projects_dir.exists():
        return []
    return sorted(
        d.name for d in projects_dir.iterdir()
        if d.is_dir() and (d / "draft_info.json").exists()
    )


class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("CapWiz · Generatore Reel")
        self.geometry("980x820")
        self.minsize(900, 720)

        self.log_queue: queue.Queue[str | None] = queue.Queue()
        self.worker: threading.Thread | None = None

        # --- header -----------------------------------------------------------
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=18, pady=(18, 6))
        ctk.CTkLabel(header, text="CapWiz",
                     font=ctk.CTkFont(size=24, weight="bold")).pack(side="left")
        ctk.CTkLabel(header, text="Reel automatici da una cartella di video",
                     font=ctk.CTkFont(size=13),
                     text_color=("gray60", "gray60")).pack(side="left", padx=(12, 0), pady=(8, 0))

        # --- tabbed body ------------------------------------------------------
        self.tabs = ctk.CTkTabview(self, height=480)
        self.tabs.pack(fill="x", padx=18, pady=8)
        self.tabs.add("Input")
        self.tabs.add("Tagli e ritmo")
        self.tabs.add("Stile & effetti")
        self.tabs.add("Output")

        self._build_input_tab(self.tabs.tab("Input"))
        self._build_pacing_tab(self.tabs.tab("Tagli e ritmo"))
        self._build_style_tab(self.tabs.tab("Stile & effetti"))
        self._build_output_tab(self.tabs.tab("Output"))

        # --- action bar -------------------------------------------------------
        action = ctk.CTkFrame(self, fg_color="transparent")
        action.pack(fill="x", padx=18, pady=(0, 6))
        self.go_btn = ctk.CTkButton(action, text="GENERA PROGETTO",
                                    height=44, font=ctk.CTkFont(size=15, weight="bold"),
                                    command=self._start)
        self.go_btn.pack(fill="x")

        # --- log --------------------------------------------------------------
        log_frame = ctk.CTkFrame(self)
        log_frame.pack(fill="both", expand=True, padx=18, pady=(8, 18))
        ctk.CTkLabel(log_frame, text="Log",
                     font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w", padx=12, pady=(8, 0))
        self.log = ctk.CTkTextbox(log_frame, font=ctk.CTkFont(family="Menlo", size=12))
        self.log.pack(fill="both", expand=True, padx=12, pady=8)
        self.log.configure(state="disabled")

        self.after(100, self._drain_log)

    # ------------------------------------------------------------------ tabs

    def _build_input_tab(self, tab: ctk.CTkFrame) -> None:
        tab.columnconfigure(1, weight=1)
        row = 0

        ctk.CTkLabel(tab, text="Sorgente:").grid(row=row, column=0, sticky="w", padx=10, pady=(14, 4))
        self.folder_var = ctk.StringVar()
        ctk.CTkEntry(tab, textvariable=self.folder_var, height=ENTRY_H,
                     placeholder_text="cartella o file video"
                     ).grid(row=row, column=1, sticky="we", padx=(0, 8), pady=(14, 4))
        btns = ctk.CTkFrame(tab, fg_color="transparent")
        btns.grid(row=row, column=2, padx=(0, 10), pady=(14, 4))
        ctk.CTkButton(btns, text="Cartella…", width=110, height=BTN_H,
                      command=self._pick_folder).pack(side="left", padx=2)
        ctk.CTkButton(btns, text="File…", width=84, height=BTN_H,
                      command=self._pick_file).pack(side="left", padx=2)
        row += 1

        ctk.CTkLabel(tab, text="Nome progetto:").grid(row=row, column=0, sticky="w", padx=10, pady=4)
        self.name_var = ctk.StringVar()
        ctk.CTkEntry(tab, textvariable=self.name_var, height=ENTRY_H,
                     placeholder_text="es. Reel 23 maggio"
                     ).grid(row=row, column=1, columnspan=2, sticky="we", padx=(0, 10), pady=4)
        row += 1

        ctk.CTkLabel(tab, text="Template CapCut:").grid(row=row, column=0, sticky="w", padx=10, pady=4)
        templates = _list_templates(CAPCUT_PROJECTS_DIR)
        default_template = next((t for t in templates if t.upper() == "CASA RIFUGIO"), "(nessuno)")
        self.template_var = ctk.StringVar(value=default_template)
        self.template_menu = ctk.CTkOptionMenu(
            tab, variable=self.template_var,
            values=["(nessuno)"] + templates,
            width=340, height=BTN_H,
        )
        self.template_menu.grid(row=row, column=1, columnspan=2, sticky="w", padx=(0, 10), pady=4)
        row += 1
        ctk.CTkLabel(tab, text="Musica, SFX e stile sottotitoli vengono ereditati dal template.",
                     text_color=("gray55", "gray55"),
                     font=ctk.CTkFont(size=11)).grid(row=row, column=1, columnspan=2, sticky="w", padx=(0, 10), pady=(0, 8))
        row += 1

        ctk.CTkLabel(tab, text="Script (opz.):").grid(row=row, column=0, sticky="w", padx=10, pady=4)
        self.script_var = ctk.StringVar()
        ctk.CTkEntry(tab, textvariable=self.script_var, height=ENTRY_H,
                     placeholder_text="Tiene solo le parti che matchano lo script"
                     ).grid(row=row, column=1, sticky="we", padx=(0, 8), pady=4)
        ctk.CTkButton(tab, text="Sfoglia…", width=110, height=BTN_H, command=self._pick_script
                      ).grid(row=row, column=2, padx=(0, 10), pady=4)
        row += 1

        ctk.CTkLabel(tab, text="Titolo iniziale (opz.):").grid(row=row, column=0, sticky="w", padx=10, pady=(8, 4))
        self.intro_title_var = ctk.StringVar()
        ctk.CTkEntry(tab, textvariable=self.intro_title_var, height=ENTRY_H,
                     placeholder_text="es. CASA INVASA  →  sostituisce i titoli iniziali del template"
                     ).grid(row=row, column=1, columnspan=2, sticky="we", padx=(0, 10), pady=(8, 4))
        row += 1
        ctk.CTkLabel(tab,
                     text="Più parole vengono spalmate sui titoli multipli (es. su CASA RIFUGIO: 'CASA' + 'RIFUGIO').",
                     text_color=("gray55", "gray55"),
                     font=ctk.CTkFont(size=11)
                     ).grid(row=row, column=1, columnspan=2, sticky="w", padx=(0, 10), pady=(0, 8))
        row += 1

    def _build_pacing_tab(self, tab: ctk.CTkFrame) -> None:
        tab.columnconfigure(1, weight=1)
        row = 0

        ctk.CTkLabel(tab, text="Pacing:").grid(row=row, column=0, sticky="w", padx=10, pady=(14, 4))
        self.pacing_var = ctk.StringVar(value="fast")
        seg = ctk.CTkSegmentedButton(tab, values=["normal", "fast", "aggressive"],
                                     variable=self.pacing_var, height=BTN_H,
                                     command=self._on_pacing_changed)
        seg.grid(row=row, column=1, columnspan=2, sticky="w", padx=(0, 10), pady=(14, 4))
        row += 1

        self.pacing_desc = ctk.CTkLabel(tab, text="", text_color=("gray55", "gray55"),
                                        font=ctk.CTkFont(size=11), justify="left")
        self.pacing_desc.grid(row=row, column=1, columnspan=2, sticky="w", padx=(0, 10), pady=(0, 14))
        self._on_pacing_changed("fast")
        row += 1

        # subtitle chunking
        ctk.CTkLabel(tab, text="Parole per sottotitolo:").grid(row=row, column=0, sticky="w", padx=10, pady=4)
        self.sub_words_var = ctk.IntVar(value=4)
        self.sub_words_label = ctk.CTkLabel(tab, text="4")
        slider = ctk.CTkSlider(tab, from_=2, to=8, number_of_steps=6, height=SLIDER_H,
                               command=lambda v: (self.sub_words_var.set(int(v)),
                                                  self.sub_words_label.configure(text=str(int(v)))))
        slider.set(4)
        slider.grid(row=row, column=1, sticky="we", padx=(0, 8), pady=4)
        self.sub_words_label.grid(row=row, column=2, padx=(0, 10), pady=4)
        row += 1

        ctk.CTkLabel(tab, text="Durata max sottotitolo (s):").grid(row=row, column=0, sticky="w", padx=10, pady=4)
        self.sub_dur_var = ctk.DoubleVar(value=1.4)
        self.sub_dur_label = ctk.CTkLabel(tab, text="1.4 s")
        slider2 = ctk.CTkSlider(tab, from_=0.8, to=3.0, number_of_steps=22, height=SLIDER_H,
                                command=lambda v: (self.sub_dur_var.set(round(v, 1)),
                                                   self.sub_dur_label.configure(text=f"{v:.1f} s")))
        slider2.set(1.4)
        slider2.grid(row=row, column=1, sticky="we", padx=(0, 8), pady=4)
        self.sub_dur_label.grid(row=row, column=2, padx=(0, 10), pady=4)
        row += 1

        # --- cleanup switches ---
        sep = ctk.CTkFrame(tab, height=2, fg_color=("gray80", "gray25"))
        sep.grid(row=row, column=0, columnspan=3, sticky="we", padx=10, pady=(14, 6))
        row += 1
        ctk.CTkLabel(tab, text="Pulizia automatica",
                     font=ctk.CTkFont(size=12, weight="bold")).grid(
            row=row, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 4))
        row += 1
        self.drop_fillers_var = ctk.BooleanVar(value=True)
        ctk.CTkSwitch(tab, text="Rimuovi filler word (ehm, uhm, ah…)",
                      variable=self.drop_fillers_var,
                      switch_width=SWITCH_W, switch_height=SWITCH_H,
                      ).grid(row=row, column=0, columnspan=3, sticky="w", padx=14, pady=6)
        row += 1
        self.aggressive_fillers_var = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(tab, text="Rimuovi anche cioè/tipo/diciamo/insomma (aggressivo)",
                      variable=self.aggressive_fillers_var,
                      switch_width=SWITCH_W, switch_height=SWITCH_H,
                      ).grid(row=row, column=0, columnspan=3, sticky="w", padx=14, pady=6)
        row += 1

    def _build_style_tab(self, tab: ctk.CTkFrame) -> None:
        tab.columnconfigure(0, weight=1)
        row = 0

        self.redistribute_var = ctk.BooleanVar(value=True)
        ctk.CTkSwitch(tab, text="Ridistribuisci SFX sui nuovi tagli del video",
                      variable=self.redistribute_var,
                      switch_width=SWITCH_W, switch_height=SWITCH_H,
                      ).grid(row=row, column=0, sticky="w", padx=14, pady=(14, 6))
        row += 1
        ctk.CTkLabel(tab, text="Sposta whoosh/swish/riser sui punti di stacco, allunga la musica di sottofondo.",
                     text_color=("gray55", "gray55"),
                     font=ctk.CTkFont(size=11)).grid(row=row, column=0, sticky="w", padx=36, pady=(0, 10))
        row += 1

        self.emphasis_var = ctk.BooleanVar(value=True)
        ctk.CTkSwitch(tab, text="Aggiungi testo di enfasi sui punti chiave",
                      variable=self.emphasis_var,
                      switch_width=SWITCH_W, switch_height=SWITCH_H,
                      ).grid(row=row, column=0, sticky="w", padx=14, pady=(8, 6))
        row += 1
        emph_row = ctk.CTkFrame(tab, fg_color="transparent")
        emph_row.grid(row=row, column=0, sticky="w", padx=36, pady=(0, 10))
        ctk.CTkLabel(emph_row, text="Quantità massima:",
                     text_color=("gray55", "gray55"), font=ctk.CTkFont(size=11)).pack(side="left")
        self.emphasis_count_var = ctk.IntVar(value=4)
        ctk.CTkOptionMenu(emph_row, variable=self.emphasis_count_var,
                          values=[str(i) for i in (0, 2, 3, 4, 5, 6, 8)],
                          command=lambda v: self.emphasis_count_var.set(int(v)),
                          width=84, height=BTN_H).pack(side="left", padx=8)
        row += 1

        self.clear_template_texts_var = ctk.BooleanVar(value=True)
        ctk.CTkSwitch(tab, text="Rimuovi i testi residui del template (es. '3, 2, 1, vai!')",
                      variable=self.clear_template_texts_var,
                      switch_width=SWITCH_W, switch_height=SWITCH_H,
                      ).grid(row=row, column=0, sticky="w", padx=14, pady=(8, 6))
        row += 1
        ctk.CTkLabel(tab, text="Tiene solo le emoji decorative. Lo consiglio se cambi argomento.",
                     text_color=("gray55", "gray55"),
                     font=ctk.CTkFont(size=11)).grid(row=row, column=0, sticky="w", padx=36, pady=(0, 10))
        row += 1

        # --- AI block ---
        sep_ai = ctk.CTkFrame(tab, height=2, fg_color=("gray80", "gray25"))
        sep_ai.grid(row=row, column=0, sticky="we", padx=10, pady=(14, 6))
        row += 1
        ctk.CTkLabel(tab, text="AI (Claude)",
                     font=ctk.CTkFont(size=13, weight="bold")
                     ).grid(row=row, column=0, sticky="w", padx=10, pady=(0, 4))
        row += 1
        ctk.CTkLabel(tab,
                     text="Richiede il `claude` CLI (npm install -g @anthropic-ai/claude-code) o ANTHROPIC_API_KEY in env.",
                     text_color=("gray55", "gray55"),
                     font=ctk.CTkFont(size=11)
                     ).grid(row=row, column=0, sticky="w", padx=14, pady=(0, 6))
        row += 1

        ai_dur_row = ctk.CTkFrame(tab, fg_color="transparent")
        ai_dur_row.grid(row=row, column=0, sticky="w", padx=36, pady=(0, 6))
        ctk.CTkLabel(ai_dur_row, text="Durata target reel:",
                     text_color=("gray55", "gray55"),
                     font=ctk.CTkFont(size=11)).pack(side="left")
        self.ai_target_var = ctk.StringVar(value="auto")
        ctk.CTkOptionMenu(ai_dur_row, variable=self.ai_target_var,
                          values=["auto", "15", "20", "30", "45", "60", "90"],
                          width=84, height=BTN_H).pack(side="left", padx=8)
        ctk.CTkLabel(ai_dur_row, text="sec",
                     text_color=("gray55", "gray55"),
                     font=ctk.CTkFont(size=11)).pack(side="left")
        row += 1

        self.use_ai_review_var = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(tab, text="Recensione finale del montato (consigli per migliorare)",
                      variable=self.use_ai_review_var,
                      switch_width=SWITCH_W, switch_height=SWITCH_H,
                      ).grid(row=row, column=0, sticky="w", padx=14, pady=6)
        row += 1

        self.stabilize_var = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(tab, text="Stabilizza le clip con ffmpeg (vidstab)",
                      variable=self.stabilize_var,
                      switch_width=SWITCH_W, switch_height=SWITCH_H,
                      ).grid(row=row, column=0, sticky="w", padx=14, pady=(8, 6))
        row += 1
        ctk.CTkLabel(tab, text="Più lento ma ottimo per riprese a mano. Risultati in cache, rifare costa zero.",
                     text_color=("gray55", "gray55"),
                     font=ctk.CTkFont(size=11)).grid(row=row, column=0, sticky="w", padx=36, pady=(0, 10))
        row += 1

    def _build_output_tab(self, tab: ctk.CTkFrame) -> None:
        tab.columnconfigure(1, weight=1)
        row = 0

        ctk.CTkLabel(tab, text="Modello Whisper:").grid(row=row, column=0, sticky="w", padx=10, pady=(14, 4))
        self.model_var = ctk.StringVar(value="large-v3")
        ctk.CTkOptionMenu(tab, variable=self.model_var,
                          values=["tiny", "base", "small", "medium", "large-v3"],
                          width=180, height=BTN_H
                          ).grid(row=row, column=1, sticky="w", padx=(0, 10), pady=(14, 4))
        row += 1
        ctk.CTkLabel(tab, text="`large-v3` (default) è il più accurato. Al primo uso scarica ~3 GB.",
                     text_color=("gray55", "gray55"),
                     font=ctk.CTkFont(size=11)).grid(row=row, column=1, columnspan=2, sticky="w", padx=(0, 10), pady=(0, 10))
        row += 1

        ctk.CTkLabel(tab, text="Lingua:").grid(row=row, column=0, sticky="w", padx=10, pady=4)
        self.language_var = ctk.StringVar(value="it")
        ctk.CTkOptionMenu(tab, variable=self.language_var,
                          values=["it", "en", "es", "fr", "de", "pt"],
                          width=100, height=BTN_H
                          ).grid(row=row, column=1, sticky="w", padx=(0, 10), pady=4)
        row += 1

        ctk.CTkLabel(tab, text="Formato:").grid(row=row, column=0, sticky="w", padx=10, pady=4)
        self.format_var = ctk.StringVar(value="vertical")
        ctk.CTkSegmentedButton(tab, values=["vertical", "horizontal"],
                               variable=self.format_var, height=BTN_H,
                               ).grid(row=row, column=1, sticky="w", padx=(0, 10), pady=4)
        row += 1
        ctk.CTkLabel(tab, text="Ignorato se usi un template (eredita il canvas del template).",
                     text_color=("gray55", "gray55"),
                     font=ctk.CTkFont(size=11)).grid(row=row, column=1, columnspan=2, sticky="w", padx=(0, 10), pady=(0, 10))
        row += 1

    # ------------------------------------------------------------------ helpers

    def _on_pacing_changed(self, name: str) -> None:
        p = PACING_BY_NAME.get(name)
        if p is None:
            return
        self.pacing_desc.configure(
            text=(f"silenzi ≥ {p.min_silence:.2f}s tagliati  ·  "
                  f"pad iniziale {p.head_pad:.2f}s, finale {p.tail_pad:.2f}s")
        )

    def _pick_folder(self) -> None:
        p = filedialog.askdirectory(title="Seleziona cartella video")
        if p:
            self.folder_var.set(p)

    def _pick_file(self) -> None:
        p = filedialog.askopenfilename(
            title="Seleziona video",
            filetypes=[("Video", "*.mp4 *.mov *.mkv *.webm *.m4v"), ("Tutti", "*.*")],
        )
        if p:
            self.folder_var.set(p)

    def _pick_script(self) -> None:
        p = filedialog.askopenfilename(
            title="Seleziona script",
            filetypes=[("Script", "*.txt *.md *.docx"), ("Word", "*.docx"),
                       ("Testo", "*.txt *.md"), ("Tutti", "*.*")],
        )
        if p:
            self.script_var.set(p)

    # ------------------------------------------------------------------ worker

    def _start(self) -> None:
        folder = self.folder_var.get().strip()
        name = self.name_var.get().strip()
        if not folder:
            messagebox.showerror("Errore", "Seleziona una cartella o un file video.")
            return
        if not name:
            messagebox.showerror("Errore", "Inserisci un nome per il progetto.")
            return

        template = self.template_var.get().strip()
        if template == "(nessuno)" or not template:
            template = None
        script_path = self.script_var.get().strip()
        script_text = read_script(Path(script_path)) if script_path else None

        kwargs = dict(
            input_path=Path(folder),
            name=name,
            template=template,
            script=script_text,
            model=self.model_var.get(),
            language=self.language_var.get(),
            pacing=self.pacing_var.get(),
            stabilize_clips=self.stabilize_var.get(),
            redistribute_sfx_enabled=self.redistribute_var.get(),
            add_emphasis=self.emphasis_var.get(),
            emphasis_count=int(self.emphasis_count_var.get()),
            subtitle_max_words=int(self.sub_words_var.get()),
            subtitle_max_duration=float(self.sub_dur_var.get()),
            drop_fillers=self.drop_fillers_var.get(),
            aggressive_fillers=self.aggressive_fillers_var.get(),
            intro_title=(self.intro_title_var.get().strip() or None),
            clear_template_texts=self.clear_template_texts_var.get(),
            use_ai_review=self.use_ai_review_var.get(),
            ai_target_duration=(float(self.ai_target_var.get())
                                if self.ai_target_var.get() != "auto" else None),
            fmt=self.format_var.get(),
        )

        self._log_clear()
        self.go_btn.configure(state="disabled", text="Lavoro in corso…")

        def _run() -> None:
            try:
                run_pipeline(log=self._log, **kwargs)
                self._log("\nDONE. Apri CapCut: il progetto è nella tua lista draft.")
            except Exception as e:  # noqa: BLE001
                self._log(f"\nERRORE: {e}")
            finally:
                self.log_queue.put(None)

        self.worker = threading.Thread(target=_run, daemon=True)
        self.worker.start()

    # ------------------------------------------------------------------ log

    def _log(self, msg: str) -> None:
        self.log_queue.put(msg)

    def _log_clear(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _drain_log(self) -> None:
        try:
            while True:
                msg = self.log_queue.get_nowait()
                if msg is None:
                    self.go_btn.configure(state="normal", text="GENERA PROGETTO")
                else:
                    self.log.configure(state="normal")
                    self.log.insert("end", msg + "\n")
                    self.log.see("end")
                    self.log.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self._drain_log)


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
