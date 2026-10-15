"""工作台配色；只影响展示，不改变运行时或会话数据。"""

from textual.theme import Theme


THEME_LABELS = (
    ("经典深色", "codefox-dark"),
    ("午夜蓝", "codefox-midnight"),
    ("暖灰", "codefox-warm"),
)
DEFAULT_THEME = "codefox-dark"

THEMES = (
    Theme(name="codefox-dark", primary="#80cbc4", foreground="#e6edf3",
          secondary="#93a4b8", accent="#80cbc4", warning="#e6c36a",
          error="#ff9b9b", success="#8fd5a1",
          background="#151719", surface="#202326", panel="#191b1d",
          variables={"codefox-muted": "#89939f", "codefox-border": "#55595d",
                     "codefox-error-background": "#321d1d"}),
    Theme(name="codefox-midnight", primary="#89b4fa", foreground="#e3eaf7",
          secondary="#b4befe", accent="#89dceb", warning="#f9d88a",
          error="#f4a0b2", success="#9ad5bd",
          background="#111827", surface="#1e293b", panel="#172033",
          variables={"codefox-muted": "#8795ad", "codefox-border": "#475569",
                     "codefox-error-background": "#342333"}),
    Theme(name="codefox-warm", primary="#d8b890", foreground="#efe7db",
          secondary="#c9aaa0", accent="#e7c7a0", warning="#e5bf7a",
          error="#eda59a", success="#b2cb9b",
          background="#1c1917", surface="#292524", panel="#24201d",
          variables={"codefox-muted": "#a1978a", "codefox-border": "#63594e",
                     "codefox-error-background": "#3c2621"}),
)
