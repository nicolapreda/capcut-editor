"""Modern Tk GUI (customtkinter): tabs for Input, Pacing, Style & Effects, Output."""
from __future__ import annotations

import queue
import threading
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from .cuts import PACING_BY_NAME
from .draft import CAPCUT_PROJECTS_DIR
from .pipeline import run_pipeline


ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


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
        self.title("CapCut Auto · Generatore Reel")
        self.geometry("920x720")
        self.minsize(820, 640)

        self.log_queue: queue.Queue[str | None] = queue.Queue()
        self.worker: threading.Thread | None = None

        # --- header -----------------------------------------------------------
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=18, pady=(18, 6))
        ctk.CTkLabel(header, text="CapCut Auto",
                     font=ctk.CTkFont(size=24, weight="bold")).pack(side="left")
        ctk.CTkLabel(header, text="Reel automatici da una cartella di video",
                     font=ctk.CTkFont(size=13),
                     text_color=("gray60", "gray60")).pack(side="left", padx=(12, 0), pady=(8, 0))

        # --- tabbed body ------------------------------------------------------
        self.tabs = ctk.CTkTabview(self, height=420)
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
        ctk.CTkEntry(tab, textvariable=self.folder_var, placeholder_text="cartella o file video"
                     ).grid(row=row, column=1, sticky="we", padx=(0, 8), pady=(14, 4))
        btns = ctk.CTkFrame(tab, fg_color="transparent")
        btns.grid(row=row, column=2, padx=(0, 10), pady=(14, 4))
        ctk.CTkButton(btns, text="Cartella…", width=90, command=self._pick_folder).pack(side="left", padx=2)
        ctk.CTkButton(btns, text="File…", width=70, command=self._pick_file).pack(side="left", padx=2)
        row += 1

        ctk.CTkLabel(tab, text="Nome progetto:").grid(row=row, column=0, sticky="w", padx=10, pady=4)
        self.name_var = ctk.StringVar()
        ctk.CTkEntry(tab, textvariable=self.name_var, placeholder_text="es. Reel 23 maggio"
                     ).grid(row=row, column=1, columnspan=2, sticky="we", padx=(0, 10), pady=4)
        row += 1

        ctk.CTkLabel(tab, text="Template CapCut:").grid(row=row, column=0, sticky="w", padx=10, pady=4)
        templates = _list_templates(CAPCUT_PROJECTS_DIR)
        default_template = next((t for t in templates if t.upper() == "CASA RIFUGIO"), "(nessuno)")
        self.template_var = ctk.StringVar(value=default_template)
        self.template_menu = ctk.CTkOptionMenu(
            tab, variable=self.template_var,
            values=["(nessuno)"] + templates,
            width=320,
        )
        self.template_menu.grid(row=row, column=1, columnspan=2, sticky="w", padx=(0, 10), pady=4)
        row += 1
        ctk.CTkLabel(tab, text="Musica, SFX e stile sottotitoli vengono ereditati dal template.",
                     text_color=("gray55", "gray55"),
                     font=ctk.CTkFont(size=11)).grid(row=row, column=1, columnspan=2, sticky="w", padx=(0, 10), pady=(0, 8))
        row += 1

        ctk.CTkLabel(tab, text="Script (opz.):").grid(row=row, column=0, sticky="w", padx=10, pady=4)
        self.script_var = ctk.StringVar()
        ctk.CTkEntry(tab, textvariable=self.script_var, placeholder_text="Tiene solo le parti che matchano lo script"
                     ).grid(row=row, column=1, sticky="we", padx=(0, 8), pady=4)
        ctk.CTkButton(tab, text="Sfoglia…", width=90, command=self._pick_script
                      ).grid(row=row, column=2, padx=(0, 10), pady=4)
        row += 1

    def _build_pacing_tab(self, tab: ctk.CTkFrame) -> None:
        tab.columnconfigure(1, weight=1)
        row = 0

        ctk.CTkLabel(tab, text="Pacing:").grid(row=row, column=0, sticky="w", padx=10, pady=(14, 4))
        self.pacing_var = ctk.StringVar(value="fast")
        seg = ctk.CTkSegmentedButton(tab, values=["normal", "fast", "aggressive"],
                                     variable=self.pacing_var,
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
        slider = ctk.CTkSlider(tab, from_=2, to=8, number_of_steps=6,
                               command=lambda v: (self.sub_words_var.set(int(v)),
                                                  self.sub_words_label.configure(text=str(int(v)))))
        slider.set(4)
        slider.grid(row=row, column=1, sticky="we", padx=(0, 8), pady=4)
        self.sub_words_label.grid(row=row, column=2, padx=(0, 10), pady=4)
        row += 1

        ctk.CTkLabel(tab, text="Durata max sottotitolo (s):").grid(row=row, column=0, sticky="w", padx=10, pady=4)
        self.sub_dur_var = ctk.DoubleVar(value=1.4)
        self.sub_dur_label = ctk.CTkLabel(tab, text="1.4 s")
        slider2 = ctk.CTkSlider(tab, from_=0.8, to=3.0, number_of_steps=22,
                                command=lambda v: (self.sub_dur_var.set(round(v, 1)),
                                                   self.sub_dur_label.configure(text=f"{v:.1f} s")))
        slider2.set(1.4)
        slider2.grid(row=row, column=1, sticky="we", padx=(0, 8), pady=4)
        self.sub_dur_label.grid(row=row, column=2, padx=(0, 10), pady=4)
        row += 1

    def _build_style_tab(self, tab: ctk.CTkFrame) -> None:
        tab.columnconfigure(0, weight=1)
        row = 0

        self.redistribute_var = ctk.BooleanVar(value=True)
        ctk.CTkSwitch(tab, text="Ridistribuisci SFX sui nuovi tagli del video",
                      variable=self.redistribute_var).grid(row=row, column=0, sticky="w", padx=14, pady=(14, 4))
        row += 1
        ctk.CTkLabel(tab, text="Sposta whoosh/swish/riser sui punti di stacco, allunga la musica di sottofondo.",
                     text_color=("gray55", "gray55"),
                     font=ctk.CTkFont(size=11)).grid(row=row, column=0, sticky="w", padx=36, pady=(0, 10))
        row += 1

        self.emphasis_var = ctk.BooleanVar(value=True)
        ctk.CTkSwitch(tab, text="Aggiungi testo di enfasi sui punti chiave",
                      variable=self.emphasis_var).grid(row=row, column=0, sticky="w", padx=14, pady=(8, 4))
        row += 1
        emph_row = ctk.CTkFrame(tab, fg_color="transparent")
        emph_row.grid(row=row, column=0, sticky="w", padx=36, pady=(0, 10))
        ctk.CTkLabel(emph_row, text="Quantità massima:",
                     text_color=("gray55", "gray55"), font=ctk.CTkFont(size=11)).pack(side="left")
        self.emphasis_count_var = ctk.IntVar(value=4)
        ctk.CTkOptionMenu(emph_row, variable=self.emphasis_count_var,
                          values=[str(i) for i in (0, 2, 3, 4, 5, 6, 8)],
                          command=lambda v: self.emphasis_count_var.set(int(v)),
                          width=70).pack(side="left", padx=8)
        row += 1

        self.stabilize_var = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(tab, text="Attiva la stabilizzazione integrata di CapCut",
                      variable=self.stabilize_var).grid(row=row, column=0, sticky="w", padx=14, pady=(8, 4))
        row += 1
        ctk.CTkLabel(tab, text="Ottima per riprese a mano. È CapCut a calcolarla all'apertura del progetto.",
                     text_color=("gray55", "gray55"),
                     font=ctk.CTkFont(size=11)).grid(row=row, column=0, sticky="w", padx=36, pady=(0, 10))
        row += 1

    def _build_output_tab(self, tab: ctk.CTkFrame) -> None:
        tab.columnconfigure(1, weight=1)
        row = 0

        ctk.CTkLabel(tab, text="Modello Whisper:").grid(row=row, column=0, sticky="w", padx=10, pady=(14, 4))
        self.model_var = ctk.StringVar(value="small")
        ctk.CTkOptionMenu(tab, variable=self.model_var,
                          values=["tiny", "base", "small", "medium", "large-v3"],
                          width=160
                          ).grid(row=row, column=1, sticky="w", padx=(0, 10), pady=(14, 4))
        row += 1
        ctk.CTkLabel(tab, text="`small` è un buon compromesso. `medium`/`large` migliorano l'accuratezza.",
                     text_color=("gray55", "gray55"),
                     font=ctk.CTkFont(size=11)).grid(row=row, column=1, columnspan=2, sticky="w", padx=(0, 10), pady=(0, 10))
        row += 1

        ctk.CTkLabel(tab, text="Lingua:").grid(row=row, column=0, sticky="w", padx=10, pady=4)
        self.language_var = ctk.StringVar(value="it")
        ctk.CTkOptionMenu(tab, variable=self.language_var,
                          values=["it", "en", "es", "fr", "de", "pt"],
                          width=80
                          ).grid(row=row, column=1, sticky="w", padx=(0, 10), pady=4)
        row += 1

        ctk.CTkLabel(tab, text="Formato:").grid(row=row, column=0, sticky="w", padx=10, pady=4)
        self.format_var = ctk.StringVar(value="vertical")
        ctk.CTkSegmentedButton(tab, values=["vertical", "horizontal"],
                               variable=self.format_var
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
            filetypes=[("Testo", "*.txt *.md"), ("Tutti", "*.*")],
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
        script_text = Path(script_path).read_text() if script_path else None

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
