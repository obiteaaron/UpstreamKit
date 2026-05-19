from tkinter import StringVar, Toplevel, ttk

from upstreamkit_core import DEFAULT_UPSTREAM


class UpstreamEditDialog:
    def __init__(self, parent, title, initial=None):
        self.result = None
        self.dialog = Toplevel(parent)
        self.dialog.title(title)
        self.dialog.geometry("420x280")
        self.dialog.transient(parent)
        self.dialog.grab_set()

        frame = ttk.Frame(self.dialog, padding=14)
        frame.pack(fill="both", expand=True)

        initial = initial or dict(DEFAULT_UPSTREAM)

        self.name_var = StringVar(value=initial.get("name", "新上游"))
        self.provider_var = StringVar(value=initial.get("provider", "openai"))
        self.url_var = StringVar(value=initial.get("base_url", "https://api.openai.com"))
        self.key_var = StringVar(value=initial.get("api_key", ""))
        self.model_var = StringVar(value=initial.get("model", "gpt-4.1"))

        ttk.Label(frame, text="名称").grid(row=0, column=0, sticky="w", pady=6)
        ttk.Entry(frame, textvariable=self.name_var, width=40).grid(row=0, column=1, sticky="ew", pady=6)

        ttk.Label(frame, text="类型").grid(row=1, column=0, sticky="w", pady=6)
        provider_box = ttk.Combobox(frame, textvariable=self.provider_var, values=["openai", "anthropic"], state="readonly", width=18)
        provider_box.grid(row=1, column=1, sticky="w", pady=6)

        ttk.Label(frame, text="URL").grid(row=2, column=0, sticky="w", pady=6)
        ttk.Entry(frame, textvariable=self.url_var, width=40).grid(row=2, column=1, sticky="ew", pady=6)

        ttk.Label(frame, text="Key").grid(row=3, column=0, sticky="w", pady=6)
        ttk.Entry(frame, textvariable=self.key_var, show="*", width=40).grid(row=3, column=1, sticky="ew", pady=6)

        ttk.Label(frame, text="模型").grid(row=4, column=0, sticky="w", pady=6)
        ttk.Entry(frame, textvariable=self.model_var, width=40).grid(row=4, column=1, sticky="ew", pady=6)

        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=5, column=0, columnspan=2, sticky="e", pady=14)
        ttk.Button(btn_frame, text="确定", command=self.on_ok, width=10).pack(side="left", padx=6)
        ttk.Button(btn_frame, text="取消", command=self.on_cancel, width=10).pack(side="left", padx=6)

        frame.columnconfigure(1, weight=1)
        self.dialog.wait_window()

    def on_ok(self):
        name = self.name_var.get().strip()
        if not name:
            name = "未命名上游"
        self.result = {
            "name": name,
            "provider": self.provider_var.get(),
            "base_url": self.url_var.get().strip(),
            "api_key": self.key_var.get().strip(),
            "model": self.model_var.get().strip(),
        }
        self.dialog.destroy()

    def on_cancel(self):
        self.dialog.destroy()


